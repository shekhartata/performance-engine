"""Per-test-case connection pool size and checkout statistics."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any

from pymongo import MongoClient, monitoring


def required_pool_size(concurrency: int, parallel_steps: int, headroom: int = 4) -> int:
    """Connections a test case can check out at once, plus a small spare."""
    return max(1, int(concurrency)) * max(1, int(parallel_steps)) + max(0, int(headroom))


def pool_formula(concurrency: int, parallel_steps: int, headroom: int) -> str:
    size = required_pool_size(concurrency, parallel_steps, headroom)
    return (
        f"maxPoolSize={size} (derived: concurrency {int(concurrency)} "
        f"× parallel steps {max(1, int(parallel_steps))} + headroom {int(headroom)})"
    )


class PoolMonitor(monitoring.ConnectionPoolListener):
    """Records checkout wait and peak in-use connections while a test case is measuring.

    PyMongo's listener base raises NotImplementedError, so every event method is
    implemented. Unused events are ignored.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.in_use = 0
        self.measuring = False
        self.peak = 0
        self.created = 0
        self.waits_ms: list[float] = []

    def pool_created(self, event: Any) -> None:
        return None

    def pool_ready(self, event: Any) -> None:
        return None

    def pool_cleared(self, event: Any) -> None:
        return None

    def pool_closed(self, event: Any) -> None:
        return None

    def connection_ready(self, event: Any) -> None:
        return None

    def connection_closed(self, event: Any) -> None:
        return None

    def connection_check_out_started(self, event: Any) -> None:
        return None

    def connection_check_out_failed(self, event: Any) -> None:
        return None

    def begin_measurement(self) -> None:
        with self._lock:
            self.measuring = True
            self.peak = self.in_use
            self.created = 0
            self.waits_ms = []

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self.measuring = False
            waits = list(self.waits_ms)
            peak = self.peak
            created = self.created
        return {
            "peak_in_use": peak,
            "connections_created": created,
            "wait_p50_ms": _percentile(waits, 50),
            "wait_p95_ms": _percentile(waits, 95),
            "wait_max_ms": max(waits) if waits else 0.0,
            "checkouts": len(waits),
        }

    def connection_checked_out(self, event: Any) -> None:
        with self._lock:
            self.in_use += 1
            if self.measuring:
                self.peak = max(self.peak, self.in_use)
                self.waits_ms.append(_duration_ms(getattr(event, "duration", 0)))

    def connection_checked_in(self, event: Any) -> None:  # noqa: ARG002
        with self._lock:
            self.in_use = max(0, self.in_use - 1)

    def connection_created(self, event: Any) -> None:  # noqa: ARG002
        with self._lock:
            if self.measuring:
                self.created += 1


@dataclass
class SizedClientCache:
    """One MongoClient per pool size. Reconnecting only happens when the size changes."""

    uri: str
    server_selection_timeout_ms: int = 15_000
    clients: dict[int, MongoClient] = field(default_factory=dict)
    monitors: dict[int, PoolMonitor] = field(default_factory=dict)

    def client(self, size: int) -> tuple[MongoClient, PoolMonitor]:
        size = max(1, int(size))
        if size not in self.clients:
            from perf_envelope.environment.mongodb import open_client

            monitor = PoolMonitor()
            client = open_client(
                self.uri,
                max_pool_size=size,
                min_pool_size=size,
                server_selection_timeout_ms=self.server_selection_timeout_ms,
                event_listeners=[monitor],
            )
            prefill_pool(client, size)
            self.clients[size] = client
            self.monitors[size] = monitor
        return self.clients[size], self.monitors[size]

    def close(self) -> None:
        for client in self.clients.values():
            client.close()
        self.clients.clear()
        self.monitors.clear()


def prefill_pool(client: MongoClient, size: int) -> None:
    """Check out `size` connections before measurement so setup is not query latency."""

    def ping(_: int) -> None:
        client.admin.command("ping")

    workers = max(1, size)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(ping, index) for index in range(workers)]
        wait(futures)
        for future in futures:
            future.result()


def measure_rtt_ms(client: MongoClient, samples: int = 5) -> float:
    timings: list[float] = []
    for _ in range(max(1, samples)):
        started = time.perf_counter()
        client.admin.command("ping")
        timings.append((time.perf_counter() - started) * 1000)
    timings.sort()
    return timings[len(timings) // 2]


def _duration_ms(duration: Any) -> float:
    if hasattr(duration, "total_seconds"):
        return float(duration.total_seconds()) * 1000
    return float(duration) * 1000 if duration and float(duration) < 1000 else float(duration or 0)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((pct / 100) * (len(ordered) - 1)))))
    return float(ordered[index])
