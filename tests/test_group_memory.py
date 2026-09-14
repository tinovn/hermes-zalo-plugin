"""Tests for group_memory.py (per-group facts/rules notebook, append-only)
and its wiring into adapter.py — deny-list membership, tool registration,
and prompt-injection ordering.

Logic tests import group_memory.py directly (dep-free, no gateway.*).
Wiring tests check adapter.py source text, mirroring the style in
tests/test_owner_slash_command_gate.py (adapter.py can't be imported
directly outside a Hermes install because it pulls in gateway.*).
"""

import os
import re
import unittest

import group_memory as gm

_ADAPTER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "adapter.py"
)


def _source() -> str:
    with open(_ADAPTER, encoding="utf-8") as f:
        return f.read()


class TestCoerceLines(unittest.TestCase):
    def test_list_passthrough_strips_and_drops_empty(self):
        self.assertEqual(
            gm.coerce_lines([" a ", "", "  ", "b"]), ["a", "b"]
        )

    def test_legacy_string_split_on_newline(self):
        self.assertEqual(
            gm.coerce_lines("dòng 1\ndòng 2\n\ndòng 3"),
            ["dòng 1", "dòng 2", "dòng 3"],
        )

    def test_none_and_other_types_yield_empty(self):
        self.assertEqual(gm.coerce_lines(None), [])
        self.assertEqual(gm.coerce_lines(42), [])


class TestDedupAppend(unittest.TestCase):
    def test_add_then_read_back(self):
        out = gm.dedup_append([], "Xưng hô: gọi Anh là Ba")
        self.assertEqual(out, ["Xưng hô: gọi Anh là Ba"])

    def test_multiple_adds_accumulate(self):
        """The exact regression the old persona-blob design had: a second
        add() must not lose the first line."""
        out = gm.dedup_append([], "fact 1")
        out = gm.dedup_append(out, "fact 2")
        out = gm.dedup_append(out, "fact 3")
        self.assertEqual(out, ["fact 1", "fact 2", "fact 3"])

    def test_exact_duplicate_append_ignored_case_insensitive(self):
        out = gm.dedup_append(["Lịch thi: 20/12"], "lịch thi: 20/12")
        self.assertEqual(out, ["Lịch thi: 20/12"])

    def test_multiline_add_appends_each_new_line(self):
        out = gm.dedup_append(["a"], "b\na\nc")
        self.assertEqual(out, ["a", "b", "c"])


class TestRemoveLine(unittest.TestCase):
    def setUp(self):
        self.lines = ["dòng một", "dòng hai", "dòng ba"]

    def test_remove_by_index(self):
        new_lines, removed = gm.remove_line(self.lines, "2")
        self.assertEqual(removed, "dòng hai")
        self.assertEqual(new_lines, ["dòng một", "dòng ba"])

    def test_remove_by_substring_case_insensitive(self):
        new_lines, removed = gm.remove_line(self.lines, "HAI")
        self.assertEqual(removed, "dòng hai")
        self.assertEqual(new_lines, ["dòng một", "dòng ba"])

    def test_remove_nonexistent_index_no_mutation(self):
        new_lines, removed = gm.remove_line(self.lines, "99")
        self.assertIsNone(removed)
        self.assertEqual(new_lines, self.lines)

    def test_remove_nonexistent_substring_no_mutation(self):
        new_lines, removed = gm.remove_line(self.lines, "không tồn tại")
        self.assertIsNone(removed)
        self.assertEqual(new_lines, self.lines)


class TestCap(unittest.TestCase):
    def test_entry_cap_drops_oldest_keeps_newest(self):
        lines = [f"line {i}" for i in range(gm.MAX_ENTRIES + 5)]
        out, warnings = gm.cap(lines)
        self.assertEqual(len(out), gm.MAX_ENTRIES)
        self.assertEqual(out, lines[-gm.MAX_ENTRIES:])
        self.assertTrue(any("30" in w or str(gm.MAX_ENTRIES) in w for w in warnings))

    def test_char_cap_keeps_joined_length_under_limit(self):
        lines = ["x" * 300 for _ in range(20)]  # 6000 chars total, well over 2000
        out, warnings = gm.cap(lines)
        self.assertLessEqual(len("\n".join(out)), gm.MAX_CHARS)
        self.assertTrue(warnings)

    def test_under_both_caps_is_untouched(self):
        lines = ["short line 1", "short line 2"]
        out, warnings = gm.cap(lines)
        self.assertEqual(out, lines)
        self.assertEqual(warnings, [])


class TestRenderBlock(unittest.TestCase):
    def test_empty_renders_empty_string(self):
        self.assertEqual(gm.render_block([]), "")

    def test_numbered_and_authoritative(self):
        block = gm.render_block(["fact A", "fact B"])
        self.assertIn("GROUP NOTEBOOK", block)
        self.assertIn("1. fact A", block)
        self.assertIn("2. fact B", block)
        self.assertIn("TRUTH", block)
        self.assertIn("owner", block.lower())


class TestAdapterWiring(unittest.TestCase):
    """Guard against the failure mode that actually bites: the feature works
    but is registered into the wrong toolset, or the ACL entry is forgotten,
    so it silently becomes world-writable."""

    def test_both_tools_are_owner_only(self):
        src = _source()
        m = re.search(
            r"_NON_OWNER_BLOCKED_TOOLS[^=]*=\s*\{(.*?)\n\}", src, re.DOTALL
        )
        self.assertIsNotNone(m, "could not locate _NON_OWNER_BLOCKED_TOOLS set")
        deny_list_src = m.group(1)
        self.assertIn("zalo_set_group_memory", deny_list_src)
        self.assertIn("zalo_get_group_memory", deny_list_src)

    def test_both_tools_registered_in_hermes_zalo_toolset(self):
        src = _source()
        for name in ("zalo_set_group_memory", "zalo_get_group_memory"):
            m = re.search(
                r'ctx\.register_tool\(\s*name="%s".*?toolset="hermes-zalo"' % re.escape(name),
                src, re.DOTALL,
            )
            self.assertIsNotNone(m, f"{name} not registered under toolset='hermes-zalo'")

    def test_both_tools_have_handlers_wired(self):
        src = _source()
        self.assertIn("handler=_zalo_set_group_memory_handler", src)
        self.assertIn("handler=_zalo_get_group_memory_handler", src)

    def test_notebook_prepended_before_owner_and_non_owner_note(self):
        """The notebook must be computed once and prepended ahead of
        whichever note (owner directive / non-owner identity) applies,
        before either gets folded into channel_prompt."""
        src = _source()
        m = re.search(
            r"notebook_note\s*=\s*_groupmem\.render_block\(_get_group_memory\(thread_id\)\)"
            r"\s*\n\s*if notebook_note:\s*\n\s*note\s*=\s*notebook_note\s*\+.*?\+\s*note",
            src,
        )
        self.assertIsNotNone(
            m, "group notebook is no longer prepended ahead of the owner/non-owner note"
        )

    def test_non_owner_declined_from_writing_notebook(self):
        src = _source()
        self.assertIn("chỉ sếp mới", src)


if __name__ == "__main__":
    unittest.main()
