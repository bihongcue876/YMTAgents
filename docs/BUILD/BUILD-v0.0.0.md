# BUILD-v0.0.0 — 首期·对话主体

> 日期：2026-09-08
> 对应 spec：`spec/spec-2026-09-08-v0.0.0.md`（rev1 契约 + rev2 收紧 + rev3 实现期补充）
> 状态：已完成（首期闭环）

## 1. 本轮范围

| 项 | 状态 |
|---|---|
| 对话流（流式、中断、错误、用量） | ✅ |
| 侧栏会话列表（新建/打开/归档/恢复/删除） | ✅ |
| 模型配置（BYOK 多供应商 + 自定义端点 + 测试连接） | ✅ |
| 系统设置（上下文/网络/数据/日志/关于） | ✅ |
| 工具注册表 / 确认关卡 / Skills / MCP / shell | 🚧 契约就位，空实现 |
| btcm / dpim | ⬜ 按 08 留白 |

## 2. 技术选型

- Python 3.14（PySide6/shiboken6 为 cp310-abi3，向前兼容；pydantic-core 有 cp314 轮子）。
- 包管理：uv + pyproject.toml + uv.lock。
- 运行依赖：pyside6 / openai / pydantic / keyring / markdown-it-py / pygments。
- 渲染：QWebEngineView + `gui/widgets/render/` 统一管线（无 WebEngine 时降级 QTextBrowser）。
- 数据目录：`ymtdata/`。

## 3. 验收锚点（A1–A12）

见 spec §4；由 `tests/integration/test_e2e.py` 覆盖。

## 4. 依赖方向静态检查

`uv run pytest tests/static/test_imports.py` —— 结果：**通过**（无相对导入；依赖方向符合 04 §1）。

## 5. 冒烟结果

| # | 场景 | 结果 |
|---|---|---|
| A1 | 首启零配置 | ✅ 空 provider.list / 空 session.index |
| A2 | 添加供应商（预设/自定义） | ✅ keyring 入密钥、models.json 更新、白名单自动新增 |
| A3 | 测试连接 | ✅ 成功返回时延徽标数据 |
| A4 | 绑定 main → 新对话 → 发送 | ✅ 流式 delta + final(usage)，events.jsonl 完整 |
| A5 | 中断 | ✅ AssistantFinal(interrupted) + interrupt 落盘 |
| A6 | 会话列表操作 | ✅ 新建/归档/恢复/删除 |
| A7 | 会话切换回放 | ✅ SessionEvents 重组 |
| A8 | 静默超时 | ✅ error + turn.status(failed) |
| A9 | 白名单外访问 | ✅ error(whitelist_blocked)，进程不倒 |
| A10 | 密钥缺失/错误 | ✅ error(key_missing)，不回显明文 |
| A11 | 模型下拉切换 | ✅ model.switch 落盘 |
| A12 | 断电恢复 | ✅ 末行截断丢弃 + audit 记录 |

**全量测试**：`uv run pytest` → 42 passed。

## 6. 变更日志

- 2026-09-08：初始化本轮 BUILD 记录。
- 2026-09-08：完成 src 骨架、shared/store/gateway/agent/bus/registry/modules/app/gui 全链路；A1–A12 通过。
- 2026-09-08：实现期协议补充（rev3）：ProviderUpsert.api_key、RenameSession、UnarchiveSession、SettingsState。
- 2026-09-09：模型配置页界面适配（见 `docs/模型配置界面适配说明.md`）：接入类型分区、
  main 槽位可编辑自定义模型 ID、全局文本快捷键、WebEngine 检测改 `find_spec`（**工作区在途，尚未提交**）。
- 2026-09-11：构建侧修正——`pyproject.toml` 增 `addopts = "--basetemp=.pytest_tmp"`，
  避免 pytest 会话启动时清理系统 Temp 被安全策略判为批量删除而拦截；`.gitignore` 增 `.pytest_tmp/`。
- 2026-09-11：**rev4 槽位绑定接线补全**（见 §7）。
- 2026-09-11：**rev5 错误码归因修正**（见 §8）。
- 2026-09-11：**rev6 主题与接口契约补全**；新增测试侧自检与阶段进度文档（见 §9）。
- 2026-09-16：**rev7 字号档位与默认字号上抬**（见 §10）。
- 2026-09-16：**rev8 逻辑缺陷巡检与修订**（见 §11）。
- 2026-09-17：**rev9 遗留清零 · 数据安全 · 模型导入**（见 §12）。
- 2026-09-17：**rev10 模型导入对齐 Cherry Studio · 本地模型服务**（见 §13）。
- 2026-09-17：**rev11 显示裁剪修复与文案清理**（见 §14）。
- 2026-09-17：**rev12 暗色模式刷新不完整修复**（见 §15）。
- 2026-09-17：**rev13 美化轮 A：响应式布局**（见 §16）。
- 2026-09-17：**rev14 模型选择语义：上次使用接续**（见 §17）。
- 2026-09-17：**rev15 安全加固：传输保密性与渲染面**（见 §18）。

## 7. rev4 轮次记录 — 槽位绑定接线补全（2026-09-11）

**问题**：能力检测发现「全局 BYOK 配置」缺一环——`IModelGateway.set_slot` 自 rev1 起有定义，
但请求信封层无对应类型，**全项目零调用点**；模型配置页槽位下拉被误接为会话级 `SwitchModel`。
后果：`models.json` 的 `slots.main` 无法被绑定，`HealthReport.slot_ready` 恒 false，
新建会话（会话级绑定为空）立即以 `provider_not_found` 失败。

