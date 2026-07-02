"""WebSocket endpoint streaming per-conversation step-update events."""

from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(tags=["ws"])


@router.websocket("/ws/{conversation_id}")
async def conversation_events(websocket: WebSocket, conversation_id: str) -> None:
    """Forward Redis pub/sub events for ``conversation_id`` to the client."""
    await websocket.accept()
    redis_async = websocket.app.state.redis
    channel = f"conv:{conversation_id}:events"
    pubsub = redis_async.pubsub()
    await pubsub.subscribe(channel)
    try:
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=1.0
            )
            if message is None:
                continue
            data = message["data"]
            if not isinstance(data, str):
                data = data.decode("utf-8")
            await websocket.send_text(data)
    except WebSocketDisconnect:
        pass
    finally:
        try:
            await pubsub.unsubscribe(channel)
        finally:
            await pubsub.aclose()
