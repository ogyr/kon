"""Fleet-compatible Starlette application.

Exposes the OpenCode-compatible HTTP API that the opencode-fleet
orchestrator expects from worker instances.  All routes use the URL
patterns the fleet's proxy calls directly (no /api prefix for session
routes -- the fleet calls ``/session``, ``/event``, ``/question``, etc.).

The health endpoint is at ``/api/health`` because the fleet's
``getSpawnCommand()`` for kon hardcodes that path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from sse_starlette.sse import EventSourceResponse

from .state import ServerState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared state -- created once at import, used by all handlers
# ---------------------------------------------------------------------------

server_state = ServerState()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

async def health(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "server": "kon"})


async def global_health(request: Request) -> JSONResponse:
    """OpenCode-style health at /global/health for compatibility."""
    return JSONResponse({"ok": True, "server": "kon"})


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

async def list_sessions(request: Request) -> JSONResponse:
    """GET /session -- returns an array (fleet expects raw array)."""
    sessions = server_state.list_sessions()
    return JSONResponse(sessions)


async def create_session(request: Request) -> JSONResponse:
    """POST /session -- create a new session."""
    state = await server_state.create_session()
    return JSONResponse({"id": state.session_id})


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

async def send_message(request: Request) -> JSONResponse:
    """POST /session/:id/message -- send a prompt to trigger the agent.

    Fleet sends: {"parts": [{"type":"text","text":"..."}]}
    Also supports: {"message": "..."} for simple text
    Also supports: {"noReply": true} for context injection
    """
    session_id = request.path_params["session_id"]
    body = await request.json()

    # Extract text from OpenCode parts format or simple message
    text = ""
    no_reply = body.get("noReply", False)

    parts = body.get("parts", [])
    if parts:
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "text":
                text += part.get("text", "")
    else:
        text = body.get("message", body.get("text", ""))

    if not text.strip():
        return JSONResponse({"error": "No message content"}, status_code=400)

    state = server_state.get_session(session_id)
    if state is None:
        # Auto-create session if it doesn't exist
        state = await server_state.create_session()
        # Update the session_id to the new one if needed
        if session_id != state.session_id:
            # Re-register under the requested ID won't work; just use new session
            pass

    if state is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    if no_reply:
        await server_state.send_context(state.session_id, text)
        return JSONResponse({"ok": True})

    started = await server_state.send_message(state.session_id, text)
    if not started:
        return JSONResponse({"error": "Session is already running"}, status_code=409)

    return JSONResponse({"ok": True, "sessionId": state.session_id})


async def get_messages(request: Request) -> JSONResponse:
    """GET /session/:id/message -- read conversation history."""
    session_id = request.path_params["session_id"]
    messages = server_state.get_messages(session_id)
    return JSONResponse(messages)


# ---------------------------------------------------------------------------
# Abort
# ---------------------------------------------------------------------------

async def abort_session(request: Request) -> JSONResponse:
    """POST /session/:id/abort -- interrupt the running agent."""
    session_id = request.path_params["session_id"]
    ok = await server_state.abort(session_id)
    if not ok:
        return JSONResponse({"error": "Session not found or not running"}, status_code=404)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# SSE -- Global event stream
# ---------------------------------------------------------------------------

async def event_stream(request: Request) -> EventSourceResponse:
    """GET /event -- global SSE stream for all sessions.

    The fleet relay subscribes to this and intercepts question.asked events
    from workers to route them to the conductor.
    """
    try:
        queue = server_state.event_bus.subscribe()
    except RuntimeError as exc:
        async def error_gen():
            yield {"event": "error", "data": json.dumps({"error": str(exc)})}
        return EventSourceResponse(error_gen())

    async def generator():
        try:
            # Send initial connection event
            yield {
                "data": json.dumps({"type": "server.connected"}),
            }

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    # Keepalive
                    yield {"data": json.dumps({"type": "ping"})}
                    continue

                if event is None:
                    continue

                event_type = event.get("type", "unknown")
                yield {
                    "data": json.dumps(event),
                }

        except asyncio.CancelledError:
            logger.info("SSE stream cancelled")
        except Exception as e:
            logger.exception(f"SSE stream error: {e}")
            yield {"data": json.dumps({"type": "error", "error": str(e)})}
        finally:
            server_state.event_bus.unsubscribe(queue)

    return EventSourceResponse(generator())


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------

async def list_questions(request: Request) -> JSONResponse:
    """GET /question -- list pending questions across all sessions."""
    questions = server_state.list_pending_questions()
    return JSONResponse(questions)


async def reply_question(request: Request) -> JSONResponse:
    """POST /question/:id/reply -- answer a pending question."""
    request_id = request.path_params["request_id"]
    body = await request.json()
    answers = body.get("answers", [])

    ok = await server_state.reply_question(request_id, answers)
    if not ok:
        return JSONResponse({"error": "Question not found"}, status_code=404)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Permissions (stub -- kon has no permission system)
# ---------------------------------------------------------------------------

async def list_permissions(request: Request) -> JSONResponse:
    """GET /permission -- always returns empty (kon auto-grants all)."""
    return JSONResponse([])


async def reply_permission(request: Request) -> JSONResponse:
    """POST /permission/:id/reply -- no-op, always OK."""
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

async def patch_config(request: Request) -> JSONResponse:
    """PATCH /config -- set model at runtime.

    Fleet sends: {"model": "anthropic/claude-sonnet-4-6"}
    """
    body = await request.json()
    model = body.get("model")
    if model:
        server_state.set_model(model)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Session status
# ---------------------------------------------------------------------------

async def session_status(request: Request) -> JSONResponse:
    """GET /session/status -- check busy/idle status for all sessions."""
    statuses = {}
    for sid, state in server_state.sessions.items():
        statuses[sid] = "busy" if state.running else "idle"
    return JSONResponse(statuses)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app() -> Starlette:
    """Create the Starlette ASGI application."""
    routes = [
        # Health (fleet expects /api/health for kon type)
        Route("/api/health", health, methods=["GET"]),
        # Also support /global/health for OpenCode compat
        Route("/global/health", global_health, methods=["GET"]),

        # Sessions
        Route("/session", list_sessions, methods=["GET"]),
        Route("/session", create_session, methods=["POST"]),
        Route("/session/status", session_status, methods=["GET"]),
        Route("/session/{session_id}/message", send_message, methods=["POST"]),
        Route("/session/{session_id}/message", get_messages, methods=["GET"]),
        Route("/session/{session_id}/abort", abort_session, methods=["POST"]),

        # Global SSE
        Route("/event", event_stream, methods=["GET"]),

        # Questions
        Route("/question", list_questions, methods=["GET"]),
        Route("/question/{request_id}/reply", reply_question, methods=["POST"]),

        # Permissions (stubs)
        Route("/permission", list_permissions, methods=["GET"]),
        Route("/permission/{permission_id}/reply", reply_permission, methods=["POST"]),

        # Config
        Route("/config", patch_config, methods=["PATCH"]),
    ]

    app = Starlette(
        routes=routes,
        middleware=[
            Middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_methods=["*"],
                allow_headers=["*"],
            ),
        ],
    )

    return app


# Module-level app instance for uvicorn
app = create_app()
