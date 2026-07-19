"""An isolated adapter over the installed Agents SDK's resumable run state.

``agents.RunState`` is the real SDK's own durable pause/resume boundary for
``needs_approval`` tool interruptions: ``to_json()``/``to_string()`` and the
async ``from_json()``/``from_string()`` (spiked and confirmed against the
project's installed ``openai-agents`` release; see
``tests/executor/test_sdk_state.py`` for the round-trip this module relies
on). Nothing outside this module ever touches ``agents.RunState`` directly,
so a future SDK upgrade that changes its serialization shape only has one
place to adapt.

``from_json``/``from_string`` are async (they may resolve nested agent-tool
state); the rest of this harness is synchronous, so ``SDKStateStore``
wraps that one call in ``asyncio.run`` to present a plain, sync API.
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

from agents import Agent
from agents.items import ToolApprovalItem
from agents.run_context import RunContextWrapper
from agents.run_internal.run_steps import NextStepInterruption
from agents.run_state import RunState
from openai.types.responses import ResponseFunctionToolCall

__all__ = ["SDKStateStore", "build_pending_interruption_state"]


class SDKStateStore:
    """Serializes and restores one ``RunState`` as plain JSON text.

    Callers persist the returned text verbatim (this harness stores it in
    ``runs.sdk_state_ref``) and hand it back unchanged to ``deserialize``;
    the adapter never inspects or mutates the text in between.
    """

    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    def serialize(self, state: RunState) -> str:
        return state.to_string()

    def deserialize(self, text: str) -> RunState:
        # ``asyncio.run`` raises if this thread already runs an event loop
        # (e.g. called from an async endpoint); fall back to a dedicated
        # worker thread with its own loop rather than blowing up.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(RunState.from_string(self._agent, text))
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, RunState.from_string(self._agent, text)).result()


def build_pending_interruption_state(
    agent: Agent,
    *,
    run_id: str,
    tool_name: str,
    call_id: str,
) -> RunState:
    """Build a ``RunState`` paused on exactly one pending tool-approval interruption.

    Mirrors the shape ``Runner.run`` produces the instant the model calls a
    ``needs_approval=True`` tool and the SDK pauses before invoking it: one
    ``ToolApprovalItem`` wrapping a ``function_call`` for ``tool_name`` whose
    single JSON argument is ``run_id``. Used by the production executor to
    construct the state it persists across an L2/L3 approval boundary
    without needing a live model call to produce that interruption.
    """
    raw_item = ResponseFunctionToolCall(
        type="function_call",
        call_id=call_id,
        name=tool_name,
        arguments=json.dumps({"run_id": run_id}),
    )
    approval_item = ToolApprovalItem(agent=agent, raw_item=raw_item, tool_name=tool_name)
    context = RunContextWrapper(context={})
    state = RunState(context, original_input=f"activate {run_id}", starting_agent=agent)
    state._current_step = NextStepInterruption(interruptions=[approval_item])
    return state
