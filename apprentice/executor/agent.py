"""The GPT-5.6 twin agent: runs the playbook against ``TwinEnvironment``'s tools.

Mirrors ``apprentice.induction.induce``'s injectable-runner pattern: the same
``Callable[[Agent, str], X]`` shape, with a ``_default_runner`` that calls
``Runner.run_sync`` against the real Agents SDK (requiring a real model and
network access) and an injectable fake used by every non-``live`` test. The
fake runner drives ``TwinEnvironment``'s methods directly --- exactly what a
model calling function tools would cause to happen --- so no test ever needs
an API key.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from agents import Agent, Runner, function_tool
from agents.exceptions import MaxTurnsExceeded, UserError
from playwright.sync_api import Page
from pydantic import BaseModel

from apprentice.canonical import digest
from apprentice.executor.replay import FileResolver, execute_authorized_run
from apprentice.executor.twin_tools import FileValue, TwinEnvironment
from apprentice.induction.induce import Anchor, Playbook
from apprentice.models import RecordedAction
from apprentice.providers import provider_model, provider_model_settings
from apprentice.sidecar.run_service import RunService
from apprentice.twin.responses import TwinAbortError

__all__ = [
    "ActivationCallback",
    "TwinRunOutcome",
    "TwinRunner",
    "build_production_agent",
    "build_twin_agent",
    "default_activation",
    "run_twin_agent",
]

TwinRunner = Callable[[Agent[Any], str], str]


class TwinPlanError(RuntimeError):
    """The model proposed a malformed batched rehearsal plan."""


class TwinToolCall(BaseModel):
    """One model-proposed action in the batched rehearsal tool call."""

    action: Literal["navigate", "fill", "upload", "click", "commit"]
    path: str | None = None
    anchor: Anchor | None = None
    value: str | None = None
    filename: str | None = None
    sha256: str | None = None


@dataclass(frozen=True)
class TwinRunOutcome:
    """What one twin-agent run produced: the exact recorded plan, and how it ended."""

    actions: tuple[RecordedAction, ...]
    action_plan_hash: str
    aborted_reason: str | None


def build_twin_agent(env: TwinEnvironment) -> Agent[Any]:
    """Construct the single GPT-5.6 twin agent bound to one environment's tools."""

    @function_tool(failure_error_function=None)
    def rehearse_action_plan(actions: list[TwinToolCall]) -> str:
        """Execute the complete proposed plan, in order, inside the offline twin."""

        for proposed in actions:
            if proposed.action == "navigate":
                # The Build Week playbook records the page's semantic anchor,
                # while this single-domain demo has one known entry route.
                env.navigate(proposed.path or "/expense")
                env.read_page()
            elif proposed.action == "fill" and proposed.anchor is not None:
                if proposed.value is None:
                    raise TwinPlanError("fill action omitted value")
                env.fill(proposed.anchor, proposed.value)
            elif proposed.action == "upload" and proposed.anchor is not None:
                if proposed.filename is None or proposed.sha256 is None:
                    raise TwinPlanError("upload action omitted file identity")
                env.upload(
                    proposed.anchor,
                    FileValue(filename=proposed.filename, sha256=proposed.sha256),
                )
            elif proposed.action == "click" and proposed.anchor is not None:
                env.click(proposed.anchor)
            elif proposed.action == "commit" and proposed.anchor is not None:
                env.commit(proposed.anchor)
            else:
                raise TwinPlanError(f"{proposed.action} action omitted required arguments")
        return "rehearsal plan executed"

    return Agent(
        name="apprentice-twin-executor",
        instructions=(
            "Convert the reviewed playbook and supplied inputs into one complete ordered "
            "action list, then call rehearse_action_plan exactly once. Include every "
            "playbook step, including fills whose rendered value is an empty string. "
            "For upload, copy filename and sha256 from the matching input. Copy every "
            "semantic anchor exactly from the playbook. Do not omit, add, reorder, or "
            "repeat steps. This tool runs only inside an offline rehearsal twin."
        ),
        model=provider_model(),
        model_settings=provider_model_settings(),
        tools=[rehearse_action_plan],
        tool_use_behavior="stop_on_first_tool",
    )


