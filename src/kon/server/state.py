"""Server state -- session management, global event bus, provider lifecycle."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from kon.core.types import ImageContent, Usage
from kon.events import Event
from kon.llm import (
    API_TYPE_TO_PROVIDER_CLASS,
    BaseProvider,
    ProviderConfig,
    get_max_tokens,
    resolve_provider_api_type,
)
from kon.loop import Agent, AgentConfig
from kon.session import Session
from kon.tools import DEFAULT_TOOLS, get_tools

from .question import PendingQuestion, QuestionTool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Monkey-patch: fix Kon AnthropicProvider for Anthropic SDK v0.83+
# ---------------------------------------------------------------------------

def _patch_anthropic_provider() -> None:
    try:
        from kon.llm.providers.anthropic import AnthropicProvider

        _original_init = AnthropicProvider.__init__

        def _patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
            _original_init(self, *args, **kwargs)
            if hasattr(self, "_client") and self._client is not None:
                _original_stream = self._client.messages.stream

                def _patched_stream(**kw: Any) -> Any:
                    kw.pop("stream", None)
                    return _original_stream(**kw)

                self._client.messages.stream = _patched_stream

        AnthropicProvider.__init__ = _patched_init  # type: ignore[assignment]
        logger.info("Patched AnthropicProvider (strips stream=True from SDK calls)")
    except Exception as e:
        logger.warning(f"Failed to patch AnthropicProvider: {e}")

_patch_anthropic_provider()


# ---------------------------------------------------------------------------
# Global event bus
# ---------------------------------------------------------------------------

class GlobalEventBus:
    """Fan-out event bus for the global SSE endpoint.

    Every SSE consumer subscribes and gets its own asyncio.Queue.
    Events are broadcast to all subscribers.
    """

    def __init__(self, max_subscribers: int = 50) -> None:
        self._subscribers: list[asyncio.Queue] = []
        self._max = max_subscribers

    def subscribe(self) -> asyncio.Queue:
        if len(self._subscribers) >= self._max:
            raise RuntimeError(f"Global SSE bus at capacity ({self._max})")
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    def broadcast(self, event: dict[str, Any]) -> None:
        for q in self._subscribers:
            q.put_nowait(event)


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

@dataclass
class SessionState:
    """Tracks a live agent session."""

    session_id: str
    session: Session
    agent: Agent | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    pending_question: PendingQuestion | None = None
    question_tool: QuestionTool = field(default_factory=QuestionTool)
    running: bool = False
    _run_task: asyncio.Task | None = None
    created_at: datetime = field(default_factory=datetime.now)
    last_activity: datetime = field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# Server state
# ---------------------------------------------------------------------------

class ServerState:
    """Central state for the fleet-compatible server."""

    def __init__(self) -> None:
        self.sessions: dict[str, SessionState] = {}
        self.event_bus = GlobalEventBus()
        self._provider: BaseProvider | None = None
        self._model: str | None = None
        self._provider_name: str | None = None
        self._workspace: str = os.environ.get("KON_WORKSPACE", os.getcwd())
        self._system_prompt: str | None = os.environ.get("KON_SYSTEM_PROMPT")

        # Auto-load fleet instructions when running as a fleet worker
        if not self._system_prompt and os.environ.get("FLEET_API_URL"):
            self._system_prompt = self._load_fleet_instructions()

    def _load_fleet_instructions(self) -> str | None:
        """Load fleet worker instructions bundled with the package."""
        from pathlib import Path
        instructions_path = Path(__file__).parent / "fleet_instructions.md"
        if instructions_path.exists():
            logger.info(f"Loading fleet instructions from {instructions_path}")
            return instructions_path.read_text()
        return None

    # -- Provider management ------------------------------------------------

    def _get_provider(self) -> BaseProvider:
        if self._provider is not None:
            return self._provider

        provider_name = self._provider_name or os.environ.get("KON_PROVIDER", "anthropic")
        model_id = self._model or os.environ.get("KON_MODEL", "claude-sonnet-4-20250514")
        api_key = (
            os.environ.get("KON_API_KEY", "").strip()
            or os.environ.get("ANTHROPIC_API_KEY", "").strip()
        )
        base_url = os.environ.get("KON_BASE_URL")

        max_tokens = get_max_tokens(model_id) or 8192

        config = ProviderConfig(
            api_key=api_key if api_key else None,
            base_url=base_url,
            model=model_id,
            max_tokens=max_tokens,
            provider=provider_name,
        )

        if provider_name == "anthropic":
            from kon.llm.providers.anthropic import AnthropicProvider
            self._provider = AnthropicProvider(config)
            logger.info(f"Created AnthropicProvider model={model_id}")
            return self._provider

        try:
            api_type = resolve_provider_api_type(provider_name)
            provider_cls = API_TYPE_TO_PROVIDER_CLASS[api_type]
        except (KeyError, ValueError):
            from kon.llm.providers.anthropic import AnthropicProvider
            provider_cls = AnthropicProvider  # type: ignore[assignment]

        self._provider = provider_cls(config)
        logger.info(f"Created {provider_cls.__name__} model={model_id}")
        return self._provider

    def set_model(self, model_spec: str) -> None:
        """Set model at runtime.  Accepts 'provider/model' or just 'model'."""
        if "/" in model_spec:
            parts = model_spec.split("/", 1)
            self._provider_name = parts[0]
            self._model = parts[1]
        else:
            self._model = model_spec
        # Reset provider so it gets recreated with new model
        self._provider = None
        logger.info(f"Model updated: {model_spec}")

    # -- Session management -------------------------------------------------

    async def create_session(self) -> SessionState:
        provider_name = self._provider_name or os.environ.get("KON_PROVIDER", "anthropic")
        model_id = self._model or os.environ.get("KON_MODEL", "claude-sonnet-4-20250514")

        session = Session.create(
            cwd=self._workspace,
            persist=True,
            provider=provider_name,
            model_id=model_id,
        )

        state = SessionState(
            session_id=session.id,
            session=session,
        )

        state.question_tool.set_callback(
            lambda pq: self._on_question(state, pq)
        )

        self.sessions[session.id] = state
        logger.info(f"Created session: {session.id}")

        # Broadcast session.created event
        self.event_bus.broadcast({
            "type": "session.created",
            "properties": {
                "sessionID": session.id,
            },
        })

        return state

    def get_session(self, session_id: str) -> SessionState | None:
        return self.sessions.get(session_id)

    def list_sessions(self) -> list[dict[str, Any]]:
        result = []
        for sid, state in self.sessions.items():
            result.append({
                "id": sid,
                "title": state.session.name or "",
                "createdAt": state.created_at.isoformat(),
                "updatedAt": state.last_activity.isoformat(),
            })
        return result

    # -- Messaging ----------------------------------------------------------

    def _get_tools(self, state: SessionState) -> list:
        """Assemble tool list for an agent run."""
        import kon.tools as _kon_tools

        base_tools = get_tools(DEFAULT_TOOLS) + [state.question_tool]

        # Register question tool globally so turn dispatcher can resolve it
        _kon_tools.tools_by_name[state.question_tool.name] = state.question_tool

        # Add fleet tools if running in fleet mode
        fleet_tools = self._get_fleet_tools()
        for ft in fleet_tools:
            base_tools.append(ft)
            _kon_tools.tools_by_name[ft.name] = ft

        return base_tools

    def _get_fleet_tools(self) -> list:
        """Load fleet tools if FLEET_API_URL is set."""
        fleet_url = os.environ.get("FLEET_API_URL")
        if not fleet_url:
            return []

        try:
            from kon.tools.fleet import get_fleet_tools
            instance_id = os.environ.get("OPENCODE_FLEET_INSTANCE_ID", "")
            return get_fleet_tools(fleet_url, instance_id)
        except ImportError:
            logger.warning("Fleet tools module not found")
            return []

    async def send_message(self, session_id: str, text: str) -> bool:
        """Start an agent run.  Returns True if started."""
        state = self.sessions.get(session_id)
        if state is None or state.running:
            return False

        state.last_activity = datetime.now()
        state.cancel_event = asyncio.Event()
        state.running = True

        provider = self._get_provider()
        tools = self._get_tools(state)

        agent_config = AgentConfig(
            system_prompt=self._system_prompt,
            cwd=self._workspace,
        )

        state.agent = Agent(
            provider=provider,
            tools=tools,
            session=state.session,
            config=agent_config,
        )

        state._run_task = asyncio.create_task(
            self._run_agent(state, text)
        )
        return True

    async def send_context(self, session_id: str, text: str) -> bool:
        """Inject context without triggering a run (noReply mode)."""
        from kon.core.types import UserMessage
        state = self.sessions.get(session_id)
        if state is None:
            return False

        user_msg = UserMessage(content=text)
        state.session.append_message(user_msg)
        state.last_activity = datetime.now()
        return True

    async def _run_agent(self, state: SessionState, query: str) -> None:
        """Run the agent loop and push events to the global bus."""
        from .events import translate_event
        session_id = state.session_id

        # Track message ID and part indices for OpenCode-compatible events
        message_id = str(uuid.uuid4())
        part_index = 0
        current_text = ""
        current_reasoning = ""

        try:
            async for event in state.agent.run(  # type: ignore[union-attr]
                query=query,
                cancel_event=state.cancel_event,
            ):
                # Translate kon event -> OpenCode-compatible SSE events
                oc_events = translate_event(
                    event, session_id, message_id, part_index,
                    current_text, current_reasoning,
                )
                for oc_event in oc_events:
                    self.event_bus.broadcast(oc_event)

                    # Track state for subsequent translations
                    etype = oc_event.get("type", "")
                    if etype == "message.part.updated":
                        props = oc_event.get("properties", {})
                        part = props.get("part", {})
                        if part.get("type") == "text":
                            current_text = part.get("text", "")
                        elif part.get("type") == "reasoning":
                            current_reasoning = part.get("reasoning", "")
                    elif etype == "message.part.delta":
                        props = oc_event.get("properties", {})
                        content = props.get("content", "")
                        # This is a delta, accumulate
                        # The actual accumulation happens in translate_event

        except Exception as e:
            logger.exception(f"Agent run error in {session_id}: {e}")
            error_msg = str(e)[:300]
            self.event_bus.broadcast({
                "type": "message.updated",
                "properties": {
                    "sessionID": session_id,
                    "message": {
                        "id": message_id,
                        "role": "assistant",
                        "error": error_msg,
                    },
                },
            })
        finally:
            state.running = False
            # Emit session.idle
            self.event_bus.broadcast({
                "type": "session.idle",
                "properties": {
                    "sessionID": session_id,
                },
            })

    async def abort(self, session_id: str) -> bool:
        state = self.sessions.get(session_id)
        if state is None or not state.running:
            return False
        state.cancel_event.set()
        logger.info(f"Aborted session: {session_id}")
        return True

    # -- Questions ----------------------------------------------------------

    async def _on_question(self, state: SessionState, pending: PendingQuestion) -> None:
        pending.session_id = state.session_id
        state.pending_question = pending

        self.event_bus.broadcast({
            "type": "question.asked",
            "properties": {
                "sessionID": state.session_id,
                "question": {
                    "id": pending.request_id,
                    "questions": pending.questions,
                },
            },
        })

    def list_pending_questions(self) -> list[dict[str, Any]]:
        result = []
        for state in self.sessions.values():
            pq = state.pending_question
            if pq and not pq.future.done():
                result.append({
                    "id": pq.request_id,
                    "sessionId": state.session_id,
                    "questions": pq.questions,
                })
        return result

    async def reply_question(self, request_id: str, answers: list[list[str]]) -> bool:
        for state in self.sessions.values():
            pq = state.pending_question
            if pq and pq.request_id == request_id:
                if not pq.future.done():
                    pq.future.set_result(answers)
                state.pending_question = None
                return True
        return False

    # -- Messages -----------------------------------------------------------

    def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        """Get messages in OpenCode-compatible format."""
        from .events import serialize_messages
        state = self.sessions.get(session_id)
        if state is None:
            return []
        return serialize_messages(state.session.all_messages)
