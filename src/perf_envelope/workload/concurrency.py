"""Closed-loop concurrent workload execution."""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any

from pymongo.collection import Collection

from perf_envelope.config.models import QueryConfig, WorkloadConfig
from perf_envelope.query.parameters import QueryParameterGenerator
from perf_envelope.telemetry.latency import summarize_latencies
from perf_envelope.workload.executor import QueryResult, execute_once
from perf_envelope.workload.warmup import warmup as run_warmup


@dataclass
class WorkloadMetrics:
    latencies_ms: list[float] = field(default_factory=list)
    successes: int = 0
    errors: int = 0
    timeouts: int = 0
    returned_total: int = 0
    duration_seconds: float = 0.0
    step_samples: list[list[float]] = field(default_factory=list)

    @property
    def qps(self) -> float:
        if self.duration_seconds <= 0:
            return 0.0
        return self.successes / self.duration_seconds

    def as_dict(self) -> dict[str, Any]:
        summary = summarize_latencies(self.latencies_ms)
        total = self.successes + self.errors
        return {
            **summary,
            "qps": self.qps,
            "successes": self.successes,
            "errors": self.errors,
            "timeouts": self.timeouts,
            "failed_requests": self.errors,
            "error_rate": (self.errors / total) if total else 0.0,
            "returned_total": self.returned_total,
            "duration_seconds": self.duration_seconds,
            "samples": len(self.latencies_ms),
            "step_ms_mean": _mean_steps(self.step_samples),
        }


class WorkloadExecutor:
    def __init__(
        self,
        collection: Collection,
        query: QueryConfig,
        workload: WorkloadConfig,
        param_gen: QueryParameterGenerator,
        selectivity: float | None = None,
        plan: Any = None,
        database: Any = None,
    ):
        self.collection = collection
        self.query = query
        self.workload = workload
        self.param_gen = param_gen
        self.selectivity = selectivity
        self.plan = plan
        self.database = database
        self.sample_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    def run(
        self,
        *,
        concurrency: int | None = None,
        include_warmup: bool = True,
        request_count: int | None = None,
        duration_seconds: int | None = None,
    ) -> WorkloadMetrics:
        workers = max(1, concurrency or self.workload.concurrency)
        if include_warmup:
            self.warmup(workers)
        duration = duration_seconds if duration_seconds is not None else self.workload.duration_seconds
        total_requests = request_count if request_count is not None else self.workload.request_count
        mode = self.workload.mode
        if total_requests and mode != "fixed_duration":
            return self._run_count(workers, total_requests)
        return self._run_duration(workers, duration or 10)

    def warmup(self, concurrency: int | None = None) -> None:
        times = self.workload.warmup_queries
        workers = max(1, concurrency or self.workload.concurrency)
        run_warmup(times * workers, self._one)

    def _one(self) -> QueryResult:
        params = self.param_gen.next(self.selectivity)
        for key, value in params.items():
            self.sample_counts[key][str(value)[:120]] += 1
        if self.plan is not None and self.database is not None:
            from perf_envelope.workload.plan import execute_plan

            return execute_plan(self.database, self.plan, params, self.workload.timeout_ms)
        return execute_once(self.collection, self.query, params, self.workload.timeout_ms)

    def _run_duration(self, workers: int, duration: int) -> WorkloadMetrics:
        stop = threading.Event()
        metrics = WorkloadMetrics()
        lock = threading.Lock()

        def worker() -> None:
            while not stop.is_set():
                result = self._one()
                _record(metrics, lock, result)
                if self.workload.think_time_ms:
                    time.sleep(self.workload.think_time_ms / 1000)

        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(worker) for _ in range(workers)]
            time.sleep(max(duration, 0))
            stop.set()
            wait(futures)
        metrics.duration_seconds = time.perf_counter() - t0
        return metrics

    def _run_count(self, workers: int, total_requests: int) -> WorkloadMetrics:
        metrics = WorkloadMetrics()
        lock = threading.Lock()
        remaining = {"n": total_requests}

        def worker() -> None:
            while True:
                with lock:
                    if remaining["n"] <= 0:
                        return
                    remaining["n"] -= 1
                result = self._one()
                _record(metrics, lock, result)
                if self.workload.think_time_ms:
                    time.sleep(self.workload.think_time_ms / 1000)

        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(worker) for _ in range(workers)]
            wait(futures)
        metrics.duration_seconds = time.perf_counter() - t0
        return metrics


def _record(metrics: WorkloadMetrics, lock: threading.Lock, result: QueryResult) -> None:
    with lock:
        metrics.latencies_ms.append(result.latency_ms)
        metrics.returned_total += result.returned
        if result.step_ms:
            metrics.step_samples.append(list(result.step_ms))
        if result.ok:
            metrics.successes += 1
        else:
            metrics.errors += 1
            if result.timeout:
                metrics.timeouts += 1


def _mean_steps(samples: list[list[float]]) -> list[float]:
    if not samples:
        return []
    width = max(len(sample) for sample in samples)
    means: list[float] = []
    for index in range(width):
        values = [sample[index] for sample in samples if index < len(sample)]
        means.append(sum(values) / len(values) if values else 0.0)
    return means
