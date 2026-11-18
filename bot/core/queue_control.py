"""Generic async queue control primitives.

This module is intentionally standalone: callers must explicitly register and
check the cancellation event. It does not start, download, upload, or publish jobs.
"""
import asyncio
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class QueueControl:
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_task: Optional[asyncio.Task] = None
    pending: list[Any] = field(default_factory=list)
    cancelled: bool = False


class QueueControlRegistry:
    def __init__(self) -> None:
        self._queues: dict[str, QueueControl] = {}

    def get_or_create(self, name: str) -> QueueControl:
        return self._queues.setdefault(name, QueueControl())

    def register(self, name: str, *, active_task=None, pending=None) -> QueueControl:
        control = self.get_or_create(name)
        control.cancel_event.clear()
        control.cancelled = False
        control.active_task = active_task
        control.pending = list(pending or [])
        return control

    async def request_cancel(self, name: str) -> bool:
        control = self._queues.get(name)
        if control is None:
            return False
        async with control.lock:
            control.cancelled = True
            control.cancel_event.set()
            control.pending.clear()
        return True

    def is_cancel_requested(self, name: str) -> bool:
        control = self._queues.get(name)
        return bool(control and control.cancel_event.is_set())

    def unregister(self, name: str) -> None:
        control = self._queues.get(name)
        if control and control.active_task is None:
            self._queues.pop(name, None)


queue_controls = QueueControlRegistry()
