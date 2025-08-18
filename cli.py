"""
Minimalist CLI for Think Alike (Single-Player Control)
Uses HTTP (REST) for room/player ops and Socket.IO for realtime play.
Echoes EVERYTHING it sends/receives.
"""

import os
import json
import requests
import socketio
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

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
        "players_claim_reward": {"method": "POST", "path": "/players/{player_id}/claim-reward"},
        "players_anonymous": {"method": "GET", "path": "/players/anonymous"},
        "rooms_list": {"method": "GET", "path": "/rooms"},
        "rooms_get": {"method": "GET", "path": "/rooms/{room_key}"},
        "rooms_join": {"method": "POST", "path": "/rooms/join"},
        "rooms_quick_join": {"method": "POST", "path": "/rooms/quick-join"},
        "rooms_leave": {"method": "POST", "path": "/rooms/leave"},
        "rooms_skip": {"method": "POST", "path": "/rooms/skip"},
        "rooms_events": {"method": "GET", "path": "/rooms/{room_key}/events"},
        "leaderboard": {"method": "GET", "path": "/leaderboard"},
        "game_stats": {"method": "GET", "path": "/game/stats"}
    }
}


def load_cfg():
    p = os.environ.get("TA_CLI_CONFIG", "config.json")
    if os.path.exists(p):
        with open(p, "r") as f:
            user = json.load(f)
        merged = DEFAULT_CFG.copy()
        merged.update(user)
        if "ENDPOINTS" in user:
            merged["ENDPOINTS"] = {**DEFAULT_CFG["ENDPOINTS"], **user["ENDPOINTS"]}
        return merged
    return DEFAULT_CFG


@dataclass
class Session:
    player_id: Optional[str] = None
    username: Optional[str] = None
    room_key: Optional[str] = None
    room_token: Optional[str] = None
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


class WS:
    def __init__(self, url: str, namespace: str = ""):
        self.sio = socketio.Client(logger=False, engineio_logger=False, reconnection=True)
        self.url = url
        self.ns = namespace if namespace else "/"

        @self.sio.event(namespace=self.ns)
        def connect():
            print("[WS] Connected")

        @self.sio.event(namespace=self.ns)
        def disconnect():
            print("[WS] Disconnected")

        @self.sio.event(namespace=self.ns)
        def connect_error(data):
            print(f"[WS] Connect error: {data}")

        @self.sio.on("*", namespace=self.ns)
        def catchall(event, data):
            print(f"[WS <-] {event}: {json.dumps(data, ensure_ascii=False)}")

    def connect(self, headers=None):
        print(f"[WS ->] connect {self.url} ns={self.ns} headers={headers}")
        self.sio.connect(self.url, namespaces=[self.ns], headers=headers, transports=["websocket", "polling"])

    def disconnect(self):
        print("[WS ->] disconnect")
        try:
            self.sio.disconnect()
        except Exception as e:
            print(f"[WS] disconnect error: {e}")

    def emit(self, event: str, data: Any = None):
        payload = {} if data is None else data
        print(f"[WS ->] emit {event} {payload}")
        self.sio.emit(event, payload, namespace=self.ns)


HELP = """
Home:
  help                  Show this help
  quit                  Exit

Player:
  p get [username]      Create or fetch named player (GET /players/username/<u> or POST /players)
  p me                  Print current session (player_id, username, room)
  p stats               GET /players/{player_id}/stats
  p claim <quest_id>    POST /players/{player_id}/claim-reward
  p anon [limit]        GET /players/anonymous

Rooms:
  r list [tier]         GET /rooms (optionally filter by tier)
  r get <room_key>      GET /rooms/{room_key}
  r events [limit]      GET /rooms/{room_key}/events
  r join c|o|h|<key>    POST /rooms/quick-join or /rooms/join
  r obs <room_key>      POST /rooms/join as spectator
  r leave [immediate]   POST /rooms/leave
  r skip                POST /rooms/skip

Meta:
  lb [limit]            GET /leaderboard
  stats                 GET /game/stats

Round (WebSocket):
  ws on                 Connect WS
  ws off                Disconnect WS
  ws commit <idx>       emit commit
  ws reveal             emit reveal
  ws queue on|off       spectator_queue
  ws emote <emoji>      send_emote
  ws e <event> <json>   emit arbitrary WS event
"""


