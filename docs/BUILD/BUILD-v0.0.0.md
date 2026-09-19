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
- 2026-09-17：**rev16 纵向裁切 · 爆闪 · 客观默认提示词**（见 §19）。
- 2026-09-17：**rev17 BYOK 体验优化**（见 §20）。
- 2026-09-18：**rev18 尺寸与可读性 · 测试进程退出修复**（见 §21）。
- 2026-09-18：**rev19 渲染通道重做：壳 + 局部更新 · 惰性创建**（见 §22）。
- 2026-09-18：**rev20 上下文预算自适应 · 每文件截断**（见 §23）。
- 2026-09-18：**rev22 复杂度巡检：结构简化（零行为变化）**（见 §24）。
- 2026-09-18：**rev23 阶段 2 第一片：Persona 全局配置 · 会话各自选择**（见 §25）。

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

## 19. rev16 轮次记录 — 纵向裁切 · 爆闪 · 客观默认提示词（2026-09-17）

用户截图实证 + 两项反馈：① 暗色空状态两行文案被**腰斩**；② 暗色切换**爆闪**；
③ 底层提示词不该自述身份/能力（「超级 Agent」定位说不清），persona 轮加 YMT 角色承载。

### 19.1 纵向裁切：rev11 只修了一半

| # | 内容 |
|---|---|
| 根因 | 「QSS 字号不参与 sizeHint」**纵向同样成立**：换行标签行数/行高按默认字号估，真实字号大一号即每行切半 |
| **系统性修法** | `theme.apply()` 把 **QApplication 字体设为 ui 档真实像素字号** → 全项目 sizeHint 从根上算对（默认字号标签全部自准，颜色字号仍单一来源） |
| 逐个适配 | title 档标签（比 ui 更大）仍需 `text_fit.line_height()` 设最小高：空状态标题/两行文案、两页 pageTitle，随 `_apply_appearance` 重算 |
| 排障记录 | 自测两次被自己抓漏：`line_height` 只返回不设置（页面标题 min 高为 0）→ 补 `setMinimumHeight`；空状态标题漏适配 → 测试当场抓住 |

### 19.2 爆闪：WebEngine 页面底色

`setHtml` = 页面重载，重载瞬间露出**默认白底** —— 这就是暗色切换的爆闪。
修法：`RendererView.set_html(html, bg)` 随每帧下发主题背景色（WebEngine `page().setBackgroundColor`，缓存判变），
`MessageList._render` 每次渲染带上；QTextBrowser 路径同步样式。

### 19.3 默认提示词客观化

| 项 | 内容 |
|---|---|
| 旧 | `你是言明通，一个运行在本地的个人超级 Agent。…` —— 应用替自己设计身份与能力叙事 |
| 新 | `使用简体中文回答。对不确定的内容如实说明；不虚构能力、工具或信息来源。` |
| 去向 | 身份叙事归 **persona 轮的 YMT 默认角色**（用户意向，可换可编辑）；UI 空状态 tagline 同步去掉自我定位 |

### 19.4 冒烟结果

全量 **165 passed, 2 skipped**（161+2 → 165+2，净增 4）；新增锚点：纵向适配 + 应用字体同步、
页面底色、消息流接线、提示词客观性；门禁全绿。

## 20. rev17 轮次记录 — BYOK 体验优化（2026-09-17）

用户邀请优化 BYOK。三项改进，无协议变化：

| # | 改进 | 说明 |
|---|---|---|
| 1 | 供应商卡片「**全部检测**」 | 一键逐个探测该供应商全部模型；点击先置「检测中…」再逐个发 `provider.test`（协议复用，零新增） |
| 2 | 密钥**存储说明** | 编辑对话框 API Key 行下注明：密钥存系统凭据管理器（OS 级加密）、界面不回显、留空 = 不变 —— 回答「密码存哪了」的真实困惑 |
| 3 | 模型导入**筛选框** | `ModelPickerDialog` 按关键字只**隐藏**不匹配项，勾选状态原样保留（筛选 ≠ 取消勾选） |

