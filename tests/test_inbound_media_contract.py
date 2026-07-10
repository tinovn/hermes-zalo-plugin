"""Tests for inbound_media.py — magic sniffing, path safety, normalization and
the bounded RecentImageIndex. Pure stdlib unittest, no Hermes / Zalo needed."""

import os
import tempfile
import unittest

from inbound_media import (
    CANONICAL_SESSION_DIR,
    NormalizedMedia,
    RecentImage,
    RecentImageIndex,
    image_mime_for_ext,
    is_within_root,
    normalize_inbound_media,
    resolve_media_cache_dir,
    resolve_session_dir,
    sniff_image_bytes,
)

JPEG = bytes([0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46, 0x00, 0x01])
PNG = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0, 0, 0, 0x0D])
GIF = bytes([0x47, 0x49, 0x46, 0x38, 0x39, 0x61, 1, 0, 1, 0, 0x80, 0])
WEBP = b"RIFF" + (8).to_bytes(4, "little") + b"WEBP"
PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3 rest"


class TestSniff(unittest.TestCase):
    def test_detects_formats(self):
        self.assertEqual(sniff_image_bytes(JPEG), ("image/jpeg", "jpg"))
        self.assertEqual(sniff_image_bytes(PNG), ("image/png", "png"))
        self.assertEqual(sniff_image_bytes(GIF), ("image/gif", "gif"))
        self.assertEqual(sniff_image_bytes(WEBP), ("image/webp", "webp"))

    def test_rejects_non_image(self):
        self.assertIsNone(sniff_image_bytes(PDF))
        self.assertIsNone(sniff_image_bytes(b"\x00\x01"))
        self.assertIsNone(sniff_image_bytes(None))

    def test_mime_for_ext(self):
        self.assertEqual(image_mime_for_ext("JPG"), "image/jpeg")
        self.assertEqual(image_mime_for_ext(".webp"), "image/webp")
        self.assertIsNone(image_mime_for_ext("txt"))


class TestRoots(unittest.TestCase):
    def test_default_canonical(self):
        self.assertEqual(resolve_session_dir({}), CANONICAL_SESSION_DIR)
        self.assertEqual(resolve_session_dir({"ZALO_PERSONAL_SESSION_DIR": "  "}), CANONICAL_SESSION_DIR)
        self.assertEqual(resolve_session_dir({"ZALO_PERSONAL_SESSION_DIR": "/x"}), "/x")
        self.assertEqual(resolve_media_cache_dir({"ZALO_PERSONAL_SESSION_DIR": "/x"}), os.path.join("/x", "media-cache"))

    def test_within_root(self):
        with tempfile.TemporaryDirectory() as d:
            inside = os.path.join(d, "a.jpg")
            open(inside, "wb").close()
            self.assertTrue(is_within_root(d, inside))
            self.assertFalse(is_within_root(d, "/etc/passwd"))
            self.assertFalse(is_within_root(d, os.path.join(d, "..", "b.jpg")))


