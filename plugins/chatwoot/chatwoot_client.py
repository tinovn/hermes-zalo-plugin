"""Thin async client for the Chatwoot Application API.

Only the calls the adapter needs: post an outgoing message, toggle the
typing indicator. Uses httpx (already a Hermes dependency). Kept separate
from the adapter so the HTTP surface is easy to mock in tests.

Auth: Chatwoot Application API uses the ``api_access_token`` header carrying
an agent Access Token (Profile Settings -> Access Token).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:  # pragma: no cover - import guard
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


class ChatwootClient:
    """Minimal Application-API client scoped to one Chatwoot account."""

    def __init__(self, base_url: str, access_token: str, account_id: str):
        self._base = base_url.rstrip("/")
        self._token = (access_token or "").strip()
        self._account_id = str(account_id).strip()
        self._client: Optional["httpx.AsyncClient"] = None

    async def start(self) -> None:
        """Create the shared AsyncClient. Idempotent."""
        if self._client is None and HTTPX_AVAILABLE:
            self._client = httpx.AsyncClient(timeout=20.0)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> Dict[str, str]:
        return {"api_access_token": self._token, "Content-Type": "application/json"}

    def _conv_url(self, conversation_id: str, suffix: str = "") -> str:
        return (
            f"{self._base}/api/v1/accounts/{self._account_id}"
            f"/conversations/{conversation_id}{suffix}"
        )

    async def send_message(
        self, conversation_id: str, content: str
    ) -> Dict[str, Any]:
        """Post an outgoing (agent-side) message into a conversation.

        Chatwoot relays this out to the connected channel (Zalo via the
        bridge). The returned message surfaces as ``message_type=outgoing``
        on the next webhook — which the parser skips, preventing a loop.
        """
        if not self._client:
            raise RuntimeError("ChatwootClient not started")
        payload = {"content": content, "message_type": "outgoing", "private": False}
        resp = await self._client.post(
            self._conv_url(conversation_id, "/messages"),
            json=payload,
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    async def toggle_typing(self, conversation_id: str, on: bool) -> None:
        """Best-effort typing indicator; failures are logged, never raised."""
        if not self._client:
            return
        status = "on" if on else "off"
        try:
            await self._client.post(
                self._conv_url(conversation_id, "/toggle_typing_status"),
                json={"typing_status": status},
                headers=self._headers(),
            )
        except Exception as exc:  # typing is cosmetic — don't break the flow
            logger.debug("[chatwoot] toggle_typing failed: %s", exc)
