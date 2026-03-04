# Kon Fleet Integration

This document covers three things:

1. How the **opencode-fleet** orchestrator works and what it expects from workers
2. What **kon** had before this work and where the gaps were
3. What was **added** to make kon a fleet-compatible worker

---

## 1. The Fleet: How It Works

The opencode-fleet is a three-tier orchestrator:

```
Browser UI (Vite SPA)
    |  WebSocket + REST
Fleet Backend (Bun, port 5174)
    |  HTTP + SSE (per-instance)
Worker Instances (port 4096+)
```

The fleet backend spawns worker processes, subscribes to their SSE event
streams, proxies REST requests to them, and aggregates everything into a
single WebSocket for the browser UI.

### Worker Lifecycle

```
Spawn  -->  Health Check  -->  Session Create  -->  Instructions  -->  Work  -->  Deliver  -->  Kill
```

1. **Spawn** -- fleet allocates a port, creates an isolated runtime
   directory, spawns the server process.
2. **Health Check** -- polls the health endpoint every 500 ms for up to 30 s.
3. **Session Create** -- discovers or creates a session via HTTP.
4. **Instructions** -- sends initial instructions as the first user message.
5. **Work** -- agent uses tools, can ask questions (routed to parent), can
   spawn sub-workers.
6. **Deliver** -- worker calls `fleet_deliver` to send results back.
7. **Kill** -- parent calls `fleet_kill_worker`, fleet sends SIGTERM.

### HTTP API Contract

Every worker must expose these endpoints:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/health` | GET | Startup health check |
| `/session` | GET | List sessions (returns JSON array) |
| `/session` | POST | Create session (returns `{id}`) |
| `/session/:id/message` | POST | Send prompt / trigger agent |
| `/session/:id/message` | GET | Read conversation history |
| `/session/:id/abort` | POST | Interrupt running agent |
| `/event` | GET | Global SSE stream (all sessions) |
| `/question` | GET | List pending questions |
| `/question/:id/reply` | POST | Answer a pending question |
| `/permission` | GET | List pending permissions |
| `/permission/:id/reply` | POST | Grant/deny permission |
| `/config` | PATCH | Set model at runtime |

### SSE Event Types

The fleet relay subscribes to `GET /event` and expects these types:

| Event Type | When |
|------------|------|
| `message.updated` | Full message snapshot |
| `message.part.updated` | Part created/updated (text, tool call) |
| `message.part.delta` | Incremental text or reasoning |
| `session.idle` | Agent finished |
| `session.created` | New session created |
| `question.asked` | Agent asked a question |

### Question Routing

When a worker asks a question, the fleet intercepts the `question.asked`
SSE event, stores it, and forwards it to the parent as a message. The
parent answers via `fleet_answer_question`, which the fleet proxies back
to the worker's `POST /question/:id/reply`.

### Fleet `serverType`

The fleet has a `serverType` field that flows through every layer. The
spawn command, health URL, and config patching behaviour are keyed on
this value. Before this work, `"kon"` was wired in the type system but
marked as "Future" and never tested.

---

## 2. Kon Before This Work

Kon is a minimal Python coding agent library (~108 files, ~215 token
system prompt):

```
src/kon/
  loop.py          # Agent class -- async generator agentic loop
  turn.py          # Single LLM request/response streaming
  session.py       # JSONL append-only session persistence
  events.py        # 22 typed Event dataclasses
  config.py        # TOML config system
  core/types.py    # Pydantic message types
  core/compaction.py  # Context overflow summarization
  tools/           # 6 built-in tools (read, edit, write, bash, grep, find)
  llm/             # Provider abstraction (Anthropic, OpenAI, Copilot, etc.)
  ui/              # Textual TUI (terminal application)
