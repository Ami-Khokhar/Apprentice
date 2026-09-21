"""Optional, privacy-aware tracing for Apprentice model operations."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel

LOGGER = logging.getLogger(__name__)
REDACTED = "[REDACTED]"
_SECRET_MARKERS = (
    "apikey",
    "auth",
    "cookie",
    "credential",
    "password",
    "privatekey",
    "secret",
    "token",
)


class Observation(Protocol):
    def update(
        self,
        *,
        output: object | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> None: ...


class Tracer(Protocol):
    @contextmanager
    def operation(
        self,
        name: str,
        *,
        session_id: str,
        input: object,
        metadata: Mapping[str, object] | None = None,
    ) -> Iterator[Observation]: ...

    @contextmanager
    def generation(
        self,
        name: str,
        *,
        model: str,
        output_schema: str,
        input: object,
        metadata: Mapping[str, object] | None = None,
    ) -> Iterator[Observation]: ...


class _NoopObservation:
    def update(
        self,
        *,
        output: object | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        return None


class NoopTracer:
    @contextmanager
    def operation(
        self,
        name: str,
        *,
        session_id: str,
        input: object,
        metadata: Mapping[str, object] | None = None,
    ) -> Iterator[Observation]:
        yield _NoopObservation()

    @contextmanager
    def generation(
        self,
        name: str,
        *,
        model: str,
        output_schema: str,
        input: object,
        metadata: Mapping[str, object] | None = None,
    ) -> Iterator[Observation]:
        yield _NoopObservation()


_local_parent: ContextVar[str | None] = ContextVar("apprentice_trace_parent", default=None)


class LocalTraceStore:
    """Append-only local trace storage with bounded, best-effort retention."""

    def __init__(
        self,
        path: str | Path,
        *,
        retention_days: int = 30,
        content_limit_bytes: int = 256_000,
    ) -> None:
        self.path = Path(path)
        self.retention_days = max(1, retention_days)
        self.content_limit_bytes = max(1_024, content_limit_bytes)
        self._lock = threading.RLock()
        self._last_pruned_at = 0.0

    def append(self, record: Mapping[str, object]) -> None:
        try:
            bounded = _bounded_record(record, self.content_limit_bytes)
            line = json.dumps(bounded, default=str, separators=(",", ":"), sort_keys=True)
            with self._lock:
                _ensure_private_directory(self.path.parent)
                _harden_private_file(self.path)
                self._prune_if_due()
                descriptor = os.open(
                    self.path,
                    os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                    0o600,
                )
                _harden_private_file(self.path)
                with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
                    stream.write(line + "\n")
                    stream.flush()
        except Exception as error:  # tracing must never interrupt a learner
            LOGGER.warning("Local trace write failed: %s", type(error).__name__)

    def records(self, session_id: str | None = None) -> list[dict[str, object]]:
        try:
            with self._lock:
                _harden_private_file(self.path)
                self._prune_if_due()
                records = self._read_unlocked()
        except Exception as error:
            LOGGER.warning("Local trace read failed: %s", type(error).__name__)
            return []
        if session_id is not None:
            records = [item for item in records if item.get("session_id") == session_id]
        return sorted(records, key=lambda item: float(item.get("started_at", 0)))

    def sessions(self) -> list[dict[str, object]]:
        grouped: dict[str, list[dict[str, object]]] = {}
        for record in self.records():
            session_id = str(record.get("session_id", ""))
            if session_id:
                grouped.setdefault(session_id, []).append(record)
        summaries = [
            _session_summary(session_id, records) for session_id, records in grouped.items()
        ]
        return sorted(summaries, key=lambda item: float(item["updated_at"]), reverse=True)

    def _read_unlocked(self) -> list[dict[str, object]]:
        if not self.path.exists():
            return []
        records: list[dict[str, object]] = []
        with self.path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    records.append(value)
        return records

    def _prune_if_due(self) -> None:
        now = time.time()
        if now - self._last_pruned_at < 3_600:
            return
        self._last_pruned_at = now
        if not self.path.exists():
            return
        cutoff = now - timedelta(days=self.retention_days).total_seconds()
        kept = [
            item for item in self._read_unlocked() if float(item.get("started_at", 0)) >= cutoff
        ]
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_TRUNC | os.O_WRONLY,
            0o600,
        )
        _harden_private_file(temporary)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            for item in kept:
                stream.write(json.dumps(item, separators=(",", ":"), sort_keys=True) + "\n")
            stream.flush()
        temporary.replace(self.path)
        _harden_private_file(self.path)


class _LocalObservation:
    def __init__(self, record: dict[str, object]) -> None:
        self.record = record

    def update(
        self,
        *,
        output: object | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        if output is not None:
            self.record["output"] = output
        if metadata is not None:
            current = self.record.setdefault("metadata", {})
            if isinstance(current, dict):
                current.update(metadata)


class LocalTracer:
    def __init__(self, store: LocalTraceStore, *, capture_content: bool) -> None:
        self.store = store
        self._policy = TraceDataPolicy(capture_content=capture_content)

    @contextmanager
    def operation(
        self,
        name: str,
        *,
        session_id: str,
        input: object,
        metadata: Mapping[str, object] | None = None,
    ) -> Iterator[Observation]:
        with self._record(
            name=name,
            kind="operation",
            session_id=session_id,
            input=input,
            metadata=metadata,
        ) as observation:
            yield observation

    @contextmanager
    def generation(
        self,
        name: str,
        *,
        model: str,
        output_schema: str,
        input: object,
        metadata: Mapping[str, object] | None = None,
    ) -> Iterator[Observation]:
        merged = {"model": model, "output_schema": output_schema, **(metadata or {})}
        with self._record(
            name=name,
            kind="generation",
            session_id=None,
            input=input,
            metadata=merged,
        ) as observation:
            yield observation

    @contextmanager
    def _record(
        self,
        *,
        name: str,
        kind: str,
        session_id: str | None,
        input: object,
        metadata: Mapping[str, object] | None,
    ) -> Iterator[Observation]:
        started = time.time()
        parent_id = _local_parent.get()
        observation_id = str(uuid4())
        if session_id is None:
            session_id = _current_session_id.get()
        record: dict[str, object] = {
            "id": observation_id,
            "parent_id": parent_id,
            "session_id": session_id or "unscoped",
            "kind": kind,
            "name": name,
            "started_at": started,
            "started_at_iso": datetime.fromtimestamp(started, UTC).isoformat(),
            "status": "running",
            "input": self._policy.value(input),
            "metadata": redact(metadata or {}),
        }
        observation = _LocalObservation(record)
        parent_token = _local_parent.set(observation_id)
        session_token = _current_session_id.set(session_id)
        try:
            yield observation
        except BaseException as error:
            record["status"] = "error"
            record["error"] = self._policy.exception(error)
            raise
        else:
            record["status"] = "success"
        finally:
            ended = time.time()
            record["ended_at"] = ended
            record["duration_ms"] = round((ended - started) * 1_000, 2)
            if "output" in record:
                record["output"] = self._policy.value(record["output"])
            record["metadata"] = redact(record.get("metadata", {}))
            _current_session_id.reset(session_token)
            _local_parent.reset(parent_token)
            self.store.append(record)


_current_session_id: ContextVar[str | None] = ContextVar("apprentice_trace_session", default=None)


class _CompositeObservation:
    def __init__(self, observations: Sequence[Observation]) -> None:
        self._observations = observations

    def update(
        self,
        *,
        output: object | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        for observation in self._observations:
            try:
                observation.update(output=output, metadata=metadata)
            except Exception as error:
                LOGGER.warning("Trace observation update failed: %s", type(error).__name__)


class CompositeTracer:
    def __init__(self, tracers: Sequence[Tracer]) -> None:
        self._tracers = tuple(tracers)

    @contextmanager
    def operation(self, name: str, **kwargs: Any) -> Iterator[Observation]:
        with ExitStack() as stack:
            observations = [
                stack.enter_context(tracer.operation(name, **kwargs)) for tracer in self._tracers
            ]
            yield _CompositeObservation(observations)

    @contextmanager
    def generation(self, name: str, **kwargs: Any) -> Iterator[Observation]:
        with ExitStack() as stack:
            observations = [
                stack.enter_context(tracer.generation(name, **kwargs)) for tracer in self._tracers
            ]
            yield _CompositeObservation(observations)


@dataclass(frozen=True)
class TraceDataPolicy:
    capture_content: bool = False

    def value(self, value: object) -> object:
        sanitized = redact(value)
        if self.capture_content:
            return sanitized
        encoded = json.dumps(sanitized, default=str, sort_keys=True).encode()
        summary: dict[str, object] = {
            "content_recorded": False,
            "type": type(sanitized).__name__,
            "size_bytes": len(encoded),
        }
        if isinstance(sanitized, Mapping):
            summary["field_count"] = len(sanitized)
        elif isinstance(sanitized, (list, tuple)):
            summary["item_count"] = len(sanitized)
        return summary

    def exception(self, error: BaseException) -> dict[str, object]:
        result: dict[str, object] = {"type": type(error).__name__}
        if self.capture_content:
            result["message"] = redact(str(error)[:500])
        else:
            result["content_recorded"] = False
        return result


class _LangfuseObservation:
    def __init__(self, observation: Any, policy: TraceDataPolicy) -> None:
        self._observation = observation
        self._policy = policy

    def update(
        self,
        *,
        output: object | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        kwargs: dict[str, object] = {}
        if output is not None:
            kwargs["output"] = self._policy.value(output)
        if metadata is not None:
            kwargs["metadata"] = _metadata(metadata)
        if not kwargs:
            return
        try:
            self._observation.update(**kwargs)
        except Exception as error:  # tracing must never interrupt a learner
            LOGGER.warning("Langfuse observation update failed: %s", type(error).__name__)


class LangfuseTracer:
    """Small adapter around Langfuse v4 manual observations."""

    def __init__(self, client: Any, propagate_attributes: Any, *, capture_content: bool) -> None:
        self._client = client
        self._propagate_attributes = propagate_attributes
        self._policy = TraceDataPolicy(capture_content=capture_content)

    @contextmanager
    def operation(
        self,
        name: str,
        *,
        session_id: str,
        input: object,
        metadata: Mapping[str, object] | None = None,
    ) -> Iterator[Observation]:
        kwargs = {
            "as_type": "span",
            "name": name,
            "input": self._policy.value(input),
            "metadata": _metadata(metadata or {}),
        }
        manager: Any | None = None
        try:
            manager = self._client.start_as_current_observation(**kwargs)
            raw = manager.__enter__()
            attributes = self._propagate_attributes(
                trace_name=name,
                session_id=session_id,
                tags=["apprentice"],
            )
            attributes.__enter__()
        except Exception as error:
            if manager is not None:
                _safe_exit(manager, type(error), error, error.__traceback__)
            LOGGER.warning("Langfuse operation could not start: %s", type(error).__name__)
            yield _NoopObservation()
            return
        try:
            yield _LangfuseObservation(raw, self._policy)
        except BaseException as error:
            _record_langfuse_error(raw, self._policy, error)
            exc_info = _langfuse_exc_info(self._policy, error)
            _safe_exit(attributes, *exc_info)
            _safe_exit(manager, *exc_info)
            raise
        else:
            _safe_exit(attributes, None, None, None)
            _safe_exit(manager, None, None, None)

    @contextmanager
    def generation(
        self,
        name: str,
        *,
        model: str,
        output_schema: str,
        input: object,
        metadata: Mapping[str, object] | None = None,
    ) -> Iterator[Observation]:
        kwargs = {
            "as_type": "generation",
            "name": name,
            "model": model,
            "input": self._policy.value(input),
            "metadata": _metadata({"output_schema": output_schema, **(metadata or {})}),
        }
        try:
            manager = self._client.start_as_current_observation(**kwargs)
            raw = manager.__enter__()
        except Exception as error:
            LOGGER.warning("Langfuse generation could not start: %s", type(error).__name__)
            yield _NoopObservation()
            return
        try:
            yield _LangfuseObservation(raw, self._policy)
        except BaseException as error:
            _record_langfuse_error(raw, self._policy, error)
            _safe_exit(manager, *_langfuse_exc_info(self._policy, error))
            raise
        else:
            _safe_exit(manager, None, None, None)


def build_tracer(
    environ: Mapping[str, str] | None = None,
    *,
    local_store: LocalTraceStore | None = None,
) -> Tracer:
    environment = os.environ if environ is None else environ
    tracers: list[Tracer] = []
    if _enabled(environment.get("APPRENTICE_OBSERVER_ENABLED")):
        store = local_store or LocalTraceStore(
            environment.get("APPRENTICE_TRACE_PATH", ".apprentice/traces.jsonl"),
            retention_days=_positive_int(environment.get("APPRENTICE_TRACE_RETENTION_DAYS"), 30),
            content_limit_bytes=_positive_int(
                environment.get("APPRENTICE_TRACE_CONTENT_LIMIT_BYTES"), 256_000
            ),
        )
        tracers.append(
            LocalTracer(
                store,
                capture_content=_enabled(environment.get("APPRENTICE_TRACE_CONTENT")),
            )
        )
    langfuse = _build_langfuse_tracer(environment)
    if langfuse is not None:
        tracers.append(langfuse)
    if not tracers:
        return NoopTracer()
    if len(tracers) == 1:
        return tracers[0]
    return CompositeTracer(tracers)


def _build_langfuse_tracer(environment: Mapping[str, str]) -> Tracer | None:
    if not _enabled(environment.get("APPRENTICE_LANGFUSE_ENABLED")):
        return None
    missing = [
        name
        for name in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL")
        if not environment.get(name)
    ]
    if missing:
        LOGGER.warning(
            "Langfuse tracing is enabled but required configuration is missing: %s",
            ", ".join(missing),
        )
        return None
    try:
        from langfuse import Langfuse, propagate_attributes

        client = Langfuse(
            public_key=environment["LANGFUSE_PUBLIC_KEY"],
            secret_key=environment["LANGFUSE_SECRET_KEY"],
            base_url=environment["LANGFUSE_BASE_URL"],
        )
    except Exception as error:
        LOGGER.warning("Langfuse tracing could not be initialized: %s", type(error).__name__)
        return None
    return LangfuseTracer(
        client,
        propagate_attributes,
        capture_content=_enabled(environment.get("APPRENTICE_TRACE_CONTENT")),
    )


def redact(value: object) -> object:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    if isinstance(value, Mapping):
        return {
            str(key): REDACTED if _is_secret_key(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


def _metadata(value: Mapping[str, object]) -> dict[str, str]:
    sanitized = redact(value)
    assert isinstance(sanitized, Mapping)
    return {str(key): str(item)[:200] for key, item in sanitized.items()}


def _enabled(value: str | None) -> bool:
    return value is not None and value.strip().casefold() in {"1", "true", "yes", "on"}


def enabled(value: str | None) -> bool:
    return _enabled(value)


def _positive_int(value: str | None, default: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _is_secret_key(key: str) -> bool:
    normalized = "".join(character for character in key.casefold() if character.isalnum())
    return any(marker in normalized for marker in _SECRET_MARKERS)


def _safe_exit(manager: Any, *exc_info: object) -> None:
    try:
        manager.__exit__(*exc_info)
    except Exception as error:
        LOGGER.warning("Langfuse operation could not finish: %s", type(error).__name__)


def _record_langfuse_error(
    observation: Any,
    policy: TraceDataPolicy,
    error: BaseException,
) -> None:
    try:
        observation.update(metadata={"error": policy.exception(error)})
    except Exception as update_error:
        LOGGER.warning(
            "Langfuse observation update failed: %s",
            type(update_error).__name__,
        )


def _langfuse_exc_info(
    policy: TraceDataPolicy,
    error: BaseException,
) -> tuple[object | None, object | None, object | None]:
    if not policy.capture_content:
        return None, None, None
    return type(error), error, error.__traceback__


def _ensure_private_directory(path: Path) -> None:
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if existed and path.name != ".apprentice":
        # A configured trace file may live in a shared directory such as /tmp.
        # Do not change permissions on a directory Apprentice did not create.
        return
    # Windows and some mounted filesystems do not expose POSIX modes.
    with suppress(OSError):
        path.chmod(0o700)


def _harden_private_file(path: Path) -> None:
    if not path.exists():
        return
    # Windows and some mounted filesystems do not expose POSIX modes.
    with suppress(OSError):
        path.chmod(0o600)


def _bounded_record(record: Mapping[str, object], limit: int) -> dict[str, object]:
    value = dict(record)
    encoded = json.dumps(value, default=str, sort_keys=True).encode()
    if len(encoded) <= limit:
        return value
    for key in ("input", "output"):
        if key in value:
            original = json.dumps(value[key], default=str, sort_keys=True).encode()
            value[key] = {
                "content_truncated": True,
                "original_size_bytes": len(original),
            }
        if len(json.dumps(value, default=str, sort_keys=True).encode()) <= limit:
            break
    return value


def _session_summary(session_id: str, records: Sequence[Mapping[str, object]]) -> dict[str, object]:
    ordered = sorted(records, key=lambda item: float(item.get("started_at", 0)))
    operations = [item for item in ordered if item.get("kind") == "operation"]
    names = {str(item.get("name", "")) for item in operations}
    status = "debriefed" if "practice.debrief" in names else "active"
    if any(item.get("status") == "error" for item in ordered):
        status = "error"
    first = operations[0] if operations else ordered[0]
    metadata = first.get("metadata", {})
    if not isinstance(metadata, Mapping):
        metadata = {}
    captured_input = first.get("input", {})
    profile = (
        captured_input.get("learner_profile", {}) if isinstance(captured_input, Mapping) else {}
    )
    if not isinstance(profile, Mapping):
        profile = {}
    start_output = next(
        (item.get("output", {}) for item in operations if item.get("name") == "practice.start"),
        {},
    )
    scenario = start_output.get("scenario", {}) if isinstance(start_output, Mapping) else {}
    if not isinstance(scenario, Mapping):
        scenario = {}
    return {
        "session_id": session_id,
        "started_at": float(ordered[0].get("started_at", 0)),
        "updated_at": max(float(item.get("ended_at", 0)) for item in ordered),
        "status": status,
        "difficulty_level": metadata.get("difficulty_level"),
        "field": profile.get("field"),
        "title": scenario.get("title"),
        "event_count": len(ordered),
        "error_count": sum(item.get("status") == "error" for item in ordered),
        "content_recorded": _record_has_content(first),
    }


def _record_has_content(record: Mapping[str, object]) -> bool:
    value = record.get("input")
    return not (isinstance(value, Mapping) and value.get("content_recorded") is False)