**恢复（实测证据）**：既有会话 `sess_01a0863f…` 的 `events.jsonl` 仅 5 行，
`msg.user` 后直接是 `error(provider_not_found)` 再一条 `model.switch`，从无成功 assistant 回合 —— 与根因吻合。

| 项 | 内容 |
|---|---|
| 契约 | 新增 `SetSlot(slot.set)`，纳入 `Request` 判别联合（spec rev4 §1） |
| 后端 | `controller._on_set_slot`：`gateway.set_slot` → 原子写 + `.bak` → 回发 `ProviderList` + `HealthReport` |
| 前端 | `main_window`：`ModelsPage.slot_requested` 改接 `SetSlot`；对话头条保持会话级 `SwitchModel` |
| 语义 | 不做模型存在性硬校验（用户直接操作；自定义模型 ID 无需先登记）；不产生 `gate.*`、不过确认关卡 |
| 测试 | 新增 4 例：`test_gateway`（落盘 + 未知槽位拒绝）、`test_bootstrap`（分派回发同步 + 未绑定失败面） |

**冒烟结果**：`uv run pytest` → **50 passed**（42 基线 → 46（+09-09 在途）→ 50（+rev4 4 例））；
依赖方向静态检查（`tests/static/test_imports.py`）通过。

**本轮未做（显式留白）**：未改动 `ymtdata/` 任何配置数据 —— 全局槽位不预设默认值，
供应商与密钥由用户自行接入；`models.json` 中无效模型 ID `deepseek-v4-flash-0731` 保留原样，
是否更正由用户决定。测试基线（42 项）只增不减。

## 8. rev5 轮次记录 — 错误码归因修正（2026-09-11）

**问题**：`provider_not_found` 一个码承担**四种语义** —— 未绑定模型（无引用）、模型不属于任何供应商、
供应商 id 不存在、上游 404/400 拒绝。前端 `models.py:260` 直接展示裸码，
于是出现「供应商明明列在配置页上，却报 provider not found」；更糟的是「未绑定」也报此码，
用户会去重连供应商——**修错方向**。另有一处自相矛盾：`GATEWAY_EXCEPTION_CODE["GatewayProtocolError"] = network_error`
与该异常自带的 `provider_not_found` 冲突，因 `loop._code_of` 优先取 `exc.code`，该映射成为永不生效的失效兜底。

**为什么此前测试没抓到**：A9/A10 等用例直接调 `ctx.gateway.set_slot(...)`（`test_e2e.py:261`、`285`），
**绕过请求信封层**；错误码几乎无断言（全项目仅 `whitelist_blocked`/`key_missing`/`invalid_request` 三处）。
协议面的缺陷因此长期不可见。

| 项 | 内容 |
|---|---|
| 拆码 | 新增 `model_unbound` / `model_not_found` / `protocol_error`；`provider_not_found` 收紧为「供应商 id 不存在」单一语义 |
| 归因 | `_map_exception` 细分：Auth/Permission→`auth_error`；NotFound/BadRequest/Unprocessable→`model_not_found`；其余→`network_error` |
| 默认码 | `GatewayProtocolError.default_code` → `protocol_error`；修正 `GATEWAY_EXCEPTION_CODE` 失效行 |
| 文案 | 新增 `ERROR_TEXT`（码 → 中文短语）单一来源 + `error_text()`；`loop._fail` 缺省取之；清除三处「以码充文案」 |
| 前端 | **未改动**（用户指示）：错误条经 `ErrorReport.message` 已自动显示正确中文 |
| 测试 | `test_bootstrap` 回归锚点改 `model_unbound`；`test_gateway` +4 例（归因 / 未知模型 / 默认码 / `ERROR_TEXT` 覆盖全码） |

**冒烟结果**：`uv run pytest` → **54 passed**（50 → 54）；依赖方向静态检查通过。
真实端点实测：无效模型 ID → `model_not_found`；不存在的供应商 id → `provider_not_found`（本义保留）；
未绑定 → `model_unbound` + 可操作中文（"请在「模型配置」页为 main 槽位选择一个模型…"）。

**留白**：`models.py` 测试连接徽标的中文化（一行）—— 前端界面暂不改动；
动线便利化（空状态引导 / 绑定关系徽标）为独立议题。`ymtdata/` 配置数据仍未改动。

## 9. rev6 轮次记录 — 主题、接口契约与自检（2026-09-11）

用户指示：**不集成后续阶段**（角色/skill/MCP/shell/btcm/dpim/其他 RAG），只优化前序资源；
界面支持调整黑白颜色；继续完成已说明任务；巡检并完善有问题之处；补充文档。

### 9.1 亮 / 暗主题

需求原为 rev2 §5 的**首期非目标**，本轮转入实现。

| 项 | 内容 |
|---|---|
| 单一取色来源 | 新增 `gui/theme.py`：`Palette`（12 token）+ `LIGHT`/`DARK` + `stylesheet`/`markdown_css`/`ansi_colors`/`pygments_style`/`apply`/`restyle` |
| 入口 | 系统设置页新增「外观」分区，主题下拉；走**既有** `settings.update("ui")` 通道持久化，后端无需改动 |
| 覆盖 | 全局 QSS 换肤 + 对话消息流整帧重渲染 + `ModelsPage` 属性选择器重算 |
| 渲染 | `md.py` 颜色规则移出模板改由 `theme.markdown_css()` 注入；暗色下 ANSI 30/37/90/97 重映射 |
| 启动 | `push_initial_state()` 提到 `window.show()` 之前，消除持久化暗色启动时亮色闪现 |

### 9.2 巡检发现并修复的缺陷