```

**What kon already had:**

- Agentic loop (`Agent.run()` yields typed events via async generator)
- Session persistence (JSONL files)
- 6 built-in tools via `BaseTool[T]` (read, edit, write, bash, grep, find)
- Multi-provider LLM support (Anthropic, OpenAI, Copilot, local models)
- Context compaction (automatic summarization on overflow)
- Cooperative cancellation (`asyncio.Event`)
- Custom tool support (subclass `BaseTool[T]`)

**What kon did NOT have:**

- HTTP server mode (no REST API at all)
- SSE event streaming (events are Python async generators, not HTTP)
- MCP client or server support
- Question/ask tool (no way to pause and wait for external input)
- Permission system
- Any network-facing interface

### Gap Analysis

| Fleet Requires | Kon Had | Gap |
|----------------|---------|-----|
| Health endpoint | -- | Must build |
| Session CRUD over HTTP | `Session` class in library | Must expose via HTTP |
| Send message over HTTP | `Agent.run(query)` in library | Must wrap in HTTP handler |
| Global SSE stream | Async generator events | Must build multiplexed SSE + translate event format |
| Question tool + HTTP reply | -- | Must build tool + REST endpoint |
| Permission endpoints | -- | Stub (kon auto-grants everything) |
| Runtime model config | Model set at startup | Must build PATCH endpoint |
| Fleet tools (deliver, spawn, etc.) | -- | Must build as native Python tools |

---

## 3. What Was Added

### 3.1 Fleet-Compatible HTTP Server (`src/kon/server/`)

A new Starlette ASGI server that wraps kon's library into the HTTP API
the fleet expects. All routes match the fleet's URL patterns exactly.

```
src/kon/server/
  __init__.py        # Package docs
  __main__.py        # CLI: kon-serve --port N --workspace DIR --model PROVIDER/MODEL
  app.py             # Starlette app with all route handlers
  state.py           # ServerState: sessions, global event bus, provider lifecycle
  events.py          # Kon Event -> OpenCode-compatible SSE event translation
  question.py        # QuestionTool (BaseTool subclass, blocks on asyncio.Future)
  fleet_instructions.md  # System prompt for fleet-managed workers
