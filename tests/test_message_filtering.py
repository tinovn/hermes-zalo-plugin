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