冒烟：全量 **168 passed, 2 skipped**（165+2 → 168+2，净增 3）；门禁全绿。

## 21. rev18 轮次记录 — 尺寸与可读性 · 测试进程退出修复（2026-09-18）

用户反馈两点 + 一处澄清：① rail 图标右侧加文字；② 启动窗口默认更大；
③ **「AI 不方便导入」的真意是「那个 dialog 太小了，看起来不舒服」**（与导入功能无关）。

### 21.1 改动

| # | 修订 | 落点 |
|---|---|---|
| 1 | rail 48 → **96px**，三个按钮改「图标 + 文字」（☰ 侧栏 / ⚙ 模型 / 🛠 设置）；按钮 QSS 不再自设字号（随应用字体，避免 rev16 同源的 sizeHint 脱节） | `gui/sidebar.py`、`gui/theme.py` |
| 2 | 启动尺寸按可用桌面 **86%** 自适应（上限 1440×920、下限 1024×720）；侧栏默认宽 264 → 312 | `gui/main_window.py` |
| 3 | `ProviderDialog` 最小 720×560 / 默认 860×640，模型表最小高 220；`ModelPickerDialog` 最小 560×460 / 默认 680×620 | `gui/pages/models.py` |
| 4 | 折叠/展开的宽度计算去掉硬编码（48/228/264/360 → `gui.sidebar` 常量） | `gui/main_window.py` |

### 21.2 顺带发现的旧隐患：测试全绿但进程 fast-fail

| 项 | 内容 |
|---|---|
| 现象 | `uv run pytest` 报 **170 passed**，但进程退出码 **0xC0000409 / 127**，stderr 有 `QThread: Destroyed while thread '' is still running` |
| 判定 | 在 HEAD（stash 掉本轮改动）复测同样崩溃 → **旧隐患，非本轮引入** |
| 根因 | `bootstrap()` 的 `CoreWorker` QThread 在个别用例里未停；Qt 对「退出时仍运行的 QThread」fast-fail。产品侧退出干净（`app/main.py` finally 有 stop + shutdown），故属**测试卫生** |
| 修法 | `tests/conftest.py` autouse fixture `_reap_core_workers`：跟踪并回收本用例创建的全部 CoreWorker |
| **教训** | **「pytest 全绿」≠「进程正常退出」**；此前多轮以 `; Write-Output ok` 收尾，退出码取自最后一条命令，把崩溃掩盖了十几轮。核对测试结论必须同时看退出码与 stderr |

### 21.3 冒烟结果

| 项 | 结果 |
|---|---|
| 全量测试 | `uv run pytest` → **170 passed, 2 skipped**（168+2 → 170+2，净增 2），**退出码 0**、stderr 无 QThread 警告 |
| 新增用例 | rail 按钮带文字 + 启动尺寸区间；两对话框尺寸下限 |
| 门禁 | 依赖方向 / 契约 / 主题与字号纪律 全部通过 |

## 22. rev19 轮次记录 — 渲染通道重做：壳 + 局部更新 · 惰性创建（2026-09-18）

用户反馈：刷新过程**黑屏**（亮色模式亦有）、页面显示刷新异常、**启动慢**；要求先检索经验再优化。

### 22.1 经验检索结论（社区 + Qt/PySide 官方文档）

| 现象 | 根因（有出处） |
|---|---|
| 黑屏 / 爆闪（亮暗两态） | `setHtml` 每次都是**整页重载**，旧页销毁 → 新渲染表面，切换瞬间露未初始化帧。社区标准解法：**停止整页 setHtml，改 runJavaScript 局部更新** |
| 长对话显示刷新异常 | `setHtml` 走 data: URL，**超 2MB 即 `loadFinished(success=false)`**（PySide 文档明示）——长会话静默渲染失败 |
| 流式期间上翻被拽回 | 整页重载重置滚动位置到顶部 |
| 启动慢 | WebEngine 首视图要拉起 GPU/渲染子进程，实测 **~800ms** 卡在启动路径 |

### 22.2 新渲染通道

