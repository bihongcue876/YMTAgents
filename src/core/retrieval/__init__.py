"""检索模块（第七个附加功能；spec-2026-09-25-retrieval）。

工具前缀 `search.*`；引擎端点常量与密钥 vault 命名单一来源。
"""

from core.retrieval.manager import IRetrievalManager, RetrievalManager

__all__ = ["IRetrievalManager", "RetrievalManager"]
