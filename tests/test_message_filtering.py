"""Tests for message_filtering.py — the segment-aware outbound classifier and
the bounded recovery-notice limiter. Pure stdlib unittest."""

import unittest

from message_filtering import (
    MAX_NOTICE_LEN,
    RECOVERY_NOTICE,
    FilterAction,
    RecoveryNoticeLimiter,
    classify,
    parse_name_triggers,
    resolve_notice,
    text_has_name_trigger,
)


class TestClassifyOperational(unittest.TestCase):
    def test_busy_interrupt_dropped(self):
        for t in [
            "⚡ Interrupting current task. I'll respond to your message shortly.",
            "Interrupting current task",
            "I'll respond to your message shortly",
        ]:
            d = classify(t)
            self.assertEqual(d.action, FilterAction.DROP_OPERATIONAL, t)
            self.assertEqual(d.cleaned_text, "")

    def test_compaction_progress_dropped(self):
        d = classify("Context too large (99,631 tokens). Compressing conversation…")
        self.assertEqual(d.action, FilterAction.DROP_OPERATIONAL)

    def test_autoraise_notice_dropped(self):
        d = classify("ℹ gpt-5.5 caps context at 272K, so auto-compaction was raised to 85%.")
        self.assertEqual(d.action, FilterAction.DROP_OPERATIONAL)


# Verbatim status wording emitted by NousResearch/hermes-agent — copied from
# agent/conversation_compression.py (ROUTINE_COMPRESSION_STATUS_SAMPLES,
# COMPACTION_DONE_STATUS) and tests/gateway/test_telegram_noise_filter.py
# (NOISY_STATUS_MESSAGES). Every one of these must be invisible on Zalo,
# including in the owner DM. Re-check this list when bumping hermes-agent.
HERMES_OPERATIONAL_STATUSES = [
    # The one that actually leaked into a customer group: upstream keeps the
    # "compacted" lifecycle edge deliverable on chat surfaces on purpose.
    "✓ Context compaction complete — continuing turn...",
    "🗜️ Compacting context — summarizing earlier conversation so I can continue...",
    "🗜️ Preflight compression check before sending...",
    "📦 Preflight compression: ~120,000 tokens >= 100,000 threshold. This may take a moment.",
    (
        "📦 Pre-API compression: ~123,456 tokens near the context/output limit. "
        "Compacting before the next model call."
    ),
    "💤 Resumed after 3600s idle — compacting ~120,000 tokens before continuing.",
    "🗜️ Context too large (~250,000 tokens) — compressing (1/3)...",
    "🗜️ Compressed 30 → 12 messages, retrying...",
    "🗜️ Compressed ~250,000 → ~120,000 tokens, retrying...",
    "🗜️ Context reduced to 120,000 tokens (was 250,000), retrying...",
    "⚠️  Session compressed 12 times — accuracy may degrade. Consider /new to start fresh.",
    "⚠ Compression summary failed: upstream error. Inserted a fallback context marker.",
    "⚠ Auxiliary title generation failed: HTTP 400: Operation contains cybersecurity risk",
    (
        "ℹ Configured compression model 'small-model' failed (timeout). Recovered "
        "using main model — check auxiliary.compression.model in config.yaml."
    ),
    (
        "⚠ Compression model small (openrouter) context is 32,000 tokens, but the "
        "main model big (anthropic)'s compression threshold was 100,000 tokens. "
        "Auto-lowered this session's threshold to 30,000 tokens so compression can run."
    ),
    (
        "⚠ Configured auxiliary compression provider 'openai' is unavailable — "
        "context compression will drop middle turns without a summary. Check "
        "auxiliary.compression in config.yaml and reauthenticate that provider."
    ),
    (
        "⚠ Skipping concurrent compression — another path is already compressing "
        "this session. Will retry after it finishes."
    ),
    "⏱️ Rate limited. Waiting 30.0s (attempt 2/3)...",
    "⏳ Retrying in 4.2s (attempt 1/3)...",
    "⚠️ Max retries (3) exhausted — trying fallback...",
    "ℹ gpt-5.5 caps context at 272K, so auto-compaction was raised to 85%.",
    # Turn-recovery mechanics (agent/conversation_loop.py).
    "↻ Empty response after tool calls — using earlier content as final answer",
    "↻ Model signaled a tool call but sent none — retrying",
    "↻ Stream interrupted — using delivered content",
    "↻ Thinking-only response — prefilling to continue",
    "↻ Switched to fallback: big-model (anthropic)",
    "🔄 Primary model failed — switching to fallback: big-model",
    "🔄 Retrying API call (2/3)...",
    "⚠️  API call failed (attempt 2/3): TimeoutError",
    "⚠️  Invalid API response (attempt 1/3): missing content",
    "⚠️  Invalid JSON in tool call arguments for 'web_search': unexpected token",
    "⚠️  Unknown tool 'foo_bar' — sending error to model for agent-correction (1/3)",
    "⚠️  Reached maximum iterations (40). Requesting summary...",
    "⚠️  Request payload too large (413) — compression attempt 1/3...",
    "⚠️  Stripped invalid surrogate characters from messages. Retrying...",
    "⚠️  Truncated tool call detected — retrying",
    "⚠️  Incomplete <REASONING_SCRATCHPAD> detected (opened but never closed)",
    "⏱️ Agent inactive for 10 min — no tool calls",
    "⚠️  Context length exceeded — using provider limit: 200,000 → 272,000 tokens",
    (
        "⚠️  Context length exceeded, but provider did not report a max context "
        "length; falling back to the configured limit."
    ),
]