```
首帧：setHtml(空壳文档)            ← 恒小（远低于 2MB 上限）
loadFinished → 应用排队帧
后续帧：runJavaScript(
            var nb = 在底部?; 
            #stream.innerHTML = <JSON 字符串字面量>;
            原本在底部才自动跟底)
```
| # | 落点 | 内容 |
|---|---|---|
| 1 | `md.py` | 拆 `_BASE_CSS`（结构性排版，随壳一次）+ `_inner()`（主题样式 + 消息体，即载荷）+ `assemble()` / `stub_doc()` / `messages_inner()` / `markdown_inner()`；`messages_to_html` 保留供降级路径 |
| 2 | `view.py` | `set_stream(inner, bg)`；壳/排队/`loadFinished` 状态机；`_update_script`（`json.dumps` 载荷，任意内容安全转义）；**惰性创建**视图；隐藏期置脏 + `showEvent` 重放（rev12 协议保留） |
| 3 | `message_list.py` | 改为下发片段（含主题样式），底色随帧 |

### 22.3 安全复核（关键）

`runJavaScript` 是**嵌入方 API，不受页面 CSP 约束** —— 真机探针实证：CSP `default-src 'none'` 下
局部更新照常（len1=5326、第二次更新生效）。故 `JavascriptEnabled` 由 False 改回 **True**（通道必需），
防线由 CSP 承担：内容侧脚本、连接、框架全禁不变；链接仍系统浏览器打开。

### 22.4 实证结果

| 项 | 数据 |
|---|---|
| 壳 → 排队帧 → 局部更新 | **PASS**（真机 WebEngine，非离屏） |
| CSP 是否拦更新通道 | **不拦**（实证） |
| 启动到可交互 | **2305ms → 1510ms（-35%）**；WebEngine 视图创建 ~800ms 移出启动路径 |
| 全量测试 | **172 passed, 2 skipped，退出码 0**（净增 2，零回归）；套件耗时 60s+ → 40s 级 |
| 门禁 | 依赖方向 / 契约 / 主题与字号纪律 全部通过 |

## 23. rev20 轮次记录 — 上下文预算自适应 · 每文件截断（2026-09-18）

用户裁决：**项目定位是超级 Agent，上下文预算要按 200K/300K/1M 级窗口的尺度来**；
上下文大小应可自定义且**不准那么小**；压缩（历史摘要化）后续再说。

### 23.1 改动

| # | 修订 | 说明 |
|---|---|---|
| 1 | **输出预留自适应**：`effective_reserve = min(max(配置值, min(W/8, 32K)), max(1024, W/4))` | 200K→25K、300K/1M→32K；8K 小窗被 1/4 上限压回 2K（输入侧保半窗）；存量旧默认自动放大，**无需迁移** |
| 2 | **挂载文件截断改每文件语义**：`effective_file_cap = max(配置值, min(W/4, 64K))` | 旧实现全部文件共享 8192 总额，挂 3 个文件各分 2.7K |
| 3 | **文件总额护栏**：文件不可淘汰，超出「输入预算 − system − env」时整块截断并带可见标记 | 窗口未知（10^9 哨兵）时护栏不触发，由每文件上限兜底 |
| 4 | 设置页：标签改「挂载文件截断（每文件）」+ 自适应说明小字 | 讲清「配置值是下限」 |
| 5 | **决策记录：不向供应商下发 max_tokens** | 推理模型的思考 token 也计入 max_tokens，硬顶会截断思考；reserve 保持空间预算语义，输出侧策略留待压缩轮一并设计 |

### 23.2 冒烟结果

| 项 | 结果 |
|---|---|
| 全量测试 | **177 passed, 2 skipped，退出码 0**（170+2 → 177+2，净增 7，只增不减） |
| 新增用例 | 自适应公式 ×2（200K/300K/1M/8K/未知）、每文件语义、总额护栏、文件超预算带标记、跳底脚本无条件分支、设置页提示 |
| 门禁 | 依赖方向 / 契约 / 主题与字号纪律 全部通过 |