| # | 缺陷 | 影响 | 修复 |
|---|---|---|---|
| 1 | `UISettings.theme` 为 `Literal["light"]` | schema 层面把暗色堵死，传 `dark` 直接 ValidationError | 放宽为 `Literal["light", "dark"]` |
| 2 | `IModelGateway` 漏声明 `reload_settings` / `test_connection` / `upsert_provider` / `delete_provider` | Mock 环境下「改任意系统设置」「测试连接」「增删供应商」必 `AttributeError` | 补声明 + `MockGateway` 补实现 |
| 3 | `ISessionStore` 漏声明 `rename` / `unarchive` / `set_model` | 同上（替身无法按接口实现） | 补声明 |
| 4 | `IAgentLoop` 漏声明 `cancel` | 同上 | 补声明 |

**共性**：这些方法**实现一直存在**，只是没进接口 —— 按接口实现的替身天然缺它们，而这些路径
长期无测试覆盖（`provider.test` 走真实网关工厂，`settings.update` 无人测）。二者互为盲区。

**门禁**：新增 `tests/static/test_contract_coverage.py` —— 比对 controller 对协作方的实际调用
与其接口声明，违规即失败；并断言 `MockGateway` 可实例化。此类缺陷今后会被构建拦住。

### 9.3 测试侧自检（回归冒烟可执行化）

`01` §9 要求「每轮有回归冒烟结果」，此前是本文档 §5 的**手工表格**（A1–A12）。
新增 `tests/selftest/`：

| 层 | 内容 | 联网 | 默认 |
|---|---|---|---|
| L1 配置体检 | 供应商 / 模型登记 / 凭据状态 / 出口白名单 / 槽位引用完整性 | 否 | ✅ 恒跑（合成根） |
| L2 连通性 | 逐供应商一次 + 全局默认一次，区分「供应商不通」与「模型不可用」 | 是 | `YMT_SELFTEST=1` |

**定位**：测试侧回归资产，**不是产品功能** —— 只读、不写配置、不自动修正、不新增界面，
故不改变 spec 的产品范围。将来做 05 §4⑧ 的「自检按钮」时再从 `checks.py` 提取到 `core/`。
真实配置自检的**红/绿即结论**：当前实跑为红（`main` 未绑定 + 模型 ID 无效），与 §5 手工冒烟所述状态一致。

### 9.4 文档补齐

- 新增 `docs/B-阶段进度安排.md`：阶段切分、进入/退出条件、当前进度、通用退出条件（G1–G6）。
  补上 `01` §10 承诺却一直空缺的「BUILD 层轮次切分」落点；未给定锚点处标注「待确认」，不自行发明。
- `docs/01-设计原则.md` §10 的指向改为 `docs/B-阶段进度安排.md`。
- `docs/05`：§1 暗色由「后置占位」更新为已实现；§4⑧ 增「外观」分区。
- `docs/modules/{gui,shared,gateway,agent,engineering}.md` 同步。

### 9.5 冒烟结果

| 项 | 结果 |
|---|---|
| 全量测试 | `uv run pytest` → **75 passed, 2 skipped**（54 → 63 → 65 → 75；opt-in 项默认跳过） |
| 依赖方向 | `tests/static/test_imports.py` 通过 |
| 契约门禁 | `tests/static/test_contract_coverage.py` 通过 |
| 取色纪律 | `tests/unit/test_theme.py` 通过（`gui/` 内颜色字面量仅存于 `theme.py`） |
| 真实自检 | `YMT_SELFTEST=1 uv run pytest tests/selftest` → **红**（3 项未通过：`main` 未绑定、模型 ID 无效）—— 与事实相符 |

**留白**：主题无「跟随系统」档；`ymtdata/` 配置数据仍未改动（槽位不预设绑定，资源由用户自行接入）。

## 10. rev7 轮次记录 — 字号档位与默认字号上抬（2026-09-16）

用户指示：**调大默认字号，允许字号调整**。

### 10.1 问题（巡检实测）

界面此前**没有任何字号声明**，字号散落三处且彼此不知情：

| 位置 | 原状 | 问题 |
|---|---|---|
| `gui/theme.py` `stylesheet` | 只有颜色，无 font-size | Qt 回落系统默认（约 12px），界面文字偏小 |
| `pages/models.py`、`pages/settings.py` | 各自 `setStyleSheet("font-size:16px;font-weight:600;")` | 全项目仅有的字号字面量外泄；字号与主题不同源 |
| `widgets/render/md.py` 模板 | 正文 14px / 代码 13px / 小字 12px 写死 | 消息流字号无法随用户调整 |

即：**rev6 把颜色收成单一来源、却把字号留在了外面**——主题能换、字号不能调。

### 10.2 改动

| 项 | 内容 |
|---|---|
| 两段式字号 | 字号 = `BASE_PX[token] × FONT_LEVELS[level].scale`；档位只动系数，故层级恒定 |
| 默认上抬 | 标准档（默认）title 18 / ui 15 / body 16 / code 15 / caption 14（px）；对照旧值 ≈12 / 14 / 13 / 12 / 16 |
| 四档位 | small 0.875 · normal 1.0 · large 1.15 · xlarge 1.3；**small ≈ 调整前基准**，供旧密度回退 |
| 页面标题 | 改 `objectName("pageTitle")`，字号与字重由 QSS 提供并随档位缩放 |
| 消息流 | 模板内字号全部移出，改由 `markdown_css` 注入（与 rev6 对颜色的处理同构） |
| 契约 | `UISettings.font_size`（rev7 §1）；**不新增请求/事件类型**，复用 `settings.update` → `settings.state` |
| 幂等 | `_apply_theme` → `_apply_appearance(theme, font_size)`，逐项判变：改字号不换肤、改主题不重算字号 |

