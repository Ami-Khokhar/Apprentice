"""Codex-backed facilitation over generated deterministic practice worlds."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any, Literal, Protocol, TypeVar, cast
from uuid import uuid4

from pydantic import BaseModel

from apprentice.database import SQLiteDatabase
from apprentice.incident.generated import (
    GeneratedScenarioError,
    GeneratedScenarioRuntime,
    GeneratedScenarioSpec,
    is_near_duplicate,
)
from apprentice.observability import Tracer, build_tracer

from .codex_runner import AgentSpec, CodexOutputError, CodexStructuredRunner
from .contracts import (
    ClarificationExchange,
    ClarificationResponse,
    EvidenceReference,
    FacilitatorDecision,
    FinalDebrief,
    JudgmentProfile,
    JudgmentProfileCounts,
    JudgmentProfileIdentity,
    LearnerProfile,
    PortfolioCase,
    PortfolioTurn,
    PracticeSession,
    PracticeTurn,
    ScenarioBlueprint,
)

TOutput = TypeVar("TOutput", bound=BaseModel)
StructuredRunner = Callable[[AgentSpec, str], BaseModel]
DEFAULT_PRACTICE_MODEL = "gpt-5.6-terra"
MAX_GENERATION_ATTEMPTS = 3
DIFFICULTY_DESCRIPTIONS = (
    "Topic knowledge but no practical field experience; use one clear problem, direct evidence, "
    "low ambiguity, and forgiving consequences.",
    "A novice with limited guided exposure; use a contained problem, visible signals, and light "
    "time pressure.",
    "An early practitioner who can handle routine work with support; introduce one meaningful "
    "tradeoff and a small amount of ambiguity.",
    "A developing independent practitioner; require prioritization across a few signals and "
    "moderate operational pressure.",
    "A competent mid-level practitioner; use interacting concerns, incomplete evidence, and "
    "realistic stakeholder pressure.",
    "A strong senior practitioner; require independent judgment across multiple systems or teams "
    "with consequential tradeoffs.",
    "A seasoned lead; use substantial ambiguity, competing stakeholders, and second-order effects.",
    "A staff or principal-level leader; require cross-team systems thinking, strategic tradeoffs, "
    "and incident-tested judgment.",
    "An executive technical leader; use organization-wide consequences, sparse signals, and "
    "high-stakes decisions under pressure.",
    "A CTO-level engineer with roughly 15 years of experience and extensive real-incident "
    "leadership; demand expert judgment across technical, organizational, business, and long-term "
    "risk dimensions.",
)


class PracticeSessionNotFoundError(KeyError):
    pass


class InvalidPracticeResponseError(ValueError):
    pass


class PracticeNotReadyForDebriefError(ValueError):
    pass


class SessionStore(Protocol):
    def get(self, session_id: str) -> PracticeSession | None: ...

    def save(self, session: PracticeSession) -> None: ...

    def list_sessions(self) -> tuple[PracticeSession, ...]: ...

    def get_profile_identity(self) -> JudgmentProfileIdentity: ...

    def save_profile_identity(self, identity: JudgmentProfileIdentity) -> None: ...

    def generated_history(self, profile: LearnerProfile) -> tuple[GeneratedScenarioSpec, ...]: ...


class MemorySessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, PracticeSession] = {}
        self._profile_identity = JudgmentProfileIdentity()

    def get(self, session_id: str) -> PracticeSession | None:
        return self._sessions.get(session_id)

    def save(self, session: PracticeSession) -> None:
        self._sessions[session.id] = session

    def list_sessions(self) -> tuple[PracticeSession, ...]:
        return tuple(
            sorted(self._sessions.values(), key=lambda item: item.created_at, reverse=True)
        )

    def get_profile_identity(self) -> JudgmentProfileIdentity:
        return self._profile_identity

    def save_profile_identity(self, identity: JudgmentProfileIdentity) -> None:
        self._profile_identity = identity

    def generated_history(self, profile: LearnerProfile) -> tuple[GeneratedScenarioSpec, ...]:
        scope = _profile_scope(profile)
        return tuple(
            session.generated_spec
            for session in self.list_sessions()
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

    def list_sessions(self) -> tuple[PracticeSession, ...]:
        with self._database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT session_json
                FROM practice_sessions
                WHERE json_type(session_json, '$.generated_spec') = 'object'
                ORDER BY created_at DESC
                """
            ).fetchall()
        return tuple(PracticeSession.model_validate_json(row["session_json"]) for row in rows)

    def get_profile_identity(self) -> JudgmentProfileIdentity:
        with self._database.transaction() as connection:
            row = connection.execute(
                """
                SELECT display_name, headline, bio, updated_at
                FROM judgment_profile
                WHERE singleton_id = 1
                """
            ).fetchone()
        if row is None:
            return JudgmentProfileIdentity()
        return JudgmentProfileIdentity(
            display_name=row["display_name"],
            headline=row["headline"],
            bio=row["bio"],
            updated_at=row["updated_at"],
        )

    def save_profile_identity(self, identity: JudgmentProfileIdentity) -> None:
        with self._database.transaction(write=True) as connection:
            connection.execute(
                """
                INSERT INTO judgment_profile(
                  singleton_id, display_name, headline, bio, updated_at
                ) VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(singleton_id) DO UPDATE SET
                  display_name = excluded.display_name,
                  headline = excluded.headline,
                  bio = excluded.bio,
                  updated_at = excluded.updated_at
                """,
                (
                    identity.display_name,
                    identity.headline,
                    identity.bio,
                    identity.updated_at,
                ),
            )

    def generated_history(self, profile: LearnerProfile) -> tuple[GeneratedScenarioSpec, ...]:
        scope = _profile_scope(profile)
        return tuple(
            session.generated_spec
            for session in self.list_sessions()
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
        tracer: Tracer | None = None,
    ) -> None:
        self._runner = runner if runner is not None else CodexStructuredRunner(timeout_seconds=120)
        self._model = model
        self._store = store or (SQLiteSessionStore(database) if database else MemorySessionStore())
        self._generated_runtime = generated_runtime or GeneratedScenarioRuntime()
        self._tracer = tracer if tracer is not None else build_tracer()

    def start(
        self, field: str, work_context: str | None = None, difficulty_level: int = 1
    ) -> PracticeSession:
        profile = LearnerProfile(
            field=field.strip(),
            work_context=work_context.strip() if work_context and work_context.strip() else None,
            difficulty_level=difficulty_level,
        )
        session_id = str(uuid4())
        with self._tracer.operation(
            "practice.start",
            session_id=session_id,
            input={"learner_profile": profile},
            metadata={"difficulty_level": difficulty_level},
        ) as trace:
            history = self._store.generated_history(profile)
            spec: GeneratedScenarioSpec | None = None
            last_output_error: CodexOutputError | None = None
            retry_reason: str | None = None
            for attempt in range(MAX_GENERATION_ATTEMPTS):
                nonce = str(uuid4())
                try:
                    candidate = self._run(
                        self._generator_agent(),
                        self._generation_prompt(profile, history, attempt, nonce),
                        GeneratedScenarioSpec,
                        metadata={
                            "attempt": attempt + 1,
                            "max_attempts": MAX_GENERATION_ATTEMPTS,
                            "is_retry": attempt > 0,
                            "retry_reason": retry_reason or "initial_attempt",
                        },
                        result_metadata=lambda output, expected_nonce=nonce: (
                            self._generation_result_metadata(
                                output, expected_nonce, history
                            )
                        ),
                    )
                except CodexOutputError as error:
                    last_output_error = error
                    retry_reason = "invalid_structured_output"
                    continue
                if candidate.generation_nonce != nonce:
                    retry_reason = "generation_nonce_mismatch"
                    continue
                if any(is_near_duplicate(candidate, prior) for prior in history):
                    retry_reason = "near_duplicate"
                    continue
                spec = candidate
                break
            if spec is None:
                if last_output_error is not None:
                    raise last_output_error
                raise InvalidPracticeResponseError(
                    "The guide could not create a sufficiently different situation. "
                    "Please try again."
                )
            world = self._generated_world(self._generated_runtime.create(spec))
            blueprint = self._blueprint_from_spec(spec)
            now = time.time()
            session = PracticeSession(
                id=session_id,
                profile=profile,
                scenario=blueprint,
                incident_id=cast(str, world["id"]),
                world=world,
                generated_spec=spec,
                created_at=now,
                updated_at=now,
            )
            self._store.save(session)
            trace.update(
                output={"scenario": blueprint, "world_outcome": world["outcome"]},
                metadata={"generation_attempts": attempt + 1},
            )
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

    def get_judgment_profile(self) -> JudgmentProfile:
        identity = self._store.get_profile_identity()
        cases = self.list_portfolio_cases()
        counts = JudgmentProfileCounts(
            encountered=len(cases),
            resolved=sum(item.status == "resolved" for item in cases),
            reviewed=sum(item.status == "reviewed" for item in cases),
            active=sum(item.status == "active" for item in cases),
        )
        return JudgmentProfile(**identity.model_dump(), counts=counts)

    def update_judgment_profile(
        self, display_name: str, headline: str, bio: str
    ) -> JudgmentProfile:
        identity = JudgmentProfileIdentity(
            display_name=display_name.strip(),
            headline=headline.strip(),
            bio=bio.strip(),
            updated_at=time.time(),
        )
        self._store.save_profile_identity(identity)
        return self.get_judgment_profile()

    def list_portfolio_cases(self) -> tuple[PortfolioCase, ...]:
        return tuple(self._portfolio_case(session) for session in self._store.list_sessions())

    def get_portfolio_case(self, session_id: str) -> PortfolioCase:
        session = self._store.get(session_id)
        if session is None:
            raise PracticeSessionNotFoundError(f"Unknown practice session: {session_id}")
        return self._portfolio_case(session)

    def respond(self, session_id: str, response: str) -> PracticeSession:
        session = self.get_session(session_id)
        response = response.strip()
        if not response:
            raise InvalidPracticeResponseError("A response is required")
        if len(response) > 8_000:
            raise InvalidPracticeResponseError("A response cannot exceed 8000 characters")
        if session.debrief is not None:
            raise InvalidPracticeResponseError("This practice session has already been debriefed")
        if session.manually_stopped:
            raise InvalidPracticeResponseError("This practice session has already been ended")
        if session.world.get("completed") or session.world.get("terminal"):
            raise InvalidPracticeResponseError("This practice scenario has already concluded")

        with self._tracer.operation(
            "practice.respond",
            session_id=session.id,
            input={"learner_response": response, "turn_number": len(session.turns) + 1},
            metadata={"turn_number": len(session.turns) + 1},
        ) as trace:
            decision = self._run(
                self._facilitator_agent(),
                self._facilitator_prompt(session, response),
                FacilitatorDecision,
            )
            incomplete_actions = {
                str(action["kind"])
                for action in cast(list[dict[str, object]], session.world["available_actions"])
                if str(action["kind"])
                not in cast(list[str], session.world.get("completed_actions", []))
            }
            if decision.action_kind is not None and decision.action_kind not in incomplete_actions:
                raise InvalidPracticeResponseError(
                    f"Facilitator selected unknown or completed action: {decision.action_kind}"
                )
            enabled_actions = {
                str(action["kind"])
                for action in cast(list[dict[str, object]], session.world["available_actions"])
                if action.get("enabled")
            }
            if (
                decision.action_kind is not None
                and decision.action_kind not in enabled_actions
                and not decision.prerequisite_override
            ):
                raise InvalidPracticeResponseError(
                    "Facilitator selected an action with unmet dependencies without justification"
                )
            if decision.assessment.response_excerpt not in response:
                raise InvalidPracticeResponseError(
                    "Facilitator response_excerpt was not present in the learner response"
                )
            if (
                decision.assessment.disposition in {"accepted", "partially_effective"}
                and not decision.assessment.evidence
            ):
                raise InvalidPracticeResponseError(
                    "An accepted assessment must cite observable evidence"
                )
            # Validate every claimed source before mutating the deterministic
            # world. A malformed model response must fail closed with no partial
            # incident transition.
            self._validate_evidence(decision.assessment.evidence, session.world)

            previous_metrics = deepcopy(session.world.get("metrics", {}))
            world = session.world
            if decision.action_kind is not None:
                world = self._generated_world(
                    self._generated_runtime.apply(
                        session.incident_id,
                        decision.action_kind,
                        allow_out_of_order=decision.prerequisite_override,
                    )
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
            trace.update(
                output={
                    "facilitator_decision": decision,
                    "selected_action": decision.action_kind,
                    "disposition": decision.assessment.disposition,
                    "prerequisite_override": decision.prerequisite_override,
                    "world_outcome": world["outcome"],
                    "metric_delta": self._metric_delta(
                        cast(dict[str, Any], previous_metrics),
                        cast(dict[str, Any], world.get("metrics", {})),
                    ),
                },
                metadata={
                    "selected_action": decision.action_kind or "none",
                    "disposition": decision.assessment.disposition,
                    "world_outcome": world["outcome"],
                },
            )
            return updated

    def clarify(self, session_id: str, question: str) -> PracticeSession:
        session = self.get_session(session_id)
        question = question.strip()
        if not question:
            raise InvalidPracticeResponseError("A clarification question is required")
        if len(question) > 2_000:
            raise InvalidPracticeResponseError(
                "A clarification question cannot exceed 2000 characters"
            )
        if session.debrief is not None:
            raise InvalidPracticeResponseError("This practice session has already been debriefed")
        if session.manually_stopped:
            raise InvalidPracticeResponseError("This practice session has already been ended")
        if session.world.get("completed") or session.world.get("terminal"):
            raise InvalidPracticeResponseError("This practice scenario has already concluded")

        with self._tracer.operation(
            "practice.clarify",
            session_id=session.id,
            input={"learner_question": question},
            metadata={"clarification_number": len(session.clarifications) + 1},
        ) as trace:
            response = self._run(
                self._clarification_agent(),
                self._clarification_prompt(session, question),
                ClarificationResponse,
            )
            if response.status == "refused":
                response = response.model_copy(
                    update={
                        "answer": (
                            "I can clarify facts in the situation, but I cannot suggest what "
                            "you should do or evaluate a possible decision."
                        )
                    }
                )
            elif response.status == "unavailable":
                response = response.model_copy(
                    update={
                        "answer": (
                            "That information is not established in the situation as currently "
                            "presented."
                        )
                    }
                )
            allowed = self._allowed_clarification_evidence(session)
            for reference in response.evidence:
                if reference.ref not in allowed[reference.source]:
                    raise InvalidPracticeResponseError(
                        f"Clarification cited unknown {reference.source}: {reference.ref}"
                    )
            exchange = ClarificationExchange(question=question, response=response)
            updated = session.model_copy(
                update={
                    "clarifications": (*session.clarifications, exchange),
                    "updated_at": time.time(),
                }
            )
            self._store.save(updated)
            trace.update(
                output=response,
                metadata={"status": response.status},
            )
            return updated

    def stop(self, session_id: str) -> PracticeSession:
        session = self.get_session(session_id)
        if session.manually_stopped:
            return session
        if session.debrief is not None:
            raise InvalidPracticeResponseError("This practice session has already been debriefed")
        if session.world.get("completed") or session.world.get("terminal"):
            return session
        if not session.turns:
            raise InvalidPracticeResponseError(
                "Make at least one decision before ending the simulation"
            )
        with self._tracer.operation(
            "practice.stop",
            session_id=session.id,
            input={"turn_count": len(session.turns), "world_outcome": session.world["outcome"]},
            metadata={"turn_count": len(session.turns)},
        ) as trace:
            updated = session.model_copy(
                update={"manually_stopped": True, "updated_at": time.time()}
            )
            self._store.save(updated)
            trace.update(
                output={"manually_stopped": True, "world_outcome": session.world["outcome"]}
            )
            return updated

    def get_debrief(self, session_id: str) -> FinalDebrief:
        session = self.get_session(session_id)
        if session.debrief is not None:
            return session.debrief
        if (
            not session.world.get("completed")
            and not session.world.get("terminal")
            and not session.manually_stopped
        ):
            raise PracticeNotReadyForDebriefError(
                "The scenario must reach recovery or terminal escalation before debrief"
            )
        with self._tracer.operation(
            "practice.debrief",
            session_id=session.id,
            input={
                "turns": session.turns,
                "world_outcome": session.world["outcome"],
                "manually_stopped": session.manually_stopped,
            },
            metadata={
                "turn_count": len(session.turns),
                "ending_reason": self._ending_reason(session),
            },
        ) as trace:
            debrief = self._run(
                self._debrief_agent(), self._debrief_prompt(session), FinalDebrief
            )
            if debrief.score is None or debrief.score_rationale is None:
                raise InvalidPracticeResponseError("The new debrief did not include a score")
            self._validate_evidence(
                debrief.evidence, session.world, allowed=self._debrief_evidence(session.world)
            )
            updated = session.model_copy(update={"debrief": debrief, "updated_at": time.time()})
            self._store.save(updated)
            trace.update(
                output=debrief,
                metadata={"score": debrief.score, "ending_reason": self._ending_reason(session)},
            )
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
                "Include multiple defensible response paths where the real situation permits it. "
                "Use prerequisites only for genuine causal or safety dependencies, never to force "
                "one preferred teaching sequence. Include common containment, rollback, "
                "mitigation, investigation, and validation actions when realistic. "
                "Define success primarily by observable outcomes such as controlled metrics or "
                "revealed verification evidence, not by completing one prescribed action list. "
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
                "Act as a fair real-world incident assessor, not an answer-key matcher. Interpret "
                "the learner's whole proposed plan against the facts, evidence, constraints, and "
                "likely consequences in the supplied world. Accept multiple defensible approaches "
                "and orderings. Do not penalize an action merely because it differs from a "
                "preferred sequence. Map the immediate executable step to at most one incomplete "
                "action by "
                "meaning and outcome, including a reasonable equivalent such as rolling back an "
                "implicated recent change. An action may be selected before its generated "
                "prerequisites when it is independently plausible from the currently known facts; "
                "treat prerequisites as causal guidance, not a hidden answer key. Use disposition "
                "accepted when the decision is sound, partially_effective when it helps but leaves "
                "a material gap, needs_clarification only when ambiguity prevents safe execution, "
                "and unsafe only for a materially harmful or unjustified action. If clarification "
                "is needed, ask one precise question and select no action. If unsafe, select no "
                "action and explain the concrete danger. Never change metrics, events, "
                "availability, or outcomes yourself. Assess reasoning briefly and cite only IDs "
                "present in the payload using event, artifact, metric, or action references. "
                "Set assessment.risk to a concrete remaining risk only when one actually remains; "
                "otherwise set it to null. Never invent criticism to fill the field. Acknowledge "
                "every valid step in a multi-step plan in recognized_intents while executing only "
                "its immediate step. If a defensible action has no semantic equivalent in the "
                "transition catalog, mark it accepted or partially_effective with action_kind "
                "null; its reasoning will be recorded without inventing a world transition. Set "
                "prerequisite_override true only when selecting a normally unavailable action that "
                "is independently feasible now, and give a concrete override_justification based "
                "on observable evidence. Otherwise keep it false and justification null. "
                "Copy assessment.response_excerpt verbatim from learner_response as one "
                "contiguous substring, preserving exact case and punctuation."
            ),
            model=self._model,
            output_type=FacilitatorDecision,
        )

    def _clarification_agent(self) -> AgentSpec:
        return AgentSpec(
            name="dojo-situation-clarifier",
            instructions=(
                "Answer only factual questions that help the learner understand the currently "
                "observable situation. Ground every answered claim only in the supplied scenario "
                "framing and observable world, and cite the exact supplied evidence IDs. Do not "
                "use outside knowledge or infer missing facts. Never recommend, suggest, rank, or "
                "reveal an action; never give a hint about what the learner should do next; never "
                "evaluate a proposed choice or predict its likely consequences. Do not reveal "
                "hidden actions, prerequisites, success conditions, scoring criteria, or future "
                "events. If the request asks for any of those, is not a clarification question, "
                "or evaluates a decision, set status to refused and cite no evidence. If it is a "
                "valid factual question whose answer is not established by the observable "
                "material, set status to unavailable and cite no evidence. Otherwise set status "
                "to answered and answer directly and neutrally without adding advice."
            ),
            model=self._model,
            output_type=ClarificationResponse,
        )

    def _debrief_agent(self) -> AgentSpec:
        return AgentSpec(
            name="dojo-practice-debrief",
            instructions=(
                "Produce a concise professional debrief grounded only in the canonical world, its "
                "deterministic rubric, and the learner transcript. Separate observed strengths "
                "from missed evidence and risky assumptions. Offer one stronger possible approach, "
                "without presenting it as the only correct sequence. "
                "Give a 0-100 score and concise rationale against the frozen weighted rubric. "
                "Score only decisions actually recorded, completed actions, revealed evidence, "
                "current metrics, and the achieved outcome. Never award credit for available or "
                "future actions. Future actions may appear only in the better path or expert "
                "recommendation. If the learner stopped early, do not invent unattempted "
                "decisions; incomplete rubric achievement may limit the score. Do not invent a "
                "miss or risky assumption when none is supported; those lists may be empty. Judge "
                "real-world effectiveness rather than conformity to one preferred ordering. Cite "
                "only IDs allowed by the scoring evidence payload; do not invent causes or "
                "outcomes."
            ),
            model=self._model,
            output_type=FinalDebrief,
        )

    def _run(
        self,
        agent: AgentSpec,
        prompt: str,
        output_type: type[TOutput],
        *,
        metadata: dict[str, object] | None = None,
        result_metadata: Callable[[TOutput], Mapping[str, object]] | None = None,
    ) -> TOutput:
        with self._tracer.generation(
            agent.name,
            model=agent.model,
            output_schema=output_type.__name__,
            input={"instructions": agent.instructions, "prompt": prompt},
            metadata={"agent_name": agent.name, **(metadata or {})},
        ) as trace:
            output = self._runner(agent, prompt)
            if not isinstance(output, output_type):
                raise TypeError(
                    f"Practice runner returned {type(output).__name__}; "
                    f"expected {output_type.__name__}"
                )
            trace.update(
                output=output,
                metadata=result_metadata(output) if result_metadata is not None else None,
            )
            return output

    @staticmethod
    def _generation_result_metadata(
        candidate: GeneratedScenarioSpec,
        nonce: str,
        history: tuple[GeneratedScenarioSpec, ...],
    ) -> dict[str, object]:
        if candidate.generation_nonce != nonce:
            return {"result": "rejected", "rejection_reason": "generation_nonce_mismatch"}
        if any(is_near_duplicate(candidate, prior) for prior in history):
            return {"result": "rejected", "rejection_reason": "near_duplicate"}
        return {"result": "accepted"}

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
                "difficulty_calibration": {
                    "selected_level": profile.difficulty_level,
                    "learner_experience": DIFFICULTY_DESCRIPTIONS[
                        profile.difficulty_level - 1
                    ],
                    "scale_anchors": {
                        "1": (
                            "Topic knowledge, no practical field experience; keep the situation "
                            "approachable and teachable."
                        ),
                        "10": (
                            "CTO-level, roughly 15 years of experience, with extensive real-life "
                            "incident leadership; make the situation appropriately demanding."
                        ),
                    },
                    "instruction": (
                        "Calibrate the scenario to this exact level. Increase interacting systems, "
                        "ambiguity, time pressure, stakeholder conflict, consequence severity, "
                        "and required autonomy gradually from level 1 to level 10. Do not test "
                        "expertise above the selected level. Keep the language clear at every "
                        "difficulty level; difficulty must come from the judgment required, not "
                        "from difficult wording."
                    ),
                },
                "readability_contract": {
                    "briefing": (
                        "State the immediate problem in the first sentence. Use plain, direct "
                        "English and short sentences. Put one fact in each sentence."
                    ),
                    "terminology": (
                        "Replace jargon and acronyms with common words. If a technical term is "
                        "necessary, define it briefly the first time it appears."
                    ),
                    "focus": "Remove scene-setting that does not help the learner decide.",
                    "first_decision": (
                        "Ask one direct question. Do not combine multiple questions and do not "
                        "suggest or reveal the correct action."
                    ),
                    "difficulty_independence": (
                        "Apply these readability rules at every difficulty level. More advanced "
                        "scenarios may require harder judgment, but must not use harder English."
                    ),
                },
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
                "observable_world": PracticeService._facilitator_world(session),
                "transition_catalog": PracticeService._transition_catalog(session),
                "allowed_evidence": PracticeService._allowed_evidence(session.world),
                "prior_turns": [turn.model_dump(mode="json") for turn in session.turns],
                "learner_response": response,
                "response_excerpt_contract": (
                    "Copy one non-empty contiguous substring from learner_response verbatim. "
                    "Preserve its exact case, spacing, and punctuation."
                ),
                "adjudication_contract": (
                    "Judge real-world plausibility and consequences, not conformity to a hidden "
                    "solution order. Multiple defensible paths are valid. Generated prerequisites "
                    "describe the usual causal path but may be bypassed when the proposed action "
                    "is independently safe and plausible from known evidence. Execute one "
                    "immediate mapped action. A defensible unmodelled action may be accepted with "
                    "action_kind null and no deterministic world mutation. Never manufacture a "
                    "remaining risk after it has been addressed."
                ),
            },
            sort_keys=True,
        )

    @staticmethod
    def _clarification_prompt(session: PracticeSession, question: str) -> str:
        return json.dumps(
            {
                "scenario_framing": session.scenario.model_dump(mode="json"),
                "observable_world": PracticeService._facilitator_world(session),
                "allowed_evidence": PracticeService._allowed_clarification_evidence(session),
                "learner_question": question,
                "clarification_contract": (
                    "Answer only what the supplied observable facts explicitly establish. Do not "
                    "offer actions, options, priorities, decision criteria, evaluation, likely "
                    "outcomes, or clues to the scenario solution. Decline instead when answering "
                    "would require inference, invention, outside knowledge, or any such hint."
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
                "ending_reason": PracticeService._ending_reason(session),
                "weighted_rubric": [
                    criterion.model_dump(mode="json")
                    for criterion in session.generated_spec.rubric
                ],
                "scoring_evidence": PracticeService._debrief_evidence(session.world),
                "learner_turns": [turn.model_dump(mode="json") for turn in session.turns],
                "scoring_contract": (
                    "Return a 0-100 score and concise score_rationale. Use only the frozen "
                    "weighted rubric, learner transcript, completed actions, revealed artifacts, "
                    "recorded events, current metrics, and outcome. Available or future actions "
                    "earn no credit and may be mentioned only in better_path or expert_approach. "
                    "Credit defensible decisions by their real-world effect; do not require one "
                    "preferred order. A recorded accepted unmodelled decision may earn reasoning "
                    "credit when its cited evidence supports it, but do not claim an unobserved "
                    "world outcome. Leave missed or risky_assumptions empty when unsupported."
                ),
            },
            sort_keys=True,
        )

    @staticmethod
    def _ending_reason(session: PracticeSession) -> str:
        if session.manually_stopped:
            return "manual_stop"
        if session.world.get("completed"):
            return "recovered"
        return "terminal_escalation"

    @classmethod
    def _portfolio_case(cls, session: PracticeSession) -> PortfolioCase:
        turns = tuple(
            PortfolioTurn(
                response=turn.response,
                action_kind=turn.action_kind,
                recognized_intents=turn.assessment.recognized_intents,
                disposition=turn.assessment.disposition,
                interpretation=turn.assessment.interpretation,
                strength=turn.assessment.strength,
                risk=turn.assessment.risk,
                evidence=turn.assessment.evidence,
                coaching_question=turn.coaching_question,
                event_count=turn.event_count,
                outcome=turn.outcome,
            )
            for turn in session.turns
        )
        evidence: list[EvidenceReference] = []
        seen_evidence: set[tuple[str, str]] = set()
        evidence_groups = [turn.evidence for turn in turns]
        if session.debrief is not None:
            evidence_groups.append(session.debrief.evidence)
        for group in evidence_groups:
            for reference in group:
                key = (reference.source, reference.ref)
                if key not in seen_evidence:
                    evidence.append(reference)
                    seen_evidence.add(key)
        outcome = cls._portfolio_outcome(session)
        return PortfolioCase(
            session_id=session.id,
            title=session.scenario.title,
            field=session.profile.field,
            work_context=session.profile.work_context,
            difficulty_level=session.profile.difficulty_level,
            learner_role=session.scenario.learner_role,
            briefing=session.scenario.briefing,
            immediate_constraints=session.scenario.immediate_constraints,
            first_decision=session.scenario.first_decision,
            status=cls._portfolio_status(session),
            outcome=outcome,
            turns=turns,
            clarifications=session.clarifications,
            evidence=tuple(evidence),
            score=session.debrief.score if session.debrief else None,
            debrief=session.debrief,
            created_at=session.created_at,
            updated_at=session.updated_at,
        )

    @staticmethod
    def _portfolio_status(
        session: PracticeSession,
    ) -> Literal["active", "resolved", "reviewed"]:
        if session.world.get("completed") and session.world.get("outcome") == "recovered":
            return "resolved"
        if (
            session.world.get("terminal")
            or session.world.get("outcome") == "terminal_escalation"
            or session.manually_stopped
            or session.debrief is not None
        ):
            return "reviewed"
        return "active"

    @staticmethod
    def _portfolio_outcome(
        session: PracticeSession,
    ) -> Literal["active", "recovered", "terminal_escalation"]:
        if session.world.get("completed") and session.world.get("outcome") == "recovered":
            return "recovered"
        if (
            session.world.get("terminal")
            or session.world.get("outcome") == "terminal_escalation"
        ):
            return "terminal_escalation"
        return "active"

    @staticmethod
    def _metric_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, object]:
        return {
            key: {"before": before.get(key), "after": after.get(key)}
            for key in sorted(before.keys() | after.keys())
            if before.get(key) != after.get(key)
        }

    @staticmethod
    def _validate_evidence(
        references: tuple[EvidenceReference, ...],
        world: dict[str, Any],
        *,
        allowed: dict[str, list[str]] | None = None,
    ) -> None:
        allowed = allowed or PracticeService._allowed_evidence(world)
        for reference in references:
            if reference.ref not in allowed[reference.source]:
                raise InvalidPracticeResponseError(
                    f"Facilitator cited unknown {reference.source}: {reference.ref}"
                )

    @staticmethod
    def _debrief_evidence(world: dict[str, Any]) -> dict[str, list[str]]:
        allowed = PracticeService._allowed_evidence(world)
        allowed["action"] = sorted(str(item) for item in world.get("completed_actions", []))
        allowed["artifact"] = sorted(str(item) for item in world.get("revealed_artifacts", []))
        return allowed

    @staticmethod
    def _facilitator_world(session: PracticeSession) -> dict[str, Any]:
        """Return only state and evidence the learner could currently know."""
        world = session.world
        return {
            "known_facts": [
                fact.model_dump(mode="json")
                for fact in session.generated_spec.facts
                if fact.known_at_start
            ],
            "sim_time": world.get("sim_time"),
            "outcome": world.get("outcome"),
            "metrics": world.get("metrics", {}),
            "artifacts": world.get("artifacts", []),
            "events": world.get("events", []),
            "completed_actions": world.get("completed_actions", []),
        }

    @staticmethod
    def _transition_catalog(session: PracticeSession) -> list[dict[str, object]]:
        """Expose semantic transition names without the hidden solution graph or effects."""
        completed = set(cast(list[str], session.world.get("completed_actions", [])))
        return [
            {
                "id": action.id,
                "label": action.label,
                "description": action.description,
                "risk": action.risk,
                "completed": action.id in completed,
            }
            for action in session.generated_spec.actions
        ]

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

    @staticmethod
    def _allowed_clarification_evidence(
        session: PracticeSession,
    ) -> dict[str, list[str]]:
        world = session.world
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
        return {
            "scenario": [
                "briefing",
                "learner_role",
                *(
                    f"timeline:{index}"
                    for index, _item in enumerate(session.scenario.how_it_developed)
                ),
                *(
                    f"constraint:{index}"
                    for index, _item in enumerate(session.scenario.immediate_constraints)
                ),
            ],
            "fact": sorted(
                fact.id for fact in session.generated_spec.facts if fact.known_at_start
            ),
            "event": sorted(events),
            "artifact": sorted(str(item["id"]) for item in world.get("artifacts", [])),
            "metric": sorted(str(key) for key in world.get("metrics", {})),
        }


def _profile_scope(profile: LearnerProfile) -> str:
    def normalize(value: str) -> str:
        return " ".join(value.casefold().split())

    # Novelty follows the professional field even when the learner refines the
    # optional work description between sessions.
    return normalize(profile.field)