## 24. rev22 轮次记录 — 复杂度巡检：结构简化（2026-09-18）

用户要求：检查有没有写得太复杂的地方，做同类优化。方法：AST 探针扫全 src
（超长文件 / 超长函数 / 疑似未引用定义），只挑**真实可简化且不改变行为**的点。

### 24.1 已简化

| # | 位置 | 改动 |
|---|---|---|
| 1 | `gui/pages/settings.py` | `__init__` 111 行 → 20 行组装 + 5 个分区构建器（`_build_appearance/_context/_whitelist/_data/_logging`），组装顺序即页面顺序 |
| 2 | `core/agent/loop.py` | `run_turn` 82 行 → 45 行：装配段抽成 `_prepare_context`（设置装载 → 自适应预算 → 组装 → 超窗拦截），超窗返回 None 由 run_turn 收口 |
| 3 | `gui/widgets/render/view.py` | `_ExternalPage` 由「每次建视图都在方法里定义类」提为 `_make_external_page()` 模块级工厂（WebEngine import 仍惰性） |

### 24.2 巡检后判定保留（不简化，避免为改而改）

| 项 | 理由 |
|---|---|
| `core/registry`（register/list_tools/execute） | 阶段 4 Skills/MCP 契约占位（探针显示「未引用」是因为实现轮未到） |
| `md.render_to_html / plain_to_html / ansi_to_html` | 阶段 5 shell 输出渲染管线的契约面（docs 09 §2） |
| `theme.stylesheet`（97 行 f-string） | 声明式 QSS，拆分不会更简单 |
| `empty_state.cta_text` / `md.messages_to_html` | 探针误报 —— 分别被测试与降级路径使用 |
| `context.build` / `provider._stream_once` | 近轮刚按 spec 重构过，再拆属于为拆而拆 |

### 24.3 冒烟结果

全量 **177 passed, 2 skipped，退出码 0**（零行为变化、零用例增减）；门禁全绿。

## 25. rev23 轮次记录 — 阶段 2 第一片：Persona 全局配置 · 会话各自选择（2026-09-18）

用户裁决：Persona = 全局配置形同模型；**多角色库**；**单对话选择、单对话不一致**（对齐
Coding agents 平台）；YMT 默认角色由 AI 起草；工作区设计暂缓。规划先行、前后端联做。

### 25.1 交付

| # | 交付 | 落点 |
|---|---|---|
| 1 | `PersonaStore`：角色库（list/save/delete/get/resolve_content/默认角色）；YMT 预置 `prs_ymt` 缺席即创建、可编辑不可删；任何解析失败回退 YMT | `core/agent/persona.py`（新） |
| 2 | 协议：`persona.list/save/delete/set_default/switch` 5 请求 + 1 事件；`SessionMeta.persona_id`；`NewSession.persona_id` 启用 | `shared/envelope.py` |
| 3 | 会话侧：`ISessionStore.set_persona / get_meta`（契约门禁当场抓到 `get_meta` 漏声明并拦截） | `core/agent/session.py` |
| 4 | 装配：system 首段 = 会话角色的 prompt.md，失败回退 YMT；rev16 客观化结论不变（身份叙事在可编辑的预置角色里） | `core/agent/loop.py` |
| 5 | GUI：**角色配置页**（rail「🎭 角色」：卡片 + 设为默认/编辑/删除 + 编辑对话框）；对话头条**角色下拉**（与模型并列，★=默认） | `gui/pages/personas.py`（新）、`gui/chat/header.py`、`gui/chat/view.py`、`gui/sidebar.py`、`gui/main_window.py` |
| 6 | **模型语义修订（rev14→rev23）**：会话内 `model.switch` 只写会话 meta，不再登记「上次使用」；对齐 Coding agents 平台 | `app/controller.py` |

### 25.2 冒烟结果

