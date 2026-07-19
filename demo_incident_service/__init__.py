"""A deliberately faulty checkout service used by the Incident Command drill.

The service is local-only and deterministic: it gives an incident learner a
real HTTP surface to inspect and remediate without depending on production
infrastructure.
"""

from .app import app, build_app
from .scenario import SCENARIO_ID, SCHEMA_VERSION, export_worker_lease_fixture
from .service import CheckoutService

__all__ = [
    "SCENARIO_ID",
    "SCHEMA_VERSION",
    "CheckoutService",
    "app",
    "build_app",
    "export_worker_lease_fixture",
]
