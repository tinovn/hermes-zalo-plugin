"""Chatwoot Agent Bot platform adapter (Hermes plugin).

Runs an aiohttp webhook receiver that Chatwoot's Agent Bot posts inbound
customer messages to. Each actionable message is dispatched to the Hermes
agent via ``handle_message``; the agent's reply is sent back through the
Chatwoot Application API (``message_type=outgoing``), which Chatwoot relays
out to the original channel — for CSKH Tino that is Zalo via zca-bridge.

Architecture (no edits to core Hermes — plugin path):

    Zalo customer -> zca-bridge -> Chatwoot inbox
                                      | Agent Bot webhook (this adapter)
                                      v
                                 Hermes agent
                                      | Chatwoot API (outgoing)
                                      v
                       Chatwoot outgoing webhook -> zca-bridge -> Zalo

Mute / human handoff (both mechanisms, per product decision):
  * status-based: bot only replies while the conversation status is in
    CHATWOOT_BOT_REPLY_STATUSES (default: pending). A human reply flips the
    status to "open" and the bot goes silent automatically.
  * label-based: a CHATWOOT_MUTE_LABEL ("mute-ai") on the conversation mutes
    the bot manually even while still pending.

Anti-loop: the adapter only acts on incoming (customer) messages; the bot's
own outgoing sends come back as outgoing webhooks and are skipped.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover - import guard
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

from .chatwoot_client import ChatwootClient, HTTPX_AVAILABLE
from .webhook_parser import ParsedMessage, SkipReason, parse_webhook, should_reply

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8088
DEFAULT_MUTE_LABEL = "mute-ai"
DEFAULT_REPLY_STATUSES = "pending"
MAX_MESSAGE_LENGTH = 8000  # Chatwoot has no hard cap; keep replies sane
DEDUP_WINDOW_SECONDS = 300
DEDUP_MAX_SIZE = 2000


def check_requirements() -> bool:
    """Both aiohttp (receiver) and httpx (sender) must be importable."""
    return AIOHTTP_AVAILABLE and HTTPX_AVAILABLE


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


class ChatwootAdapter(BasePlatformAdapter):
    """Chatwoot Agent Bot receiver + Application-API sender."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):
        platform = Platform("chatwoot")
        super().__init__(config=config, platform=platform)

        extra = config.extra or {}

        def _cfg(key: str, env: str, default: str = "") -> str:
            return str(extra.get(key) or os.getenv(env, default)).strip()

        self._base_url = _cfg("base_url", "CHATWOOT_BASE_URL").rstrip("/")
        self._token = _cfg("api_access_token", "CHATWOOT_API_ACCESS_TOKEN")
        self._account_id = _cfg("account_id", "CHATWOOT_ACCOUNT_ID", "1")
        self._webhook_secret = _cfg("webhook_secret", "CHATWOOT_WEBHOOK_SECRET")
        self._host = _cfg("host", "CHATWOOT_PLUGIN_HOST", DEFAULT_HOST)
        self._port = int(_cfg("port", "CHATWOOT_PLUGIN_PORT", str(DEFAULT_PORT)) or DEFAULT_PORT)
        self._mute_label = _cfg("mute_label", "CHATWOOT_MUTE_LABEL", DEFAULT_MUTE_LABEL)
        self._reply_statuses = [
            s.strip()
            for s in _cfg("reply_statuses", "CHATWOOT_BOT_REPLY_STATUSES", DEFAULT_REPLY_STATUSES).split(",")
            if s.strip()
        ]

        self._client = ChatwootClient(self._base_url, self._token, self._account_id)
        self._runner: Optional["web.AppRunner"] = None
        self._seen_messages: Dict[str, float] = {}

    # -- Connection lifecycle ----------------------------------------------

    async def connect(self) -> bool:
        if not check_requirements():
            logger.warning("[chatwoot] aiohttp/httpx not installed")
            return False
        if not (self._base_url and self._token and self._account_id):
            logger.warning("[chatwoot] missing base_url/token/account_id")
            return False

        await self._client.start()

        app = web.Application()
        app.router.add_get("/health", self._handle_health)
        app.router.add_post("/chatwoot/webhook", self._handle_webhook)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        try:
            await site.start()
        except OSError as exc:
            logger.error("[chatwoot] cannot bind %s:%d — %s", self._host, self._port, exc)
            return False

        self._mark_connected()
        logger.info(
            "[chatwoot] webhook receiver on %s:%d (reply_statuses=%s, mute_label=%s)",
            self._host, self._port, self._reply_statuses, self._mute_label,
        )
        return True

    async def disconnect(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        await self._client.close()
        self._mark_disconnected()
        logger.info("[chatwoot] disconnected")

    # -- Webhook receiver ---------------------------------------------------

    async def _handle_health(self, request) -> "web.Response":
        return web.json_response({"status": "ok", "platform": "chatwoot"})

    def _verify_secret(self, request) -> bool:
        """Constant-time check of the shared webhook secret, if configured."""
        if not self._webhook_secret:
            return True  # no secret set — accept (rely on loopback bind)
        sent = request.headers.get("X-Chatwoot-Webhook-Token", "")
        return hmac.compare_digest(sent, self._webhook_secret)

    async def _handle_webhook(self, request) -> "web.Response":
        if not self._verify_secret(request):
            logger.warning("[chatwoot] rejected webhook: bad secret")
            return web.json_response({"error": "unauthorized"}, status=401)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad_json"}, status=400)

        parsed = parse_webhook(body)
        if isinstance(parsed, SkipReason):
            logger.debug("[chatwoot] skip: %s", parsed.reason)
            return web.json_response({"ok": True, "ignored": parsed.reason})

        if self._is_duplicate(parsed.message_id):
            return web.json_response({"ok": True, "ignored": "duplicate"})

        mute = should_reply(
            parsed, reply_statuses=self._reply_statuses, mute_label=self._mute_label
        )
        if mute:
            logger.info("[chatwoot] muted conv=%s reason=%s", parsed.conversation_id, mute)
            return web.json_response({"ok": True, "muted": mute})

        # Ack fast, run the agent in the background — Chatwoot expects a quick
        # 200 and the agent run can take seconds.
        asyncio.create_task(self._dispatch(parsed))
        return web.json_response({"ok": True})

    async def _dispatch(self, msg: ParsedMessage) -> None:
        """Build a MessageEvent and hand it to the gateway."""
        try:
            source = self.build_source(
                chat_id=msg.conversation_id,
                chat_name=f"conv:{msg.conversation_id}",
                chat_type="dm",
                user_id=msg.contact_id or msg.conversation_id,
                user_name=msg.contact_name,
            )
            event = MessageEvent(
                text=msg.content,
                message_type=MessageType.TEXT,
                source=source,
                message_id=msg.message_id,
                raw_message=msg.__dict__,
                timestamp=datetime.now(tz=timezone.utc),
            )
            await self.handle_message(event)
        except Exception as exc:
            logger.error("[chatwoot] dispatch failed conv=%s: %s", msg.conversation_id, exc)

    def _is_duplicate(self, msg_id: str) -> bool:
        now = time.time()
        if len(self._seen_messages) > DEDUP_MAX_SIZE:
            cutoff = now - DEDUP_WINDOW_SECONDS
            self._seen_messages = {k: v for k, v in self._seen_messages.items() if v > cutoff}
        if msg_id in self._seen_messages:
            return True
        self._seen_messages[msg_id] = now
        return False

    # -- Outbound -----------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Post the agent reply into the Chatwoot conversation (=chat_id)."""
        if len(content) > self.MAX_MESSAGE_LENGTH:
            logger.warning("[chatwoot] truncating reply %d->%d", len(content), self.MAX_MESSAGE_LENGTH)
            content = content[: self.MAX_MESSAGE_LENGTH]
        try:
            created = await self._client.send_message(chat_id, content)
            return SendResult(success=True, message_id=str(created.get("id", "")))
        except Exception as exc:
            logger.error("[chatwoot] send failed conv=%s: %s", chat_id, exc)
            return SendResult(success=False, error=str(exc))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        await self._client.toggle_typing(chat_id, on=True)

    async def send_image(self, chat_id: str, image_url: str, caption: str = "") -> SendResult:
        """No native image upload yet — send caption + link as text."""
        text = f"{caption}\n{image_url}".strip() if caption else image_url
        return await self.send(chat_id, text)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": f"conv:{chat_id}", "type": "dm", "chat_id": chat_id}


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def _env_enablement() -> dict | None:
    """Seed PlatformConfig.extra from env so env-only setups show in status."""
    base_url = os.getenv("CHATWOOT_BASE_URL", "").strip()
    token = os.getenv("CHATWOOT_API_ACCESS_TOKEN", "").strip()
    if not (base_url and token):
        return None
    seed: dict = {
        "base_url": base_url.rstrip("/"),
        "api_access_token": token,
        "account_id": os.getenv("CHATWOOT_ACCOUNT_ID", "1").strip(),
        "host": os.getenv("CHATWOOT_PLUGIN_HOST", DEFAULT_HOST).strip(),
        "port": os.getenv("CHATWOOT_PLUGIN_PORT", str(DEFAULT_PORT)).strip(),
        "mute_label": os.getenv("CHATWOOT_MUTE_LABEL", DEFAULT_MUTE_LABEL).strip(),
        "reply_statuses": os.getenv("CHATWOOT_BOT_REPLY_STATUSES", DEFAULT_REPLY_STATUSES).strip(),
    }
    secret = os.getenv("CHATWOOT_WEBHOOK_SECRET", "").strip()
    if secret:
        seed["webhook_secret"] = secret
    home = os.getenv("CHATWOOT_HOME_CHANNEL", "").strip()
    if home:
        seed["home_channel"] = {"chat_id": home, "name": f"conv:{home}"}
    return seed


def validate_config(config: PlatformConfig) -> bool:
    """True when the platform is minimally configured (base_url + token).

    The core treats a falsy return as "misconfigured" (``if not
    entry.validate_config(config)``), so this must return a bool, not a
    list of errors.
    """
    extra = config.extra or {}
    has_url = bool(extra.get("base_url") or os.getenv("CHATWOOT_BASE_URL"))
    has_token = bool(
        extra.get("api_access_token") or os.getenv("CHATWOOT_API_ACCESS_TOKEN")
    )
    return has_url and has_token


def is_connected(config: PlatformConfig) -> bool:
    extra = config.extra or {}
    return bool(
        (extra.get("base_url") or os.getenv("CHATWOOT_BASE_URL"))
        and (extra.get("api_access_token") or os.getenv("CHATWOOT_API_ACCESS_TOKEN"))
    )


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Out-of-process send for cron / send_message_tool fallbacks."""
    if not HTTPX_AVAILABLE:
        return {"error": "chatwoot standalone send: httpx not installed"}
    extra = (pconfig.extra or {}) if pconfig else {}
    base_url = (extra.get("base_url") or os.getenv("CHATWOOT_BASE_URL", "")).rstrip("/")
    token = extra.get("api_access_token") or os.getenv("CHATWOOT_API_ACCESS_TOKEN", "")
    account_id = extra.get("account_id") or os.getenv("CHATWOOT_ACCOUNT_ID", "1")
    if not (base_url and token):
        return {"error": "chatwoot not configured"}
    client = ChatwootClient(base_url, token, account_id)
    await client.start()
    try:
        created = await client.send_message(chat_id, message)
        return {"ok": True, "message_id": created.get("id")}
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        await client.close()


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system at startup."""
    ctx.register_platform(
        name="chatwoot",
        label="Chatwoot",
        adapter_factory=lambda cfg: ChatwootAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["CHATWOOT_BASE_URL", "CHATWOOT_API_ACCESS_TOKEN", "CHATWOOT_ACCOUNT_ID"],
        install_hint="pip install aiohttp httpx   # both already Hermes deps",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="CHATWOOT_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="CHATWOOT_ALLOWED_USERS",
        allow_all_env="CHATWOOT_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="💬",
    )