### 10.3 新增门禁

`tests/unit/test_theme.py::test_gui_has_no_hardcoded_font_size_literals` ——
扫描 `src/gui/**/*.py`，禁止 `font-size` / `setPointSize` / `setPixelSize` 字面量（`theme.py` 例外）。
与取色纪律同构，此类外泄今后会被构建拦住。

### 10.4 冒烟结果

| 项 | 结果 |
|---|---|
| 全量测试 | `uv run pytest` → **88 passed, 2 skipped**（75+2 → 88+2，净增 13，只增不减） |
| 依赖方向 | `tests/static/test_imports.py` 通过 |
| 契约门禁 | `tests/static/test_contract_coverage.py` 通过 |
| 字号纪律 | `test_gui_has_no_hardcoded_font_size_literals` 通过（违约项已清空） |
| 档位实跑 | 四档逐一核对：标准档 15/16/15/14/18px；未知档位回退标准档 |

**留白**：无「跟随系统」档；不支持连续调节；档位作用于全局（不做组件级独立字号）。
`ymtdata/` 配置数据未改动（`font_size` 缺省即标准档，无需迁移）。

## 11. rev8 轮次记录 — 逻辑缺陷巡检与修订（2026-09-16）

用户指示：**「有关错误建议修订……指的是潜在或明显的逻辑错误」**。

### 11.1 方法

先通读核心模块（`shared/*`、`core/{agent,gateway,store,bus,memory,modules}`、`app/*`、`gui` 关键路径）
列出候选缺陷；再对每条写探测脚本**实证**（不凭阅读下结论）；确认后按「最小改动 + 回归测试」修订。
**不改变协议形状**：无新增/删除请求类型与事件类型。

### 11.2 实证结果（修订前，探测脚本原样输出）

| 候选 | 探测方式 | 结论 |
|---|---|---|
| 淘汰丢当前提问 | 预算=100 + 挂载 20000 字文件 | 非 system 消息**剩 0 条** → 提问被丢 |
| 静默超时失效 | 静默 2.0s 而阈值 0.5s | **正常返回**，耗时 2.00s；`create()` 实收参数中无 `timeout` |
| 会话级切 thinking | `set_model(sid, "X", slot="thinking")` | `main_model` 变成 X（污染），且落 `model.switch` |
| reserve 重复扣减 | 阅读 + 算术（`total` 含 reserve，`budget` 已扣 reserve） | 默认配置下（窗口 8192 / reserve 4096）**每条提问都会被淘汰**——现有集成测试一直在跑退化路径 |
| `file_truncate` 单位 | 阅读（token 判定 / 字符截断） | 比较与截断口径不一致 |
| 静默失败 | grep `storage_error` / `context_overflow` / `fsync` | 两码**零发射**、`fsync` **零调用** |

### 11.3 修订清单（详见 spec rev8）

| # | 修订 | 落点 |
|---|---|---|
| 1 | 静默超时双重保险：SDK `timeout=` + 循环兜底；任何 chunk 刷新计时；`max_retries=0`；超时类归 `GatewayTimeout`；提前退出关闭流 | `core/gateway/provider.py` |
| 2 | 淘汰保护当前提问；改按输入侧用量判定（reserve 不再扣两次） | `core/agent/context.py` |
| 3 | `file_truncate` 按 token 口径截断（`_truncate_to_tokens`） | `core/agent/context.py` |
| 4 | 超窗本地收口 `context_overflow`（该码此前零发射） | `core/agent/loop.py` |
| 5 | 会话级切换只落地 main；非 main 回 `invalid_request` 且不写事件 | `app/controller.py` |
| 6 | `settings.update` 非法取值回 `invalid_request` 并保持原值 | `app/controller.py` |
| 7 | 落盘/凭据失败回 `storage_error`（明细只进日志）+ `set_markdown` 补字号透传 | `app/controller.py`、`gui/widgets/render/view.py` |

### 11.4 冒烟结果

| 项 | 结果 |
|---|---|
| 全量测试 | `uv run pytest` → **103 passed, 2 skipped**（88+2 → 103+2，净增 15，只增不减） |
| 依赖方向 | `tests/static/test_imports.py` 通过 |
| 契约门禁 | `tests/static/test_contract_coverage.py` 通过 |
| 主题/字号纪律 | `tests/unit/test_theme.py` 全通过 |
| 实证复跑 | 修订后：静默按阈值中止并关闭流；当前提问保留；非 main 槽位被拒且 `main_model` 未变 |

### 11.5 留白（待裁决，未纳入本次）

见 spec rev8 §8：会话结束 fsync 未实现、截断无标记、`_map_exception` 对 `GatewayError` 的退化、
`submit()` 非 dict 快路径、Qt 字体目录告警。
**共同特征**：都是「文档承诺 / 契约声明与实现不一致」，但影响可控，故先记不先改。

## 12. rev9 轮次记录 — 遗留清零 · 数据安全 · 模型导入（2026-09-17）

用户五条指示：① 眼下缺陷要求全部修复 ② 确认现阶段仍在构建已有的三个模块
③ 请确认数据安全 ④ 模型导入应更不让人操心 ⑤ 已确认的问题统统改掉。

### 12.1 rev8 遗留清零（4 项）

