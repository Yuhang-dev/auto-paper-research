"""LangGraph model/tool loop with SQLite persistence and bounded LLM context."""

from __future__ import annotations

import json
import re
from types import TracebackType
from typing import Any, Dict, List, Optional, Sequence, Type

from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.utils import count_tokens_approximately, trim_messages
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.runtime import Runtime

from .config import HarnessSettings
from .memory import recall_notes, render_memory_context
from .persistence import HarnessPersistence
from .prompts import render_system_prompt
from .skill_registry import SkillRegistry
from .state import HarnessContext, HarnessState
from .tools import build_tools


THREAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")


def _message_text(message: AnyMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    return str(content)


def _last_human_text(messages: Sequence[AnyMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return _message_text(message)
    return ""


def _tool_result_failed(message: ToolMessage) -> bool:
    text = _message_text(message)
    try:
        payload = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("ok") is False


def build_graph(
    *,
    model: BaseChatModel,
    tools: Sequence[BaseTool],
    settings: HarnessSettings,
    persistence: HarnessPersistence,
):
    if persistence.checkpointer is None or persistence.store is None:
        raise RuntimeError(
            "Harness persistence must be open before compiling the graph"
        )
    model_with_tools = model.bind_tools(list(tools))

    def prepare_turn(state: HarnessState) -> Dict[str, Any]:
        return {
            "iteration": 0,
            "tool_failures": 0,
            "stop_reason": "",
            "context_message_count": 0,
            "recalled_memory_count": 0,
            "last_tools": [],
        }

    def call_model(
        state: HarnessState,
        runtime: Runtime[HarnessContext],
    ) -> Dict[str, Any]:
        messages = list(state.get("messages") or [])
        trimmed = trim_messages(
            messages,
            strategy="last",
            token_counter=count_tokens_approximately,
            max_tokens=settings.context_token_budget,
            start_on="human",
            include_system=False,
            allow_partial=True,
        )
        if not trimmed and messages:
            trimmed = [messages[-1]]
        memory_records = []
        if runtime.store is not None:
            memory_records = recall_notes(
                runtime.store,
                runtime.context.workspace_id,
                query=_last_human_text(trimmed),
                limit=8,
            )
        system_message = SystemMessage(
            content=render_system_prompt(
                workspace_id=runtime.context.workspace_id,
                allow_network=runtime.context.allow_network,
                memory_context=render_memory_context(memory_records),
            )
        )
        iteration = int(state.get("iteration") or 0)
        if iteration >= settings.max_tool_iterations:
            final_instruction = SystemMessage(
                content=(
                    "The tool-iteration limit has been reached. Synthesize the available "
                    "evidence now. Do not request another tool call."
                )
            )
            raw_response = model.invoke([system_message, final_instruction, *trimmed])
            response = AIMessage(content=raw_response.content)
            stop_reason = "max-tool-iterations"
        else:
            response = model_with_tools.invoke([system_message, *trimmed])
            stop_reason = ""
        return {
            "messages": [response],
            "context_message_count": len(trimmed),
            "recalled_memory_count": len(memory_records),
            "stop_reason": stop_reason,
        }

    def observe_tools(state: HarnessState) -> Dict[str, Any]:
        recent_tools: List[ToolMessage] = []
        for message in reversed(list(state.get("messages") or [])):
            if isinstance(message, ToolMessage):
                recent_tools.append(message)
                continue
            if isinstance(message, AIMessage):
                break
        recent_tools.reverse()
        failures = sum(_tool_result_failed(message) for message in recent_tools)
        return {
            "iteration": int(state.get("iteration") or 0) + 1,
            "tool_failures": int(state.get("tool_failures") or 0) + failures,
            "last_tools": [str(message.name or "unknown") for message in recent_tools],
        }

    builder = StateGraph(HarnessState, context_schema=HarnessContext)
    builder.add_node("prepare", prepare_turn)
    builder.add_node("agent", call_model)
    builder.add_node(
        "tools",
        ToolNode(
            list(tools),
            handle_tool_errors=lambda exc: json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            ),
        ),
    )
    builder.add_node("observe", observe_tools)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "agent")
    builder.add_conditional_edges(
        "agent",
        tools_condition,
        {"tools": "tools", "__end__": END},
    )
    builder.add_edge("tools", "observe")
    builder.add_edge("observe", "agent")
    return builder.compile(
        checkpointer=persistence.checkpointer,
        store=persistence.store,
        name="research-harness",
    )


def load_chat_model(settings: HarnessSettings) -> BaseChatModel:
    if not settings.model:
        raise ValueError(
            "No model configured. Set HARNESS_MODEL or pass --model, for example "
            "openai:deepseek-v4-flash."
        )
    return init_chat_model(settings.model)


class ResearchHarness:
    """Lifecycle wrapper around the compiled graph and its SQLite connections."""

    def __init__(
        self,
        settings: Optional[HarnessSettings] = None,
        *,
        model: Optional[BaseChatModel] = None,
    ):
        self.settings = settings or HarnessSettings.from_env()
        self.settings.validate()
        self._provided_model = model
        self.skill_registry = SkillRegistry(self.settings.skills_root)
        self.persistence = HarnessPersistence(self.settings)
        self.tools: List[BaseTool] = []
        self.graph = None

    def open(self) -> "ResearchHarness":
        if self.graph is not None:
            return self
        self.persistence.open()
        try:
            self.tools = build_tools(self.settings)
            model = self._provided_model or load_chat_model(self.settings)
            self.graph = build_graph(
                model=model,
                tools=self.tools,
                settings=self.settings,
                persistence=self.persistence,
            )
        except Exception:
            self.close()
            raise
        return self

    def close(self) -> None:
        self.graph = None
        self.tools = []
        self.persistence.close()

    def invoke(
        self,
        task: str,
        *,
        thread_id: str,
        workspace_id: Optional[str] = None,
        allow_network: bool = False,
    ) -> Dict[str, Any]:
        if self.graph is None:
            raise RuntimeError("ResearchHarness is not open")
        clean_task = task.strip()
        if not clean_task:
            raise ValueError("task cannot be empty")
        if not THREAD_ID_PATTERN.fullmatch(thread_id):
            raise ValueError(
                "thread_id must be 1-200 ASCII letters, digits, dots, colons, underscores, or hyphens"
            )
        context = HarnessContext(
            workspace_id=workspace_id or self.settings.workspace_id,
            allow_network=allow_network,
        )
        config = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": self.settings.max_tool_iterations * 4 + 8,
        }
        return self.graph.invoke(
            {"messages": [HumanMessage(content=clean_task)]},
            config,
            context=context,
        )

    def get_state(self, thread_id: str):
        if self.graph is None:
            raise RuntimeError("ResearchHarness is not open")
        return self.graph.get_state({"configurable": {"thread_id": thread_id}})

    def __enter__(self) -> "ResearchHarness":
        return self.open()

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        self.close()
