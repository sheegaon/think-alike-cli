
# Version 12 - Final fix for client disconnect and title
import os
import json
import asyncio
import requests
import socketio
import secrets
import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from nicegui import Client, ui, app

# --- Configuration ---
def load_config():
    """Loads configuration from a JSON file specified by an environment variable."""
    default_config = {
        'API_BASE_URL': 'http://localhost:8000/api/v1',
        'WS_URL': 'http://localhost:8000',
    }
    config_path = os.environ.get("CLI_CONFIG_JSON", "heroku_config.json")
    if os.path.exists(config_path):
        with open(config_path) as f:
            user_config = json.load(f)
        mapped_config = {
            'API_BASE_URL': user_config.get('API_BASE', user_config.get('API_BASE_URL')),
            'WS_URL': user_config.get('WS_URL'),
        }
        default_config.update({k: v for k, v in mapped_config.items() if v is not None})
    return default_config

config = load_config()
API_BASE_URL = config.get('API_BASE_URL')
WS_URL = config.get('WS_URL')
PORT = int(os.environ.get('PORT', 8080))

# --- Dataclasses ---
@dataclass
class UserContext:
    player_id: Optional[int] = None
    username: Optional[str] = None
    balance: Optional[float] = None
    room_key: Optional[str] = None
    room_token: Optional[str] = None
    is_spectator: bool = False
    player_count: int = 0
    round_key: Optional[str] = None
    adjective: Optional[str] = None
    nouns: List[str] = field(default_factory=list)
    choice: Optional[int] = None
    nonce: Optional[str] = None

# --- Backend Integration ---
class REST:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip('/') if base_url else None

    def post(self, endpoint: str, data: dict) -> dict:
        if not self.base_url:
            app.get_client().loop.create_task(ui.notify('API URL not configured.', color='negative'))
            return {}
        try:
            response = requests.post(f'{self.base_url}/{endpoint}', json=data, timeout=5)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            app.get_client().loop.create_task(ui.notify(f'Backend Error: {e}', color='negative'))
            return {}

    def get(self, endpoint: str) -> dict:
        if not self.base_url:
            app.get_client().loop.create_task(ui.notify('API URL not configured.', color='negative'))
            return {}
        try:
            response = requests.get(f'{self.base_url}/{endpoint}', timeout=5)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            app.get_client().loop.create_task(ui.notify(f'Backend Error: {e}', color='negative'))
            return {}

class AsyncWS:
    def __init__(self, url: str, user_context: UserContext, ui_handlers: Dict[str, callable]):
        self.sio = socketio.AsyncClient()
        self.url = url
        self.user = user_context
        self.ui_handlers = ui_handlers
        self.register_events()

    async def connect(self):
        if not self.url:
            self._run_ui_handler('notify', 'WebSocket URL not configured.', {'color': 'negative'})
            return
        try:
            await self.sio.connect(self.url, transports=['websocket', 'polling'])
        except socketio.exceptions.ConnectionError as e:
            self._run_ui_handler('notify', f'WebSocket Connection Error: {e}', {'color': 'negative'})

    async def disconnect(self, *args):
        if self.sio.connected:
            await self.sio.disconnect()

    async def emit(self, event: str, data: dict):
        if self.sio.connected:
            await self.sio.emit(event, data)

    def _run_ui_handler(self, name: str, *args):
        if name in self.ui_handlers:
            app.get_client().loop.create_task(self.ui_handlers[name](*args))

    def register_events(self):
        @self.sio.event
        async def connect():
            self._run_ui_handler('notify', 'Connected to WebSocket', {'color': 'positive'})

        @self.sio.event
        async def disconnect(*args):
            self._run_ui_handler('notify', 'Disconnected from WebSocket', {'color': 'negative'})

        @self.sio.on('*')
        async def catch_all(event, data):
            if event in self.ui_handlers:
                self._run_ui_handler(event, data)

