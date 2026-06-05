"""Unit tests for the Chatwoot webhook parser + mute logic.

Pure logic, no network — run with: python3 -m pytest plugins/chatwoot/ -q
or standalone: python3 plugins/chatwoot/test_webhook_parser.py
"""

from webhook_parser import ParsedMessage, SkipReason, parse_webhook, should_reply


def _incoming(content="xin chào", status="pending", labels=None, msg_id="100"):
    """Build a minimal valid incoming message_created webhook body."""
    conv = {"id": 42, "status": status}
    if labels is not None:
        conv["labels"] = labels
    return {
        "event": "message_created",
        "message_type": "incoming",
        "id": msg_id,
        "content": content,
        "private": False,
        "conversation": conv,
        "sender": {"id": 7, "name": "Khách A"},
    }


# -- parse_webhook ----------------------------------------------------------

def test_parse_valid_incoming():
    r = parse_webhook(_incoming())
    assert isinstance(r, ParsedMessage)
    assert r.conversation_id == "42"
    assert r.content == "xin chào"
    assert r.contact_id == "7"
    assert r.contact_name == "Khách A"
    assert r.status == "pending"


def test_skip_outgoing():
    body = _incoming()
    body["message_type"] = "outgoing"
    assert isinstance(parse_webhook(body), SkipReason)


def test_skip_outgoing_numeric():
    body = _incoming()
    body["message_type"] = 1
    assert isinstance(parse_webhook(body), SkipReason)


def test_accept_incoming_numeric():
    body = _incoming()
    body["message_type"] = 0
    assert isinstance(parse_webhook(body), ParsedMessage)


def test_skip_private_note():
    body = _incoming()
    body["private"] = True
    assert isinstance(parse_webhook(body), SkipReason)


def test_skip_non_message_event():
    body = _incoming()
    body["event"] = "conversation_updated"
    assert isinstance(parse_webhook(body), SkipReason)


def test_skip_empty_content():
    body = _incoming(content="")
    assert isinstance(parse_webhook(body), SkipReason)


def test_skip_missing_conversation():
    body = _incoming()
    del body["conversation"]
    assert isinstance(parse_webhook(body), SkipReason)


def test_labels_from_additional_attributes():
    body = _incoming()
    body["conversation"].pop("labels", None)
    body["conversation"]["additional_attributes"] = {"labels": ["vip", "mute-ai"]}
    r = parse_webhook(body)
    assert isinstance(r, ParsedMessage)
    assert "mute-ai" in r.labels


# -- should_reply (mute logic) ---------------------------------------------

def test_reply_when_pending_no_label():
    msg = parse_webhook(_incoming(status="pending"))
    assert should_reply(msg, reply_statuses=["pending"], mute_label="mute-ai") is None


def test_mute_when_status_open():
    msg = parse_webhook(_incoming(status="open"))
    reason = should_reply(msg, reply_statuses=["pending"], mute_label="mute-ai")
    assert reason and "open" in reason


def test_mute_when_resolved():
    msg = parse_webhook(_incoming(status="resolved"))
    assert should_reply(msg, reply_statuses=["pending"], mute_label="mute-ai") is not None


def test_mute_by_label_even_if_pending():
    msg = parse_webhook(_incoming(status="pending", labels=["mute-ai"]))
    assert should_reply(msg, reply_statuses=["pending"], mute_label="mute-ai") == "muted_by_label"


def test_reply_when_status_empty():
    # Some payloads omit status; a brand-new conversation should still reply.
    msg = parse_webhook(_incoming(status=""))
    assert should_reply(msg, reply_statuses=["pending"], mute_label="mute-ai") is None


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        passed += 1
        print(f"  ✓ {fn.__name__}")
    print(f"\n{passed}/{len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