| 项 | 结果 |
|---|---|
| 全量测试 | **186 passed, 2 skipped，退出码 0**（177+2 → 186+2，净增 9，只增不减） |
| 新增用例 | PersonaStore ×5（预置/往返/回退/删除/重开）、端到端 ×2（保存→切换→system 段生效→删除回退；会话内切换不漂移全局默认）、GUI 接线 ×1、下拉跟随修订 ×1 |
| 门禁 | 依赖方向 / 契约（抓到 `ISessionStore.get_meta` 漏声明并修复）/ 主题与字号纪律 全部通过 |

## 26. rev24 轮次记录 — 会话级上下文策略 · 右栏会话详情 · 模型参数下发（2026-09-18）

用户裁决（原文要点）：右键会话可处理操作（删除、**详情**）；**取消全局上下文策略**，
改为**逐对话（任务）设计**，参考 Cowork 类项目；明确**反对「保留最近多少轮」**
（「本质是对话应用，不想丢对话」）。详情用**右侧边栏**而非 Dialog；消息框加宽。
另：用户问「为何每轮 tokens 增长」→ 说明用量是**单次（prompt = 全量历史重发，completion = 本轮）**
而非累计，右栏一并呈现单次与累计。

### 26.1 交付

| # | 交付 | 落点 |
|---|---|---|
| 1 | **取消全局上下文设置**：`SettingsConfig.context` 移除，设置页「上下文策略」分区删除；存量 `settings.json` 的 `context` 静默忽略不迁移 | `shared/schema.py`、`gui/pages/settings.py` |
| 2 | **历史不设轮数上限**：删 `history_turns` / `_limit_turns`，仅按 token 预算从最旧淘汰，永不触及最后一条用户消息 | `core/agent/context.py` |
| 3 | **逐对话有效窗口**：`SessionMeta.max_context`（None = 跟随模型窗口，即「不设默认上限」）；`effective = max_context or model_ctx_window` | `shared/envelope.py`、`core/agent/loop.py`、`core/agent/session.py` |
| 4 | 契约：`session.detail` → `SessionDetailResult`（轮次/消息数/`data_bytes`/累计 tokens/最近 `ctx.usage`/有效窗口）；`session.update`（名称/作用/最大上下文/模型参数） | `shared/envelope.py`、`app/controller.py` |
| 5 | **模型参数下发**：`SessionParams` → `stream_chat(params=...)` → `_param_options`，**只发显式启用项**；rev20「不把 reserve 映射 max_tokens」不变 | `core/gateway/provider.py` |
| 6 | **右栏 `SessionPanel`**（可折叠，非 Dialog）：状态 / 用量分段（单次 vs 累计，讲清全量重发）/ 策略与参数编辑 / 问题列表跳转；双入口（会话右键「详情」+ 头条「详情」开关） | `gui/chat/session_panel.py`（新）、`gui/main_window.py`、`gui/sidebar.py`、`gui/chat/header.py`、`gui/chat/view.py` |
| 7 | **补 `ctx.usage` 零消费缺口**（阶段 1 遗留）：用量事件终于上屏 | `gui/main_window.py` |
| 8 | 输入体验：**Ctrl+Enter 发送 / Enter 换行** + 独立发送按钮；消息气泡 `78% → 90%`；消息块带 `id` 锚点供问题跳转 | `gui/chat/input_bar.py`、`gui/widgets/render/{md,view}.py`、`gui/chat/message_list.py` |

### 26.2 冒烟结果

| 项 | 结果 |
|---|---|
| 全量测试 | **190 passed, 2 skipped，退出码 0**（186+2 → 190+2，净增 4，只增不减） |
| 新增/改造用例 | token 预算淘汰（替代固定轮数，含「全部保留」与「丢最旧留当前问」）×1、会话 `update/data_bytes` 往返 ×1、`_param_options` ×1、参数下发到网关 ×1、`max_context` 覆盖模型窗口 ×1；改造 `settings.context` 既有用例、设置页标签用例、回合完成事件顺序断言（详情事件为末条） |
| 门禁 | 依赖方向（gui 仅见 shared + bus）/ 契约（`ISessionStore.update / data_bytes` 当场声明）/ 主题与字号纪律 全部通过 |

