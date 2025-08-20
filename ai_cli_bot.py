import asyncio
import random
from typing import Dict
import secrets

from cli import UserContext, REST, AsyncWS, load_config, process_command


async def bot_loop(cfg):
    rest = REST(cfg["API_BASE"], cfg["ENDPOINTS"])
    user = UserContext()
    ws = AsyncWS(cfg["WS_URL"], cfg["WS_NAMESPACE"], user=user)

    try:
        # Initialize an AI bot user
        raw_cmd = f"player get AI_Bot_{random.randint(1000, 9999)}"
        should_continue = await process_command(raw_cmd, rest, ws, user)
        assert should_continue, "Initial player creation failed"

        # Join a "casual" room
        raw_cmd = "room join casual"
        should_continue = await process_command(raw_cmd, rest, ws, user)

    finally:
        # Clean up websocket connection
        if ws and ws.connected:
            await ws.disconnect_async()


async def main():
    cfg = load_config()
    await bot_loop(cfg)


if __name__ == "__main__":
    asyncio.run(main())
