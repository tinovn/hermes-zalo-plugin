"""Segment-aware outbound message filtering for the Zalo plugin.

Goal: NEVER surface Hermes/runtime lifecycle & context diagnostics to a Zalo
chat (busy/interrupt acks, compression progress, "context length exceeded",
"cannot compress further"…), while ALWAYS preserving the real assistant answer
that may sit right next to such a notice.

Design:
  * ``classify(text)`` returns a :class:`FilterDecision` carrying an action AND
    the cleaned remainder — not a bare enum — so a real answer adjacent to an
    operational notice survives.
  * Patterns are ANCHORED, specific multi-word phrase families, never a broad
    match on common words like "context" or "model" (regression-tested).
  * Idempotent: classifying the cleaned output again is a no-op / KEEP.
  * :class:`RecoveryNoticeLimiter` bounds terminal recovery notices to at most
    one per (account, chat, correlation, category) within a TTL, with an LRU
    cap — no unbounded global state, no cross-chat suppression.

Pure module: no Hermes imports, ``now`` injected for testability.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Tuple

# ── Localized user-facing notice (real Vietnamese sentence; must NOT itself
#    match any operational/terminal pattern, so classify() is idempotent). ──
RECOVERY_NOTICE = "Dạ em đang hơi quá tải, anh/chị nhắn lại giúp em câu vừa rồi nha 🙏"


class FilterAction(str, Enum):
    KEEP = "keep"                    # deliver cleaned_text
    DROP_OPERATIONAL = "drop"        # deliver nothing (pure lifecycle/progress)
    REPLACE_TERMINAL = "replace"     # deliver a single recovery notice (rate-limited)


@dataclass
class FilterDecision:
    action: FilterAction
    cleaned_text: str
    categories: Tuple[str, ...] = ()
    recovery_key: Optional[str] = None


# Operational / progress families — dropped, never shown. Each entry matches the
# WHOLE offending line/phrase so surrounding real text is preserved.
_OPERATIONAL = [
    ("busy_interrupt", re.compile(r"^[\s>*_⚡]*Interrupting current task\.?.*$", re.IGNORECASE | re.MULTILINE)),
    ("busy_interrupt", re.compile(r"^[\s>*_⚡]*I['’]?ll respond to your (?:message|msg) shortly\.?.*$", re.IGNORECASE | re.MULTILINE)),
    ("queued", re.compile(r"^[\s>*_]*I['’]?ve queued your (?:message|msg)\b.*$", re.IGNORECASE | re.MULTILINE)),
    ("steering", re.compile(r"^[\s>*_]*I['’]?ll steer the (?:current|running) (?:task|work)\b.*$", re.IGNORECASE | re.MULTILINE)),
    ("compaction_progress", re.compile(r"^[\s>*_ℹ️]*Context too large[^\n]*$", re.IGNORECASE | re.MULTILINE)),
    ("compaction_progress", re.compile(r"^[\s>*_ℹ️]*(?:Compacting|Compressing) (?:the )?(?:conversation|context)\b[^\n]*$", re.IGNORECASE | re.MULTILINE)),
    ("compaction_autoraise", re.compile(r"^[\s>*_ℹ️]*[^\n]*(?:caps context at|auto[- ]?compaction was raised)[^\n]*$", re.IGNORECASE | re.MULTILINE)),
]

# Terminal technical failures — replaced by ONE localized recovery notice.
_TERMINAL = [
    ("context_exceeded", re.compile(r"^[\s>*_]*Context length exceeded\b[^\n]*$", re.IGNORECASE | re.MULTILINE)),
    ("cannot_compress", re.compile(r"^[\s>*_]*Cannot compress (?:the conversation )?further\.?[^\n]*$", re.IGNORECASE | re.MULTILINE)),
]


def _collapse_blank_lines(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def classify(text: Optional[str]) -> FilterDecision:
    """Split operational/terminal notices from the real answer."""
    if not text or not text.strip():
        return FilterDecision(FilterAction.DROP_OPERATIONAL, "", ())

    categories: List[str] = []
    cleaned = text
    has_terminal = False
    terminal_cat: Optional[str] = None

    for cat, pat in _OPERATIONAL:
        if pat.search(cleaned):
            categories.append(cat)
            cleaned = pat.sub("", cleaned)
    for cat, pat in _TERMINAL:
        if pat.search(cleaned):
            categories.append(cat)
            has_terminal = True
            terminal_cat = terminal_cat or cat
            cleaned = pat.sub("", cleaned)

    cleaned = _collapse_blank_lines(cleaned)
    cats = tuple(dict.fromkeys(categories))  # dedupe, preserve order

    # No notice matched → keep verbatim.
    if not cats:
        return FilterDecision(FilterAction.KEEP, text, ())

    # A real answer remains alongside the notice → keep the answer, notice gone.
    if cleaned:
        return FilterDecision(FilterAction.KEEP, cleaned, cats)

    # Nothing real left. Terminal failure → one recovery notice; pure lifecycle
    # progress → drop silently.
    if has_terminal:
        return FilterDecision(FilterAction.REPLACE_TERMINAL, RECOVERY_NOTICE, cats,
                              recovery_key=terminal_cat)
    return FilterDecision(FilterAction.DROP_OPERATIONAL, "", cats)


class RecoveryNoticeLimiter:
    """At most one recovery notice per key within ``ttl`` seconds; bounded LRU.

    Key should encode account/chat/correlation/category so one chat's terminal
    failure never suppresses another's, and repeats within the window collapse
    to silence. ``now`` is injected (monotonic seconds) for testability.
    """

    def __init__(self, ttl: float = 300.0, max_size: int = 500):
        self._ttl = float(ttl)
        self._max = int(max_size)
        self._seen: "OrderedDict[str, float]" = OrderedDict()

    def should_emit(self, key: Optional[str], now: float) -> bool:
        if not key:
            # No safe correlation → prefer silence over global/repetitive state.
            return False
        # Purge expired.
        expired = [k for k, exp in self._seen.items() if exp <= now]
        for k in expired:
            self._seen.pop(k, None)
        if key in self._seen:
            self._seen.move_to_end(key)
            return False
        self._seen[key] = now + self._ttl
        self._seen.move_to_end(key)
        while len(self._seen) > self._max:
            self._seen.popitem(last=False)
        return True
