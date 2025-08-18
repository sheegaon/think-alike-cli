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
    "WS_NAMESPACE": "",
    "ENDPOINTS": {
        "health": {"method": "GET", "path": "/health"},
        "players_create": {"method": "POST", "path": "/players"},
        "players_anonymous": {"method": "GET", "path": "/players/anonymous"},
        "players_get": {"method": "GET", "path": "/players/{player_id}"},
        "players_by_username": {"method": "GET", "path": "/players/username/{username}"},
        "players_stats": {"method": "GET", "path": "/players/{player_id}/stats"},
        "players_claim_reward": {"method": "POST", "path": "/players/{player_id}/claim-reward"},

        "rooms_create": {"method": "POST", "path": "/rooms"},
        "rooms_list": {"method": "GET", "path": "/rooms"},
        "rooms_get": {"method": "GET", "path": "/rooms/{room_key}"},
        "rooms_join": {"method": "POST", "path": "/rooms/join"},
        "rooms_quick_join": {"method": "POST", "path": "/rooms/quick-join"},
        "rooms_leave": {"method": "POST", "path": "/rooms/leave"},
        "rooms_skip": {"method": "POST", "path": "/rooms/skip"},

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
                path_t = path_t.replace("{%s}" % k, str(v))
        url = self.base + path_t
        m = ep["method"].upper()
        headers = headers or {}
        print("\n[REST ->] %s %s" % (m, url))
        if params:
            print("        params=%s" % json.dumps(params))
        if body is not None:
            print("        body=%s" % json.dumps(body))
        print("        headers=%s" % headers)
        r = requests.request(m, url, params=params, json=body, headers=headers, timeout=30)
        print("[REST <-] status=%s" % r.status_code)
        try:
            js = r.json()
            print("[REST <-] json=%s" % json.dumps(js)[:1000])
            return js
        except Exception:
            print("[REST <-] text=%s" % r.text[:500])
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
            print("[WS] Connect error: %s" % data)

        @self.sio.on("*", namespace=self.ns)
        def catchall(event, data):
            print("[WS <-] %s: %s" % (event, json.dumps(data, ensure_ascii=False)))

    def connect(self, headers=None):
        print("[WS ->] connect %s ns=%s headers=%s" % (self.url, self.ns, headers))
        self.sio.connect(self.url, namespaces=[self.ns], headers=headers, transports=["websocket", "polling"])

    def disconnect(self):
        print("[WS ->] disconnect")
        try:
            self.sio.disconnect()
        except Exception as e:
            print("[WS] disconnect error: %s" % e)

    def emit(self, event: str, data: Any = None):
        payload = {} if data is None else data
        print("[WS ->] emit %s %s" % (event, payload))
        self.sio.emit(event, payload, namespace=self.ns)


HELP = """
Home / Player:
  help                  Show this help
  home                  Home hints
  pa [username]         Provision player (GET /players/anonymous when no username, else POST /players)
  pu <username>         Create or fetch named player (GET /players/username/<u> or POST /players)
  me                    Print current session (player_id, username, room)

Rooms (HTTP):
  l                     GET /rooms
  jc                    POST /rooms/quick-join (casual)
  jo                    POST /rooms/quick-join (competitive)
  jr <room_key>         POST /rooms/join (as player)
  cr <stake> <tier>     POST /rooms (create)
  obs <room_key>        POST /rooms/join as spectator
  lr                    POST /rooms/leave
  skip                  POST /rooms/skip

Round (WebSocket):
  ws on                 Connect WS
  ws off                Disconnect WS
  wsjp                  emit join_player
  wsjr                  emit join_room
  commit <idx>          emit commit
  reveal                emit reveal
  queue on|off          spectator_queue
  emote <emoji>         send_emote
  wse <event> <json>    emit arbitrary WS event

Meta:
  lb                    GET /leaderboard
  stats                 GET /game/stats
  quit                  Exit
"""


def main():
    cfg = load_cfg()
    print("[BOOT] cfg: %s" % json.dumps(cfg, indent=2))
    rest = REST(cfg["API_BASE"], cfg["ENDPOINTS"])
    ws = None
    sess = Session()

    print("\n===== Think Alike CLI =====")
    print("Type: 'jc' quick-join casual, 'jo' competitive, 'l' list rooms, 'cr <stake> <tier>' create. 'help' for all.")

    while True:
        try:
            raw = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break
        if not raw:
            continue
        parts = raw.split()
        cmd, args = parts[0], parts[1:]

        try:
            if cmd in ("help", "?"):
                print(HELP)

            elif cmd == "home":
                print(
                    "Type 'jc' to quick-join casual, 'jo' competitive, 'l' list rooms, 'cr <stake> <tier>' to create.")

            # Player
            elif cmd == "pa":
                uname = args[0] if args else None
                if uname:
                    data = rest.call("players_create", body={"username": uname})
                else:
                    data = rest.call("players_anonymous")
                if isinstance(data, dict):
                    sess.player_id = data.get("player_id") or data.get("id")
                    sess.username = data.get("username") or uname
                print("[STATE] player_id=%s username=%s" % (sess.player_id, sess.username))

            elif cmd == "pu":
                if not args:
                    print("Usage: pu <username>")
                    continue
                uname = args[0]
                data = rest.call("players_by_username", path={"username": uname})
                if not (isinstance(data, dict) and (data.get("player_id") or data.get("id"))):
                    data = rest.call("players_create", body={"username": uname})
                if isinstance(data, dict):
                    sess.player_id = data.get("player_id") or data.get("id")
                    sess.username = data.get("username") or uname
                print("[STATE] player_id=%s username=%s" % (sess.player_id, sess.username))

            elif cmd == "me":
                print(json.dumps({"player_id": sess.player_id, "username": sess.username, "room_key": sess.room_key},
                                 indent=2))

            # Rooms
            elif cmd == "l":
                rest.call("rooms_list")
            elif cmd == "jc":
                data = rest.call("rooms_quick_join", body={"tier": "casual", "as_spectator": False})
                if isinstance(data, dict):
                    sess.room_key = data.get("room_key") or data.get("id")
                    sess.room_token = data.get("room_token") or data.get("token")
                    print("[STATE] room_key=%s room_token=%s" % (sess.room_key, sess.room_token))
            elif cmd == "jo":
                data = rest.call("rooms_quick_join", body={"tier": "competitive", "as_spectator": False})
                if isinstance(data, dict):
                    sess.room_key = data.get("room_key") or data.get("id")
                    sess.room_token = data.get("room_token") or data.get("token")
                    print("[STATE] room_key=%s room_token=%s" % (sess.room_key, sess.room_token))
            elif cmd == "jr":
                if not args:
                    print("Usage: jr <room_key>")
                    continue
                rk = args[0]
                data = rest.call("rooms_join",
                                 body={"room_key": rk, "as_spectator": False, "player_id": sess.player_id})
                if isinstance(data, dict):
                    sess.room_key = rk
                    sess.room_token = data.get("room_token") or data.get("token")
                    print("[STATE] joined room_key=%s room_token=%s" % (sess.room_key, sess.room_token))
            elif cmd == "cr":
                if len(args) < 2:
                    print("Usage: cr <stake> <tier>")
                    continue
                stake = float(args[0])
                tier = args[1]
                data = rest.call("rooms_create", body={"stake": stake, "tier": tier})
                if isinstance(data, dict):
                    sess.room_key = data.get("room_key") or data.get("id")
                    print(f"[STATE] created room_key={sess.room_key}")
            elif cmd == "obs":
                if not args:
                    print("Usage: obs <room_key>")
                    continue
                rk = args[0]
                data = rest.call("rooms_join", body={"room_key": rk, "as_spectator": True, "player_id": sess.player_id})
                if isinstance(data, dict):
                    sess.room_key = rk
                    sess.room_token = data.get("room_token") or data.get("token")
                    print(f"[STATE] observing room_key={sess.room_key}")
            elif cmd == "lr":
                data = rest.call("rooms_leave",
                                 body={"room_key": sess.room_key, "player_id": sess.player_id})
                print("[STATE] left (if server accepted).")
                sess.room_key = None
                sess.room_token = None
            elif cmd == "skip":
                rest.call("rooms_skip", body={"room_key": sess.room_key})

            # Meta HTTP
            elif cmd == "lb":
                rest.call("leaderboard")
            elif cmd == "stats":
                rest.call("game_stats")

            # WS connect
            elif cmd == "ws":
                if not args:
                    print("Usage: ws on|off")
                    continue
                sub = args[0]
                if sub == "on":
                    if ws is None:
                        ws = WS(cfg["WS_URL"], cfg.get("WS_NAMESPACE", ""))
                    ws.connect()
                elif sub == "off":
                    if ws:
                        ws.disconnect()
                else:
                    print("Unknown ws subcommand")

            # WS helpers
            elif cmd == "wse":
                if len(args) < 2:
                    print("Usage: wse <event> <json>")
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

            elif cmd == "wsjp":
                if not ws:
                    print("[WARN] WS not connected")
                    continue
                ws.emit("join_player", {"player_id": sess.player_id, "username": sess.username})
            elif cmd == "wsjr":
                if not ws:
                    print("[WARN] WS not connected")
                    continue
                ws.emit("join_room", {"room_key": sess.room_key, "token": sess.room_token})

            elif cmd in ("quit", "exit", "q"):
                break
            else:
                print("Unknown command. Type 'help'.")

        except requests.RequestException as e:
            print("[HTTP ERR] %s" % e)
        except Exception as e:
            print("[ERR] %s: %s" % (type(e).__name__, e))


if __name__ == "__main__":
    main()
