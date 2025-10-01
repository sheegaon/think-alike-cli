"""
Think Alike CLI using REST-only polling framework.

This CLI has been migrated from Socket.IO to REST polling, following the same
architecture as the AI bot. All gameplay interactions now use REST endpoints
with event polling for real-time updates.
"""

import os
import json
import asyncio
import requests
import secrets
import hashlib
import traceback
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone, timedelta
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

HELP = """
Home:
  help                    Show this help
  quit                    Exit

Player:
  p get [username]        Create or fetch named player
  p me                    Print current session info
  p stats                 GET /players/{player_id}/stats
  p quests                View available quests and progress
  p claim <quest_id>      POST /players/{player_id}/claim-reward
  p presence [read]       POST (default) or GET /players/{player_id}/presence
  p wallet [limit]        GET /players/{player_id}/wallet

Rooms:
  r list [tier]           GET /rooms (optionally filter by tier)
  r summary               GET /rooms/summary (aggregated room info)
  r details <room_key>    GET /rooms/{room_key}
  r events [limit]        GET /rooms/{room_key}/events
  r status                GET /rooms/{room_key}/status (current round state)
  r join c|o|h|<key>      POST /rooms/quick-join or /rooms/join
  r obs <room_key>        POST /rooms/join as spectator
  r leave [immediate]     POST /rooms/leave
  r skip                  POST /rooms/skip

Meta:
  lb [limit]              GET /leaderboard
  stats                   GET /game/stats

Gameplay (REST):
  gp key                  POST /gameplay/commit-key (fetch round token)
  gp commit <idx>         POST /gameplay/commit (hash + submit)
  gp reveal               POST /gameplay/reveal (submit choice)
  gp emote <emoji_id>     POST /gameplay/emote
  gp queue on|off         POST /gameplay/spectator-queue

Polling:
  poll on                 Start event polling
  poll off                Stop event polling
  poll once               Poll events once
"""

GAMEPLAY_HELP = """
Gameplay commands (gp):
  gp key                  Fetch and cache the commit key for the round
  gp commit <idx>         Hash selection + submit commit
  gp reveal               Reveal your selection
  gp emote <emoji_id>     Send an emote to the room
  gp queue on|off         Join or leave the spectator queue
  gp status               Show cached round tokens + commit state
"""

DEFAULT_CFG = {
    "API_BASE": "http://localhost:8000/api/v1",
    "ENDPOINTS": {
        "health": {"method": "GET", "path": "/game/health"},
        "players_create": {"method": "POST", "path": "/players"},
        "players_get": {"method": "GET", "path": "/players/{player_id}"},
        "players_by_username": {"method": "GET", "path": "/players/username/{username}"},
        "players_settings": {"method": "PATCH", "path": "/players/{player_id}/settings"},
        "players_stats": {"method": "GET", "path": "/players/{player_id}/stats"},
        "players_quests": {"method": "GET", "path": "/players/{player_id}/quests"},
        "players_claim_reward": {"method": "POST", "path": "/players/{player_id}/claim-reward"},
        "players_wallet": {"method": "GET", "path": "/players/{player_id}/wallet"},
        "players_presence_post": {"method": "POST", "path": "/players/{player_id}/presence"},
        "players_presence_get": {"method": "GET", "path": "/players/{player_id}/presence"},
        "rooms_list": {"method": "GET", "path": "/rooms"},
        "rooms_summary": {"method": "GET", "path": "/rooms/summary"},
        "rooms_details": {"method": "GET", "path": "/rooms/{room_key}"},
        "rooms_join": {"method": "POST", "path": "/rooms/join"},
        "rooms_quick_join": {"method": "POST", "path": "/rooms/quick-join"},
        "rooms_leave": {"method": "POST", "path": "/rooms/leave"},
        "rooms_skip": {"method": "POST", "path": "/rooms/skip"},
        "rooms_events": {"method": "GET", "path": "/rooms/{room_key}/events"},
        "rooms_status": {"method": "GET", "path": "/rooms/{room_key}/status"},
        "leaderboard": {"method": "GET", "path": "/game/leaderboard"},
        "game_stats": {"method": "GET", "path": "/game/stats"},
        "gameplay_commit_key": {
            "method": "POST",
            "path": "/gameplay/commit-key",
            "request": {
                "room_key": "string",
                "player_id": "integer"
            },
            "response": {
                "round_id": "string",
                "round_key": "string",
                "round_token": "string",
                "expires_at": "datetime"
            }
        },
        "gameplay_commit": {
            "method": "POST",
            "path": "/gameplay/commit",
            "request": {
                "room_key": "string",
                "player_id": "integer",
                "commit_hash": "string",
                "round_token": "string?"
            },
            "response": {
                "round_id": "string",
                "commit_token": "string",
                "round_token": "string",
                "commit_hash": "string"
            }
        },
        "gameplay_reveal": {
            "method": "POST",
            "path": "/gameplay/reveal",
            "request": {
                "room_key": "string",
                "player_id": "integer",
                "choice": "integer",
                "nonce": "string",
                "round_key": "string",
                "round_token": "string?",
                "commit_token": "string?"
            },
            "response": {
                "round_id": "string",
                "round_token": "string",
                "reveal_token": "string",
                "status": "string"
            }
        },
        "gameplay_emote": {
            "method": "POST",
            "path": "/gameplay/emote",
            "request": {
                "room_key": "string",
                "player_id": "integer",
                "emote": "string"
            },
            "response": {
                "emote": "string",
                "sent_at": "datetime"
            }
        },
        "gameplay_spectator_queue": {
            "method": "POST",
            "path": "/gameplay/spectator-queue",
            "request": {
                "room_key": "string",
                "player_id": "integer",
                "want_to_join": "boolean"
            },
            "response": {
                "want_to_join": "boolean",
                "queued": "boolean"
            }
        },
        "admin_balance": {"method": "POST", "path": "/admin/balance"},
    },
    "EVENT_POLL_INTERVAL": 2.0,
    "STATUS_POLL_INTERVAL": 5.0,
    "HEARTBEAT_INTERVAL": 10.0,
    "EVENT_POLL_LIMIT": 50,
    "INITIAL_EVENT_LIMIT": 100,
}

