"""Deterministic model of a checkout worker-pool regression.

The faulty release leaks a worker lease after each successful request.  Once
all workers are leaked, new checkouts return a 503 and appear in the backlog.
Rolling back restarts the worker pool and drains the recorded backlog.  It is a
small model, but the metrics and logs come from the same state that serves
checkout requests, rather than from a scripted incident transcript.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CheckoutService:
    worker_limit: int = 4
    faulty_release: bool = True
    leased_workers: int = 0
    total_requests: int = 0
    successful_checkouts: int = 0
    rejected_checkouts: int = 0
    recovered_checkouts: int = 0
    backlog: list[str] = field(default_factory=list)
    events: list[dict[str, object]] = field(default_factory=list)
    _sequence: int = 0

    def __post_init__(self) -> None:
        if self.worker_limit < 1:
            raise ValueError("worker_limit must be at least one")
        self._log(
            "deploy.active",
            release="checkout-api-2026.07.14.3",
            worker_lease_release="disabled",
        )

    def checkout(self, order_id: str) -> dict[str, object]:
        """Process one checkout through the modeled worker pool."""
        order_id = order_id.strip()
        if not order_id:
            raise ValueError("order_id is required")
        self.total_requests += 1
        if self.leased_workers >= self.worker_limit:
            self.rejected_checkouts += 1
            self.backlog.append(order_id)
            self._log(
                "checkout.rejected",
                order_id=order_id,
                reason="worker_pool_exhausted",
                status_code=503,
            )
            return {
                "accepted": False,
                "order_id": order_id,
                "status_code": 503,
                "reason": "checkout workers exhausted; order queued for retry",
            }

        self.leased_workers += 1
        self.successful_checkouts += 1
        leaked = self.faulty_release
        if not leaked:
            self.leased_workers -= 1
        self._log(
            "checkout.completed",
            order_id=order_id,
            status_code=201,
            worker_lease="leaked" if leaked else "released",
        )
        return {
            "accepted": True,
            "order_id": order_id,
            "status_code": 201,
            "worker_lease": "leaked" if leaked else "released",
        }

    def rollback(self) -> dict[str, object]:
        """Restore the known-good release and safely replay queued orders."""
        was_faulty = self.faulty_release
        queued_orders = list(self.backlog)
        self.faulty_release = False
        self.leased_workers = 0  # a rollback restarts the pool
        self.backlog.clear()
        self.recovered_checkouts += len(queued_orders)
        self.successful_checkouts += len(queued_orders)
        for order_id in queued_orders:
            self._log(
                "checkout.retried",
                order_id=order_id,
                status_code=201,
                worker_lease="released",
            )
        self._log(
            "deploy.rolled_back",
            from_release="checkout-api-2026.07.14.3" if was_faulty else "known-good",
            to_release="checkout-api-2026.07.12.8",
            workers_restarted=self.worker_limit,
            backlog_replayed=len(queued_orders),
        )
        return self.snapshot()

    def health(self) -> dict[str, object]:
        exhausted = self.leased_workers >= self.worker_limit
        return {
            "status": "degraded" if exhausted else "ok",
            "release": (
                "checkout-api-2026.07.14.3"
                if self.faulty_release
                else "checkout-api-2026.07.12.8"
            ),
            "worker_pool": {
                "capacity": self.worker_limit,
                "leased": self.leased_workers,
                "available": self.worker_limit - self.leased_workers,
            },
            "backlog": len(self.backlog),
        }

    def metrics(self) -> dict[str, int | float]:
        failures = self.rejected_checkouts
        total = self.total_requests or 1
        return {
            "checkout_requests_total": self.total_requests,
            "checkout_success_total": self.successful_checkouts,
            "checkout_rejected_total": failures,
            "checkout_backlog": len(self.backlog),
            "checkout_worker_leases": self.leased_workers,
            "checkout_worker_capacity": self.worker_limit,
            "checkout_error_rate": round(failures / total, 3),
            "checkout_recovered_total": self.recovered_checkouts,
        }

    def evidence(self) -> list[dict[str, object]]:
        return list(self.events)

    def snapshot(self) -> dict[str, object]:
        return {"health": self.health(), "metrics": self.metrics(), "evidence": self.evidence()}

    def _log(self, event: str, **fields: object) -> None:
        self._sequence += 1
        self.events.append({"sequence": self._sequence, "event": event, **fields})
