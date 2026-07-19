from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

RUN_STATES = (
    "created",
    "rehearsing",
    "rehearsal_failed",
    "rehearsed",
    "approval_pending",
    "veto_pending",
    "authorized",
    "denied",
    "executing",
    "succeeded",
    "failed",
    "vetoed",
    "expired",
)
EVENT_TYPES = (
    "shadow_pass",
    "shadow_fail",
    "twin_pass",
    "twin_fail",
    "approved",
    "approved_with_edits",
    "vetoed",
    "run_success",
    "run_failure",
    "audit_pass",
    "audit_fail",
)


def _quoted(values: tuple[str, ...]) -> str:
    return ",".join(f"'{value}'" for value in values)


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS buckets (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('playbook', 'tool')),
  risk_class TEXT NOT NULL CHECK(risk_class IN ('low','medium','high','critical')),
  level INTEGER NOT NULL CHECK(level BETWEEN 0 AND 4),
  origin TEXT NOT NULL CHECK(origin IN ('demonstrated','self_authored')),
  playbook_version INTEGER NOT NULL CHECK(playbook_version >= 1),
  playbook_digest TEXT NOT NULL,
  target_base_url TEXT NOT NULL,
  allowed_hosts_json TEXT NOT NULL,
  tool_versions_json TEXT NOT NULL,
  toolset_digest TEXT NOT NULL,
  created_at REAL NOT NULL,
  last_activity_at REAL NOT NULL,
  last_decay_at REAL,
  activated_at REAL,
  CHECK(NOT (risk_class = 'critical' AND level > 3))
);

CREATE TABLE IF NOT EXISTS playbooks (
  bucket_id INTEGER NOT NULL REFERENCES buckets(id),
  version INTEGER NOT NULL CHECK(version >= 1),
  content_json TEXT NOT NULL,
  digest TEXT NOT NULL,
  reviewed_at REAL NOT NULL,
  PRIMARY KEY(bucket_id, version),
  UNIQUE(bucket_id, digest)
);