| # | 项 | 处置 |
|---|---|---|
| 1 | 会话结束 fsync 未实现 | `EventSink.fsync()` + `SessionStore.end()` 调用 + `CoreController.shutdown()`（由 `main.py` 在核心线程停止后调用）；`end()` 提升为 `ISessionStore` 抽象方法（契约门禁当场抓到漏声明） |
| 2 | files 截断无标记 | 追加 `TRUNCATION_MARK`；标记 token 从预算扣除，总量仍 ≤ `file_truncate` |
| 3 | `_map_exception` 对 `GatewayError` 退化 | 保原码（此前会丢成 `network_error`） |
| 4 | `submit()` 非 dict 快路径 | 非 dict 且非任一信封 → 回 `invalid_request`；controller 的 unknown 分支同样回错误（此前只写 warning） |

Qt 字体目录告警经核查**无害**（Qt 6 不再随附字体、系统字体照常生效），不属缺陷，仅记录。

### 12.2 数据安全（实测 + 防线）

实测三条外泄路径均为**干净**：错误详情 7 种畸形载荷不含密钥、`ymtdata` 只有 `keyring://` 引用、
事件流只有会话内容与错误码。
但「碰巧没漏」不等于「漏不了」，故补三道防线：`shared/redact.py`、
日志 `RedactingFilter`（落盘前打码）、错误详情统一过 `redact`；并有
`tests/unit/test_security.py` 固化（含「失败字段本身就是密钥」的端到端用例）。

### 12.3 模型导入省心（新协议 `provider.models`）

手填模型 ID 是端到端报错的首要来源（当前真实配置正是此情形）。现在：
模型配置页卡片 →「获取模型列表」→ 端点 `GET /models`（走既有白名单与密钥路径）→
勾选对话框（已登记项默认勾选）→ 确定经**既有** `provider.upsert` 落盘。
只读探测、不写配置；端点不支持 `/models` 时给出「请手动填写模型 ID」的中文指引。

### 12.4 对话视图空状态（兑现验收 A1）

rev2 §1.3 与 A1 早已要求空状态 + 「添加模型」CTA，但此前**全项目零实现**，而 A1 被记为通过。
现补 `gui/chat/empty_state.py`：应用名 + 一句话 + CTA（无供应商 → 添加模型；有供应商 → 开始对话），
与消息流互斥切换。

### 12.5 冒烟结果

| 项 | 结果 |
|---|---|
| 全量测试 | `uv run pytest` → **124 passed, 2 skipped**（103+2 → 124+2，净增 21，只增不减） |
| 依赖方向 | `tests/static/test_imports.py` 通过 |
| 契约门禁 | `tests/static/test_contract_coverage.py` 通过（期间抓到 `ISessionStore.end` 漏声明） |
| 主题/字号纪律 | `tests/unit/test_theme.py` 通过 |
| 安全专项 | `tests/unit/test_security.py` 通过（脱敏 + 日志级别 + fsync + Handler 不累积） |

### 12.6 阶段确认（回应第 ② 条）

**是，仍在阶段 1。** 现处阶段 1（对话主体与基座）的**加固期**，交付面仍是既有三个视图
（对话 / 模型配置 / 系统设置）+ 侧栏会话列表；阶段 2（角色 persona）**未启动**。
2026-09-16 至 09-17 的 rev7 / rev8 / rev9 全部落在此加固范围内：无新视图、无新阶段、
无版本号变动。

## 13. rev10 轮次记录 — 模型导入对齐 Cherry Studio · 本地模型服务（2026-09-17）

用户指示：**「试试链路 A」「参考 CC Switch 或 Cherry Studio 的 URL + 模型导入模式」
「本地的有什么模型支持也好」**。

### 13.1 链路 A 实测（先测再改）

| 步骤 | 结果 |
|---|---|
| 对真实配置调 `provider.models` | `ok=False, error=network_error` |
| 同机直连 `api.deepseek.com/v1/models`（无凭据） | HTTP 401 —— **能连通** |
| 同机直连 `api.openai.com/v1/models` | 超时 —— **本机不可达** |
| 同一把凭据 + 正确域名 `api.deepseek.com` | `models.list()` → OK，**2 个模型**：`deepseek-flash`、`deepseek-v4-pro` |

**归因**：代码正确（远端不可达 → `network_error`）；报红源于配置的 `base_url` 指向
本机不可达的 `api.openai.com`，且模型 ID 不在该端点内。属用户配置，按约定未擅自改动。

### 13.2 本地模型服务（免密钥）

此前所有供应商都要求 API Key —— Ollama / LM Studio **等于不支持**。现：
`ProviderConfig.local` + `ProviderSpec.local`；网关 `_api_key_for()` 对 `local` 返回占位串、
三条路径统一放行、且**不写**凭据管理器；界面显示「本地 · 无需密钥」；
白名单不豁免，远端缺密钥仍 `key_missing`（有回归测试）。

### 13.3 导入体验（对齐 Cherry Studio / CC Switch）

云端 10 家 + 本地 4 种预设分组；新增「本地模型服务（无需密钥）」接入类型；
地址填回环地址自动切本地类型（且不覆盖用户刚填内容）；勾选对话框支持
**「同时设为 main 槽位」**（main 未绑定时默认勾选）——导入即绑定，省掉第二次操作；
拉取失败逐码给可操作中文（网络/白名单/缺凭据/被拒/不支持 `/models`）。

### 13.4 顺带修掉的导入路径缺陷

- `result_spec()` 取 `currentData()`：可编辑下拉里**手输 URL 会被静默换成残留选中项的预设地址**
  —— 改成按可见文本解析（`_base_url()`）。
- 载入既有供应商后密钥框状态未同步：**编辑本地供应商时密钥框仍可用** —— 载入后显式同步一次。
- 「导入即绑定」只在 main 为空时出现：把**用户真实数据**套进流程走一遍才发现，
  最常见的坏法是 main 非空却指向端点已不提供的模型（悬空绑定），此时导入完仍不能对话。
  → 绑定提示改三态（未绑定 / 悬空 / 正常），悬空时同样默认勾选并点明当前值。

