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


# Every notice below is emitted by hermes-agent with a leading status glyph
# (⚠ ℹ 🗜️ 📦 💤 ⏳ ⏱️ ✓ …) that varies per release, so anchoring on a fixed
# character class kept missing the real wording. ``_line`` allows any run of
# non-word characters before the phrase and matches to end-of-line, so the
# whole offending line disappears and adjacent real text survives.
# NB: "not a word char" is wrong here — Python's ``\w`` matches ℹ (U+2139 is
# Other_Alphabetic), so ``[^\w\n]`` would reject the "ℹ Configured …" family.
# Anchor on "not an ASCII alphanumeric" instead: whitespace, markdown marks and
# every status glyph qualify, while a real Vietnamese sentence starts with an
# ASCII letter and is left alone.
_LEAD = r"[^A-Za-z0-9\n]{0,12}"


def _line(body: str) -> "re.Pattern[str]":
    """Whole-line pattern for a notice that OPENS the line (after its glyph)."""
    return re.compile(r"^" + _LEAD + body + r"[^\n]*$", re.IGNORECASE | re.MULTILINE)


def _line_any(body: str) -> "re.Pattern[str]":
    """Whole-line pattern for a phrase that sits mid-sentence.

    Used only for phrases specific enough that their presence makes the entire
    line a diagnostic (e.g. "Auto-lowered this session's threshold" arrives at
    the tail of a two-clause English warning)."""
    return re.compile(r"^[^\n]*" + body + r"[^\n]*$", re.IGNORECASE | re.MULTILINE)


# Operational / progress families — dropped, never shown. Each entry matches the
# WHOLE offending line/phrase so surrounding real text is preserved.
#
# Wording mirrors NousResearch/hermes-agent:
#   * agent/conversation_compression.py — COMPACTION_STATUS,
#     COMPACTION_DONE_STATUS and the *_STATUS_TEMPLATE family.
#   * gateway/run.py::_TELEGRAM_NOISY_STATUS_RE — aux/provider/retry chatter.
# Upstream gates routine compression progress behind ``compression
# .progress_notices`` but deliberately keeps the "compacted" lifecycle edge
# deliverable on chat surfaces (tests/gateway/test_compression_progress_
# notices.py), so Zalo has to drop it here.
_OPERATIONAL = [
    # Busy / interrupt / queue acks.
    ("busy_interrupt", _line(r"Interrupting current task\.?")),
    ("busy_interrupt", _line(r"I['’]?ll respond to your (?:message|msg) shortly\.?")),
    ("queued", _line(r"I['’]?ve queued your (?:message|msg)\b")),
    ("steering", _line(r"I['’]?ll steer the (?:current|running) (?:task|work)\b")),
    # Context-compaction lifecycle: start, progress, retry chatter, completion.
    ("compaction_progress", _line(r"Context too large\b")),
    ("compaction_progress", _line(r"(?:Compacting|Compressing) (?:the )?(?:conversation|context)\b")),
    ("compaction_progress", _line(r"Preflight compression\b")),
    ("compaction_progress", _line(r"Pre[-\s]?API compression\b")),
    ("compaction_progress", _line(r"Resumed after \d+\s*s idle\b")),
    ("compaction_progress", _line(r"Compressed [\d,]+ (?:→|->) [\d,]+ messages, retrying\b")),
    ("compaction_progress", _line(r"Compressed ~[\d,]+ (?:→|->) ~[\d,]+ tokens, retrying\b")),
    ("compaction_progress", _line(r"Context reduced to [\d,]+ tokens \(was [\d,]+\), retrying\b")),
    ("compaction_done", _line(r"Context compaction complete\b")),
    ("compaction_autoraise", _line_any(r"(?:caps context at|auto[-\s]?compaction was raised)")),
    ("compaction_autoraise", _line_any(r"Auto-lowered (?:compression threshold|(?:this )?session'?s?\s+threshold)")),
    ("compaction_degraded", _line(r"Session compressed \d+ times\b")),
    ("compaction_lock", _line(r"Skipping concurrent compression\b")),
    # Auxiliary / summary-model diagnostics.
    ("aux_failure", _line(r"Auxiliary .{0,80}? failed\b")),
    ("aux_failure", _line(r"Compression summary failed\b")),
    ("aux_failure", _line_any(r"fallback context marker")),
    ("aux_failure", _line(r"Configured compression model .{0,80}? failed\b")),
    ("aux_failure", _line(r"No auxiliary LLM provider configured\b")),
    ("aux_failure", _line(r"Configured auxiliary compression provider .{0,80}? (?:is )?unavailable\b")),
    # Provider retry / rate-limit / transport chatter.
    ("provider_retry", _line(r"Rate limited\.?\s+Waiting \d")),
    ("provider_retry", _line(r"Retrying in \d")),
    ("provider_retry", _line(r"Retrying API call \(")),
    ("provider_retry", _line(r"Max retries \(\d+\)")),
    ("provider_retry", _line(r"Stream drop\b")),
    ("provider_retry", _line_any(r"stale connections from a previous provider issue")),
    ("provider_retry", _line(r"(?:Primary model failed|Switched to fallback|Fallback model:|Fallback chain \()")),
    ("provider_retry", _line(r"waiting on .{0,60} — ")),
    # Turn-recovery notes the loop prints when it patches up a bad model
    # response. Pure mechanics — meaningless to a Zalo reader in any role.
    ("turn_recovery", _line(r"Empty response after tool calls\b")),
    ("turn_recovery", _line(r"Model signaled a tool call but sent none\b")),
    ("turn_recovery", _line(r"Stream interrupted\b")),
    ("turn_recovery", _line(r"Thinking-only response\b")),
    ("turn_recovery", _line(r"model returned reasoning with no final answer\b")),
    ("turn_recovery", _line(r"Injecting recovery tool results\b")),
    ("turn_recovery", _line(r"(?:Stripped invalid surrogate|Surrogate encoding error)")),
    ("turn_recovery", _line(r"Truncated tool call detected\b")),
    ("turn_recovery", _line(r"Unknown tool '")),
    ("turn_recovery", _line(r"Invalid JSON in tool call arguments\b")),
    ("turn_recovery", _line(r"Invalid API response \(attempt")),
    ("turn_recovery", _line(r"API call failed \(attempt")),
    ("turn_recovery", _line(r"Incomplete <REASONING_SCRATCHPAD>")),
    ("turn_recovery", _line(r"Reached maximum iterations \(")),
    ("turn_recovery", _line(r"Output cap too large for current prompt\b")),
    ("turn_recovery", _line(r"Request payload too large \(413\)")),
    ("turn_recovery", _line(r"Agent inactive for \d+ min\b")),
    ("turn_recovery", _line(r"Context file .{0,80}? TRUNCATED")),
    ("turn_recovery", _line(r"Anthropic long-context tier\b")),
    # Progress variants of the context-limit message — these are recoveries,
    # NOT the terminal "Context length exceeded" below, so they must be
    # stripped before the terminal tier turns them into a recovery notice.
    ("compaction_progress", _line(r"Context length exceeded[ \t]*[—,-][^\n]*(?:provider limit|did not report)")),
]

