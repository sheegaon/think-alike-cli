"""
Minimalist CLI for Think Alike (Single-Player Control)
Uses HTTP (REST) for room/player ops and Socket.IO for realtime play.
Asynchronous CLI with non-blocking input that redraws prompt after websocket events.
"""

import os
import json
import asyncio
import requests
import socketio
import secrets
import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

HELP = """
Home:
  help                    Show this help
  quit                    Exit

Player:
  p get [username]        Create or fetch named player (GET /players/username/<u> or POST /players) and emit join_player
  p me                    Print current session (player_id, username, room)
  p stats                 GET /players/{player_id}/stats
  p quests                View available quests and progress
  p claim <quest_id>      POST /players/{player_id}/claim-reward

Rooms:
  r list [tier]           GET /rooms (optionally filter by tier)
  r details <room_key>    GET /rooms/{room_key}
  r events [limit]        GET /rooms/{room_key}/events
  r join c|o|h|<key>      POST /rooms/quick-join or /rooms/join and emit join_room
  r obs <room_key>        POST /rooms/join as spectator
  r leave [immediate]     POST /rooms/leave and emit leave_room
  r skip                  POST /rooms/skip

Meta:
  lb [limit]              GET /leaderboard
  stats                   GET /game/stats

Round (WebSocket):
  ws on                   Connect WS
  ws off                  Disconnect WS
  ws commit <idx>         emit commit
  ws reveal               emit reveal
  ws queue on|off         spectator_queue
  ws emote <emoji>        send_emote
  ws event <event> <json> emit arbitrary WS event
"""

DEFAULT_CFG = {
    "API_BASE": "http://localhost:8000/api/v1",
    "WS_URL": "http://localhost:8000",
    "WS_NAMESPACE": "/",
    "ENDPOINTS": {
        "health": {"method": "GET", "path": "/health"},
        "players_create": {"method": "POST", "path": "/players"},
        "players_get": {"method": "GET", "path": "/players/{player_id}"},
        "players_by_username": {"method": "GET", "path": "/players/username/{username}"},
        "players_stats": {"method": "GET", "path": "/players/{player_id}/stats"},
        "players_quests": {"method": "GET", "path": "/players/{player_id}/quests"},
        "players_claim_reward": {"method": "POST", "path": "/players/{player_id}/claim-reward"},
        "rooms_list": {"method": "GET", "path": "/rooms"},
        "rooms_details": {"method": "GET", "path": "/rooms/{room_key}"},
        "rooms_join": {"method": "POST", "path": "/rooms/join"},
        "rooms_quick_join": {"method": "POST", "path": "/rooms/quick-join"},
        "rooms_leave": {"method": "POST", "path": "/rooms/leave"},
        "rooms_skip": {"method": "POST", "path": "/rooms/skip"},
        "rooms_events": {"method": "GET", "path": "/rooms/{room_key}/events"},
        "leaderboard": {"method": "GET", "path": "/leaderboard"},
        "game_stats": {"method": "GET", "path": "/game/stats"}
    }
}

EMOTE_LIST = ['👍', '😂', '😮', '😡', '🎉', '🤔', '❤️']


@dataclass
class UserContext:
    player_id: Optional[int] = None
    username: Optional[str] = None
    room_key: Optional[str] = None
    room_token: Optional[str] = None
    round_key: Optional[str] = None
    round_phase: Optional[str] = None
    choice: Optional[int] = None
    nonce: Optional[str] = None
    adjective: Optional[str] = None
    nouns: Optional[List[str]] = None
    balance: Optional[int] = None
    next_round_info: Optional[Dict[str, Any]] = None
    last_http: Dict[str, Any] = field(default_factory=dict)


