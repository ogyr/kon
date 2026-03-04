"""Event translation -- Kon events -> OpenCode-compatible SSE format.

The fleet relay expects SSE events with specific ``type`` fields that match
OpenCode's naming convention.  This module translates Kon's dataclass events
into dicts that the relay can parse.

OpenCode SSE event types the relay cares about:
  - message.updated       -- full message snapshot (role, parts)
  - message.part.updated  -- a single part created/updated (text, tool_use)
  - message.part.delta    -- incremental text content
  - session.idle          -- agent finished
  - session.created       -- session was created
  - question.asked        -- agent asked a question
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime
from typing import Any

from kon.core.types import (
    AssistantMessage,
    Message,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from kon.events import (
    AgentEndEvent,
    AgentStartEvent,
    CompactionEndEvent,
    CompactionStartEvent,
    ErrorEvent,
    Event,
    InterruptedEvent,
    RetryEvent,
    TextDeltaEvent,
    TextEndEvent,
    TextStartEvent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
    ThinkingStartEvent,
    ToolArgsDeltaEvent,
    ToolArgsTokenUpdateEvent,
    ToolEndEvent,
    ToolResultEvent,
    ToolStartEvent,
    TurnEndEvent,
    TurnStartEvent,
    WarningEvent,
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class _PartTracker:
    """Tracks part indices and accumulated content for a message."""

    def __init__(self) -> None:
        self.next_index: int = 0
        self.text_accumulator: str = ""
        self.reasoning_accumulator: str = ""
        self.tool_call_index: dict[str, int] = {}  # tool_call_id -> part index

    def alloc_index(self) -> int:
        idx = self.next_index
        self.next_index += 1
        return idx


# Module-level tracker per session
_trackers: dict[str, _PartTracker] = {}


def _get_tracker(session_id: str) -> _PartTracker:
    if session_id not in _trackers:
        _trackers[session_id] = _PartTracker()
    return _trackers[session_id]


def _reset_tracker(session_id: str) -> None:
    _trackers.pop(session_id, None)


def translate_event(
    event: Event,
    session_id: str,
    message_id: str,
    part_index: int,
    current_text: str,
    current_reasoning: str,
) -> list[dict[str, Any]]:
    """Translate a single Kon event into zero or more OpenCode-compatible SSE dicts."""

    tracker = _get_tracker(session_id)
    results: list[dict[str, Any]] = []

    # -- Agent lifecycle ---------------------------------------------------

    if isinstance(event, AgentStartEvent):
        _reset_tracker(session_id)
        _get_tracker(session_id)  # fresh tracker
        # Emit a status change
        results.append({
            "type": "session.status",
            "properties": {
                "sessionID": session_id,
                "status": "busy",
            },
        })

    elif isinstance(event, AgentEndEvent):
        # session.idle is emitted in state.py's finally block
        pass

    # -- Turn lifecycle ----------------------------------------------------

    elif isinstance(event, TurnStartEvent):
        # Reset text accumulator for new turn
        tracker.text_accumulator = ""
        tracker.reasoning_accumulator = ""

    elif isinstance(event, TurnEndEvent):
        # Emit a message.updated with full message snapshot
        if event.assistant_message:
            results.append({
                "type": "message.updated",
                "properties": {
                    "sessionID": session_id,
                    "message": _serialize_assistant_message(
                        event.assistant_message, message_id, session_id
                    ),
                },
            })

    # -- Text streaming ----------------------------------------------------

    elif isinstance(event, TextStartEvent):
        idx = tracker.alloc_index()
        tracker.text_accumulator = ""
        results.append({
            "type": "message.part.updated",
            "properties": {
                "sessionID": session_id,
                "messageID": message_id,
                "part": {
                    "type": "text",
                    "text": "",
                    "index": idx,
                },
            },
        })

    elif isinstance(event, TextDeltaEvent):
        tracker.text_accumulator += event.delta
        results.append({
            "type": "message.part.delta",
            "properties": {
                "sessionID": session_id,
                "messageID": message_id,
                "content": event.delta,
            },
        })

    elif isinstance(event, TextEndEvent):
        # Emit final text part
        results.append({
            "type": "message.part.updated",
            "properties": {
                "sessionID": session_id,
                "messageID": message_id,
                "part": {
                    "type": "text",
                    "text": tracker.text_accumulator,
                },
            },
        })

    # -- Thinking/reasoning streaming --------------------------------------

    elif isinstance(event, ThinkingStartEvent):
        idx = tracker.alloc_index()
        tracker.reasoning_accumulator = ""
        results.append({
            "type": "message.part.updated",
            "properties": {
                "sessionID": session_id,
                "messageID": message_id,
                "part": {
                    "type": "reasoning",
                    "reasoning": "",
                    "index": idx,
                },
            },
        })

    elif isinstance(event, ThinkingDeltaEvent):
        tracker.reasoning_accumulator += event.delta
        results.append({
            "type": "message.part.delta",
            "properties": {
                "sessionID": session_id,
                "messageID": message_id,
                "content": event.delta,
            },
        })

    elif isinstance(event, ThinkingEndEvent):
        results.append({
            "type": "message.part.updated",
            "properties": {
                "sessionID": session_id,
                "messageID": message_id,
                "part": {
                    "type": "reasoning",
                    "reasoning": tracker.reasoning_accumulator,
                },
            },
        })

    # -- Tool events -------------------------------------------------------

    elif isinstance(event, ToolStartEvent):
        idx = tracker.alloc_index()
        tracker.tool_call_index[event.tool_call_id] = idx
        results.append({
            "type": "message.part.updated",
            "properties": {
                "sessionID": session_id,
                "messageID": message_id,
                "part": {
                    "type": "tool-invocation",
                    "toolInvocationId": event.tool_call_id,
                    "toolName": event.tool_name,
                    "state": "partial-call",
                    "args": {},
                    "index": idx,
                },
            },
        })

    elif isinstance(event, ToolArgsDeltaEvent):
        results.append({
            "type": "message.part.delta",
            "properties": {
                "sessionID": session_id,
                "messageID": message_id,
                "toolInvocationId": event.tool_call_id,
                "content": event.delta,
            },
        })

    elif isinstance(event, ToolEndEvent):
        results.append({
            "type": "message.part.updated",
            "properties": {
                "sessionID": session_id,
                "messageID": message_id,
                "part": {
                    "type": "tool-invocation",
                    "toolInvocationId": event.tool_call_id,
                    "toolName": event.tool_name,
                    "state": "call",
                    "args": event.arguments,
                },
            },
        })

    elif isinstance(event, ToolResultEvent):
        result_text = ""
        if event.result:
            # ToolResultMessage has content list
            for block in event.result.content:
                if hasattr(block, "text"):
                    result_text += block.text
                elif hasattr(block, "output"):
                    result_text += block.output

        results.append({
            "type": "message.part.updated",
            "properties": {
                "sessionID": session_id,
                "messageID": message_id,
                "part": {
                    "type": "tool-invocation",
                    "toolInvocationId": event.tool_call_id,
                    "toolName": event.tool_name,
                    "state": "result",
                    "result": result_text[:5000],  # Truncate large results
                },
            },
        })

    # -- Other events ------------------------------------------------------

    elif isinstance(event, ErrorEvent):
        results.append({
            "type": "message.updated",
            "properties": {
                "sessionID": session_id,
                "message": {
                    "id": message_id,
                    "role": "assistant",
                    "error": event.error,
                },
            },
        })

    elif isinstance(event, InterruptedEvent):
        pass  # session.idle is emitted in the finally block

    elif isinstance(event, (CompactionStartEvent, CompactionEndEvent)):
        pass  # Internal events, fleet doesn't need them

    elif isinstance(event, RetryEvent):
        pass  # Internal retry logic

    elif isinstance(event, WarningEvent):
        pass  # Warnings don't map to fleet events

    return results


def _serialize_assistant_message(
    msg: AssistantMessage,
    message_id: str,
    session_id: str,
) -> dict[str, Any]:
    """Convert an AssistantMessage to OpenCode-compatible message dict."""
    parts = []
    for block in msg.content:
        if isinstance(block, TextContent):
            parts.append({
                "type": "text",
                "text": block.text,
            })
        elif isinstance(block, ThinkingContent):
            parts.append({
                "type": "reasoning",
                "reasoning": block.thinking,
            })
        elif isinstance(block, ToolCall):
            parts.append({
                "type": "tool-invocation",
                "toolInvocationId": block.id,
                "toolName": block.name,
                "state": "call",
                "args": block.arguments if isinstance(block.arguments, dict) else {},
            })

    return {
        "id": message_id,
        "role": "assistant",
        "parts": parts,
        "createdAt": _now_iso(),
        "metadata": {
            "sessionID": session_id,
        },
    }


def serialize_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Serialize a list of kon Messages to OpenCode-compatible format."""
    result = []
    for msg in messages:
        if isinstance(msg, UserMessage):
            text = ""
            if isinstance(msg.content, str):
                text = msg.content
            elif isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, TextContent):
                        text += block.text
            result.append({
                "id": str(uuid.uuid4()),
                "role": "user",
                "parts": [{"type": "text", "text": text}],
                "createdAt": _now_iso(),
            })
        elif isinstance(msg, AssistantMessage):
            mid = str(uuid.uuid4())
            result.append(_serialize_assistant_message(msg, mid, ""))
        elif isinstance(msg, ToolResultMessage):
            # Tool results are embedded in the assistant message in OpenCode format
            # but we include them for completeness
            result_text = ""
            for block in msg.content:
                if hasattr(block, "text"):
                    result_text += block.text
                elif hasattr(block, "output"):
                    result_text += block.output
            result.append({
                "id": str(uuid.uuid4()),
                "role": "tool",
                "toolCallId": msg.tool_call_id,
                "parts": [{"type": "text", "text": result_text[:5000]}],
                "createdAt": _now_iso(),
            })
    return result