# Operator-facing beyond compression: gateway back-pressure and host admin.
HERMES_OWNER_ONLY_EXTRA = [
    "⏳ Working — 3 min",
    "⏳ Queued for the next turn. Your message will be picked up shortly.",
    "⏳ Another turn is still running on this session. To interrupt, send /stop.",
    "⏳ Gateway is compacting and is not accepting another turn right now.",
    "⏳ Subagent working — your message is queued for the next turn.",
    "⚠️ **Command Approval Required**",
    "⚠ ⚠ Credits 90% used",
    "❌ Hermes update failed (exit code 1).",
    "⚠️  Some tools may not work due to missing requirements: playwright",
    "⚠️  Warning: API key appears invalid or missing",
    "⚠ Lightpanda fallback: Chrome was used for this browser action.",
    "🛑 [board] Kanban task-42 routed to TRIAGE",
]

# Provider/terminal failures: the owner sees the technical line, the customer
# gets one localized recovery notice instead.
HERMES_PROVIDER_FAILURES = [
    "❌ API failed after 3 retries — timeout",
    "❌ Billing or credits exhausted — top up to continue",
    "❌ Rate limited after 3 retries — provider 429",
    "❌ Connection to provider failed after 3 attempts",
    "❌ Provider returned an empty response stream after 3 retries",
    "❌ Non-retryable error (HTTP 401): unauthorized",
    "❌ Model returned no content after all retries",
    "❌ TLS certificate verification failed: self-signed cert",
    "⚠ no response from provider in 120s — aborting",
    "⏱️ The model provider is rate-limiting requests. Please wait a moment and try again.",
    "⚠️  The model declined to respond to this request",
]

# hermes-agent keeps these visible on chat surfaces on purpose (the operator
# must act on them). The owner DM still gets them; a customer never does.
HERMES_OWNER_ONLY_STATUSES = [
    "Compressed: 30 → 12 messages",
    "Compressed with fallback: 30 → 12 messages",
    "No changes from compression: 30 messages",
    "Compression aborted: 30 messages preserved",
    (
        "⚠ Compression returned an empty transcript. No session split was "
        "performed; conversation continues unchanged."
    ),
    (
        "⏳ Compression already in progress for this session "
        "(holder: pid=12345:tid=7:agent=1:nonce=ab). Please wait for it to finish."
    ),
    (
        "⏳ Compression skipped: could not acquire this session's compression "
        "lock. Another compression may still be running, or the lock check "
        "failed — try again shortly."
    ),
]


