"""Fleet tools -- native Python implementations of the fleet-spawn MCP tools.

These tools allow a kon worker to interact with the fleet backend:
  - Deliver results (fleet_deliver)
  - Spawn sub-workers (fleet_spawn_worker)
  - Kill workers (fleet_kill_worker)
  - List instances (fleet_list_instances)
  - Answer worker questions (fleet_answer_question)
  - Get pending questions (fleet_get_pending_questions)
  - Get worker deliverables (fleet_get_worker_deliverables)
  - Get worker sessions (fleet_get_worker_sessions)
  - Get worker messages (fleet_get_worker_messages)

All tools communicate with the fleet backend REST API via HTTP.
They are only loaded when FLEET_API_URL is set in the environment.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Literal

import aiohttp
from pydantic import BaseModel, Field

from kon.core.types import ToolResult
from kon.tools.base import BaseTool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

async def _fleet_request(
    method: str,
    url: str,
    body: dict[str, Any] | None = None,
    timeout: int = 30,
) -> tuple[int, Any]:
    """Make an HTTP request to the fleet backend."""
    async with aiohttp.ClientSession() as session:
        kwargs: dict[str, Any] = {
            "timeout": aiohttp.ClientTimeout(total=timeout),
        }
        if body is not None:
            kwargs["json"] = body

        async with session.request(method, url, **kwargs) as resp:
            try:
                data = await resp.json()
            except Exception:
                data = await resp.text()
            return resp.status, data


# ---------------------------------------------------------------------------
# fleet_deliver
# ---------------------------------------------------------------------------

class FleetDeliverParams(BaseModel):
    type: Literal["string", "file", "dir"] = Field(
        description="Type of deliverable: 'string' for inline text (<4KB), "
        "'file' for a single file, 'dir' for a directory"
    )
    message: str = Field(description="Human-readable summary of what is being delivered")
    finished: bool = Field(description="Whether this delivery signals task completion")
    content: str | None = Field(default=None, description="Inline content for type='string'")
    path: str | None = Field(
        default=None,
        description="Relative path to file/dir for type='file' or 'dir'"
    )


class FleetDeliverTool(BaseTool[FleetDeliverParams]):
    name = "fleet_deliver"
    params = FleetDeliverParams
    description = (
        "Deliver results back to the conductor. Workers MUST call this when they "
        "have output to share or when their task is complete. Set finished=true "
        "on the final delivery to signal task completion."
    )

    def __init__(self, fleet_url: str, instance_id: str) -> None:
        self._fleet_url = fleet_url
        self._instance_id = instance_id

    async def execute(
        self, params: FleetDeliverParams, cancel_event: asyncio.Event | None = None
    ) -> ToolResult:
        url = f"{self._fleet_url}/instance/{self._instance_id}/deliverable"
        body: dict[str, Any] = {
            "type": params.type,
            "message": params.message,
            "finished": params.finished,
        }
        if params.content is not None:
            body["content"] = params.content
        if params.path is not None:
            # Resolve relative path to absolute
            resolved = os.path.abspath(params.path)
            body["path"] = resolved

        try:
            status, data = await _fleet_request("POST", url, body)
            if status < 300:
                return ToolResult(
                    success=True,
                    result=f"Delivered ({params.type}): {params.message}",
                )
            return ToolResult(
                success=False,
                result=f"Fleet delivery failed (HTTP {status}): {data}",
            )
        except Exception as e:
            return ToolResult(success=False, result=f"Fleet delivery error: {e}")


# ---------------------------------------------------------------------------
# fleet_spawn_worker
# ---------------------------------------------------------------------------

class FleetSpawnWorkerParams(BaseModel):
    role: str | None = Field(default=None, description="Name or role for this worker")
    instructions: str | None = Field(
        default=None, description="Initial instructions to send to the worker"
    )


class FleetSpawnWorkerTool(BaseTool[FleetSpawnWorkerParams]):
    name = "fleet_spawn_worker"
    params = FleetSpawnWorkerParams
    description = (
        "Spawn a new worker instance in the fleet. Returns the instance ID and port."
    )

    def __init__(self, fleet_url: str, instance_id: str) -> None:
        self._fleet_url = fleet_url
        self._instance_id = instance_id

    async def execute(
        self, params: FleetSpawnWorkerParams, cancel_event: asyncio.Event | None = None
    ) -> ToolResult:
        url = f"{self._fleet_url}/instance/spawn"
        body: dict[str, Any] = {
            "role": params.role or "worker",
            "serverType": "kon",
            "parentId": self._instance_id,
        }

        try:
            status, data = await _fleet_request("POST", url, body, timeout=60)
            if status >= 300:
                return ToolResult(success=False, result=f"Spawn failed (HTTP {status}): {data}")

            instance_id = data.get("id", "unknown")
            port = data.get("port", "unknown")

            # If instructions provided, send them
            if params.instructions:
                await asyncio.sleep(2)  # Wait for session creation
                # Discover session
                sessions_url = f"{self._fleet_url}/instance/{instance_id}/sessions"
                _, sessions = await _fleet_request("GET", sessions_url)
                if isinstance(sessions, list) and sessions:
                    sid = sessions[0].get("id")
                    if sid:
                        msg_url = f"{self._fleet_url}/instance/{instance_id}/message"
                        await _fleet_request("POST", msg_url, {
                            "sessionId": sid,
                            "parts": [{"type": "text", "text": params.instructions}],
                        })

            return ToolResult(
                success=True,
                result=f"Spawned worker: id={instance_id}, port={port}",
            )
        except Exception as e:
            return ToolResult(success=False, result=f"Spawn error: {e}")


# ---------------------------------------------------------------------------
# fleet_kill_worker
# ---------------------------------------------------------------------------

class FleetKillWorkerParams(BaseModel):
    instanceId: str = Field(description="ID of the instance to kill")


class FleetKillWorkerTool(BaseTool[FleetKillWorkerParams]):
    name = "fleet_kill_worker"
    params = FleetKillWorkerParams
    description = "Kill a running worker instance."

    def __init__(self, fleet_url: str) -> None:
        self._fleet_url = fleet_url

    async def execute(
        self, params: FleetKillWorkerParams, cancel_event: asyncio.Event | None = None
    ) -> ToolResult:
        url = f"{self._fleet_url}/instance/{params.instanceId}"
        try:
            status, data = await _fleet_request("DELETE", url)
            if status < 300:
                return ToolResult(success=True, result=f"Killed {params.instanceId}")
            return ToolResult(success=False, result=f"Kill failed (HTTP {status}): {data}")
        except Exception as e:
            return ToolResult(success=False, result=f"Kill error: {e}")


# ---------------------------------------------------------------------------
# fleet_list_instances
# ---------------------------------------------------------------------------

class FleetListInstancesParams(BaseModel):
    pass


class FleetListInstancesTool(BaseTool[FleetListInstancesParams]):
    name = "fleet_list_instances"
    params = FleetListInstancesParams
    description = "List all running instances in the fleet."

    def __init__(self, fleet_url: str) -> None:
        self._fleet_url = fleet_url

    async def execute(
        self, params: FleetListInstancesParams, cancel_event: asyncio.Event | None = None
    ) -> ToolResult:
        url = f"{self._fleet_url}/instances"
        try:
            status, data = await _fleet_request("GET", url)
            if status < 300:
                if isinstance(data, list):
                    lines = []
                    for inst in data:
                        lines.append(
                            f"  {inst.get('id', '?')}: "
                            f"role={inst.get('role', '?')}, "
                            f"status={inst.get('status', '?')}, "
                            f"port={inst.get('port', '?')}"
                        )
                    return ToolResult(
                        success=True,
                        result=f"Instances ({len(data)}):\n" + "\n".join(lines),
                    )
                return ToolResult(success=True, result=json.dumps(data, indent=2))
            return ToolResult(success=False, result=f"List failed (HTTP {status}): {data}")
        except Exception as e:
            return ToolResult(success=False, result=f"List error: {e}")


# ---------------------------------------------------------------------------
# fleet_answer_question
# ---------------------------------------------------------------------------

class FleetAnswerQuestionParams(BaseModel):
    questionId: str = Field(description="The question ID")
    answers: list[list[str]] = Field(
        description="Answers -- one inner array per question"
    )


class FleetAnswerQuestionTool(BaseTool[FleetAnswerQuestionParams]):
    name = "fleet_answer_question"
    params = FleetAnswerQuestionParams
    description = "Answer a pending question from a worker."

    def __init__(self, fleet_url: str) -> None:
        self._fleet_url = fleet_url

    async def execute(
        self, params: FleetAnswerQuestionParams, cancel_event: asyncio.Event | None = None
    ) -> ToolResult:
        url = f"{self._fleet_url}/question/{params.questionId}/answer"
        try:
            status, data = await _fleet_request("POST", url, {"answers": params.answers})
            if status < 300:
                return ToolResult(success=True, result="Question answered.")
            return ToolResult(success=False, result=f"Answer failed (HTTP {status}): {data}")
        except Exception as e:
            return ToolResult(success=False, result=f"Answer error: {e}")


# ---------------------------------------------------------------------------
# fleet_get_pending_questions
# ---------------------------------------------------------------------------

class FleetGetPendingQuestionsParams(BaseModel):
    pass


class FleetGetPendingQuestionsTool(BaseTool[FleetGetPendingQuestionsParams]):
    name = "fleet_get_pending_questions"
    params = FleetGetPendingQuestionsParams
    description = "List all pending questions from workers."

    def __init__(self, fleet_url: str) -> None:
        self._fleet_url = fleet_url

    async def execute(
        self, params: FleetGetPendingQuestionsParams, cancel_event: asyncio.Event | None = None
    ) -> ToolResult:
        url = f"{self._fleet_url}/questions/pending"
        try:
            status, data = await _fleet_request("GET", url)
            if status < 300:
                return ToolResult(success=True, result=json.dumps(data, indent=2))
            return ToolResult(success=False, result=f"Failed (HTTP {status}): {data}")
        except Exception as e:
            return ToolResult(success=False, result=f"Error: {e}")


# ---------------------------------------------------------------------------
# fleet_get_worker_deliverables
# ---------------------------------------------------------------------------

class FleetGetWorkerDeliverablesParams(BaseModel):
    instanceId: str = Field(description="ID of the worker instance")


class FleetGetWorkerDeliverablesTool(BaseTool[FleetGetWorkerDeliverablesParams]):
    name = "fleet_get_worker_deliverables"
    params = FleetGetWorkerDeliverablesParams
    description = "Get deliverables submitted by a worker."

    def __init__(self, fleet_url: str) -> None:
        self._fleet_url = fleet_url

    async def execute(
        self, params: FleetGetWorkerDeliverablesParams, cancel_event: asyncio.Event | None = None
    ) -> ToolResult:
        url = f"{self._fleet_url}/instance/{params.instanceId}/deliverables"
        try:
            status, data = await _fleet_request("GET", url)
            if status < 300:
                return ToolResult(success=True, result=json.dumps(data, indent=2))
            return ToolResult(success=False, result=f"Failed (HTTP {status}): {data}")
        except Exception as e:
            return ToolResult(success=False, result=f"Error: {e}")


# ---------------------------------------------------------------------------
# fleet_get_worker_sessions
# ---------------------------------------------------------------------------

class FleetGetWorkerSessionsParams(BaseModel):
    instanceId: str = Field(description="ID of the worker instance")


class FleetGetWorkerSessionsTool(BaseTool[FleetGetWorkerSessionsParams]):
    name = "fleet_get_worker_sessions"
    params = FleetGetWorkerSessionsParams
    description = "List sessions on a specific worker instance."

    def __init__(self, fleet_url: str) -> None:
        self._fleet_url = fleet_url

    async def execute(
        self, params: FleetGetWorkerSessionsParams, cancel_event: asyncio.Event | None = None
    ) -> ToolResult:
        url = f"{self._fleet_url}/instance/{params.instanceId}/sessions"
        try:
            status, data = await _fleet_request("GET", url)
            if status < 300:
                return ToolResult(success=True, result=json.dumps(data, indent=2))
            return ToolResult(success=False, result=f"Failed (HTTP {status}): {data}")
        except Exception as e:
            return ToolResult(success=False, result=f"Error: {e}")


# ---------------------------------------------------------------------------
# fleet_get_worker_messages
# ---------------------------------------------------------------------------

class FleetGetWorkerMessagesParams(BaseModel):
    instanceId: str = Field(description="ID of the worker instance")
    sessionId: str = Field(description="Session ID to read messages from")
    lastN: int | None = Field(default=None, description="Only return last N messages")


class FleetGetWorkerMessagesTool(BaseTool[FleetGetWorkerMessagesParams]):
    name = "fleet_get_worker_messages"
    params = FleetGetWorkerMessagesParams
    description = "Read messages from a worker's session."

    def __init__(self, fleet_url: str) -> None:
        self._fleet_url = fleet_url

    async def execute(
        self, params: FleetGetWorkerMessagesParams, cancel_event: asyncio.Event | None = None
    ) -> ToolResult:
        url = f"{self._fleet_url}/instance/{params.instanceId}/sessions/{params.sessionId}/messages"
        try:
            status, data = await _fleet_request("GET", url)
            if status < 300:
                messages = data if isinstance(data, list) else data.get("messages", [])
                if params.lastN and len(messages) > params.lastN:
                    messages = messages[-params.lastN:]
                # Truncate for context window
                text = json.dumps(messages, indent=2)
                if len(text) > 10000:
                    text = text[:10000] + "\n... (truncated)"
                return ToolResult(success=True, result=text)
            return ToolResult(success=False, result=f"Failed (HTTP {status}): {data}")
        except Exception as e:
            return ToolResult(success=False, result=f"Error: {e}")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_fleet_tools(fleet_url: str, instance_id: str) -> list[BaseTool]:
    """Create all fleet tools for a given fleet URL and instance ID."""
    return [
        FleetDeliverTool(fleet_url, instance_id),
        FleetSpawnWorkerTool(fleet_url, instance_id),
        FleetKillWorkerTool(fleet_url),
        FleetListInstancesTool(fleet_url),
        FleetAnswerQuestionTool(fleet_url),
        FleetGetPendingQuestionsTool(fleet_url),
        FleetGetWorkerDeliverablesTool(fleet_url),
        FleetGetWorkerSessionsTool(fleet_url),
        FleetGetWorkerMessagesTool(fleet_url),
    ]
