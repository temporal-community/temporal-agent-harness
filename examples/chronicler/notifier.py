"""Pluggable notification channel for "your long job is ready" pings.

Transcribing a multi-hour session is a genuinely long, durable activity — the kind of work you
kick off and walk away from. When it finishes, the agent should be able to *ping* you. This
module is the seam that makes the channel swappable:

  * :class:`Notifier` — the interface: one ``notify(...)`` coroutine.
  * :class:`InAppNotifier` — the default. It delivers *in-band*: the ``notify`` tool call is
    itself visible in the UI as a tool lifecycle event, and the agent echoes the message in its
    reply. No external setup, works the moment you run the example.
  * :class:`WebhookNotifier` — a real out-of-band push (Slack/Discord/any webhook). Selected by
    env, it proves the interface is honest: the same durable completion can fire a real HTTP
    POST so you get pinged with your laptop closed.

``get_notifier()`` picks the implementation from ``CHRONICLER_NOTIFIER`` at call time (on the
worker, inside the activity), so switching channels is config, not code — no agent or workflow
change. Adding a third channel (desktop, email, SMS) is a new :class:`Notifier` here and nothing
else.

Runs worker-side (activities aren't determinism-constrained), so real I/O is fine.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request
from typing import Protocol

from .chronicler_models import NotificationResult, NotifyRequest

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    """A notification channel. The only contract: deliver a title+message, report the outcome."""

    async def notify(self, request: NotifyRequest) -> NotificationResult: ...


class InAppNotifier:
    """Default channel: deliver in-band. The ``notify`` tool call surfaces in the UI as a tool
    event, and the agent is prompted to echo the message in its reply — so the notification is
    visible without any external service. We just log it and report delivered."""

    channel = "in-app"

    async def notify(self, request: NotifyRequest) -> NotificationResult:
        logger.info("[notify:in-app] %s — %s", request.title, request.message)
        return NotificationResult(
            delivered=True,
            channel=self.channel,
            title=request.title,
            message=request.message,
        )


class WebhookNotifier:
    """Out-of-band channel: POST ``{"text": "<title>: <message>"}`` to a webhook URL (Slack /
    Discord / anything that accepts a JSON body). Proves the pluggable seam is real — the same
    durable transcription completion can ping you off-machine."""

    channel = "webhook"

    def __init__(self, url: str) -> None:
        self._url = url

    async def notify(self, request: NotifyRequest) -> NotificationResult:
        payload = json.dumps({"text": f"{request.title}: {request.message}"}).encode()

        def _post() -> int:
            req = urllib.request.Request(
                self._url, data=payload, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 (config'd URL)
                return resp.status

        try:
            status = await asyncio.to_thread(_post)
            delivered = 200 <= status < 300
            if not delivered:
                logger.warning("[notify:webhook] non-2xx status %s", status)
        except Exception as e:  # network failures shouldn't crash the turn
            logger.warning("[notify:webhook] delivery failed: %s", e)
            delivered = False

        return NotificationResult(
            delivered=delivered,
            channel=self.channel,
            title=request.title,
            message=request.message,
        )


def get_notifier() -> Notifier:
    """Resolve the notifier from the environment (evaluated worker-side, per call).

    ``CHRONICLER_NOTIFIER=webhook`` + ``CHRONICLER_WEBHOOK_URL=...`` selects the webhook channel;
    anything else (default) uses the in-app channel. Kept env-driven so the channel is a
    deployment choice, matching how the audio activities read ``GEMINI_API_KEY``."""
    choice = os.environ.get("CHRONICLER_NOTIFIER", "inapp").strip().lower()
    if choice == "webhook":
        url = os.environ.get("CHRONICLER_WEBHOOK_URL", "").strip()
        if url:
            return WebhookNotifier(url)
        logger.warning(
            "CHRONICLER_NOTIFIER=webhook but CHRONICLER_WEBHOOK_URL is unset; "
            "falling back to in-app notifications."
        )
    return InAppNotifier()
