"""Failover notifier adapters.

The default :class:`LoggingFailoverNotifier` just logs; the server wires a
:class:`CallbackFailoverNotifier` whose callback pushes an SSE event onto
the affected session's stream. Notifiers must never raise — a failed
notification must not break detection/failover.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from omnigent.cswap.application.ports.ports import FailoverEvent, FailoverNotifier

logger = logging.getLogger(__name__)


class LoggingFailoverNotifier(FailoverNotifier):
    """Notifier that records failover events to the log."""

    def notify(self, event: FailoverEvent) -> None:
        """Log *event* at INFO."""
        logger.info(
            "cswap failover: session=%s exhausted=%s next-launch=%s mode=%s",
            event.session_id,
            event.exhausted_credential_id,
            event.next_credential_id,
            event.mode,
        )


class CallbackFailoverNotifier(FailoverNotifier):
    """Notifier that forwards events to a callback (e.g. SSE emit)."""

    def __init__(self, callback: Callable[[FailoverEvent], None]) -> None:
        """:param callback: Invoked with each event; exceptions are swallowed."""
        self._callback = callback

    def notify(self, event: FailoverEvent) -> None:
        """Invoke the callback, logging (not raising) on failure."""
        try:
            self._callback(event)
        except Exception:
            logger.exception("cswap failover notification callback failed")
