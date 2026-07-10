"""Tests for landing_media_bridge.py — safe cached-image read, content-addressed
naming, URL validation and the fail-closed upload orchestration. No network."""

import os
import tempfile
import unittest

from landing_media_bridge import (
    BridgeConfig,
    BridgeError,
    LandingMediaBridge,
    content_addressed_name,
    load_bridge_config,
    read_cached_image,
    sanitize_stem,
)

JPEG = bytes([0xFF, 0xD8, 0xFF, 0xE0, 0, 0x10, 0x4A, 0x46, 0x49, 0x46, 0, 1]) + b"payload"
PNG = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0, 0, 0, 0x0D]) + b"payload"
PDF = b"%PDF-1.7\n%stuff and more bytes here"


class _Rec:
    def __init__(self, local_path, from_name="cust"):
        self.local_path = local_path
        self.from_name = from_name


class TestHelpers(unittest.TestCase):
    def test_sanitize_stem(self):
        self.assertEqual(sanitize_stem("Hero Image!.jpg"), "hero-image")
        self.assertEqual(sanitize_stem(""), "img")
        self.assertEqual(sanitize_stem("../../etc/passwd"), "passwd")

    def test_content_addressed_name(self):
        n = content_addressed_name("hero", "a1b2c3d4e5f6aaaa", "webp")
        self.assertEqual(n, "hero-a1b2c3d4e5f6.webp")


class TestReadCachedImage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _w(self, name, data):
        p = os.path.join(self.tmp, name)
        with open(p, "wb") as fh:
            fh.write(data)
        return p

    def test_reads_valid_image(self):
        p = self._w("a.jpg", JPEG)
        data, mime, ext = read_cached_image(self.tmp, p)
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(ext, "jpg")
        self.assertEqual(data, JPEG)

    def test_rejects_path_escape(self):
        with self.assertRaises(BridgeError):
            read_cached_image(self.tmp, "/etc/passwd")

    def test_rejects_symlink(self):
        target = self._w("real.jpg", JPEG)
        link = os.path.join(self.tmp, "link.jpg")
        os.symlink(target, link)
        with self.assertRaises(BridgeError):
            read_cached_image(self.tmp, link)

    def test_rejects_non_image(self):
        p = self._w("doc.pdf", PDF)
        with self.assertRaises(BridgeError):
            read_cached_image(self.tmp, p)

    def test_rejects_oversize(self):
        p = self._w("big.jpg", JPEG)
        with self.assertRaises(BridgeError):
            read_cached_image(self.tmp, p, max_bytes=4)


class TestLoadConfig(unittest.TestCase):
    def test_requires_https(self):
        with self.assertRaises(BridgeError):
            load_bridge_config({"TINO_LANDING_BRIDGE_URL": "http://x/y", "TINO_LANDING_BRIDGE_KEY": "k"}, "/c")

    def test_rejects_userinfo(self):
        with self.assertRaises(BridgeError):
            load_bridge_config({"TINO_LANDING_BRIDGE_URL": "https://u:p@x/y", "TINO_LANDING_BRIDGE_KEY": "k"}, "/c")

    def test_requires_key(self):
        with self.assertRaises(BridgeError):
            load_bridge_config({"TINO_LANDING_BRIDGE_URL": "https://x/y"}, "/c")

    def test_ok(self):
        cfg = load_bridge_config({"TINO_LANDING_BRIDGE_URL": "https://mcp.tino.vn/tools/landing_upload_image", "TINO_LANDING_BRIDGE_KEY": "k"}, "/c")
        self.assertEqual(cfg.key, "k")


