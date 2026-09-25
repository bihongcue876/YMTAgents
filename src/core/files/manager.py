"""file.* 工具面：装配五个内置文件工具并分派执行（spec-2026-09-24-file-tools §1/§3）。

设计（§2.3 α / §2.4 / §11 裁决）：
- 声明档一律 confirm：**区内读取靠 precheck 返回 allow 直接放行**（D3），
  **区外读取由 precheck 返回 None 走审核关卡**（D1），禁止区返回 deny（不打扰用户）；
- 写族：区外 / 禁止区 deny；**区内始终走关卡**（D4），本次未先读则 warn（卡片按高危档呈现）；
- precheck 签名保持 Callable[[args], verdict]：闭包捕获**动态**访问器（set_active_session 由控制器
  在每次请求分发前更新；工作区可运行期切换）；
- 出口文本过 redact；审计只记操作 / 结果 / 计数与**工作区相对路径**（区外只记标记）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, ClassVar


from shared.enums import Permission
from shared.errors import ErrorCode, error_text
from shared.redact import redact
from shared.schema import BuiltinToolConfig

from core.files import ops
from core.files.paths import (
    FileAmbiguous,
    FileDenied,
    FileMissing,
    FileNoMatch,
    FilePathError,
    classify,
    needs_review,
    path_key,
    relative_display,
    resolve_target,
)
from core.registry.registry import ToolResult
from core.registry.toolspec import ToolSpec

log = logging.getLogger(__name__)

TOOL_READ = "file.read"
TOOL_GLOB = "file.glob"
TOOL_GREP = "file.grep"
TOOL_EDIT = "file.edit"
TOOL_WRITE = "file.write"

#: 五个内置文件工具（顺序即装配顺序）。
TOOL_NAMES: tuple[str, ...] = (TOOL_READ, TOOL_GLOB, TOOL_GREP, TOOL_EDIT, TOOL_WRITE)
_READ_TOOLS = (TOOL_READ, TOOL_GLOB, TOOL_GREP)
_WRITE_TOOLS = (TOOL_EDIT, TOOL_WRITE)

#: prompt_block 上限（spec-2026-09-24-tool-prompts §2.1）：注册期静态校验，超限 fail-closed。
PROMPT_BLOCK_LIMIT = 600

#: 对照模式单侧最多带多少字符进预览（只给 UI 看，不落事件）。
_PREVIEW_CHARS = 8000


def _int_arg(value: Any, default: int) -> int:
    """整数参数收口（安全修订轮）：非法值归 invalid_args，不给后端异常留通道。"""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise FilePathError("行号参数必须是整数。") from exc


def _head_text(path: Path, limit: int) -> str:
    """读取文件头部文本（非 UTF-8 字节按替换字符处理，不抛）。"""
    try:
        with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            return handle.read(limit)
    except OSError:
        return ""

_DESCRIPTIONS = {
    TOOL_READ: "读取工作区内的文本文件，返回带行号的内容，可按行区间分页。",
    TOOL_GLOB: "按路径模式列出文件（只列文件、不列目录），结果有界。",
    TOOL_GREP: "按正则搜索文件内容，返回命中行与行号，结果有界。",
    TOOL_EDIT: "按字面匹配替换文件中的一段文本（默认要求唯一命中）。",
    TOOL_WRITE: "以整文件内容写入（新建或覆盖），原子落盘。",
}

#: 第一批使用指引（切片 C 起进环境声明「工具使用指引」段）。
PROMPT_BLOCKS = {
    TOOL_READ: (
        "读文本文件（带行号，可按 offset/limit 分页）。工作区内直接读，工作区外会先请用户确认。"
        "文件过大时只返回窗口，并说明总行数是否已知；非 UTF-8 或含 NUL 的一律报错，不做猜测。"
        "要改某个文件前先读它，否则写操作会被标为高危。"
    ),
    TOOL_GLOB: (
        "按模式列出文件（只列文件、不列目录），按修改时间倒序，最多 100 条。"
        "模式含 / 时匹配相对路径，否则匹配文件名；默认不含隐藏项，敏感文件名永不列出。"
    ),
    TOOL_GREP: (
        "按正则搜索文件内容，返回「文件:行号: 文本」，最多 250 条，可用 include 过滤文件名。"
        "不支持的正则特性会如实报错，不静默降级。"
    ),
    TOOL_EDIT: (
        "按字面替换一段文本（默认要求唯一命中）。命中 0 处或多处都会失败并说明原因；"
        "写回保持原行尾，覆盖前留一代 .bak。"
    ),
    TOOL_WRITE: (
        "整文件写入（新建或覆盖）。父目录不存在会失败（不自动建目录）；"
        "覆盖既有文件时保持其行尾，并留一代 .bak。"
    ),
}

_SCHEMAS: dict[str, dict] = {
    TOOL_READ: {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径（相对当前工作区；也可给绝对路径）"},
            "offset": {"type": "integer", "description": "起始行号（1 基，默认 1）"},
            "limit": {"type": "integer", "description": "最多返回行数（默认 2000，上限 5000）"},
        },
        "required": ["path"],
    },
    TOOL_GLOB: {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "路径模式，如 *.py 或 src/**/*.ts"},
            "path": {"type": "string", "description": "起始目录，缺省为当前工作区"},
            "include_hidden": {"type": "boolean", "description": "是否包含隐藏项（敏感文件名仍排除）"},
        },
        "required": ["pattern"],
    },
    TOOL_GREP: {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "正则表达式"},
            "path": {"type": "string", "description": "起始目录，缺省为当前工作区"},
            "include": {"type": "string", "description": "文件名过滤（单个 glob，不支持取反）"},
            "include_hidden": {"type": "boolean", "description": "是否包含隐藏项（敏感文件名仍排除）"},
        },
        "required": ["pattern"],
    },
    TOOL_EDIT: {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径（相对当前工作区）"},
            "old_string": {"type": "string", "description": "要被替换的原文（字面匹配）"},
            "new_string": {"type": "string", "description": "替换后的文本（可为空串表示删除）"},
            "replace_all": {"type": "boolean", "description": "替换全部命中（默认 false，要求唯一命中）"},
        },
        "required": ["path", "old_string"],
    },
    TOOL_WRITE: {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件路径（相对当前工作区）"},
            "content": {"type": "string", "description": "整文件内容（新建或覆盖）"},
        },
        "required": ["path", "content"],
    },
}


class FilesTools:
    """内置文件工具族（宿主侧装配 + 调用分派）。

    配置真值：plugins.json -> PluginsConfig.builtin（零 schema 变更）。enabled=false ⇒ **不注册**。
    """

    def __init__(self, registry: Any, config_store: Any, *,
                 session_root: Callable[[str], str | Path | None],
                 data_root: str | Path,
                 audit: Callable[..., None] | None = None) -> None:
        self._registry = registry
        self._store = config_store
        self._session_root = session_root
        self._data_root = Path(data_root)
        self._audit = audit or (lambda *a, **k: None)
        self._active_session: str | None = None
        self._read_seen: set[str] = set()
        self._registered: dict[str, str] = {}

    # ---------- 装配 ----------
    def refresh(self) -> None:
        """按 plugins.json 装配：enabled=false ⇒ 不注册（rev64「关 = 真卸载」同口径）。"""
        config = self._builtin()
        for name in TOOL_NAMES:
            entry = config.get(name)
            if entry is not None and not entry.enabled:
                self._unregister(name)
                continue
            permission = getattr(entry, "permission", None) or "confirm"
            self._register(name, str(permission))

    def shutdown(self) -> None:
        for name in list(self._registered):
            self._unregister(name)

    def registered(self) -> dict[str, str]:
        return dict(self._registered)

    # -- 配置面（v0.0.11 D-1）：插件页「内置工具」分区 -------------------------
    KNOWN_BUILTIN_TOOLS: ClassVar[tuple[str, ...]] = TOOL_NAMES

    def state(self) -> list[dict]:
        """面板快照（全量五工具，含 enabled=false）：{name, title, permission, enabled}。"""
        config = self._builtin()
        items: list[dict] = []
        for name in TOOL_NAMES:
            entry = config.get(name)
            items.append(
                {
                    "name": name,
                    "title": _DESCRIPTIONS.get(name, name),
                    "permission": str(getattr(entry, "permission", None) or "confirm"),
                    "enabled": True if entry is None else bool(entry.enabled),
                }
            )
        return items

    def toggle_builtin(self, name: str, enabled: bool) -> None:
        """写入 plugins.json -> builtin（真值源）并即时生效；未知工具名抛 ValueError。"""
        if name not in TOOL_NAMES:
            raise ValueError(f"未知内置工具：{name}")
        try:
            config = self._store.load("plugins")
            entry = dict(getattr(config, "builtin", {}) or {})
            current = entry.get(name)
            entry[name] = BuiltinToolConfig(
                enabled=bool(enabled),
                permission=str(getattr(current, "permission", None) or "confirm"),
            )
            setattr(config, "builtin", entry)
            self._store.save("plugins", config)
        except Exception:
            log.exception("plugins 配置读写失败（builtin 开关）")
            raise
        self.refresh()

    def _builtin(self) -> dict:
        try:
            plugins = self._store.load("plugins")
            return dict(getattr(plugins, "builtin", {}) or {})
        except Exception:  # noqa: BLE001 - 配置异常回退默认，不阻断启动
            log.exception("plugins 配置读取失败，内置文件工具回退默认")
            return {}

    def _register(self, name: str, permission: str) -> None:
        if self._registered.get(name) == permission:
            return
        self._unregister(name)
        spec = self._spec(name, permission)
        self._registry.register(spec, self._handler(name))
        self._registered[name] = permission

    def _unregister(self, name: str) -> None:
        if name in self._registered:
            self._registry.unregister(name)
            self._registered.pop(name, None)

    def _spec(self, name: str, permission: str) -> ToolSpec:
        block = PROMPT_BLOCKS.get(name, "")
        if len(block) > PROMPT_BLOCK_LIMIT:
            raise ValueError(
                f"prompt_block 超长（{len(block)} > {PROMPT_BLOCK_LIMIT} 字），fail-closed 拒绝注册：{name}"
            )
        return ToolSpec(
            name=name,
            title=_DESCRIPTIONS[name],
            description=_DESCRIPTIONS[name],
            permission=Permission(permission),
            input_schema=_SCHEMAS[name],
            prompt_block=block,
            precheck=self._make_precheck(name),
            preview=self._make_preview(name),
        )

    # ---------- 会话与工作区 ----------
    def set_active_session(self, session_id: str | None) -> None:
        """控制器每次请求分发前更新；换会话即清空「读记账」（进程内、会话级、不落盘）。"""
        if session_id == self._active_session:
            return
        self._active_session = session_id
        self._read_seen.clear()

    def _active_root(self) -> Path | None:
        session = self._active_session
        if not session:
            return None
        return self._root_of(session)

    def _root_of(self, session: str | None) -> Path | None:
        if not session:
            return None
        try:
            root = self._session_root(session)
        except Exception:  # noqa: BLE001 - 无效 / 旧会话：交给「未知工作区」分支
            log.exception("解析会话工作区失败：%s", session)
            return None
        return Path(root) if root else None

    # ---------- 策略裁决（α 方案：allow / deny / warn，签名不收 ctx） ----------
    def _make_precheck(self, name: str) -> Callable[[dict], tuple[str, str] | None]:
        def precheck(args: dict) -> tuple[str, str] | None:
            try:
                return self._verdict(name, args or {})
            except FileDenied as exc:
                return ("deny", str(exc))
            except FilePathError:
                return None  # 路径本身非法：留给执行期按 invalid_args 归码，不在关卡前冒充策略拒绝
        return precheck

    def _make_preview(self, name: str) -> Callable[[dict], dict] | None:
        """写族的关卡预览（对照模式）：edit 直接给 old/new；write 读出旧文一并给出。"""
        if name not in _WRITE_TOOLS:
            return None

        def preview(args: dict) -> dict:
            raw = str(args.get("path") or "")
            if name == TOOL_EDIT:
                return {
                    "kind": "edit",
                    "path": raw,
                    "old": str(args.get("old_string") or "")[:_PREVIEW_CHARS],
                    "new": str(args.get("new_string") or "")[:_PREVIEW_CHARS],
                    "replace_all": bool(args.get("replace_all")),
                }
            data: dict = {
                "kind": "write", "path": raw,
                "old": "", "new": str(args.get("content") or "")[:_PREVIEW_CHARS], "exists": False,
            }
            root = self._active_root()
            if root is not None:
                try:
                    target = resolve_target(raw, root=root, must_exist=False)
                    access = classify(target, workspace_root=root, data_root=self._data_root)
                    if access.read and target.is_file():
                        data["old"] = _head_text(target, _PREVIEW_CHARS)
                        data["exists"] = True
                        data["path"] = relative_display(target, root)
                except (FilePathError, OSError):
                    pass
            return data

        return preview

    def _verdict(self, name: str, args: dict) -> tuple[str, str] | None:
        root = self._active_root()
        if root is None:
            return None  # 当前工作区未知：一律走关卡（fail-closed 到人工确认）
        data = self._data_root
        raw = str(args.get("path") or "").strip()

        if name in (TOOL_GLOB, TOOL_GREP):
            base = resolve_target(raw, root=root, must_exist=False) if raw else root
            access = classify(base, workspace_root=root, data_root=data)
            if not access.read:
                return ("deny", access.reason)
            return None if needs_review(access) else ("allow", "")

        target = resolve_target(raw, root=root, must_exist=False)
        access = classify(target, workspace_root=root, data_root=data)
        if name in _WRITE_TOOLS:
            if not access.write:
                return ("deny", access.reason or "工作区之外不允许写入。")
            if self._key(target) not in self._read_seen:
                return ("warn", "本次尚未先读取该文件；确认卡按高危档呈现（可拒绝）。")
            return None
        if not access.read:
            return ("deny", access.reason)
        return None if needs_review(access) else ("allow", "")

    # ---------- 执行 ----------
    def _handler(self, name: str) -> Callable[[dict, Any], ToolResult]:
        def handle(args: dict, ctx: Any = None) -> ToolResult:
            session = getattr(ctx, "session_id", "") or self._active_session
            root = self._root_of(session) or self._active_root()
            if root is None:
                return ToolResult(
                    ok=False,
                    error={"code": ErrorCode.TOOL_INVALID_ARGS.value,
                           "message": "当前会话未绑定可用工作区，无法执行文件操作。"},
                )
            try:
                result = self._run(name, args or {}, root=root)
            except FileMissing as exc:
                return self._failure(ErrorCode.FILE_NOT_FOUND, str(exc), name, root)
            except FileNoMatch as exc:
                return self._failure(ErrorCode.EDIT_NO_MATCH, str(exc), name, root)
            except FileAmbiguous as exc:
                return self._failure(ErrorCode.EDIT_AMBIGUOUS, str(exc), name, root)
            except FileDenied as exc:
                return self._failure(ErrorCode.TOOL_DENIED, str(exc), name, root)
            except FilePathError as exc:
                return self._failure(ErrorCode.TOOL_INVALID_ARGS, str(exc), name, root)
            except OSError:
                log.exception("文件工具后端异常：%s", name)
                return self._failure(ErrorCode.TOOL_BACKEND_ERROR, "", name, root)
            output = str(redact(str(result.get("output", ""))) or "")
            return ToolResult(ok=True, output=output, usage={"chars": len(output)})
        return handle

    def _run(self, name: str, args: dict, *, root: Path) -> dict:
        if name == TOOL_READ:
            return self._read(args, root=root)
        if name == TOOL_GLOB:
            return self._glob(args, root=root)
        if name == TOOL_GREP:
            return self._grep(args, root=root)
        if name == TOOL_EDIT:
            return self._edit(args, root=root)
        if name == TOOL_WRITE:
            return self._write(args, root=root)
        raise FilePathError(f"未实现的文件工具：{name}")

    def _read(self, args: dict, *, root: Path) -> dict:
        target = resolve_target(args.get("path"), root=root, must_exist=True)
        access = classify(target, workspace_root=root, data_root=self._data_root)
        if not access.read:
            raise FileDenied(access.reason)
        result = ops.read_text(
            target,
            offset=_int_arg(args.get("offset"), 1),
            limit=_int_arg(args.get("limit"), ops.DEFAULT_READ_LIMIT),
        )
        self._read_seen.add(self._key(target))
        # 审计口径（spec §2.4）：区外只记标记——用户磁盘布局不进 audit.jsonl（安全修订轮）。
        audit_path = relative_display(target, root) if access.inside_workspace else "（工作区外）"
        self._audit("file.read", tool=TOOL_READ, inside=access.inside_workspace,
                    path=audit_path, bytes=result["bytes"])
        total = f"共 {result['total_lines']} 行" if result["total_known"] else "总行数未知（文件过大）"
        head = f"{relative_display(target, root)}（{total}；已显示 {result['shown_from']}–{result['shown_to']}）"
        if result["truncated"]:
            head += "（未显示完）"
        return {"output": head + "\n" + result["text"]}

    def _glob(self, args: dict, *, root: Path) -> dict:
        raw = str(args.get("path") or "").strip()
        base = resolve_target(raw, root=root, must_exist=False) if raw else root
        access = classify(base, workspace_root=root, data_root=self._data_root)
        if not access.read:
            raise FileDenied(access.reason)
        result = ops.glob_files(
            str(args.get("pattern") or ""), base=base, data_root=self._data_root,
            include_hidden=bool(args.get("include_hidden")),
        )
        lines = [item["path"] for item in result["files"]]
        head = f"{len(lines)} 个文件（{relative_display(base, root)}）"
        if result["overflow"]:
            head += "；仅显示前 100 条"
        self._audit("file.glob", tool=TOOL_GLOB, inside=access.inside_workspace, count=len(lines))
        return {"output": head + ("\n" + "\n".join(lines) if lines else "")}

    def _grep(self, args: dict, *, root: Path) -> dict:
        raw = str(args.get("path") or "").strip()
        base = resolve_target(raw, root=root, must_exist=False) if raw else root
        access = classify(base, workspace_root=root, data_root=self._data_root)
        if not access.read:
            raise FileDenied(access.reason)
        result = ops.grep_files(
            str(args.get("pattern") or ""), base=base, data_root=self._data_root,
            include=str(args.get("include") or ""), include_hidden=bool(args.get("include_hidden")),
        )
        lines = [f"{item['path']}:{item['line']}: {item['text']}" for item in result["matches"]]
        head = f"{len(lines)} 处命中（扫描 {result['files_scanned']} 个文件）"
        if result["overflow"]:
            head += "；仅显示前 250 条"
        self._audit("file.grep", tool=TOOL_GREP, inside=access.inside_workspace, count=len(lines))
        return {"output": head + ("\n" + "\n".join(lines) if lines else "")}

    def _edit(self, args: dict, *, root: Path) -> dict:
        target = resolve_target(args.get("path"), root=root, must_exist=True)
        access = classify(target, workspace_root=root, data_root=self._data_root)
        if not access.write:
            raise FileDenied(access.reason or "工作区之外不允许写入。")
        result = ops.edit_text(
            target, args.get("old_string") or "", args.get("new_string") or "",
            replace_all=bool(args.get("replace_all")),
        )
        self._read_seen.add(self._key(target))
        rel = relative_display(target, root)
        self._audit("file.edit", tool=TOOL_EDIT, inside=access.inside_workspace,
                    path=rel if access.inside_workspace else "（工作区外）",
                    replacements=result["replacements"])
        if result["noop"]:
            return {"output": f"未修改 {rel}：替换结果与原内容相同。"}
        return {"output": f"已修改 {rel}（+{result['added']} / -{result['removed']}，替换 {result['replacements']} 处）。"}

    def _write(self, args: dict, *, root: Path) -> dict:
        target = resolve_target(args.get("path"), root=root, must_exist=False)
        access = classify(target, workspace_root=root, data_root=self._data_root)
        if not access.write:
            raise FileDenied(access.reason or "工作区之外不允许写入。")
        result = ops.write_text(target, args.get("content"))
        self._read_seen.add(self._key(target))
        rel = relative_display(target, root)
        self._audit("file.write", tool=TOOL_WRITE, inside=access.inside_workspace,
                    path=rel if access.inside_workspace else "（工作区外）",
                    bytes=result["bytes"], created=result["created"])
        verb = "新建" if result["created"] else "覆盖"
        return {"output": f"已{verb} {rel}（{result['lines']} 行 / {result['bytes']} 字节）。"}

    # ---------- 小工具 ----------
    def _failure(self, code: ErrorCode, detail: str, name: str, root: Path | None) -> ToolResult:
        message = detail or error_text(code.value)
        self._audit("file.error", tool=name, code=code.value)
        return ToolResult(ok=False, error={"code": code.value, "message": message})

    @staticmethod
    def _key(path: Path) -> str:
        """路径同一性键：与 paths.path_key 同一实现（单一来源，安全修订轮）。"""
        return path_key(path)
