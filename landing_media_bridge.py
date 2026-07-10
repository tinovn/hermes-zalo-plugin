"""Server-to-server Zalo→landing image bridge.

Replaces the ``zalo_recent_image_base64 -> model -> landing_upload_image`` flow
(which injected up to ~8MB of base64 into the LLM context and repeatedly blew the
token budget) with a direct, chat-scoped upload that the model never sees the
bytes of. The model only receives durable ``image_url`` metadata.

Security contract (see plan Phase 3):
  * The model supplies only ``slug`` (+ optional ``filename``/``count``). It can
    NOT pass ``chat_id``, a local path, or a session id — those come from the
    trusted Hermes ``task_id`` and the recent-image index.
  * The cached file is opened with ``O_NOFOLLOW`` relative to the media-cache
    directory fd; ``fstat`` / size-check / read / magic / SHA-256 all happen on
    that same descriptor (no check-then-open TOCTOU, no symlink escape).
  * Remote filenames are content-addressed (``<stem>-<sha256[:12]>.<ext>``) so a
    retry with identical bytes targets the same object and never overwrites a
    different newer image.
  * The upload key is read from the environment only, never logged, and the HTTP
    caller must not follow redirects.

The module is pure aside from the injected ``http_post`` and ``session_resolver``
callables, so it is unit-testable without a network or a live Hermes.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

try:  # reuse the authoritative magic sniff (DRY with the inbound contract)
    from .inbound_media import sniff_image_bytes, is_within_root  # type: ignore
except Exception:  # pragma: no cover - path-loaded in production
    from inbound_media import sniff_image_bytes, is_within_root

MAX_IMAGE_BYTES = 6 * 1024 * 1024  # matches landing_upload_image server cap
_STEM_RE = re.compile(r"[^a-z0-9]+")


class BridgeError(Exception):
    """Raised for any validation/authorization failure (fail closed)."""


def sanitize_stem(stem: Optional[str], fallback: str = "img") -> str:
    """Reduce an optional filename stem to a safe slug fragment."""
    base = os.path.basename(str(stem or "")).rsplit(".", 1)[0]
    base = _STEM_RE.sub("-", base.lower()).strip("-")
    return base[:40] or fallback


def content_addressed_name(stem: Optional[str], digest_hex: str, ext: str) -> str:
    """``<sanitized-stem>-<sha256[:12]>.<ext>`` — stable per byte content."""
    return f"{sanitize_stem(stem)}-{digest_hex[:12]}.{ext}"


def read_cached_image(cache_dir: str, local_path: str, *, max_bytes: int = MAX_IMAGE_BYTES) -> Tuple[bytes, str, str]:
    """Safely read a cached image. Returns ``(data, mime, ext)``.

    Opens the basename relative to the cache-dir fd with ``O_NOFOLLOW`` and does
    fstat/size/read/magic all on the same descriptor. Raises ``BridgeError`` for
    a path outside the root, a symlink, a non-regular file, an oversize file, or
    bytes that are not a valid image.
    """
    if not local_path or not is_within_root(cache_dir, local_path):
        raise BridgeError("path outside media-cache root")
    basename = os.path.basename(local_path)
    if not basename or basename in (".", "..") or os.sep in basename or (os.altsep and os.altsep in basename):
        raise BridgeError("invalid basename")

    dir_fd = os.open(cache_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(basename, flags, dir_fd=dir_fd)
    except OSError as e:
        os.close(dir_fd)
        raise BridgeError(f"open failed: {e.__class__.__name__}")
    else:
        os.close(dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise BridgeError("not a regular file")
        if st.st_size > max_bytes:
            raise BridgeError("image too large")
        data = os.read(fd, max_bytes + 1)
    finally:
        os.close(fd)
    if len(data) > max_bytes:
        raise BridgeError("image too large")
    sniff = sniff_image_bytes(data[:32])
    if not sniff:
        raise BridgeError("not a valid image")
    mime, ext = sniff
    return data, mime, ext


@dataclass
class BridgeConfig:
    url: str          # fixed HTTPS origin, e.g. https://mcp.tino.vn/tools/landing_upload_image
    key: str          # upload-only agent key (read from env, never logged)
    cache_dir: str


def load_bridge_config(env: Dict[str, str], cache_dir: str) -> BridgeConfig:
    """Read the bridge config from the environment and validate the URL shape."""
    url = str(env.get("TINO_LANDING_BRIDGE_URL") or "").strip()
    key = str(env.get("TINO_LANDING_BRIDGE_KEY") or "").strip()
    if not url or not key:
        raise BridgeError("bridge URL/key not configured")
    if not url.lower().startswith("https://"):
        raise BridgeError("bridge URL must be https")
    if "@" in url.split("//", 1)[-1].split("/", 1)[0]:
        raise BridgeError("bridge URL must not contain userinfo")
    return BridgeConfig(url=url, key=key, cache_dir=cache_dir)


# session_resolver(task_id) -> {"chat_id": str, "session_id": str} | None
SessionResolver = Callable[[str], Optional[Dict[str, str]]]
# recent_fn(chat_id, count) -> list of objects with .local_path / .from_name
RecentFn = Callable[[str, int], List[Any]]
# http_post(url, headers, json_body) -> dict  (must NOT follow redirects)
HttpPost = Callable[[str, Dict[str, str], Dict[str, Any]], Dict[str, Any]]


class LandingMediaBridge:
    def __init__(self, config: BridgeConfig, *, recent_fn: RecentFn,
                 session_resolver: SessionResolver, http_post: HttpPost):
        self._cfg = config
        self._recent = recent_fn
        self._resolve = session_resolver
        self._post = http_post

    def upload_recent(self, *, task_id: str, slug: str,
                      filename: Optional[str] = None, count: int = 1) -> Dict[str, Any]:
        slug = str(slug or "").strip()
        if not slug:
            raise BridgeError("slug required")
        # Trusted identity: resolve chat + session from task_id ONLY.
        sess = self._resolve(str(task_id or ""))
        if not sess or not sess.get("chat_id") or not sess.get("session_id"):
            raise BridgeError("cannot resolve current chat/session")
        chat_id = str(sess["chat_id"])
        session_id = str(sess["session_id"])

        try:
            n = max(1, min(int(count), 5))
        except (TypeError, ValueError):
            n = 1

        recents = self._recent(chat_id, n)  # oldest→newest
        if not recents:
            raise BridgeError("no recent image in this chat")

        images: List[Dict[str, Any]] = []
        for rec in recents:
            local_path = getattr(rec, "local_path", None) or (rec.get("local_path") if isinstance(rec, dict) else None)
            data, mime, ext = read_cached_image(self._cfg.cache_dir, str(local_path or ""))
            digest = hashlib.sha256(data).hexdigest()
            remote_name = content_addressed_name(filename, digest, ext)
            image_url = self._upload_one(session_id, slug, data, mime, remote_name)
            images.append({
                "image_url": image_url,
                "filename": remote_name,
                "mime": mime,
                "size": len(data),
            })
        # Compact result; never returns sender, local path, chat id or base64.
        return {"slug": slug, "count": len(images), "images": images}

    def _upload_one(self, session_id: str, slug: str, data: bytes, mime: str, remote_name: str) -> str:
        import base64 as _b64
        headers = {
            "X-Agent-Key": self._cfg.key,
            "X-Session": session_id,
            "Content-Type": "application/json",
        }
        body = {
            "slug": slug,
            # base64 exists ONLY inside this process-to-process request, never
            # in the model transcript.
            "image_base64": "data:%s;base64,%s" % (mime, _b64.b64encode(data).decode()),
            "filename": remote_name,
        }
        resp = self._post(self._cfg.url, headers, body) or {}
        image_url = _extract_image_url(resp)
        if not image_url:
            raise BridgeError("upload failed: no image_url in response")
        if not _is_durable_asset_url(image_url):
            raise BridgeError("upload returned a non-durable URL")
        return image_url


def _extract_image_url(resp: Dict[str, Any]) -> str:
    for key in ("image_url", "url"):
        v = resp.get(key)
        if isinstance(v, str) and v:
            return v
    result = resp.get("result")
    if isinstance(result, dict):
        for key in ("image_url", "url"):
            v = result.get(key)
            if isinstance(v, str) and v:
                return v
    return ""


def _is_durable_asset_url(url: str) -> bool:
    """Accept only durable ``/<slug>/assets/`` URLs; reject temporary transports
    (``/media/...``), local paths and empty values."""
    u = str(url or "")
    if not u.lower().startswith("https://"):
        return False
    if "/media/" in u:
        return False
    return "/assets/" in u
