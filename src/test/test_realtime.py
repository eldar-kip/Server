import asyncio
import io
import json
import sys

import httpx
import websockets

# Fix encoding for Windows console
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

API_URL = "http://localhost:8000/api/v1/procurement/upload"
WS_BASE_URL = "ws://localhost:8000/api/v1/ws/procurement"


async def upload_file() -> int:
    csv_content = (
        "raw_name;price;quantity;article\n"
        "Pirelli P Zero 225/45 R17 91V;12500;4;PIR-123\n"
        "Michelin Pilot Sport 4 205/55 R16;9800;8;MICH-456\n"
        "Nokian Hakkapeliitta 10 215/60 R16;11200;2;NOK-789\n"
        "Bridgestone Turanza T005 235/50 R18;14300;6;BR-001\n"
    )
    file_bytes = io.BytesIO(csv_content.encode("utf-8"))

    async with httpx.AsyncClient() as client:
        response = await client.post(
            API_URL,
            files={"file": ("test_price.csv", file_bytes, "text/csv")},
            timeout=10.0,
        )
        data = response.json()
        print(f"[API] Session created: ID={data['session_id']}, rows={data['total_items']}")
        return data["session_id"]


async def listen_ws(session_id: int):
    uri = f"{WS_BASE_URL}/{session_id}"
    print(f"[WS] Connecting to: {uri}")
    try:
        async with websockets.connect(uri) as ws:
            print("[WS] Connected. Waiting for progress...")
            while True:
                msg = await ws.recv()
                data = json.loads(msg)
                print(
                    f"[WS] Progress: {data.get('progress_percent')}% | "
                    f"Status: {data.get('status')} | "
                    f"Processed: {data.get('matched_items')}/{data.get('total_items')} | "
                    f"Msg: {data.get('message', '')}"
                )
                if data.get("status") in ("COMPLETED", "FAILED"):
                    print("[WS] Session finished, connection closed.")
                    break
    except Exception as exc:
        print(f"[WS] Error: {exc}")


async def main():
    session_id = await upload_file()
    await listen_ws(session_id)


if __name__ == "__main__":
    asyncio.run(main())
