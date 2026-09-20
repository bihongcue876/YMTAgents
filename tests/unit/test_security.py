"""数据安全专项测试：脱敏层与日志装配（spec rev9 §3/§4）。

密钥只入本地加密库（v0.0.2），**永不**进入 args / 提示词 / 事件流 / 日志 / 错误信息（docs 09 B3）；
本文件固化「最后一道防线」：任何要落盘或回显的文本都过 `redact()`。
"""

from __future__ import annotations

import logging

from core.agent.session import SessionStore
from shared.redact import MASK, redact

SECRET = "sk-LEAK1234567890"


# -- 脱敏函数 -----------------------------------------------------------------


def test_redact_masks_common_key_shapes():
    assert SECRET not in redact(f"调用失败：{SECRET}")
    assert MASK in redact("Authorization: Bearer abcdefgh12345678")
    assert MASK in redact(f'{{"api_key": "{SECRET}"}}')
    assert MASK in redact(f"secret={SECRET}")
    assert MASK in redact("xoxb-1234567890-abcdef")


def test_redact_passes_plain_text_and_none():
    assert redact(None) is None
    assert redact("") == ""
    text = "普通中文与代码片段：print(1)  # 不含密钥"
    assert redact(text) == text


# -- 日志装配：级别生效 + 文件落地 + 脱敏 --------------------------------------


def test_configure_applies_level_and_writes_file(tmp_path):
    """「日志级别」与「打开日志目录」都必须名副其实（此前级别从不生效、目录里只有 audit）。"""
    from app import logging_setup

    root_logger = logging.getLogger()
    original = root_logger.level
    try:
        log_dir = logging_setup.configure(tmp_path, "DEBUG")
        assert root_logger.level == logging.DEBUG
        assert log_dir == tmp_path / "logs"

        logging.getLogger("ymt.test").debug("调试信息 %s", SECRET)
        for handler in root_logger.handlers:
            handler.flush()
        content = (log_dir / logging_setup.LOG_FILENAME).read_text(encoding="utf-8")
        assert "调试信息" in content or "已脱敏" in content
        assert SECRET not in content  # 落盘前已打码
    finally:
        root_logger.setLevel(original)


def test_apply_level_is_hot_updatable(tmp_path):
    from app import logging_setup

    root_logger = logging.getLogger()
    original = root_logger.level
    try:
        logging_setup.apply_level("WARN")
        assert root_logger.level == logging.WARNING
        logging_setup.apply_level("不认识的级别")  # 未知回退 INFO，不得抛
        assert root_logger.level == logging.INFO
    finally:
        root_logger.setLevel(original)


def test_configure_replaces_file_handler(tmp_path):
    """重复装配（测试各自临时数据根）不得累积 Handler。"""
    from app import logging_setup

    from logging.handlers import RotatingFileHandler

    logging_setup.configure(tmp_path / "root_a", "INFO")
    logging_setup.configure(tmp_path / "root_b", "INFO")
    files = [h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)]
    assert len(files) == 1
    assert (tmp_path / "root_b" / "logs" / logging_setup.LOG_FILENAME).exists()


# -- 会话收尾 fsync（docs 03 §10） -------------------------------------------


def test_session_end_appends_and_fsyncs(tmp_path, monkeypatch):
    """回归锚点：`end()` 此前无调用点，「会话结束 fsync」实际不发生。"""
    calls: list[int] = []
    monkeypatch.setattr("core.bus.sink.os.fsync", lambda fd: calls.append(fd))

    store = SessionStore(tmp_path)
    meta = store.create(None, None)
    store.end(meta.id, "app_exit")

    assert calls, "end() 必须触发 fsync"
    assert store.replay(meta.id)[-1]["type"] == "session.end"
    assert store.replay(meta.id)[-1]["payload"]["reason"] == "app_exit"


def test_session_end_is_safe_without_events_file(tmp_path):
    store = SessionStore(tmp_path)
    store.end("sess_不存在", "app_exit")  # 目录都不存在也不得抛


# -- 传输保密性（rev15） -------------------------------------------------------


def test_secure_transport_rules():
    """凭据与对话内容不得走明文：https 唯一合法，http 仅限本机回环（本地模型服务）。"""
    from shared.net import is_secure_transport

    assert is_secure_transport("https://api.deepseek.com/v1")
    assert is_secure_transport("https://api.test.com")
    assert is_secure_transport("http://127.0.0.1:11434/v1")  # Ollama 等本地服务
    assert is_secure_transport("http://localhost:1234/v1")  # LM Studio
    assert not is_secure_transport("http://api.evil.com/v1")  # 远程明文 → 拒
    assert not is_secure_transport("http://192.168.1.10:8000/v1")  # 局域网也非本机
    assert not is_secure_transport("ftp://api.test.com")  # 未知 scheme → 拒


def test_static_no_tls_verification_disabled():
    """TLS 校验不得被关闭：全 src 扫描 verify=False / CERT_NONE 等。"""
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src"
    pattern = re.compile(r"verify\s*=\s*False|verify=False|CERT_NONE|check_hostname\s*=\s*False")
    offenders = [
        f"{path.relative_to(src)}:{lineno}"
        for path in sorted(src.rglob("*.py"))
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert not offenders, "不得关闭 TLS 校验：\n" + "\n".join(offenders)


def test_provider_presets_scheme_discipline():
    """预设表纪律：云供应商全 https；本地服务全 http + 回环。"""
    from gui.pages.models import CLOUD_PRESETS, LOCAL_PRESETS
    from shared.net import is_local_url, is_secure_transport

    for name, url in CLOUD_PRESETS.items():
        assert url.startswith("https://"), name
    for name, url in LOCAL_PRESETS.items():
        assert url.startswith("http://"), name
        assert is_local_url(url), name
    for name, url in {**CLOUD_PRESETS, **LOCAL_PRESETS}.items():
        assert is_secure_transport(url), name
