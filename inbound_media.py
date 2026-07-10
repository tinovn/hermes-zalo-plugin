"""Pure, testable helpers for the Zalo inbound media contract (Python side).

Extracted from ``adapter.py`` so the media boundary can be unit-tested without a
running Hermes gateway or a live Zalo session. No Hermes imports here on purpose:
the adapter maps the returned ``kind`` to its own ``MessageType``.

Responsibilities:
  * Single session / media-cache root resolver (mirror of the sidecar's
    ``media-contract.js`` — canonical default ``/opt/data/zalo``) so both
    processes agree on one location.
  * Authoritative image detection by magic bytes (a re-check of the sidecar's
    work; header / declared type are only hints).
  * ``normalize_inbound_media`` — validate the cached local path is inside the
    cache root, choose the real MIME, and return the local path + MIME + kind
    with NO transport URL leaking through.
  * ``RecentImageIndex`` — a bounded, namespace-aware replacement for the old
    ``_LAST_THREAD_IMAGES`` module dict: dedupe by ``msg_id``, order by
    ``(event_ts, ingress_seq)``, keep N newest per chat, LRU-cap total chats,
    and clear on adapter reload / account change.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

# ─── Cache root resolver (single source of truth) ──────────────────────────

CANONICAL_SESSION_DIR = "/opt/data/zalo"


def resolve_session_dir(env: Optional[dict] = None) -> str:
    """Resolve the Zalo session dir; canonical default ``/opt/data/zalo``."""
    env = env if env is not None else os.environ
    v = env.get("ZALO_PERSONAL_SESSION_DIR")
    if v and str(v).strip():
        return str(v).strip()
    return CANONICAL_SESSION_DIR


def resolve_media_cache_dir(env: Optional[dict] = None) -> str:
    return os.path.join(resolve_session_dir(env), "media-cache")


# ─── Magic-byte image sniffing (authoritative) ─────────────────────────────

_IMAGE_MIME_BY_EXT = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
}


def image_mime_for_ext(ext: Optional[str]) -> Optional[str]:
    return _IMAGE_MIME_BY_EXT.get(str(ext or "").lower().lstrip("."))


def sniff_image_bytes(head: Optional[bytes]) -> Optional[Tuple[str, str]]:
    """Return ``(mime, ext)`` for a recognized image or ``None``.

    Recognizes JPEG, PNG, GIF87a/89a and WebP from leading bytes.
    """
    if not head or len(head) < 12:
        return None
    b = head
    if b[0] == 0xFF and b[1] == 0xD8 and b[2] == 0xFF:
        return ("image/jpeg", "jpg")
    if (
        b[0] == 0x89 and b[1] == 0x50 and b[2] == 0x4E and b[3] == 0x47
        and b[4] == 0x0D and b[5] == 0x0A and b[6] == 0x1A and b[7] == 0x0A
    ):
        return ("image/png", "png")
    if (
        b[0] == 0x47 and b[1] == 0x49 and b[2] == 0x46 and b[3] == 0x38
        and b[4] in (0x37, 0x39) and b[5] == 0x61
    ):
        return ("image/gif", "gif")
    if (
        b[0] == 0x52 and b[1] == 0x49 and b[2] == 0x46 and b[3] == 0x46
        and b[8] == 0x57 and b[9] == 0x45 and b[10] == 0x42 and b[11] == 0x50
    ):
        return ("image/webp", "webp")
    return None


# ─── Path safety ────────────────────────────────────────────────────────────

def is_within_root(root: str, target: str) -> bool:
    """True when ``target`` resolves to a path inside ``root``."""
    try:
        root_r = os.path.realpath(root)
        tgt_r = os.path.realpath(target)
    except (OSError, ValueError):
        return False
    return tgt_r == root_r or tgt_r.startswith(root_r + os.sep)


def _default_read_head(path: str, n: int = 32) -> Optional[bytes]:
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as fh:
            return fh.read(n)
    except OSError:
        return None


# ─── Normalized media result ────────────────────────────────────────────────

@dataclass
class NormalizedMedia:
    kind: Optional[str]           # "image" | "voice" | "file" | None
    is_image: bool
    local_path: Optional[str]
    mime_type: Optional[str]
    reason: Optional[str] = None  # why the media is not a usable local image

    @property
    def usable(self) -> bool:
        return self.local_path is not None


def normalize_inbound_media(
    content: dict,
    cache_root: str,
    *,
    read_head: Optional[Callable[[str], Optional[bytes]]] = None,
) -> NormalizedMedia:
    """Validate a sidecar media payload into a safe local reference.

    * Rejects a ``local_path`` outside ``cache_root`` (path escape).
    * Image-ness is decided by re-sniffing the local file's magic bytes, not by
      the declared kind — so a file whose bytes are an image is treated as an
      image, and a non-image with an image extension is not.
    * Never returns a transport URL (``media_url`` / ``url``); only the local
      path and the real MIME.
    """
    read_head = read_head or _default_read_head
    kind = content.get("kind")
    if kind not in ("image", "voice", "file"):
        return NormalizedMedia(None, False, None, None, reason="not_media")

    lp = content.get("local_path")
    declared_mime = content.get("mime_type")
    if not lp:
        return NormalizedMedia(kind, False, None, declared_mime, reason="no_local_path")
    if not is_within_root(cache_root, lp):
        return NormalizedMedia(kind, False, None, None, reason="path_escape")

    head = read_head(lp)
    if head is None:
        return NormalizedMedia(kind, False, None, None, reason="unreadable")

    sniff = sniff_image_bytes(head)
    if sniff:
        # Magic is authoritative — this is a real image regardless of how it
        # was delivered (native photo or Zalo File).
        return NormalizedMedia("image", True, lp, sniff[0], reason=None)

    # Not an image by magic — keep original non-image kind (voice/file) so it is
    # not routed to vision. A declared "image" that fails magic is downgraded.
    if kind == "image":
        return NormalizedMedia("file", False, lp, declared_mime, reason="not_image_bytes")
    return NormalizedMedia(kind, False, lp, declared_mime, reason=None)


# ─── Recent-image index (bounded, namespaced) ───────────────────────────────

@dataclass
class RecentImage:
    msg_id: str
    local_path: str
    from_uid: str
    from_name: str
    event_ts: float
    ingress_seq: int
    mime_type: Optional[str] = None
    caption: str = ""


def _order_key(img: "RecentImage") -> Tuple[float, int]:
    return (float(img.event_ts or 0), int(img.ingress_seq or 0))


class RecentImageIndex:
    """Bounded per-chat recent-image store.

    Namespaced by ``(self_uid, chat_id)`` so a hot-reloaded / replaced account
    never inherits another account's images. Dedupes by ``msg_id``, orders by
    ``(event_ts, ingress_seq)`` (NOT insertion / download-completion order),
    keeps ``per_chat`` newest per chat, and LRU-evicts whole chats past
    ``max_chats``. Not rebuilt from disk after a restart — returns empty.
    """

    def __init__(self, per_chat: int = 5, max_chats: int = 200):
        self._per_chat = int(per_chat)
        self._max_chats = int(max_chats)
        self._self_uid: Optional[str] = None
        # namespace_key -> OrderedDict[msg_id -> RecentImage]
        self._chats: "OrderedDict[str, OrderedDict[str, RecentImage]]" = OrderedDict()

    def set_account(self, self_uid) -> None:
        """Bind the active account; clears everything on account change."""
        new = str(self_uid) if self_uid is not None else None
        if self._self_uid is not None and new != self._self_uid:
            self._chats.clear()
        self._self_uid = new

    def clear(self) -> None:
        self._chats.clear()

    def _ns(self, chat_id) -> str:
        return f"{self._self_uid}:{chat_id}"

    def add(self, chat_id, img: RecentImage) -> None:
        if not img or not img.local_path:
            return
        ns = self._ns(chat_id)
        bucket = self._chats.get(ns)
        if bucket is None:
            bucket = OrderedDict()
            self._chats[ns] = bucket
        self._chats.move_to_end(ns)  # LRU: most-recently-touched chat last
        bucket[str(img.msg_id)] = img  # dedupe by msg_id
        # Keep only the newest ``per_chat`` by order key.
        ordered = sorted(bucket.values(), key=_order_key)
        keep = ordered[-self._per_chat:]
        bucket.clear()
        for it in keep:
            bucket[str(it.msg_id)] = it
        # LRU-evict whole chats past the cap.
        while len(self._chats) > self._max_chats:
            self._chats.popitem(last=False)

    def recent(self, chat_id, count: int = 1) -> List[RecentImage]:
        """Return up to ``count`` newest images for a chat, oldest→newest."""
        bucket = self._chats.get(self._ns(chat_id))
        if not bucket:
            return []
        ordered = sorted(bucket.values(), key=_order_key)
        n = max(1, min(int(count), self._per_chat))
        return ordered[-n:]

    def chat_count(self) -> int:
        return len(self._chats)
