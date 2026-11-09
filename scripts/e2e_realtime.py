import asyncio
import io
import json
import os
import sys

import httpx
import websockets

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

BASE_HTTP = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")
BASE_WS = os.getenv("WS_BASE_URL", "ws://127.0.0.1:8000")
EMAIL = os.getenv("E2E_EMAIL", "buyer@taires.com")
PASSWORD = os.getenv("E2E_PASSWORD", "buyer123")


async def get_auth_token(client: httpx.AsyncClient) -> str:
    """Регистрирует (при необходимости) тестового закупщика и получает JWT-токен."""
    await client.post(
        f"{BASE_HTTP}/auth/register",
        json={"email": EMAIL, "password": PASSWORD},
    )
    resp = await client.post(
        f"{BASE_HTTP}/auth/login",
        json={"email": EMAIL, "password": PASSWORD},
    )
    resp.raise_for_status()
    return str(resp.json()["access_token"])


async def upload_file(client: httpx.AsyncClient, token: str) -> int:
    csv_content = (
        "raw_name;price;quantity;article\n"
        "Pirelli P Zero 225/45 R17 91V;12500;4;PIR-123\n"
        "Michelin Pilot Sport 4 205/55 R16;9800;8;MICH-456\n"
        "Nokian Hakkapeliitta 10 215/60 R16;11200;2;NOK-789\n"
        "Bridgestone Turanza T005 235/50 R18;14300;6;BR-001\n"
    )
    file_bytes = io.BytesIO(csv_content.encode("utf-8"))
    response = await client.post(
        f"{BASE_HTTP}/api/v1/procurement/upload",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("test_price.csv", file_bytes, "text/csv")},
        timeout=15.0,
    )
    response.raise_for_status()
    data = response.json()
    print(f"[API] Создана сессия: ID={data['session_id']}, позиций={data['total_items']}")
    return int(data["session_id"])


async def listen_ws(session_id: int, token: str) -> None:
    uri = f"{BASE_WS}/api/v1/ws/procurement/{session_id}?token={token}"
    print(f"[WS] Подключение к {uri}")
    async with websockets.connect(uri, proxy=None) as ws:
        while True:
            msg = await ws.recv()
            data = json.loads(msg)
            print(
                f"[WS] Прогресс: {data.get('progress_percent')}% | "
                f"Статус: {data.get('status')} | "
                f"AUTO: {data.get('matched_items')}/{data.get('total_items')} | "
                f"{data.get('message', '')}"
            )
            if data.get("status") in ("COMPLETED", "FAILED", "APPROVED"):
                print("[WS] Обработка завершена.")
                break


async def main() -> None:
    async with httpx.AsyncClient(trust_env=False, timeout=20.0) as client:
        token = await get_auth_token(client)
        session_id = await upload_file(client, token)
    await listen_ws(session_id, token)


if __name__ == "__main__":
    asyncio.run(main())
