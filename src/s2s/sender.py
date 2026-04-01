from __future__ import annotations

from fastapi import WebSocket
from starlette.websockets import WebSocketState

from src.s2s.events import AnyEvent, event_to_dict


async def send_event(websocket: WebSocket, event: AnyEvent) -> None:
    if websocket.client_state != WebSocketState.CONNECTED:
        return
    await websocket.send_json(event_to_dict(event))