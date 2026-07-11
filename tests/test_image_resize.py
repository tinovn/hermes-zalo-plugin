"""Tests for image_resize.py — bounded downscale with a fail-open contract.

Real Pillow images exercise the happy paths; the failure paths must return the
ORIGINAL bytes untouched (never raise), because the bridge relies on that to
degrade to the old 6MB-cap behavior.
"""

import io
import sys
import unittest
from unittest import mock

from image_resize import resize_image_to_max_dim

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:  # pragma: no cover - CI always has Pillow
    HAS_PIL = False


def _make(fmt, size=(2048, 1536), mode="RGB", **save_kw):
    img = Image.new(mode, size, (200, 30, 30) if mode == "RGB" else None)
    buf = io.BytesIO()
    img.save(buf, format=fmt, **save_kw)
    return buf.getvalue()


def _dims(data):
    with Image.open(io.BytesIO(data)) as img:
        return img.size, img.format, img.mode


@unittest.skipUnless(HAS_PIL, "Pillow required")
class TestResizeHappyPath(unittest.TestCase):
    def test_large_jpeg_landscape(self):
        rr = resize_image_to_max_dim(_make("JPEG"), mime="image/jpeg", ext="jpg")
        self.assertTrue(rr.resized)
        self.assertIsNone(rr.reason)
        (w, h), fmt, _ = _dims(rr.data)
        self.assertEqual((w, h), (1024, 768))  # aspect preserved
        self.assertEqual((rr.width, rr.height), (1024, 768))
        self.assertEqual(fmt, "JPEG")
        self.assertEqual((rr.mime, rr.ext), ("image/jpeg", "jpg"))

    def test_large_jpeg_portrait(self):
        rr = resize_image_to_max_dim(
            _make("JPEG", size=(1536, 2048)), mime="image/jpeg", ext="jpg")
        (w, h), _, _ = _dims(rr.data)
        self.assertEqual((w, h), (768, 1024))

    def test_small_image_returned_byte_identical(self):
        src = _make("JPEG", size=(800, 600))
        rr = resize_image_to_max_dim(src, mime="image/jpeg", ext="jpg")
        self.assertFalse(rr.resized)
        self.assertEqual(rr.reason, "already_small")
        self.assertIs(rr.data, src)  # no lossy re-encode of a small photo
        self.assertEqual((rr.width, rr.height), (800, 600))

    def test_png_alpha_preserved(self):
        rr = resize_image_to_max_dim(
            _make("PNG", size=(3000, 1000), mode="RGBA"),
            mime="image/png", ext="png")
        self.assertTrue(rr.resized)
        (w, h), fmt, pmode = _dims(rr.data)
        self.assertEqual((w, h), (1024, 341))
        self.assertEqual(fmt, "PNG")
        self.assertEqual(pmode, "RGBA")

    def test_webp_stays_webp(self):
        rr = resize_image_to_max_dim(
            _make("WEBP", size=(2000, 2000)), mime="image/webp", ext="webp")
        self.assertTrue(rr.resized)
        (_, _), fmt, _ = _dims(rr.data)
        self.assertEqual(fmt, "WEBP")

    def test_custom_max_dim(self):
        rr = resize_image_to_max_dim(
            _make("JPEG"), mime="image/jpeg", ext="jpg", max_dim=512)
        (w, h), _, _ = _dims(rr.data)
        self.assertEqual((w, h), (512, 384))

    def test_exif_orientation_baked_in(self):
        # Orientation 6 (rotate 90 CW to display): a 2048x1024 sensor frame is
        # really a portrait photo — the output must be portrait WITHOUT relying
        # on EXIF (which the re-encode drops).
        img = Image.new("RGB", (2048, 1024), (10, 20, 30))
        exif = Image.Exif()
        exif[0x0112] = 6
        buf = io.BytesIO()
        img.save(buf, format="JPEG", exif=exif)
        rr = resize_image_to_max_dim(buf.getvalue(), mime="image/jpeg", ext="jpg")
        self.assertTrue(rr.resized)
        (w, h), _, _ = _dims(rr.data)
        self.assertEqual((w, h), (512, 1024))


@unittest.skipUnless(HAS_PIL, "Pillow required")
class TestResizePassthrough(unittest.TestCase):
    def test_animated_gif_untouched(self):
        frames = [Image.new("P", (2000, 2000), i) for i in (0, 128)]
        buf = io.BytesIO()
        frames[0].save(buf, format="GIF", save_all=True,
                       append_images=frames[1:], duration=100)
        src = buf.getvalue()
        rr = resize_image_to_max_dim(src, mime="image/gif", ext="gif")
        self.assertFalse(rr.resized)
        self.assertEqual(rr.reason, "animated")
        self.assertIs(rr.data, src)

    def test_garbage_bytes_untouched(self):
        src = b"\xff\xd8\xff" + b"not really a jpeg"
        rr = resize_image_to_max_dim(src, mime="image/jpeg", ext="jpg")
        self.assertFalse(rr.resized)
        self.assertEqual(rr.reason, "decode_failed")
        self.assertIs(rr.data, src)

    def test_unsupported_ext_untouched(self):
        rr = resize_image_to_max_dim(b"BM...", mime="image/bmp", ext="bmp")
        self.assertEqual(rr.reason, "unsupported_format")

    def test_non_positive_max_dim_disables(self):
        src = _make("JPEG")
        rr = resize_image_to_max_dim(src, mime="image/jpeg", ext="jpg", max_dim=0)
        self.assertEqual(rr.reason, "disabled")
        self.assertIs(rr.data, src)

    def test_bad_max_dim_untouched(self):
        rr = resize_image_to_max_dim(
            _make("JPEG"), mime="image/jpeg", ext="jpg", max_dim="huge")
        self.assertEqual(rr.reason, "bad_max_dim")


class TestPillowUnavailable(unittest.TestCase):
    def test_missing_pillow_passes_original_through(self):
        src = b"\xff\xd8\xff" + b"x" * 64
        with mock.patch.dict(sys.modules, {"PIL": None}):
            rr = resize_image_to_max_dim(src, mime="image/jpeg", ext="jpg")
        self.assertFalse(rr.resized)
        self.assertEqual(rr.reason, "pillow_unavailable")
        self.assertIs(rr.data, src)
        self.assertEqual((rr.mime, rr.ext), ("image/jpeg", "jpg"))


if __name__ == "__main__":
    unittest.main()