### 26.3 与既有决策的关系

- rev20（自适应 reserve/file_truncate）**保留**，但其配置来源由「全局 settings.context」改为模块默认常量
  `_DEFAULT_RESERVE=4096` / `_DEFAULT_FILE=8192`（作为下限），逐对话上限由 `max_context` 决定。
- rev8 §2 淘汰不变量（token 口径、不丢当前提问、不双计 reserve）不变。
- rev14 的「上次使用接续」在 rev23 已修订为「全局配置 + 会话各自选择」，rev24 只承接（详情面板显示会话模型）。


## 27. rev25 轮次记录 — 折叠思考块 · 思考能力探测 · 平均 TPS（2026-09-18）

用户裁决（原文要点）：交互界面在模型**能思考**时出现**可折叠思考部分**；开关**由程序自动判定**
（「开或者关并非由我来觉得」）；在**调用时**实际尝试 reasoning 参数探测，需**预告**会消耗一点
token（单次 **50–100 token** 内）、**不每次都测**；**允许人工配置**、出错**允许反馈纠正**；
**只探测 `reasoning_effort`**；探测结果落盘；完成后**自动折叠且允许手动展开**；TPS 取**生成阶段**。
另：输入区默认 **2 行**、最多 **10 行**、发送后回 2 行（发送按钮样式不变）。
用户同时澄清：**btcm 本阶段用不上**，不作为上下文系统。

### 27.1 交付

| # | 交付 | 落点 |
|---|---|---|
| 1 | **思考采集**：`_reasoning_text` 依次读 `reasoning_content` → `reasoning`，经 `on_reasoning` 与正文分流 | `core/gateway/provider.py` |
| 2 | **能力判定**：`reasoning` 偏好（auto/on/off）优先；`reasoning_pending` 仅 `auto+unknown`；`probe_reasoning` 带 `reasoning_effort="low"`、`max_tokens=64` 探测一次，被拒则去参重探并记 `reasoning_param_ok=False` | `core/gateway/provider.py` |
| 3 | **探测缓存落盘**：`reasoning_detected` / `reasoning_param_ok` 写入 `models.json`；`upsert_provider` 按 id 合并保留缓存、采纳新偏好 | `shared/schema.py`、`core/gateway/provider.py` |
| 4 | **参数门控**：仅 `reasoning_param_ok` 为真才下发 `reasoning_effort`；失败一律 `unknown`（不写缓存、不阻断对话） | `core/gateway/provider.py` |
| 5 | **回合接线**：`_probe_reasoning` 在装配后/调用前发瞬态 `turn.status(probing, note)`（**不落盘**）；思考增量独立累积；`final` 携带 reasoning | `core/agent/loop.py`、`core/agent/session.py` |
| 6 | **计时**：`Usage` 增 `elapsed_ms` / `first_token_ms`（流内计时，落盘回放一致） | `shared/envelope.py`、`core/gateway/provider.py` |
| 7 | **折叠思考块**：原生 `<details class="think">`（CSP 免脚本），流式 `open`、完成自动折叠；思考按纯文本转义 | `gui/widgets/render/md.py`、`gui/chat/message_list.py` |
| 8 | **平均 TPS**：「本次 N tokens · X.X tokens/s · Y.Ys」，TPS = completion ÷（末期−首 token），旧数据不编速度 | `gui/chat/message_list.py`、`gui/chat/view.py` |
| 9 | **模型页人工覆盖**：模型表增「思考」列（自动检测/强制开启/强制关闭）；卡片 muted 文案显示探测状态供纠偏 | `gui/pages/models.py` |
| 10 | **输入区 2–10 行**自适应，发送清空回 2 行；探测预告经瞬态提示条显示 | `gui/chat/input_bar.py`、`gui/chat/view.py`、`gui/main_window.py` |

### 27.2 冒烟结果