EMOTE_LIST = ['👍', '😂', '😮', '😡', '🎉', '🤔', '❤️']


def parse_iso_datetime(value: Any) -> Optional[datetime]:
    """Parse an ISO8601 timestamp into a timezone-aware datetime."""
    if not isinstance(value, str):
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass
class RoundContext:
    """Tracks state for the active round."""
    round_id: Optional[str] = None
    round_key: Optional[str] = None
    round_phase: Optional[str] = None
    adjective: Optional[str] = None
    nouns: Optional[List[str]] = None
    choice: Optional[int] = None
    nonce: Optional[str] = None
    round_token: Optional[str] = None
    commit_token: Optional[str] = None
    reveal_token: Optional[str] = None
    commit_hash: Optional[str] = None
    key_expires_at: Optional[str] = None


@dataclass
class UserContext:
    player_id: Optional[int] = None
    username: Optional[str] = None
    room_key: Optional[str] = None
    room_token: Optional[str] = None
    balance: Optional[int] = None
    next_round_info: Optional[Dict[str, Any]] = None
    player_count: int = 0
    last_http: Dict[str, Any] = field(default_factory=dict)
    last_event_id: Optional[int] = None
    last_heartbeat: Optional[datetime] = None
    last_status_poll: Optional[datetime] = None
    spectator_queue_opt_in: bool = False

    # Round-specific state
    round: RoundContext = field(default_factory=RoundContext)
    has_committed: bool = False
    has_revealed: bool = False

    # Polling control
    polling_active: bool = False
    polling_task: Optional[asyncio.Task] = None


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
        if headers:
            print(f"     headers={headers}")
            
        try:
            r = requests.request(m, url, params=params, json=body, headers=headers, timeout=30)
            print(f"[REST <-] {r.status_code}")
            
            if r.status_code // 100 == 2:
                try:
                    js = r.json()
                    print(f"[REST <-] json={json.dumps(js)[:1000]}")
                    return js, None
                except Exception:
                    print(f"[REST <-] text={r.text[:500]}")
                    return r.text, None
            else:
                print(f"[REST <-] error={r.text[:500]}")
                return None, r.text or str(r.status_code)
        except Exception as e:
            print(f"[REST <-] exception={e}")
            return None, str(e)


def reset_round_state(user: UserContext) -> None:
    """Reset round-specific state."""
    user.round = RoundContext()
    user.has_committed = False
    user.has_revealed = False


def reset_room_tracking(user: UserContext) -> None:
    """Reset polling-related room tracking state."""
    user.last_event_id = None
    user.last_status_poll = None


def cache_round_artifacts(user: UserContext, payload: Dict[str, Any]) -> None:
    """Persist any round/gameplay metadata returned by REST calls."""
    if not isinstance(payload, dict):
        return

    rc = user.round
    field_map = {
        "round_id": "round_id",
        "round_key": "round_key",
        "round_token": "round_token",
        "commit_token": "commit_token",
        "reveal_token": "reveal_token",
        "nonce": "nonce",
        "choice": "choice",
        "round_phase": "round_phase",
        "commit_hash": "commit_hash",
        "key_expires_at": "key_expires_at",
        "expires_at": "key_expires_at",
    }

    for source, attr in field_map.items():
        if source in payload and payload[source] is not None:
            setattr(rc, attr, payload[source])

    if "balance" in payload and isinstance(payload["balance"], int):
        user.balance = payload["balance"]

    if "has_committed" in payload:
        user.has_committed = bool(payload["has_committed"])

    if "has_revealed" in payload:
        user.has_revealed = bool(payload["has_revealed"])


