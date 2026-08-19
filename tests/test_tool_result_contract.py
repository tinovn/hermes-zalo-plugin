"""Test that tool handlers return what the Hermes tool pipeline accepts.

Hermes >= 0.20 enforces the result shape in ``tools/registry.py``::

    if isinstance(result, str): ...
    if isinstance(result, dict) and result.get("_multimodal") is True: ...
    logger.error("Tool %s handler returned unsupported result type: %s", ...)

Every handler in this plugin is annotated ``-> Dict[str, Any]``, so on a
0.20.4 host 51 of 52 tools failed — 102 logged failures between 2026-08-18
19:36 and 2026-08-19 16:26, covering zalo_get_chat_mode, zalo_set_chat_mode,
zalo_set_chat_persona, zalo_send_image, zalo_read_recent_image, zalo_send_pdf,
zalo_groups_list and zalo_master_sheet. The same plugin kept working on a
0.18.0 host, which coerces with ``str()`` instead of rejecting — which is why
this went unnoticed on the older fleet.

``adapter.py`` cannot be imported (it pulls in ``gateway.*``), so the two
helpers are lifted out with ``ast``.
"""

import ast
import json
import os
import re
import textwrap
import unittest
from typing import Any

_ADAPTER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "adapter.py"
)


def _source() -> str:
    with open(_ADAPTER, encoding="utf-8") as f:
        return f.read()


def _load(*names):
    src = _source()
    tree = ast.parse(src)
    ns: dict = {"Any": Any, "json": json}
    import functools as _ft
    ns["functools"] = _ft
    for name in names:
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == name
        )
        exec(compile(textwrap.dedent(ast.get_source_segment(src, fn)), _ADAPTER, "exec"), ns)
    return ns


_NS = _load("_tool_result_as_text", "_stringify_tool_handler")
_as_text = _NS["_tool_result_as_text"]
_wrap = _NS["_stringify_tool_handler"]


def _hermes_020_accepts(result) -> bool:
    """Mirror of the 0.20 gate in tools/registry.py."""
    if isinstance(result, str):
        return True
    return (
        isinstance(result, dict)
        and result.get("_multimodal") is True
        and isinstance(result.get("content"), list)
    )


class TestToolResultContract(unittest.TestCase):
    def test_dict_result_becomes_json_string(self):
        out = _as_text({"success": True, "mode": "active"})
        self.assertIsInstance(out, str)
        self.assertEqual(json.loads(out), {"success": True, "mode": "active"})

    def test_wrapped_handler_output_passes_the_020_gate(self):
        """The exact failure: a dict handler under Hermes 0.20."""
        def handler(args=None, **kw):
            return {"success": True, "chat_id": "123", "mode": "default"}

        self.assertFalse(_hermes_020_accepts(handler()), "precondition: raw dict is rejected")
        self.assertTrue(_hermes_020_accepts(_wrap(handler)()))

    def test_vietnamese_text_is_not_escaped(self):
        """ensure_ascii=False — the model should read real text, not \\uXXXX."""
        out = _as_text({"msg": "Đã chuyển mode → active"})
        self.assertIn("Đã chuyển mode → active", out)

    def test_string_result_passes_through_untouched(self):
        self.assertEqual(_as_text("plain text"), "plain text")

    def test_multimodal_envelope_is_preserved(self):
        env = {"_multimodal": True, "content": [{"type": "text", "text": "hi"}]}
        self.assertIs(_as_text(env), env)

    def test_non_serialisable_value_degrades_instead_of_raising(self):
        """A stray object must not blow up and lose the whole tool result."""
        class Weird:
            def __repr__(self):
                return "<weird>"

        out = _as_text({"obj": Weird()})
        self.assertIn("<weird>", out)

    def test_wrapper_preserves_handler_identity(self):
        """functools.wraps — the registry logs and introspects handler names."""
        def _zalo_demo_handler(args=None, **kw):
            return {"ok": True}

        self.assertEqual(_wrap(_zalo_demo_handler).__name__, "_zalo_demo_handler")

    def test_wrapper_forwards_args_and_kwargs(self):
        seen = {}

        def handler(args=None, **kw):
            seen["args"] = args
            seen["kw"] = kw
            return {"ok": True}

        _wrap(handler)({"chat_id": "1"}, task_id="t1")
        self.assertEqual(seen["args"], {"chat_id": "1"})
        self.assertEqual(seen["kw"], {"task_id": "t1"})

    def test_registration_is_wrapped_in_register(self):
        """Guard the wiring: every ctx.register_tool must go through the wrapper."""
        m = re.search(
            r"def register\(ctx\):.*?"
            r"_orig_register_tool = ctx\.register_tool.*?"
            r"kw\[\"handler\"\] = _stringify_tool_handler\(kw\[\"handler\"\]\).*?"
            r"ctx\.register_tool = _register_tool",
            _source(), re.DOTALL,
        )
        self.assertIsNotNone(
            m, "register() no longer wraps ctx.register_tool; tool results would "
               "go back to raw dicts and fail on Hermes >= 0.20",
        )


if __name__ == "__main__":
    unittest.main()
