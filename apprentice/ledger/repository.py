from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from apprentice.canonical import canonical_json, digest, normalize_base_url, normalize_hostname
from apprentice.config import Policy
from apprentice.ledger.db import SQLiteDatabase
from apprentice.models import (
    Bucket,
    BucketKind,
    BucketOrigin,
    Demonstration,
    DemonstrationRole,
    Event,
    EventType,
    RecordedAction,
    ReviewedPlaybook,
    RiskClass,
    Run,
    RunState,
    RunTransition,
    Severity,
    Verdict,
    VerdictActor,
    VerdictDecision,
    Veto,
    VetoStatus,
)


class RepositoryError(RuntimeError):
    pass


class ConflictError(RepositoryError):
    pass


class UnknownBucketError(RepositoryError):
    def __init__(self, bucket: str | int) -> None:
        super().__init__(f"Unknown bucket: {bucket}")
        self.bucket = bucket


class UnknownRunError(RepositoryError):
    def __init__(self, run_id: str) -> None:
        super().__init__(f"Unknown run: {run_id}")
        self.run_id = run_id


class EvidenceConflictError(ConflictError):
    pass


class Repository:
    def __init__(
        self,
        db_path: str | Path,
        *,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self.db = SQLiteDatabase(db_path)
        self._clock = clock
        self._id_factory = id_factory
        self._fault_injector = fault_injector or (lambda _point: None)

    def create_bucket(
        self,
        name: str,
        *,
        kind: BucketKind | str = BucketKind.PLAYBOOK,
        risk_class: RiskClass | str = RiskClass.MEDIUM,
        origin: BucketOrigin | str = BucketOrigin.DEMONSTRATED,
        target_base_url: str = "http://127.0.0.1",
        approved_hosts: list[str] | None = None,
        tool_versions: dict[str, str] | None = None,
    ) -> Bucket:
        kind = BucketKind(kind)
        risk_class = RiskClass(risk_class)
        origin = BucketOrigin(origin)
        if not name.strip():
            raise ValueError("Bucket name cannot be blank")
        tool_versions = dict(tool_versions or {})
        if not all(
            isinstance(tool_name, str) and isinstance(version, str)
            for tool_name, version in tool_versions.items()
        ):
            raise TypeError("Tool names and versions must be strings")
        normalized_target, target_host = normalize_base_url(target_base_url)
        allowed_hosts = sorted(
            {
                target_host,
                *(_normalize_approved_host(host) for host in (approved_hosts or [])),
            }
        )
        toolset_digest = digest(tool_versions)
        timestamp = self._clock()

        try:
            with self.db.transaction(write=True) as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO buckets (
                      name, kind, risk_class, level, origin, playbook_version,
                      playbook_digest, target_base_url, allowed_hosts_json,
                      tool_versions_json, toolset_digest, created_at,
                      last_activity_at, last_decay_at, activated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                    """,
                    (
                        name,
                        kind.value,
                        risk_class.value,
                        0,
                        origin.value,
                        1,
                        "",
                        normalized_target,
                        canonical_json(allowed_hosts).decode("utf-8"),
                        canonical_json(tool_versions).decode("utf-8"),
                        toolset_digest,
                        timestamp,
                        timestamp,
                        None,
                    ),
                )
                bucket_id = cursor.lastrowid
                assert bucket_id is not None
                row = connection.execute(
                    "SELECT * FROM buckets WHERE id = ?", (bucket_id,)
                ).fetchone()
        except sqlite3.IntegrityError as error:
            if "buckets.name" in str(error):
                raise ConflictError(f"Bucket already exists: {name}") from error
            raise
        assert row is not None
        return _bucket_from_row(row)

    def register_self_authored_tool(
        self,
        name: str,
        *,
        artifact_digest: str,
        risk_class: RiskClass | str = RiskClass.LOW,
        target_base_url: str = "http://127.0.0.1",
        approved_hosts: list[str] | None = None,
    ) -> Bucket:
        """Register one reviewed self-authored tool at L1, pinned to its SHA-256.

        Self-authored tools do not use browser demonstrations or a reviewed
        playbook. Their reviewed artifact digest is the activation boundary;
        like every newly activated capability, they begin at L1 probation.
        """
        if re.fullmatch(r"[0-9a-f]{64}", artifact_digest) is None:
            raise ValueError("Tool artifact digest must be a lowercase SHA-256 digest")
        bucket = self.create_bucket(
            name,
            kind=BucketKind.TOOL,
            risk_class=risk_class,
            origin=BucketOrigin.SELF_AUTHORED,
            target_base_url=target_base_url,
            approved_hosts=approved_hosts,
        )
        activated_at = self._clock()
        with self.db.transaction(write=True) as connection:
            cursor = connection.execute(
                """
                UPDATE buckets
                SET level = 1, playbook_digest = ?, activated_at = ?,
                    last_activity_at = ?
                WHERE id = ? AND kind = 'tool' AND origin = 'self_authored'
                  AND level = 0 AND playbook_digest = '' AND activated_at IS NULL
                """,
                (artifact_digest, activated_at, activated_at, bucket.id),
            )
            if cursor.rowcount != 1:
                raise ConflictError(f"Concurrent tool registration lost for bucket {bucket.id}")
            row = connection.execute(
                "SELECT * FROM buckets WHERE id = ?", (bucket.id,)
            ).fetchone()
        assert row is not None
        return _bucket_from_row(row)

    def declare_tool_dependency(
        self,
        bucket_id: int,
        *,
        tool_name: str,
        artifact_digest: str,
    ) -> Bucket:
        """Pin an activated playbook to the exact reviewed artifact of a tool."""
        with self.db.transaction(write=True) as connection:
            bucket_row = connection.execute(
                "SELECT * FROM buckets WHERE id = ?", (bucket_id,)
            ).fetchone()
            if bucket_row is None:
                raise UnknownBucketError(bucket_id)
            bucket = _bucket_from_row(bucket_row)
            if bucket.kind is not BucketKind.PLAYBOOK:
                raise ConflictError("Only playbooks can declare tool dependencies")

            tool_row = connection.execute(
                "SELECT * FROM buckets WHERE name = ?", (tool_name,)
            ).fetchone()
            if tool_row is None:
                raise UnknownBucketError(tool_name)
            tool = _bucket_from_row(tool_row)
            if tool.kind is not BucketKind.TOOL or tool.activated_at is None:
                raise ConflictError(f"Dependency is not an activated tool: {tool_name}")
            if artifact_digest != tool.playbook_digest:
                raise ConflictError(f"Tool artifact digest mismatch: {tool_name}")

            tool_versions = dict(bucket.tool_versions)
            existing = tool_versions.get(tool_name)
            if existing is not None and existing != artifact_digest:
                raise ConflictError(f"Tool dependency is already pinned: {tool_name}")
            if existing == artifact_digest:
                return bucket
            tool_versions[tool_name] = artifact_digest
            toolset_digest = digest(tool_versions)
            cursor = connection.execute(
                """
                UPDATE buckets
                SET tool_versions_json = ?, toolset_digest = ?
                WHERE id = ? AND tool_versions_json = ?
                """,
                (
                    canonical_json(tool_versions).decode("utf-8"),
                    toolset_digest,
                    bucket_id,
                    canonical_json(dict(bucket.tool_versions)).decode("utf-8"),
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError(
                    f"Concurrent dependency declaration lost for bucket {bucket_id}"
                )
            updated = connection.execute(
                "SELECT * FROM buckets WHERE id = ?", (bucket_id,)
            ).fetchone()
        assert updated is not None
        return _bucket_from_row(updated)

    def get_bucket_by_name(self, name: str) -> Bucket:
        with self.db.transaction() as connection:
            row = connection.execute("SELECT * FROM buckets WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise UnknownBucketError(name)
        return _bucket_from_row(row)

    def get_bucket_by_id(self, bucket_id: int) -> Bucket:
        with self.db.transaction() as connection:
            row = connection.execute("SELECT * FROM buckets WHERE id = ?", (bucket_id,)).fetchone()
        if row is None:
            raise UnknownBucketError(bucket_id)
        return _bucket_from_row(row)

    def list_buckets(self) -> list[Bucket]:
        with self.db.transaction() as connection:
            rows = connection.execute("SELECT * FROM buckets ORDER BY id").fetchall()
        return [_bucket_from_row(row) for row in rows]

    def get_reviewed_playbook(self, bucket_id: int, version: int | None = None) -> ReviewedPlaybook:
        bucket = self.get_bucket_by_id(bucket_id)
        selected_version = bucket.playbook_version if version is None else version
        with self.db.transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM playbooks WHERE bucket_id = ? AND version = ?
                """,
                (bucket_id, selected_version),
            ).fetchone()
        if row is None:
            raise ConflictError(f"Bucket {bucket_id} has no reviewed playbook v{selected_version}")
        return _playbook_from_row(row)

    def register_reviewed_playbook(
        self,
        bucket_id: int,
        reviewed_playbook: Any,
        *,
        version: int,
    ) -> Bucket:
        if version < 1:
            raise ValueError("Playbook version must be at least 1")
        playbook_json = canonical_json(reviewed_playbook).decode("utf-8")
        playbook_digest = digest(reviewed_playbook)
        reviewed_at = self._clock()
        with self.db.transaction(write=True) as connection:
            bucket_row = connection.execute(
                "SELECT * FROM buckets WHERE id = ?", (bucket_id,)
            ).fetchone()
            if bucket_row is None:
                raise UnknownBucketError(bucket_id)
            bucket = _bucket_from_row(bucket_row)
            demonstration_row = connection.execute(
                """
                SELECT COUNT(DISTINCT artifact_digest)
                FROM demonstrations
                WHERE bucket_id = ? AND role = 'training'
                """,
                (bucket_id,),
            ).fetchone()
            assert demonstration_row is not None
            training_count = int(demonstration_row[0])
            if training_count < 2:
                raise ConflictError(
                    "Capability activation requires two unique training demonstrations"
                )
            existing = connection.execute(
                """
                SELECT digest, content_json FROM playbooks
                WHERE bucket_id = ? AND version = ?
                """,
                (bucket_id, version),
            ).fetchone()
            if existing is not None:
                if (
                    version == bucket.playbook_version
                    and existing["digest"] == playbook_digest
                    and existing["content_json"] == playbook_json
                    and bucket.playbook_digest == playbook_digest
                    and bucket.activated_at is not None
                ):
                    return bucket
                raise ConflictError(f"Reviewed playbook v{version} is already bound")

            existing_digest = connection.execute(
                """
                SELECT version FROM playbooks WHERE bucket_id = ? AND digest = ?
                """,
                (bucket_id, playbook_digest),
            ).fetchone()
            if existing_digest is not None:
                raise ConflictError(
                    f"Reviewed content is already registered as v{existing_digest['version']}"
                )

            expected_version = (
                bucket.playbook_version
                if bucket.activated_at is None
                else bucket.playbook_version + 1
            )
            if version != expected_version:
                raise ConflictError(
                    f"Next reviewed playbook must be v{expected_version}, not v{version}"
                )
            connection.execute(
                """
                INSERT INTO playbooks (
                  bucket_id, version, content_json, digest, reviewed_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (bucket_id, version, playbook_json, playbook_digest, reviewed_at),
            )
            cursor = connection.execute(
                """
                UPDATE buckets
                SET level = 1, playbook_version = ?, playbook_digest = ?,
                    activated_at = ?, last_activity_at = ?
                WHERE id = ? AND playbook_version = ?
                """,
                (
                    version,
                    playbook_digest,
                    reviewed_at,
                    reviewed_at,
                    bucket_id,
                    bucket.playbook_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError(f"Concurrent playbook registration lost for bucket {bucket_id}")
            updated = connection.execute(
                "SELECT * FROM buckets WHERE id = ?", (bucket_id,)
            ).fetchone()
        assert updated is not None
        return _bucket_from_row(updated)

    def add_demonstration(
        self,
        bucket_id: int,
        artifact_ref: str,
        artifact_digest: str,
        role: DemonstrationRole | str,
        *,
        demonstration_id: str | None = None,
        created_at: float | None = None,
    ) -> Demonstration:
        role = DemonstrationRole(role)
        demonstration_id = demonstration_id or self._id_factory()
        timestamp = self._clock() if created_at is None else created_at
        with self.db.transaction(write=True) as connection:
            existing = connection.execute(
                "SELECT * FROM demonstrations WHERE artifact_digest = ?",
                (artifact_digest,),
            ).fetchone()
            if existing is not None:
                demonstration = _demonstration_from_row(existing)
                if (
                    demonstration.bucket_id != bucket_id
                    or demonstration.artifact_ref != artifact_ref
                    or demonstration.role is not role
                ):
                    raise ConflictError(
                        f"Artifact digest already belongs to another demonstration: "
                        f"{artifact_digest}"
                    )
                return demonstration
            if not _bucket_exists(connection, bucket_id):
                raise UnknownBucketError(bucket_id)
            connection.execute(
                """
                INSERT INTO demonstrations (
                  id, bucket_id, artifact_ref, artifact_digest, role, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    demonstration_id,
                    bucket_id,
                    artifact_ref,
                    artifact_digest,
                    role.value,
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM demonstrations WHERE id = ?", (demonstration_id,)
            ).fetchone()
        assert row is not None
        return _demonstration_from_row(row)

    def count_demonstrations(
        self, bucket_id: int, *, role: DemonstrationRole | str | None = None
    ) -> int:
        parameters: list[Any] = [bucket_id]
        query = "SELECT COUNT(*) FROM demonstrations WHERE bucket_id = ?"
        if role is not None:
            query += " AND role = ?"
            parameters.append(DemonstrationRole(role).value)
        with self.db.transaction() as connection:
            row = connection.execute(query, parameters).fetchone()
        assert row is not None
        return int(row[0])

    def get_demonstration_by_digest(self, artifact_digest: str) -> Demonstration:
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM demonstrations WHERE artifact_digest = ?",
                (artifact_digest,),
            ).fetchone()
        if row is None:
            raise ConflictError(f"Unknown demonstration digest: {artifact_digest}")
        return _demonstration_from_row(row)

    def _create_run(
        self,
        *,
        run_id: str,
        bucket: Bucket,
        inputs: dict[str, Any],
        input_digest: str,
        created_at: float,
    ) -> Run:
        inputs_json = canonical_json(inputs).decode("utf-8")
        try:
            with self.db.transaction(write=True) as connection:
                current_bucket_row = connection.execute(
                    "SELECT * FROM buckets WHERE id = ?", (bucket.id,)
                ).fetchone()
                if current_bucket_row is None:
                    raise UnknownBucketError(bucket.id)
                current_bucket = _bucket_from_row(current_bucket_row)
                if current_bucket.activated_at is None or current_bucket.level < 1:
                    raise ConflictError(
                        f"Capability is not activated for execution: {current_bucket.name}"
                    )
                playbook_row = connection.execute(
                    """
                    SELECT digest FROM playbooks
                    WHERE bucket_id = ? AND version = ?
                    """,
                    (current_bucket.id, current_bucket.playbook_version),
                ).fetchone()
                if playbook_row is None or playbook_row["digest"] != current_bucket.playbook_digest:
                    raise ConflictError("Active capability has no matching reviewed playbook")
                tool_versions_json = canonical_json(current_bucket.tool_versions).decode("utf-8")
                allowed_hosts_json = canonical_json(current_bucket.allowed_hosts).decode("utf-8")
                connection.execute(
                    """
                    INSERT INTO runs (
                      id, bucket_id, playbook_version, playbook_digest,
                      inputs_json, input_digest, target_base_url, allowed_hosts_json,
                      tool_versions_json, toolset_digest, state, action_plan_hash,
                      sdk_state_ref, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                    """,
                    (
                        run_id,
                        current_bucket.id,
                        current_bucket.playbook_version,
                        current_bucket.playbook_digest,
                        inputs_json,
                        input_digest,
                        current_bucket.target_base_url,
                        allowed_hosts_json,
                        tool_versions_json,
                        current_bucket.toolset_digest,
                        RunState.CREATED.value,
                        created_at,
                        created_at,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO run_transitions (
                      run_id, from_state, to_state, reason, created_at
                    ) VALUES (?, NULL, ?, ?, ?)
                    """,
                    (run_id, RunState.CREATED.value, "run_created", created_at),
                )
                row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        except sqlite3.IntegrityError as error:
            if "runs.id" in str(error):
                raise ConflictError(f"Run already exists: {run_id}") from error
            raise
        assert row is not None
        return _run_from_row(row)

    def get_run(self, run_id: str) -> Run:
        with self.db.transaction() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise UnknownRunError(run_id)
        return _run_from_row(row)

    def list_runs(self, *, bucket_id: int | None = None) -> list[Run]:
        query = "SELECT * FROM runs"
        parameters: tuple[Any, ...] = ()
        if bucket_id is not None:
            query += " WHERE bucket_id = ?"
            parameters = (bucket_id,)
        query += " ORDER BY created_at, id"
        with self.db.transaction() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_run_from_row(row) for row in rows]

    def _transition_run(
        self,
        run_id: str,
        *,
        expected_state: RunState,
        new_state: RunState,
        changed_at: float,
        reason: str,
    ) -> Run:
        if not reason.strip():
            raise ValueError("Transition reason cannot be blank")
        with self.db.transaction(write=True) as connection:
            row = connection.execute("SELECT state FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise UnknownRunError(run_id)
            actual_state = RunState(row["state"])
            if actual_state is not expected_state:
                raise ConflictError(
                    f"Run {run_id} is {actual_state.value}, expected {expected_state.value}"
                )
            cursor = connection.execute(
                """
                UPDATE runs SET state = ?, updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (new_state.value, changed_at, run_id, expected_state.value),
            )
            if cursor.rowcount != 1:
                raise ConflictError(f"Concurrent transition lost for run {run_id}")
            connection.execute(
                """
                INSERT INTO run_transitions (
                  run_id, from_state, to_state, reason, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, expected_state.value, new_state.value, reason, changed_at),
            )
            updated = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        assert updated is not None
        return _run_from_row(updated)

    def list_run_transitions(self, run_id: str) -> list[RunTransition]:
        self.get_run(run_id)
        with self.db.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM run_transitions WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        return [_transition_from_row(row) for row in rows]

    def _record_event(
        self,
        *,
        event_id: str,
        bucket_id: int,
        run_id: str | None,
        event_type: EventType,
        weight: float,
        severity: Severity | None,
        evidence_key: str,
        detail: dict[str, Any],
        demotion_levels: int,
        created_at: float,
    ) -> Event:
        with self.db.transaction(write=True) as connection:
            existing = connection.execute(
                "SELECT * FROM events WHERE evidence_key = ?", (evidence_key,)
            ).fetchone()
            if existing is not None:
                event = _event_from_row(existing)
                _verify_idempotent_event(
                    event,
                    bucket_id=bucket_id,
                    run_id=run_id,
                    event_type=event_type,
                    weight=weight,
                    severity=severity,
                    detail=detail,
                )
                return event
            _validate_event_ownership(connection, bucket_id, run_id)
            playbook_version = _event_playbook_version(connection, bucket_id, run_id)
            connection.execute(
                """
                INSERT INTO events (
                  id, bucket_id, run_id, type, weight, severity,
                  evidence_key, detail_json, created_at, playbook_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    bucket_id,
                    run_id,
                    event_type.value,
                    weight,
                    severity.value if severity is not None else None,
                    evidence_key,
                    canonical_json(detail).decode("utf-8"),
                    created_at,
                    playbook_version,
                ),
            )
            connection.execute(
                """
                UPDATE buckets
                SET level = MAX(0, level - ?), last_activity_at = ?
                WHERE id = ?
                """,
                (demotion_levels, created_at, bucket_id),
            )
            row = connection.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        assert row is not None
        return _event_from_row(row)

    def _transition_with_event(
        self,
        *,
        run_id: str,
        expected_state: RunState,
        new_state: RunState,
        event_id: str,
        event_type: EventType,
        weight: float,
        severity: Severity | None,
        evidence_key: str,
        detail: dict[str, Any],
        demotion_levels: int,
        created_at: float,
        reason: str,
        verdict: tuple[str, VerdictDecision, VerdictActor] | None = None,
        snapshot_trust_level: bool = False,
    ) -> tuple[Run, Event]:
        if not reason.strip():
            raise ValueError("Transition reason cannot be blank")
        with self.db.transaction(write=True) as connection:
            run_row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run_row is None:
                raise UnknownRunError(run_id)
            run = _run_from_row(run_row)
            existing = connection.execute(
                "SELECT * FROM events WHERE evidence_key = ?", (evidence_key,)
            ).fetchone()
            if existing is not None:
                event = _event_from_row(existing)
                expected_detail = detail
                if snapshot_trust_level:
                    trust_level = event.detail.get("trust_level")
                    if not isinstance(trust_level, int):
                        raise EvidenceConflictError(
                            f"Evidence {evidence_key} has no trust-level snapshot"
                        )
                    expected_detail = {**detail, "trust_level": trust_level}
                _verify_idempotent_event(
                    event,
                    bucket_id=run.bucket_id,
                    run_id=run_id,
                    event_type=event_type,
                    weight=weight,
                    severity=severity,
                    detail=expected_detail,
                )
                if run.state is not new_state:
                    raise EvidenceConflictError(
                        f"Evidence {evidence_key} exists but run is {run.state.value}"
                    )
                if verdict is not None:
                    existing_verdict = connection.execute(
                        "SELECT * FROM verdicts WHERE run_id = ?", (run_id,)
                    ).fetchone()
                    if existing_verdict is None:
                        raise EvidenceConflictError(
                            f"Evidence {evidence_key} exists without its verdict"
                        )
                    _, decision, actor = verdict
                    resolved = _verdict_from_row(existing_verdict)
                    if resolved.decision is not decision or resolved.decided_by is not actor:
                        raise EvidenceConflictError(f"Run {run_id} already has a different verdict")
                return run, event
            if run.state is not expected_state:
                raise ConflictError(
                    f"Run {run_id} is {run.state.value}, expected {expected_state.value}"
                )
            cursor = connection.execute(
                """
                UPDATE runs SET state = ?, updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (new_state.value, created_at, run_id, expected_state.value),
            )
            if cursor.rowcount != 1:
                raise ConflictError(f"Concurrent transition lost for run {run_id}")
            connection.execute(
                """
                INSERT INTO run_transitions (
                  run_id, from_state, to_state, reason, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, expected_state.value, new_state.value, reason, created_at),
            )
            if verdict is not None:
                verdict_id, decision, actor = verdict
                connection.execute(
                    """
                    INSERT INTO verdicts (id, run_id, decision, decided_by, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (verdict_id, run_id, decision.value, actor.value, created_at),
                )
            persisted_detail = detail
            if snapshot_trust_level:
                bucket_row = connection.execute(
                    "SELECT level FROM buckets WHERE id = ?", (run.bucket_id,)
                ).fetchone()
                if bucket_row is None:
                    raise UnknownBucketError(run.bucket_id)
                persisted_detail = {**detail, "trust_level": int(bucket_row["level"])}
            connection.execute(
                """
                INSERT INTO events (
                  id, bucket_id, run_id, type, weight, severity,
                  evidence_key, detail_json, created_at, playbook_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    run.bucket_id,
                    run_id,
                    event_type.value,
                    weight,
                    severity.value if severity is not None else None,
                    evidence_key,
                    canonical_json(persisted_detail).decode("utf-8"),
                    created_at,
                    run.playbook_version,
                ),
            )
            connection.execute(
                """
                UPDATE buckets
                SET level = MAX(0, level - ?), last_activity_at = ?
                WHERE id = ?
                """,
                (demotion_levels, created_at, run.bucket_id),
            )
            self._fault_injector("before_compound_commit")
            updated_run = connection.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            event_row = connection.execute(
                "SELECT * FROM events WHERE id = ?", (event_id,)
            ).fetchone()
        assert updated_run is not None and event_row is not None
        return _run_from_row(updated_run), _event_from_row(event_row)

    def record_rehearsal(
        self,
        run_id: str,
        *,
        passed: bool,
        checks: Mapping[str, bool],
        trace_ref: str,
        actions: Sequence[RecordedAction],
        action_plan_hash: str,
        event_id: str,
        event_type: EventType,
        weight: float,
        severity: Severity | None,
        demotion_levels: int,
        created_at: float,
    ) -> tuple[Run, Event]:
        """Persist the rehearsal row, its recorded actions, the plan hash, the state
        transition, and the twin_pass/twin_fail event in one transaction.

        This is the sole write path for a completed rehearsal: production
        later replays exactly this stored, immutable action plan. Only a
        *passing* rehearsal sets ``runs.action_plan_hash`` --- a failed
        rehearsal's rehearsal row and ``run_actions`` are retained purely as
        an audit trail (``rehearsal_failed`` is terminal, so they can never
        be replayed), and its run keeps a NULL plan hash.
        """

        evidence_key = f"rehearsal:{run_id}"
        new_state = RunState.REHEARSED if passed else RunState.REHEARSAL_FAILED
        detail: dict[str, Any] = {}
        with self.db.transaction(write=True) as connection:
            run_row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run_row is None:
                raise UnknownRunError(run_id)
            run = _run_from_row(run_row)
            existing_event = connection.execute(
                "SELECT * FROM events WHERE evidence_key = ?", (evidence_key,)
            ).fetchone()
            if existing_event is not None:
                event = _event_from_row(existing_event)
                _verify_idempotent_event(
                    event,
                    bucket_id=run.bucket_id,
                    run_id=run_id,
                    event_type=event_type,
                    weight=weight,
                    severity=severity,
                    detail=detail,
                )
                if run.state is not new_state:
                    raise EvidenceConflictError(
                        f"Evidence {evidence_key} exists but run is {run.state.value}"
                    )
                return run, event
            if run.state is not RunState.REHEARSING:
                raise ConflictError(
                    f"Run {run_id} is {run.state.value}, expected {RunState.REHEARSING.value}"
                )
            cursor = connection.execute(
                """
                UPDATE runs SET state = ?, action_plan_hash = ?, updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (
                    new_state.value,
                    action_plan_hash if passed else None,
                    created_at,
                    run_id,
                    RunState.REHEARSING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError(f"Concurrent rehearsal recording lost for run {run_id}")
            connection.execute(
                """
                INSERT INTO run_transitions (
                  run_id, from_state, to_state, reason, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    RunState.REHEARSING.value,
                    new_state.value,
                    "rehearsal_passed" if passed else "rehearsal_failed",
                    created_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO rehearsals (
                  id, run_id, passed, checks_json, trace_ref, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    self._id_factory(),
                    run_id,
                    1 if passed else 0,
                    canonical_json(dict(checks)).decode("utf-8"),
                    trace_ref,
                    created_at,
                ),
            )
            for action in actions:
                connection.execute(
                    """
                    INSERT INTO run_actions (
                      run_id, ordinal, tool_name, arguments_json, arguments_digest,
                      effect, status
                    ) VALUES (?, ?, ?, ?, ?, ?, 'planned')
                    """,
                    (
                        run_id,
                        action.ordinal,
                        action.tool_name,
                        canonical_json(action.arguments).decode("utf-8"),
                        action.arguments_digest,
                        action.effect,
                    ),
                )
            connection.execute(
                """
                INSERT INTO events (
                  id, bucket_id, run_id, type, weight, severity,
                  evidence_key, detail_json, created_at, playbook_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    run.bucket_id,
                    run_id,
                    event_type.value,
                    weight,
                    severity.value if severity is not None else None,
                    evidence_key,
                    canonical_json(detail).decode("utf-8"),
                    created_at,
                    run.playbook_version,
                ),
            )
            connection.execute(
                """
                UPDATE buckets
                SET level = MAX(0, level - ?), last_activity_at = ?
                WHERE id = ?
                """,
                (demotion_levels, created_at, run.bucket_id),
            )
            updated_run = connection.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            event_row = connection.execute(
                "SELECT * FROM events WHERE id = ?", (event_id,)
            ).fetchone()
        assert updated_run is not None and event_row is not None
        return _run_from_row(updated_run), _event_from_row(event_row)

    def get_verdict(self, run_id: str) -> Verdict:
        self.get_run(run_id)
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM verdicts WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise ConflictError(f"Run has no verdict: {run_id}")
        return _verdict_from_row(row)

    def get_run_actions(self, run_id: str) -> tuple[RecordedAction, ...]:
        """Load a run's stored, ordered action plan --- production replays only this."""
        self.get_run(run_id)
        with self.db.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM run_actions WHERE run_id = ? ORDER BY ordinal", (run_id,)
            ).fetchall()
        return tuple(_recorded_action_from_row(row) for row in rows)

    def set_run_action_status(self, run_id: str, ordinal: int, status: str) -> None:
        """Persist one stored action's consumption status during production replay."""
        with self.db.transaction(write=True) as connection:
            cursor = connection.execute(
                "UPDATE run_actions SET status = ? WHERE run_id = ? AND ordinal = ?",
                (status, run_id, ordinal),
            )
            if cursor.rowcount != 1:
                raise ConflictError(f"Run {run_id} has no stored action at ordinal {ordinal}")

    def get_rehearsal_checks(self, run_id: str) -> dict[str, bool]:
        """Load a run's stored six-check rehearsal report, for pre-activation re-verification."""
        self.get_run(run_id)
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT checks_json FROM rehearsals WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise ConflictError(f"Run has no stored rehearsal: {run_id}")
        return json.loads(row["checks_json"])

    def store_sdk_state(self, run_id: str, state_text: str | None) -> None:
        """Persist (or clear) the run's isolated, opaque SDK resume state."""
        with self.db.transaction(write=True) as connection:
            cursor = connection.execute(
                "UPDATE runs SET sdk_state_ref = ? WHERE id = ?", (state_text, run_id)
            )
            if cursor.rowcount != 1:
                raise UnknownRunError(run_id)

    def begin_approval_pending(
        self, run_id: str, *, sdk_state_text: str | None, changed_at: float
    ) -> Run:
        """Atomically pause an L2 run and persist its resumable SDK state."""

        with self.db.transaction(write=True) as connection:
            row = connection.execute(
                "SELECT state FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise UnknownRunError(run_id)
            actual = RunState(row["state"])
            if actual is not RunState.REHEARSED:
                raise ConflictError(
                    f"Run {run_id} is {actual.value}, expected {RunState.REHEARSED.value}"
                )
            connection.execute(
                """
                UPDATE runs SET state = ?, sdk_state_ref = ?, updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (
                    RunState.APPROVAL_PENDING.value,
                    sdk_state_text,
                    changed_at,
                    run_id,
                    RunState.REHEARSED.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO run_transitions (
                  run_id, from_state, to_state, reason, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    RunState.REHEARSED.value,
                    RunState.APPROVAL_PENDING.value,
                    "approval_requested",
                    changed_at,
                ),
            )
            self._fault_injector("before_compound_commit")
            updated = connection.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        assert updated is not None
        return _run_from_row(updated)

    def consume_sdk_state(self, run_id: str) -> str | None:
        """Atomically consume resume state from an authorized run exactly once."""

        with self.db.transaction(write=True) as connection:
            row = connection.execute(
                "SELECT state, sdk_state_ref FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise UnknownRunError(run_id)
            if RunState(row["state"]) is not RunState.AUTHORIZED:
                return None
            state_text = row["sdk_state_ref"]
            if state_text is not None:
                connection.execute(
                    """
                    UPDATE runs SET sdk_state_ref = NULL
                    WHERE id = ? AND sdk_state_ref IS NOT NULL
                    """,
                    (run_id,),
                )
            return state_text

    def begin_veto_window(
        self,
        run_id: str,
        *,
        veto_id: str,
        deadline: float,
        sdk_state_text: str | None,
        changed_at: float,
    ) -> tuple[Run, Veto]:
        """Open a run's one L3 veto countdown atomically with its state transition.

        Persists the ``rehearsed`` -> ``veto_pending`` transition, the one
        countdown row (``vetoes.run_id`` is UNIQUE: a second call for the same
        run raises), and the resumable SDK state, all in the one transaction
        --- a crash between them can never strand a ``veto_pending`` run with
        no veto row, or a veto row whose run never actually moved.
        """
        with self.db.transaction(write=True) as connection:
            run_row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run_row is None:
                raise UnknownRunError(run_id)
            run = _run_from_row(run_row)
            if run.state is not RunState.REHEARSED:
                raise ConflictError(
                    f"Run {run_id} is {run.state.value}, expected {RunState.REHEARSED.value}"
                )
            cursor = connection.execute(
                """
                UPDATE runs SET state = ?, sdk_state_ref = ?, updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (
                    RunState.VETO_PENDING.value,
                    sdk_state_text,
                    changed_at,
                    run_id,
                    RunState.REHEARSED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError(f"Concurrent transition lost for run {run_id}")
            connection.execute(
                """
                INSERT INTO run_transitions (
                  run_id, from_state, to_state, reason, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    RunState.REHEARSED.value,
                    RunState.VETO_PENDING.value,
                    "veto_window_started",
                    changed_at,
                ),
            )
            try:
                connection.execute(
                    """
                    INSERT INTO vetoes (id, run_id, deadline, status, resolved_at)
                    VALUES (?, ?, ?, ?, NULL)
                    """,
                    (veto_id, run_id, deadline, VetoStatus.PENDING.value),
                )
            except sqlite3.IntegrityError as error:
                if "vetoes.run_id" in str(error):
                    raise ConflictError(f"Run already has a veto countdown: {run_id}") from error
                raise
            self._fault_injector("before_compound_commit")
            updated_run = connection.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            veto_row = connection.execute(
                "SELECT * FROM vetoes WHERE run_id = ?", (run_id,)
            ).fetchone()
        assert updated_run is not None and veto_row is not None
        return _run_from_row(updated_run), _veto_from_row(veto_row)

    def get_veto(self, run_id: str) -> Veto:
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM vetoes WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise ConflictError(f"Run has no veto countdown: {run_id}")
        return _veto_from_row(row)

    def resolve_veto_with_event(
        self,
        run_id: str,
        *,
        from_status: VetoStatus,
        to_status: VetoStatus,
        resolved_at: float,
        expected_state: RunState,
        new_state: RunState,
        event_id: str,
        event_type: EventType,
        weight: float,
        severity: Severity | None,
        evidence_key: str,
        detail: dict[str, Any],
        demotion_levels: int,
        created_at: float,
        reason: str,
        verdict: tuple[str, VerdictDecision, VerdictActor] | None = None,
    ) -> tuple[Run, Event] | None:
        """Compare-and-swap a veto's status and transition its run, in one transaction.

        ``None`` means the veto was already resolved (canceled or expired) by
        an earlier call --- exactly like the CAS-only predecessor this
        replaces, only the caller whose CAS actually flips ``pending`` to a
        terminal status ever proceeds to transition the run, so a canceled or
        expired veto can never be resolved twice. Folding the veto CAS and the
        run's terminal transition (plus its verdict/event/demotion) into the
        same transaction closes the crash window where a resolved veto could
        previously be left with a run that never actually transitioned, or
        vice versa.
        """
        with self.db.transaction(write=True) as connection:
            veto_row = connection.execute(
                "SELECT * FROM vetoes WHERE run_id = ?", (run_id,)
            ).fetchone()
            if veto_row is None:
                raise ConflictError(f"Run has no veto countdown: {run_id}")
            if VetoStatus(veto_row["status"]) is not from_status:
                return None
            veto_cursor = connection.execute(
                """
                UPDATE vetoes SET status = ?, resolved_at = ?
                WHERE run_id = ? AND status = ?
                """,
                (to_status.value, resolved_at, run_id, from_status.value),
            )
            if veto_cursor.rowcount != 1:
                return None
            run_row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run_row is None:
                raise UnknownRunError(run_id)
            run = _run_from_row(run_row)
            if run.state is not expected_state:
                raise ConflictError(
                    f"Run {run_id} is {run.state.value}, expected {expected_state.value}"
                )
            run_cursor = connection.execute(
                """
                UPDATE runs SET state = ?, updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (new_state.value, created_at, run_id, expected_state.value),
            )
            if run_cursor.rowcount != 1:
                raise ConflictError(f"Concurrent transition lost for run {run_id}")
            connection.execute(
                """
                INSERT INTO run_transitions (
                  run_id, from_state, to_state, reason, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, expected_state.value, new_state.value, reason, created_at),
            )
            if verdict is not None:
                verdict_id, decision, actor = verdict
                connection.execute(
                    """
                    INSERT INTO verdicts (id, run_id, decision, decided_by, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (verdict_id, run_id, decision.value, actor.value, created_at),
                )
            connection.execute(
                """
                INSERT INTO events (
                  id, bucket_id, run_id, type, weight, severity,
                  evidence_key, detail_json, created_at, playbook_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    run.bucket_id,
                    run_id,
                    event_type.value,
                    weight,
                    severity.value if severity is not None else None,
                    evidence_key,
                    canonical_json(detail).decode("utf-8"),
                    created_at,
                    run.playbook_version,
                ),
            )
            connection.execute(
                """
                UPDATE buckets
                SET level = MAX(0, level - ?), last_activity_at = ?
                WHERE id = ?
                """,
                (demotion_levels, created_at, run.bucket_id),
            )
            self._fault_injector("before_compound_commit")
            updated_run = connection.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            event_row = connection.execute(
                "SELECT * FROM events WHERE id = ?", (event_id,)
            ).fetchone()
        assert updated_run is not None and event_row is not None
        return _run_from_row(updated_run), _event_from_row(event_row)

    def list_events(self, bucket_id: int, *, run_id: str | None = None) -> list[Event]:
        query = "SELECT * FROM events WHERE bucket_id = ?"
        parameters: list[Any] = [bucket_id]
        if run_id is not None:
            query += " AND run_id = ?"
            parameters.append(run_id)
        query += " ORDER BY created_at, id"
        with self.db.transaction() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_event_from_row(row) for row in rows]

    def confirm_promotion_if_eligible(
        self, bucket_id: int, policy: Policy, *, now: float
    ) -> Bucket:
        """Re-evaluate and apply one promotion under the same write lock."""

        from apprentice.ledger.scoring import evaluate_promotion

        with self.db.transaction(write=True) as connection:
            bucket_row = connection.execute(
                "SELECT * FROM buckets WHERE id = ?", (bucket_id,)
            ).fetchone()
            if bucket_row is None:
                raise UnknownBucketError(bucket_id)
            bucket = _bucket_from_row(bucket_row)
            event_rows = connection.execute(
                "SELECT * FROM events WHERE bucket_id = ? ORDER BY created_at, id",
                (bucket_id,),
            ).fetchall()
            demonstration_row = connection.execute(
                "SELECT COUNT(*) FROM demonstrations WHERE bucket_id = ? AND role = 'training'",
                (bucket_id,),
            ).fetchone()
            assert demonstration_row is not None
            evaluation = evaluate_promotion(
                bucket,
                [_event_from_row(row) for row in event_rows],
                policy,
                training_demonstrations=int(demonstration_row[0]),
                now=now,
            )
            if not evaluation.eligible or evaluation.target_level is None:
                raise ConflictError(f"Bucket {bucket_id} is not eligible for promotion")
            cursor = connection.execute(
                """
                UPDATE buckets SET level = ?, last_activity_at = ?
                WHERE id = ? AND level = ? AND playbook_version = ?
                """,
                (
                    evaluation.target_level,
                    now,
                    bucket_id,
                    bucket.level,
                    bucket.playbook_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError(f"Concurrent promotion lost for bucket {bucket_id}")
            updated = connection.execute(
                "SELECT * FROM buckets WHERE id = ?", (bucket_id,)
            ).fetchone()
        assert updated is not None
        return _bucket_from_row(updated)

    def apply_idle_decay_if_due(self, bucket_id: int, policy: Policy, *, now: float) -> Bucket:
        """Evaluate and persist lazy decay under one write lock."""

        with self.db.transaction(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM buckets WHERE id = ?", (bucket_id,)
            ).fetchone()
            if row is None:
                raise UnknownBucketError(bucket_id)
            bucket = _bucket_from_row(row)
            interval_seconds = policy.decay_idle_days * 86_400
            reference = (
                bucket.last_decay_at
                if bucket.last_decay_at is not None
                else bucket.last_activity_at
            )
            due = (
                bucket.level > 0
                and now - bucket.last_activity_at >= interval_seconds
                and now - reference >= interval_seconds
            )
            if due:
                connection.execute(
                    "UPDATE buckets SET level = level - 1, last_decay_at = ? WHERE id = ?",
                    (now, bucket_id),
                )
                row = connection.execute(
                    "SELECT * FROM buckets WHERE id = ?", (bucket_id,)
                ).fetchone()
        assert row is not None
        return _bucket_from_row(row)

    def record_self_tool_failure(self, bucket_id: int, *, detail: dict[str, Any]) -> Bucket:
        """Record a major runtime failure and immediately demote a self-authored tool."""

        timestamp = self._clock()
        event_id = self._id_factory()
        with self.db.transaction(write=True) as connection:
            row = connection.execute("SELECT * FROM buckets WHERE id = ?", (bucket_id,)).fetchone()
            if row is None:
                raise UnknownBucketError(bucket_id)
            bucket = _bucket_from_row(row)
            if (
                bucket.kind is not BucketKind.TOOL
                or bucket.origin is not BucketOrigin.SELF_AUTHORED
            ):
                raise ConflictError("Self-tool failure evidence requires a self-authored tool")
            connection.execute(
                "UPDATE buckets SET level = MAX(0, level - 1), last_activity_at = ? WHERE id = ?",
                (timestamp, bucket_id),
            )
            connection.execute(
                """
                INSERT INTO events (
                  id, bucket_id, run_id, type, weight, severity,
                  evidence_key, detail_json, created_at, playbook_version
                ) VALUES (?, ?, NULL, ?, 1.0, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    bucket_id,
                    EventType.TWIN_FAIL.value,
                    Severity.MAJOR.value,
                    f"tool_failure:{event_id}",
                    canonical_json(detail).decode("utf-8"),
                    timestamp,
                    bucket.playbook_version,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM buckets WHERE id = ?", (bucket_id,)
            ).fetchone()
        assert updated is not None
        return _bucket_from_row(updated)

    def count_events(self, bucket_id: int) -> int:
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM events WHERE bucket_id = ?", (bucket_id,)
            ).fetchone()
        assert row is not None
        return int(row[0])


def _bucket_exists(connection: sqlite3.Connection, bucket_id: int) -> bool:
    return (
        connection.execute("SELECT 1 FROM buckets WHERE id = ?", (bucket_id,)).fetchone()
        is not None
    )


def _run_exists(connection: sqlite3.Connection, run_id: str) -> bool:
    return connection.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone() is not None


def _normalize_approved_host(host: str) -> str:
    normalized = host.strip()
    if not normalized or "/" in normalized or "://" in normalized:
        raise ValueError(f"Invalid approved host: {host}")
    return normalize_hostname(normalized)


def _validate_event_ownership(
    connection: sqlite3.Connection, bucket_id: int, run_id: str | None
) -> None:
    if not _bucket_exists(connection, bucket_id):
        raise UnknownBucketError(bucket_id)
    if run_id is None:
        return
    row = connection.execute("SELECT bucket_id FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise UnknownRunError(run_id)
    if row["bucket_id"] != bucket_id:
        raise ConflictError(f"Run {run_id} does not belong to bucket {bucket_id}")


def _event_playbook_version(
    connection: sqlite3.Connection, bucket_id: int, run_id: str | None
) -> int:
    if run_id is not None:
        row = connection.execute(
            "SELECT playbook_version FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT playbook_version FROM buckets WHERE id = ?", (bucket_id,)
        ).fetchone()
    if row is None:
        raise UnknownRunError(run_id) if run_id is not None else UnknownBucketError(bucket_id)
    return int(row["playbook_version"])


def _verify_idempotent_event(
    event: Event,
    *,
    bucket_id: int,
    run_id: str | None,
    event_type: EventType,
    weight: float,
    severity: Severity | None,
    detail: dict[str, Any],
) -> None:
    # A reloaded Event.detail deep-freezes lists to tuples (FrozenModel), while a
    # freshly supplied detail keeps plain lists; compare canonical encodings
    # rather than Python equality so that difference alone is never a conflict.
    if (
        event.bucket_id != bucket_id
        or event.run_id != run_id
        or event.type is not event_type
        or event.weight != weight
        or event.severity is not severity
        or canonical_json(event.detail) != canonical_json(detail)
    ):
        raise EvidenceConflictError(
            f"Evidence key already exists with different content: {event.evidence_key}"
        )


def _bucket_from_row(row: sqlite3.Row) -> Bucket:
    return Bucket(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        risk_class=row["risk_class"],
        level=row["level"],
        origin=row["origin"],
        playbook_version=row["playbook_version"],
        playbook_digest=row["playbook_digest"],
        target_base_url=row["target_base_url"],
        allowed_hosts=json.loads(row["allowed_hosts_json"]),
        tool_versions=json.loads(row["tool_versions_json"]),
        toolset_digest=row["toolset_digest"],
        created_at=row["created_at"],
        last_activity_at=row["last_activity_at"],
        last_decay_at=row["last_decay_at"],
        activated_at=row["activated_at"],
    )


def _playbook_from_row(row: sqlite3.Row) -> ReviewedPlaybook:
    return ReviewedPlaybook(
        bucket_id=row["bucket_id"],
        version=row["version"],
        content=json.loads(row["content_json"]),
        digest=row["digest"],
        reviewed_at=row["reviewed_at"],
    )


def _demonstration_from_row(row: sqlite3.Row) -> Demonstration:
    return Demonstration(
        id=row["id"],
        bucket_id=row["bucket_id"],
        artifact_ref=row["artifact_ref"],
        artifact_digest=row["artifact_digest"],
        role=row["role"],
        created_at=row["created_at"],
    )


def _run_from_row(row: sqlite3.Row) -> Run:
    return Run(
        id=row["id"],
        bucket_id=row["bucket_id"],
        playbook_version=row["playbook_version"],
        playbook_digest=row["playbook_digest"],
        inputs=json.loads(row["inputs_json"]),
        input_digest=row["input_digest"],
        target_base_url=row["target_base_url"],
        allowed_hosts=json.loads(row["allowed_hosts_json"]),
        tool_versions=json.loads(row["tool_versions_json"]),
        toolset_digest=row["toolset_digest"],
        state=row["state"],
        action_plan_hash=row["action_plan_hash"],
        sdk_state_ref=row["sdk_state_ref"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _transition_from_row(row: sqlite3.Row) -> RunTransition:
    return RunTransition(
        id=row["id"],
        run_id=row["run_id"],
        from_state=row["from_state"],
        to_state=row["to_state"],
        reason=row["reason"],
        created_at=row["created_at"],
    )


def _event_from_row(row: sqlite3.Row) -> Event:
    return Event(
        id=row["id"],
        bucket_id=row["bucket_id"],
        run_id=row["run_id"],
        type=row["type"],
        weight=row["weight"],
        severity=row["severity"],
        evidence_key=row["evidence_key"],
        detail=json.loads(row["detail_json"]),
        created_at=row["created_at"],
        playbook_version=row["playbook_version"],
    )


def _verdict_from_row(row: sqlite3.Row) -> Verdict:
    return Verdict(
        id=row["id"],
        run_id=row["run_id"],
        decision=row["decision"],
        decided_by=row["decided_by"],
        created_at=row["created_at"],
    )


def _recorded_action_from_row(row: sqlite3.Row) -> RecordedAction:
    return RecordedAction(
        ordinal=row["ordinal"],
        tool_name=row["tool_name"],
        arguments=json.loads(row["arguments_json"]),
        arguments_digest=row["arguments_digest"],
        effect=row["effect"],
    )


def _veto_from_row(row: sqlite3.Row) -> Veto:
    return Veto(
        id=row["id"],
        run_id=row["run_id"],
        deadline=row["deadline"],
        status=row["status"],
        resolved_at=row["resolved_at"],
    )
