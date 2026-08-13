"""Tests for the adapter's non-owner scrub layer (``_scrub_outgoing``).

``adapter.py`` cannot be imported here — it pulls in ``gateway.*``, which only
exists inside a Hermes install. Every regex and helper under test is defined
ABOVE that import, so we exec the module prefix up to it and get the REAL
functions (not copies that can drift from the source).
"""

import os
import re
import unittest

_ADAPTER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "adapter.py")
_CUTOFF = "from gateway.platforms.base import"


def _load_adapter_prefix():
    with open(_ADAPTER, encoding="utf-8") as f:
        src = f.read()
    idx = src.index(_CUTOFF)
    ns: dict = {"__name__": "adapter_prefix"}
    exec(compile(src[:idx], _ADAPTER, "exec"), ns)
    return ns


_NS = _load_adapter_prefix()
_scrub_outgoing = _NS["_scrub_outgoing"]


class TestLeakedLifecycleStatus(unittest.TestCase):
    """The "✓ Context compaction complete" class of leak (seen in a group)."""

    LEAKED = [
        "✓ Context compaction complete — continuing turn...",
        "⚡ Interrupting current task. I'll respond to your message shortly.",
        "🗜️ Compacting context — summarizing earlier conversation so I can continue...",
        "📦 Pre-API compression: ~123,456 tokens near the context/output limit.",
        "💤 Resumed after 3600s idle — compacting ~120,000 tokens before continuing.",
        "⏱️ Rate limited. Waiting 30.0s (attempt 2/3)...",
        "↻ Empty response after tool calls — using earlier content as final answer",
    ]

    def test_dropped_for_non_owner(self):
        for t in self.LEAKED:
            self.assertIsNone(_scrub_outgoing(t), t)


class TestLegitimateRepliesSurvive(unittest.TestCase):
    """Regression guard: the check-mark/lightning glyphs are also used by real
    Vietnamese answers, so the glyph alone must not drop the message."""

    KEPT = [
        "✔ Đã đặt lịch cho anh lúc 15h nha.",
        "✓ Em đã lưu đơn của anh rồi ạ.",
        "☑ Đơn hàng đã được xác nhận ạ.",
        "⚡ Đơn của anh đang giao, chiều nay tới nha.",
        "✅ Dạ xong rồi ạ.",
        "🎉 Chúc mừng anh, đăng ký thành công!",
        "Dạ shop mở cửa 8h-22h mỗi ngày ạ.",
        "Dạ combo 2 người là 350k ạ.",
    ]

    def test_kept_for_non_owner(self):
        for t in self.KEPT:
            self.assertEqual(_scrub_outgoing(t), t, t)

    def test_english_only_variant_still_dropped(self):
        # Same glyphs, but no Vietnamese diacritic → raw runtime output.
        self.assertIsNone(_scrub_outgoing("✔ Session ready — continuing turn"))
        self.assertIsNone(_scrub_outgoing("⚡ Interrupting current task"))


class TestClassifierCallSites(unittest.TestCase):
    """``classify`` defaults to the strict non-owner tier, so a call site that
    forgets ``is_owner`` would silently hide diagnostics from the owner DM too.
    The send paths live on the adapter class (un-importable here), so pin the
    invariant at source level instead."""

    def test_every_call_site_passes_is_owner(self):
        with open(_ADAPTER, encoding="utf-8") as f:
            src = f.read()
        calls = re.findall(r"_classify_outbound\((.*?)\)\n", src)
        self.assertGreaterEqual(len(calls), 2, "call sites disappeared — update this test")
        for args in calls:
            self.assertIn("is_owner=", args, f"_classify_outbound({args}) misses is_owner")


class TestBrandRedaction(unittest.TestCase):
    """Unchanged behaviour — pinned so the scrub edits above stay scoped."""

    def test_vendor_names_swapped(self):
        out = _scrub_outgoing("Dạ em chạy trên Hermes với GPT-5 ạ.")
        self.assertIsNotNone(out)
        self.assertNotIn("Hermes", out)
        self.assertNotIn("GPT-5", out)
        self.assertIn("trợ lý", out)

    def test_hostbill_becomes_tino(self):
        out = _scrub_outgoing("Dạ anh vào HostBill để xem hóa đơn nha.")
        self.assertIn("Tino", out)


if __name__ == "__main__":
    unittest.main()
