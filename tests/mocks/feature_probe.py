"""惰性 import 探针：仅当工厂被调用时才会进入 `sys.modules`（切片 0 六指标测试用）。

本模块**不得**被任何生产代码或其它测试 import —— 否则「关档不 import」的断言失真。
"""

PROBE = "feature_probe"