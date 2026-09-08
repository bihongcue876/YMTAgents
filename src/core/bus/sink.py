"""事件流落盘与审计（docs 03 §5.4 / §6）。

- events.jsonl：会话内容唯一事实源，append-only，唯一写者是边界关卡层。
- audit.jsonl：全局安全审计，跨会话追加。
- 读取时末条不完整行截断，截断事实记入 audit（docs 03 §6 不变量 4）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def now_iso_ms() -> str:
    """ISO 8601 UTC 毫秒（docs 03 §1）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class EventSink:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def session_dir(self, session_id: str) -> Path:
        return self.root / "sessions" / session_id

    def events_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "events.jsonl"

    def append_event(
        self, session_id: str, seq: int, src: str, type_: str, payload: dict[str, Any]
    ) -> None:
        path = self.events_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = {
            "v": 1,
            "seq": seq,
            "ts": now_iso_ms(),
            "src": src,
            "type": type_,
            "payload": payload,
        }
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
            fh.flush()

    def read_events(self, session_id: str) -> list[dict]:
        path = self.events_path(session_id)
        if not path.exists():
            return []
        events: list[dict] = []
        truncated = False
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    events.append(json.loads(raw))
                except json.JSONDecodeError:
                    truncated = True
                    break
        if truncated:
            self.append_audit("event_stream_truncated", session_id=session_id)
        return events

    def append_audit(self, action: str, **fields: Any) -> None:
        path = self.root / "logs" / "audit.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = {"ts": now_iso_ms(), "action": action, **fields}
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
            fh.flush()
