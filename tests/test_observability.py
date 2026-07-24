from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any

from apprentice.observability import (
    REDACTED,
    CompositeTracer,
    LangfuseTracer,
    LocalTracer,
    LocalTraceStore,
    NoopTracer,
    TraceDataPolicy,
    build_tracer,
    redact,
)


class FakeSDKObservation:
    def __init__(self, record: dict[str, Any]) -> None:
        self.record = record

    def update(self, **kwargs: object) -> None:
        self.record.setdefault("updates", []).append(kwargs)


class FakeSDKManager:
    def __init__(self, client: FakeSDKClient, record: dict[str, Any]) -> None:
        self.client = client
        self.record = record

    def __enter__(self) -> FakeSDKObservation:
        self.record["parent"] = self.client.stack[-1]["name"] if self.client.stack else None
        self.client.stack.append(self.record)
        return FakeSDKObservation(self.record)

    def __exit__(self, error_type: object, error: object, traceback: object) -> None:
        self.client.stack.pop()
        self.record["error_type"] = getattr(error_type, "__name__", None)


class FakeSDKClient:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.stack: list[dict[str, Any]] = []

    def start_as_current_observation(self, **kwargs: object) -> FakeSDKManager:
        record = dict(kwargs)
        self.records.append(record)
        return FakeSDKManager(self, record)


class FakePropagation:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @contextmanager
    def __call__(self, **kwargs: object):
        self.calls.append(dict(kwargs))
        yield


def test_tracing_is_noop_by_default_and_misconfiguration_is_safe(
    caplog,
) -> None:
    assert isinstance(build_tracer({}), NoopTracer)

    with caplog.at_level(logging.WARNING):
        tracer = build_tracer({"APPRENTICE_LANGFUSE_ENABLED": "true"})

    assert isinstance(tracer, NoopTracer)
    assert "required configuration is missing" in caplog.text
    with tracer.operation("practice.start", session_id="session", input={}) as observation:
        observation.update(output={"ok": True})


def test_content_is_opt_in_and_secret_values_are_recursively_redacted() -> None:
    payload = {
        "learner_response": "Restart the service",
        "nested": {
            "LANGFUSE_SECRET_KEY": "secret-value",
            "apiKey": "key-value",
            "Authorization": "Bearer private",
        },
    }

    summary = TraceDataPolicy().value(payload)
    captured = TraceDataPolicy(capture_content=True).value(payload)

    assert summary["content_recorded"] is False
    assert "Restart the service" not in str(summary)
    assert captured["learner_response"] == "Restart the service"
    assert captured["nested"] == {
        "LANGFUSE_SECRET_KEY": REDACTED,
        "apiKey": REDACTED,
        "Authorization": REDACTED,
    }
    assert redact('{"password":"private","safe":"visible"}') == {
        "password": REDACTED,
        "safe": "visible",
    }


def test_langfuse_v4_manual_observations_nest_generation_and_propagate_session() -> None:
    client = FakeSDKClient()
    propagation = FakePropagation()
    tracer = LangfuseTracer(client, propagation, capture_content=True)

    with tracer.operation(
        "practice.respond",
        session_id="practice-123",
        input={"learner_response": "Inspect first"},
    ) as operation:
        with tracer.generation(
            "dojo-decision-facilitator",
            model="gpt-5.6-terra",
            output_schema="FacilitatorDecision",
            input={"prompt": '{"learner_response":"Inspect first"}'},
            metadata={"attempt": 1},
        ) as generation:
            generation.update(output={"action_kind": "inspect-pipeline"})
        operation.update(output={"world_outcome": "active"})

    root, child = client.records
    assert root["as_type"] == "span"
    assert root["input"]["learner_response"] == "Inspect first"
    assert child["as_type"] == "generation"
    assert child["parent"] == "practice.respond"
    assert child["model"] == "gpt-5.6-terra"
    assert child["metadata"]["output_schema"] == "FacilitatorDecision"
    assert child["updates"][0]["output"] == {"action_kind": "inspect-pipeline"}
    assert propagation.calls == [
        {
            "trace_name": "practice.respond",
            "session_id": "practice-123",
            "tags": ["apprentice"],
        }
    ]


def test_langfuse_failures_do_not_replace_application_results(caplog) -> None:
    class BrokenClient:
        def start_as_current_observation(self, **kwargs: object) -> object:
            raise RuntimeError("exporter unavailable")

    tracer = LangfuseTracer(BrokenClient(), FakePropagation(), capture_content=False)

    with (
        caplog.at_level(logging.WARNING),
        tracer.operation("practice.stop", session_id="session", input={}) as observation,
    ):
        observation.update(output={"stopped": True})

    assert "Langfuse operation could not start: RuntimeError" in caplog.text


def test_local_trace_records_nested_content_and_rejection_reason(tmp_path) -> None:
    store = LocalTraceStore(tmp_path / "traces.jsonl")
    tracer = LocalTracer(store, capture_content=True)

    with tracer.operation(
        "practice.start",
        session_id="practice-123",
        input={"profile": {"field": "Operations", "api_key": "private"}},
    ), tracer.generation(
        "dojo-scenario-generator",
        model="gpt-5.6-terra",
        output_schema="GeneratedScenarioSpec",
        input={"prompt": "Create a situation"},
        metadata={"attempt": 1},
    ) as generation:
        generation.update(
            output={"title": "Queue pressure"},
            metadata={"result": "rejected", "rejection_reason": "near_duplicate"},
        )

    operation, generation = store.records("practice-123")
    assert operation["parent_id"] is None
    assert operation["input"]["profile"]["api_key"] == REDACTED
    assert generation["parent_id"] == operation["id"]
    assert generation["metadata"]["model"] == "gpt-5.6-terra"
    assert generation["metadata"]["rejection_reason"] == "near_duplicate"
    assert generation["output"] == {"title": "Queue pressure"}
    assert generation["duration_ms"] >= 0


def test_local_trace_content_is_gated_and_writes_fail_open(tmp_path, caplog) -> None:
    store = LocalTraceStore(tmp_path / "traces.jsonl")
    tracer = LocalTracer(store, capture_content=False)
    with tracer.operation(
        "practice.respond", session_id="session", input={"learner_response": "secret text"}
    ) as operation:
        operation.update(output={"selected_action": "inspect"})

    record = store.records("session")[0]
    assert record["input"]["content_recorded"] is False
    assert record["output"]["content_recorded"] is False
    assert "secret text" not in str(record)

    blocked_store = LocalTraceStore(tmp_path / "blocked" / "trace.jsonl")
    (tmp_path / "blocked").write_text("not a directory")
    with caplog.at_level(logging.WARNING):
        blocked_store.append({"session_id": "safe"})
    assert "Local trace write failed" in caplog.text


def test_composite_keeps_local_trace_when_other_observer_update_fails(tmp_path) -> None:
    class BrokenObservation:
        def update(self, **kwargs: object) -> None:
            raise RuntimeError("export failed")

    class BrokenTracer(NoopTracer):
        @contextmanager
        def operation(self, name: str, **kwargs: object):
            yield BrokenObservation()

    store = LocalTraceStore(tmp_path / "traces.jsonl")
    tracer = CompositeTracer((LocalTracer(store, capture_content=True), BrokenTracer()))
    with tracer.operation("practice.stop", session_id="session", input={}) as observation:
        observation.update(output={"stopped": True})

    assert store.records("session")[0]["output"] == {"stopped": True}
