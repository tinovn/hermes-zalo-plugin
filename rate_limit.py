"""Hourly quota chống spam dùng chung (không phụ thuộc Hermes core).

Tách khỏi adapter.py để (a) test độc lập không cần import cả gateway, (b) tái
dùng cho nhiều đường (hiện dùng cho nhắc hẹn native khi non-owner tự đặt).

State là JSON `{"by_chat": {id: {window_start, count}}, "by_user": {...}}` lưu ra
đĩa; cửa sổ trượt 1 giờ. Caller truyền sẵn `path` + ngưỡng nên module này thuần
logic, không đọc env. Owner KHÔNG đi qua đây (cổng non-owner đã cho owner full
access trước đó) nên không cần bypass owner tại đây.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_WINDOW_SEC = 3600


def load_state(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"by_chat": {}, "by_user": {}}


def save_state(path: Path, state: Dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:  # pragma: no cover - lỗi đĩa hiếm
        logger.warning("quota state save failed (%s): %s", path.name, e)


def check(
    path: Path,
    chat_id: str,
    user_id: str,
    per_chat: int,
    per_user: int,
    chat_msg: str,
    user_msg: str,
) -> Optional[str]:
    """None nếu cho phép, else chuỗi lỗi (ưu tiên chat trước, rồi user)."""
    state = load_state(path)
    now = time.time()
    hour_ago = now - _WINDOW_SEC
    by_chat = state.get("by_chat", {}) or {}
    by_user = state.get("by_user", {}) or {}
    if chat_id:
        rec = by_chat.get(chat_id) or {"window_start": 0, "count": 0}
        if rec.get("window_start", 0) >= hour_ago and rec.get("count", 0) >= per_chat:
            return chat_msg
    if user_id:
        rec = by_user.get(user_id) or {"window_start": 0, "count": 0}
        if rec.get("window_start", 0) >= hour_ago and rec.get("count", 0) >= per_user:
            return user_msg
    return None


def bump(path: Path, chat_id: str, user_id: str) -> None:
    """Ghi nhận 1 lượt cho cả bucket chat và user; tự reset cửa sổ đã hết hạn."""
    state = load_state(path)
    now = time.time()
    hour_ago = now - _WINDOW_SEC
    by_chat = state.setdefault("by_chat", {})
    by_user = state.setdefault("by_user", {})
    for key, bucket in ((chat_id, by_chat), (user_id, by_user)):
        if not key:
            continue
        rec = bucket.get(key) or {"window_start": now, "count": 0}
        if rec.get("window_start", 0) < hour_ago:
            rec = {"window_start": now, "count": 0}
        rec["count"] = rec.get("count", 0) + 1
        bucket[key] = rec
    save_state(path, state)
