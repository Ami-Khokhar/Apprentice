"""Codex-backed facilitation over generated deterministic practice worlds."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, Protocol, TypeVar, cast
from uuid import uuid4

from pydantic import BaseModel

from apprentice.database import SQLiteDatabase
from apprentice.incident.generated import (
    GeneratedScenarioError,
    GeneratedScenarioRuntime,
    GeneratedScenarioSpec,
    is_near_duplicate,
)

from .codex_runner import AgentSpec, CodexOutputError, CodexStructuredRunner
from .contracts import (
    EvidenceReference,
    FacilitatorDecision,
    FinalDebrief,
    LearnerProfile,
    PracticeSession,
    PracticeTurn,
    ScenarioBlueprint,
)

TOutput = TypeVar("TOutput", bound=BaseModel)
StructuredRunner = Callable[[AgentSpec, str], BaseModel]
DEFAULT_PRACTICE_MODEL = "gpt-5.6-terra"
MAX_GENERATION_ATTEMPTS = 3


class PracticeSessionNotFoundError(KeyError):
    pass


class InvalidPracticeResponseError(ValueError):
    pass


class PracticeNotReadyForDebriefError(ValueError):
    pass


class SessionStore(Protocol):
    def get(self, session_id: str) -> PracticeSession | None: ...

    def save(self, session: PracticeSession) -> None: ...

    def generated_history(self, profile: LearnerProfile) -> tuple[GeneratedScenarioSpec, ...]: ...


class MemorySessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, PracticeSession] = {}

    def get(self, session_id: str) -> PracticeSession | None:
        return self._sessions.get(session_id)

    def save(self, session: PracticeSession) -> None:
        self._sessions[session.id] = session

    def generated_history(self, profile: LearnerProfile) -> tuple[GeneratedScenarioSpec, ...]:
        scope = _profile_scope(profile)
        return tuple(
            session.generated_spec
            for session in sorted(
                self._sessions.values(), key=lambda item: item.created_at, reverse=True
            )
            if _profile_scope(session.profile) == scope
        )


class SQLiteSessionStore:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._database = database

    def get(self, session_id: str) -> PracticeSession | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                """
                SELECT session_json
                FROM practice_sessions
                WHERE id = ?
                  AND json_type(session_json, '$.generated_spec') = 'object'
                """,
                (session_id,),
            ).fetchone()
        return None if row is None else PracticeSession.model_validate_json(row["session_json"])

    def save(self, session: PracticeSession) -> None:
        with self._database.transaction(write=True) as connection:
            connection.execute(
                """
                INSERT INTO practice_sessions(
                  id, incident_id, session_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  session_json = excluded.session_json,
                  updated_at = excluded.updated_at
                """,
                (
                    session.id,
                    session.incident_id,
                    session.model_dump_json(),
                    session.created_at,
                    session.updated_at,
                ),
            )

    def generated_history(self, profile: LearnerProfile) -> tuple[GeneratedScenarioSpec, ...]:
        scope = _profile_scope(profile)
        with self._database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT session_json
                FROM practice_sessions
                WHERE json_type(session_json, '$.generated_spec') = 'object'
                ORDER BY created_at DESC
                """
            ).fetchall()
        sessions = (PracticeSession.model_validate_json(row["session_json"]) for row in rows)
        return tuple(
            session.generated_spec
            for session in sessions
            if _profile_scope(session.profile) == scope
        )


