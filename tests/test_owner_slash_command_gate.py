"""Test that owner slash commands survive a leading @BotName mention.

In a group the bot only reacts when mentioned (the default ``mention_only``
mode), so the natural way for the owner to issue a command is
``@Bot /bot status``. The gate compared the RAW text against ``/bot``, which
that string does not start with, so the command fell through to the agent —
which then tried to answer it conversationally and reported a broken tool
instead of running the command. Observed 2026-08-18 on the "Vi" bot: the
owner sent ``@Vi /bot status`` and got "Em chưa đọc được trạng thái chat do
công cụ kiểm tra đang lỗi".

The mention stripper already existed; it just ran ~70 lines too late.

``adapter.py`` pulls in ``gateway.*`` (absent outside a Hermes install), so
the real ``_strip_self_mention`` is lifted out with ``ast`` and bound to a
stub carrying only the ``_self_uid`` attribute it reads.
"""

import ast
import os
import re
import textwrap
import unittest
from typing import Any, Dict

_ADAPTER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "adapter.py"
)
_BOT_UID = "2161700482801034170"


def _source() -> str:
    with open(_ADAPTER, encoding="utf-8") as f:
        return f.read()


def _load_stripper():
    src = _source()
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "_strip_self_mention"
    )
    ns: dict = {"Dict": Dict, "Any": Any}
    exec(compile(textwrap.dedent(ast.get_source_segment(src, fn)), _ADAPTER, "exec"), ns)

    class _Bot:
        _self_uid = _BOT_UID
        _strip_self_mention = ns["_strip_self_mention"]

    return _Bot()


_bot = _load_stripper()


def _mention_payload(text: str, name: str = "@Vi") -> dict:
    """Zalo delivers mentions as (pos, len) offsets into the raw text."""
    return {"mentions": [{"uid": _BOT_UID, "pos": text.index(name), "len": len(name)}]}


def _is_command(text: str, content: dict) -> bool:
    """Mirror of the gate's predicate, over the real stripper."""
    cleaned = _bot._strip_self_mention(text, content)
    return cleaned.strip().lower().startswith("/bot")


class TestOwnerSlashCommandGate(unittest.TestCase):
    def test_command_recognised_after_leading_mention(self):
        """The exact message that failed in production."""
        text = "@Vi /bot status"
        self.assertTrue(_is_command(text, _mention_payload(text)))

    def test_command_still_recognised_without_mention(self):
        """DMs need no mention — must not regress."""
        self.assertTrue(_is_command("/bot status", {}))

    def test_all_documented_verbs_survive_a_mention(self):
        for cmd in (
            "/bot status",
            "/bot mode active",
            "/bot mode listen_only",
            "/bot digest on",
            "/bot modes",
            "/bot help",
        ):
            text = f"@Vi {cmd}"
            with self.subTest(cmd=cmd):
                self.assertTrue(_is_command(text, _mention_payload(text)))

    def test_ordinary_message_is_not_a_command(self):
        text = "@Vi đọc file này giúp anh"
        self.assertFalse(_is_command(text, _mention_payload(text)))

    def test_mention_of_someone_else_is_not_stripped(self):
        """Only the bot's own mention is removed, keyed on uid."""
        text = "@Trung /bot status"
        other = {"mentions": [{"uid": "9999999999", "pos": 0, "len": 6}]}
        self.assertEqual(_bot._strip_self_mention(text, other), text)

    def test_gate_uses_the_stripped_text(self):
        """Guard the wiring, not just the helper: the gate must not read raw text."""
        m = re.search(
            r"_cmd_text\s*=\s*self\._strip_self_mention\(.*?\).*?"
            r"startswith\(\"/bot\"\).*?_handle_owner_command\((\w+)",
            _source(), re.DOTALL,
        )
        self.assertIsNotNone(m, "slash-command gate no longer strips the mention first")
        self.assertEqual(
            m.group(1), "_cmd_text",
            "command is dispatched with the raw text, so '@Bot /bot mode active' "
            "would reach _handle_owner_command with the mention still attached",
        )


if __name__ == "__main__":
    unittest.main()
