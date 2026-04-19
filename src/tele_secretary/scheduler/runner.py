"""Reminder scheduler skeleton.

Phase 0 only establishes the boundary. Reminder polling and delivery are part
of later phases.
"""

from __future__ import annotations

import logging


class Scheduler:
    def __init__(self) -> None:
        self._logger = logging.getLogger(__name__)

    def start(self) -> None:
        self._logger.info("Scheduler skeleton started.")

    def stop(self) -> None:
        self._logger.info("Scheduler skeleton stopped.")
