from __future__ import annotations

from dataclasses import dataclass
from threading import Event
from typing import Callable, Protocol

from .errors import OperationCancelled


@dataclass(frozen=True)
class ProgressUpdate:
    stage: str
    current: int | None = None
    total: int | None = None
    message: str = ""


ProgressCallback = Callable[[ProgressUpdate], None]


class CancellationToken:
    def __init__(self, event: object | None = None) -> None:
        self._event = Event() if event is None else event

    def cancel(self) -> None:
        self._event.set()  # type: ignore[attr-defined]

    @property
    def cancelled(self) -> bool:
        return bool(self._event.is_set())  # type: ignore[attr-defined]

    def check(self) -> None:
        if self.cancelled:
            raise OperationCancelled("Operation cancelled.")


class SupportsProgress(Protocol):
    def __call__(self, update: ProgressUpdate) -> None: ...


def report(progress: ProgressCallback | None, update: ProgressUpdate) -> None:
    if progress is not None:
        progress(update)
