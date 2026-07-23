"""Scheduler cho nhắc động non-owner: tính fire_times (escalation), due, advance,
overdue, persist/restart. Logic thuần + IO → test không cần gateway.

unittest stdlib — chạy không cần Hermes/Zalo (xem README).
"""
import tempfile
import unittest
from pathlib import Path

import reminder_scheduler as rs


def _rec(rid="r1", fire_times=None, next_idx=0, **extra):
    base = {
        "id": rid,
        "chat_id": "g1",
        "thread_type": "group",
        "task": "nộp bài",
        "target_display": "Trân",
        "fire_times": fire_times if fire_times is not None else [100.0],
        "next_idx": next_idx,
        "max_attempts": len(fire_times) if fire_times else 1,
    }
    base.update(extra)
    return base


class TestComputeFireTimes(unittest.TestCase):
    def test_no_deadline_single_fire(self):
        self.assertEqual(rs.compute_fire_times(100.0, None, 3), [100.0])

    def test_max_attempts_one(self):
        self.assertEqual(rs.compute_fire_times(100.0, 400.0, 1), [100.0])

    def test_deadline_three_attempts_even_spacing(self):
        # start=100, deadline=400, 3 mốc: [100, 250, 400]
        self.assertEqual(rs.compute_fire_times(100.0, 400.0, 3), [100.0, 250.0, 400.0])

    def test_deadline_before_start_falls_back_single(self):
        # hạn <= giờ đặt → vô nghĩa, chỉ 1 fire tại start
        self.assertEqual(rs.compute_fire_times(400.0, 300.0, 3), [400.0])


class TestDueAndAdvance(unittest.TestCase):
    def test_current_fire_time(self):
        self.assertEqual(rs.current_fire_time(_rec(fire_times=[100.0, 200.0], next_idx=1)), 200.0)

    def test_current_fire_time_exhausted(self):
        self.assertIsNone(rs.current_fire_time(_rec(fire_times=[100.0], next_idx=1)))

    def test_due_returns_when_reached(self):
        state = {"reminders": {"r1": _rec(fire_times=[100.0])}}
        self.assertEqual([r["id"] for r in rs.due(state, 100.0)], ["r1"])
        self.assertEqual([r["id"] for r in rs.due(state, 99.0)], [])

    def test_done_not_due(self):
        state = {"reminders": {"r1": _rec(fire_times=[100.0], next_idx=1)}}
        self.assertEqual(rs.due(state, 999.0), [])

    def test_advance_increments_and_finishes(self):
        rec = _rec(fire_times=[100.0, 200.0])
        rs.advance_rec(rec)
        self.assertEqual(rec["next_idx"], 1)
        self.assertFalse(rs.is_done(rec))
        rs.advance_rec(rec)
        self.assertTrue(rs.is_done(rec))


class TestOverdue(unittest.TestCase):
    def test_within_grace_not_overdue(self):
        rec = _rec(fire_times=[100.0])
        self.assertFalse(rs.is_overdue(rec, now=100.0 + 60, grace=1800))

    def test_beyond_grace_overdue(self):
        rec = _rec(fire_times=[100.0])
        self.assertTrue(rs.is_overdue(rec, now=100.0 + 1801, grace=1800))


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "reminder_schedule.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_empty(self):
        self.assertEqual(rs.load(self.path), {"reminders": {}})

    def test_add_then_reload(self):
        rs.add(self.path, _rec("rX", fire_times=[100.0, 250.0, 400.0]))
        state = rs.load(self.path)
        self.assertIn("rX", state["reminders"])
        self.assertEqual(state["reminders"]["rX"]["fire_times"], [100.0, 250.0, 400.0])

    def test_advance_persisted(self):
        rs.add(self.path, _rec("rX", fire_times=[100.0, 200.0]))
        rs.advance(self.path, "rX")
        self.assertEqual(rs.load(self.path)["reminders"]["rX"]["next_idx"], 1)

    def test_remove(self):
        rs.add(self.path, _rec("rX"))
        rs.remove(self.path, "rX")
        self.assertNotIn("rX", rs.load(self.path)["reminders"])

    def test_corrupt_file_safe(self):
        self.path.write_text("{ broken", encoding="utf-8")
        self.assertEqual(rs.load(self.path), {"reminders": {}})


if __name__ == "__main__":
    unittest.main()
