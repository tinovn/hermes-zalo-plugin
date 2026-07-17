"""Tests cho image_url_fetch.py — SSRF-guarded image fetch.

Pure stdlib unittest, KHÔNG mạng: http_open được inject bằng fake. Bao phủ:
validate (https-only, allowlist, IP literal), stream cap, sniff, reject redirect,
resolve_allowed_hosts.
"""
import io
import unittest

from image_url_fetch import (
    DEFAULT_ALLOWED_HOSTS,
    ImageUrlError,
    fetch_image_from_url,
    resolve_allowed_hosts,
    validate_image_url,
)

# Ảnh thật tối thiểu theo magic bytes (inbound_media.sniff_image_bytes cần ≥12 byte).
PNG = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) + b"\x00" * 24
JPEG = bytes([0xFF, 0xD8, 0xFF, 0xE0, 0, 0x10, 0x4A, 0x46, 0x49, 0x46, 0, 1]) + b"payload-bytes"
NOT_IMAGE = b"<html>redirected login page</html>" + b"\x00" * 8


class _FakeResp:
    """File-like response trả sẵn bytes theo từng chunk."""

    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)

    def read(self, n: int) -> bytes:
        return self._buf.read(n)

    def close(self):
        pass


def _opener(data: bytes):
    def _open(url, timeout):
        return _FakeResp(data)
    return _open


def _raising_opener(exc):
    def _open(url, timeout):
        raise exc
    return _open


class TestValidateUrl(unittest.TestCase):
    def test_https_mcp_ok(self):
        self.assertEqual(
            validate_image_url("https://mcp.tino.vn/media/abc.png", DEFAULT_ALLOWED_HOSTS),
            "mcp.tino.vn",
        )

    def test_reject_http(self):
        with self.assertRaises(ImageUrlError):
            validate_image_url("http://mcp.tino.vn/media/abc.png", DEFAULT_ALLOWED_HOSTS)

    def test_reject_other_host(self):
        with self.assertRaises(ImageUrlError):
            validate_image_url("https://evil.com/x.png", DEFAULT_ALLOWED_HOSTS)

    def test_reject_ip_literal_metadata(self):
        with self.assertRaises(ImageUrlError):
            validate_image_url("https://169.254.169.254/latest/meta-data", DEFAULT_ALLOWED_HOSTS)

    def test_reject_loopback_ip(self):
        with self.assertRaises(ImageUrlError):
            validate_image_url("https://127.0.0.1/x.png", ("127.0.0.1",))

    def test_host_case_insensitive(self):
        self.assertEqual(
            validate_image_url("https://MCP.Tino.VN/media/abc.png", DEFAULT_ALLOWED_HOSTS),
            "mcp.tino.vn",
        )


class TestFetch(unittest.TestCase):
    def test_happy_png(self):
        data, ext = fetch_image_from_url(
            "https://mcp.tino.vn/media/abc.png", http_open=_opener(PNG)
        )
        self.assertEqual(data, PNG)
        self.assertEqual(ext, ".png")

    def test_happy_jpeg(self):
        data, ext = fetch_image_from_url(
            "https://mcp.tino.vn/media/abc.jpg", http_open=_opener(JPEG)
        )
        self.assertEqual(ext, ".jpg")

    def test_reject_non_image_body(self):
        with self.assertRaises(ImageUrlError):
            fetch_image_from_url(
                "https://mcp.tino.vn/media/x.png", http_open=_opener(NOT_IMAGE)
            )

    def test_reject_oversize_streamed(self):
        big = PNG + b"\x00" * (200)
        with self.assertRaises(ImageUrlError):
            fetch_image_from_url(
                "https://mcp.tino.vn/media/big.png",
                http_open=_opener(big),
                max_bytes=64,
            )

    def test_reject_empty(self):
        with self.assertRaises(ImageUrlError):
            fetch_image_from_url(
                "https://mcp.tino.vn/media/empty.png", http_open=_opener(b"")
            )

    def test_validate_runs_before_open(self):
        # host lạ → raise trước khi gọi opener (opener raise nếu bị gọi)
        called = {"n": 0}

        def _open(url, timeout):
            called["n"] += 1
            return _FakeResp(PNG)

        with self.assertRaises(ImageUrlError):
            fetch_image_from_url("https://evil.com/x.png", http_open=_open)
        self.assertEqual(called["n"], 0)

    def test_redirect_error_propagates(self):
        with self.assertRaises(ImageUrlError):
            fetch_image_from_url(
                "https://mcp.tino.vn/media/x.png",
                http_open=_raising_opener(ImageUrlError("từ chối redirect (302)")),
            )

    def test_network_error_wrapped(self):
        with self.assertRaises(ImageUrlError):
            fetch_image_from_url(
                "https://mcp.tino.vn/media/x.png",
                http_open=_raising_opener(OSError("connection reset")),
            )


class TestDefaultOpenerRedirectBlock(unittest.TestCase):
    """Nhánh production quan trọng nhất: chặn 3xx (SSRF-qua-redirect).

    Test THẲNG _NoRedirectHandler.redirect_request (cơ chế thật) — không mạng."""

    def test_no_redirect_handler_raises_on_3xx(self):
        import urllib.request

        from image_url_fetch import ImageUrlError, _NoRedirectHandler

        h = _NoRedirectHandler()
        req = urllib.request.Request("https://mcp.tino.vn/media/x.png")
        with self.assertRaises(ImageUrlError):
            h.redirect_request(req, None, 302, "Found", {}, "https://evil.com/x.png")

    def test_default_opener_wires_no_redirect_handler(self):
        import urllib.request

        from image_url_fetch import _NoRedirectHandler, _default_http_open

        # Dựng opener như production, xác nhận handler redirect là _NoRedirectHandler.
        captured = {}
        orig_open = urllib.request.OpenerDirector.open

        def _spy_open(self, *a, **k):
            for hdlr in self.handlers:
                if isinstance(hdlr, urllib.request.HTTPRedirectHandler):
                    captured["h"] = hdlr
            raise OSError("blocked network in test")

        urllib.request.OpenerDirector.open = _spy_open
        try:
            try:
                _default_http_open("https://mcp.tino.vn/media/x.png", 0.001)
            except OSError:
                pass
        finally:
            urllib.request.OpenerDirector.open = orig_open

        self.assertIsInstance(captured.get("h"), _NoRedirectHandler)


class TestResolveAllowedHosts(unittest.TestCase):
    def test_none_defaults(self):
        self.assertEqual(resolve_allowed_hosts(None), DEFAULT_ALLOWED_HOSTS)

    def test_empty_defaults(self):
        self.assertEqual(resolve_allowed_hosts("   "), DEFAULT_ALLOWED_HOSTS)

    def test_csv_parsed(self):
        self.assertEqual(
            resolve_allowed_hosts("mcp.tino.vn, cdn.tino.vn"),
            ("mcp.tino.vn", "cdn.tino.vn"),
        )


if __name__ == "__main__":
    unittest.main()
