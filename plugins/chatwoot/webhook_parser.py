"""Parse Chatwoot webhook payloads and decide whether the bot should reply.

Pure functions, no Hermes/aiohttp imports — so the parsing + mute logic can
be unit-tested in isolation. The adapter wires these into the live gateway.

Chatwoot Agent Bot posts a JSON body to the bot's ``outgoing_url`` on each
event. We only act on ``message_created`` events that are *incoming*
(customer-sent) text, and only when the conversation is not being handled by
a human (auto-handoff) and not explicitly muted via a label.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ParsedMessage:
    """A normalized inbound customer message extracted from a webhook."""

    message_id: str
    conversation_id: str
    content: str
    contact_id: str
    contact_name: str
    status: str
    labels: List[str] = field(default_factory=list)


@dataclass
class SkipReason:
    """Why a webhook was ignored (carried for logging, never raised)."""

    reason: str


def _as_str(value: Any) -> str:
    """Coerce ids/values to a trimmed string ('' for None)."""
    if value is None:
        return ""
    return str(value).strip()


def _extract_labels(conversation: Dict[str, Any]) -> List[str]:
    """Pull conversation labels from the spots Chatwoot may place them.

    Chatwoot has surfaced labels under ``labels`` (newer) and
    ``additional_attributes.labels`` (older) at different times; read both
    so the mute-label check is robust across versions.
    """
    labels: List[str] = []
    raw = conversation.get("labels")
    if isinstance(raw, list):
        labels.extend(_as_str(x) for x in raw)
    extra = conversation.get("additional_attributes")
    if isinstance(extra, dict):
        raw2 = extra.get("labels")
        if isinstance(raw2, list):
            labels.extend(_as_str(x) for x in raw2)
    return [x for x in labels if x]


def parse_webhook(body: Dict[str, Any]) -> ParsedMessage | SkipReason:
    """Turn a raw Chatwoot webhook body into a ParsedMessage or a SkipReason.

    Only ``message_created`` + ``message_type == incoming`` text messages are
    actionable. Everything else (outgoing echoes, private notes, activity
    events, template/bot messages, empty bodies) is skipped — this is the
    first line of anti-loop defense: the bot never reacts to its own sends.
    """
    if not isinstance(body, dict):
        return SkipReason("body_not_object")

    event = _as_str(body.get("event"))
    if event != "message_created":
        return SkipReason(f"event_{event or 'missing'}")

    # message_type: 0/"incoming" = customer, 1/"outgoing" = agent/bot.
    msg_type = body.get("message_type")
    if msg_type not in ("incoming", 0):
        return SkipReason(f"message_type_{msg_type}")

    # Private notes are agent-only; never customer-facing input.
    if body.get("private") is True:
        return SkipReason("private_note")

    content = _as_str(body.get("content"))
    if not content:
        return SkipReason("empty_content")

    conversation = body.get("conversation")
    if not isinstance(conversation, dict):
        return SkipReason("no_conversation")

    conversation_id = _as_str(conversation.get("id"))
    if not conversation_id:
        return SkipReason("no_conversation_id")

    sender = body.get("sender") if isinstance(body.get("sender"), dict) else {}
    contact_id = _as_str(sender.get("id"))
    contact_name = _as_str(sender.get("name")) or contact_id or f"contact:{conversation_id}"

    return ParsedMessage(
        message_id=_as_str(body.get("id")) or f"{conversation_id}:{content[:16]}",
        conversation_id=conversation_id,
        content=content,
        contact_id=contact_id,
        contact_name=contact_name,
        status=_as_str(conversation.get("status")),
        labels=_extract_labels(conversation),
    )


def should_reply(
    msg: ParsedMessage,
    *,
    reply_statuses: List[str],
    mute_label: str,
) -> Optional[str]:
    """Return a skip-reason string if the bot must stay silent, else None.

    Two independent mute mechanisms (user chose "both"):

    1. Auto-handoff by status — the bot only speaks while the conversation
       sits in an allowed status (default: ``pending``). The moment a human
       agent replies or assigns, Chatwoot flips it to ``open`` and the bot
       falls silent on its own.
    2. Manual label override — a ``mute-ai`` label mutes the bot even while
       the conversation is still in an otherwise-allowed status.
    """
    if mute_label and mute_label in msg.labels:
        return "muted_by_label"
    # An empty status (some payloads omit it) is treated as allowed so a
    # first inbound on a brand-new conversation still gets a reply.
    if msg.status and reply_statuses and msg.status not in reply_statuses:
        return f"status_{msg.status}_not_in_reply_statuses"
    return None