def main():
    cfg = load_cfg()
    print(f"[BOOT] cfg: {json.dumps(cfg, indent=2)}")
    rest = REST(cfg["API_BASE"], cfg["ENDPOINTS"])
    sess = Session()
    ws = WS(cfg["WS_URL"], cfg.get("WS_NAMESPACE", "/"))
    ws.connect()

    print("\n===== Think Alike CLI =====")
    print("Quick-start: 'p get <username>' create or fetch player, "
          "'r list' list rooms, "
          "'r join c' quick-join casual room. "
          "\n'help' for all commands.")

    while True:
        try:
            raw = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break
        if not raw:
            continue
        parts = raw.split()
        if len(parts) == 0:
            continue
        if len(parts) == 1:
            root, cmd, args = parts[0], "", []
        else:
            root, cmd, args = parts[0], parts[1], parts[2:]

        try:
            if root in ("help", "?", "h"):
                print(HELP)

            if root[0] == 'q':
                print("[BOOT] Bye.")
                break

            # REST API /players calls and WebSocket join_player
            if root[0] == "p":
                if cmd == "get" or cmd == "g":
                    if not args:
                        print("Usage: p get <username>")
                        continue
                    username = args[0]
                    data = rest.call("players_by_username", path={"username": username})
                    if not (isinstance(data, dict) and ("player_id" in data or "id" in data)):
                        data = rest.call("players_create", body={"username": username})
                    if isinstance(data, dict):
                        sess.player_id = str(data["id"]) if "id" in data else str(data["player_id"])
                        sess.username = data["username"]
                    print(f"[STATE] player_id={sess.player_id} username={sess.username}")
                    if not ws:
                        print("[WARN] WS not connected")
                        continue
                    ws.emit("join_player", {"player_id": sess.player_id, "username": sess.username})

                elif cmd == "me":
                    print(json.dumps({
                        "player_id": sess.player_id,
                        "username": sess.username,
                        "room_key": sess.room_key
                    }, indent=2))

                elif cmd == "stats" or cmd == "st":
                    if not sess.player_id:
                        print("[WARN] No player selected. Use 'p get <username>' first.")
                        continue
                    data = rest.call("players_stats", path={"player_id": sess.player_id})
                    if isinstance(data, dict):
                        print(f"[STATS] Games: {data.get('games_played', 0)}, "
                              f"Wins: {data.get('wins', 0)}, "
                              f"Win Rate: {data.get('win_rate', 0):.1f}%")

                elif cmd == "claim" or cmd == "cl":
                    if not args:
                        print("Usage: p claim <quest_id>")
                        continue
                    if not sess.player_id:
                        print("[WARN] No player selected. Use 'p get <username>' first.")
                        continue
                    quest_id = args[0]
                    data = rest.call("players_claim_reward",
                                     path={"player_id": sess.player_id},
                                     body={"quest_id": quest_id, "player_id": int(sess.player_id)})
                    if isinstance(data, dict) and data["success"]:
                        print(f"[REWARD] Claimed {data.get('reward_amount', 0)} coins! "
                              f"New balance: {data.get('new_balance', 0)}")

                elif cmd == "anon" or cmd == "a":
                    limit = int(args[0]) if args and args[0].isdigit() else 20
                    params = {"limit": limit}
                    if sess.player_id:
                        params["exclude_player_id"] = int(sess.player_id)
                    data = rest.call("players_anonymous", params=params)
                    if isinstance(data, list):
                        print(f"[ANON] {len(data)} anonymous players:")
                        for player in data[:10]:  # Show first 10
                            print(f"  - {player['username']}: {player['rating']} rating, "
                                  f"{player['win_rate']}% win rate")

            # REST API /rooms calls and WebSocket join_room
            if root[0] == "r":
                if cmd == "list" or cmd == "l":
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

                elif cmd == "get" or cmd == "g":
                    if not args:
                        print("Usage: r get <room_key>")
                        continue
                    room_key = args[0]
                    data = rest.call("rooms_get", path={"room_key": room_key})
                    if isinstance(data, dict):
                        print(f"[ROOM] Tier: {data['tier']}, Players: {data['player_count']}/{data['max_players']}, "
                              f"Stake: {data['stake']}, State: {data['state']}")
                        if data["current_round"]:
                            round_info = data["current_round"]
                            print(f"[ROUND] Active: {round_info['adjective']} + {len(round_info['nouns'])} nouns, "
                                  f"Phase: {round_info['phase']}")

                elif cmd == "events" or cmd == "ev":
                    if not sess.room_key:
                        print("[WARN] Not in a room")
                        continue
                    params = {"limit": int(args[0])} if args and args[0].isdigit() else {"limit": 20}
                    data = rest.call("rooms_events", path={"room_key": sess.room_key}, params=params)
                    if isinstance(data, dict):
                        events = data.get("events", [])
                        print(f"[EVENTS] {len(events)} recent events:")
                        for event in events:
                            timestamp = event["timestamp"][:19]  # Remove microseconds
                            print(f"  - {timestamp}: {event['event_type']} - {event.get('details', {})}")

                elif cmd == "join" or cmd == "j":
                    if not args:
                        print("Usage: r join c|o|h|<room_key>")
                        continue
                    subcmd = args[0]
                    if subcmd in ["c", "o", "h"]:
                        # Quick join by tier
                        tier_map = {"c": "casual", "o": "competitive", "h": "high_stakes"}
                        tier = tier_map[subcmd]
                        if not sess.player_id:
                            print("[WARN] No player selected. Use 'p get <username>' first.")
                            continue
                        data = rest.call("rooms_quick_join",
                                         body={"player_id": int(sess.player_id), "tier": tier, "as_spectator": False})
                        if isinstance(data, dict):
                            sess.room_key = data["room_key"]
                            sess.room_token = data["room_token"]
                            print(
                                f"[STATE] Joined {tier} room: ...{sess.room_key[-5:] if sess.room_key else 'unknown'}")
                            if not ws:
                                print("[WARN] WS not connected")
                                continue
                            ws.emit("join_room", {"room_token": sess.room_token})
                    else:
                        # Join specific room by key
                        room_key = subcmd
                        if not sess.player_id:
                            print("[WARN] No player selected. Use 'p get <username>' first.")
                            continue
                        data = rest.call("rooms_join",
                                         body={"room_key": room_key, "player_id": int(sess.player_id),
                                               "username": sess.username, "balance": 1000, "as_spectator": False})
                        if isinstance(data, dict) and data["success"]:
                            sess.room_key = room_key
                            sess.room_token = data["room_token"]
                            print(f"[STATE] Joined room: ...{sess.room_key[-5:]}")

                elif cmd == "obs" or cmd == "o":
                    if not args:
                        print("Usage: r obs <room_key>")
                        continue
                    room_key = args[0]
                    if not sess.player_id:
                        print("[WARN] No player selected. Use 'p get <username>' first.")
                        continue
                    data = rest.call("rooms_join",
                                     body={"room_key": room_key, "player_id": int(sess.player_id),
                                           "username": sess.username, "balance": 1000, "as_spectator": True})
                    if isinstance(data, dict) and data["success"]:
                        sess.room_key = room_key
                        sess.room_token = data["room_token"]
                        print(f"[STATE] Observing room: ...{sess.room_key[-5:]}")

                elif cmd == "leave":
                    if not sess.room_key:
                        print("[WARN] Not in a room")
                        continue
                    at_round_end = "immediate" not in args
                    data = rest.call("rooms_leave",
                                     body={"room_key": sess.room_key, "player_id": int(sess.player_id),
                                           "at_round_end": at_round_end})
                    if isinstance(data, dict) and data["success"]:
                        action = "scheduled leave" if data["scheduled"] else "left"
                        print(f"[STATE] {action} from room: ...{sess.room_key[-5:]}")
                        if not data["scheduled"]:
                            sess.room_key = None
                            sess.room_token = None

                elif cmd == "skip":
                    if not sess.room_key:
                        print("[WARN] Not in a room")
                        continue
                    data = rest.call("rooms_skip", body={"room_key": sess.room_key, "player_id": int(sess.player_id)})
                    if isinstance(data, dict) and data["success"]:
                        print("[STATE] Scheduled to skip next round")

            # Meta HTTP calls
            elif root == "lb":
                params = {}
                if args:
                    if args[0].isdigit():
                        params["limit"] = int(args[0])
                    if sess.player_id:
                        params["current_player_id"] = int(sess.player_id)
                data = rest.call("leaderboard", params=params)
                if isinstance(data, dict):
                    leaderboard = data.get("leaderboard", [])
                    print(f"[LEADERBOARD] Top {len(leaderboard)} players:")
                    for entry in leaderboard[:10]:  # Show top 10
                        print(f"  #{entry['rank']}: {entry['username']} - {entry['rating']} rating, "
                              f"{entry['win_rate']:.1f}% wins")
                    if data["current_player_rank"]:
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
            elif root == "ws":
                if not cmd:
                    print("Usage: ws <subcommand> [args]")
                    continue

                if cmd == "on":
                    if ws is None:
                        ws = WS(cfg["WS_URL"], cfg.get("WS_NAMESPACE", ""))
                    ws.connect()
                elif cmd == "off":
                    if ws:
                        ws.disconnect()
                elif cmd == "commit":
                    if not args:
                        print("Usage: commit <idx>")
                        continue
                    if not ws:
                        print("[WARN] WS not connected")
                        continue
                    ws.emit("commit", {"index": int(args[0])})
                elif cmd == "reveal":
                    if not ws:
                        print("[WARN] WS not connected")
                        continue
                    ws.emit("reveal", {})
                elif cmd == "queue":
                    if not ws:
                        print("[WARN] WS not connected")
                        continue
                    if not args:
                        print("Usage: queue on|off")
                        continue
                    want = args[0].lower() == "on"
                    ws.emit("spectator_queue", {"want_to_join": want})
                elif cmd == "emote":
                    if not ws:
                        print("[WARN] WS not connected")
                        continue
                    if not args:
                        print("Usage: emote <emoji>")
                        continue
                    ws.emit("send_emote", {"emote": args[0]})
                elif cmd == "e":
                    if len(args) < 2:
                        print("Usage: ws e <event> <json>")
                        continue
                    ev = args[0]
                    payload = " ".join(args[1:])
                    try:
                        data = json.loads(payload)
                    except:
                        data = payload
                    if ws:
                        ws.emit(ev, data)
                    else:
                        print("[WARN] WS not connected")
                else:
                    print("Unknown ws subcommand")

        except requests.RequestException as e:
            print(f"[HTTP ERR] {e}")
        except Exception as e:
            print(f"[ERR] {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
