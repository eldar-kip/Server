import asyncio
import json
import os
import redis.asyncio as aioredis
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from src.server.core.database import AsyncSessionLocal
from src.server.models.procurement import ProcurementSession

router = APIRouter(tags=["WebSocket Realtime Notifications"])

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")


@router.websocket("/api/v1/ws/procurement/{session_id}")
async def websocket_procurement_progress(websocket: WebSocket, session_id: int):
    """
    WebSocket-эндпоинт трансляции прогресса инференса.
    Слушает канал Redis Pub/Sub 'session:{session_id}:progress'.
    Защищен от Race Condition чтением статуса из БД.
    """
    await websocket.accept()

    async with AsyncSessionLocal() as db:
        stmt = select(ProcurementSession).where(ProcurementSession.id == session_id)
        result = await db.execute(stmt)
        session = result.scalar_one_or_none()

        if not session:
            await websocket.send_text(
                json.dumps({"error": f"Сессия {session_id} не найдена"}, ensure_ascii=False)
            )
            await websocket.close()
            return

        if session.status in ("COMPLETED", "FAILED"):
            status_str = session.status.value if hasattr(session.status, "value") else str(session.status)
            finished_payload = {
                "session_id": session.id,
                "progress_percent": 100,
                "status": status_str,
                "matched_items": session.matched_items,
                "total_items": session.total_items,
                "message": f"Обработано {session.matched_items} из {session.total_items} позиций",
            }
            await websocket.send_text(json.dumps(finished_payload, ensure_ascii=False))
            await websocket.close()
            return

    redis_conn = aioredis.from_url(REDIS_URL, decode_responses=True)
    pubsub = redis_conn.pubsub()
    channel_name = f"session:{session_id}:progress"
    await pubsub.subscribe(channel_name)

    try:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message and message.get("type") == "message":
                data = message["data"]
                await websocket.send_text(data)

                if '"status": "COMPLETED"' in data or '"status": "FAILED"' in data:
                    break

            await asyncio.sleep(0.01)

    except WebSocketDisconnect:
        pass
    finally:
        await pubsub.unsubscribe(channel_name)
        await pubsub.close()
        await redis_conn.aclose()
        try:
            await websocket.close()
        except RuntimeError:
            pass