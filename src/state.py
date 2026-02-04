"""
State persistence for seen tweets.
Stores last seen tweet id per username in a JSON file.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Optional, Dict, Any


logger = logging.getLogger(__name__)


@dataclass
class UserState:
    last_seen_id: Optional[str] = None
    updated_at: Optional[int] = None


class StateStore:
    """Simple JSON-backed state store."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = Lock()
        self._data: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._data = {}
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
            self._data = json.loads(raw) if raw.strip() else {}
        except Exception as e:
            logger.warning(f"读取状态文件失败，将重新生成: {e}")
            self._data = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp_path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(self.path)
        except Exception as e:
            logger.error(f"保存状态文件失败: {e}")

    def get_last_seen_id(self, username: str) -> Optional[str]:
        with self._lock:
            entry = self._data.get(username, {})
            last_seen_id = entry.get("last_seen_id")
            return str(last_seen_id) if last_seen_id else None

    def set_last_seen_id(self, username: str, last_seen_id: str) -> None:
        with self._lock:
            self._data[username] = {
                "last_seen_id": str(last_seen_id),
                "updated_at": int(time.time()),
            }
            self._save()
