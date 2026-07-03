"""Stream live DAG step events for a conversation to the terminal.

Usage:
    python scripts/ws_listen.py <conversation_id> [host]

Connects to ws://<host>/api/v1/ws/<conversation_id> and prints each
step_update event with a wall-clock timestamp. Pair it with a POST to
/api/v1/query using the same conversation_id to watch the executor's
waves run: independent steps flip to "running" together, dependents
start the moment their upstream settles.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime

import websockets


async def listen(conversation_id: str, host: str) -> None:
    uri = f"ws://{host}/api/v1/ws/{conversation_id}"
    async with websockets.connect(uri) as ws:
        print(f"listening on {uri}")
        async for raw in ws:
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                print(raw)
                continue
            stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            print(f"{stamp}  {event.get('step', '?'):24s} -> {event.get('status', '?')}")


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python scripts/ws_listen.py <conversation_id> [host]")
    conversation_id = sys.argv[1]
    host = sys.argv[2] if len(sys.argv) > 2 else "localhost:8000"
    try:
        asyncio.run(listen(conversation_id, host))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
