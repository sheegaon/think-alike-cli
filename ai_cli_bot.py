import asyncio
import random

from cli import UserContext, REST, AsyncWS, load_config, process_command


async def bot_loop(cfg, tier="casual"):
    rest = REST(cfg["API_BASE"], cfg["ENDPOINTS"])
    user = UserContext()
    ws = AsyncWS(cfg["WS_URL"], cfg["WS_NAMESPACE"], user=user)

    # Connect WebSocket at startup
    await ws.connect_async()

    try:
        # Initialize an AI bot user
        raw_cmd = f"player get AI_Bot_{random.randint(1000, 9999)}"
        should_continue = await process_command(raw_cmd, rest, ws, user)
        assert should_continue, "Initial player creation failed"

        # Join a room
        raw_cmd = f"room join {tier}"
        should_continue = await process_command(raw_cmd, rest, ws, user)
        assert should_continue, "Joining room failed"

        # Play loop
        while user.room_key is not None:
            if user.round_phase == "deal":
                # Make a random choice
                choice = random.randint(0, len(user.nouns) - 1)
                raw_cmd = f"ws commit {choice}"
                should_continue = await process_command(raw_cmd, rest, ws, user)
                assert should_continue, "Commit failed"

            elif user.round_phase == "waiting":
                # Decide whether to leave the room
                start_time = user.next_round_info['start_time']
                time_remaining = start_time - asyncio.get_event_loop().time()
                # TODO wait random time until up to 10 seconds before round starts to check
                player_count = user.next_round_info['player_count']
                if player_count > 7:
                    raw_cmd = "room leave"
                    should_continue = await process_command(raw_cmd, rest, ws, user)
                    assert should_continue, "Leaving room failed"
                    break

    finally:
        # Clean up websocket connection
        if ws and ws.connected:
            await ws.disconnect_async()


async def main():
    cfg = load_config()
    await bot_loop(cfg)


if __name__ == "__main__":
    asyncio.run(main())
