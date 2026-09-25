"""core.files.paths：路径判定与禁止区（v0.0.11 切片 H）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.files.paths import (
    FileDenied,
    FileMissing,
    FilePathError,
    classify,
    needs_review,
    resolve_target,
)


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    data = tmp_path / "ymtdata"
    root = data / "workspace"
    (data / "config").mkdir(parents=True)
    (data / "secrets").mkdir(parents=True)
    root.mkdir(parents=True)
    return root, data


def test_inside_workspace_is_fully_allowed(tmp_path):
    root, data = _layout(tmp_path)
    access = classify(root / "src" / "main.py", workspace_root=root, data_root=data)
    assert (access.read, access.write, access.search, access.inside_workspace) == (True, True, True, True)
    assert needs_review(access) is False


def test_outside_read_needs_review_and_write_is_denied(tmp_path):
    root, data = _layout(tmp_path)
    other = tmp_path / "outside.txt"
    other.write_text("x", encoding="utf-8")
    access = classify(other, workspace_root=root, data_root=data)
    assert access.read is True and access.write is False and access.search is False
    assert needs_review(access) is True


def test_filename_deny_wins_even_inside_workspace(tmp_path):
    root, data = _layout(tmp_path)
    for name in (".env", "id_rsa", "server.pem", "app.key", "vault.dat", "cert.pfx"):
        access = classify(root / name, workspace_root=root, data_root=data)
        assert access.read is False and access.write is False, name


def test_private_data_root_is_read_only_and_not_searched(tmp_path):
    root, data = _layout(tmp_path)
    access = classify(data / "config" / "settings.json", workspace_root=root, data_root=data)
    assert access.read is True and access.write is False and access.search is False


def test_secrets_vcs_and_data_home_are_fully_denied(tmp_path):
    root, data = _layout(tmp_path)
    for path in (data / "secrets" / "store.bin", root / ".git" / "HEAD", root / ".ymtdata" / "AGENTS.md"):
        access = classify(path, workspace_root=root, data_root=data)
        assert access.read is False and access.write is False, str(path)


def test_resolve_target_relative_anchors_on_root(tmp_path):
    root, _data = _layout(tmp_path)
    target = root / "a.txt"
    target.write_text("hi", encoding="utf-8")
    assert resolve_target("a.txt", root=root, must_exist=True) == target


def test_resolve_target_missing_raises_file_missing(tmp_path):
    root, _data = _layout(tmp_path)
    with pytest.raises(FileMissing):
        resolve_target("nope.txt", root=root, must_exist=True)


def test_resolve_target_rejects_symlink(tmp_path):
    root, _data = _layout(tmp_path)
    real = root / "real.txt"
    real.write_text("x", encoding="utf-8")
    link = root / "link.txt"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("本机不允许创建符号链接")
    with pytest.raises(FileDenied):
        resolve_target("link.txt", root=root)


def test_resolve_target_rejects_windows_reserved_names(tmp_path):
    root, _data = _layout(tmp_path)
    for name in ("NUL", "con.txt", "COM1", "aux.md", "lpt2.log"):
        with pytest.raises(FilePathError):
            resolve_target(name, root=root)


def test_resolve_target_rejects_junction_into_private_zone(tmp_path):
    root, data = _layout(tmp_path)
    (data / "config" / "settings.json").write_text("{}", encoding="utf-8")
    junction = root / "link"
    created = False
    try:
        import _winapi  # type: ignore[import-not-found]

        _winapi.CreateJunction(str(data / "config"), str(junction))
        created = True
    except (ImportError, OSError, NotImplementedError):
        pytest.skip("本机无法创建 junction")
    # junction 不被 is_symlink 识别：必须与符号链接同拒（安全修订轮）。
    with pytest.raises(FileDenied):
        resolve_target("link/settings.json", root=root)
