"""Skill 载体解析（v0.0.4 / docs 07 §4.2 / docs 03 §5.2）。

Skill = 带元数据的纯提示词指令包，**无代码**：
```
skills/<skl_id>/SKILL.md    # YAML frontmatter（name/description/version/permission）+ 正文
                 resources/ # 可选；本期零执行通道（占位）
```

frontmatter 用**手写行解析器**（`---` 围栏 + 平铺 `key: value`）——零新增依赖，不引入 YAML 库。
一切校验 fail-closed：解析失败/缺字段/超限/非法 id 一律拒绝注册，error 说明原因（rev32 口径）。
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel

#: SKILL.md 文件体积上限（fail-closed；rev32 persona 导入同量级）。
MAX_FILE_BYTES = 2 * 1024 * 1024

#: 正文（提示词指令）字符上限：指令包是一等输入，执行器对其**豁免外置**，
#: 故上限在加载期收口（约 1.3 万 token 成本上限，用户裁决 2026-09-21）。
MAX_BODY_CHARS = 20_000

#: 目录 id：`skl_` 前缀；不含点号（docs 09 B5：`skill.` 前缀保留 + 防冒充）。
SKILL_ID_RE = re.compile(r"^skl_[A-Za-z0-9_-]+$")

_PERMISSIONS = ("safe", "confirm", "restricted")


class SkillMeta(BaseModel):
    """一个已通过校验的技能元数据（目录 id 即技能 id）。"""

    id: str
    name: str
    description: str
    version: str = "1"
    permission: str = "safe"  # safe | confirm | restricted（docs 09 §2，缺省 safe）


def _parse_frontmatter(text: str) -> tuple[dict[str, str] | None, str]:
    """解析 `---` 围栏 frontmatter；返回 (键值表|None, 正文)。

    None = 没有 frontmatter 或围栏不闭合（fail-closed 的判定交给调用方）。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return None, text
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = stripped.partition(":")
        if not sep:
            continue
        fields[key.strip()] = value.strip()
    return fields, "\n".join(lines[end + 1 :]).strip()


def parse_skill_md(path: Path, skill_id: str | None = None) -> tuple[SkillMeta | None, str | None, str]:
    """解析一个 SKILL.md。

    返回 `(meta, error, body)`：校验通过时 `(meta, None, body)`；失败时 `(None, 原因, "")`。
    `skill_id` 缺省取文件父目录名（目录即 id，docs 03 §5.2）。
    """
    path = Path(path)
    sid = skill_id or path.parent.name
    if not SKILL_ID_RE.match(sid):
        return None, f"技能 id 不合法：{sid!r}（须为 skl_ 前缀、不含点号）", ""
    try:
        raw = path.read_bytes()
    except OSError:
        return None, "无法读取 SKILL.md。", ""
    if len(raw) > MAX_FILE_BYTES:
        return None, f"SKILL.md 过大（上限 {MAX_FILE_BYTES // (1024 * 1024)} MB）。", ""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, "SKILL.md 不是有效的 UTF-8 文本。", ""
    fields, body = _parse_frontmatter(text)
    if fields is None:
        return None, "缺少 `---` 围栏 frontmatter。", ""
    name = fields.get("name", "").strip()
    description = fields.get("description", "").strip()
    version = fields.get("version", "1").strip() or "1"
    permission = fields.get("permission", "safe").strip() or "safe"
    if not name:
        return None, "frontmatter 缺少 name。", ""
    if not description:
        return None, "frontmatter 缺少 description（给模型的一句话，环境陈述/FC 用）。", ""
    if permission not in _PERMISSIONS:
        return None, f"permission 取值不合法：{permission!r}（须为 safe/confirm/restricted）。", ""
    if not body.strip():
        return None, "正文为空：Skill 至少要有一段提示词指令。", ""
    if len(body) > MAX_BODY_CHARS:
        return None, f"正文超长（{len(body)} 字符，上限 {MAX_BODY_CHARS}）。", ""
    meta = SkillMeta(id=sid, name=name, description=description, version=version, permission=permission)
    return meta, None, body