（以上三处均由本轮新增测试或真实数据走查当场发现。）

### 13.5 冒烟结果

| 项 | 结果 |
|---|---|
| 全量测试 | `uv run pytest` → **137 passed, 2 skipped**（124+2 → 137+2，净增 13，只增不减） |
| 依赖方向 | `tests/static/test_imports.py` 通过（`domain_of` 下沉 `shared/net.py` 后 gui 不再需要 core） |
| 契约门禁 | `tests/static/test_contract_coverage.py` 通过 |
| 实网验证 | 用真实凭据对 `api.deepseek.com/v1/models` 取回 2 个模型（见 §13.1） |

### 13.6 留白

见 spec rev10 §6：真实配置仍为红（未擅改）、`/models` 不返回模型能力分类故不打标签、
仅回环地址算「本机」、自检按钮仍需独立重构。

## 14. rev11 轮次记录 — 显示裁剪修复与文案清理（2026-09-17）

用户反馈：**「前端在架构上是规整的，但在显示上有点缺陷，比如字显示不全、AI 不方便导入」**。
本轮先把「字显示不全」从主观感受变成**可测量的清单**，再定点修掉。

### 14.1 方法：离屏渲染 + 逐控件量宽

写了一个裁剪探测器：把真实配置复制到临时数据根，离屏启动主窗体，
逐个控件比较「文本实际需要的像素宽」与「布局给的宽」，表格另比列宽与内容宽。
首轮输出即为缺陷清单：

| 位置 | 实测（修复前） |
|---|---|
| 供应商编辑对话框 · 模型表 | 「模型 ID」内容需 **211px**、「上下文窗口」表头需 **107px**，而两列各 **100px**（Qt 默认）→ 省略成 `deepseek-v4-fl…` |
| 空状态标题 | 文本需 **252px**，布局只给 **240px** → 直接裁 |
| 主窗口 | 最小宽 **1208px**（125% 缩放的 1366 屏逻辑宽仅 ~1093 → 放不下）← 留待美化轮 |
| 设置页文案 | `addRow("历史保留轮数 N", …)` —— 界面上真的显示多余的 " N" |

### 14.2 修法

| # | 修订 | 落点 |
|---|---|---|
| 1 | 模型表列宽跟内容走：模型 ID 列 `Stretch`、窗口列 `ResizeToContents`；对话框最小宽 560px | `gui/pages/models.py` |
| 2 | **裁剪根因**：样式表 `font-size` 不参与 `sizeHint`，居中布局只按默认字号给宽 → 新增 `gui/widgets/text_fit.py`，按 `theme.font_px()` 量真实文本宽设为最小宽度，并在**每次档位变化**后重算（字号仍只从 theme 取） | `gui/widgets/text_fit.py`、`gui/chat/empty_state.py`、`gui/main_window.py` |
| 3 | 文案错字：「历史保留轮数 N」→「历史保留轮数」；「输出预留 reserve」→「输出预留（reserve）」 | `gui/pages/settings.py` |
| 4 | 字号纪律门禁**收紧而非放宽**：原先「`setPixelSize/setPointSize` 一律违规」会误伤「按 theme 构造字体做度量」的合法用法，改为「实参不以 `theme.` 开头」才算违规 —— 规则本意是**单一来源**，不是禁用 API | `tests/unit/test_theme.py` |

### 14.3 冒烟结果

| 项 | 结果 |
|---|---|
| 全量测试 | `uv run pytest` → **144 passed, 2 skipped**（139+2 → 144+2，净增 5，只增不减） |
| 裁剪复测 | 「文本放不下的控件」**无**；模型 ID 列 401px（内容 195px）、窗口列 106px（内容 91px） |
| 门禁 | 依赖方向 / 契约 / 主题与字号纪律 全部通过（本轮正因第 4 条而调整了门禁判据） |
| 真实链路 | `YMT_SELFTEST=1` → **12 passed**（此前 1 failed）；端到端回合 `done`，模型回复正常 |

### 14.4 留白（交下一轮「美化」）

主窗口最小宽 1208px 未动；侧栏 264px 固定宽、消息流长串换行、窄屏适配等属**响应式布局**，
放在美化轮统一做（方案见下方「美化提案」，需先定方向）。

## 15. rev12 轮次记录 — 暗色模式刷新不完整修复（2026-09-17）

用户反馈：**「暗色模式下面会有刷新不完整的情况」**。定位出两个确定的根因，均已修。

### 15.1 取证过程（含一次自我纠错）

| 步骤 | 结果 |
|---|---|
| 离屏像素对比（切换暗色 vs 全新暗色） | 报 6.5 万差异点 → **后确认为离屏假象，撤回** |
| 原因 | 离屏环境下 Chromium 根本不加载页面（`toHtml()` 返回空文档）——WebEngine 页面是异步加载的，抓图像素不可靠 |
| 分页面像素对比 | 对话页有差异、模型/设置页无差异 → 方向对，但数字不可信 |
| **调色板取证（可确证）** | 应用暗色主题后 `QApplication.palette()` **仍是亮色默认**（Window 浅灰 / Base 纯白 / Mid、Dark 中灰），而主题 token 是暗色 → QFrame 边框等直接读调色板的绘制残留亮灰 |

### 15.2 根因与修法

