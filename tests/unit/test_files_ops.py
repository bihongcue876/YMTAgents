"""core.files.ops：读 / 查 / 写 / 改（v0.0.11 切片 H）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.files import ops
from core.files.paths import FileAmbiguous, FileNoMatch, FilePathError


def _root(tmp_path: Path) -> tuple[Path, Path]:
    data = tmp_path / "ymtdata"
    root = data / "workspace"
    (data / "config").mkdir(parents=True)
    root.mkdir(parents=True)
    return root, data


def test_read_text_numbers_lines_and_reports_total(tmp_path):
    root, _data = _root(tmp_path)
    path = root / "a.txt"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    out = ops.read_text(path)
    assert "1| one" in out["text"] and "3| three" in out["text"]
    assert out["total_lines"] == 4
    assert out["truncated"] is False


def test_read_text_window_and_truncation_flag(tmp_path):
    root, _data = _root(tmp_path)
    path = root / "big.txt"
    path.write_text("\n".join(str(i) for i in range(1, 301)), encoding="utf-8")
    out = ops.read_text(path, offset=10, limit=5)
    assert out["shown_from"] == 10 and out["shown_to"] == 14
    assert out["truncated"] is True


def test_read_text_rejects_binary(tmp_path):
    root, _data = _root(tmp_path)
    path = root / "blob.bin"
    path.write_bytes(b"abc\x00def")
    with pytest.raises(FilePathError):
        ops.read_text(path)


def test_glob_returns_files_only_and_excludes_hidden_denied_and_vcs(tmp_path):
    root, data = _root(tmp_path)
    (root / "pkg").mkdir()
    (root / "pkg" / "mod.py").write_text("x", encoding="utf-8")
    (root / "top.py").write_text("x", encoding="utf-8")
    (root / ".hidden.py").write_text("x", encoding="utf-8")
    (root / "keep.pem").write_text("x", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "conf.py").write_text("x", encoding="utf-8")
    out = ops.glob_files("*.py", base=root, data_root=data)
    assert {item["path"] for item in out["files"]} == {"top.py", "pkg/mod.py"}
    deep = ops.glob_files("pkg/*.py", base=root, data_root=data)
    assert [item["path"] for item in deep["files"]] == ["pkg/mod.py"]


def test_grep_reports_line_numbers_and_respects_include(tmp_path):
    root, data = _root(tmp_path)
    (root / "a.py").write_text("alpha\nTARGET\n", encoding="utf-8")
    (root / "b.txt").write_text("TARGET\n", encoding="utf-8")
    out = ops.grep_files("TARGET", base=root, data_root=data)
    assert out["count"] == 2
    only_py = ops.grep_files("TARGET", base=root, data_root=data, include="*.py")
    assert [(m["path"], m["line"]) for m in only_py["matches"]] == [("a.py", 2)]


def test_edit_unique_match_and_line_ending_preserved(tmp_path):
    root, _data = _root(tmp_path)
    path = root / "crlf.txt"
    path.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    out = ops.edit_text(path, "two", "TWO")
    assert out["noop"] is False and out["replacements"] == 1
    assert path.read_bytes() == b"one\r\nTWO\r\nthree\r\n"


def test_edit_no_match_and_ambiguous(tmp_path):
    root, _data = _root(tmp_path)
    path = root / "a.txt"
    path.write_text("same\nsame\n", encoding="utf-8")
    with pytest.raises(FileNoMatch):
        ops.edit_text(path, "absent", "x")
    with pytest.raises(FileAmbiguous):
        ops.edit_text(path, "same", "x")
    assert ops.edit_text(path, "same", "x", replace_all=True)["replacements"] == 2


def test_write_does_not_rewrite_content_and_requires_parent(tmp_path):
    root, _data = _root(tmp_path)
    path = root / "cfg.txt"
    payload = "api_key=sk-abcdef12345\n"
    ops.write_text(path, payload)
    assert path.read_text(encoding="utf-8") == payload
    with pytest.raises(FilePathError):
        ops.write_text(root / "missing" / "x.txt", "x")


def test_write_preserves_existing_crlf_and_backs_up(tmp_path):
    root, _data = _root(tmp_path)
    path = root / "crlf.txt"
    path.write_bytes(b"a\r\nb\r\n")
    ops.write_text(path, "a\nB\n")
    assert path.read_bytes() == b"a\r\nB\r\n"
    assert (root / "crlf.txt.bak").exists()


def test_grep_only_scans_prefix_window_of_long_lines(tmp_path):
    root, data = _root(tmp_path)
    in_window = "A" * 4000 + "NEEDLE"      # 命中点在 4096 窗口内：可见
    out_window = "B" * 5000 + "NEEDLE"     # 命中点在窗口外：按「不扫」如实缺席
    (root / "long.txt").write_text(in_window + "\n" + out_window + "\n", encoding="utf-8")
    out = ops.grep_files("NEEDLE", base=root, data_root=data)
    assert [m["line"] for m in out["matches"]] == [1]
    assert out["overflow"] is False


def test_grep_rejects_backtracking_style_pattern(tmp_path):
    root, data = _root(tmp_path)
    (root / "x.txt").write_text("aaaa\n", encoding="utf-8")
    with pytest.raises(FilePathError):
        ops.grep_files("(a+)+b", base=root, data_root=data)
    # 正常带组/量词的模式不受误伤
    ok = ops.grep_files("(a+)(b+)", base=root, data_root=data)
    assert ok["count"] == 0


def test_write_rejects_oversized_content(tmp_path, monkeypatch):
    root, _data = _root(tmp_path)
    monkeypatch.setattr(ops, "MAX_WRITE_CHARS", 16)
    with pytest.raises(FilePathError):
        ops.write_text(root / "big.txt", "x" * 17)