# Terminal technical failures — replaced by ONE localized recovery notice.
_TERMINAL = [
    ("context_exceeded", _line(r"Context length exceeded\b")),
    ("cannot_compress", _line(r"Cannot compress (?:the conversation )?further\.?")),
]

# ── Owner-only tier (``classify(..., is_owner=False)``) ─────────────────────
# hermes-agent deliberately keeps these visible on chat surfaces because an
# operator has to act on them (rerun /compress, /new). They are still raw
# English diagnostics, so a Zalo customer must never receive them — the owner
# DM does, unchanged.
_OWNER_ONLY_OPERATIONAL = [
    # Manual /compress feedback and abort notices.
    ("compress_feedback", _line(r"Compressed: [\d,]+ (?:→|->) [\d,]+ messages")),
    ("compress_feedback", _line(r"Compressed with fallback: ")),
    ("compress_feedback", _line(r"No changes from compression: ")),
    ("compress_feedback", _line(r"Compression aborted\b")),
    ("compress_feedback", _line(r"Compression returned an empty transcript\b")),
    ("compress_feedback", _line(r"Compression already in progress for this session\b")),
    ("compress_feedback", _line(r"Compression skipped:")),
    # Gateway back-pressure: the turn is queued / the agent is busy. The owner
    # wants to know; a customer just sees untranslated English.
    ("gateway_busy", _line(r"Working — \d+\s*min")),
    ("gateway_busy", _line(r"Queued for the next turn\b")),
    ("gateway_busy", _line(r"Another turn is still running on this session\b")),
    ("gateway_busy", _line(r"Gateway (?:is |)\S+ .{0,60}?(?:not accepting|queued for the next turn)")),
    ("gateway_busy", _line(r"This agent is draining\b")),
    ("gateway_busy", _line(r"Agent is running — ")),
    ("gateway_busy", _line(r"Subagent working\b")),
    ("gateway_busy", _line(r"Goal parked on pid \d")),
    # Operator/host maintenance chatter.
    ("host_admin", _line_any(r"Command Approval Required")),
    ("host_admin", _line_any(r"Credits \d+% used")),
    ("host_admin", _line(r"Hermes update (?:failed|timed out)")),
    ("host_admin", _line(r"Could not load config:")),
    ("host_admin", _line(r"Some tools may not work due to missing requirements\b")),
    ("host_admin", _line(r"Missing requirements:")),
    ("host_admin", _line(r"Warning: API key appears invalid or missing\b")),
    ("host_admin", _line(r"(?:FAL_KEY environment variable not set|No web search backend configured|No auxiliary vision model available|fal_client library not found)")),
    ("host_admin", _line(r"Lightpanda fallback:")),
    ("host_admin", _line_any(r"models are NOT agentic\b")),
    ("host_admin", _line_any(r"Kanban \S+ (?:timed out|routed to|→ )")),
]