class TestHermesStatusInventory(unittest.TestCase):
    """Pinned upstream wording — nothing here may reach a Zalo chat."""

    def test_all_operational_statuses_dropped_for_everyone(self):
        for t in HERMES_OPERATIONAL_STATUSES:
            for is_owner in (False, True):
                d = classify(t, is_owner=is_owner)
                self.assertEqual(
                    d.action, FilterAction.DROP_OPERATIONAL, f"{t!r} owner={is_owner}"
                )
                self.assertEqual(d.cleaned_text, "")

    def test_owner_only_statuses_hidden_from_non_owner(self):
        for t in HERMES_OWNER_ONLY_STATUSES + HERMES_OWNER_ONLY_EXTRA:
            d = classify(t, is_owner=False)
            self.assertEqual(d.action, FilterAction.DROP_OPERATIONAL, t)
            self.assertEqual(d.cleaned_text, "")

    def test_owner_only_statuses_reach_owner_dm(self):
        for t in HERMES_OWNER_ONLY_STATUSES + HERMES_OWNER_ONLY_EXTRA:
            d = classify(t, is_owner=True)
            self.assertEqual(d.action, FilterAction.KEEP, t)
            self.assertEqual(d.cleaned_text, t)

    def test_provider_failures_become_recovery_notice_for_customer(self):
        for t in HERMES_PROVIDER_FAILURES:
            d = classify(t, is_owner=False)
            self.assertEqual(d.action, FilterAction.REPLACE_TERMINAL, t)
            self.assertEqual(d.cleaned_text, RECOVERY_NOTICE)
            self.assertIsNotNone(d.recovery_key)

    def test_provider_failures_stay_raw_for_owner(self):
        for t in HERMES_PROVIDER_FAILURES:
            d = classify(t, is_owner=True)
            self.assertEqual(d.action, FilterAction.KEEP, t)
            self.assertEqual(d.cleaned_text, t)

    def test_blocked_overflow_warning_becomes_recovery_for_customer(self):
        t = (
            "⚠ Context is over the compression threshold (~85,000 tokens >= "
            "72,000) but compression is currently blocked (cooldown:30). The "
            "model may stop responding. Run /new to start a fresh session or "
            "/compress to retry immediately."
        )
        d = classify(t, is_owner=False)
        self.assertEqual(d.action, FilterAction.REPLACE_TERMINAL)
        self.assertEqual(d.cleaned_text, RECOVERY_NOTICE)
        self.assertEqual(classify(t, is_owner=True).action, FilterAction.KEEP)

    def test_real_answer_next_to_leaked_status_survives(self):
        t = "✓ Context compaction complete — continuing turn...\nDạ combo 2 người là 350k ạ."
        d = classify(t)
        self.assertEqual(d.action, FilterAction.KEEP)
        self.assertEqual(d.cleaned_text, "Dạ combo 2 người là 350k ạ.")

    def test_dropping_is_idempotent(self):
        for t in (
            HERMES_OPERATIONAL_STATUSES
            + HERMES_OWNER_ONLY_STATUSES
            + HERMES_OWNER_ONLY_EXTRA
            + HERMES_PROVIDER_FAILURES
        ):
            first = classify(t).cleaned_text
            self.assertEqual(classify(first).cleaned_text, first, t)


class TestClassifyTerminal(unittest.TestCase):
    def test_context_exceeded_replaced(self):
        d = classify("Context length exceeded: 149,611 tokens. Cannot compress further.")
        self.assertEqual(d.action, FilterAction.REPLACE_TERMINAL)
        self.assertEqual(d.cleaned_text, RECOVERY_NOTICE)
        self.assertIn("context_exceeded", d.categories)
        self.assertIsNotNone(d.recovery_key)

    def test_cannot_compress_replaced(self):
        d = classify("Cannot compress further.")
        self.assertEqual(d.action, FilterAction.REPLACE_TERMINAL)