def ensure_player_and_room(user: UserContext, action: str) -> bool:
    """Validate player and room state before performing gameplay actions."""
    if not user.player_id:
        print(f"[ERROR] No player selected. Use 'p get <username>' before '{action}'.")
        return False
    if not user.room_key:
        print(f"[ERROR] Not in a room. Join a room before '{action}'.")
        return False
    return True


def process_room_event(event: Dict[str, Any], user: UserContext) -> None:
    """Process a single room event to update user state."""
    event_type = event.get("event_type")
    details = event.get("details") or {}
    print(f"[EVENT] {event_type}: {json.dumps(details, ensure_ascii=False)}")

    if event_type == "round_start":
        user.round.round_id = details.get("round_id")
        user.round.adjective = details.get("adjective")
        user.round.nouns = details.get("nouns") or []
        user.round.round_phase = "SELECT"
        user.has_committed = False
        user.has_revealed = False
        print(f"[ROUND] New round: {user.round.adjective} + {len(user.round.nouns)} nouns")

    elif event_type == "round_reveal_start":
        user.round.round_phase = "REVEAL"
        print("[ROUND] Reveal phase started")

    elif event_type == "round_results":
        payout = details.get("payouts", {}).get(str(user.player_id) if user.player_id else "0", 0)
        if payout:
            print(f"[ROUND] Payout: {payout}")
            if user.balance is not None:
                user.balance += payout
        user.round.round_phase = "RESULTS"
        reset_round_state(user)

    elif event_type == "next_round_scheduled":
        user.next_round_info = details
        user.player_count = details.get("player_count", user.player_count)

    elif event_type == "commit_submitted":
        if details.get("player_id") == user.player_id:
            user.has_committed = True
            print("[COMMIT] Your commit confirmed")

    elif event_type == "players_removed":
        removed = details.get("player_ids") or []
        if user.player_id in removed:
            print("[ROOM] You were removed from the room")
            user.room_key = None
            user.room_token = None
            reset_room_tracking(user)
            reset_round_state(user)


async def maybe_send_heartbeat(rest: REST, user: UserContext, cfg: Dict[str, Any]) -> Optional[datetime]:
    """Send heartbeat if needed, returning the latest timestamp."""
    if user.player_id is None:
        return None
    interval = float(cfg.get("HEARTBEAT_INTERVAL", 10.0))
    now = datetime.now(timezone.utc)
    if user.last_heartbeat and (now - user.last_heartbeat).total_seconds() < interval:
        return user.last_heartbeat
    data, err = rest.call("players_presence_post", path={"player_id": user.player_id})
    if err:
        print(f"[HEARTBEAT] Failed: {err}")
        return None

    server_ts = None
    if isinstance(data, dict):
        server_ts = parse_iso_datetime(data.get("last_seen_at"))
    user.last_heartbeat = server_ts or now
    return user.last_heartbeat


async def poll_events_once(rest: REST, user: UserContext, cfg: Dict[str, Any]) -> bool:
    """Poll events once. Returns True if room is still valid."""
    if not user.room_key:
        return False

    def _is_not_found(err: Optional[str]) -> bool:
        return bool(err and ("not found" in err.lower() or "404" in err))

    _ = await maybe_send_heartbeat(rest, user, cfg)

    # Poll events
    params: Dict[str, Any] = {"limit": int(cfg.get("EVENT_POLL_LIMIT", 50))}
    if user.last_event_id is not None:
        params["since_event_id"] = user.last_event_id
    
    data, err = rest.call("rooms_events", path={"room_key": user.room_key}, params=params)
    if err:
        print(f"[POLL] Event poll failed: {err}")
        if _is_not_found(err):
            return False
        return True

    if isinstance(data, dict):
        events = data.get("events", [])
        for event in events:
            process_room_event(event, user)
            event_id = event.get("event_id")
            if isinstance(event_id, int):
                user.last_event_id = event_id if user.last_event_id is None else max(user.last_event_id, event_id)

    # Poll status periodically
    status_interval = float(cfg.get("STATUS_POLL_INTERVAL", 5.0))
    now = datetime.now(timezone.utc)
    if not user.last_status_poll or (now - user.last_status_poll).total_seconds() >= status_interval:
        status_data, status_err = rest.call("rooms_status", path={"room_key": user.room_key})
        if status_err:
            print(f"[POLL] Status poll failed: {status_err}")
            if _is_not_found(status_err):
                return False
        elif isinstance(status_data, dict):
            user.player_count = status_data.get("player_count", user.player_count)
            round_phase = status_data.get("round_phase")
            if round_phase:
                user.round.round_phase = round_phase
            user.last_status_poll = now

    return True