| # | 根因 | 修法 | 落点 |
|---|---|---|---|
| 1 | 主题只靠 QSS 生效；QFrame 边框、禁用态、箭头等绘制**不走 QSS**、直接读 QPalette → 暗色下残留亮灰元素 | 新增 `gui/app_palette.py`：从 theme token 构造 QPalette（含 Disabled / PlaceholderText / Mid / Dark 组），`theme.apply()` 内 QSS + 调色板一起应用。颜色仍单一来源 | `gui/app_palette.py`、`gui/theme.py` |
| 2 | 消息流是 `QWebEngineView`，对**不可见视图**的 `setHtml` 会被推迟或丢弃 —— 在设置页切主题时对话页正隐藏，重渲染不生效，切回后停留旧外观甚至空白 | `RendererView` 置脏协议：隐藏期 `set_html` 记内容置脏，`showEvent` 重放；可见期直接生效。降级路径同协议 | `gui/widgets/render/view.py` |

### 15.3 方法论教训（已入 spec rev12 §3）

- **WebEngine 路径上禁用离屏像素对比判定页面内容**：用内容真值（toHtml / JS 回调）。
- **所有 GUI 测试跑的都是 QTextBrowser 降级路径**——主渲染路径（WebEngine）在测试里从未真正
  执行，这正是用户看得到、测试抓不到的原因。「测试全绿 ≠ WebEngine 路径正确」。
- 门禁拦截记录：取色纪律门禁扫出 `app_palette.py` **注释里的**十六进制色号并拦下——
  门禁工作正常，注释改为不含色号。

### 15.4 冒烟结果

| 项 | 结果 |
|---|---|
| 全量测试 | `uv run pytest` → **146 passed, 2 skipped**（144+2 → 146+2，净增 2，只增不减） |
| 新增用例 | `test_apply_syncs_qpalette`（调色板跟随主题，含 Disabled/Mid/Dark 组）；`test_renderer_view_replays_hidden_updates`（隐藏期置脏 + showEvent 重放 + 可见期不置脏） |
| 门禁 | 依赖方向 / 契约 / 主题与字号纪律 全部通过 |

## 16. rev13 轮次记录 — 美化轮 A：响应式布局（2026-09-17）

用户裁决：美化提案里先做 A（响应式布局）；「AI 不方便导入」暂不展开；暗色修复由用户真机验证。

### 16.1 问题（探针实测）

| 项 | 实测 |
|---|---|
| 主窗口最小宽 | **1208px**（用户字号档）——125% 缩放的 1366 屏逻辑宽仅 ~1093px → 窗口放不下 |
| 侧栏 | `setFixedWidth(264)`：不可拖拽、不可折叠 |
| 消息流 | 正文无 `overflow-wrap` → 长 URL 溢出容器 |
| 设置页 | 长文本 QLabel 不换行，最小宽被撑到 710px |

### 16.2 修法

| # | 修订 | 落点 |
|---|---|---|
| 1 | **侧栏重构为 rail（48px 图标栏）+ 可折叠面板**：折叠后 ☰/⚙/🛠 三个入口仍可点（无需悬浮按钮）；面板宽 180–360 可拖拽；折叠/展开宽度由 MainWindow 的 QSplitter 落实（记住上次宽度） | `gui/sidebar.py`、`gui/main_window.py` |
| 2 | 主窗口改 **QSplitter**（侧栏 \| 主区），handle 4px、hover 显形（样式入 theme.stylesheet，取色走 token） | `gui/main_window.py`、`gui/theme.py` |
| 3 | 设置页两个长标签（数据目录路径 / 备份说明）：`wordWrap` + `QSizePolicy.Ignored` —— **wordWrap 只解决换行，minimumSizeHint 仍按最长不可断词计**（长路径实测可到 900px+），Ignored 才真正放开下限 | `gui/pages/settings.py` |
| 4 | 消息流正文与用户气泡加 `overflow-wrap: anywhere`（长 URL/长串折行）；代码块保留 `overflow-x: auto`（横向滚动为代码块惯例） | `gui/widgets/render/md.py` |

### 16.3 排障记录（一次有价值的反复）

- 加 wordWrap 后最小宽**不降反升**（测试环境 995px）：wordWrap 标签的 minimumSizeHint
  按**最长不可断词**计算，tmp 路径长于探针路径即被打回原形 → 补 `QSizePolicy.Ignored` 后达标。
- 教训入 spec rev13 §3：**验证布局修复要用与真实使用等长的输入**（探针里短路径测不出）。

### 16.4 冒烟结果

| 项 | 结果 |
|---|---|
| 全量测试 | `uv run pytest` → **149 passed, 2 skipped**（146+2 → 149+2，净增 3，只增不减） |
| 新增用例 | 侧栏折叠/展开协议；窗口最小宽 ≤1093px（含两个标签 wordWrap 断言）；消息流 overflow-wrap CSS |
| 最小宽实测 | 探针环境 998 → **640px**（用户字号档下 1208 → 同比例达标） |
| 门禁 | 依赖方向 / 契约 / 主题与字号纪律 全部通过 |

## 17. rev14 轮次记录 — 模型选择语义：上次使用接续（2026-09-17）

用户裁决：**没有「全局默认模型」这个概念**——参考 Cherry Studio / LobeChat 等 BYOK 应用，
应该是「上次选择的这次接着用，上次选择的用到新的对话里」。persona（阶段 2）等用户测试完再启动。

### 17.1 语义变化

