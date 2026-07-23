"""Scheduler cho nhắc động non-owner (dep-free, không phụ thuộc Hermes core).

Một reminder = danh sách ``fire_times`` tính SẴN lúc tạo (escalation: các mốc
từ giờ đặt → hạn) + con trỏ ``next_idx``. Deterministic + test được không cần
gateway. IO là JSON atomic ra đĩa (giống rate_limit.py); các hàm thuần
(compute/current/due/overdue) nhận ``now`` làm tham số để test bằng giá trị cố
định. Tick asyncio + gọi LLM nằm ở adapter (Phase 4), KHÔNG ở module này.

Record schema:
    {id, chat_id, thread_type='group', task, target_display, target_uid?,
     fire_times: [epoch,...], next_idx, max_attempts, deadline_at?,
     created_by_uid, created_at}
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_STATE_FILENAME = "reminder_schedule.json"


def state_path() -> Path:
    """Đường mặc định của file state (đồng bộ với rate_limit/session dir)."""
    return Path(os.getenv("ZALO_PERSONAL_SESSION_DIR") or "/opt/data/zalo") / _STATE_FILENAME


# ── Pure logic ─────────────────────────────────────────────────────────────
def compute_fire_times(
    start_at: float, deadline_at: Optional[float], max_attempts: int
) -> List[float]:
    """Các mốc bắn. Không hạn / max_attempts<=1 → chỉ 1 mốc tại start.
    Có hạn hợp lệ → ``max_attempts`` mốc CÁCH ĐỀU từ start tới deadline (mốc
    cuối = deadline)."""
    n = max(1, int(max_attempts))
    start = float(start_at)
    if not deadline_at or float(deadline_at) <= start or n <= 1:
        return [start]
    span = float(deadline_at) - start
    return [start + span * i / (n - 1) for i in range(n)]


def current_fire_time(rec: Dict[str, Any]) -> Optional[float]:
    """Mốc bắn kế tiếp chưa xử lý, hoặc None nếu đã hết."""
    ft = rec.get("fire_times") or []
    idx = int(rec.get("next_idx", 0))
    if 0 <= idx < len(ft):
        return float(ft[idx])
    return None


def is_done(rec: Dict[str, Any]) -> bool:
    return current_fire_time(rec) is None


def advance_rec(rec: Dict[str, Any]) -> None:
    """Đánh dấu đã xử lý mốc hiện tại (tăng con trỏ). Thuần, không IO."""
    rec["next_idx"] = int(rec.get("next_idx", 0)) + 1


def is_overdue(rec: Dict[str, Any], now: float, grace: float) -> bool:
    """True nếu mốc hiện tại trễ quá ``grace`` giây (bỏ qua, không bắn spam)."""
    cft = current_fire_time(rec)
    return cft is not None and (now - cft) > grace


def due(state: Dict[str, Any], now: float) -> List[Dict[str, Any]]:
    """Các reminder tới giờ (mốc hiện tại <= now, chưa hết)."""
    out: List[Dict[str, Any]] = []
    for rec in (state.get("reminders") or {}).values():
        cft = current_fire_time(rec)
        if cft is not None and cft <= now:
            out.append(rec)
    return out


# ── IO (atomic, an toàn khi hỏng) ──────────────────────────────────────────
def load(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("reminders"), dict):
            return data
    except Exception:
        pass
    return {"reminders": {}}


def save(path: Path, state: Dict[str, Any]) -> None:
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
    except Exception as e:  # pragma: no cover - lỗi đĩa hiếm
        logger.warning("reminder_scheduler save failed (%s): %s", p.name, e)


def add(path: Path, rec: Dict[str, Any]) -> Dict[str, Any]:
    state = load(path)
    rec.setdefault("next_idx", 0)
    state["reminders"][rec["id"]] = rec
    save(path, state)
    return rec


def advance(path: Path, rid: str) -> Optional[Dict[str, Any]]:
    """Tăng con trỏ + persist. Hết mốc → xoá khỏi store, trả None."""
    state = load(path)
    rec = state["reminders"].get(rid)
    if rec is None:
        return None
    advance_rec(rec)
    if is_done(rec):
        del state["reminders"][rid]
        save(path, state)
        return None
    save(path, state)
    return rec


def remove(path: Path, rid: str) -> None:
    state = load(path)
    if rid in state["reminders"]:
        del state["reminders"][rid]
        save(path, state)
