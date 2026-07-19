# Checkout Worker-Leak Scenario Fixture Contract

`export_worker_lease_fixture()` and `GET /scenario-fixture` return the same
JSON-compatible mapping. They do not import or mutate `IncidentRuntime`.

```json
{
  "schema_version": 1,
  "scenario_id": "checkout-worker-lease-leak",
  "title": "...",
  "failure": {
    "health": {},
    "metrics": {},
    "logs": [],
    "deployment": {}
  },
  "remediation": {
    "action": "rollback",
    "endpoint": "POST /admin/rollback",
    "expected_effect": "..."
  },
  "recovery": {
    "health": {},
    "metrics": {},
    "logs": [],
    "deployment": {}
  }
}
```

Runtime integration should treat `failure` as the incident's initial evidence,
apply its own action rules, and use `recovery` to validate or render the
rollback outcome. `logs` are ordered by their deterministic `sequence` field.