async def poll_events_loop(rest: REST, user: UserContext, cfg: Dict[str, Any]) -> None:
    """Background polling loop."""
    interval = float(cfg.get("EVENT_POLL_INTERVAL", 2.0))
    
    while user.polling_active and user.room_key:
        try:
            room_valid = await poll_events_once(rest, user, cfg)
            if not room_valid:
                print("[POLL] Room no longer valid, stopping poll")
                break
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            print("[POLL] Polling cancelled")
            break
        except Exception as e:
            print(f"[POLL] Error: {e}")
            await asyncio.sleep(interval)


async def start_polling(rest: REST, user: UserContext, cfg: Dict[str, Any]) -> None:
    """Start background event polling."""
    if user.polling_active:
        print("[POLL] Already polling")
        return
    
    if not user.room_key:
        print("[POLL] Not in a room")
        return

    user.polling_active = True
    user.polling_task = asyncio.create_task(poll_events_loop(rest, user, cfg))
    print("[POLL] Started background polling")


async def stop_polling(user: UserContext) -> None:
    """Stop background event polling."""
    user.polling_active = False
    if user.polling_task and not user.polling_task.done():
        user.polling_task.cancel()
        try:
            await user.polling_task
        except asyncio.CancelledError:
            pass
    user.polling_task = None
    print("[POLL] Stopped background polling")


async def fetch_commit_key(rest: REST, user: UserContext, *, silent: bool = False) -> Optional[Dict[str, Any]]:
    """Fetch the per-round encryption key needed for commits."""
    if not user.player_id or not user.room_key:
        return None

    payload = {"room_key": user.room_key, "player_id": user.player_id}
    data, err = rest.call("gameplay_commit_key", body=payload)
    if err:
        print(f"[COMMIT] Failed to fetch commit key: {err}")
        return None

    if isinstance(data, dict):
        user.last_http["gameplay_commit_key"] = data
        cache_round_artifacts(user, data)

        if not silent:
            round_id = user.round.round_id or data.get("round_id")
            token_hint = None
            if user.round.round_token:
                token_hint = f"…{user.round.round_token[-6:]}" if len(user.round.round_token) > 6 else user.round.round_token
            key_hint = None
            if user.round.round_key:
                key_hint = f"…{user.round.round_key[-6:]}" if len(user.round.round_key) > 6 else user.round.round_key
            print("[COMMIT] Round key cached" +
                  (f" (round_id={round_id})" if round_id else "") +
                  (f", key={key_hint}" if key_hint else "") +
                  (f", token={token_hint}" if token_hint else ""))

        return data

    print("[COMMIT] Unexpected commit key response")
    return None