| 项 | 结果 |
|---|---|
| 全量测试 | **202 passed, 2 skipped，退出码 0**（190+2 → 202+2，净增 12，只增不减） |
| 新增用例 | 思考采集与生成阶段计时、探测 yes / no / 参数被拒回退 / 失败不写缓存、`upsert` 保留探测缓存、循环探测预告与 `final.reasoning` 落盘、计时透传、TPS 文案、输入区 2–10 行、思考块折叠渲染 |
| 门禁 | 依赖方向 / 契约（`IModelGateway` 新增 `reasoning_pending/probe_reasoning`，`MockGateway` 全实现）/ 主题与字号纪律 全部通过 |

### 27.3 与既有决策的关系

- 探测**一次即缓存**、不每回合重测；人工覆盖可随时纠正（模型页「思考」列），符合用户「允许人工配置 + 反馈纠正」。
- 思考内容**不进上下文**（`_history_messages` 只取正文），不改变 token 预算与淘汰语义（rev8 §2 不变）。
- rev20「不把 reserve 映射 max_tokens」不受影响；`reasoning_effort` 为独立参数通道。
- 本轮**不涉及 btcm**（用户澄清其本阶段用不上，且非上下文系统）。


## 28. rev26 轮次记录 — 历史摘要化 / 压缩 · 输出侧策略（2026-09-19）

用户裁决（原文要点）：**允许**历史摘要化，但**长度不到「特定压缩大小」时不需要马上压缩**；
压缩本质是**概括**——「在一个文件里面概括前面做了什么，有格式有要求地概括」，且须采用**新的提示词**；
**聊天记录与 `events.jsonl` 都要保留**；预告 token 成本**保留**；更深刻的部分留待后续轮次。
关键裁决：**「超过 90%」应是用户指定的一个数值**（不得写死）。
另指示：登记「**命令系统**」为后续需做项（当前交互全为按键/GUI 系统）。

### 28.1 交付

| # | 交付 | 落点 |
|---|---|---|
| 1 | **契约**：`SummaryConfig`（threshold/auto/keep_ratio/model_slot）、`SessionSummary`、`SessionMeta.summary_threshold`、请求 `session.summarize`、事件 `session.summary.result`、`SessionDetailResult` 摘要字段、`ContextUsage.summary` 段、`TurnStatus.summarizing` | `shared/schema.py`、`shared/envelope.py` |
| 2 | **摘要提示词**：`config/summary_prompt.md` 首次使用时以内置默认落盘，之后以文件为准，**不与角色提示词叠加** | `core/agent/summarize.py` |
| 3 | **摘要文件**：`sessions/<id>/summary.md`（五节模板，人工可编辑、权威）+ `summary.json`（revision/covered_seq/…）；`revision` 只增 | `core/agent/summarize.py`、`core/agent/session.py` |
| 4 | **规划算法**：`plan_summary` 只取 `seq > covered_seq`；尾部按 token 预算保留（`window//keep_ratio`，下限 1024）；上一版摘要并入概括（渐进摘要） | `core/agent/summarize.py` |
| 5 | **阈值**：会话级覆盖优先、否则全局默认，钳制 50–99；越界 → `invalid_request` | `core/agent/summarize.py`、`app/controller.py` |
| 6 | **装配**：顺序 system·memory → summary → files → env → history；history 只取未覆盖事件、不设轮数上限 | `core/agent/context.py`、`core/agent/loop.py` |
| 7 | **安全**：摘要落盘前过 `redact()`；失败或无内容 **fail-closed 不写** | `core/agent/summarize.py`、`core/agent/loop.py` |
| 8 | **右栏「历史压缩」段**：阈值勾选 + 50–99 输入、「压缩历史」「打开摘要文件」按钮、状态与失败提示 | `gui/chat/session_panel.py`、`gui/main_window.py` |
| 9 | **输出侧策略定稿**：沿用 rev20 —— 不下发 `max_tokens`，`reserve` 保持空间预算；仅保留逐会话显式 `max_tokens` 通道 | `shared/envelope.py`、`core/gateway/provider.py` |

### 28.2 冒烟结果