# Failure-class for the owner, but for a customer it just means "the bot is
# about to stop answering" → collapse to the localized recovery notice.
_OWNER_ONLY_TERMINAL = [
    ("context_overflow_blocked", _line(r"Context is over the compression threshold\b")),
    ("provider_failure", _line(r"API failed after \d+ retries\b")),
    ("provider_failure", _line(r"Rate limited after \d+ retries\b")),
    ("provider_failure", _line(r"Billing or credits exhausted\b")),
    ("provider_failure", _line(r"Connection to provider failed\b")),
    ("provider_failure", _line(r"Provider returned (?:an empty response stream|malformed streaming data)")),
    ("provider_failure", _line(r"Non-retryable error \(HTTP")),
    ("provider_failure", _line(r"Model returned no content after all retries\b")),
    ("provider_failure", _line(r"Max retries \(\d+\) exceeded for invalid responses\b")),
    ("provider_failure", _line(r"TLS certificate verification failed\b")),
    ("provider_failure", _line(r"Ollama runtime context is too small\b")),
    ("provider_failure", _line(r"no (?:output|response) from provider\b")),
    ("provider_failure", _line(r"The model provider is rate-limiting requests\b")),
    ("provider_failure", _line(r"The model (?:declined to respond|provider's safety filter blocked)")),
    ("provider_failure", _line(r"Provider safety filter blocked this request\b")),
]


def _collapse_blank_lines(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def classify(text: Optional[str], is_owner: bool = False) -> FilterDecision:
    """Split operational/terminal notices from the real answer.

    ``is_owner`` marks the owner DM — the one surface allowed to receive the
    operator-facing notices in ``_OWNER_ONLY_*`` (manual /compress feedback,
    compression-aborted, blocked-overflow warning). Defaults to False so any
    caller that forgets the flag gets the strict customer-facing behaviour.
    """
    if not text or not text.strip():
        return FilterDecision(FilterAction.DROP_OPERATIONAL, "", ())

    categories: List[str] = []
    cleaned = text
    has_terminal = False
    terminal_cat: Optional[str] = None

    operational = _OPERATIONAL if is_owner else _OPERATIONAL + _OWNER_ONLY_OPERATIONAL
    terminal = _TERMINAL if is_owner else _TERMINAL + _OWNER_ONLY_TERMINAL

    for cat, pat in operational:
        if pat.search(cleaned):
            categories.append(cat)
            cleaned = pat.sub("", cleaned)
    for cat, pat in terminal:
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


# ── Persona-aware canned notices ────────────────────────────────────────────
# User-facing lines emitted WITHOUT an LLM in the loop (soft-error, terminal
# recovery, non-owner deny) can be overridden per agent persona via the
# ``notices`` map in bot_persona.json — e.g. the "Ông Bụt" persona replaces
# "Anh/chị chờ em chút…" with "Con chờ ta một chút…". The custom line is
# validated so it cannot itself match an operational/terminal pattern (which
# would break classify() idempotency) and is length-capped.

MAX_NOTICE_LEN = 300


def resolve_notice(persona: Optional[dict], key: str, default: str) -> str:
    """Return the persona's custom notice for ``key`` or ``default``.

    Accepts the loaded bot-persona dict (may be None/partial). A custom notice
    is used only when it is a non-empty single-purpose string that classifies
    as KEEP — otherwise the safe default wins.
    """
    try:
        notices = (persona or {}).get("notices")
        if not isinstance(notices, dict):
            return default
        val = notices.get(key)
        if not isinstance(val, str):
            return default
        val = val.strip()
        if not val or len(val) > MAX_NOTICE_LEN:
            return default
        decision = classify(val)
        if decision.action != FilterAction.KEEP or decision.cleaned_text != val:
            return default
        return val
    except Exception:
        return default


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


# ── Name triggers ──────────────────────────────────────────────────────────
# Cho phép gọi bot bằng TÊN trong nhóm (vd "ông bụt ơi") mà không cần @tag.
# Ở chế độ mention_only, một tin coi như "được nhắc" nếu chứa một trong các tên
# gọi cấu hình → bot phản hồi đúng khi được gọi tên, KHÔNG phản hồi mọi tin.

def parse_name_triggers(raw) -> List[str]:
    """Parse cấu hình tên gọi → list token thường-hoá, bỏ trống/trùng.

    Nhận chuỗi phân tách bằng dấu phẩy ("Ông Bụt, Bụt") hoặc list. Trả về các
    token đã ``strip().lower()`` (khớp không phân biệt hoa/thường), giữ thứ tự.
    """
    if isinstance(raw, str):
        items = raw.split(",")
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        return []
    out: List[str] = []
    seen = set()
    for it in items:
        t = str(it).strip().lower()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def text_has_name_trigger(text: Optional[str], triggers) -> bool:
    """True nếu ``text`` (không phân biệt hoa/thường) chứa BẤT KỲ tên gọi nào.

    So khớp dạng substring — "ông bụt" khớp "ông bụt ơi", "nhờ ông bụt tí". Rỗng
    triggers → luôn False (tắt tính năng, chỉ @tag mới kích hoạt).
    """
    low = (text or "").lower()
    if not low:
        return False
    return any(t and t in low for t in (triggers or ()))
