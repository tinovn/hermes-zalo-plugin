"""SSRF-guarded image fetch cho ``zalo_send_image(image_url=...)``.

Model chỉ đưa MỘT url (vd ``qr_url`` từ media_store ``mcp.tino.vn``); adapter tải
ảnh SERVER-SIDE rồi gửi vào Zalo. Bytes/URL KHÔNG hiển thị cho khách và base64
11KB không còn phải đi qua LLM (nguồn gốc cú treo ~160s ở flow QR cũ).

Chống SSRF (model-controlled URL fetch server-side):
  * chỉ ``https`` — từ chối http/ftp/file/...
  * host phải nằm trong allowlist (mặc định ``mcp.tino.vn`` — domain nội bộ Tino,
    nơi media_store phát QR); từ chối IP literal + host lạ → chặn 169.254.169.254,
    127.0.0.1, dịch vụ nội bộ.
  * CẤM redirect (3xx → lỗi) — chống bounce sang host nội bộ sau khi qua allowlist.
  * cap 10MB enforce TRONG lúc stream (không tin ``Content-Length``) + timeout ngắn.
  * magic-byte sniff (dùng chung ``inbound_media.sniff_image_bytes``) → chắc là ảnh
    thật, không phải HTML/redirect body.

Pure aside from the injected ``http_open`` callable → unit-test không cần mạng.
"""
from __future__ import annotations

import io
import ipaddress
import urllib.error
import urllib.request
from typing import Callable, Iterable, Optional, Tuple
from urllib.parse import urlsplit

try:  # reuse the authoritative magic sniff (DRY with inbound contract)
    from .inbound_media import sniff_image_bytes  # type: ignore
except Exception:  # pragma: no cover - path-loaded in production
    from inbound_media import sniff_image_bytes

MAX_IMAGE_BYTES = 10 * 1024 * 1024   # khớp cap của _zalo_send_image_handler
DEFAULT_TIMEOUT = 20.0
DEFAULT_ALLOWED_HOSTS = ("mcp.tino.vn",)

# http_open(url, timeout) -> file-like response (.read(n), .close()).
# MUST NOT follow redirects. Default impl bên dưới; test inject fake.
HttpOpen = Callable[[str, float], object]


class ImageUrlError(Exception):
    """Bất kỳ lỗi validate/tải nào (fail closed)."""


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def validate_image_url(url: str, allowed_hosts: Iterable[str]) -> str:
    """Trả host đã validate hoặc raise ``ImageUrlError``.

    https-only, host ∈ allowlist (case-insensitive), không IP literal.
    """
    parts = urlsplit((url or "").strip())
    if parts.scheme != "https":
        raise ImageUrlError("chỉ chấp nhận link https")
    host = (parts.hostname or "").lower()
    if not host:
        raise ImageUrlError("URL thiếu host")
    if _is_ip_literal(host):
        raise ImageUrlError("không chấp nhận địa chỉ IP trực tiếp")
    allowed = {h.strip().lower() for h in allowed_hosts if h and h.strip()}
    if host not in allowed:
        raise ImageUrlError(f"host '{host}' không nằm trong danh sách cho phép")
    return host


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Chặn mọi redirect 3xx → SSRF không bounce được sang host nội bộ sau allowlist."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise ImageUrlError(f"từ chối redirect ({code})")


def _default_http_open(url: str, timeout: float):
    """Opener urllib TỪ CHỐI redirect (3xx → ImageUrlError) + KHÔNG dùng proxy.

    ProxyHandler({}) rỗng vô hiệu proxy env (http_proxy/https_proxy) → fetch đi
    THẲNG tới host đã allowlist (mcp.tino.vn, Hermes vốn nối thẳng), xác định +
    tránh route bất ngờ qua proxy.
    """
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirectHandler)
    req = urllib.request.Request(url, headers={"User-Agent": "hermes-zalo-plugin"})
    return opener.open(req, timeout=timeout)


def fetch_image_from_url(
    url: str,
    *,
    http_open: HttpOpen = _default_http_open,
    allowed_hosts: Iterable[str] = DEFAULT_ALLOWED_HOSTS,
    max_bytes: int = MAX_IMAGE_BYTES,
    timeout: float = DEFAULT_TIMEOUT,
) -> Tuple[bytes, str]:
    """Validate URL → stream-download ≤``max_bytes`` → magic sniff → ``(data, ext)``.

    ``ext`` có dấu chấm đầu (vd ``.png``) để khớp ``_prepare_file_send``.
    Raise ``ImageUrlError`` ở mọi nhánh lỗi (fail closed).
    """
    validate_image_url(url, allowed_hosts)
    try:
        resp = http_open(url.strip(), timeout)
    except ImageUrlError:
        raise
    except urllib.error.HTTPError as e:
        raise ImageUrlError(f"tải ảnh lỗi HTTP {e.code}")
    except Exception as e:  # noqa: BLE001 - mạng/parse → gộp về fail closed
        raise ImageUrlError(f"không tải được ảnh ({e})")

    try:
        buf = io.BytesIO()
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            buf.write(chunk)
            if buf.tell() > max_bytes:
                raise ImageUrlError("ảnh quá lớn (>10MB)")
        data = buf.getvalue()
    finally:
        try:
            resp.close()
        except Exception:  # pragma: no cover
            pass

    if not data:
        raise ImageUrlError("ảnh rỗng")
    hit = sniff_image_bytes(data[:32])
    if not hit:
        raise ImageUrlError("dữ liệu tải về không phải ảnh hợp lệ (PNG/JPEG/GIF/WebP)")
    _mime, ext = hit
    return data, "." + ext.lstrip(".")


def resolve_allowed_hosts(env_value: Optional[str]) -> Tuple[str, ...]:
    """Parse ``ZALO_IMAGE_URL_ALLOWED_HOSTS`` (phẩy ngăn cách) → tuple host.

    Trống/None → mặc định ``mcp.tino.vn``. Luôn có ít nhất default để không mở
    toang allowlist do config trống.
    """
    if not env_value or not env_value.strip():
        return DEFAULT_ALLOWED_HOSTS
    hosts = tuple(h.strip().lower() for h in env_value.split(",") if h.strip())
    return hosts or DEFAULT_ALLOWED_HOSTS
