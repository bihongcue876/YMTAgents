"""pytest 全局配置：GUI 测试使用离屏平台并禁用 WebEngine。"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("YMT_NO_WEBENGINE", "1")
