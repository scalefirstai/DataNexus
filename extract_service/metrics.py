"""Lightweight metrics registry (§9.2)."""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Histogram:
    observations: list[float] = field(default_factory=list)

    def observe(self, v: float) -> None:
        self.observations.append(v)

    def snapshot(self) -> dict[str, Any]:
        if not self.observations:
            return {"count": 0}
        ordered = sorted(self.observations)
        n = len(ordered)

        def q(p: float) -> float:
            idx = min(n - 1, max(0, int(p * n) - 1))
            return ordered[idx]

        return {
            "count": n,
            "sum": sum(ordered),
            "p50": q(0.50),
            "p95": q(0.95),
            "p99": q(0.99),
        }


class Metrics:
    def __init__(self) -> None:
        self._counters: dict[tuple[str, tuple], float] = defaultdict(float)
        self._gauges: dict[tuple[str, tuple], float] = defaultdict(float)
        self._histograms: dict[tuple[str, tuple], Histogram] = defaultdict(Histogram)
        self._lock = threading.Lock()

    @staticmethod
    def _labels_key(labels: dict[str, str] | None) -> tuple:
        return tuple(sorted((labels or {}).items()))

    def inc(self, name: str, amount: float = 1.0, labels: dict[str, str] | None = None) -> None:
        with self._lock:
            self._counters[(name, self._labels_key(labels))] += amount

    def set_gauge(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        with self._lock:
            self._gauges[(name, self._labels_key(labels))] = value

    def observe(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        with self._lock:
            self._histograms[(name, self._labels_key(labels))].observe(value)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "counters": {f"{name}{dict(lbl)}": v for (name, lbl), v in self._counters.items()},
                "gauges": {f"{name}{dict(lbl)}": v for (name, lbl), v in self._gauges.items()},
                "histograms": {
                    f"{name}{dict(lbl)}": h.snapshot()
                    for (name, lbl), h in self._histograms.items()
                },
            }


_metrics: Metrics | None = None


def get_metrics() -> Metrics:
    global _metrics
    if _metrics is None:
        _metrics = Metrics()
    return _metrics


def reset_metrics_for_tests() -> None:
    global _metrics
    _metrics = None
