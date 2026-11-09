import json
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.server.main import app
from src.server.models import ProcurementSession, TaskStatusEnum, User


async def test_auth_register_login_and_swagger_token(client: AsyncClient) -> None:
    reg = await client.post(
        "/auth/register",
        json={"email": "newbuyer@taires.com", "password": "secretpassword"},
    )
    assert reg.status_code == 201
    assert reg.json()["email"] == "newbuyer@taires.com"

    # JSON login
    login_resp = await client.post(
        "/auth/login",
        json={"email": "newbuyer@taires.com", "password": "secretpassword"},
    )
    assert login_resp.status_code == 200
    assert "access_token" in login_resp.json()

    # Swagger OAuth2 form login (/auth/token)
    form_resp = await client.post(
        "/auth/token",
        data={"username": "newbuyer@taires.com", "password": "secretpassword"},
    )
    assert form_resp.status_code == 200
    assert "access_token" in form_resp.json()


async def test_sessions_route_order_auth_and_ownership(
    client: AsyncClient,
    db_session: AsyncSession,
    buyer_user: User,
    other_buyer_user: User,
    buyer_token: str,
    other_buyer_token: str,
    admin_token: str,
) -> None:
    # Без токена эндпоинт закрыт (401)
    unauth = await client.get("/api/v1/procurement/sessions")
    assert unauth.status_code == 401

    s1 = ProcurementSession(
        user_id=buyer_user.id,
        title="Сессия закупщика 1",
        status=TaskStatusEnum.PENDING,
        total_items=2,
    )
    s2 = ProcurementSession(
        user_id=other_buyer_user.id,
        title="Сессия закупщика 2",
        status=TaskStatusEnum.COMPLETED,
        total_items=5,
    )
    db_session.add_all([s1, s2])
    await db_session.commit()
    await db_session.refresh(s1)
    await db_session.refresh(s2)

    # Запрос /sessions БЕЗ завершающего слэша не перехватывается /{session_id} -> 200 OK
    res_buyer = await client.get(
        "/api/v1/procurement/sessions",
        headers={"Authorization": f"Bearer {buyer_token}"},
    )
    assert res_buyer.status_code == 200
    buyer_list = res_buyer.json()
    assert len(buyer_list) == 1
    assert buyer_list[0]["id"] == s1.id
    assert buyer_list[0]["status"] == "PENDING"

    # Чужая сессия недоступна другому закупщику (403)
    forbidden = await client.get(
        f"/api/v1/procurement/{s1.id}",
        headers={"Authorization": f"Bearer {other_buyer_token}"},
    )
    assert forbidden.status_code == 403

    # Admin видит все сессии
    res_admin = await client.get(
        "/api/v1/procurement/sessions",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res_admin.status_code == 200
    assert len(res_admin.json()) == 2


async def test_websocket_auth_and_subscribe_before_db_check(
    db_session_factory: async_sessionmaker[AsyncSession],
    buyer_user: User,
    buyer_token: str,
) -> None:
    async with db_session_factory() as db:
        sess = ProcurementSession(
            user_id=buyer_user.id,
            title="WS Session",
            status=TaskStatusEnum.PROCESSING,
            total_items=10,
            matched_items=0,
        )
        db.add(sess)
        await db.commit()
        await db.refresh(sess)
        session_id = sess.id

    events_order: list[str] = []

    fake_pubsub = MagicMock()

    async def _subscribe(channel: str) -> None:
        events_order.append(f"subscribe:{channel}")

    messages_iter = iter(
        [
            {
                "type": "message",
                "data": json.dumps(
                    {
                        "session_id": session_id,
                        "progress_percent": 100,
                        "status": "COMPLETED",
                        "matched_items": 8,
                        "total_items": 10,
                        "message": "Обработано 10 из 10 позиций",
                    },
                    ensure_ascii=False,
                ),
            }
        ]
    )

    async def _get_message(**kwargs: object) -> dict[str, str] | None:
        return next(messages_iter, None)

    fake_pubsub.subscribe = AsyncMock(side_effect=_subscribe)
    fake_pubsub.get_message = AsyncMock(side_effect=_get_message)
    fake_pubsub.unsubscribe = AsyncMock()
    fake_pubsub.close = AsyncMock()

    fake_redis = MagicMock()
    fake_redis.pubsub.return_value = fake_pubsub
    fake_redis.aclose = AsyncMock()

    orig_session_local = db_session_factory

    def _tracked_session_local() -> AsyncSession:
        events_order.append("db_open")
        return orig_session_local()

    with (
        patch("src.server.api.v1.websocket.AsyncSessionLocal", side_effect=_tracked_session_local),
        patch("src.server.api.v1.websocket.aioredis.from_url", return_value=fake_redis),
    ):
        sync_client = TestClient(app)

        # 1. Без токена возвращается ошибка авторизации
        with sync_client.websocket_connect(f"/api/v1/ws/procurement/{session_id}") as ws_no_auth:
            err_msg = json.loads(ws_no_auth.receive_text())
            assert "error" in err_msg

        events_order.clear()
        # 2. С валидным токеном подписка на Redis выполняется ДО чтения статуса сессии из БД
        with sync_client.websocket_connect(
            f"/api/v1/ws/procurement/{session_id}?token={buyer_token}"
        ) as ws_ok:
            payload = json.loads(ws_ok.receive_text())
            assert payload["status"] == "COMPLETED"
            assert payload["matched_items"] == 8

    # events_order[0] — проверка пользователя, затем СНАЧАЛА subscribe, ПОТОМ чтение статуса сессии
    sub_idx = events_order.index(f"subscribe:session:{session_id}:progress")
    session_db_idx = [i for i, e in enumerate(events_order) if e == "db_open"][-1]
    assert sub_idx < session_db_idx
