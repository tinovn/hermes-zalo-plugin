"""Rate-limit quota cho nhắc hẹn native khi non-owner tự đặt.

Chỉ test phần logic thuần trong rate_limit.py — đủ chứng minh cơ chế chống spam
đúng ngưỡng per-chat / per-user và tự reset sau 1 giờ. Cổng before_tool_call
đọc sessions.json từ đĩa (cần Hermes) nên không test ở đây.

unittest stdlib — chạy không cần Hermes/Zalo (xem README).
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import rate_limit

_MSGS = ("chat quá", "user quá")


class TestReminderQuota(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "reminder_create_state.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_allows_until_per_user_limit(self):
        # 3 lần đầu OK cho cùng 1 user, lần thứ 4 chặn (per_user=3).
        for _ in range(3):
            self.assertIsNone(
                rate_limit.check(self.path, "chatA", "userX", 6, 3, *_MSGS)
            )
            rate_limit.bump(self.path, "chatA", "userX")
        self.assertEqual(
            rate_limit.check(self.path, "chatA", "userX", 6, 3, *_MSGS), "user quá"
        )

    def test_per_chat_limit_across_users(self):
        # 6 người khác nhau, mỗi người 1 nhắc → chạm ngưỡng per_chat=6.
        for i in range(6):
            self.assertIsNone(
                rate_limit.check(self.path, "chatB", f"user{i}", 6, 3, *_MSGS)
            )
            rate_limit.bump(self.path, "chatB", f"user{i}")
        # Người thứ 7 bị chặn vì QUOTA CHAT, dù bản thân chưa đặt lần nào.
        self.assertEqual(
            rate_limit.check(self.path, "chatB", "user7", 6, 3, *_MSGS), "chat quá"
        )

    def test_window_resets_after_one_hour(self):
        base = 1_000_000.0
        with mock.patch.object(rate_limit.time, "time", return_value=base):
            for _ in range(3):
                rate_limit.bump(self.path, "chatC", "userY")
            self.assertEqual(
                rate_limit.check(self.path, "chatC", "userY", 6, 3, *_MSGS), "user quá"
            )
        # +3601s: cửa sổ cũ hết hạn → cho phép lại.
        with mock.patch.object(rate_limit.time, "time", return_value=base + 3601):
            self.assertIsNone(
                rate_limit.check(self.path, "chatC", "userY", 6, 3, *_MSGS)
            )

    def test_missing_state_file_allows(self):
        # Chưa có file state → không chặn.
        self.assertIsNone(
            rate_limit.check(self.path, "chatD", "userZ", 6, 3, *_MSGS)
        )


if __name__ == "__main__":
    unittest.main()
