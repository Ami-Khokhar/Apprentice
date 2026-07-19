"""HTTP entry point for the local checkout incident service.

Run with ``uv run uvicorn demo_incident_service.app:app --port 8090``.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

from .scenario import export_worker_lease_fixture
from .service import CheckoutService


class CheckoutRequest(BaseModel):
    order_id: str = Field(min_length=1, max_length=100)


def build_app(service: CheckoutService | None = None) -> FastAPI:
    app = FastAPI(title="Deliberately Flawed Checkout Service")
    app.state.checkout_service = service or CheckoutService()

    def active_service() -> CheckoutService:
        return app.state.checkout_service

    @app.post("/checkout")
    def checkout(request: CheckoutRequest) -> dict[str, object]:
        result = active_service().checkout(request.order_id)
        if not result["accepted"]:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=result)
        return result

    @app.get("/health")
    def health() -> dict[str, object]:
        return active_service().health()

    @app.get("/metrics")
    def metrics() -> dict[str, int | float]:
        return active_service().metrics()

    @app.get("/evidence")
    def evidence() -> list[dict[str, object]]:
        return active_service().evidence()

    @app.get("/scenario-fixture")
    def scenario_fixture() -> dict[str, object]:
        """Export standalone failure and recovery evidence for scenario authors."""
        return export_worker_lease_fixture()

    @app.post("/admin/rollback")
    def rollback() -> dict[str, object]:
        return active_service().rollback()

    return app


app = build_app()