| 项 | 结果 |
|---|---|
| 全量测试 | **209 passed, 2 skipped，退出码 0**（202+2 → 209+2，净增 7，只增不减） |
| 新增用例 | 尾部保留按 token 预算、摘要写盘并替换历史、无可压缩内容报错、上游失败不落盘、阈值用户值与钳制、控制器分派与详情回推、右栏压缩控件与失败提示 |
| 门禁 | 依赖方向 / 契约（`IAgentLoop.summarize` 已声明，`MockGateway` 全实现）/ 主题与字号纪律 全部通过 |

### 28.3 与既有决策的关系

- `events.jsonl` 与聊天记录**只增不改**；摘要是**独立文件**，历史仅按 `covered_seq` 过滤，符合用户「记录要保留」。
- rev8 §2 淘汰不变量不变（token 口径、不丢当前提问、不双计 reserve）；压缩只改变「更早历史」的表达形式。
- 阈值为**用户指定值**（会话级优先），自动开关**默认关闭**，低于阈值不压缩，符合 m0339 裁决。
- rev20「不把 reserve 映射 max_tokens」**保持不变**；输出侧本轮无新增全局设置。
- 本轮**不涉及 btcm**。


## 29. rev27 轮次记录 — 小缺陷修复 / 加固（2026-09-19）

阶段 2 连续交付（rev24–rev26）后的**收口巡检**：用户要求「先看看有没有现阶段可直接修掉的小问题」。
本轮**不改协议**（无新增请求/事件类型），只修正与文档不符或违背既定裁决之处。

### 29.1 修复清单

| # | 问题 | 修复 | 落点 |
|---|---|---|---|
| 1 | 思考探测未得结论（`unknown`）时不写缓存 → 每回合重复探测与重复预告，违背「不要每次都测试」 | 进程内 `_probe_attempted`：无论成败本进程不再重探；得结论仍落盘，未得结论不臆断 | `core/gateway/provider.py` |
| 2 | 压缩失败信息直接回显 `str(exc)`，未接入脱敏层 | `_fail_summary` 先过 `redact()` 再回显 | `core/agent/loop.py` |
| 3 | 摘要提示词首次落盘非原子写 | 改走 `atomic_write_text` | `core/agent/summarize.py` |
| 4 | `session_not_found` 字面量散落 | 统一 `ErrorCode.SESSION_NOT_FOUND.value` | `app/controller.py` |
| 5 | 「打开摘要文件」在无文件时静默调用系统打开 | 缺失时回可读提示 | `gui/main_window.py` |

### 29.2 冒烟结果

| 项 | 结果 |
|---|---|
| 全量测试 | **211 passed, 2 skipped，退出码 0**（209+2 → 211+2，净增 2，只增不减） |
| 新增用例 | 探测失败后同进程不再重探（新进程仍再试一次）、压缩失败信息过脱敏 |
| 门禁 | 依赖方向（`agent` → `store.atomic` 属既有允许方向）/ 契约 / 主题与字号纪律 全部通过 |

### 29.3 未决

- `SummaryConfig.auto` 的**自动触发**仍属待设计（当前默认关闭）：阈值目前仅作展示与手动参考，
  未在占用达阈值时自动压缩。留待后续轮次给方案裁决。


## 30. rev28 轮次记录 — 思考块默认折叠（2026-09-19）

用户裁决（m0556）：「思考过程默认折叠而不张开」——**取代 rev25「流式展开、完成折叠」**。

### 30.1 变更

| # | 变更 | 落点 |
|---|---|---|
| 1 | 思考块 `<details class="think">` 一律不带 `open`（流式期间也折叠，手动展开） | `gui/widgets/render/md.py` |
| 2 | 移除仅供「流式展开」用的 `streaming` 标志与 `_block_assistant` 形参 | `gui/chat/message_list.py`、`gui/widgets/render/md.py` |

### 30.2 冒烟结果

| 项 | 结果 |
|---|---|
| 全量测试 | **211 passed, 2 skipped，退出码 0**（用例数不变，改写渲染断言为「两种状态都折叠」） |
| 门禁 | 依赖方向 / 契约 / 主题与字号纪律 全部通过 |



