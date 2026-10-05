"""Official A2A 1.0 streaming client shared by coordinator and peers."""
from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from a2a.client import ClientConfig, create_client
from a2a.helpers.proto_helpers import new_data_part
from a2a.types import CancelTaskRequest, Message, Role, SendMessageRequest, TaskState
from google.protobuf.json_format import MessageToDict

from backend.contracts import AgentRequest, CommentaryTurn, uid

Emit = Callable[[dict[str, Any]], Awaitable[None]]


async def exchange(url: str, payload: AgentRequest, emit: Emit,
                   cancel_event: asyncio.Event | None = None) -> list[CommentaryTurn]:
    """Return completed turns while forwarding every ordered streaming artifact.

    Cancellation closes the HTTP stream and explicitly cancels the remote SDK
    task, including nested peer work. No direct Python agent calls are used.
    """
    started = time.perf_counter()
    task_id: str | None = None
    client = None
    remote_terminal = False
    turns: list[CommentaryTurn] = []
    seen_artifacts: set[str] = set()
    received_ids: set[str] = set()
    task_known = asyncio.Event()

    async def artifact(value: Any) -> None:
        if value.artifact_id in seen_artifacts:
            return
        seen_artifacts.add(value.artifact_id)
        for part in value.parts:
            if not part.HasField("data"):
                continue
            app_event = MessageToDict(part.data)
            if not isinstance(app_event.get("type"), str) or not isinstance(app_event.get("data"), dict):
                raise ValueError("Invalid commentary artifact received from A2A agent")
            app_event["data"].setdefault("task_id", task_id)
            app_event["data"].setdefault("exchange_id", payload.exchange_id)
            app_event["data"].setdefault("snapshot_hash", payload.snapshot.hash)
            if app_event["type"] == "turn":
                turn = CommentaryTurn.model_validate(app_event["data"]["turn"])
                if turn.snapshot_hash != payload.snapshot.hash or turn.exchange_id != payload.exchange_id:
                    raise ValueError("A2A agent returned commentary for a different immutable snapshot")
                if turn.id not in received_ids:
                    received_ids.add(turn.id)
                    turns.append(turn)
            await emit(app_event)

    async with httpx.AsyncClient(timeout=httpx.Timeout(65, connect=5), trust_env=False) as http:
        try:
            client = await create_client(url.rstrip("/"), client_config=ClientConfig(
                streaming=True, httpx_client=http, accepted_output_modes=["application/json"]))
            await emit({"type": "trace", "data": {"node": "a2a.send", "phase": "start", "target": url,
                                                      "protocol": "A2A/1.0", "exchange_id": payload.exchange_id,
                                                      "snapshot_hash": payload.snapshot.hash, "mode": payload.mode,
                                                      "hop_budget": payload.hop_budget,
                                                      "request": payload.model_dump(mode="json")}})
            message = Message(message_id=uid(), role=Role.ROLE_USER,
                              context_id=payload.session_id,
                              parts=[new_data_part(payload.model_dump(mode="json"), media_type="application/json")])

            async def consume() -> None:
                nonlocal task_id, remote_terminal
                failed_state = None
                async for event in client.send_message(SendMessageRequest(message=message)):
                    if event.HasField("task"):
                        task_id = event.task.id
                        task_known.set()
                        for value in event.task.artifacts:
                            await artifact(value)
                        state = event.task.status.state
                    elif event.HasField("artifact_update"):
                        task_id = event.artifact_update.task_id
                        task_known.set()
                        await artifact(event.artifact_update.artifact)
                        continue
                    elif event.HasField("status_update"):
                        task_id = event.status_update.task_id
                        task_known.set()
                        state = event.status_update.status.state
                    else:
                        continue
                    await emit({"type": "trace", "data": {"node": "a2a.status", "task_id": task_id,
                                                              "state": TaskState.Name(state).removeprefix("TASK_STATE_").lower(),
                                                              "target": url, "snapshot_hash": payload.snapshot.hash,
                                                              "exchange_id": payload.exchange_id}})
                    if state in {TaskState.TASK_STATE_CANCELED, TaskState.TASK_STATE_FAILED,
                                 TaskState.TASK_STATE_REJECTED}:
                        remote_terminal = True
                        failed_state = state
                    if state == TaskState.TASK_STATE_COMPLETED:
                        remote_terminal = True
                if failed_state is not None:
                    raise RuntimeError(f"Agent task {task_id} ended with {TaskState.Name(failed_state)}")

            consumer = asyncio.create_task(consume())
            waiter = asyncio.create_task(cancel_event.wait()) if cancel_event else None

            async def cancel_connected() -> None:
                nonlocal remote_terminal
                # A service can start executing before its submitted event has
                # reached this socket. Keep the reader alive until its task ID
                # arrives so even cancellation in that window reaches the task.
                if not task_id and not consumer.done():
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(task_known.wait(), 1)
                if task_id and not remote_terminal:
                    try:
                        await asyncio.wait_for(client.cancel_task(CancelTaskRequest(id=task_id)), 3)
                        remote_terminal = True
                    except Exception:
                        if not remote_terminal:
                            raise

            try:
                if waiter:
                    done, _ = await asyncio.wait({consumer, waiter}, return_when=asyncio.FIRST_COMPLETED)
                    if waiter in done and cancel_event.is_set():
                        # Cancel through the protocol while the stream is still
                        # connected, so the SDK can deliver its terminal event.
                        await cancel_connected()
                        try:
                            await asyncio.wait_for(asyncio.shield(consumer), 3)
                        except (Exception, asyncio.CancelledError):
                            consumer.cancel()
                        await asyncio.gather(consumer, return_exceptions=True)
                        raise asyncio.CancelledError
                # Directly awaiting consumer would propagate parent Task.cancel
                # to the reader before CancelTask can identify the remote task.
                await asyncio.shield(consumer)
            except asyncio.CancelledError:
                await asyncio.shield(cancel_connected())
                raise
            finally:
                if not consumer.done():
                    consumer.cancel()
                if waiter:
                    waiter.cancel()
                await asyncio.gather(consumer, *([waiter] if waiter else []), return_exceptions=True)
            if not remote_terminal:
                raise RuntimeError("A2A stream ended without a completed task")
            if not turns:
                raise RuntimeError("A2A task completed without a commentary turn")
            expected_count = 2 if payload.mode == "exchange" else 1
            if len(turns) != expected_count:
                raise ValueError(f"A2A {payload.mode} must return exactly {expected_count} commentary turn(s)")
            if payload.mode == "exchange":
                if turns[0].agent_id == turns[1].agent_id or turns[1].reply_to_turn_id != turns[0].id:
                    raise ValueError("A2A exchange did not contain an opposing reply to the lead turn")
            elif (payload.peer_utterance is None or turns[0].agent_id == payload.peer_utterance.agent_id
                  or turns[0].reply_to_turn_id != payload.peer_utterance.id):
                raise ValueError("A2A reply did not match its supplied peer utterance")
            await emit({"type": "trace", "data": {"node": "a2a.receive", "phase": "complete", "task_id": task_id,
                                                      "target": url, "turn_count": len(turns),
                                                      "exchange_id": payload.exchange_id,
                                                      "snapshot_hash": payload.snapshot.hash,
                                                      "latency_ms": round((time.perf_counter() - started) * 1000, 1)}})
            return turns
        finally:
            if client is not None:
                if task_id and not remote_terminal:
                    with contextlib.suppress(Exception):
                        await asyncio.shield(asyncio.wait_for(client.cancel_task(CancelTaskRequest(id=task_id)), 3))
                await client.close()
