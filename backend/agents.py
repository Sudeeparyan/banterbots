"""Two independent NFL agent services speaking official A2A 1.0 over HTTP."""
from __future__ import annotations

import argparse
import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypedDict

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.helpers.proto_helpers import new_data_part
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.routes.fastapi_routes import add_a2a_routes_to_fastapi
from a2a.server.tasks import DatabaseTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill, Part, Task, TaskState, TaskStatus
from fastapi import FastAPI
from google.protobuf.json_format import MessageToDict
from google.protobuf.timestamp_pb2 import Timestamp
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from sqlalchemy.ext.asyncio import create_async_engine

from backend.a2a_client import exchange
from backend.commentary import generate_turn, load_persona, validate_request
from backend.config import AGENT_A_URL, AGENT_B_URL, RUNTIME
from backend.contracts import AgentRequest, CommentaryTurn
from backend.security import redact, safe_error
from backend.speech import speak

logger = logging.getLogger("banterbots.agent")


class AgentState(TypedDict, total=False):
    request: dict[str, Any]
    own_turn: dict[str, Any]
    turns: list[dict[str, Any]]
    snapshot_hash: str
    stage: str


class CommentaryExecutor(AgentExecutor):
    def __init__(self, agent_id: str, peer_url: str):
        self.persona = load_persona(agent_id)
        self.peer_url = peer_url
        self.checkpointer: AsyncSqliteSaver | None = None

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if not context.task_id or not context.context_id or not context.message:
            raise ValueError("A2A request is missing task, context or message")
        task_id, context_id = context.task_id, context.context_id
        updater = TaskUpdater(event_queue, task_id, context_id)
        timestamp = Timestamp()
        timestamp.GetCurrentTime()
        await event_queue.enqueue_event(Task(id=task_id, context_id=context_id,
                                              status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED, timestamp=timestamp),
                                              history=[context.message]))
        await updater.start_work()
        request: AgentRequest | None = None

        async def emit(value: dict[str, Any]) -> None:
            data = value.setdefault("data", {})
            data.setdefault("task_id", task_id)
            data.setdefault("agent_id", self.persona.id)
            if request:
                data.setdefault("session_id", request.session_id)
                data.setdefault("epoch", request.epoch)
                data.setdefault("exchange_id", request.exchange_id)
                data.setdefault("event_id", request.snapshot.event.id)
                data.setdefault("snapshot_id", request.snapshot.id)
                data.setdefault("snapshot_hash", request.snapshot.hash)
            await updater.add_artifact(parts=[new_data_part(redact(value), media_type="application/json")],
                                       name=value["type"], last_chunk=True)

        async def trace(node: str, phase: str, **details: Any) -> None:
            await emit({"type": "trace", "data": {"node": node, "phase": phase, **details}})

        try:
            inputs = [MessageToDict(part.data) for part in context.message.parts if part.HasField("data")]
            if len(inputs) != 1:
                raise ValueError("Expected exactly one application/json AgentRequest data part")
            request = AgentRequest.model_validate(inputs[0])

            async def validate(state: AgentState) -> AgentState:
                await trace("validate_snapshot", "start")
                validate_request(request, self.persona.id)
                await trace("validate_snapshot", "complete", mode=request.mode, hop_budget=request.hop_budget,
                            facts=request.snapshot.model_dump())
                return {"snapshot_hash": request.snapshot.hash, "stage": "validated"}

            async def generate(state: AgentState) -> AgentState:
                started = time.perf_counter()
                await trace("generate", "start", persona=self.persona.name)
                turn = await generate_turn(request, self.persona, task_id, emit)
                await trace("generate", "complete", latency_ms=round((time.perf_counter() - started) * 1000, 1))
                await emit({"type": "turn.start", "data": {"turn_id": turn.id, "segment_id": turn.id,
                                                                "agent_id": turn.agent_id, "persona": turn.persona,
                                                                "team_id": turn.team_id, "voice": turn.voice,
                                                                "mode": turn.mode}})
                return {"own_turn": turn.model_dump(mode="json"), "stage": "generated"}

            async def speech(state: AgentState) -> AgentState:
                turn = CommentaryTurn.model_validate(state["own_turn"])
                if request.provider == "openai" and request.voice_enabled:
                    try:
                        turn = await speak(request, turn, emit)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        message = safe_error(exc)
                        observed_text = getattr(exc, "observed_text", "")
                        observed_chunks = getattr(exc, "audio_chunks", 0)
                        observed_samples = getattr(exc, "samples", 0)
                        turn = turn.model_copy(update={"spoken_text": observed_text if observed_chunks else None,
                                                       "speech_complete": False, "speech_error": message})
                        await emit({"type": "error", "data": {"stage": "speech", "message": message,
                                                                   "text_fallback": observed_chunks == 0,
                                                                   "observed_text": observed_text, "turn_id": turn.id}})
                        await emit({"type": "segment.sealed", "data": {"turn_id": turn.id, "segment_id": turn.id,
                                                                            "agent_id": turn.agent_id, "chunks": observed_chunks,
                                                                            "samples": observed_samples, "total_samples": observed_samples,
                                                                            "transcript": observed_text or turn.text,
                                                                            "text": observed_text or turn.text,
                                                                            "boundary": "speech_failed", "finalized": False,
                                                                            "discard_audio": True, "browser_speech": False,
                                                                            "speech_complete": False}})
                        if observed_chunks and not observed_text:
                            raise RuntimeError("Partial audio had no observed transcript; peer exchange cannot safely continue") from exc
                return {"own_turn": turn.model_dump(mode="json"), "stage": "spoken"}

            async def publish(state: AgentState) -> AgentState:
                turn = CommentaryTurn.model_validate(state["own_turn"])
                await emit({"type": "turn", "data": {"turn": turn.model_dump(mode="json")}})
                if request.provider == "demo" or not request.voice_enabled:
                    await emit({"type": "segment.sealed", "data": {"turn_id": turn.id, "segment_id": turn.id,
                                                                        "agent_id": turn.agent_id, "chunks": 0,
                                                                        "samples": 0, "total_samples": 0,
                                                                        "transcript": turn.text, "text": turn.text,
                                                                        "browser_speech": request.provider == "demo" and request.voice_enabled,
                                                                        "voice": turn.voice, "finalized": True,
                                                                        "boundary": "browser_speech" if request.voice_enabled else "text_only"}})
                return {"turns": [turn.model_dump(mode="json")], "stage": "published"}

            async def peer(state: AgentState) -> AgentState:
                await trace("peer_exchange", "start", source_agent=self.persona.id, target=self.peer_url)
                peer_request = request.model_copy(update={"mode": "reply", "hop_budget": 0,
                                                           "peer_utterance": CommentaryTurn.model_validate(state["own_turn"])})
                # Awaiting a real streaming HTTP call means the peer's observed speech
                # and task identifiers are forwarded to the coordinator, not invented.
                timeout = 60 if request.provider == "openai" else 15
                turns = await asyncio.wait_for(exchange(self.peer_url, peer_request, emit), timeout=timeout)
                await trace("peer_exchange", "complete", peer_tasks=[turn.task_id for turn in turns],
                            matching_snapshot=all(turn.snapshot_hash == request.snapshot.hash for turn in turns))
                return {"turns": state["turns"] + [turn.model_dump(mode="json") for turn in turns], "stage": "peer_completed"}

            async def finalize(state: AgentState) -> AgentState:
                await trace("persist", "complete", turn_count=len(state["turns"]),
                            checkpoint_thread=task_id)
                return {"stage": "completed"}

            def safe_node(handler):
                async def run(state):
                    try:
                        return await handler(state)
                    except Exception as exc:
                        # LangGraph records failed node errors in checkpoint writes.
                        # Sanitize before the exception reaches that persistence layer.
                        raise RuntimeError(safe_error(exc)) from None
                return run

            graph = StateGraph(AgentState)
            for name, handler in [("validate_snapshot", validate), ("generate", generate), ("speak", speech),
                                  ("publish", publish), ("peer_exchange", peer), ("persist", finalize)]:
                graph.add_node(name, safe_node(handler))
            graph.add_edge(START, "validate_snapshot")
            graph.add_edge("validate_snapshot", "generate")
            graph.add_edge("generate", "speak")
            graph.add_edge("speak", "publish")
            graph.add_conditional_edges("publish", lambda state: "peer_exchange" if request.mode == "exchange" else "persist")
            graph.add_edge("peer_exchange", "persist")
            graph.add_edge("persist", END)
            workflow = graph.compile(checkpointer=self.checkpointer)
            await workflow.ainvoke({"request": request.model_dump(mode="json"), "turns": []},
                                    config={"configurable": {"thread_id": task_id},
                                            "run_name": f"{self.persona.name}:{request.mode}",
                                            "metadata": {"session_id": request.session_id, "exchange_id": request.exchange_id,
                                                         "snapshot_hash": request.snapshot.hash, "a2a_task_id": task_id}})
            await updater.complete()
        except asyncio.CancelledError:
            # The official request handler invokes cancel() after cancelling execute.
            raise
        except Exception as exc:
            message = safe_error(exc)
            logger.error("agent_task_failed: %s", message,
                         extra={"agent_id": self.persona.id, "task_id": task_id, "error_type": type(exc).__name__})
            await emit({"type": "error", "data": {"stage": "agent", "message": message}})
            await updater.failed(updater.new_agent_message([Part(text=message)]))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        if context.task_id and context.context_id:
            await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()


