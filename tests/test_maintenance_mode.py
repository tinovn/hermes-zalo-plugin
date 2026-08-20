"""Mode bảo trì: cờ persist + câu thông báo + lệnh owner `/bot baotri`.

Khi owner bật bảo trì, tin của khách KHÔNG được đẩy qua agent nữa — adapter
tự trả một câu thông báo (rate-limit 1 lần / chat / 15 phút). Câu mặc định cố
ý không gắn tên/nhân xưng riêng của bot; persona ghi đè qua
``bot_persona.json → notices.maintenance``, owner ghi đè bằng
``/bot baotri <câu>``.

``adapter.py`` import ``gateway.*`` (không có ngoài bản cài Hermes) nên phần
module-level được exec tới ngay trước import đó, còn ``_handle_owner_command``
được lift bằng ``ast`` và gắn vào một stub adapter.
"""

import ast
import importlib.util
import json
import os
import sys
import tempfile
import textwrap
import unittest
from typing import Dict, Optional

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ADAPTER = os.path.join(_ROOT, "adapter.py")


def _source() -> str:
    with open(_ADAPTER, encoding="utf-8") as f:
        return f.read()


def _load_message_filtering():
    spec = importlib.util.spec_from_file_location(
        "zalo_message_filtering_test", os.path.join(_ROOT, "message_filtering.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclass() cần module có trong sys.modules
    spec.loader.exec_module(mod)
    return mod


def _module_ns() -> dict:
    """Exec phần module-level của adapter.py tới ngay trước import gateway."""
    src = _source()
    head = src.split("from gateway.platforms.base import")[0]
    ns: dict = {"__name__": "zalo_adapter_head_test"}
    exec(compile(head, _ADAPTER, "exec"), ns)
    mf = _load_message_filtering()
    # Hai hook này định nghĩa SAU import gateway trong file thật.
    ns["_classify_outbound"] = mf.classify
    ns["_FilterAction"] = mf.FilterAction
    ns["_persona_notice"] = lambda key, default: default
    return ns


_NS = _module_ns()


def _lift(name: str, ns: dict):
    src = _source()
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    )
    exec(compile(textwrap.dedent(ast.get_source_segment(src, fn)), _ADAPTER, "exec"), ns)
    return ns[name]


class _Bot:
    """Stub adapter mang đúng những thuộc tính `_handle_owner_command` đọc."""

    _handle_owner_command = staticmethod(None)  # gán ở dưới

    def __init__(self):
        self._maint_notified: Dict[str, float] = {}

    def _owner_command_help(self) -> str:
        return "HELP"


_Bot._handle_owner_command = _lift("_handle_owner_command", _NS)
# `mode`/`digest` không thuộc phạm vi test này — stub cho đủ global.
_NS.setdefault("_get_chat_setting", lambda *a, **k: "default")
_NS.setdefault("_set_chat_setting", lambda *a, **k: None)
_NS.setdefault("_VALID_CHAT_MODES", {"default"})


class MaintenanceStateTest(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("ZALO_PERSONAL_SESSION_DIR")
        self._dir = tempfile.mkdtemp()
        os.environ["ZALO_PERSONAL_SESSION_DIR"] = self._dir

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("ZALO_PERSONAL_SESSION_DIR", None)
        else:
            os.environ["ZALO_PERSONAL_SESSION_DIR"] = self._prev

    def test_default_is_off_when_no_file(self):
        self.assertFalse(_NS["_get_maintenance"]().get("enabled"))

    def test_set_then_get_roundtrip(self):
        self.assertTrue(_NS["_set_maintenance"](True, "Bên em bảo trì tới 15h30 ạ"))
        state = _NS["_get_maintenance"]()
        self.assertTrue(state["enabled"])
        self.assertEqual(state["message"], "Bên em bảo trì tới 15h30 ạ")
        with open(os.path.join(self._dir, "maintenance.json"), encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertTrue(on_disk["enabled"])

    def test_corrupt_file_fails_open(self):
        with open(os.path.join(self._dir, "maintenance.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertFalse(_NS["_get_maintenance"]().get("enabled"))

    def test_custom_message_wins_over_default(self):
        _NS["_set_maintenance"](True, "Bên em bảo trì tới 15h30 ạ")
        self.assertEqual(_NS["_maintenance_message"](), "Bên em bảo trì tới 15h30 ạ")
        _NS["_set_maintenance"](True, "")
        self.assertEqual(_NS["_maintenance_message"](), _NS["_MAINT_DEFAULT_MSG"])


class MaintenanceMessageTest(unittest.TestCase):
    def test_default_message_survives_outbound_filter(self):
        # Câu bảo trì đi thẳng ra khách, không qua LLM — nếu bộ lọc drop nó thì
        # khách im lặng tuyệt đối, đúng lỗi mà mode này sinh ra để tránh.
        self.assertTrue(_NS["_maint_message_deliverable"](_NS["_MAINT_DEFAULT_MSG"]))

    def test_default_message_is_not_personalised(self):
        low = _NS["_MAINT_DEFAULT_MSG"].lower()
        for name in ("ông bụt", "hermes", "codex", "gpt"):
            self.assertNotIn(name, low)

    def test_status_emoji_lead_is_rejected(self):
        self.assertFalse(_NS["_maint_message_deliverable"]("🔧 Bảo trì tới 15h30 nhé"))

    def test_plain_vietnamese_message_is_accepted(self):
        self.assertTrue(
            _NS["_maint_message_deliverable"](
                "Bên em bảo trì tới 15h30 hôm nay ạ, mong mọi người thông cảm"
            )
        )


class OwnerCommandTest(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("ZALO_PERSONAL_SESSION_DIR")
        os.environ["ZALO_PERSONAL_SESSION_DIR"] = tempfile.mkdtemp()
        self.bot = _Bot()

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("ZALO_PERSONAL_SESSION_DIR", None)
        else:
            os.environ["ZALO_PERSONAL_SESSION_DIR"] = self._prev

    def _cmd(self, text: str) -> Optional[str]:
        return self.bot._handle_owner_command(text, "chat1", False)

    def test_status_when_off(self):
        out = self._cmd("/bot baotri")
        self.assertIn("ĐANG TẮT", out)
        self.assertFalse(_NS["_get_maintenance"]().get("enabled"))

    def test_on_with_custom_message(self):
        out = self._cmd("/bot baotri Bên em bảo trì tới 15h30 hôm nay ạ")
        self.assertIn("Đã BẬT", out)
        state = _NS["_get_maintenance"]()
        self.assertTrue(state["enabled"])
        self.assertEqual(state["message"], "Bên em bảo trì tới 15h30 hôm nay ạ")
        self.assertIn("ĐANG BẬT", self._cmd("/bot baotri"))

    def test_on_default_then_off(self):
        self.assertIn("Đã BẬT", self._cmd("/bot baotri on"))
        self.assertEqual(_NS["_get_maintenance"]()["message"], "")
        self.assertIn("Đã TẮT", self._cmd("/bot baotri off"))
        self.assertFalse(_NS["_get_maintenance"]()["enabled"])

    def test_undeliverable_message_is_refused_and_does_not_enable(self):
        out = self._cmd("/bot baotri 🔧 Bảo trì tới 15h30 nhé")
        self.assertIn("CHƯA được bật", out)
        self.assertFalse(_NS["_get_maintenance"]().get("enabled"))

    def test_off_clears_notice_rate_limit(self):
        self.bot._maint_notified["chat1"] = 123.0
        self._cmd("/bot baotri off")
        self.assertEqual(self.bot._maint_notified, {})

    def test_aliases(self):
        for verb in ("baotri", "maintenance", "maint"):
            self._cmd(f"/bot {verb} on")
            self.assertTrue(_NS["_get_maintenance"]()["enabled"])
            self._cmd(f"/bot {verb} off")
            self.assertFalse(_NS["_get_maintenance"]()["enabled"])

    def test_help_lists_maintenance(self):
        src = _source()
        self.assertIn("/bot baotri off", src)


if __name__ == "__main__":
    unittest.main()