def _render_twin_prompt(playbook: Playbook, inputs: Mapping[str, Any]) -> str:
    return json.dumps(
        {"playbook": playbook.model_dump(mode="json"), "inputs": inputs}, sort_keys=True
    )


def _default_runner(agent: Agent[Any], prompt: str) -> str:
    result = Runner.run_sync(agent, prompt, max_turns=2)
    return str(result.final_output)


def run_twin_agent(
    env: TwinEnvironment,
    playbook: Playbook,
    inputs: Mapping[str, Any],
    *,
    runner: TwinRunner | None = None,
) -> TwinRunOutcome:
    """Run the twin agent once against ``env``; fail closed rather than raise on any abort.

    The caller owns ``env`` construction (bound to one ``Twin`` and target
    base URL), so tests can inspect --- or a fake runner can directly drive
    --- the exact environment instance the agent's tools act on. ``runner``
    is injected in tests: the fake runner drives ``TwinEnvironment`` directly
    instead of going through the real Agents SDK loop, so no test ever
    requires an OpenAI API key.
    """

    agent = build_twin_agent(env)
    prompt = _render_twin_prompt(playbook, inputs)
    run = runner or _default_runner
    aborted_reason: str | None = None
    try:
        run(agent, prompt)
    except TwinAbortError as error:
        aborted_reason = error.reason
    except MaxTurnsExceeded:
        aborted_reason = "max_turns_exceeded"
    except TwinPlanError:
        aborted_reason = "invalid_model_plan"
    except UserError:
        aborted_reason = "invalid_model_plan"
    actions = tuple(env.actions)
    action_plan_hash = digest([action.model_dump(mode="json") for action in actions])
    return TwinRunOutcome(
        actions=actions, action_plan_hash=action_plan_hash, aborted_reason=aborted_reason
    )


# --- Production executor: one needs_approval tool, no Playwright page in sight. ---

ActivationCallback = Callable[[str], str]


def default_activation(
    run_service: RunService,
    *,
    file_resolver: FileResolver,
    headless: bool = True,
    authenticate: Callable[[Page], None] | None = None,
) -> ActivationCallback:
    """Build the tool's real body: replay exactly the stored plan, once authorized.

    Only ever invoked by the SDK after ``ApprovalService`` has already
    resolved the run's approval boundary to ``authorized`` --- L1 denials
    and pending L2/L3 approvals never reach here, since ``needs_approval``
    keeps the SDK from calling this until the interruption resolves.
    """

    def activation(run_id: str) -> str:
        _updated_run, outcome = execute_authorized_run(
            run_service,
            run_id,
            file_resolver=file_resolver,
            headless=headless,
            authenticate=authenticate,
        )
        if outcome.succeeded:
            return f"run {run_id} succeeded"
        return f"run {run_id} failed: {outcome.abort_reason or 'execution_error'}"

    return activation


def build_production_agent(activation: ActivationCallback) -> Agent[Any]:
    """Construct the single production agent: one ``needs_approval`` activation tool.

    The model's only capability is calling ``activate_rehearsed_plan`` with a
    ``run_id`` it was given; it never receives a Playwright page and can
    never supply or alter which actions get replayed --- the sidecar
    (``ApprovalService`` and ``ProductionReplayer``) re-reads everything by
    ``run_id`` alone once the SDK's approval interruption is resolved.
    """

    @function_tool(needs_approval=True)
    async def activate_rehearsed_plan(run_id: str) -> str:
        return activation(run_id)

    return Agent(
        name="apprentice-production-executor",
        instructions=(
            "You have exactly one job: call activate_rehearsed_plan with the "
            "given run_id, exactly once. You never see or choose any "
            "production action --- the sidecar re-reads the rehearsed plan "
            "by run_id alone and always requires approval before it runs."
        ),
        model=provider_model(),
        model_settings=provider_model_settings(),
        tools=[activate_rehearsed_plan],
    )