def create_app(agent_id: str = "a", port: int | None = None, peer_url: str | None = None,
               runtime: Path | None = None) -> FastAPI:
    if agent_id not in {"a", "b"}:
        raise ValueError("Agent must be a or b")
    port = port or (8001 if agent_id == "a" else 8002)
    runtime = runtime or RUNTIME
    runtime.mkdir(parents=True, exist_ok=True)
    executor = CommentaryExecutor(agent_id, peer_url or (AGENT_B_URL if agent_id == "a" else AGENT_A_URL))
    engine = create_async_engine(f"sqlite+aiosqlite:///{(runtime / f'agent_{agent_id}_tasks.sqlite').as_posix()}")
    store = DatabaseTaskStore(engine)
    card = AgentCard(name=executor.persona.name, description=f"Original {executor.persona.allegiance}-team NFL commentator.",
                     version="1.0.0", capabilities=AgentCapabilities(streaming=True),
                     supported_interfaces=[AgentInterface(url=f"http://127.0.0.1:{port}/a2a/jsonrpc",
                                                            protocol_binding="JSONRPC", protocol_version="1.0")],
                     default_input_modes=["application/json"], default_output_modes=["application/json"],
                     skills=[AgentSkill(id="nfl-commentary", name="NFL play commentary", tags=["nfl", "commentary", "banter"],
                                        description="React to an immutable game snapshot, then engage an opposing agent over A2A.")])

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await store.initialize()
        async with AsyncSqliteSaver.from_conn_string(str(runtime / f"agent_{agent_id}_checkpoints.sqlite")) as saver:
            await saver.setup()
            executor.checkpointer = saver
            try:
                yield
            finally:
                await handler.aclose()
                executor.checkpointer = None
        await engine.dispose()

    app = FastAPI(title=f"BanterBots • {executor.persona.name}", lifespan=lifespan)
    app.state.executor = executor
    app.state.task_store = store
    app.state.agent_card = card
    handler = DefaultRequestHandler(agent_executor=executor, task_store=store, agent_card=card)
    add_a2a_routes_to_fastapi(app, agent_card_routes=create_agent_card_routes(card),
                             jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/a2a/jsonrpc"))

    @app.get("/health")
    async def health():
        return {"status": "ok", "agent_id": agent_id, "persona": executor.persona.model_dump(),
                "protocol": "A2A/1.0", "peer": executor.peer_url,
                "checkpoints": executor.checkpointer is not None}

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a BanterBots A2A commentator service")
    parser.add_argument("--agent", choices=["a", "b"], required=True)
    parser.add_argument("--port", type=int)
    arguments = parser.parse_args()
    import uvicorn
    uvicorn.run(create_app(arguments.agent, arguments.port), host="127.0.0.1",
                port=arguments.port or (8001 if arguments.agent == "a" else 8002))


if __name__ == "__main__":
    main()