class REST:
    def __init__(self, base: str, endpoints: Dict[str, Dict[str, str]]):
        self.base = base.rstrip("/")
        self.endpoints = endpoints

    def call(self, name: str, *, path=None, params=None, body=None, headers=None):
        ep = self.endpoints[name]
        path_t = ep["path"]
        if path:
            for k, v in path.items():
                path_t = path_t.replace(f"{{{k}}}", str(v))
        url = self.base + path_t
        m = ep["method"].upper()
        headers = headers or {}
        print(f"\n[REST ->] {m} {url}")
        if params:
            print(f"     params={json.dumps(params)}")
        if body is not None:
            print(f"     body={json.dumps(body)}")
        print(f"     headers={headers}")
        r = requests.request(m, url, params=params, json=body, headers=headers, timeout=30)
        if r.status_code == 200:
            print(f"[REST <-] status OK")
        else:
            print(f"[REST <-] {r.status_code=}")
        try:
            js = r.json()
            print(f"[REST <-] json={json.dumps(js)[:1000]}")
            return js
        except Exception:
            print(f"[REST <-] text={r.text[:500]}")
            return r.text


class AsyncWS:
    """Async Socket.IO client wrapper"""

    def __init__(self, url: str, namespace: str = "", user: Optional[UserContext] = None):
        self.sio = socketio.AsyncClient(logger=False, engineio_logger=False, reconnection=True)
        self.url = url
        self.ns = namespace if namespace else "/"
        self.connected = False
        self.user = user

        @self.sio.event(namespace=self.ns)
        async def connect():
            self.connected = True
            print("[WS] Connected")

        @self.sio.event(namespace=self.ns)
        async def disconnect():
            self.connected = False
            print("[WS] Disconnected")

        @self.sio.event(namespace=self.ns)
        async def connect_error(data):
            print(f"[WS] Connect error: {data}")

        @self.sio.on("deal", namespace=self.ns)
        async def on_deal(data):
            print(f"[WS <-] deal: {json.dumps(data, ensure_ascii=False)}")
            # Save deal info to user context for later commit
            if self.user and isinstance(data, dict):
                self.user.round_key = data["round_key"]
                self.user.choice = None
                self.user.nonce = None
                self.user.adjective = data.get("adjective")
                self.user.nouns = data.get("nouns")
                self.user.round_phase = "deal"

        @self.sio.on("request_reveal", namespace=self.ns)
        async def on_request_reveal(data):
            print(f"[WS <-] request_reveal: {json.dumps(data, ensure_ascii=False)}")
            # Clear previous deal info
            self.user.adjective = None
            self.user.nouns = None

            # Auto-reveal
            if (self.user and self.user.player_id and self.user.round_key and self.user.choice is not None and
                    self.user.nonce):
                await asyncio.sleep(1)
                await self.emit_async(
                    "reveal", {"choice": self.user.choice, "nonce": self.user.nonce, "round_key": self.user.round_key})

        @self.sio.on("round_results", namespace=self.ns)
        async def on_round_results(data):
            print(f"[WS <-] round_results: {json.dumps(data, ensure_ascii=False)}")
            if self.user and isinstance(data, dict):
                self.user.balance = data.get("new_balance", self.user.balance)
                print(f"[STATE] New balance: {self.user.balance}")
            self.user.round_phase = "results"

        @self.sio.on("next_round_info", namespace=self.ns)
        async def on_next_round_info(data):
            print(f"[WS <-] next_round_info: {json.dumps(data, ensure_ascii=False)}")
            self.user.round_phase = "waiting"
            self.user.next_round_info = data

        @self.sio.on("removed_from_room", namespace=self.ns)
        async def on_removed_from_room(data):
            print(f"[WS <-] removed_from_room: {json.dumps(data, ensure_ascii=False)}")
            if self.user:
                self.user.room_key = None
                self.user.room_token = None
                self.user.round_key = None
                self.user.round_phase = None
                self.user.choice = None
                self.user.nonce = None
                self.user.adjective = None
                self.user.nouns = None
                self.user.next_round_info = None

        @self.sio.on("*", namespace=self.ns)
        async def catchall(event, data):
            print(f"[WS <-] {event}: {json.dumps(data, ensure_ascii=False)}")

    async def connect_async(self, headers=None):
        """Connect to websocket server"""
        print(f"[WS ->] connect {self.url} ns={self.ns} headers={headers}")
        try:
            await self.sio.connect(self.url, namespaces=[self.ns], headers=headers,
                                   transports=["websocket", "polling"])
        except Exception as e:
            print(f"[WS] Connection failed: {e}")

    async def disconnect_async(self):
        """Disconnect from websocket server"""
        print("[WS ->] disconnect")
        try:
            await self.sio.disconnect()
        except Exception as e:
            print(f"[WS] Disconnect error: {e}")

    async def emit_async(self, event: str, data: Any = None):
        """Emit websocket event"""
        payload = {} if data is None else data
        print(f"[WS ->] emit {event} {payload}")
        if self.connected:
            await self.sio.emit(event, payload, namespace=self.ns)
        else:
            print("[WS] Not connected - cannot emit")