# --- Main Application ---
@ui.page('/')
async def main_page(client: Client):
    # --- Per-Client State ---
    user = UserContext()
    rest = REST(API_BASE_URL)

    # --- UI Update Handlers ---
    async def handle_notify(message: str, options: dict):
        ui.notify(message, **options)

    async def handle_deal(data):
        user.round_key = data.get('round_key')
        user.adjective = data.get('adjective')
        user.nouns = data.get('nouns', [])
        user.choice = None
        user.nonce = None
        await update_game_view()

    async def handle_round_results(data):
        user.balance = data.get('new_balance')
        user.choice = None
        user.nonce = None
        await update_player_display()
        await show_results(data)

    async def handle_removed_from_room(data):
        ui.notify(data.get('message', 'You have been removed from the room.'), color='warning')
        await leave_room(notify_server=False)
        
    async def handle_player_joined_room(data):
        ui.notify(f'{data.get("username")} joined the room.')
        user.player_count = data.get('player_count', user.player_count + 1)
        await update_player_display()

    async def handle_player_left_room(data):
        ui.notify(f'{data.get("username")} left the room.')
        user.player_count = data.get('player_count', user.player_count - 1)
        await update_player_display()

    async def handle_commits_update(data):
        ui.notify(f'{data.get("commits_count")}/{data.get("total_players")} players have committed.')

    # --- WebSocket Client Setup ---
    ws = AsyncWS(WS_URL, user, {
        'notify': handle_notify,
        'deal': handle_deal,
        'round_results': handle_round_results,
        'removed_from_room': handle_removed_from_room,
        'player_joined_room': handle_player_joined_room,
        'player_left_room': handle_player_left_room,
        'commits_update': handle_commits_update,
    })
    client.on_disconnect(ws.disconnect)

    # --- UI Components ---
    player_display = ui.label()
    room_list_card = ui.card()
    in_room_card = ui.card()
    game_view = ui.column()

    # --- UI Logic ---
    async def create_or_fetch_player(username_input: ui.input):
        username = username_input.value
        if not username:
            ui.notify('Please enter a username.', color='negative')
            return

        response = rest.get(f'players/username/{username}')
        if not response or 'id' not in response:
            response = rest.post('players', {'username': username})

        if response and 'id' in response:
            user.player_id = response['id']
            user.username = response['username']
            user.balance = response['balance']
            await update_player_display()
            await ws.connect()
            if ws.sio.connected:
                await ws.emit('join_player', {'player_id': user.player_id})
                await list_rooms()
        else:
            ui.notify('Could not create or fetch player. Is the backend server running?', color='negative')

    async def update_player_display():
        if user.player_id is None:
            player_display.set_text('Not logged in. Please create or fetch a player.')
            return
        balance_text = f'{user.balance:.2f}' if user.balance is not None else 'N/A'
        text = f'Player: {user.username} (ID: {user.player_id}) | Balance: {balance_text}'
        if user.room_key and isinstance(user.room_key, str):
            text += f' | In Room ({user.player_count} players)'
        player_display.set_text(text)

    async def list_rooms():
        response = rest.get('rooms')
        room_list_card.clear()
        with room_list_card:
            ui.label('Rooms').classes('text-h6')
            with ui.row():
                ui.button('Quick Join Casual', on_click=lambda: quick_join('casual'))
                ui.button('Quick Join Competitive', on_click=lambda: quick_join('competitive'))
                ui.button('Quick Join High Stakes', on_click=lambda: quick_join('high_stakes'))
            if response and 'rooms' in response:
                for room in response['rooms']:
                    with ui.card():
                        ui.label(f'Tier: {room["tier"]}, Players: {room["player_count"]}/{room["max_players"]}')
                        ui.button('Join', on_click=lambda r=room: join_room(r['room_key']))
                        ui.button('Observe', on_click=lambda r=room: observe_room(r['room_key']))
            elif not response:
                ui.label('Could not fetch rooms. Is the backend server running?').classes('text-negative')

    async def quick_join(tier: str):
        response = rest.post('rooms/quick-join', {'player_id': user.player_id, 'tier': tier})
        if response and 'room_key' in response:
            user.room_key = response['room_key']
            user.room_token = response['room_token']
            user.balance = response.get('new_balance', user.balance)
            user.is_spectator = False
            await ws.emit('join_room', {'room_token': user.room_token})
            ui.notify(f'Quick joined {tier} room.', color='positive')
            await update_player_display()

    async def join_room(room_key: str):
        response = rest.post('rooms/join', {'room_key': room_key, 'player_id': user.player_id})
        if response and 'room_token' in response:
            user.room_key = room_key
            user.room_token = response['room_token']
            user.balance = response.get('new_balance', user.balance)
            user.is_spectator = False
            await ws.emit('join_room', {'room_token': user.room_token})
            ui.notify(f'Joined room {room_key}.', color='positive')
            await update_player_display()

    async def observe_room(room_key: str):
        response = rest.post('rooms/join', {'room_key': room_key, 'player_id': user.player_id, 'as_spectator': True})
        if response and 'room_token' in response:
            user.room_key = room_key
            user.room_token = response['room_token']
            user.balance = response.get('new_balance', user.balance)
            user.is_spectator = True
            await ws.emit('join_room', {'room_token': user.room_token, 'as_spectator': True})
            ui.notify(f'Observing room {room_key}.', color='positive')
            await update_player_display()

    async def leave_room(notify_server=True):
        if user.room_key:
            if notify_server:
                rest.post('rooms/leave', {'room_key': user.room_key, 'player_id': user.player_id})
                await ws.emit('leave_room', {})
                ui.notify('Left room.', color='positive')
            user.room_key = None
            user.room_token = None
            user.is_spectator = False
            user.player_count = 0
            game_view.clear()
            await update_player_display()
            await list_rooms()

    async def skip_round():
        if user.room_key:
            rest.post('rooms/skip', {'room_key': user.room_key, 'player_id': user.player_id})
            ui.notify('Skipping next round.', color='positive')

    async def update_game_view():
        game_view.clear()
        with game_view:
            if user.adjective and user.nouns:
                ui.label(f'Adjective: {user.adjective}').classes('text-2xl')
                if user.is_spectator:
                    ui.label('You are spectating.')
                    with ui.grid(columns=4):
                        for noun in user.nouns:
                            with ui.card():
                                ui.label(noun)
                else:
                    ui.label('Choose a noun:').classes('text-lg')
                    with ui.grid(columns=4):
                        for i, noun in enumerate(user.nouns):
                            button = ui.button(noun, on_click=lambda i=i: select_card(i))
                            if user.choice is not None:
                                button.disable()
                                if i == user.choice:
                                    button.props('color=primary')

    async def select_card(choice: int):
        user.choice = choice
        user.nonce = secrets.token_hex(16)
        payload = f'{user.player_id}{user.round_key}{user.choice}{user.nonce}'.encode('utf-8')
        commit_hash = hashlib.sha256(payload).hexdigest()
        await ws.emit('commit', {'hash': commit_hash})
        ui.notify(f'You chose: {user.nouns[choice]}', color='positive')
        await update_game_view()

    async def show_results(data: dict):
        game_view.clear()
        with game_view:
            with ui.card():
                ui.label('Round Results').classes('text-2xl')
                your_choice_index = data.get("your_choice")
                your_choice_text = "N/A"
                if your_choice_index is not None and user.nouns and your_choice_index < len(user.nouns):
                    your_choice_text = user.nouns[your_choice_index]
                ui.label(f'Your choice: {your_choice_text}')
                ui.label(f'Payout: {data.get("payout", 0):.2f}')
                ui.label(f'New Balance: {data.get("new_balance", 0):.2f}')

    # --- Page Layout ---
    ui.label('Think Alike').classes('text-h4')
    with ui.card():
        ui.label('Player').classes('text-h6')
        username_input = ui.input('Username', placeholder='Enter your username')
        ui.button('Create/Fetch Player', on_click=lambda: create_or_fetch_player(username_input))
        player_display.classes('text-lg mt-2')
        await update_player_display()

    room_list_card.bind_visibility(user, 'room_key', forward=lambda value: value is None)
    with room_list_card:
        pass

    in_room_card.bind_visibility(user, 'room_key', forward=lambda value: value is not None)
    with in_room_card:
        ui.label('Game').classes('text-h6')
        with ui.row():
            ui.button('Leave Room', on_click=leave_room)
            ui.button('Skip Round', on_click=skip_round).bind_visibility(user, 'is_spectator', forward=lambda value: not value)
        game_view.classes('mt-4')

    with ui.card():
        ui.label('Quests & Leaderboard').classes('text-h6')
        ui.label('TODO: Implement Quests and Leaderboard UI')

# --- Main ---
if __name__ in {'__main__', '__mp_main__'}:
    ui.run(host='0.0.0.0', port=PORT)
