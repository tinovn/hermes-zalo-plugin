"""Tests for outbound message chunking (``send`` → ``_split_long_message``).

Zalo's ``/send/text`` rejects long bodies with ``ZaloApiError code 118``
("Nội dung quá dài"). The adapter guards against that by splitting, but the
guard silently stopped covering a whole band of message sizes: the constant
that DOCUMENTS the invariant (``SEND_CHUNK_LIMIT = 1900``, "we keep each
outgoing chunk under this threshold") was only used as the chunk *size*,
while the *decision* to split at all was gated on a separate, laxer
``SEND_CHUNK_HARD_LIMIT = 4500``. Anything in between was sent whole and
bounced — and because the plain-text fallback resends the same body, the
customer got nothing at all.

Production logs on 2026-08-18 confirmed the band empirically: 15 sends failed,
every one of them between 3023 and 4381 characters, while sends above 4500
(up to 24895) all succeeded because those *were* split.

``adapter.py`` can't be imported here — it pulls in ``gateway.*``, which only
exists inside a Hermes install — and ``_split_long_message`` is defined below
that import, so the prefix-exec trick used by the other test modules doesn't
reach it. We lift the real function out of the source with ``ast`` instead, so
these tests can never drift from the code they claim to cover.
"""

import ast
import os
import re
import textwrap
import unittest
from typing import List

_ADAPTER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "adapter.py"
)

# Smallest send that Zalo actually rejected in production (2026-08-18 logs).
# Any chunk limit at or above this is known-broken.
_OBSERVED_ZALO_REJECT = 3023


def _adapter_source() -> str:
    with open(_ADAPTER, encoding="utf-8") as f:
        return f.read()


def _load_split_fn():
    """Lift the real ``_split_long_message`` out of adapter.py."""
    src = _adapter_source()
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "_split_long_message"
    )
    code = textwrap.dedent(ast.get_source_segment(src, fn)).replace("@staticmethod\n", "")
    ns: dict = {"List": List, "re": re}
    exec(compile(code, _ADAPTER, "exec"), ns)
    return ns["_split_long_message"]


def _class_int_const(name: str) -> int:
    m = re.search(rf"^\s*{name}\s*=\s*(\d+)", _adapter_source(), re.MULTILINE)
    assert m, f"constant {name} not found in adapter.py"
    return int(m.group(1))


_split_long_message = _load_split_fn()
_LIMIT = _class_int_const("SEND_CHUNK_LIMIT")


class TestSplitGateMatchesChunkSize(unittest.TestCase):
    """The invariant that regressed: gate and chunk size must be the SAME knob.

    This is the test whose absence let the bug live from 2026-07-09 to
    2026-08-18. Splitting into 1900-char pieces only helps if the decision to
    split is taken at 1900 too.
    """

    def test_gate_and_chunk_size_use_the_same_constant(self):
        m = re.search(
            r"if len\(content\) > self\.(\w+):\s*\n"
            r"\s*chunks = self\._split_long_message\(content, self\.(\w+)\)",
            _adapter_source(),
        )
        self.assertIsNotNone(
            m, "could not find the split gate in send() — did the shape change?"
        )
        gate, chunk_size = m.group(1), m.group(2)
        self.assertEqual(
            gate, chunk_size,
            f"split is gated on {gate} but chunks are sized by {chunk_size}; "
            f"messages between the two are sent whole and Zalo rejects them",
        )

    def test_limit_is_below_what_zalo_actually_rejects(self):
        self.assertLess(
            _LIMIT, _OBSERVED_ZALO_REJECT,
            f"SEND_CHUNK_LIMIT={_LIMIT} is at or above the smallest body Zalo "
            f"rejected in production ({_OBSERVED_ZALO_REJECT} chars)",
        )


class TestSplitLongMessage(unittest.TestCase):
    def test_short_message_is_not_split(self):
        text = "Dạ em gửi anh bảng giá nha."
        self.assertEqual(_split_long_message(text, _LIMIT), [text])

    def test_message_at_limit_is_not_split(self):
        self.assertEqual(len(_split_long_message("x" * _LIMIT, _LIMIT)), 1)

    def test_message_just_over_limit_is_split(self):
        self.assertGreater(len(_split_long_message("x" * (_LIMIT + 1), _LIMIT)), 1)

    def test_previously_lost_band_is_split(self):
        """3023-4381 chars: the band that used to be dropped on the floor."""
        for n in (_OBSERVED_ZALO_REJECT, 3026, 4381, 4500):
            with self.subTest(length=n):
                chunks = _split_long_message("x" * n, _LIMIT)
                self.assertGreater(len(chunks), 1)
                # Chunks carry a "[i/N]" progress suffix appended after the
                # size check, so allow that much headroom over the limit.
                self.assertLess(max(len(c) for c in chunks), _OBSERVED_ZALO_REJECT)

    def test_no_content_is_lost(self):
        para = (
            "Dưới đây là mẫu biên bản hội nghị tổ dân phố anh chị tham khảo nhé. "
            "Nội dung gồm phần mở đầu, thành phần tham dự, diễn biến và kết luận. "
        )
        text = (para * 25)[:3026]
        chunks = _split_long_message(text, _LIMIT)
        # Strip the "[i/N]" suffixes, then compare ignoring whitespace runs.
        body = " ".join(re.sub(r"\n\n\[\d+/\d+\]$", "", c) for c in chunks)
        self.assertEqual(
            re.sub(r"\s+", " ", body).strip(),
            re.sub(r"\s+", " ", text).strip(),
        )

    def test_every_chunk_is_numbered_when_split(self):
        chunks = _split_long_message("x" * 4000, _LIMIT)
        for i, c in enumerate(chunks, 1):
            self.assertTrue(c.endswith(f"[{i}/{len(chunks)}]"), c[-20:])


if __name__ == "__main__":
    unittest.main()
