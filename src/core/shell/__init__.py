"""本地 shell 宿主（spec v0.0.5）。

本包只依赖 `shared` 与 `core.registry`（与 `core.mcp` 同层，见 docs 04 §1 依赖方向）。

- `process.py`：持久 shell 进程与两个协议方言（ps / posix）；
- `policy.py`：高危命令静态清单（调用前拒绝依据）；
- `manager.py`：`ShellManager`（池、注册、权限、审计、懒回收、宿主态）。
"""