async def process_command(raw_cmd: str, rest: REST, ws: Optional[AsyncWS], user: UserContext):
    """Process a single command asynchronously"""
    cmd_parts = raw_cmd.strip().split()
    if len(cmd_parts) == 0:
        return True

    if len(cmd_parts) == 1:
        root, cmd, args = cmd_parts[0], "", []
    else:
        root, cmd, args = cmd_parts[0], cmd_parts[1], cmd_parts[2:]

    try:
        if root in ("help", "?", "h"):
            print(HELP)

        elif root in ('q', 'quit', 'exit', 'bye'):
            print("[BOOT] Bye.")
            return False  # Signal to quit

        # REST API /players calls and WebSocket join_player
        elif root in ('player', 'p'):
            if cmd in ('get', 'g'):
                if not args:
                    print("Usage: p get <username>")
                    return True
                username = args[0]
                data = rest.call("players_by_username", path={"username": username})
                if not (isinstance(data, dict) and ("player_id" in data or "id" in data)):
                    data = rest.call("players_create", body={"username": username})
                if isinstance(data, dict):
                    user.player_id = data["id"] if "id" in data else data["player_id"]
                    user.username = data["username"]
                    user.balance = data["balance"]
                print(f"[STATE] {user.player_id=} {user.username=} {user.balance=}")
                if not ws:
                    print("[WARN] WS not connected")
                    return True
                await ws.emit_async("join_player", {"player_id": user.player_id, "username": user.username})

            elif cmd == "me":
                print(json.dumps({
                    "player_id": user.player_id,
                    "username": user.username,
                    "balance": user.balance,
                    "room_key": user.room_key,
                    "room_token": user.room_token,
                    "round_key": user.round_key,
                    "round_phase": user.round_phase,
                    "next_round_info": user.next_round_info,
                }, indent=2))

            elif cmd in ('stats', 's'):
                if not user.player_id:
                    print("[WARN] No player selected. Use 'p get <username>' first.")
                    return True
                data = rest.call("players_stats", path={"player_id": user.player_id})
                if isinstance(data, dict):
                    print(f"[STATS] Games: {data.get('games_played', 0)}, "
                          f"Wins: {data.get('wins', 0)}, "
                          f"Win Rate: {data.get('win_rate', 0):.1f}%")

            elif cmd in ('quests', 'q'):
                if not user.player_id:
                    print("[WARN] No player selected. Use 'p get <username>' first.")
                    return True
                data = rest.call("players_quests", path={"player_id": user.player_id})
                if not isinstance(data, dict):
                    return True
                quests = data.get("quests", [])
                claimable_count = data.get("claimable_count", 0)
                total_coins = data.get("total_claimable_coins", 0)

                print("[QUESTS] Available Quests & Progress:\n")

                # Group quests by type
                daily_quests = [q for q in quests if q["quest_type"] == "daily"]
                seasonal_quests = [q for q in quests if q["quest_type"] == "seasonal"]

                if daily_quests:
                    print("🌅 DAILY QUESTS:")
                    for quest in daily_quests:
                        status = "✅ COMPLETE" if quest["completed"] else f"📊 {quest['progress']}/{quest['required']}"

                        print(f"[QUESTS] {quest['name']} ({quest['quest_id']}):")
                        print(f"\t{quest['description']} - {quest['reward']} coins\tStatus: {status}")
                        print(f"    Status: {status}")
                        if quest["claimable"]:
                            print(f"    💰 Ready to claim! Use: p claim {quest['quest_id']}")

                if seasonal_quests:
                    print("🌟 SEASONAL QUESTS:")
                    for quest in seasonal_quests:
                        status = "✅ COMPLETE" if quest["completed"] else f"📊 {quest['progress']}/{quest['required']}"

                        print(f"[QUESTS] {quest['name']} ({quest['quest_id']}):")
                        print(f"\t{quest['description']} - {quest['reward']} coins\tStatus: {status}")
                        if quest["claimable"]:
                            print(f"    💰 Ready to claim! Use: p claim {quest['quest_id']}")

                if claimable_count > 0:
                    print(f"💰 Summary: {claimable_count} rewards ready to claim worth {total_coins} coins total!")

            elif cmd in ('claim', 'c'):
                if not args:
                    print("Usage: p claim <quest_id>")
                    return True
                if not user.player_id:
                    print("[WARN] No player selected. Use 'p get <username>' first.")
                    return True
                quest_id = args[0]
                data = rest.call("players_claim_reward",
                                 path={"player_id": user.player_id},
                                 body={"quest_id": quest_id, "player_id": user.player_id})
                if isinstance(data, dict) and data.get("success"):
                    print(f"[REWARD] Claimed {data.get('reward_amount', 0)} coins! "
                          f"New balance: {data.get('new_balance', 0)}")

        # REST API /rooms calls
        elif root in ('room', 'r'):
            if cmd in ('list', 'l'):
                params = {}
                if args and args[0] in ["casual", "competitive", "high_stakes"]:
                    params["tier"] = args[0]
                data = rest.call("rooms_list", params=params)
                if isinstance(data, dict):
                    rooms = data.get("rooms", [])
                    if not rooms:
                        print("[STATE] No rooms available.")
                    else:
                        print(f"[STATE] {len(rooms)} rooms:")
                        for room in rooms:
                            room_info = f"{room['tier']}, {room['player_count']}/{room['max_players']} players, " \
                                        f"{room['stake']} stake, {room['state']}"
                            print(f"  - ...{room['room_key'][-5:]} ({room_info})")

            elif cmd in ('details', 'd'):
                if not args:
                    print("Usage: room details <room_key>")
                    return True
                room_key = args[0]
                data = rest.call("rooms_details", path={"room_key": room_key})
                if isinstance(data, dict):
                    print(f"[ROOM] Tier: {data['tier']}, Players: {data['player_count']}/{data['max_players']}, "
                          f"Stake: {data['stake']}, State: {data['state']}")
                    if data.get("current_round"):
                        round_info = data["current_round"]
                        print(f"[ROUND] Active: {round_info['adjective']} + {len(round_info['nouns'])} nouns, "
                              f"Phase: {round_info['phase']}")

            elif cmd == "events":
                if not user.room_key:
                    print("[WARN] Not in a room")
                    return True
                params = {"limit": int(args[0])} if args and args[0].isdigit() else {"limit": 20}
                data = rest.call("rooms_events", path={"room_key": user.room_key}, params=params)
                if isinstance(data, dict):
                    events = data.get("events", [])
                    print(f"[EVENTS] {len(events)} recent events:")
                    for event in events:
                        timestamp = event["timestamp"][:19]  # Remove microseconds
                        print(f"  - {timestamp}: {event['event_type']} - {event.get('details', {})}")

            elif cmd in ('join', 'j'):
                if not args:
                    print("Usage: room join c|o|h|<room_key>")
                    return True

                if not user.player_id:
                    print("[WARN] No player selected. Use 'p get <username>' first.")
                    return True

                subcmd = args[0]
                if subcmd in ["c", "o", "h"]:
                    # Quick join by tier
                    tier_map = {"c": "casual", "o": "competitive", "h": "high_stakes"}
                    tier = tier_map[subcmd]
                    data = rest.call("rooms_quick_join",
                                     body={"player_id": user.player_id, "tier": tier, "as_spectator": False})
                elif subcmd in ["casual", "competitive", "high_stakes"]:
                    tier = subcmd
                    data = rest.call("rooms_quick_join",
                                     body={"player_id": user.player_id, "tier": tier, "as_spectator": False})
                else:
                    # Join specific room by key
                    room_key = subcmd
                    data = rest.call("rooms_join",
                                     body={"room_key": room_key, "player_id": user.player_id, "as_spectator": False})
                    tier = data.get("tier", "unknown")
                if isinstance(data, dict):
                    user.room_key = data["room_key"]
                    user.room_token = data["room_token"]
                    print(f"[STATE] Joining {tier} room: ...{user.room_key[-5:] if user.room_key else 'unknown'}")
                    if not ws:
                        print("[WARN] WS not connected")
                        return True
                    await ws.emit_async("join_room", {"room_token": user.room_token})

            elif cmd in ('observe', 'obs', 'o'):
                if not args:
                    print("Usage: room observe <room_key>")
                    return True
                room_key = args[0]
                if not user.player_id:
                    print("[WARN] No player selected. Use 'p get <username>' first.")
                    return True
                data = rest.call("rooms_join",
                                 body={"room_key": room_key, "player_id": user.player_id, "as_spectator": True})
                if isinstance(data, dict) and data.get("success"):
                    user.room_key = room_key
                    user.room_token = data["room_token"]
                    print(f"[STATE] Observing room: ...{user.room_key[-5:]}")

            elif cmd == "leave":
                if not user.room_key:
                    print("[WARN] Not in a room")
                    return True
                at_round_end = "immediate" not in args
                data = rest.call("rooms_leave",
                                 body={"room_key": user.room_key, "player_id": user.player_id,
                                       "at_round_end": at_round_end})
                if isinstance(data, dict) and data.get("success"):
                    action = "scheduled leave" if data.get("scheduled") else "left"
                    print(f"[STATE] {action} from room: ...{user.room_key[-5:]}")
                    if not data.get("scheduled"):
                        user.room_key = None
                        user.room_token = None
                if not ws:
                    print("[WARN] WS not connected")
                    return True
                await ws.emit_async("leave_room")

            elif cmd == "skip":
                if not user.room_key:
                    print("[WARN] Not in a room")
                    return True
                data = rest.call("rooms_skip", body={"room_key": user.room_key, "player_id": user.player_id})
                if isinstance(data, dict) and data.get("success"):
                    print("[STATE] Scheduled to skip next round")

        # Meta HTTP calls
        elif root in ('leaderboard', 'lb'):
            params = {}
            if args:
                if args[0].isdigit():
                    params["limit"] = int(args[0])
                if user.player_id:
                    params["current_player_id"] = user.player_id
            data = rest.call("leaderboard", params=params)
            if isinstance(data, dict):
                leaderboard = data.get("leaderboard", [])
                print(f"[LEADERBOARD] Top {len(leaderboard)} players:")
                for entry in leaderboard[:10]:  # Show top 10
                    print(f"  #{entry['rank']}: {entry['username']} - {entry['rating']} rating, "
                          f"{entry['win_rate']:.1f}% wins")
                if data.get("current_player_rank"):
                    print(f"[RANK] Your rank: #{data['current_player_rank']}")

        elif root == "stats":
            data = rest.call("game_stats")
            if isinstance(data, dict):
                print(f"[GAME] {data.get('total_rooms', 0)} total rooms, "
                      f"{data.get('active_rooms', 0)} active, "
                      f"{data.get('total_players', 0)} players online")
                breakdown = data.get("room_breakdown", {})
                if breakdown:
                    tier_info = ", ".join([f"{tier}: {count}" for tier, count in breakdown.items()])
                    print(f"[TIERS] {tier_info}")

        # Emit WebSocket events
        elif root in ('websocket', 'ws'):
            if not cmd:
                print("Usage: ws <subcommand> [args]")
                return True

            if cmd == "on":
                if ws is None:
                    print("[WARN] WebSocket client not initialized")
                    return True
                await ws.connect_async()
            elif cmd == "off":
                if ws:
                    await ws.disconnect_async()
            elif cmd == "commit":
                if not args:
                    print("Usage: commit <idx>")
                    return True
                if not ws or not ws.connected:
                    print("[WARN] WS not connected")
                    return True
                user.choice = int(args[0])
                user.nonce = secrets.token_hex(16)
                user.round_phase = "committed"
                payload = f"{user.player_id}{user.round_key}{user.choice}{user.nonce}".encode("utf-8")
                commit_hash = hashlib.sha256(payload).hexdigest()
                await ws.emit_async("commit", {"hash": commit_hash})
            elif cmd == "reveal":
                if not ws or not ws.connected:
                    print("[WARN] WS not connected")
                    return True
                await ws.emit_async("reveal", {})
            elif cmd == "queue":
                if not ws or not ws.connected:
                    print("[WARN] WS not connected")
                    return True
                if not args:
                    print("Usage: queue on|off")
                    return True
                want = args[0].lower() == "on"
                await ws.emit_async("spectator_queue", {"want_to_join": want})
            elif cmd == "emote":
                if not ws or not ws.connected:
                    print("[WARN] WS not connected")
                    return True
                try:
                    await ws.emit_async("send_emote", {"emote": EMOTE_LIST[args[0]]})
                except TypeError:
                    str_emote = ", ".join([f"{i}: {emote}" for i, emote in enumerate(EMOTE_LIST)])
                    print(f"Usage: emote <emoji_id>\nemoji list: {str_emote}")
                    return True
            elif cmd == "event":
                if len(args) < 2:
                    print("Usage: ws event <event> <json>")
                    return True
                ev = args[0]
                payload = " ".join(args[1:])
                try:
                    data = json.loads(payload)
                except:
                    data = payload
                if ws and ws.connected:
                    await ws.emit_async(ev, data)
                else:
                    print("[WARN] WS not connected")
            else:
                print("Unknown ws subcommand")

    except requests.RequestException as e:
        print(f"[HTTP ERR] {e}")
    except Exception as e:
        print(f"[ERR] {type(e).__name__}: {e}")

    return True  # Continue running