class TestUploadRecent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.posted = []

    def _img(self, name, data=JPEG):
        p = os.path.join(self.tmp, name)
        with open(p, "wb") as fh:
            fh.write(data)
        return p

    def _bridge(self, recents, resolver_result, post_result):
        cfg = BridgeConfig(url="https://mcp.tino.vn/tools/landing_upload_image", key="secret-key", cache_dir=self.tmp)

        def http_post(url, headers, body):
            self.posted.append({"url": url, "headers": headers, "body": body})
            return post_result

        return LandingMediaBridge(
            cfg,
            recent_fn=lambda chat_id, n: recents[:n],
            session_resolver=lambda task_id: resolver_result,
            http_post=http_post,
        )

    def test_happy_path_returns_compact_and_hides_bytes(self):
        p = self._img("x.jpg")
        b = self._bridge(
            [_Rec(p)],
            {"chat_id": "chatA", "conv_id": "chatA"},
            {"image_url": "https://landingpage.tino.vn/my-slug/assets/hero-abc.jpg"},
        )
        out = b.upload_recent(task_id="sess-1", slug="my-slug", filename="hero")
        self.assertEqual(out["count"], 1)
        img = out["images"][0]
        self.assertTrue(img["image_url"].endswith(".jpg"))
        self.assertEqual(img["mime"], "image/jpeg")
        self.assertIn("size", img)
        # never leak sender/local path/base64 in the returned metadata
        self.assertNotIn("local_path", img)
        self.assertNotIn("image_base64", img)
        # the trusted conv id (== chat id) is sent as X-Session; key never in body
        sent = self.posted[0]
        self.assertEqual(sent["headers"]["X-Session"], "chatA")
        self.assertEqual(sent["headers"]["X-Agent-Key"], "secret-key")
        self.assertNotIn("secret-key", str(sent["body"]))
        # content-addressed filename
        self.assertRegex(sent["body"]["filename"], r"^hero-[0-9a-f]{12}\.jpg$")

    def test_fail_closed_when_session_unresolved(self):
        p = self._img("x.jpg")
        b = self._bridge([_Rec(p)], None, {"image_url": "https://x/y/assets/z.jpg"})
        with self.assertRaises(BridgeError):
            b.upload_recent(task_id="bad", slug="s")

    def test_fail_closed_when_no_recent_image(self):
        b = self._bridge([], {"chat_id": "c", "conv_id": "c"}, {})
        with self.assertRaises(BridgeError):
            b.upload_recent(task_id="s", slug="s")

    def test_mcp_error_message_surfaced(self):
        """A wrong slug (MCP not_found/forbidden) must surface the MCP's own
        message so the agent can self-correct — not a generic no-image_url."""
        p = self._img("x.jpg")
        b = self._bridge(
            [_Rec(p)],
            {"chat_id": "c", "conv_id": "c"},
            {"error": "not_found", "message": "landing 'ict-sai-gon' không tồn tại"},
        )
        with self.assertRaises(BridgeError) as cm:
            b.upload_recent(task_id="s", slug="ict-sai-gon")
        self.assertIn("upload rejected", str(cm.exception))
        self.assertIn("không tồn tại", str(cm.exception))

    def test_rejects_non_durable_url(self):
        p = self._img("x.jpg")
        b = self._bridge(
            [_Rec(p)],
            {"chat_id": "c", "conv_id": "c"},
            {"image_url": "https://mcp.tino.vn/media/tmp123.jpg"},  # temporary transport
        )
        with self.assertRaises(BridgeError):
            b.upload_recent(task_id="s", slug="s")

    def test_same_bytes_same_remote_name(self):
        p1 = self._img("a.jpg")
        p2 = self._img("b.jpg")  # identical bytes
        b = self._bridge(
            [_Rec(p1), _Rec(p2)],
            {"chat_id": "c", "conv_id": "c"},
            {"image_url": "https://x/s/assets/n.jpg"},
        )
        b.upload_recent(task_id="s", slug="s", count=2)
        names = [pp["body"]["filename"] for pp in self.posted]
        self.assertEqual(names[0], names[1])  # content-addressed → identical


if __name__ == "__main__":
    unittest.main()
