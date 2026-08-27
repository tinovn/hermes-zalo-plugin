"""Cuộc gọi nhỡ → bot tự nhắn lại (bot chỉ nhắn tin, không nghe gọi được).

Zalo đẩy log cuộc gọi dưới dạng msgType "chat.recommended" + ``content.action``
chứa "call" ("…call.miss" = gọi nhỡ). Sidecar bóc thành ``kind="call"``;
adapter trả lời cuộc gọi NHỠ, rate-limit theo chat.

``adapter.py`` import ``gateway.*`` (không có ngoài bản cài Hermes) nên
``_handle_call_event`` được lift bằng ``ast`` và gắn vào một stub adapter.
"""

import ast
import asyncio
import importlib.util
import os
import sys
import tempfile
import textwrap
import unittest
from typing import Any, Dict, Optional

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ADAPTER = os.path.join(_ROOT, "adapter.py")
_OWNER = "owner-uid-1"


def _source() -> str:
    with open(_ADAPTER, encoding="utf-8") as f:
        return f.read()


def _module_ns() -> dict:
    src = _source()
    head = src.split("from gateway.platforms.base import")[0]
    ns: dict = {"__name__": "zalo_adapter_head_call_test"}
    exec(compile(head, _ADAPTER, "exec"), ns)
    spec = importlib.util.spec_from_file_location(
        "zalo_message_filtering_call_test", os.path.join(_ROOT, "message_filtering.py")
    )
    mf = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mf
    spec.loader.exec_module(mf)
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
    _handle_call_event = _lift("_handle_call_event", _NS)

    def __init__(self, **over):
        self.owner_uid = _OWNER
        self._missed_call_reply = True
        self._missed_call_in_groups = False
        self._missed_call_interval_s = 600.0
        self._missed_call_notified: Dict[str, float] = {}
        self._maint_notified: Dict[str, float] = {}
        self.sent: list = []
        self.__dict__.update(over)

    async def send(self, chat_id, content, **kw):
        self.sent.append((chat_id, content))


def _call(missed=True, action="recommened.rmsg.call.miss", video=False) -> Dict[str, Any]:
    return {"kind": "call", "action": action, "missed": missed, "video": video}


def _run(bot, content, thread_id="chat1", from_uid="cust-1", is_group=False, mode="default"):
    asyncio.run(bot._handle_call_event(content, thread_id, from_uid, is_group, mode))
    return bot.sent


class MissedCallReplyTest(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("ZALO_PERSONAL_SESSION_DIR")
        os.environ["ZALO_PERSONAL_SESSION_DIR"] = tempfile.mkdtemp()

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("ZALO_PERSONAL_SESSION_DIR", None)
        else:
            os.environ["ZALO_PERSONAL_SESSION_DIR"] = self._prev

    def test_missed_call_gets_reply(self):
        sent = _run(_Bot(), _call())
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1], _NS["_MISSED_CALL_DEFAULT_MSG"])

    def test_connected_call_is_silent(self):
        self.assertEqual(_run(_Bot(), _call(missed=False, action="call.connected")), [])

    def test_rate_limited_per_chat(self):
        bot = _Bot()
        _run(bot, _call())
        _run(bot, _call())          # khách bấm gọi lại ngay
        _run(bot, _call())
        self.assertEqual(len(bot.sent), 1)

    def test_other_chat_not_suppressed(self):
        bot = _Bot()
        _run(bot, _call(), thread_id="chat1")
        _run(bot, _call(), thread_id="chat2")
        self.assertEqual(len(bot.sent), 2)

    def test_replies_again_after_interval(self):
        bot = _Bot(_missed_call_interval_s=0.0)
        _run(bot, _call())
        _run(bot, _call())
        self.assertEqual(len(bot.sent), 2)

    def test_group_call_silent_by_default(self):
        self.assertEqual(_run(_Bot(), _call(), is_group=True), [])

    def test_group_call_when_opted_in(self):
        bot = _Bot(_missed_call_in_groups=True)
        self.assertEqual(len(_run(bot, _call(), is_group=True)), 1)

    def test_disabled_by_env_flag(self):
        self.assertEqual(_run(_Bot(_missed_call_reply=False), _call()), [])

    def test_listen_only_and_mute_stay_silent(self):
        for mode in ("listen_only", "mute"):
            self.assertEqual(_run(_Bot(), _call(), mode=mode), [], mode)

    def test_maintenance_message_wins(self):
        _NS["_set_maintenance"](True, "Bên em bảo trì tới 15h30 ạ")
        bot = _Bot()
        _run(bot, _call())
        self.assertEqual(bot.sent[0][1], "Bên em bảo trì tới 15h30 ạ")
        # dùng RL của bảo trì, không phải RL cuộc gọi
        self.assertIn("chat1", bot._maint_notified)
        self.assertEqual(bot._missed_call_notified, {})

    def test_owner_call_during_maintenance_gets_normal_reply(self):
        _NS["_set_maintenance"](True, "Bên em bảo trì tới 15h30 ạ")
        bot = _Bot()
        _run(bot, _call(), from_uid=_OWNER)
        self.assertEqual(bot.sent[0][1], _NS["_MISSED_CALL_DEFAULT_MSG"])

    def test_send_failure_does_not_raise(self):
        bot = _Bot()

        async def boom(*a, **k):
            raise RuntimeError("sidecar down")

        bot.send = boom
        _run(bot, _call())  # không được ném ra ngoài _handle_message

    def test_notified_map_is_bounded(self):
        bot = _Bot(_missed_call_interval_s=0.0)
        for i in range(600):
            _run(bot, _call(), thread_id=f"chat{i}")
        self.assertLessEqual(len(bot._missed_call_notified), 501)


class MissedCallMessageTest(unittest.TestCase):
    def test_default_message_survives_outbound_filter(self):
        self.assertTrue(
            _NS["_maint_message_deliverable"](_NS["_MISSED_CALL_DEFAULT_MSG"])
        )

    def test_default_message_promises_no_callback(self):
        # Bot không gọi lại được — câu mặc định không được hứa gọi lại.
        low = _NS["_MISSED_CALL_DEFAULT_MSG"].lower()
        for promise in ("gọi lại", "sẽ gọi", "liên hệ lại qua điện thoại"):
            self.assertNotIn(promise, low)


if __name__ == "__main__":
    unittest.main()
