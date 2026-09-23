"""模型配置页：BYOK 多供应商 + 自定义端点 + 测试连接 + 槽位绑定（rev2 §1.4）。

模型导入（rev9 §2）：卡片上的「获取模型列表」从端点取候选，勾选即登记 ——
用户不必凭空知道模型 ID（手填错 ID 是端到端报错的首要来源）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from shared.envelope import ModelSpec, ProviderSpec
from shared.errors import ERROR_TEXT
from shared.ids import PRV, new_id
from shared.net import is_local_url

from gui import theme
from gui.widgets import card, section_label, text_fit

#: 云端预设（URL 即 OpenAI 兼容端点；Cherry Studio 式「选预设 → 填 Key → 取模型」）
CLOUD_PRESETS = {
    "DeepSeek": "https://api.deepseek.com/v1",
    "Moonshot Kimi": "https://api.moonshot.cn/v1",
    "智谱 GLM": "https://open.bigmodel.cn/api/paas/v4",
    "通义千问 DashScope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "硅基流动 SiliconFlow": "https://api.siliconflow.cn/v1",
    "火山方舟 Ark": "https://ark.cn-beijing.volces.com/api/v3",
    "腾讯混元": "https://api.hunyuan.cloud.tencent.com/v1",
    "百度千帆": "https://qianfan.baidubce.com/v2",
    "MiniMax": "https://api.minimax.chat/v1",
    "OpenAI": "https://api.openai.com/v1",
}

#: 本地模型服务预设：**无需密钥**（spec rev10 §1）
LOCAL_PRESETS = {
    "Ollama（本地）": "http://127.0.0.1:11434/v1",
    "LM Studio（本地）": "http://127.0.0.1:1234/v1",
    "vLLM / llama.cpp（本地）": "http://127.0.0.1:8000/v1",
    "Xinference（本地）": "http://127.0.0.1:9997/v1",
}

PRESETS = {**CLOUD_PRESETS, **LOCAL_PRESETS}

#: 拉取模型列表失败时的**可操作**中文指引（码 → 文案）。
#: 只报「网络或连接错误」等于把排查成本丢回给用户。
FETCH_ERROR_HINT = {
    "network_error": "无法连接该端点：请检查 base_url 与网络（该域名在本机可能不可达）",
    "whitelist_blocked": "该域名不在网络白名单内：保存供应商后会自动加入",
    "key_missing": "还没有可用凭据：请填入 API Key；本地服务请把接入类型选为「本地模型服务」",
    "auth_error": "凭据被拒绝：确认这把 API Key 属于该端点",
    "provider_not_found": "供应商不存在：刷新后重试",
    "protocol_error": "该端点未提供模型列表（不支持 /models），请手动填写模型 ID",
    "model_not_found": "该端点拒绝了请求，请手动填写模型 ID",
}


class ProviderDialog(QDialog):
    def __init__(self, provider: ProviderSpec | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("供应商")
        self._existing = provider
        self._provider_id = provider.id if provider else new_id(PRV)
        self._loading = True

        self._name = QLineEdit(provider.name if provider else "")
        # 接入类型：预设供应商 / 自定义模型 API / 本地模型服务（无需密钥）
        self._kind = QComboBox()
        self._kind.addItem("预设供应商", "preset")
        self._kind.addItem("自定义模型 API", "custom")
        self._kind.addItem("本地模型服务（无需密钥）", "local")
        self._kind.currentIndexChanged.connect(self._on_kind_changed)
        self._auto_switching = False

        self._base = QComboBox()
        self._base.setEditable(True)
        for label, url in CLOUD_PRESETS.items():
            self._base.addItem(label, url)
        self._base.insertSeparator(self._base.count())
        for label, url in LOCAL_PRESETS.items():
            self._base.addItem(label, url)
        self._base.currentTextChanged.connect(self._on_base_changed)
        self._key = QLineEdit()
        self._key.setEchoMode(QLineEdit.Password)
        self._key.setPlaceholderText("留空表示保持不变" if provider else "API Key")

        if provider:
            self._base.setCurrentText(provider.base_url)
            if provider.local:
                self._kind.setCurrentIndex(2)
            else:
                is_preset = provider.base_url in PRESETS.values()
                self._kind.setCurrentIndex(0 if is_preset else 1)

        self._models = QTableWidget(0, 3)
        self._models.setHorizontalHeaderLabels(["模型 ID", "上下文窗口", "思考"])
        # 列宽必须跟内容走：默认每列 100px 会把模型 ID 与表头都省略成「deepseek-v4-fl…」
        # （实测「模型 ID」内容需 211px、「上下文窗口」表头需 107px）。
        header = self._models.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self._models.setMinimumWidth(420)
        self._models.setMinimumHeight(220)  # rev18：表格太扁看着憋屈，给足高度
        if provider:
            for model in provider.models:
                self._add_row(model.id, model.ctx_window, model.reasoning)
        add_row = QPushButton("添加模型")
        add_row.clicked.connect(lambda: self._add_row("", 0))
        del_row = QPushButton("删除选中模型")
        del_row.clicked.connect(self._remove_selected)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        form = QFormLayout()
        form.addRow("名称", self._name)
        form.addRow("接入类型", self._kind)
        form.addRow("base_url", self._base)
        form.addRow("API Key", self._key)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        # 密钥可解释性（rev17）：回答「密码存哪了、为什么看不到」——
        # 密钥只入本地加密库（设备绑定），界面永不回显
        self._key_note = QLabel(
            "密钥保存到本地加密库（设备绑定，界面不回显）；留空表示保持不变。"
        )
        self._key_note.setObjectName("mutedNote")
        self._key_note.setWordWrap(True)
        layout.addWidget(self._key_note)
        layout.addWidget(self._models)
        row = QHBoxLayout()
        row.addWidget(add_row)
        row.addWidget(del_row)
        layout.addLayout(row)
        layout.addWidget(buttons)

        # rev18：对话框此前最小 560 宽、高度随内容 → 看着局促，也不便编辑长 base_url 与模型表
        self.setMinimumSize(720, 560)
        self.resize(860, 640)
        self._loading = False
        # 载入期 _on_kind_changed 被守卫挡住，故显式同步一次控件状态 ——
        # 否则「编辑一个本地供应商」时密钥框仍是可用的，与类型不符。
        self._apply_kind(clear_fields=False)

    def _on_kind_changed(self, index: int) -> None:
        if self._loading:
            return
        self._apply_kind(clear_fields=not self._auto_switching)

    def _apply_kind(self, *, clear_fields: bool) -> None:
        """把「接入类型」落到控件状态上（密钥框可用性 / 占位提示）。"""
        data = self._kind.currentData()
        if data == "custom":
            if clear_fields:
                self._base.clearEditText()
            self._base.setPlaceholderText("https://host/v1（OpenAI 兼容端点）")
            self._key.setDisabled(False)
            self._key.setPlaceholderText("留空表示保持不变" if self._existing else "API Key")
        elif data == "local":
            if clear_fields:
                self._base.clearEditText()
                self._key.clear()
            self._base.setPlaceholderText("http://127.0.0.1:11434/v1（Ollama / LM Studio / vLLM）")
            self._key.setDisabled(True)
            self._key.setPlaceholderText("本地服务无需密钥")
        else:
            self._base.setPlaceholderText("选择或输入 OpenAI 兼容端点")
            self._key.setDisabled(False)
            self._key.setPlaceholderText("留空表示保持不变" if self._existing else "API Key")

    def _base_url(self) -> str:
        """解析 base_url：**以用户可见的文本为准**。

        可编辑下拉里 `currentData()` 会返回残留选中项的数据 —— 此前直接取它，
        导致「手输的 URL 被静默换成下拉里选中的预设地址」（spec rev10 §3）。
        规则：文本命中某个预设项（标签）时取该项 URL，否则一律用文本本身。
        """
        text = self._base.currentText().strip()
        if not text:
            return ""
        for index in range(self._base.count()):
            if text == self._base.itemText(index):
                return str(self._base.itemData(index) or text)
        return text

    def _on_base_changed(self, text: str) -> None:
        """地址填成本机就自动切「本地模型服务」，填回远端则切回自定义。

        本地服务（Ollama / LM Studio / vLLM）不需要鉴权，让用户自己去理解
        「为什么本地也要填 Key」是多余的心智负担。
        """
        if self._loading or self._auto_switching:
            return
        if is_local_url(text):
            if self._kind.currentData() != "local":
                self._switch_kind(2)
        elif text.strip() and self._kind.currentData() == "local":
            self._switch_kind(1)

    def _switch_kind(self, index: int) -> None:
        self._auto_switching = True
        try:
            self._kind.setCurrentIndex(index)
        finally:
            self._auto_switching = False

    def _add_row(self, model_id: str, ctx_window: int, reasoning: str = "auto") -> None:
        row = self._models.rowCount()
        self._models.insertRow(row)
        self._models.setItem(row, 0, QTableWidgetItem(model_id))
        self._models.setItem(row, 1, QTableWidgetItem(str(ctx_window)))
        # rev25：思考 = 自动（首次调用探测）/ 强制开 / 强制关；人工覆盖用于纠正探测误判
        combo = QComboBox()
        combo.addItem("自动检测", "auto")
        combo.addItem("强制开启", "on")
        combo.addItem("强制关闭", "off")
        index = combo.findData(reasoning)
        combo.setCurrentIndex(index if index >= 0 else 0)
        self._models.setCellWidget(row, 2, combo)

    def _remove_selected(self) -> None:
        for index in sorted({i.row() for i in self._models.selectedIndexes()}, reverse=True):
            self._models.removeRow(index)

    def result_spec(self) -> ProviderSpec:
        models: list[ModelSpec] = []
        for row in range(self._models.rowCount()):
            id_item = self._models.item(row, 0)
            ctx_item = self._models.item(row, 1)
            model_id = (id_item.text().strip() if id_item else "")
            if not model_id:
                continue
            try:
                ctx = int(ctx_item.text()) if ctx_item else 0
            except ValueError:
                ctx = 0
            combo = self._models.cellWidget(row, 2)
            reasoning = combo.currentData() if isinstance(combo, QComboBox) else "auto"
            models.append(ModelSpec(id=model_id, ctx_window=ctx, reasoning=reasoning or "auto"))
        return ProviderSpec(
            id=self._provider_id,
            name=self._name.text().strip() or "未命名",
            base_url=self._base_url(),
            models=models,
            local=self.is_local(),
        )

    def is_local(self) -> bool:
        """当前接入类型是否为「本地模型服务」（免密钥）。"""
        return self._kind.currentData() == "local"

    def api_key(self) -> str | None:
        if self.is_local():
            return None  # 本地服务不需要密钥（core 侧亦按 local 放行）
        text = self._key.text().strip()
        return text or None


class ModelPickerDialog(QDialog):
    """从端点自报的候选中勾选要登记的模型（spec rev9 §2）。

    已登记的模型默认勾选；上下文窗口未知的按 0 记（0 = 未知，可再编辑）。
    """

    def __init__(
        self,
        candidates: list[str],
        known: dict[str, int] | None = None,
        bind_hint: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("选择要登记的模型")
        self._known = dict(known or {})

        # 筛选框（rev17）：端点动辄自报几十个模型，滚动找模型不可用
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("输入关键字筛选模型…")
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._apply_filter)

        self._list = QListWidget()
        for model_id in candidates:
            item = QListWidgetItem(model_id)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if model_id in self._known else Qt.Unchecked)
            self._list.addItem(item)

        hint = QLabel("勾选后点「确定」即写入该供应商的模型表；上下文窗口未知的按 0 记，可稍后编辑。")
        hint.setWordWrap(True)

        # 上次使用为空、或指着的模型不在候选里时，顺手设为第一个选中项 ——
        # 否则「导入了模型但还不能对话」，用户还得再去找一次模型下拉（rev14 语义）。
        self._bind_main: QCheckBox | None = None
        if bind_hint:
            self._bind_main = QCheckBox(bind_hint)
            self._bind_main.setChecked(True)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"端点自报 {len(candidates)} 个模型："))
        layout.addWidget(self._filter)
        layout.addWidget(self._list)
        layout.addWidget(hint)
        if self._bind_main is not None:
            layout.addWidget(self._bind_main)
        layout.addWidget(buttons)
        # rev18：勾选对话框同样给足尺寸（模型多时要能一眼看一屏）
        self.setMinimumSize(560, 460)
        self.resize(680, 620)

    def _apply_filter(self, text: str) -> None:
        """只隐藏不匹配项，勾选状态原样保留（筛选 ≠ 取消勾选）。"""
        text = text.strip().lower()
        for row in range(self._list.count()):
            item = self._list.item(row)
            item.setHidden(bool(text) and text not in item.text().lower())

    def should_bind_main(self) -> bool:
        return bool(self._bind_main is not None and self._bind_main.isChecked())

    def selected_models(self) -> list[ModelSpec]:
        out: list[ModelSpec] = []
        for row in range(self._list.count()):
            item = self._list.item(row)
            if item.checkState() == Qt.Checked:
                out.append(ModelSpec(id=item.text(), ctx_window=self._known.get(item.text(), 0)))
        return out


class ModelsPage(QWidget):
    upsert_requested = Signal(object, object)  # ProviderSpec, api_key | None
    delete_requested = Signal(str)
    test_requested = Signal(str, str)  # provider_id, model_id
    models_requested = Signal(str)  # provider_id：拉取端点模型列表（rev9 §2）
    slot_requested = Signal(str, str)  # slot, model_id ("" = 未绑定)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._providers: list[ProviderSpec] = []
        self._slots: dict = {}
        self._test_labels: dict[tuple[str, str], QLabel] = {}
        self._fetch_labels: dict[str, QLabel] = {}
        self._badges: dict[str, QLabel] = {}

        self._title = QLabel("模型配置")
        self._title.setObjectName("pageTitle")  # 字号与字重由 theme.stylesheet 提供
        add = QPushButton("添加供应商")
        add.setObjectName("primaryButton")
        add.clicked.connect(self._on_add)
        head = QHBoxLayout()
        head.addWidget(self._title)
        head.addStretch(1)
        head.addWidget(add)

        self._list = QVBoxLayout()
        self._list.setAlignment(Qt.AlignTop)
        self._list.setSpacing(8)
        holder = QWidget()
        holder.setLayout(self._list)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(holder)

        self._main_slot = QComboBox()
        self._main_slot.setEditable(True)  # 支持直接输入自定义模型
        self._main_slot.setInsertPolicy(QComboBox.NoInsert)
        self._main_slot.currentIndexChanged.connect(self._on_slot_changed)
        self._main_slot.lineEdit().editingFinished.connect(self._on_slot_typed)
        self._other_slots = []
        slot_form = QFormLayout()
        # rev14 语义：main 槽位 = 「上次使用的模型」，自动记录、不用手动设默认
        slot_form.addRow("上次使用", self._main_slot)
        for name in ("thinking", "fast", "embedding"):
            combo = QComboBox()
            combo.addItem("随轮次启用", "")
            combo.setEnabled(False)
            self._other_slots.append(combo)
            slot_form.addRow(name, combo)

        # 槽位分区入卡（rev57）：模块有自己的容器，不再与卡片列表挤在一起
        slot_card, slot_box = card()
        slot_box.addWidget(section_label("槽位"))
        self._note = QLabel("「上次使用」自动记录你最近选择的模型，新对话从它开始；无需手动设默认。")
        self._note.setObjectName("mutedNote")
        self._note.setWordWrap(True)
        slot_box.addWidget(self._note)
        slot_box.addLayout(slot_form)

        layout = QVBoxLayout(self)
        layout.addLayout(head)
        layout.addWidget(scroll, 1)
        layout.addSpacing(8)
        layout.addWidget(slot_card)
        self._loading = False
        self.refresh_metrics()

    # -- 外观（rev16：主题字号 token 不参与 sizeHint，宽高须按真实字号适配） --
    def refresh_metrics(self, font_size: str | None = None) -> None:
        """页标题（title token）的纵向适配；其余标签随应用字体（theme.apply 设置）自准。"""
        self._title.setMinimumHeight(text_fit.line_height(self._title, "title", font_size))

    # -- 更新 --------------------------------------------------------------
    def update_providers(self, providers, slots) -> None:
        self._providers = list(providers)
        self._slots = dict(slots or {})
        self._rebuild()

        self._loading = True
        self._main_slot.clear()
        self._main_slot.addItem("未绑定", "")
        for provider in self._providers:
            for model in provider.models:
                self._main_slot.addItem(f"{provider.name} / {model.id}", model.id)
        current = slots.get("main") or ""
        index = self._main_slot.findData(current)
        if index >= 0:
            self._main_slot.setCurrentIndex(index)
        elif current:
            self._main_slot.setEditText(current)  # 自定义模型回显
        self._loading = False

    def _rebuild(self) -> None:
        while self._list.count():
            item = self._list.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # 先摘父再 deleteLater（rev58，同 plugins）：孤儿控件带悬挂引用
                widget.setParent(None)
                widget.deleteLater()
        self._test_labels.clear()
        self._fetch_labels.clear()
        self._badges.clear()

        if not self._providers:
            empty = QLabel("尚无供应商。点击「添加供应商」开始。")
            empty.setObjectName("mutedNote")
            self._list.addWidget(empty)
            return

        for provider in self._providers:
            self._list.addWidget(self._make_card(provider))

    def _make_card(self, provider: ProviderSpec) -> QFrame:
        card_frame, layout = card()

        header = QHBoxLayout()
        header.addWidget(QLabel(f"<b>{provider.name}</b>"))
        if provider.local:
            # 本地服务不需要密钥：显示「未设置」会把人引向错误的排查方向
            status, stored = "本地 · 无需密钥", True
        elif provider.key_status == "stored":
            status, stored = "已存储", True
        else:
            status, stored = "未设置", False
        badge = QLabel(status)
        badge.setObjectName("keyBadge")
        badge.setProperty("keyStored", stored)
        self._badges[provider.id] = badge
        header.addWidget(badge)
        header.addStretch(1)
        fetch = QPushButton("获取模型列表")
        fetch.clicked.connect(
            lambda _=False, pid=provider.id: self._on_fetch(pid)
        )
        header.addWidget(fetch)
        # 一键逐个探测该供应商的全部模型（rev17）：逐个点「测试」在模型多时不可用
        test_all = QPushButton("全部检测")
        test_all.setEnabled(bool(provider.models))
        test_all.clicked.connect(
            lambda _=False, pid=provider.id, models=provider.models: self._test_all(pid, models)
        )
        header.addWidget(test_all)
        edit = QPushButton("编辑")
        edit.clicked.connect(lambda _=False, p=provider: self._on_edit(p))
        delete = QPushButton("删除")
        delete.clicked.connect(lambda _=False, p=provider: self._on_delete(p))
        header.addWidget(edit)
        header.addWidget(delete)
        layout.addLayout(header)

        fetch_label = QLabel("")
        fetch_label.setObjectName("fetchResult")
        fetch_label.setWordWrap(True)
        self._fetch_labels[provider.id] = fetch_label
        layout.addWidget(fetch_label)

        layout.addWidget(QLabel(provider.base_url))
        for model in provider.models:
            row = QHBoxLayout()
            row.addWidget(QLabel(f"· {model.id}"))
            test = QPushButton("测试")
            test.clicked.connect(
                lambda _=False, pid=provider.id, mid=model.id: self.test_requested.emit(pid, mid)
            )
            label = QLabel("")
            label.setObjectName("testResult")
            self._test_labels[(provider.id, model.id)] = label
            row.addWidget(test)
            row.addWidget(label)
            # rev25：思考能力状态（探测/覆盖）——出错时用户可在「编辑」里人工指定
            thinking = QLabel(self._reasoning_text(model))
            thinking.setObjectName("mutedNote")
            row.addWidget(thinking)
            row.addStretch(1)
            layout.addLayout(row)
        return card_frame

    @staticmethod
    def _reasoning_text(model) -> str:
        """思考状态文案（rev25）：覆盖优先，其次探测结果，最后「未检测」。"""
        if model.reasoning == "on":
            return "思考：强制开启"
        if model.reasoning == "off":
            return "思考：强制关闭"
        if model.reasoning_detected == "yes":
            return "思考：支持（自动）"
        if model.reasoning_detected == "no":
            return "思考：不支持（自动）"
        return "思考：未检测（首次使用时探测）"

    def on_test_result(self, event) -> None:
        label = self._test_labels.get((event.provider_id, event.model_id))
        if label is None:
            return
        if event.ok:
            label.setText(f"成功 · {event.latency_ms}ms")
        else:
            # 面向用户一律中文提示；未知码回退显示原因码本身（便于排查）
            code = event.error or ""
            label.setText(f"失败 · {ERROR_TEXT.get(code, code)}")
        label.setProperty("testOk", bool(event.ok))
        theme.restyle(label)

    def _test_all(self, provider_id: str, models) -> None:
        """全部检测（rev17）：先把每行置「检测中…」，再逐个发探测请求。"""
        for m in models:
            label = self._test_labels.get((provider_id, m.id))
            if label is not None:
                label.setText("检测中…")
                label.setProperty("testOk", None)
                theme.restyle(label)
            self.test_requested.emit(provider_id, m.id)

    def on_models_result(self, event) -> None:
        """处理 `provider.models.result`（rev9 §2）。

        成功 → 弹勾选框，勾选结果经既有 `provider.upsert` 落盘（api_key=None 表示密钥不变）；
        失败 → 就地给中文原因；端点不支持 /models 时直接告诉用户「手动填写」。
        """
        label = self._fetch_labels.get(event.provider_id)
        if not event.ok:
            code = event.error or ""
            # 指引优先于码：写清「下一步做什么」，码只作兜底（未知码直接显示）
            hint = FETCH_ERROR_HINT.get(code) or ERROR_TEXT.get(code, code)
            if label is not None:
                label.setText(f"失败 · {hint}")
                label.setProperty("fetchOk", False)
                theme.restyle(label)
            return

        provider = next((p for p in self._providers if p.id == event.provider_id), None)
        if provider is None:
            return
        if label is not None:
            label.setText(f"端点自报 {len(event.models)} 个模型")
            label.setProperty("fetchOk", True)
            theme.restyle(label)

        known = {m.id: m.ctx_window for m in provider.models}
        dialog = ModelPickerDialog(
            event.models, known, bind_hint=self._main_bind_hint(event.models), parent=self
        )
        if dialog.exec() != QDialog.Accepted:
            return
        models = dialog.selected_models()
        self.upsert_requested.emit(provider.model_copy(update={"models": models}), None)
        if dialog.should_bind_main() and models:
            # 导入即绑定：否则「模型导进来了但还不能对话」，还得再找一次槽位下拉
            self.slot_requested.emit("main", models[0].id)

    def _main_bind_hint(self, candidates: list[str]) -> str | None:
        """决定「设为开始对话的模型」是否出现、以及怎么措辞（rev14 语义）。

        - 上次使用为空 → 默认勾选（首次接入）
        - 上次使用的模型**不在本次候选里** → 也提示改用：这正是「换了模型表、上次使用
          还指着旧 ID」的情形，不提示就会让人对着一个不能用的模型发呆。
        - 上次使用的模型就在候选里 → 不打扰。
        """
        current = self._slots.get("main")
        if not current:
            return "把第一个选中的模型设为开始对话的模型"
        if current not in set(candidates):
            return f"上次使用的「{current}」不在本次候选内，勾选后改用第一个选中项"
        return None

    def set_theme(self, name: str | None) -> None:
        """主题切换后重算属性选择器（Qt 不会自动重算，须显式 unpolish/polish）。"""
        theme.restyle(
            *self._badges.values(), *self._test_labels.values(), *self._fetch_labels.values()
        )

    # -- 交互 --------------------------------------------------------------
    def _on_fetch(self, provider_id: str) -> None:
        label = self._fetch_labels.get(provider_id)
        if label is not None:
            label.setText("正在从端点获取…")
            label.setProperty("fetchOk", None)
            theme.restyle(label)
        self.models_requested.emit(provider_id)

    def _on_add(self) -> None:
        dialog = ProviderDialog(None, self)
        if dialog.exec() == QDialog.Accepted:
            self.upsert_requested.emit(dialog.result_spec(), dialog.api_key())

    def _on_edit(self, provider: ProviderSpec) -> None:
        dialog = ProviderDialog(provider, self)
        if dialog.exec() == QDialog.Accepted:
            self.upsert_requested.emit(dialog.result_spec(), dialog.api_key())

    def _on_delete(self, provider: ProviderSpec) -> None:
        if QMessageBox.question(self, "删除供应商", f"确定删除「{provider.name}」？") == QMessageBox.Yes:
            self.delete_requested.emit(provider.id)

    def _emit_main_slot(self) -> None:
        if self._loading:
            return
        data = self._main_slot.currentData()
        text = self._main_slot.currentText().strip()
        if data:
            self.slot_requested.emit("main", data)
        elif text and text != "未绑定":
            # 直接输入的自定义模型
            self.slot_requested.emit("main", text)
        else:
            # 回退默认（清绑定）
            self.slot_requested.emit("main", "")

    def _on_slot_changed(self, _index: int) -> None:
        self._emit_main_slot()

    def _on_slot_typed(self) -> None:
        self._emit_main_slot()
