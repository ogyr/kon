"""Question tool -- allows the agent to ask structured questions.

When the agent calls this tool, execution blocks on an asyncio.Future
until the answer arrives via the REST API (either from the fleet's
question-routing mechanism or a human user).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from kon.core.types import ToolResult
from kon.tools.base import BaseTool


# -- Data models -----------------------------------------------------------

class QuestionOption(BaseModel):
    label: str = Field(description="Short display text (1-5 words)")
    description: str = Field(description="Explanation of this choice")


class QuestionItem(BaseModel):
    question: str = Field(description="The complete question to ask")
    header: str = Field(description="Short label (max 30 chars)")
    options: list[QuestionOption] = Field(description="Available choices")
    multiple: bool = Field(default=False, description="Allow selecting more than one choice")


class QuestionParams(BaseModel):
    questions: list[QuestionItem] = Field(description="Questions to ask")


# -- Pending question state ------------------------------------------------

@dataclass
class PendingQuestion:
    request_id: str
    session_id: str
    questions: list[dict[str, Any]]
    future: asyncio.Future = field(
        default_factory=lambda: asyncio.get_event_loop().create_future()
    )


# -- Tool ------------------------------------------------------------------

class QuestionTool(BaseTool[QuestionParams]):
    """Ask one or more structured questions and wait for answers."""

    name = "question"
    params = QuestionParams
    description = (
        "Ask the user a question with structured options. "
        "Use this when you need user input, preferences, or decisions. "
        "Returns the selected answers as a list."
    )

    def __init__(self, on_question: Any = None) -> None:
        self._on_question = on_question

    def set_callback(self, on_question: Any) -> None:
        self._on_question = on_question

    async def execute(
        self,
        params: QuestionParams,
        cancel_event: asyncio.Event | None = None,
    ) -> ToolResult:
        if self._on_question is None:
            return ToolResult(
                success=False,
                result="Question tool not connected to a session.",
            )

        request_id = str(uuid.uuid4())
        questions_data = [q.model_dump() for q in params.questions]

        loop = asyncio.get_running_loop()
        pending = PendingQuestion(
            request_id=request_id,
            session_id="",
            questions=questions_data,
            future=loop.create_future(),
        )

        await self._on_question(pending)

        try:
            if cancel_event:
                cancel_task = asyncio.create_task(cancel_event.wait())
                answer_task = asyncio.ensure_future(pending.future)
                done, pending_tasks = await asyncio.wait(
                    {cancel_task, answer_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending_tasks:
                    t.cancel()
                if cancel_task in done:
                    return ToolResult(success=False, result="Question was interrupted.")
                result = answer_task.result()
            else:
                result = await pending.future
        except asyncio.CancelledError:
            return ToolResult(success=False, result="Question was cancelled.")

        if result is None:
            return ToolResult(
                success=True,
                result="Question dismissed without answer.",
                display="Question dismissed",
            )

        answers_text = []
        for i, q in enumerate(params.questions):
            if i < len(result):
                selected = result[i]
                answers_text.append(f"Q: {q.header}\nA: {', '.join(selected)}")
            else:
                answers_text.append(f"Q: {q.header}\nA: (no answer)")

        formatted = "\n\n".join(answers_text)
        return ToolResult(success=True, result=formatted, display=formatted)

    def format_call(self, params: QuestionParams) -> str:
        headers = [q.header for q in params.questions]
        return f"Asking: {', '.join(headers)}"
