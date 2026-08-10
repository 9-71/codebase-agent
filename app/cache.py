from dataclasses import dataclass
import time
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass
class _Entry(Generic[T]):
    value: T
    expires_at: float


class TTLCache(Generic[T]):
    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._items: dict[str, _Entry[T]] = {}

    def get(self, key: str) -> T | None:
        entry = self._items.get(key)
        if entry is None:
            return None
        if entry.expires_at <= time.monotonic():
            self._items.pop(key, None)
            return None
        return entry.value

    def put(self, key: str, value: T) -> None:
        self._items[key] = _Entry(value, time.monotonic() + self._ttl)
