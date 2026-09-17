"""日志装配（docs 05 §4⑧「日志与诊断」）。

- 级别取自 `settings.logging.level`：启动时应用、设置变更时热更新
  —— 此前该设置只落盘、**从不生效**（设置页那个下拉是假控件，spec rev9 §4）。
- 文件日志落 `<数据根>/logs/app.log`（轮转 1MB×3），「打开日志目录」因此名副其实。
- 所有日志经 `shared.redact.redact` 过滤：密钥形态一律掩码（docs 09 B3 的最后一道防线）。

装配点：`bootstrap()`（数据根已知之后）。重复装配会**替换**旧的文件 Handler，
故测试反复 bootstrap（各自临时数据根）不会累积 Handler。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from shared.redact import redact

LOG_FILENAME = "app.log"
_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


class RedactingFilter(logging.Filter):
    """在落地前把日志正文中的密钥形态打码。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - 格式化失败不应影响日志本身
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True


def level_of(level: str) -> int:
    """设置里的级别名 → logging 级别；未知回退 INFO。"""
    return getattr(logging, str(level or "INFO").upper(), logging.INFO)


def apply_level(level: str) -> None:
    """热更新根 logger 级别（设置变更后调用；不新增 Handler）。"""
    logging.getLogger().setLevel(level_of(level))


def configure(root: Path | str, level: str = "INFO") -> Path:
    """装配控制台 + 文件日志，返回日志目录。幂等：重入只替换文件 Handler。"""
    root_logger = logging.getLogger()
    root_logger.setLevel(level_of(level))

    formatter = logging.Formatter(_FORMAT)
    redactor = RedactingFilter()

    for handler in list(root_logger.handlers):
        if isinstance(handler, RotatingFileHandler):
            root_logger.removeHandler(handler)
            handler.close()

    if not any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)
        for h in root_logger.handlers
    ):
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        console.addFilter(redactor)
        root_logger.addHandler(console)

    log_dir = Path(root) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / LOG_FILENAME, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redactor)
    root_logger.addHandler(file_handler)
    return log_dir
