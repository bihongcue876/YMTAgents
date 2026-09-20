"""会话记忆（spec v0.0.1：记忆文件 / 记忆压缩 / 外置记忆）。

记忆的本质是**概括**：把较早的对话概括成一份 **AGENTS.md 风格**的记忆文件
（`sessions/<id>/branches/<bid>/memory.md`），供后续对话继续使用；
`events.jsonl` 只增不改；被替换的旧记忆归档到 `memory/history/<rev>.md`（可查看、不注入）。

- 专用提示词：`config/memory_prompt.md`（缺失时落内置默认，**不叠加 persona**）。
- 触发：用户点按钮或达阈值（本模块只负责执行与选择）。
- 选择面：从「上次记忆覆盖点」之后到「尾部保留预算」之前的消息进记忆；
  近段按 token 预算保留，**不是**固定「最近 N 轮」。
- 目标：按 `target_ratio` 推导**推荐范围**（不设绝对目标、不硬钳窗口）。
- 失败安全：任何异常都不写文件、由上层转成可读错误，原对话不受影响。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from shared.redact import redact
from shared.schema import MemoryConfig

from core.agent.context import estimate_tokens
from core.store.atomic import atomic_write_text

#: 内置默认记忆提示词（v0.0.1）。
#: 风格对齐 AGENTS.md：自由文本为主、只对「已概括的部分」做**轻度**小节组织，
#: 明确要求不要过度结构化（用户裁决：太结构化是有问题的）。
DEFAULT_MEMORY_PROMPT = (
    "你是一个会话记忆整理器。请把下面这段**较早的对话**整理为一份「会话记忆」笔记，"
    "供后续对话继续使用。要求：\n"
    "1. 只保留对后续对话有用的事实，不得编造、不得加入原文没有的信息；\n"
    "2. 整体写成自然、连贯的中文笔记（AGENTS.md 风格），不要写成字段堆砌或表格；\n"
    "3. 可对**已概括的部分**做轻度小节组织，例如「目标」「已确定」「事实与偏好」"
    "「未决与下一步」，但不要追求固定模板，宁可少分节也不要过度结构化；\n"
    "4. 保留结论、决定、关键事实、偏好与约束、未决问题；省略寒暄与过程。\n"
)

#: 尾部保留下限（窗口未知或很小时），避免把近段也压进去。
_MIN_KEEP = 1024


def memory_prompt_path(root: Path) -> Path:
    return Path(root) / "config" / "memory_prompt.md"


def load_memory_prompt(root: Path, ensure: bool = True) -> str:
    """读取专用记忆提示词；缺失时落内置默认（`ensure=True`）并返回。"""
    path = memory_prompt_path(root)
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8")
            if text.strip():
                return text
        except OSError:
            pass
    if ensure:
        try:
            # 配置资源同样走原子写（docs 03 §10），避免半写文件。
            atomic_write_text(path, DEFAULT_MEMORY_PROMPT)
        except OSError:
            pass
    return DEFAULT_MEMORY_PROMPT


def effective_threshold(meta, config: MemoryConfig) -> int:
    """本会话实际生效阈值：会话覆盖优先，否则全局默认；夹到 50–99。"""
    value = meta.memory_threshold if getattr(meta, "memory_threshold", None) is not None else None
    if value is None:
        value = config.threshold
    return max(50, min(99, int(value)))


def effective_switches(meta, config: MemoryConfig) -> tuple[bool, bool, bool]:
    """本会话实际生效开关 `(是否使用, 是否允许压缩, 是否自动压缩)`：会话覆盖优先，否则全局默认。

    `auto` 默认关；自动压缩须 `use & compress & auto` 三者皆真（spec v0.0.1）。
    """
    use = meta.memory_use if getattr(meta, "memory_use", None) is not None else config.use
    compress = (
        meta.memory_compress
        if getattr(meta, "memory_compress", None) is not None
        else config.compress
    )
    auto = (
        meta.memory_auto
        if getattr(meta, "memory_auto", None) is not None
        else config.auto
    )
    return bool(use), bool(compress), bool(auto)


def recommended_range(window: int, config: MemoryConfig) -> tuple[int, int]:
    """按窗口比例推导记忆的**推荐范围**（不设绝对目标、不硬钳窗口）。

    窗口未知（<=0）时返回 (0, 0)；窗口上限内 `max = window // target_ratio`，
    `min = max // 2`，保证不超过模型预设上下文长度。
    """
    ratio = max(1, int(config.target_ratio))
    if window <= 0:
        return 0, 0
    high = max(1, window // ratio)
    return high // 2, high


def _message_events(events: list[dict]) -> list[tuple[int, str, str]]:
    """(seq, role, text)：只取可进历史的用户/助手终稿消息（同 context._history_messages）。"""
    out: list[tuple[int, str, str]] = []
    for event in events:
        t = event.get("type")
        payload = event.get("payload") or {}
        if t == "msg.user":
            out.append((int(event.get("seq", -1)), "user", str(payload.get("text", ""))))
        elif t == "msg.assistant.final":
            out.append(
                (int(event.get("seq", -1)), "assistant", str(payload.get("content", "")))
            )
    return out


@dataclass
class MemoryPlan:
    covered_seq: int
    transcript: str
    tokens_before: int  # 被覆盖消息的估算 token（压缩前这部分占用）


def plan_memory(
    events: list[dict],
    previous_text: str,
    covered_seq: int,
    keep_budget: int,
    force: bool = False,
) -> MemoryPlan | None:
    """挑选要概括的消息并拼成转录；返回 None = 无可压缩内容。

    - 只考虑 `seq > covered_seq` 的新消息（已覆盖的不重复概括，但旧记忆会并入转录，
      即**渐进式**记忆，而非只概括增量）。
    - 常规：从最新往回累计，保留尾部 token 预算内的消息；更早的进记忆。
    - `force=True`（用户显式点按按钮，**强求**）：不按预算保留，只留最后 1 轮问答，
      其余全部进记忆；仍不足 2 轮（`cutoff == 0`）则无可概括内容。
    """
    messages = [(seq, role, text) for seq, role, text in _message_events(events) if seq > covered_seq]
    if not messages:
        return None
    if force:
        cutoff = max(0, len(messages) - 2)
    else:
        keep = max(_MIN_KEEP, keep_budget)
        running = 0
        cutoff = len(messages)  # 从该下标起的消息保留
        for index in range(len(messages) - 1, -1, -1):
            size = estimate_tokens(messages[index][2])
            if running + size > keep:
                break
            running += size
            cutoff = index
    to_summarize = messages[:cutoff]
    if not to_summarize:
        return None
    covered = to_summarize[-1][0]
    lines: list[str] = []
    if previous_text.strip():
        lines.append("（此前记忆）\n" + previous_text.strip())
    for _seq, role, text in to_summarize:
        if not text.strip():
            continue
        speaker = "用户" if role == "user" else "助手"
        lines.append(f"{speaker}：{text.strip()}")
    transcript = "\n\n".join(lines)
    tokens_before = sum(estimate_tokens(t) for _s, _r, t in to_summarize)
    return MemoryPlan(covered_seq=covered, transcript=transcript, tokens_before=tokens_before)


def sanitize_memory(text: str) -> str:
    """记忆写盘前过统一脱敏（spec §5：新增持久化通道必须接入）。"""
    return redact(text) or ""