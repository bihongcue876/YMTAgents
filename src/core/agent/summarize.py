"""历史摘要化（spec rev26：压缩 / 轮 C）。

压缩的本质是**概括**：把较早的对话概括成一份**有格式的摘要文件**
（`sessions/<id>/summary.md`），供后续对话继续使用；`events.jsonl` 只增不改。

- 专用提示词：`config/summary_prompt.md`（缺失时落内置默认，**不叠加 persona**）。
- 触发：用户点按钮（本模块只负责执行）；阈值判定由调用方用 `effective_threshold`。
- 选择面：从「上次摘要覆盖点」之后到「尾部保留预算」之前的消息进摘要；
  近段按 token 预算保留，**不是**固定「最近 N 轮」。
- 失败安全：任何异常都不写文件、由上层转成可读错误，原对话不受影响。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from shared.redact import redact
from shared.schema import SummaryConfig

from core.agent.context import estimate_tokens
from core.store.atomic import atomic_write_text

#: 内置默认摘要提示词（rev26）。文件 `config/summary_prompt.md` 存在时优先。
DEFAULT_SUMMARY_PROMPT = (
    "你是一个对话历史压缩器。请把下面这段**较早的对话**概括为一份结构化的中文摘要，"
    "供后续对话继续使用。要求：\n"
    "1. 只保留对后续对话有用的事实，不得编造、不得加入原文没有的信息；\n"
    "2. 保持客观，不复述寒暄与过程，保留结论、决定、关键事实、偏好与约束、未决问题；\n"
    "3. 严格使用以下小节，缺内容的小节写「（无）」：\n"
    "## 会话目标\n"
    "## 已达成的结论与决定\n"
    "## 关键事实 / 偏好 / 约束\n"
    "## 未决问题与下一步\n"
    "## 时间线\n"
)

#: 尾部保留下限（窗口未知或很小时），避免把近段也压进去。
_MIN_KEEP = 1024


def summary_prompt_path(root: Path) -> Path:
    return Path(root) / "config" / "summary_prompt.md"


def load_summary_prompt(root: Path, ensure: bool = True) -> str:
    """读取专用摘要提示词；缺失时落内置默认（`ensure=True`）并返回。"""
    path = summary_prompt_path(root)
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8")
            if text.strip():
                return text
        except OSError:
            pass
    if ensure:
        try:
            # rev27：配置资源同样走原子写（docs 03 §10），避免半写文件。
            atomic_write_text(path, DEFAULT_SUMMARY_PROMPT)
        except OSError:
            pass
    return DEFAULT_SUMMARY_PROMPT


def effective_threshold(meta, config: SummaryConfig) -> int:
    """本会话实际生效阈值：会话覆盖优先，否则全局默认；夹到 50–99。"""
    value = meta.summary_threshold if getattr(meta, "summary_threshold", None) is not None else None
    if value is None:
        value = config.threshold
    return max(50, min(99, int(value)))


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
class SummaryPlan:
    covered_seq: int
    transcript: str
    tokens_before: int  # 被覆盖消息的估算 token（压缩前这部分占用）


def plan_summary(
    events: list[dict], previous_text: str, covered_seq: int, keep_budget: int
) -> SummaryPlan | None:
    """挑选要概括的消息并拼成转录；返回 None = 无可压缩内容。

    - 只考虑 `seq > covered_seq` 的新消息（已覆盖的不重复概括，但旧摘要会并入转录，
      即**渐进式**摘要，而非只概括增量）。
    - 从最新往回累计，保留尾部 token 预算内的消息；更早的进摘要。
    """
    messages = [(seq, role, text) for seq, role, text in _message_events(events) if seq > covered_seq]
    if not messages:
        return None
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
        lines.append("（此前摘要）\n" + previous_text.strip())
    for _seq, role, text in to_summarize:
        if not text.strip():
            continue
        speaker = "用户" if role == "user" else "助手"
        lines.append(f"{speaker}：{text.strip()}")
    transcript = "\n\n".join(lines)
    tokens_before = sum(estimate_tokens(t) for _s, _r, t in to_summarize)
    return SummaryPlan(covered_seq=covered, transcript=transcript, tokens_before=tokens_before)


def sanitize_summary(text: str) -> str:
    """摘要写盘前过统一脱敏（spec §5：新增持久化通道必须接入）。"""
    return redact(text) or ""