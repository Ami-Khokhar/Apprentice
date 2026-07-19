"""A serialization round-trip test against the *installed* Agents SDK.

This is the spike the task brief requires before ever relying on delayed
(cross-request) resume: it exercises ``agents.RunState`` --- the real SDK's
own pause/resume primitive for ``needs_approval`` interruptions --- against
this project's ``SDKStateStore`` adapter, with no network access and no
OpenAI API key. A real ``ToolApprovalItem`` interruption is constructed
directly (the same shape ``Runner.run`` would produce when the model calls
``activate_rehearsed_plan`` before it is approved), stored through the
adapter, reloaded, and resolved.
"""

from __future__ import annotations

import json

from agents import Agent, function_tool
from agents.items import ToolApprovalItem
from agents.run_context import RunContextWrapper
from agents.run_internal.run_steps import NextStepInterruption
from agents.run_state import RunState
from openai.types.responses import ResponseFunctionToolCall

from apprentice.executor.sdk_state import SDKStateStore, build_pending_interruption_state


@function_tool(needs_approval=True)
async def activate_rehearsed_plan(run_id: str) -> str:
    return f"activated {run_id}"


def _agent() -> Agent:
    return Agent(
        name="apprentice-production-executor",
        instructions="Call activate_rehearsed_plan with the given run_id exactly once.",
        tools=[activate_rehearsed_plan],
    )


def _interrupted_state(agent: Agent, run_id: str, *, call_id: str = "call-1") -> RunState:
    raw_item = ResponseFunctionToolCall(
        type="function_call",
        call_id=call_id,
        name="activate_rehearsed_plan",
        arguments=json.dumps({"run_id": run_id}),
    )
    approval_item = ToolApprovalItem(
        agent=agent, raw_item=raw_item, tool_name="activate_rehearsed_plan"
    )
    context = RunContextWrapper(context={})
    state = RunState(context, original_input=f"activate {run_id}", starting_agent=agent)
    state._current_step = NextStepInterruption(interruptions=[approval_item])
    return state


def test_pending_interruption_state_round_trips_through_the_installed_sdk() -> None:
    agent = _agent()
    state = _interrupted_state(agent, "run-123")
    store = SDKStateStore(agent)

    text = store.serialize(state)
    restored = store.deserialize(text)

    interruptions = restored.get_interruptions()
    assert len(interruptions) == 1
    assert interruptions[0].tool_name == "activate_rehearsed_plan"
    raw_arguments = json.loads(interruptions[0].raw_item.arguments)
    assert raw_arguments == {"run_id": "run-123"}


def test_deserialized_state_can_be_approved() -> None:
    agent = _agent()
    state = _interrupted_state(agent, "run-456")
    store = SDKStateStore(agent)

    restored = store.deserialize(store.serialize(state))
    interruption = restored.get_interruptions()[0]
    restored.approve(interruption)

    approvals = restored._context._approvals["activate_rehearsed_plan"]
    assert approvals.approved == [interruption.raw_item.call_id]


def test_deserialized_state_can_be_rejected() -> None:
    agent = _agent()
    state = _interrupted_state(agent, "run-789")
    store = SDKStateStore(agent)

    restored = store.deserialize(store.serialize(state))
    interruption = restored.get_interruptions()[0]
    restored.reject(interruption)

    approvals = restored._context._approvals["activate_rehearsed_plan"]
    assert approvals.rejected == [interruption.raw_item.call_id]


def test_serialized_state_is_plain_json_text_suitable_for_sqlite_storage() -> None:
    agent = _agent()
    state = _interrupted_state(agent, "run-abc")
    store = SDKStateStore(agent)

    text = store.serialize(state)

    assert isinstance(text, str)
    parsed = json.loads(text)
    assert parsed["current_step"]["type"] == "next_step_interruption"


def test_build_pending_interruption_state_matches_a_manually_built_state() -> None:
    """The adapter's own construction helper produces an equivalent, resolvable state."""
    agent = _agent()
    built = build_pending_interruption_state(
        agent, run_id="run-xyz", tool_name="activate_rehearsed_plan", call_id="call-9"
    )
    store = SDKStateStore(agent)

    restored = store.deserialize(store.serialize(built))
    interruption = restored.get_interruptions()[0]

    assert interruption.tool_name == "activate_rehearsed_plan"
    assert json.loads(interruption.raw_item.arguments) == {"run_id": "run-xyz"}
