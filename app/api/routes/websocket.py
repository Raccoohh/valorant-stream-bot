"""WebSocket endpoint for the OBS overlay.

The overlay (a Browser Source in OBS) connects to /ws/overlay and
receives every StreamEvent published to Redis as JSON. The fan-out
task lives in main.py's lifespan; this module only owns the
connection registry.
"""

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.events import StreamEvent

logger = logging.getLogger(__name__)

router = APIRouter()


class ConnectionManager:
    """Tracks live overlay connections and broadcasts events to them."""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)
        logger.info("Overlay connected (%d total)", len(self._connections))

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)
        logger.info("Overlay disconnected (%d total)", len(self._connections))

    async def broadcast(self, event: StreamEvent) -> None:
        message = event.model_dump_json()
        dead: list[WebSocket] = []
        for ws in self._connections:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


# Module-level singleton: the lifespan fan-out task and the route
# handler both import this same instance.
manager = ConnectionManager()


@router.websocket("/ws/overlay")
async def overlay_ws(websocket: WebSocket) -> None:
    await manager.connect(websocket)
    try:
        # The overlay is receive-only, but we must keep reading so the
        # connection stays alive and disconnects are detected.
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