```

**Routes implemented:**

| Route | Handler | Notes |
|-------|---------|-------|
| `GET /api/health` | `health()` | Returns `{"ok": true}` |
| `GET /global/health` | `global_health()` | OpenCode compat alias |
| `GET /session` | `list_sessions()` | Returns JSON array (not wrapped) |
| `POST /session` | `create_session()` | Returns `{"id": "..."}` |
| `POST /session/:id/message` | `send_message()` | Accepts OpenCode `parts` format and flat `message` |
| `GET /session/:id/message` | `get_messages()` | Messages in OpenCode-compatible schema |
| `POST /session/:id/abort` | `abort_session()` | Sets `cancel_event` |
| `GET /event` | `event_stream()` | Global SSE, OpenCode event types |
| `GET /question` | `list_questions()` | All pending questions across sessions |
| `POST /question/:id/reply` | `reply_question()` | Resolves the asyncio.Future |
| `GET /permission` | `list_permissions()` | Stub, returns `[]` |
| `POST /permission/:id/reply` | `reply_permission()` | Stub, returns OK |
| `PATCH /config` | `patch_config()` | Accepts `{"model": "provider/model"}` |
| `GET /session/status` | `session_status()` | Busy/idle per session |

**Event translation (`events.py`):**

Kon's 22 event dataclasses are translated to OpenCode's SSE naming:

| Kon Event | OpenCode SSE Type |
|-----------|-------------------|
| `TextStartEvent` | `message.part.updated` (type=text) |
| `TextDeltaEvent` | `message.part.delta` |
| `TextEndEvent` | `message.part.updated` |
| `ThinkingStartEvent` | `message.part.updated` (type=reasoning) |
| `ThinkingDeltaEvent` | `message.part.delta` |
| `ToolStartEvent` | `message.part.updated` (type=tool-invocation, state=partial-call) |
| `ToolEndEvent` | `message.part.updated` (state=call) |
| `ToolResultEvent` | `message.part.updated` (state=result) |
| `TurnEndEvent` | `message.updated` |
| `AgentEndEvent` | `session.idle` |
| Question tool invocation | `question.asked` |

**Global Event Bus:**

Unlike cobrowse_kon (per-session SSE), the server has a single `GlobalEventBus`
that multiplexes events from all sessions. Every SSE subscriber gets its own
`asyncio.Queue`. This matches the fleet relay's expectation of a single
`GET /event` stream per instance.

### 3.2 Question Tool (`question.py`)

A `BaseTool[QuestionParams]` subclass that:

1. Creates a `PendingQuestion` with an `asyncio.Future`
2. Emits a `question.asked` event on the global bus
3. Blocks the agent loop until the future resolves
4. Supports cooperative cancellation

The fleet relay intercepts `question.asked` from workers and routes the
question to the parent. The parent's answer arrives via
`POST /question/:id/reply`, which resolves the future.

### 3.3 Native Fleet Tools (`src/kon/tools/fleet.py`)

9 tools implemented as Python `BaseTool` subclasses that call the fleet
backend REST API via `aiohttp`. No MCP dependency.

| Tool | Fleet API Call |
|------|---------------|
| `fleet_deliver` | `POST /instance/{self}/deliverable` |
| `fleet_spawn_worker` | `POST /instance/spawn` |
| `fleet_kill_worker` | `DELETE /instance/{id}` |
| `fleet_list_instances` | `GET /instances` |
| `fleet_answer_question` | `POST /question/{id}/answer` |
| `fleet_get_pending_questions` | `GET /questions/pending` |
| `fleet_get_worker_deliverables` | `GET /instance/{id}/deliverables` |
| `fleet_get_worker_sessions` | `GET /instance/{id}/sessions` |
| `fleet_get_worker_messages` | `GET /instance/{id}/sessions/{sid}/messages` |

Tools are auto-loaded when `FLEET_API_URL` is set in the environment.
The `OPENCODE_FLEET_INSTANCE_ID` env var identifies the current instance
for `fleet_deliver`.

### 3.4 Fleet Worker Instructions (`fleet_instructions.md`)

Auto-loaded as the system prompt when `FLEET_API_URL` is set. Key rules:

- Every turn must end with `fleet_deliver` or `question`
- Messages come from a parent agent, not a human
- Can spawn sub-workers for parallel tasks
- Must verify file writes before delivering

### 3.5 CLI Entry Point

```bash
kon-serve --port 4097 --workspace /path/to/dir --model anthropic/claude-sonnet-4-6
```

Or: `python -m kon.server --port 4097`

Added as `[project.scripts] kon-serve = "kon.server.__main__:main"` in
`pyproject.toml`. Server dependencies (`starlette`, `uvicorn`,
`sse-starlette`) are optional under `[project.optional-dependencies] server`.

### 3.6 Fleet-Side Changes (opencode-fleet)

Minimal changes to `server/fleet.ts` and `server/index.ts`:

1. **Spawn command** -- `kon-serve --port N --workspace DIR` (was placeholder
   `uvicorn` command)
2. **Config via env vars** -- sets `KON_PROVIDER` and `KON_MODEL` from the
   fleet's `provider/model` format instead of patching `opencode.json`
3. **Session auto-creation** -- removed the `serverType === "opencode"` guard
   so kon workers also get sessions created automatically
4. **Model override** -- `spawn()` now accepts a `model` parameter, passed
   through from the fleet-spawn MCP tool and the HTTP API

---

## 4. Architecture Diagram

```
Fleet Backend (Bun, :5174)
  |
  |-- POST /api/instance/spawn {serverType:"kon", model:"..."}
  |      |
  |      +-- spawns: kon-serve --port 4097 --workspace <runtime_dir>
  |              |   env: FLEET_API_URL, KON_MODEL, OPENCODE_FLEET_INSTANCE_ID
  |              |
  |              +-- GET /api/health  (fleet polls until 200)
  |              +-- POST /session    (fleet creates session)
  |              +-- POST /session/:id/message  (fleet sends instructions)
  |              +-- GET /event  (fleet relay subscribes to SSE)
  |
  |-- Relay (SSE consumer per instance)
  |      +-- Intercepts question.asked from workers -> routes to parent
  |      +-- Broadcasts all other events to browser via WebSocket
  |
  +-- Browser (WebSocket, :5174)

Kon Worker (Python, :4097)
  |
  +-- Starlette ASGI server
  |     +-- /api/health, /global/health
  |     +-- /session, /session/:id/message, /session/:id/abort
  |     +-- /event (global SSE with OpenCode-compatible events)
  |     +-- /question, /question/:id/reply
  |     +-- /permission (stubs)
  |     +-- /config (PATCH to change model)
  |
  +-- Agent (kon async generator loop)
  |     +-- Provider (Anthropic / OpenAI / Copilot)
  |     +-- Built-in tools: read, edit, write, bash, grep, find
  |     +-- Question tool (blocks on asyncio.Future)
  |     +-- Fleet tools: fleet_deliver, fleet_spawn_worker, ...
  |
  +-- Global Event Bus (asyncio.Queue fan-out)
        +-- Translates Kon events -> OpenCode SSE format
        +-- Feeds all connected SSE subscribers
```
