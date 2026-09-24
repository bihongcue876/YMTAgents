"""Skills 宿主（v0.0.4 / docs 07 §4.2 + §5）。

职责：预置技能首启复制、skills/ 目录扫描与 plugins.json 调和、
把启用的技能注册为注册表工具 `skill.<id>`（缺省 safe，纯文本读取无副作用）、
导入（目录 / git，Claude skill 风格 + 更新）、删除、权限档覆盖。

铁律（docs 07 §4.2）：Skill 只提供指令，永不提供能力——handler 只读 SKILL.md 正文返回，
无任何执行通道；`skill.*` 前缀由本类独占注册，冲突报错禁止静默覆盖（docs 09 B5）。
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from shared.ids import SKL, new_id
from shared.redact import redact

from core.registry.registry import Registry, ToolResult
from core.registry.toolspec import ToolSpec
from core.skills.loader import SkillMeta, parse_skill_md

log = logging.getLogger(__name__)

_ORIGIN_FILE = "skill.origin.json"


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _is_git_source(source: str) -> bool:
    lowered = source.lower()
    return lowered.startswith(("git@", "http://", "https://")) or lowered.endswith(".git")


class SkillManager:
    def __init__(
        self,
        config_store,
        registry: Registry,
        skills_root: Path,
        audit=None,
        presets_dir: Path | None = None,
    ) -> None:
        self.config_store = config_store
        self.registry = registry
        self.skills_root = Path(skills_root)
        self.audit = audit or (lambda *_a, **_k: None)
        self.presets_dir = Path(presets_dir) if presets_dir else None
        # 预置技能集合（不可删）；以包内 presets 目录为准。
        self._preset_ids: set[str] = set()
        if self.presets_dir and self.presets_dir.is_dir():
            self._preset_ids = {
                p.name
                for p in self.presets_dir.iterdir()
                if p.is_dir() and (p / "SKILL.md").is_file()
            }
        #: reload 后解析失败的已知技能（id → 原因），供技能页展示。
        self._broken: dict[str, str] = {}

    # -- 配置面 -------------------------------------------------------------
    def _plugins(self):
        return self.config_store.load("plugins")

    def _save_plugins(self, plugins) -> None:
        self.config_store.save("plugins", plugins)

    def _meta_of(self, skill_id: str) -> SkillMeta | None:
        meta, error, _body = parse_skill_md(self.skills_root / skill_id / "SKILL.md", skill_id)
        if error:
            return None
        return meta

    def _effective_permission(self, skill_id: str, default: str) -> str:
        return self._plugins().skills.permissions.get(skill_id, default)

    # -- 附加功能契约（切片 0）----------------------------------------------
    def activate(self) -> None:
        """装配：预置复制 + 扫描 + 注册启用技能。"""
        self.reload()

    def deactivate(self) -> None:
        """真卸载：注销全部 `skill.*`（不删磁盘、不改期望态；再开即恢复）。"""
        for spec in self.registry.snapshot():
            if spec.name.startswith("skill."):
                self.registry.unregister(spec.name)

    def host_state(self) -> str:
        return "ready"

    # -- 生命周期 -----------------------------------------------------------
    def reload(self) -> None:
        """预置复制 → 目录扫描调和 → 重新注册全部启用技能（幂等，启动与变更后调用）。"""
        self.skills_root.mkdir(parents=True, exist_ok=True)
        self._install_presets()
        plugins = self._plugins()
        skills_cfg = plugins.skills
        installed: list[str] = []
        broken: dict[str, str] = {}
        for child in sorted(self.skills_root.iterdir()):
            if not child.is_dir():
                continue
            meta, error, _body = parse_skill_md(child / "SKILL.md", child.name)
            if meta is None:
                # 目录里有 SKILL.md 却解析失败 = 用户意图明确但内容坏 → 展示异常原因（fail-closed 不注册）。
                if (child / "SKILL.md").is_file():
                    broken[child.name] = error or "解析失败。"
                continue
            installed.append(meta.id)
        self._broken = broken
        changed = installed != skills_cfg.installed
        if changed:
            skills_cfg.installed = installed
            self._save_plugins(plugins)
        # 重新注册：先清全部 skill.*，再按 enabled∩installed 挂载（fail-closed：解析失败不注册）。
        for spec in self.registry.snapshot():
            if spec.name.startswith("skill."):
                self.registry.unregister(spec.name)
        for sid in skills_cfg.enabled:
            if sid in installed:
                self._register(sid)

    def _install_presets(self) -> None:
        """包内预置技能首启复制到 ymtdata（安装但**不停用也不启用**，03 §2 资源为备份）。"""
        if not (self.presets_dir and self.presets_dir.is_dir()):
            return
        plugins = self._plugins()
        changed = False
        for pid in sorted(self._preset_ids):
            src = self.presets_dir / pid
            dest = self.skills_root / pid
            if not dest.exists():
                shutil.copytree(src, dest)
                changed = True
                self.audit("skill.preset.install", skill=pid)
            if pid not in plugins.skills.installed:
                plugins.skills.installed.append(pid)
                changed = True
        if changed:
            self._save_plugins(plugins)

    # -- 注册 ---------------------------------------------------------------
    def _register(self, skill_id: str) -> None:
        meta = self._meta_of(skill_id)
        if meta is None:
            return
        spec = ToolSpec(
            name=f"skill.{meta.id}",
            title=meta.name,
            description=meta.description,
            permission=self._effective_permission(meta.id, meta.permission),
            input_schema={},
            availability=None,  # enabled ⇒ 可见；停用即注销，无需动态回调
        )
        self.registry.register(spec, self._read_body_handler(skill_id))

    def _read_body_handler(self, skill_id: str):
        """handler = 现读现回（docs 03 §10.4 文件为准）：手工编辑下次调用即生效。"""

        def handler(_args: dict, _ctx) -> ToolResult:
            meta, error, body = parse_skill_md(self.skills_root / skill_id / "SKILL.md", skill_id)
            if error or meta is None:
                return ToolResult(
                    ok=False,
                    error={"code": "tool_backend_error", "message": redact(error) or "技能正文读取失败。"},
                )
            return ToolResult(ok=True, output=body)

        return handler

    # -- 操作 ---------------------------------------------------------------
    def toggle(self, skill_id: str, enabled: bool) -> None:
        plugins = self._plugins()
        if skill_id not in plugins.skills.installed:
            raise ValueError("技能不存在。")
        skills = plugins.skills
        if enabled and skill_id not in skills.enabled:
            skills.enabled.append(skill_id)
        elif not enabled and skill_id in skills.enabled:
            skills.enabled.remove(skill_id)
        self._save_plugins(plugins)
        if enabled:
            self._register(skill_id)
        else:
            self.registry.unregister(f"skill.{skill_id}")
        self.audit("skill.toggle", skill=skill_id, enabled=enabled)

    def set_permission(self, skill_id: str, permission: str) -> None:
        plugins = self._plugins()
        if skill_id not in plugins.skills.installed:
            raise ValueError("技能不存在。")
        plugins.skills.permissions[skill_id] = permission
        self._save_plugins(plugins)
        if skill_id in plugins.skills.enabled:
            self.registry.unregister(f"skill.{skill_id}")
            self._register(skill_id)  # ToolSpec.permission = 覆盖 ⊕ 缺省（docs 09 §2）
        self.audit("skill.permission", skill=skill_id, permission=permission)

    # -- 导入 / 更新 ----------------------------------------------------------
    def import_skill(self, source: str) -> list[SkillMeta]:
        """从目录 / git 来源导入；返回成功导入的技能元数据列表（可含多个）。"""
        source = (source or "").strip()
        if not source:
            raise ValueError("导入来源为空。")
        with tempfile.TemporaryDirectory(prefix="ymt_skill_") as tmp:
            if _is_git_source(source):
                origin_type, root = "git", self._git_checkout(source, Path(tmp))
            else:
                path = Path(source)
                if not path.exists():
                    raise ValueError("导入路径不存在。")
                origin_type, root = "dir", path
            found = self._discover(root)
            if not found:
                raise ValueError("未在该来源找到任何 SKILL.md（支持根目录或一级子目录）。")
            imported: list[SkillMeta] = []
            for skill_dir in found:
                new_skill_id = new_id(SKL)  # 导入一律生成新 id（rev32 口径）；校验也以新 id 为准
                meta, error, _body = parse_skill_md(skill_dir / "SKILL.md", skill_id=new_skill_id)
                if meta is None:
                    log.warning("跳过无效技能目录 %s：%s", skill_dir, error)
                    continue
                dest = self.skills_root / new_skill_id
                shutil.copytree(skill_dir, dest)
                self._write_origin(
                    dest,
                    {
                        "schema_version": 1,
                        "type": origin_type,
                        "source": source,
                        "subpath": "" if skill_dir == root else skill_dir.relative_to(root).as_posix(),
                        "imported_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                plugins = self._plugins()
                plugins.skills.installed.append(new_skill_id)
                self._save_plugins(plugins)
                self.audit("skill.import", skill=new_skill_id, source_kind=origin_type)
                imported.append(meta)
            if not imported:
                raise ValueError("来源中的 SKILL.md 均未通过校验，未导入任何技能。")
            return imported

    def update_skill(self, skill_id: str) -> SkillMeta:
        """按 origin 来源重新拉取覆盖（保 id 与启用态）；无来源信息则拒绝。"""
        skill_dir = self.skills_root / skill_id
        origin = self._read_origin(skill_dir)
        if not origin:
            raise ValueError("该技能没有来源信息，无法更新。")
        source = str(origin.get("source", ""))
        subpath = str(origin.get("subpath", "")).strip("/")
        with tempfile.TemporaryDirectory(prefix="ymt_skill_") as tmp:
            if origin.get("type") == "git" or _is_git_source(source):
                root = self._git_checkout(source, Path(tmp))
            else:
                root = Path(source)
                if not root.exists():
                    raise ValueError("来源目录已不存在，无法更新。")
            skill_src = root / subpath if subpath else root
            meta, error, _body = parse_skill_md(skill_src / "SKILL.md", skill_id)
            if meta is None:
                raise ValueError(f"来源 SKILL.md 未通过校验：{error}")
            # 原子覆盖：SKILL.md 先校验后替换；resources/ 整体换新。
            new_text = (skill_src / "SKILL.md").read_text(encoding="utf-8")
            _atomic_write_text(skill_dir / "SKILL.md", new_text)
            new_resources = skill_src / "resources"
            old_resources = skill_dir / "resources"
            if old_resources.exists():
                shutil.rmtree(old_resources)
            if new_resources.is_dir():
                shutil.copytree(new_resources, old_resources)
        plugins = self._plugins()
        if skill_id in plugins.skills.enabled:
            self.registry.unregister(f"skill.{skill_id}")
            self._register(skill_id)  # 名称/描述/权限可能变了
        self.audit("skill.update", skill=skill_id)
        return meta

    def delete(self, skill_id: str) -> None:
        if skill_id in self._preset_ids:
            raise ValueError("预置技能不可删除。")
        skill_dir = self.skills_root / skill_id
        if skill_dir.exists():
            shutil.rmtree(skill_dir)
        plugins = self._plugins()
        for field in ("installed", "enabled"):
            listing = getattr(plugins.skills, field)
            if skill_id in listing:
                listing.remove(skill_id)
        plugins.skills.permissions.pop(skill_id, None)
        self._save_plugins(plugins)
        self.registry.unregister(f"skill.{skill_id}")
        self.audit("skill.delete", skill=skill_id)

    # -- 查询 ---------------------------------------------------------------
    def list_status(self) -> list[dict]:
        """技能页数据面：installed（含解析失败的，带 error）+ 运行态（注册与否）。"""
        items: list[dict] = []
        plugins = self._plugins()
        registered = {s.name for s in self.registry.snapshot() if s.name.startswith("skill.")}
        known = sorted(set(plugins.skills.installed) | self._preset_ids | set(self._broken))
        for sid in known:
            skill_dir = self.skills_root / sid
            meta, error, _body = parse_skill_md(skill_dir / "SKILL.md", sid)
            if error:
                error = self._broken.get(sid, error)
            origin = self._read_origin(skill_dir)
            items.append(
                {
                    "id": sid,
                    "name": meta.name if meta else sid,
                    "description": meta.description if meta else "",
                    "version": meta.version if meta else "",
                    "permission": self._effective_permission(sid, meta.permission if meta else "safe"),
                    "enabled": sid in plugins.skills.enabled,
                    "builtin": sid in self._preset_ids,
                    "source": str(origin.get("source", "")) if origin else "",
                    "updateable": bool(origin),
                    "registered": f"skill.{sid}" in registered,
                    "error": error,
                }
            )
        return items

    # -- 内部 ---------------------------------------------------------------
    def _git_checkout(self, source: str, workdir: Path) -> Path:
        """浅克隆 git 来源到 workdir/repo；失败抛 ValueError（可读、去敏）。"""
        dest = workdir / "repo"
        try:
            proc = subprocess.run(  # noqa: S603 - 固定参数列表，无 shell
                ["git", "clone", "--depth", "1", source, str(dest)],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except FileNotFoundError as exc:
            raise ValueError("未找到 git 命令，无法从 git 来源导入。") from exc
        except subprocess.TimeoutExpired as exc:
            raise ValueError("git 克隆超时（120s），已放弃。") from exc
        if proc.returncode != 0:
            raise ValueError("git 克隆失败：请检查地址与网络。")
        return dest

    @staticmethod
    def _discover(root: Path) -> list[Path]:
        """发现规则（spec §2.3）：根目录含 SKILL.md → 根；否则一级子目录逐个找。"""
        if (root / "SKILL.md").is_file():
            return [root]
        if not root.is_dir():
            return []
        return [p for p in sorted(root.iterdir()) if p.is_dir() and (p / "SKILL.md").is_file()]

    @staticmethod
    def _write_origin(skill_dir: Path, data: dict) -> None:
        _atomic_write_text(skill_dir / _ORIGIN_FILE, json.dumps(data, ensure_ascii=False, indent=2))

    @staticmethod
    def _read_origin(skill_dir: Path) -> dict | None:
        path = Path(skill_dir) / _ORIGIN_FILE
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None
