"""共享枚举词汇。

对应 docs 03 §8（状态词汇）、docs 09 §2（权限档）、spec §1.1（槽位）。
本模块只依赖标准库，禁止 import 项目内任何其它包。
"""

from enum import Enum


class ModuleState(str, Enum):
    """模块运行态（docs 03 §8）。期望态见 modules.json。"""

    DISABLED = "disabled"
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    ERROR = "error"
    STOPPING = "stopping"


class Permission(str, Enum):
    """工具权限档（docs 09 §2）。"""

    SAFE = "safe"
    CONFIRM = "confirm"
    RESTRICTED = "restricted"