def load_config():
    p = os.environ.get("CLI_CONFIG_JSON", "local_config.json")
    if os.path.exists(p):
        with open(p, "r") as f:
            user = json.load(f)
        merged = DEFAULT_CFG.copy()
        merged.update(user)
        if "ENDPOINTS" in user:
            merged["ENDPOINTS"] = {**DEFAULT_CFG["ENDPOINTS"], **user["ENDPOINTS"]}
        return merged
    return DEFAULT_CFG


async def main():
    """Main async CLI loop"""
    cfg = load_config()
    print(f"[BOOT] cfg: {json.dumps(cfg, indent=2)}")
    rest = REST(cfg["API_BASE"], cfg["ENDPOINTS"])
    user = UserContext()
    ws = AsyncWS(cfg["WS_URL"], cfg["WS_NAMESPACE"], user=user)

    print("\n===== Think Alike CLI =====")
    print("Quick-start: 'p get <username>' create or fetch player, "
          "'r list' list rooms, "
          "'r join c' quick-join casual room. "
          "\n'help' for all commands.")

    # Create async prompt session
    session = PromptSession()

    # Connect WebSocket at startup
    await ws.connect_async()

    try:
        while True:
            try:
                # Use patch_stdout to prevent websocket output from interrupting input
                with patch_stdout():
                    raw = await session.prompt_async("\n> ")

                if not raw.strip():
                    continue

                should_continue = await process_command(raw, rest, ws, user)

                if not should_continue:
                    break

            except (EOFError, KeyboardInterrupt):
                print("\nBye.")
                break

    finally:
        # Clean up websocket connection
        if ws and ws.connected:
            await ws.disconnect_async()


if __name__ == "__main__":
    asyncio.run(main())