class TestClassifyMixed(unittest.TestCase):
    def test_real_answer_before_notice_preserved(self):
        t = "Dạ giá combo là 250k ạ.\n⚡ Interrupting current task. I'll respond to your message shortly."
        d = classify(t)
        self.assertEqual(d.action, FilterAction.KEEP)
        self.assertEqual(d.cleaned_text, "Dạ giá combo là 250k ạ.")

    def test_real_answer_after_notice_preserved(self):
        t = "Context too large (99,631 tokens).\nDạ menu quán mình có 12 món ạ."
        d = classify(t)
        self.assertEqual(d.action, FilterAction.KEEP)
        self.assertEqual(d.cleaned_text, "Dạ menu quán mình có 12 món ạ.")

    def test_answer_between_two_notices_preserved(self):
        t = "Interrupting current task\nDạ em gửi báo giá nha.\nCannot compress further."
        d = classify(t)
        self.assertEqual(d.action, FilterAction.KEEP)
        self.assertEqual(d.cleaned_text, "Dạ em gửi báo giá nha.")


class TestClassifyLegitimateContent(unittest.TestCase):
    """Common words like 'context'/'model' must NOT be dropped."""

    def test_legit_context_word_kept(self):
        for t in [
            "Trong ngữ cảnh (context) này, mình nên chọn gói nào ạ?",
            "Model điện thoại chị đang dùng là gì để em tư vấn ốp lưng ạ?",
            "Dạ bên em có mẫu website theo context ngành nhà hàng nha.",
            "Em không compress ảnh được thì gửi bản gốc cũng ok ạ.",
        ]:
            d = classify(t)
            self.assertEqual(d.action, FilterAction.KEEP, t)
            self.assertEqual(d.cleaned_text, t)

    def test_plain_answer_kept_verbatim(self):
        t = "Dạ shop mở cửa 8h-22h mỗi ngày ạ."
        d = classify(t)
        self.assertEqual(d.action, FilterAction.KEEP)
        self.assertEqual(d.cleaned_text, t)


class TestIdempotent(unittest.TestCase):
    def test_recovery_notice_is_stable(self):
        d1 = classify("Context length exceeded. Cannot compress further.")
        d2 = classify(d1.cleaned_text)
        self.assertEqual(d2.action, FilterAction.KEEP)
        self.assertEqual(d2.cleaned_text, RECOVERY_NOTICE)

    def test_cleaned_mixed_is_stable(self):
        d1 = classify("Dạ ok ạ.\nInterrupting current task")
        d2 = classify(d1.cleaned_text)
        self.assertEqual(d2.action, FilterAction.KEEP)
        self.assertEqual(d2.cleaned_text, "Dạ ok ạ.")


class TestRecoveryLimiter(unittest.TestCase):
    def test_one_per_key_within_ttl(self):
        lim = RecoveryNoticeLimiter(ttl=300)
        self.assertTrue(lim.should_emit("acct:chatA:corr1:context_exceeded", now=1000))
        self.assertFalse(lim.should_emit("acct:chatA:corr1:context_exceeded", now=1001))

    def test_reemits_after_ttl(self):
        lim = RecoveryNoticeLimiter(ttl=300)
        self.assertTrue(lim.should_emit("k", now=1000))
        self.assertFalse(lim.should_emit("k", now=1200))
        self.assertTrue(lim.should_emit("k", now=1400))  # past ttl

    def test_different_chats_independent(self):
        lim = RecoveryNoticeLimiter(ttl=300)
        self.assertTrue(lim.should_emit("acct:chatA:c:cat", now=1000))
        self.assertTrue(lim.should_emit("acct:chatB:c:cat", now=1000))

    def test_no_key_is_silent(self):
        lim = RecoveryNoticeLimiter()
        self.assertFalse(lim.should_emit(None, now=1000))
        self.assertFalse(lim.should_emit("", now=1000))

    def test_lru_cap(self):
        lim = RecoveryNoticeLimiter(ttl=10_000, max_size=2)
        lim.should_emit("k1", now=1)
        lim.should_emit("k2", now=1)
        lim.should_emit("k3", now=1)  # evicts k1
        # k1 evicted → emits again as if new
        self.assertTrue(lim.should_emit("k1", now=2))


