"""Alert rule definitions (§9.3). Expressed as evaluator functions over metrics."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class AlertRule:
    name: str
    severity: str
    condition: Callable[[dict[str, Any]], bool]
    description: str


def _high_queue_depth(snapshot: dict[str, Any]) -> bool:
    for key, v in snapshot.get("gauges", {}).items():
        if key.startswith("extract.worker.queue_depth") and v > 30:
            return True
    return False


def _api_error_rate(snapshot: dict[str, Any]) -> bool:
    counters = snapshot.get("counters", {})
    total = sum(v for k, v in counters.items() if k.startswith("extract.api.requests"))
    errs = sum(v for k, v in counters.items() if k.startswith("extract.api.errors"))
    return total > 0 and (errs / total) > 0.01


def _dlq_growth(snapshot: dict[str, Any]) -> bool:
    for k, v in snapshot.get("gauges", {}).items():
        if "dlq" in k and v > 0:
            return True
    return False


RULES: list[AlertRule] = [
    AlertRule(
        name="high_queue_depth",
        severity="warning",
        condition=_high_queue_depth,
        description="Worker queue > 30 for > 5 min",
    ),
    AlertRule(
        name="api_error_rate",
        severity="critical",
        condition=_api_error_rate,
        description="API 5xx rate > 1% in 5 min",
    ),
    AlertRule(
        name="dlq_growth",
        severity="warning",
        condition=_dlq_growth,
        description="DLQ depth > 0",
    ),
]


def evaluate(snapshot: dict[str, Any]) -> list[str]:
    return [r.name for r in RULES if r.condition(snapshot)]