CREATE TABLE IF NOT EXISTS demonstrations (
  id TEXT PRIMARY KEY,
  bucket_id INTEGER NOT NULL REFERENCES buckets(id),
  artifact_ref TEXT NOT NULL,
  artifact_digest TEXT UNIQUE NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('training','heldout')),
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  bucket_id INTEGER NOT NULL REFERENCES buckets(id),
  playbook_version INTEGER NOT NULL CHECK(playbook_version >= 1),
  playbook_digest TEXT NOT NULL,
  inputs_json TEXT NOT NULL,
  input_digest TEXT NOT NULL,
  target_base_url TEXT NOT NULL,
  allowed_hosts_json TEXT NOT NULL,
  tool_versions_json TEXT NOT NULL,
  toolset_digest TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ({_quoted(RUN_STATES)})),
  action_plan_hash TEXT,
  sdk_state_ref TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS run_transitions (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  from_state TEXT CHECK(from_state IS NULL OR from_state IN ({_quoted(RUN_STATES)})),
  to_state TEXT NOT NULL CHECK(to_state IN ({_quoted(RUN_STATES)})),
  reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS rehearsals (
  id TEXT PRIMARY KEY,
  run_id TEXT UNIQUE NOT NULL REFERENCES runs(id),
  passed INTEGER NOT NULL CHECK(passed IN (0, 1)),
  checks_json TEXT NOT NULL,
  trace_ref TEXT NOT NULL,
  completed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS run_actions (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
  tool_name TEXT NOT NULL CHECK(tool_name IN ('navigate','fill','upload','click','commit')),
  arguments_json TEXT NOT NULL,
  arguments_digest TEXT NOT NULL,
  effect TEXT NOT NULL CHECK(effect IN ('observe','prepare','commit')),
  status TEXT NOT NULL CHECK(status IN ('planned','executing','succeeded','failed')),
  UNIQUE(run_id, ordinal)
);

CREATE TABLE IF NOT EXISTS verdicts (
  id TEXT PRIMARY KEY,
  run_id TEXT UNIQUE NOT NULL REFERENCES runs(id),
  decision TEXT NOT NULL CHECK(decision IN ('approved','denied')),
  decided_by TEXT NOT NULL CHECK(decided_by IN ('human','system')),
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS vetoes (
  id TEXT PRIMARY KEY,
  run_id TEXT UNIQUE NOT NULL REFERENCES runs(id),
  deadline REAL NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','vetoed','authorized','expired')),
  resolved_at REAL
);

CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  bucket_id INTEGER NOT NULL REFERENCES buckets(id),
  run_id TEXT REFERENCES runs(id),
  type TEXT NOT NULL CHECK(type IN ({_quoted(EVENT_TYPES)})),
  weight REAL NOT NULL CHECK(weight > 0),
  severity TEXT CHECK(severity IS NULL OR severity IN ('minor','major','critical')),
  evidence_key TEXT UNIQUE NOT NULL,
  detail_json TEXT NOT NULL,
  created_at REAL NOT NULL,
  playbook_version INTEGER NOT NULL CHECK(playbook_version >= 1)
);

CREATE INDEX IF NOT EXISTS idx_events_bucket_created
ON events(bucket_id, created_at);

CREATE INDEX IF NOT EXISTS idx_run_transitions_run
ON run_transitions(run_id, id);

-- Incident Command simulations deliberately live beside, but are isolated
-- from, the trust ledger.  A run's latest world state and every causal event
-- snapshot are stored atomically so a browser refresh or process restart can
-- never turn replay into a different simulation.
CREATE TABLE IF NOT EXISTS incident_runs (
  id TEXT PRIMARY KEY,
  scenario_id TEXT NOT NULL,
  scenario_version INTEGER NOT NULL CHECK(scenario_version >= 1),
  seed INTEGER NOT NULL,
  state_json TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS incident_events (
  incident_id TEXT NOT NULL REFERENCES incident_runs(id) ON DELETE CASCADE,
  event_index INTEGER NOT NULL CHECK(event_index >= 0),
  event_json TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  PRIMARY KEY(incident_id, event_index)
);

-- Learner progression is intentionally separate from the autonomous-agent
-- trust ledger.  It records only human simulation attempts and the immutable
-- evidence-backed debrief captured when an incident reaches an outcome.
CREATE TABLE IF NOT EXISTS incident_learners (
  id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL CHECK(length(trim(display_name)) > 0),
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS incident_learner_runs (
  incident_id TEXT PRIMARY KEY REFERENCES incident_runs(id) ON DELETE CASCADE,
  learner_id TEXT NOT NULL REFERENCES incident_learners(id) ON DELETE CASCADE,
  completed_at REAL,
  outcome TEXT CHECK(outcome IS NULL OR outcome IN ('recovered', 'terminal_escalation')),
  debrief_json TEXT,
  CHECK((completed_at IS NULL AND outcome IS NULL AND debrief_json IS NULL) OR
        (completed_at IS NOT NULL AND outcome IS NOT NULL AND debrief_json IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_incident_learner_runs_history
ON incident_learner_runs(learner_id, completed_at DESC);

-- The dojo keeps its typed practice document here. Authored sessions point at
-- incident_runs; generated sessions persist their immutable spec and runtime
-- snapshot directly in session_json, so incident_id is an opaque runtime id.
CREATE TABLE IF NOT EXISTS practice_sessions (
  id TEXT PRIMARY KEY,
  incident_id TEXT UNIQUE NOT NULL,
  session_json TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
"""


class SQLiteDatabase:
    """Open a fresh SQLite connection for each managed operation."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5_000) -> None:
        if str(path) == ":memory:":
            raise ValueError("Repository requires a file-backed SQLite path")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.busy_timeout_ms = busy_timeout_ms
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
            practice_foreign_keys = connection.execute(
                "PRAGMA foreign_key_list(practice_sessions)"
            ).fetchall()
            if practice_foreign_keys:
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    ALTER TABLE practice_sessions RENAME TO practice_sessions_authored;
                    CREATE TABLE practice_sessions (
                      id TEXT PRIMARY KEY,
                      incident_id TEXT UNIQUE NOT NULL,
                      session_json TEXT NOT NULL,
                      created_at REAL NOT NULL,
                      updated_at REAL NOT NULL
                    );
                    INSERT INTO practice_sessions(
                      id, incident_id, session_json, created_at, updated_at
                    )
                    SELECT id, incident_id, session_json, created_at, updated_at
                    FROM practice_sessions_authored;
                    DROP TABLE practice_sessions_authored;
                    COMMIT;
                    """
                )
                connection.execute("PRAGMA foreign_keys = ON")
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(events)").fetchall()
            }
            if "playbook_version" not in columns:
                connection.execute("ALTER TABLE events ADD COLUMN playbook_version INTEGER")
                connection.execute(
                    """
                    UPDATE events
                    SET playbook_version = COALESCE(
                      (SELECT runs.playbook_version FROM runs WHERE runs.id = events.run_id),
                      (SELECT buckets.playbook_version FROM buckets
                       WHERE buckets.id = events.bucket_id)
                    )
                    """
                )
        finally:
            connection.close()

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