class TestResolveNotice(unittest.TestCase):
    """Persona-aware canned notices: custom line wins only when safe."""

    ONGBUT = {"notices": {
        "soft_error": "Con chờ ta một chút, ta kiểm tra lại cho rõ rồi báo con ngay.",
        "recovery": "Ta đang hơi quá tải, con nhắn lại giúp ta câu vừa rồi nhé.",
    }}

    def test_custom_notice_used(self):
        self.assertEqual(
            resolve_notice(self.ONGBUT, "soft_error", "default"),
            "Con chờ ta một chút, ta kiểm tra lại cho rõ rồi báo con ngay.",
        )

    def test_fallback_when_missing(self):
        self.assertEqual(resolve_notice(self.ONGBUT, "deny_non_owner", "mặc định"), "mặc định")
        self.assertEqual(resolve_notice(None, "recovery", RECOVERY_NOTICE), RECOVERY_NOTICE)
        self.assertEqual(resolve_notice({}, "recovery", "d"), "d")
        self.assertEqual(resolve_notice({"notices": "not-a-dict"}, "recovery", "d"), "d")

    def test_rejects_empty_and_oversize(self):
        self.assertEqual(resolve_notice({"notices": {"recovery": "   "}}, "recovery", "d"), "d")
        self.assertEqual(
            resolve_notice({"notices": {"recovery": "x" * (MAX_NOTICE_LEN + 1)}}, "recovery", "d"),
            "d",
        )

    def test_rejects_notice_matching_operational_pattern(self):
        # A custom line that itself matches a filtered pattern would break
        # classify() idempotency — must fall back to the default.
        bad = {"notices": {"recovery": "Context length exceeded, con nhắn lại nhé"}}
        self.assertEqual(resolve_notice(bad, "recovery", "an toàn"), "an toàn")

    def test_custom_notice_is_classify_stable(self):
        v = resolve_notice(self.ONGBUT, "recovery", RECOVERY_NOTICE)
        d = classify(v)
        self.assertEqual(d.action, FilterAction.KEEP)
        self.assertEqual(d.cleaned_text, v)


class TestNameTriggers(unittest.TestCase):
    def test_parse_csv_lowercases_trims_dedups(self):
        self.assertEqual(
            parse_name_triggers("Ông Bụt, bụt ,  Ông Bụt "),
            ["ông bụt", "bụt"],
        )

    def test_parse_list_and_empty(self):
        self.assertEqual(parse_name_triggers(["Bụt", " "]), ["bụt"])
        self.assertEqual(parse_name_triggers(""), [])
        self.assertEqual(parse_name_triggers(None), [])
        self.assertEqual(parse_name_triggers(123), [])

    def test_match_substring_case_insensitive(self):
        trg = parse_name_triggers("ông bụt, bụt")
        self.assertTrue(text_has_name_trigger("Ông Bụt ơi cho hỏi", trg))
        self.assertTrue(text_has_name_trigger("nhờ ông bụt tí", trg))
        self.assertTrue(text_has_name_trigger("BỤT giúp con với", trg))

    def test_no_match_when_name_absent(self):
        trg = parse_name_triggers("ông bụt, bụt")
        self.assertFalse(text_has_name_trigger("ok anh", trg))
        self.assertFalse(text_has_name_trigger("alo alo", trg))

    def test_empty_triggers_never_match(self):
        # Rỗng = tắt: chỉ @tag mới kích hoạt, name-trigger không bao giờ đúng.
        self.assertFalse(text_has_name_trigger("ông bụt ơi", []))
        self.assertFalse(text_has_name_trigger("", ["bụt"]))


if __name__ == "__main__":
    unittest.main()