class TestNormalize(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _write(self, name, data):
        p = os.path.join(self.tmp, name)
        with open(p, "wb") as fh:
            fh.write(data)
        return p

    def test_native_image(self):
        p = self._write("x.jpg", JPEG + b"rest")
        m = normalize_inbound_media({"kind": "image", "local_path": p, "mime_type": "image/jpeg"}, self.tmp)
        self.assertTrue(m.is_image)
        self.assertEqual(m.kind, "image")
        self.assertEqual(m.mime_type, "image/jpeg")
        self.assertEqual(m.local_path, p)

    def test_image_as_file_promoted_by_magic(self):
        # A PNG delivered as a Zalo File (kind=file) is promoted to image.
        p = self._write("doc.bin", PNG + b"rest")
        m = normalize_inbound_media({"kind": "file", "local_path": p}, self.tmp)
        self.assertTrue(m.is_image)
        self.assertEqual(m.kind, "image")
        self.assertEqual(m.mime_type, "image/png")

    def test_non_image_document_stays_file(self):
        p = self._write("report.pdf", PDF)
        m = normalize_inbound_media({"kind": "file", "local_path": p, "mime_type": "application/pdf"}, self.tmp)
        self.assertFalse(m.is_image)
        self.assertEqual(m.kind, "file")

    def test_declared_image_failing_magic_is_downgraded(self):
        p = self._write("fake.jpg", PDF)
        m = normalize_inbound_media({"kind": "image", "local_path": p, "is_image": True}, self.tmp)
        self.assertFalse(m.is_image)
        self.assertEqual(m.reason, "not_image_bytes")

    def test_path_escape_rejected(self):
        m = normalize_inbound_media({"kind": "image", "local_path": "/etc/passwd"}, self.tmp)
        self.assertFalse(m.is_image)
        self.assertIsNone(m.local_path)
        self.assertEqual(m.reason, "path_escape")

    def test_missing_local_path(self):
        m = normalize_inbound_media({"kind": "image"}, self.tmp)
        self.assertEqual(m.reason, "no_local_path")

    def test_never_leaks_transport_url(self):
        p = self._write("x.jpg", JPEG)
        m = normalize_inbound_media(
            {"kind": "image", "local_path": p, "media_url": "/media/abc", "url": "https://zdn.vn/x"},
            self.tmp,
        )
        self.assertEqual(m.local_path, p)
        self.assertNotIn("/media/", m.local_path or "")
        self.assertFalse((m.local_path or "").startswith("http"))


class TestRecentImageIndex(unittest.TestCase):
    def _img(self, msg_id, ts, seq, path="/opt/data/zalo/media-cache/x.jpg"):
        return RecentImage(msg_id=msg_id, local_path=path, from_uid="u", from_name="n", event_ts=ts, ingress_seq=seq)

    def test_order_by_ts_seq_not_insertion(self):
        idx = RecentImageIndex(per_chat=5)
        idx.set_account("self1")
        # insert out of order: newer image (seq 2) added BEFORE older (seq 1)
        idx.add("chatA", self._img("m2", ts=100, seq=2))
        idx.add("chatA", self._img("m1", ts=100, seq=1))
        newest = idx.recent("chatA", count=1)
        self.assertEqual([i.msg_id for i in newest], ["m2"])
        both = idx.recent("chatA", count=2)
        self.assertEqual([i.msg_id for i in both], ["m1", "m2"])  # oldest→newest

    def test_dedupe_by_msg_id(self):
        idx = RecentImageIndex()
        idx.set_account("s")
        idx.add("c", self._img("dup", ts=1, seq=1))
        idx.add("c", self._img("dup", ts=1, seq=1))
        self.assertEqual(len(idx.recent("c", count=5)), 1)

    def test_keep_five_per_chat(self):
        idx = RecentImageIndex(per_chat=5)
        idx.set_account("s")
        for i in range(8):
            idx.add("c", self._img(f"m{i}", ts=i, seq=i))
        got = idx.recent("c", count=5)
        self.assertEqual([i.msg_id for i in got], ["m3", "m4", "m5", "m6", "m7"])

    def test_namespace_isolation_between_accounts(self):
        idx = RecentImageIndex()
        idx.set_account("acctA")
        idx.add("c", self._img("a", ts=1, seq=1))
        idx.set_account("acctB")  # account change clears
        self.assertEqual(idx.recent("c", count=5), [])

    def test_lru_evicts_whole_chats(self):
        idx = RecentImageIndex(per_chat=5, max_chats=2)
        idx.set_account("s")
        idx.add("c1", self._img("a", ts=1, seq=1))
        idx.add("c2", self._img("b", ts=1, seq=1))
        idx.add("c3", self._img("c", ts=1, seq=1))  # evicts c1 (LRU)
        self.assertEqual(idx.recent("c1", count=5), [])
        self.assertEqual(len(idx.recent("c3", count=5)), 1)
        self.assertEqual(idx.chat_count(), 2)

    def test_empty_after_clear(self):
        idx = RecentImageIndex()
        idx.set_account("s")
        idx.add("c", self._img("a", ts=1, seq=1))
        idx.clear()
        self.assertEqual(idx.recent("c", count=1), [])


if __name__ == "__main__":
    unittest.main()
