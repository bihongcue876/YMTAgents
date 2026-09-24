"""BTCM 模块包（切片 1/2/3）：引擎 + 宿主。

关档时本包不被 import（`FeatureManager` 工厂惰性构造），符合「关 = 真卸载」六指标。
"""

from __future__ import annotations

from core.modules.btcm.manager import TOOL_THINK, BtcmManager

__all__ = ["TOOL_THINK", "BtcmManager"]