async def handle_gameplay_command(action: str, args: List[str], rest: REST, user: UserContext) -> None:
    """Handle gameplay-related REST commands under the gp namespace."""
    action = (action or "").lower()

    if action in ("", "help", "?"):
        print(GAMEPLAY_HELP)
        return

    if action in ("key", "k"):
        if not ensure_player_and_room(user, "gp key"):
            return
        await fetch_commit_key(rest, user)
        return

    if action in ("commit", "c"):
        if not ensure_player_and_room(user, "gp commit"):
            return
        if not args or not args[0].isdigit():
            print("Usage: gp commit <noun_index>")
            return

        if user.round.round_phase and user.round.round_phase not in ("SELECT", None):
            print(f"[ERROR] Cannot commit in phase: {user.round.round_phase}")
            return
        if user.has_committed:
            print("[ERROR] Already committed")
            return

        choice = int(args[0])
        total_nouns = len(user.round.nouns or [])
        if total_nouns == 0:
            print("[ERROR] Round nouns not available yet from the server")
            return
        if choice < 0 or choice >= total_nouns:
            print(f"[ERROR] Invalid choice. Available indices: 0-{total_nouns - 1}")
            return

        if not user.round.round_key:
            data = await fetch_commit_key(rest, user, silent=True)
            if data is None or not user.round.round_key:
                print("[ERROR] Failed to cache commit key for this round")
                return

        user.round.choice = choice
        user.round.nonce = secrets.token_hex(16)
        payload = f"{user.player_id}{user.round.round_key}{user.round.choice}{user.round.nonce}"
        commit_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        user.round.commit_hash = commit_hash

        body: Dict[str, Any] = {
            "room_key": user.room_key,
            "player_id": user.player_id,
            "commit_hash": commit_hash,
        }
        if user.round.round_token:
            body["round_token"] = user.round.round_token
        if user.round.round_id:
            body["round_id"] = user.round.round_id

        data, err = rest.call("gameplay_commit", body=body)
        if err:
            print(f"[ERROR] Commit failed: {err}")
            return

        user.last_http["gameplay_commit"] = data
        if isinstance(data, dict):
            cache_round_artifacts(user, data)

        user.has_committed = True
        noun = user.round.nouns[choice] if user.round.nouns and choice < len(user.round.nouns) else choice
        print(f"[COMMIT] Submitted choice {choice} ({noun}) with hash {commit_hash[:12]}…")
        return

    if action in ("reveal", "r"):
        if not ensure_player_and_room(user, "gp reveal"):
            return
        if user.round.round_phase and user.round.round_phase not in ("REVEAL", None):
            print(f"[ERROR] Cannot reveal in phase: {user.round.round_phase}")
            return
        if user.has_revealed:
            print("[ERROR] Already revealed")
            return
        if user.round.choice is None or not user.round.nonce or not user.round.round_key:
            print("[ERROR] Missing reveal data - did you commit first?")
            return

        body: Dict[str, Any] = {
            "room_key": user.room_key,
            "player_id": user.player_id,
            "choice": user.round.choice,
            "nonce": user.round.nonce,
            "round_key": user.round.round_key,
        }
        if user.round.round_token:
            body["round_token"] = user.round.round_token
        if user.round.commit_token:
            body["commit_token"] = user.round.commit_token
        if user.round.round_id:
            body["round_id"] = user.round.round_id

        data, err = rest.call("gameplay_reveal", body=body)
        if err:
            print(f"[ERROR] Reveal failed: {err}")
            return

        user.last_http["gameplay_reveal"] = data
        if isinstance(data, dict):
            cache_round_artifacts(user, data)

        user.has_revealed = True
        print("[REVEAL] Reveal submitted successfully")
        return

    if action in ("emote", "e"):
        if not ensure_player_and_room(user, "gp emote"):
            return
        if not args or not args[0].isdigit():
            emote_str = ", ".join([f"{i}: {emote}" for i, emote in enumerate(EMOTE_LIST)])
            print(f"Usage: gp emote <emoji_id>\nAvailable: {emote_str}")
            return

        emote_id = int(args[0])
        if emote_id < 0 or emote_id >= len(EMOTE_LIST):
            print(f"[ERROR] Invalid emote ID. Max: {len(EMOTE_LIST) - 1}")
            return

        body = {
            "room_key": user.room_key,
            "player_id": user.player_id,
            "emote": EMOTE_LIST[emote_id],
        }
        data, err = rest.call("gameplay_emote", body=body)
        if err:
            print(f"[ERROR] Emote failed: {err}")
            return

        user.last_http["gameplay_emote"] = data
        if isinstance(data, dict):
            cache_round_artifacts(user, data)
        print(f"[EMOTE] Sent: {EMOTE_LIST[emote_id]}")
        return

    if action in ("queue", "q"):
        if not ensure_player_and_room(user, "gp queue"):
            return
        if not args:
            print("Usage: gp queue on|off")
            return

        want = args[0].lower() in ("on", "true", "yes", "1")
        body = {
            "room_key": user.room_key,
            "player_id": user.player_id,
            "want_to_join": want,
        }
        if user.round.round_id:
            body["round_id"] = user.round.round_id

        data, err = rest.call("gameplay_spectator_queue", body=body)
        if err:
            print(f"[ERROR] Queue toggle failed: {err}")
            return

        user.last_http["gameplay_spectator_queue"] = data
        if isinstance(data, dict):
            cache_round_artifacts(user, data)
            user.spectator_queue_opt_in = bool(data.get("want_to_join", want))
            queued = data.get("queued")
        else:
            user.spectator_queue_opt_in = want
            queued = None

        action_str = "joined" if user.spectator_queue_opt_in else "left"
        extra = f" (queued={queued})" if queued is not None else ""
        print(f"[QUEUE] {action_str} spectator queue{extra}")
        return

    if action in ("status", "s"):
        rc = user.round
        summary = {
            "round_id": rc.round_id,
            "phase": rc.round_phase,
            "choice": rc.choice,
            "nonce": rc.nonce,
            "round_token": rc.round_token,
            "commit_token": rc.commit_token,
            "reveal_token": rc.reveal_token,
            "commit_hash": rc.commit_hash,
            "key_expires_at": rc.key_expires_at,
            "has_committed": user.has_committed,
            "has_revealed": user.has_revealed,
            "spectator_queue": user.spectator_queue_opt_in,
        }
        print(json.dumps(summary, indent=2))
        return

    print(f"[ERROR] Unknown gameplay command: {action}")