class PracticeService:
    """Coordinate AI framing and coaching without granting AI world authority."""

    def __init__(
        self,
        *,
        database: SQLiteDatabase | None = None,
        runner: StructuredRunner | None = None,
        model: str = DEFAULT_PRACTICE_MODEL,
        store: SessionStore | None = None,
        generated_runtime: GeneratedScenarioRuntime | None = None,
    ) -> None:
        self._runner = runner if runner is not None else CodexStructuredRunner(timeout_seconds=120)
        self._model = model
        self._store = store or (SQLiteSessionStore(database) if database else MemorySessionStore())
        self._generated_runtime = generated_runtime or GeneratedScenarioRuntime()

    def start(self, field: str, work_context: str | None = None) -> PracticeSession:
        profile = LearnerProfile(
            field=field.strip(),
            work_context=work_context.strip() if work_context and work_context.strip() else None,
        )
        history = self._store.generated_history(profile)
        spec: GeneratedScenarioSpec | None = None
        last_output_error: CodexOutputError | None = None
        for attempt in range(MAX_GENERATION_ATTEMPTS):
            nonce = str(uuid4())
            try:
                candidate = self._run(
                    self._generator_agent(),
                    self._generation_prompt(profile, history, attempt, nonce),
                    GeneratedScenarioSpec,
                )
            except CodexOutputError as error:
                last_output_error = error
                continue
            if candidate.generation_nonce != nonce:
                continue
            if not any(is_near_duplicate(candidate, prior) for prior in history):
                spec = candidate
                break
        if spec is None:
            if last_output_error is not None:
                raise last_output_error
            raise InvalidPracticeResponseError(
                "The guide could not create a sufficiently different situation. Please try again."
            )
        world = self._generated_world(self._generated_runtime.create(spec))
        blueprint = self._blueprint_from_spec(spec)
        now = time.time()
        session = PracticeSession(
            id=str(uuid4()),
            profile=profile,
            scenario=blueprint,
            incident_id=cast(str, world["id"]),
            world=world,
            generated_spec=spec,
            created_at=now,
            updated_at=now,
        )
        self._store.save(session)
        return session

    def get_session(self, session_id: str) -> PracticeSession:
        session = self._store.get(session_id)
        if session is None:
            raise PracticeSessionNotFoundError(f"Unknown practice session: {session_id}")
        try:
            world = self._generated_runtime.snapshot(session.incident_id)
        except GeneratedScenarioError:
            world = self._generated_runtime.restore(session.generated_spec, session.world)
        world = self._generated_world(world)
        if world != session.world:
            session = session.model_copy(update={"world": world, "updated_at": time.time()})
            self._store.save(session)
        return session

    def respond(self, session_id: str, response: str) -> PracticeSession:
        session = self.get_session(session_id)
        response = response.strip()
        if not response:
            raise InvalidPracticeResponseError("A response is required")
        if len(response) > 8_000:
            raise InvalidPracticeResponseError("A response cannot exceed 8000 characters")
        if session.debrief is not None:
            raise InvalidPracticeResponseError("This practice session has already been debriefed")
        if session.world.get("completed") or session.world.get("terminal"):
            raise InvalidPracticeResponseError("This practice scenario has already concluded")

        decision = self._run(
            self._facilitator_agent(),
            self._facilitator_prompt(session, response),
            FacilitatorDecision,
        )
        enabled_actions = {
            str(action["kind"])
            for action in cast(list[dict[str, object]], session.world["available_actions"])
            if action.get("enabled")
        }
        if decision.action_kind is not None and decision.action_kind not in enabled_actions:
            raise InvalidPracticeResponseError(
                f"Facilitator selected unavailable action: {decision.action_kind}"
            )
        if decision.assessment.response_excerpt not in response:
            raise InvalidPracticeResponseError(
                "Facilitator response_excerpt was not present in the learner response"
            )
        if decision.action_kind is not None and not decision.assessment.evidence:
            raise InvalidPracticeResponseError(
                "An actionable assessment must cite canonical evidence"
            )
        # Validate every claimed source before mutating the deterministic
        # world. A malformed model response must fail closed with no partial
        # incident transition.
        self._validate_evidence(decision.assessment.evidence, session.world)

        world = session.world
        if decision.action_kind is not None:
            world = self._generated_world(
                self._generated_runtime.apply(session.incident_id, decision.action_kind)
            )
        turn = PracticeTurn(
            response=response,
            action_kind=decision.action_kind,
            coaching_question=decision.coaching_question,
            assessment=decision.assessment,
            event_count=len(cast(list[object], world["events"])),
            outcome=cast(Any, world["outcome"]),
        )
        updated = session.model_copy(
            update={
                "world": world,
                "turns": (*session.turns, turn),
                "updated_at": time.time(),
            }
        )
        self._store.save(updated)
        return updated

    def get_debrief(self, session_id: str) -> FinalDebrief:
        session = self.get_session(session_id)
        if session.debrief is not None:
            return session.debrief
        if not session.world.get("completed") and not session.world.get("terminal"):
            raise PracticeNotReadyForDebriefError(
                "The scenario must reach recovery or terminal escalation before debrief"
            )
        debrief = self._run(self._debrief_agent(), self._debrief_prompt(session), FinalDebrief)
        self._validate_evidence(debrief.evidence, session.world)
        updated = session.model_copy(update={"debrief": debrief, "updated_at": time.time()})
        self._store.save(updated)
        return debrief

    def _generator_agent(self) -> AgentSpec:
        return AgentSpec(
            name="dojo-scenario-generator",
            instructions=(
                "Create one entirely new, realistic stress-test situation for the learner's "
                "actual field and work context. Return a complete GeneratedScenarioSpec. The "
                "world must have a specific failure mechanism, a meaningful decision tradeoff, "
                "observable evidence, bounded metrics, an acyclic action graph, consequences, "
                "at least one terminal timed escalation, achievable success requirements, and a "
                "grounded rubric. At least two actions must be enabled initially. Every success "
                "requirement must be reachable through the action effects and prerequisites "
                "before the first terminal escalation. Schedule that terminal event late enough "
                "for the full recovery path. Keep all IDs and references exact, and keep every "
                "set effect within its metric bounds. Use the supplied generation_nonce exactly. "
                "Avoid every prior challenge, mechanism, and tradeoff supplied. Do not merely "
                "rename an old world."
            ),
            model=self._model,
            output_type=GeneratedScenarioSpec,
        )

    def _facilitator_agent(self) -> AgentSpec:
        return AgentSpec(
            name="dojo-decision-facilitator",
            instructions=(
                "Interpret the learner's proposed next step against the supplied canonical world. "
                "Map it to at most one enabled action. If intent cannot be mapped safely, request "
                "one precise clarification and select no action. Never change metrics, events, "
                "availability, or outcomes yourself. Assess reasoning briefly and cite only IDs "
                "present in the payload using event, artifact, metric, or action references. "
                "Copy assessment.response_excerpt verbatim from learner_response as one "
                "contiguous substring, preserving exact case and punctuation."
            ),
            model=self._model,
            output_type=FacilitatorDecision,
        )

    def _debrief_agent(self) -> AgentSpec:
        return AgentSpec(
            name="dojo-practice-debrief",
            instructions=(
                "Produce a concise professional debrief grounded only in the canonical world, its "
                "deterministic rubric, and the learner transcript. Separate observed strengths "
                "from missed evidence and risky assumptions. Offer a better ordered decision path. "
                "Cite only IDs present in the payload; do not invent causes or outcomes."
            ),
            model=self._model,
            output_type=FinalDebrief,
        )

    def _run(
        self, agent: AgentSpec, prompt: str, output_type: type[TOutput]
    ) -> TOutput:
        output = self._runner(agent, prompt)
        if not isinstance(output, output_type):
            raise TypeError(
                f"Practice runner returned {type(output).__name__}; expected {output_type.__name__}"
            )
        return output

    @staticmethod
    def _generation_prompt(
        profile: LearnerProfile,
        history: tuple[GeneratedScenarioSpec, ...],
        attempt: int,
        nonce: str,
    ) -> str:
        return json.dumps(
            {
                "learner_profile": profile.model_dump(mode="json"),
                "generation_nonce": nonce,
                "attempt": attempt + 1,
                "validation_contract": {
                    "initially_enabled_actions_minimum": 2,
                    "success_path": (
                        "Every requirement must be reachable through valid action prerequisites "
                        "and effects before the first terminal escalation."
                    ),
                    "terminal_timing": (
                        "Allow enough accumulated action minutes to complete the recovery path."
                    ),
                    "references": "Every ID and reference must resolve exactly.",
                    "metric_sets": "Every set effect must remain inside the metric bounds.",
                },
                "prior_situations_to_avoid": [
                    {
                        "fingerprint": item.fingerprint,
                        "core_challenge": item.core_challenge,
                        "failure_mechanism": item.failure_mechanism,
                        "decision_tradeoff": item.decision_tradeoff,
                    }
                    for item in history
                ],
            },
            sort_keys=True,
        )

    @staticmethod
    def _blueprint_from_spec(spec: GeneratedScenarioSpec) -> ScenarioBlueprint:
        known_facts = tuple(fact.statement for fact in spec.facts if fact.known_at_start)
        return ScenarioBlueprint(
            scenario_id=spec.scenario_id,
            title=spec.title,
            learner_role=spec.learner_role,
            briefing=spec.briefing,
            how_it_developed=tuple(
                f"T-{moment.minutes_before_start}m · {moment.title}: {moment.detail}"
                for moment in spec.timeline[:5]
            ),
            immediate_constraints=(known_facts or (spec.decision_tradeoff,))[:5],
            first_decision=spec.first_decision,
        )

    @staticmethod
    def _generated_world(snapshot: dict[str, object]) -> dict[str, Any]:
        world = cast(dict[str, Any], snapshot)
        world["completed"] = world.get("outcome") == "recovered"
        world["terminal"] = world.get("outcome") == "terminal_escalation"
        world["available_actions"] = [
            {**action, "kind": action["id"]}
            for action in cast(list[dict[str, Any]], world.get("available_actions", []))
        ]
        return world

    @staticmethod
    def _facilitator_prompt(session: PracticeSession, response: str) -> str:
        return json.dumps(
            {
                "learner_profile": session.profile.model_dump(mode="json"),
                "scenario_framing": session.scenario.model_dump(mode="json"),
                "canonical_world": session.world,
                "allowed_evidence": PracticeService._allowed_evidence(session.world),
                "prior_turns": [turn.model_dump(mode="json") for turn in session.turns],
                "learner_response": response,
                "response_excerpt_contract": (
                    "Copy one non-empty contiguous substring from learner_response verbatim. "
                    "Preserve its exact case, spacing, and punctuation."
                ),
            },
            sort_keys=True,
        )

    @staticmethod
    def _debrief_prompt(session: PracticeSession) -> str:
        return json.dumps(
            {
                "learner_profile": session.profile.model_dump(mode="json"),
                "scenario_framing": session.scenario.model_dump(mode="json"),
                "canonical_world": session.world,
                "allowed_evidence": PracticeService._allowed_evidence(session.world),
                "learner_turns": [turn.model_dump(mode="json") for turn in session.turns],
            },
            sort_keys=True,
        )

    @staticmethod
    def _validate_evidence(
        references: tuple[EvidenceReference, ...], world: dict[str, Any]
    ) -> None:
        allowed = PracticeService._allowed_evidence(world)
        for reference in references:
            if reference.ref not in allowed[reference.source]:
                raise InvalidPracticeResponseError(
                    f"Facilitator cited unknown {reference.source}: {reference.ref}"
                )

    @staticmethod
    def _allowed_evidence(world: dict[str, Any]) -> dict[str, list[str]]:
        actions = {str(item["kind"]) for item in world.get("available_actions", [])}
        metrics = {str(key) for key in world.get("metrics", {})}
        artifacts = {str(item["id"]) for item in world.get("artifacts", [])}
        events = {
            str(value)
            for event in world.get("events", [])
            for value in (
                event.get("event_index"),
                event.get("index"),
                event.get("rule"),
                event.get("ref"),
            )
            if value is not None
        }
        for criterion in world.get("debrief", []):
            for link in criterion.get("evidence", []):
                if "artifact_id" in link:
                    artifacts.add(str(link["artifact_id"]))
                if "event_index" in link:
                    events.add(str(link["event_index"]))
        return {
            "action": sorted(actions),
            "metric": sorted(metrics),
            "artifact": sorted(artifacts),
            "event": sorted(events),
        }


def _profile_scope(profile: LearnerProfile) -> str:
    def normalize(value: str) -> str:
        return " ".join(value.casefold().split())

    # Novelty follows the professional field even when the learner refines the
    # optional work description between sessions.
    return normalize(profile.field)
