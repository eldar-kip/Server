import asyncio
import json
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status
import redis.asyncio as aioredis
from sqlalchemy import select

from src.server.api.deps import authenticate_ws_token
from src.server.core.config import settings
from src.server.core.database import AsyncSessionLocal
from src.server.models.procurement import ProcurementSession, TaskStatusEnum
from src.server.models.user import RoleEnum

router = APIRouter(tags=["WebSocket Realtime Notifications"])

TERMINAL_STATUSES = {
    TaskStatusEnum.COMPLETED.value,
    TaskStatusEnum.FAILED.value,
    TaskStatusEnum.APPROVED.value,
}


@router.websocket("/api/v1/ws/procurement/{session_id}")
async def websocket_procurement_progress(
    websocket: WebSocket,
    session_id: int,
    token: str | None = Query(default=None),
) -> None:
    """
    WebSocket-эндпоинт трансляции прогресса инференса.
    Слушает канал Redis Pub/Sub 'session:{session_id}:progress'.
    Защита от Race Condition: сначала оформляется подписка на Redis, затем читается статус из БД.
    """
    await websocket.accept()

    async with AsyncSessionLocal() as auth_db:
        user = await authenticate_ws_token(token, auth_db)
        if user is None:
            await websocket.send_text(
                json.dumps(
                    {"error": "Требуется валидный JWT-токен в параметре ?token="},
                    ensure_ascii=False,
                )
            )
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

    redis_conn = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    pubsub = redis_conn.pubsub()
    channel_name = f"session:{session_id}:progress"

    try:
        # 1. СНАЧАЛА подписываемся на канал Redis, чтобы не потерять финальное событие
        await pubsub.subscribe(channel_name)

        # 2. ЗАТЕМ проверяем существование, права доступа и текущий статус сессии в БД
        async with AsyncSessionLocal() as db:
            stmt = select(ProcurementSession).where(ProcurementSession.id == session_id)
            result = await db.execute(stmt)
            session = result.scalar_one_or_none()

            if not session:
                await websocket.send_text(
                    json.dumps({"error": f"Сессия {session_id} не найдена"}, ensure_ascii=False)
                )
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return

            if user.role != RoleEnum.ADMIN and session.user_id != user.id:
                await websocket.send_text(
                    json.dumps(
                        {"error": "Доступ к данной закупочной сессии запрещен"},
                        ensure_ascii=False,
                    )
                )
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return

            status_str = (
                session.status.value if hasattr(session.status, "value") else str(session.status)
            )
            if status_str in TERMINAL_STATUSES:
                finished_payload = {
                    "session_id": session.id,
                    "progress_percent": 100,
                    "status": status_str,
                    "matched_items": session.matched_items,
                    "total_items": session.total_items,
                    "message": f"Обработано {session.total_items} из {session.total_items} позиций",
                }
                await websocket.send_text(json.dumps(finished_payload, ensure_ascii=False))
                return

        # 3. Слушаем события прогресса до перехода в терминальный статус
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message and message.get("type") == "message":
                raw_data = message["data"]
                await websocket.send_text(raw_data)

                try:
                    parsed = json.loads(raw_data)
                except (TypeError, json.JSONDecodeError):
                    parsed = {}

                if parsed.get("status") in TERMINAL_STATUSES:
                    break

            await asyncio.sleep(0.01)

    except WebSocketDisconnect:
        pass
    finally:
        try:
            await pubsub.unsubscribe(channel_name)
            await pubsub.close()
        except Exception:
            pass
        await redis_conn.aclose()
        try:
            await websocket.close()
        except RuntimeError:
            pass