async def process_command(raw_cmd: str, rest: REST, user: UserContext, cfg: Dict[str, Any]):
    """Process a single command."""
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
            await stop_polling(user)
            print("[CLI] Bye.")
            return False

        # Player commands
        elif root in ('player', 'p'):
            if cmd in ('get', 'g'):
                if not args:
                    print("Usage: p get <username>")
                    return True
                username = args[0]
                def is_invalid_player_data(data, err):
                    return err or not isinstance(data, dict) or "id" not in data

                username = args[0]
                data, err = rest.call("players_by_username", path={"username": username})
                if is_invalid_player_data(data, err):
                    data, err = rest.call("players_create", body={"username": username})
                    if is_invalid_player_data(data, err):
                        print(f"[ERROR] Failed to create player: {err}")
                        return True
                
                user.player_id = data["id"]
                user.username = data["username"]
                user.balance = data.get("balance")
                print(f"[PLAYER] {user.username} (ID: {user.player_id}), Balance: {user.balance}")

            elif cmd == "me":
                print(json.dumps({
                    "player_id": user.player_id,
                    "username": user.username,
                    "balance": user.balance,
                    "last_heartbeat": user.last_heartbeat.isoformat() if user.last_heartbeat else None,
                    "room_key": f"...{user.room_key[-5:]}" if user.room_key else None,
                    "round_phase": user.round.round_phase,
                    "has_committed": user.has_committed,
                    "has_revealed": user.has_revealed,
                    "polling": user.polling_active,
                }, indent=2))

            elif cmd in ('stats', 's'):
                if not user.player_id:
                    print("[ERROR] No player selected. Use 'p get <username>' first.")
                    return True
                data, err = rest.call("players_stats", path={"player_id": user.player_id})
                if err:
                    print(f"[ERROR] {err}")
                elif isinstance(data, dict):
                    print(f"[STATS] Games: {data.get('games_played', 0)}, "
                          f"Wins: {data.get('wins', 0)}, "
                          f"Win Rate: {data.get('win_rate', 0):.1f}%")

            elif cmd in ('quests', 'q'):
                if not user.player_id:
                    print("[ERROR] No player selected. Use 'p get <username>' first.")
                    return True
                data, err = rest.call("players_quests", path={"player_id": user.player_id})
                if err:
                    print(f"[ERROR] {err}")
                elif isinstance(data, dict):
                    quests = data.get("quests", [])
                    claimable_count = data.get("claimable_count", 0)
                    total_coins = data.get("total_claimable_coins", 0)
                    
                    print(f"[QUESTS] {len(quests)} quests, {claimable_count} claimable ({total_coins} coins)")
                    for quest in quests[:5]:  # Show first 5
                        status = "✅" if quest["completed"] else f"{quest['progress']}/{quest['required']}"
                        claimable = " 💰 READY" if quest["claimable"] else ""
                        print(f"  {quest['quest_id']}: {quest['name']} - {status}{claimable}")

            elif cmd in ('claim', 'c'):
                if not args:
                    print("Usage: p claim <quest_id>")
                    return True
                if not user.player_id:
                    print("[ERROR] No player selected. Use 'p get <username>' first.")
                    return True
                quest_id = args[0]
                data, err = rest.call("players_claim_reward",
                                     path={"player_id": user.player_id},
                                     body={"quest_id": quest_id, "player_id": user.player_id})
                if err:
                    print(f"[ERROR] {err}")
                elif isinstance(data, dict) and data.get("success"):
                    print(f"[REWARD] Claimed {data.get('reward_amount', 0)} coins! "
                          f"New balance: {data.get('new_balance', 0)}")

            elif cmd == "presence":
                if not user.player_id:
                    print("[ERROR] No player selected. Use 'p get <username>' first.")
                    return True
                action = args[0].lower() if args else "send"
                if action in ("read", "get", "view", "last"):
                    data, err = rest.call("players_presence_get", path={"player_id": user.player_id})
                    if err:
                        print(f"[ERROR] {err}")
                    elif isinstance(data, dict):
                        server_ts = parse_iso_datetime(data.get("last_seen_at"))
                        if server_ts:
                            user.last_heartbeat = server_ts
                        print(json.dumps(data, indent=2))
                else:
                    ts = await maybe_send_heartbeat(rest, user, cfg)
                    if ts:
                        print(f"[HEARTBEAT] Last seen at {ts.isoformat()}")

            elif cmd == "wallet":
                if not user.player_id:
                    print("[ERROR] No player selected. Use 'p get <username>' first.")
                    return True
                params = {"limit": int(args[0])} if args and args[0].isdigit() else {"limit": 10}
                data, err = rest.call("players_wallet", path={"player_id": user.player_id}, params=params)
                if err:
                    print(f"[ERROR] {err}")
                elif isinstance(data, dict):
                    balance = data.get('balance')
                    try:
                        if balance is not None:
                            user.balance = int(balance)
                    except (TypeError, ValueError):
                        pass
                    print(f"[WALLET] Balance: {data.get('balance', 0)}")
                    transactions = data.get("transactions", [])
                    for tx in transactions[:5]:
                        delta = f"+{tx['delta']}" if tx['delta'] > 0 else str(tx['delta'])
                        print(f"  {tx['timestamp'][:19]}: {delta} - {tx['reason']}")

            elif cmd == "heartbeat":
                if not user.player_id:
                    print("[ERROR] No player selected. Use 'p get <username>' first.")
                    return True
                ts = await maybe_send_heartbeat(rest, user, cfg)
                if ts:
                    print(f"[HEARTBEAT] Last seen at {ts.isoformat()}")

        # Room commands
        elif root in ('room', 'r'):
            if cmd in ('list', 'l'):
                params = {}
                if args and args[0] in ["casual", "competitive", "high_stakes"]:
                    params["tier"] = args[0]
                data, err = rest.call("rooms_list", params=params)
                if err:
                    print(f"[ERROR] {err}")
                elif isinstance(data, dict):
                    rooms = data.get("rooms", [])
                    if not rooms:
                        print("[ROOMS] No rooms available.")
                    else:
                        print(f"[ROOMS] {len(rooms)} rooms:")
                        for room in rooms:
                            room_info = f"{room['tier']}, {room['player_count']}/{room['max_players']} players, " \
                                        f"{room['stake']} stake, {room['state']}"
                            print(f"  - ...{room['room_key'][-5:]} ({room_info})")

            elif cmd == "summary":
                data, err = rest.call("rooms_summary")
                if err:
                    print(f"[ERROR] {err}")
                elif isinstance(data, dict):
                    summary = data.get("summary", [])
                    print("[SUMMARY] Room tiers:")
                    for item in summary:
                        print(f"  {item['tier']}: {item['player_count']} players, "
                              f"stake {item['stake']}, fee {item['entry_fee']}")

            elif cmd in ('details', 'd'):
                if not args:
                    print("Usage: r details <room_key>")
                    return True
                room_key = args[0]
                data, err = rest.call("rooms_details", path={"room_key": room_key})
                if err:
                    print(f"[ERROR] {err}")
                elif isinstance(data, dict):
                    print(f"[ROOM] Tier: {data['tier']}, Players: {data['player_count']}/{data['max_players']}, "
                          f"Stake: {data['stake']}, State: {data['state']}")

            elif cmd == "events":
                if not user.room_key:
                    print("[ERROR] Not in a room")
                    return True
                params = {"limit": int(args[0])} if args and args[0].isdigit() else {"limit": 20}
                data, err = rest.call("rooms_events", path={"room_key": user.room_key}, params=params)
                if err:
                    print(f"[ERROR] {err}")
                elif isinstance(data, dict):
                    events = data.get("events", [])
                    print(f"[EVENTS] {len(events)} recent events:")
                    for event in events:  # Show all fetched events
                        timestamp = event["timestamp"][:19]
                        print(f"  {timestamp}: {event['event_type']} - {event.get('details', {})}")

            elif cmd == "status":
                if not user.room_key:
                    print("[ERROR] Not in a room")
                    return True
                data, err = rest.call("rooms_status", path={"room_key": user.room_key})
                if err:
                    print(f"[ERROR] {err}")
                elif isinstance(data, dict):
                    print(f"[STATUS] Players: {data.get('player_count', 0)}, "
                          f"Phase: {data.get('round_phase', 'unknown')}, "
                          f"State: {data.get('state', 'unknown')}")

            elif cmd in ('join', 'j'):
                if not args:
                    print("Usage: r join c|o|h|<room_key>")
                    return True
                if not user.player_id:
                    print("[ERROR] No player selected. Use 'p get <username>' first.")
                    return True

                subcmd = args[0]
                if subcmd in ["c", "o", "h"]:
                    tier_map = {"c": "casual", "o": "competitive", "h": "high_stakes"}
                    tier = tier_map[subcmd]
                    data, err = rest.call("rooms_quick_join",
                                         body={"player_id": user.player_id, "tier": tier, "as_spectator": False})
                elif subcmd in ["casual", "competitive", "high_stakes"]:
                    tier = subcmd
                    data, err = rest.call("rooms_quick_join",
                                         body={"player_id": user.player_id, "tier": tier, "as_spectator": False})
                else:
                    room_key = subcmd
                    data, err = rest.call("rooms_join",
                                         body={"room_key": room_key, "player_id": user.player_id, "as_spectator": False})
                    tier = "unknown"

                if err:
                    print(f"[ERROR] Failed to join: {err}")
                elif isinstance(data, dict):
                    user.room_key = data["room_key"]
                    user.room_token = data.get("room_token")
                    user.balance = data.get("new_balance", user.balance)
                    reset_room_tracking(user)
                    reset_round_state(user)
                    print(f"[ROOM] Joined room: ...{user.room_key[-5:] if user.room_key else 'unknown'}")
                    # Auto-start polling
                    await start_polling(rest, user, cfg)

            elif cmd in ('observe', 'obs', 'o'):
                if not args:
                    print("Usage: r obs <room_key>")
                    return True
                room_key = args[0]
                if not user.player_id:
                    print("[ERROR] No player selected. Use 'p get <username>' first.")
                    return True
                data, err = rest.call("rooms_join",
                                     body={"room_key": room_key, "player_id": user.player_id, "as_spectator": True})
                if err:
                    print(f"[ERROR] {err}")
                elif isinstance(data, dict):
                    user.room_key = room_key
                    user.room_token = data.get("room_token")
                    reset_room_tracking(user)
                    reset_round_state(user)
                    print(f"[ROOM] Observing room: ...{user.room_key[-5:]}")
                    await start_polling(rest, user, cfg)

            elif cmd == "leave":
                if not user.room_key:
                    print("[ERROR] Not in a room")
                    return True
                at_round_end = "immediate" not in args
                data, err = rest.call("rooms_leave",
                                     body={"room_key": user.room_key, "player_id": user.player_id,
                                           "at_round_end": at_round_end})
                if err:
                    print(f"[ERROR] {err}")
                elif isinstance(data, dict) and data.get("success"):
                    action = "scheduled leave" if data.get("scheduled") else "left"
                    print(f"[ROOM] {action} from room")
                    if not data.get("scheduled"):
                        await stop_polling(user)
                        user.room_key = None
                        user.room_token = None
                        reset_room_tracking(user)
                        reset_round_state(user)

            elif cmd == "skip":
                if not user.room_key:
                    print("[ERROR] Not in a room")
                    return True
                data, err = rest.call("rooms_skip", body={"room_key": user.room_key, "player_id": user.player_id})
                if err:
                    print(f"[ERROR] {err}")
                elif isinstance(data, dict) and data.get("success"):
                    print("[ROOM] Scheduled to skip next round")

        # Meta commands
        elif root in ('leaderboard', 'lb'):
            params = {}
            if args and args[0].isdigit():
                params["limit"] = int(args[0])
            if user.player_id:
                params["current_player_id"] = user.player_id
            data, err = rest.call("leaderboard", params=params)
            if err:
                print(f"[ERROR] {err}")
            elif isinstance(data, dict):
                leaderboard = data.get("leaderboard", [])
                print(f"[LEADERBOARD] Top {len(leaderboard)} players:")
                for entry in leaderboard[:10]:
                    print(f"  #{entry['rank']}: {entry['username']} - {entry['rating']} rating, "
                          f"{entry['win_rate']:.1f}% wins")
                if data.get("current_player_rank"):
                    print(f"[RANK] Your rank: #{data['current_player_rank']}")

        elif root == "stats":
            data, err = rest.call("game_stats")
            if err:
                print(f"[ERROR] {err}")
            elif isinstance(data, dict):
                print(f"[GAME] {data.get('total_rooms', 0)} total rooms, "
                      f"{data.get('active_rooms', 0)} active, "
                      f"{data.get('total_players', 0)} players online")

        # Gameplay commands
        elif root in ("gp", "gameplay"):
            await handle_gameplay_command(cmd, args, rest, user)

        elif root == "commit":
            commit_args = [cmd] + args if cmd else list(args)
            await handle_gameplay_command("commit", commit_args, rest, user)

        elif root == "reveal":
            reveal_args = [cmd] + args if cmd else list(args)
            await handle_gameplay_command("reveal", reveal_args, rest, user)

        elif root == "emote":
            emote_args = [cmd] + args if cmd else list(args)
            await handle_gameplay_command("emote", emote_args, rest, user)

        elif root == "queue":
            queue_args = [cmd] + args if cmd else list(args)
            await handle_gameplay_command("queue", queue_args, rest, user)

        # Polling commands
        elif root == "poll":
            if not cmd:
                print("Usage: poll on|off|once")
                return True

            if cmd == "on":
                await start_polling(rest, user, cfg)
            elif cmd == "off":
                await stop_polling(user)
            elif cmd == "once":
                if not user.room_key:
                    print("[ERROR] Not in a room")
                    return True
                room_valid = await poll_events_once(rest, user, cfg)
                if not room_valid:
                    print("[POLL] Room no longer valid")

        else:
            print(f"Unknown command: {root}")

    except requests.RequestException as e:
        print(f"[HTTP ERROR] {e}")
    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}")
        traceback.print_exc()

    return True


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
    print(f"[BOOT] Config loaded from {os.environ.get('CLI_CONFIG_JSON', 'local_config.json')}")
    print(f"[BOOT] API Base: {cfg['API_BASE']}")
    
    rest = REST(cfg["API_BASE"], cfg["ENDPOINTS"])
    user = UserContext()

    print("\n===== Think Alike CLI (REST-only) =====")
    print("Quick-start:")
    print("  'p get <username>' - create/fetch player")
    print("  'r list' - list rooms") 
    print("  'r join c' - quick-join casual room")
    print("  'poll on' - start event polling")
    print("  'commit <N>' - commit choice N")
    print("  'reveal' - reveal your choice")
    print("\n'help' for all commands.")

    session = PromptSession()

    try:
        while True:
            try:
                with patch_stdout():
                    raw = await session.prompt_async("\n> ")

                if not raw.strip():
                    continue

                should_continue = await process_command(raw, rest, user, cfg)
                if not should_continue:
                    break

            except (EOFError, KeyboardInterrupt):
                await stop_polling(user)
                print("\nBye.")
                break

    finally:
        await stop_polling(user)


if __name__ == "__main__":
    asyncio.run(main())
