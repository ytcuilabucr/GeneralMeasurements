"""Per-point retry policy. Defaults: no retries, abort (open question in the plan)."""

from __future__ import annotations

from dataclasses import dataclass

from ..devices.transport import DriverError


@dataclass(frozen=True)
class RetryPolicy:
    retries: int = 0                      # extra attempts per point
    on_exhausted: str = "abort"           # "abort" | "pause" (wait for operator)
    backoff_s: float = 0.0
    point_timeout_s: float | None = None  # bound on move + settle + read of one point
    retry_on: tuple[type[BaseException], ...] = (TimeoutError, DriverError)

    def __post_init__(self):
        if self.on_exhausted not in ("abort", "pause"):
            raise ValueError("on_exhausted must be 'abort' or 'pause'")
        if self.retries < 0:
            raise ValueError("retries must be >= 0")