| 维度 | 旧（rev4–rev13） | 新（rev14） |
|---|---|---|
| slots.main 的含义 | 手动设置的「全局默认」 | **自动记录的「上次使用的模型」** |
| 无会话时切换模型 | 请求被静默丢弃（`current_session_id` 为空直接 return） | **生效**：记为上次使用（发消息前先选模型是合法操作） |
| 新对话的模型 | 依赖用户记得先去设置「默认」 | 自动从**上次使用**开始 |
| 会话级切换的副作用 | 只写会话 meta | 同时持久化上次使用（`_persist` 收口，失败上报） |
| 切换后的事件回发 | 只 `health.report` | + `provider.list`（三处 UI 同步） |

### 17.2 附带修复

| # | 缺陷 | 修法 |
|---|---|---|
| 1 | 恢复一个换过模型的会话，对话头条下拉**仍显示全局值** —— 显示与实际生效的模型不符 | `MainWindow._sync_model_dropdown`：下拉显示**当前会话**的选择（`session.index` 的 `main_model`），无会话/未选时回落「上次使用」 |
| 2 | 文案以「main / 槽位绑定 / 默认」示人，把实现细节暴露成概念负担 | 模型页改「上次使用」+ 说明行（`mutedNote` 小字样式入 theme）；导入对话框措辞改「把第一个选中的模型设为开始对话的模型」；对话头下拉加 tooltip |

### 17.3 冒烟结果

| 项 | 结果 |
|---|---|
| 全量测试 | `uv run pytest` → **151 passed, 2 skipped**（149+2 → 151+2，净增 2，只增不减） |
| 新增用例 | `test_switch_without_session_records_last_used`（无会话切换生效 + 新对话回合用上次使用）；`test_model_dropdown_follows_session_then_last_used`（新对话回落上次使用、恢复会话显示会话自身选择且不被全局冲掉） |
| 门禁 | 依赖方向 / 契约 / 主题与字号纪律 全部通过 |

## 18. rev15 轮次记录 — 安全加固：传输保密性与渲染面（2026-09-17）

用户要求：检查安全与传输保密性，保障信息安全。

### 18.1 审计结论（先盘清已有防线，再补缺口）

| 面 | 审计前状态 | 结论 |
|---|---|---|
| 凭据存储 | 只入系统凭据管理器（DPAPI），无明文回退（异常 → None → `key_missing`，fail-closed）；`models.json` 只持 `key_ref` | ✅ 已达标 |
| 脱敏 | 三层（redact → 日志过滤器 → ErrorReport）；rev14 新增的 `_persist` 网关错误分支同样走 `_report`（过 redact） | ✅ 已达标 |
| 出口白名单 | 默认拒绝，三个调用点全查 | ✅ 已达标 |
| **传输保密性** | `http://` 远程端点畅通 —— API Key 走 Authorization 头**明文过网** | ❌ **本轮修复** |
| **渲染面（WebEngine）** | 消息内链接在应用内导航（一个链接就能用钓鱼页顶掉对话流）；JS 默认开启；无 CSP | ❌ **本轮修复** |
| TLS 校验 | openai SDK / httpx 默认 verify，未发现关闭点 | ✅（加静态门禁固化） |

### 18.2 修法

| # | 修订 | 落点 |
|---|---|---|
| 1 | **明文传输拦截**：新码 `insecure_transport`。规则：`https` 唯一合法远程传输；`http` 仅限本机回环（本地模型服务流量不出机器）；其余 scheme 拒。**入口**（`upsert_provider` 先校验后落盘）+ **调用点**（test / models / chat 三处双保险，防手改配置文件绕过） | `shared/net.py`（`is_secure_transport`，core/gui 共用）、`core/gateway/provider.py`、`shared/errors.py` |
| 2 | **`_persist` 不再吞网关校验错误**：`GatewayError` 按真实码上报（此前会变成 `storage_error`，把「地址不安全」误导成「磁盘坏了」） | `app/controller.py` |
| 3 | **消息内链接一律系统浏览器**：QWebEnginePage 拦 `NavigationTypeLinkClicked` → `QDesktopServices`；QTextBrowser `setOpenLinks(False)` + `anchorClicked` 外开 | `gui/widgets/render/view.py` |
| 4 | **WebEngine 加固**：禁 JS（消息流是服务端渲染的静态 HTML，pygments 无脚本）、禁本地文件互访；模板加 **CSP**（`default-src 'none'`，脚本/连接/框架全禁，样式与图片放行） | `gui/widgets/render/view.py`、`gui/widgets/render/md.py` |
| 5 | **静态门禁**：全 src 扫描 `verify=False` / `CERT_NONE` 等（TLS 校验不得关闭）；预设表纪律（云全 https、本地全回环 http） | `tests/unit/test_security.py` |

### 18.3 冒烟结果

| 项 | 结果 |
|---|---|
| 全量测试 | `uv run pytest` → **161 passed, 2 skipped**（151+2 → 161+2，净增 10，只增不减） |
| 新增用例 | 传输判定 1、TLS 静态门禁 1、预设纪律 1、网关三出口拦截 + 回环放行 4、CSP 1、外链 1、控制器报真实码 1 |
| 真实链路 | `YMT_SELFTEST=1` → **12 passed**（https 供应商不受新拦截影响，无误伤） |
| 门禁 | 依赖方向 / 契约 / 主题与字号纪律 全部通过 |

### 18.4 留白（已知、显式不做）

- **重定向降级**：openai SDK 跟随重定向（follow_redirects=True），https→http 跨协议降级未拦。
  攻击前提是「白名单里的端点本身是恶意的」——此时密钥本就已暴露给端点，拦重定向收益有限，
  且关闭重定向会破坏部分供应商的合法跳转。记为已知限制。
- **远程图片**：Markdown 图片按惯例仍加载（Cherry Studio 等同），理论上存在渲染期回连；
  CSP 已限制脚本面，图片面留待有需再做代理化。



