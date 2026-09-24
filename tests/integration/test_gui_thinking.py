"""副思考链页 GUI 测试（切片 4b）：门控禁用、自动档策略、运行一次、思考流与调用记录。"""

from __future__ import annotations

import time

from app import bootstrap as bootstrap_mod
from app import paths
from gui.main_window import MainWindow
from shared.envelope import BtcmRun, BtcmUpdate, FeatureToggle, NewSession
from tests.mocks.btcm_gateway import BtcmMockGateway


def _window(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path / "ymtdata")
    gateway = BtcmMockGateway(verdicts=["pass"], slots={"main": "mock-model"})
    ctx = bootstrap_mod.bootstrap(gateway_factory=lambda _store: gateway)
    return ctx, MainWindow(ctx.bridge, data_root=str(ctx.root))


def _wait(qapp, predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _focus(window) -> None:
    """把副思考链页切到前台 —— 否则 `isVisible()` 因祖先隐藏恒为 False。"""
    window.stack.setCurrentWidget(window.thinking_page)


def test_page_disabled_until_feature_on(tmp_path, monkeypatch, qapp):
    ctx, window = _window(tmp_path, monkeypatch)
    try:
        window.show()
        ctx.controller.push_initial_state()
        qapp.processEvents()
        _focus(window)
        page = window.thinking_page
        assert page._run.isEnabled() is False  # 宿主关 → 运行一次禁用
        assert "未就绪" in page._ready_label.text()
        assert page._trigger.isEnabled() is False
        assert page._slot.isEnabled() is False
        assert page._question.isEnabled() is False
        assert page._strategy_card.isVisible() is False

        ctx.controller.handle(FeatureToggle(name="btcm", enabled=True))
        ctx.controller.handle(BtcmUpdate(trigger="manual"))
        qapp.processEvents()
        assert page._run.isEnabled() is True
        assert page._ready_label.text() == "已就绪"
        assert page._trigger.isEnabled() is True
        assert page._slot.isEnabled() is True
        assert page._question.isEnabled() is True
    finally:
        ctx.worker.stop()
        window.close()


def test_strategy_card_and_manual_button(tmp_path, monkeypatch, qapp):
    ctx, window = _window(tmp_path, monkeypatch)
    try:
        window.show()
        ctx.controller.push_initial_state()
        ctx.controller.handle(FeatureToggle(name="btcm", enabled=True))
        ctx.controller.handle(BtcmUpdate(trigger="auto"))
        qapp.processEvents()
        _focus(window)
        page = window.thinking_page
        assert page._strategy_card.isVisible() is True
        assert "严重矛盾" in page._strategy_note.text()
        assert "btcm.think" in page._strategy_note.text()

        # 「改为手动」→ 经 bus 落盘真值（worker 线程处理）
        page._to_manual.click()
        assert _wait(qapp, lambda: ctx.config_store.load("modules").btcm.trigger == "manual")
        qapp.processEvents()
        assert page._strategy_card.isVisible() is False
    finally:
        ctx.worker.stop()
        window.close()


def test_slot_change_is_persisted(tmp_path, monkeypatch, qapp):
    ctx, window = _window(tmp_path, monkeypatch)
    try:
        window.show()
        ctx.controller.push_initial_state()
        ctx.controller.handle(FeatureToggle(name="btcm", enabled=True))
        qapp.processEvents()
        _focus(window)
        page = window.thinking_page
        page._slot.setCurrentIndex(page._slot.findData("main"))
        assert _wait(qapp, lambda: ctx.config_store.load("modules").btcm.slot == "main")
    finally:
        ctx.worker.stop()
        window.close()


def test_run_once_streams_thinking_and_records_usage(tmp_path, monkeypatch, qapp):
    ctx, window = _window(tmp_path, monkeypatch)
    try:
        window.show()
        ctx.controller.push_initial_state()
        ctx.controller.handle(FeatureToggle(name="btcm", enabled=True))
        ctx.controller.handle(BtcmUpdate(trigger="manual"))
        ctx.controller.handle(NewSession())
        qapp.processEvents()
        _focus(window)
        page = window.thinking_page

        ctx.controller.handle(BtcmRun(question="需要深思的问题", effort="standard", mode="auto"))
        qapp.processEvents()

        stream = page._stream.toPlainText()
        assert "—— 创意 ——" in stream and "—— 验证 ——" in stream
        assert "验证=pass" in stream and "[运行完成]" in stream
        assert '"verdict": "pass"' in stream

        # 内部推理默认折叠，但内容已采集（勾选即见）
        assert page._reasoning.isVisible() is False
        assert "核对候选" in page._reasoning.toPlainText()
        page._reasoning_toggle.setChecked(True)
        qapp.processEvents()
        assert page._reasoning.isVisible() is True

        # 调用记录：终止原因 / 用量 / 耗时 / 状态
        assert page._history.count() == 1
        record = page._history.item(0).text()
        assert "终止=validation_passed" in record
        assert "30 tokens" in record and "完成" in record
    finally:
        ctx.worker.stop()
        window.close()


def test_empty_question_does_not_run(tmp_path, monkeypatch, qapp):
    ctx, window = _window(tmp_path, monkeypatch)
    try:
        window.show()
        ctx.controller.push_initial_state()
        ctx.controller.handle(FeatureToggle(name="btcm", enabled=True))
        qapp.processEvents()
        _focus(window)
        page = window.thinking_page
        page._question.setPlainText("   ")
        page._on_run()
        assert page._history.count() == 0
        assert page._stream.toPlainText() == ""
    finally:
        ctx.worker.stop()
        window.close()
