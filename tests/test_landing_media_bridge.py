"""Tests for landing_media_bridge.py — safe cached-image read, content-addressed
naming, URL validation and the fail-closed upload orchestration. No network."""

import base64
import io
import os
import tempfile
import unittest

from landing_media_bridge import (
    MAX_IMAGE_BYTES,
    BridgeConfig,
    BridgeError,
    LandingMediaBridge,
    content_addressed_name,
    load_bridge_config,
    read_cached_image,
    sanitize_stem,
)

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:  # pragma: no cover - CI always has Pillow
    HAS_PIL = False

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
    _OK_ENV = {"TINO_LANDING_BRIDGE_URL": "https://mcp.tino.vn/tools/landing_upload_image",
               "TINO_LANDING_BRIDGE_KEY": "k"}

    def test_requires_https(self):
        with self.assertRaises(BridgeError):
            load_bridge_config({"TINO_LANDING_BRIDGE_URL": "http://x/y", "TINO_LANDING_BRIDGE_KEY": "k"}, "/c")

    def test_max_dim_default_1024(self):
        cfg = load_bridge_config(dict(self._OK_ENV), "/c")
        self.assertEqual(cfg.max_dim, 1024)

    def test_max_dim_env_override(self):
        cfg = load_bridge_config({**self._OK_ENV, "TINO_LANDING_IMAGE_MAX_DIM": "1600"}, "/c")
        self.assertEqual(cfg.max_dim, 1600)

    def test_max_dim_bad_values_fall_back(self):
        for bad in ("banana", "0", "-5", "99999"):
            cfg = load_bridge_config({**self._OK_ENV, "TINO_LANDING_IMAGE_MAX_DIM": bad}, "/c")
            self.assertEqual(cfg.max_dim, 1024, bad)

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

    def test_oversize_undecodable_source_rejected(self):
        # Valid JPEG magic but not decodable → resize passes through → the
        # post-resize cap re-imposes the old 6MB rule.
        p = self._img("big.jpg", JPEG + b"\x00" * (MAX_IMAGE_BYTES + 1))
        b = self._bridge([_Rec(p)], {"chat_id": "c", "conv_id": "c"}, {})
        with self.assertRaises(BridgeError) as cm:
            b.upload_recent(task_id="s", slug="s")
        self.assertIn("too large", str(cm.exception))
        self.assertEqual(self.posted, [])  # nothing left the process


@unittest.skipUnless(HAS_PIL, "Pillow required")
class TestUploadResizesRealImages(unittest.TestCase):
    """The bridge must downscale real photos to max_dim before upload."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.posted = []

    def _write_jpeg(self, name, size, quality=85, noise=False):
        if noise:  # random noise defeats JPEG compression → huge file
            img = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))
        else:
            img = Image.new("RGB", size, (12, 120, 200))
        p = os.path.join(self.tmp, name)
        img.save(p, format="JPEG", quality=quality)
        return p

    def _bridge(self, recents, max_dim=1024):
        cfg = BridgeConfig(url="https://mcp.tino.vn/tools/landing_upload_image",
                           key="secret-key", cache_dir=self.tmp, max_dim=max_dim)

        def http_post(url, headers, body):
            self.posted.append({"url": url, "headers": headers, "body": body})
            return {"image_url": "https://x/s/assets/n.jpg"}

        return LandingMediaBridge(
            cfg,
            recent_fn=lambda chat_id, n: recents[:n],
            session_resolver=lambda task_id: {"chat_id": "c", "conv_id": "c"},
            http_post=http_post,
        )

    def _posted_image(self):
        b64 = self.posted[0]["body"]["image_base64"].split(",", 1)[1]
        return Image.open(io.BytesIO(base64.b64decode(b64)))

    def test_uploaded_bytes_are_downscaled(self):
        p = self._write_jpeg("photo.jpg", (4000, 3000))
        out = self._bridge([_Rec(p)]).upload_recent(task_id="s", slug="s")
        with self._posted_image() as sent:
            self.assertEqual(sent.size, (1024, 768))
        img = out["images"][0]
        self.assertEqual((img["width"], img["height"]), (1024, 768))
        self.assertEqual(img["mime"], "image/jpeg")

    def test_source_over_6mb_now_uploads(self):
        # The pre-resize bridge hard-failed any source >6MB; a downscaled
        # phone photo must now go through and land under the server cap.
        p = self._write_jpeg("huge.jpg", (2600, 2600), quality=98, noise=True)
        src_size = os.path.getsize(p)
        self.assertGreater(src_size, MAX_IMAGE_BYTES)  # test premise
        self._bridge([_Rec(p)]).upload_recent(task_id="s", slug="s")
        sent_bytes = base64.b64decode(
            self.posted[0]["body"]["image_base64"].split(",", 1)[1])
        self.assertLessEqual(len(sent_bytes), MAX_IMAGE_BYTES)
        with self._posted_image() as sent:
            self.assertLessEqual(max(sent.size), 1024)

    def test_small_image_uploaded_byte_identical(self):
        p = self._write_jpeg("small.jpg", (640, 480))
        with open(p, "rb") as fh:
            src = fh.read()
        self._bridge([_Rec(p)]).upload_recent(task_id="s", slug="s")
        sent_bytes = base64.b64decode(
            self.posted[0]["body"]["image_base64"].split(",", 1)[1])
        self.assertEqual(sent_bytes, src)  # no needless re-encode

    def test_custom_max_dim_from_config(self):
        p = self._write_jpeg("photo.jpg", (4000, 3000))
        self._bridge([_Rec(p)], max_dim=512).upload_recent(task_id="s", slug="s")
        with self._posted_image() as sent:
            self.assertEqual(sent.size, (512, 384))


if __name__ == "__main__":
    unittest.main()
