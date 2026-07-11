"""Bounded image downscaling for the Zalo media pipeline.

Phone photos arrive at 3-12MB / 3000-4000px while a landing page (and the
``landing_upload_image`` 6MB server cap) only needs ~1024px. This module
downscales a cached image's bytes to a maximum dimension BEFORE the
server-to-server upload, so payloads shrink ~10-30x and big phone photos stop
bouncing off the size cap.

Contract (fail-open by design):
  * ``resize_image_to_max_dim`` NEVER raises on bad input — on any failure
    (Pillow missing, undecodable bytes, animated image, encode error) it
    returns the ORIGINAL bytes with ``resized=False`` and a machine-readable
    ``reason``. Byte-size and validity enforcement stay with the caller (the
    bridge's caps), so a resize problem can only ever degrade to today's
    behavior, never block an upload that would previously succeed.
  * The container format is preserved (jpg→jpg, png→png, webp→webp, gif→gif)
    so the caller's sniffed mime/ext stay correct for content-addressed
    naming.
  * EXIF orientation is applied before scaling — re-encoding drops EXIF, so a
    portrait phone photo would otherwise land rotated on the page. All other
    metadata (GPS, device) is dropped: smaller AND more private.
  * Animated GIF/WebP pass through untouched — a naive re-save would silently
    flatten them to the first frame.

Pure module: no Hermes imports, no env reads, no filesystem I/O — mirrors the
testability style of ``inbound_media.py`` / ``landing_media_bridge.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

MAX_DIMENSION_DEFAULT = 1024

# JPEG/WebP re-encode quality. 85 is the classic "visually lossless on
# photos" point — below ~80 ring artifacts show on text/edges, above ~90 the
# file size climbs steeply for no visible gain at landing-page display sizes.
_JPEG_QUALITY = 85
_WEBP_QUALITY = 85

_PIL_FORMAT_BY_EXT = {
    "jpg": "JPEG",
    "jpeg": "JPEG",
    "png": "PNG",
    "webp": "WEBP",
    "gif": "GIF",
}


@dataclass
class ResizeResult:
    data: bytes
    mime: str
    ext: str
    resized: bool
    width: Optional[int] = None   # final pixel dimensions when known
    height: Optional[int] = None
    reason: Optional[str] = None  # why NOT resized (None when resized=True)


def resize_image_to_max_dim(
    data: bytes,
    *,
    mime: str,
    ext: str,
    max_dim: int = MAX_DIMENSION_DEFAULT,
) -> ResizeResult:
    """Downscale ``data`` so neither side exceeds ``max_dim`` pixels.

    Aspect ratio is preserved (LANCZOS). Images already within the bound are
    returned byte-identical (no lossy re-encode of an already-small photo).
    See the module docstring for the full fail-open contract.
    """

    def _passthrough(reason: str, w: Optional[int] = None, h: Optional[int] = None) -> ResizeResult:
        return ResizeResult(data=data, mime=mime, ext=ext, resized=False,
                            width=w, height=h, reason=reason)

    try:
        dim = int(max_dim)
    except (TypeError, ValueError):
        return _passthrough("bad_max_dim")
    if dim <= 0:
        return _passthrough("disabled")

    fmt = _PIL_FORMAT_BY_EXT.get(str(ext or "").lower().lstrip("."))
    if not fmt:
        return _passthrough("unsupported_format")

    try:
        from io import BytesIO
        from PIL import Image, ImageOps  # type: ignore
    except ImportError:
        return _passthrough("pillow_unavailable")

    try:
        img = Image.open(BytesIO(data))
        if getattr(img, "is_animated", False):
            w, h = img.size
            return _passthrough("animated", w, h)
        img.load()
    except Exception:
        # Covers truncated bytes and decompression bombs alike — the caller's
        # byte cap still guards the upload.
        return _passthrough("decode_failed")

    try:
        # Bake the EXIF orientation into the pixels BEFORE measuring/scaling
        # (the re-encoded output carries no EXIF for a viewer to honor).
        img = ImageOps.exif_transpose(img) or img
        w, h = img.size
        if w <= dim and h <= dim:
            return _passthrough("already_small", w, h)

        resampling = getattr(Image, "Resampling", Image)  # Pillow <9.1 compat
        img.thumbnail((dim, dim), resampling.LANCZOS)

        out = BytesIO()
        if fmt == "JPEG":
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")  # CMYK/palette/alpha → JPEG-safe
            img.save(out, format="JPEG", quality=_JPEG_QUALITY,
                     optimize=True, progressive=True)
        elif fmt == "WEBP":
            img.save(out, format="WEBP", quality=_WEBP_QUALITY, method=4)
        elif fmt == "PNG":
            img.save(out, format="PNG", optimize=True)
        else:  # static GIF
            if img.mode not in ("P", "L"):
                palette = getattr(Image, "Palette", Image)  # Pillow <9.2 compat
                img = img.convert("P", palette=palette.ADAPTIVE)
            img.save(out, format="GIF")
        fw, fh = img.size
        return ResizeResult(data=out.getvalue(), mime=mime, ext=ext,
                            resized=True, width=fw, height=fh, reason=None)
    except Exception:
        return _passthrough("encode_failed")
