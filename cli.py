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

Rooms:
  r list
  r join c              POST /rooms/quick-join (casual)
  r join o              POST /rooms/quick-join (competitive)
  r join h              POST /rooms/quick-join (high_stakes)
  r join <room_key>     POST /rooms/join (as player)
  r obs <room_key>      POST /rooms/join as spectator
  r leave               POST /rooms/leave
  r skip                POST /rooms/skip

Meta:
  lb                    GET /leaderboard
  stats                 GET /game/stats

Round (WebSocket):
  ws on                 Connect WS
  ws off                Disconnect WS
  ws jp                 emit join_player
  ws jr                 emit join_room
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
    ws = None
    sess = Session()

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

            # REST API /players calls
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
                        sess.player_id = data["player_id"] if "player_id" in data else data["id"]
                        sess.username = data["username"] if "username" in data else username
                    print(f"[STATE] {sess.player_id=} {sess.username=}")

                if cmd == "me":
                    print(json.dumps(
                        {"player_id": sess.player_id, "username": sess.username, "room_key": sess.room_key},
                        indent=2))

            # REST API /rooms calls
            if root[0] == "r":
                if cmd == "list" or cmd == "l":
                    data = rest.call("rooms_list")
                    if isinstance(data, dict):
                        rooms = data.get("rooms", [])
                        if not rooms:
                            print("[STATE] No rooms available.")
                        else:
                            print(f"[STATE] {len(rooms)} rooms:")
                            for room in rooms:
                                room_state = ', '.join([f'{key}: {room[key]}' for key in room if key != "room_key"])
                                print(f"  - ...{room['room_key'][-5:]} ({room_state})")

                if cmd == "join" or cmd == "j":
                    if not args:
                        print("Usage: r join <subcommand> [args]")
                        continue
                    subcmd = args[0]
                    if subcmd == "c":
                        tier = "casual"
                    elif subcmd == "o":
                        tier = "competitive"
                    elif subcmd == "h":
                        tier = "high_stakes"
                    else:
                        print("Usage: r join c|o|h")
                        continue
                    data = rest.call("rooms_quick_join",
                                     body={"player_id": sess.player_id, "tier": tier, "as_spectator": False})
                    if isinstance(data, dict):
                        sess.room_key = data["room_key"] if "room_key" in data else data["id"]
                        sess.room_token = data["room_token"] if "room_token" in data else data["token"]
                        print(f"[STATE] room_key=...{sess.room_key[-5:]} room_token=...{sess.room_token[-5:]}")

                if cmd == "obs" or cmd == "o":
                    if not args:
                        print("Usage: obs <room_key>")
                        continue
                    rk = args[0]
                    data = rest.call("rooms_join",
                                     body={"room_key": rk, "as_spectator": True, "player_id": sess.player_id})
                    if isinstance(data, dict):
                        sess.room_key = rk
                        sess.room_token = data["room_token"] if "room_token" in data else data["token"]
                        print(f"[STATE] observing room_key={sess.room_key}")

                if cmd == "leave":
                    data = rest.call("rooms_leave",
                                     body={"room_key": sess.room_key, "player_id": sess.player_id})
                    if isinstance(data, dict):
                        print(f"[STATE] leaving room_key=...{data['room_key'][-5:]} success={data['success']}")
                        sess.room_key = None
                        sess.room_token = None

                if cmd == "skip":
                    if not sess.room_key:
                        print("[WARN] Not in a room")
                        continue
                    rest.call("rooms_skip", body={"room_key": sess.room_key})

            # Meta HTTP
            elif root == "lb":
                rest.call("leaderboard")
            elif root == "stats":
                rest.call("game_stats")

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
                elif cmd == "jp":
                    if not ws:
                        print("[WARN] WS not connected")
                        continue
                    ws.emit("join_player", {"player_id": sess.player_id, "username": sess.username})
                elif cmd == "jr":
                    if not ws:
                        print("[WARN] WS not connected")
                        continue
                    ws.emit("join_room", {"room_key": sess.room_key, "token": sess.room_token})
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
