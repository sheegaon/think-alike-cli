import asyncio
import random
import json
import argparse
from datetime import datetime, timezone
from dateutil.parser import isoparse
from enum import Enum
from typing import Optional

from cli import UserContext, REST, AsyncWS, load_config, process_command


class BotState(Enum):
    INITIALIZING = "initializing"
    IN_ROOM = "in_room"
    PLAYING = "playing" 
    WAITING = "waiting"
    LEAVING = "leaving"
    DISCONNECTED = "disconnected"


class AIBot:
    """AI bot that plays Think Alike game automatically"""
    
    def __init__(self, cfg: dict, tier: str = "casual", bot_id: Optional[str] = None):
        self.cfg = cfg
        self.tier = tier
        self.bot_id = bot_id or f"AI_Bot_{random.randint(1000, 9999)}"
        
        self.rest = REST(cfg["API_BASE"], cfg["ENDPOINTS"])
        self.user = UserContext()
        self.ws = AsyncWS(cfg["WS_URL"], cfg["WS_NAMESPACE"], user=self.user)
        
        self.state = BotState.INITIALIZING
        self.running = True
        self.current_tasks = set()
        
        # Bot behavior settings
        self.max_players_threshold = 7
        self.min_wait_before_leave = 0  # seconds
        self.max_wait_before_leave = 10  # seconds
        
        # Enhanced WebSocket event handlers
        self._setup_enhanced_ws_handlers()
    
    def _setup_enhanced_ws_handlers(self):
        """Setup additional WebSocket event handlers for bot behavior"""
        
        # Store original handlers to call them first
        original_handlers = {}
        
        # Get existing deal handler if it exists
        if hasattr(self.ws.sio, '_handlers') and self.ws.ns in self.ws.sio._handlers:
            ns_handlers = self.ws.sio._handlers[self.ws.ns]
            if 'deal' in ns_handlers:
                original_handlers['deal'] = ns_handlers['deal']
            if 'next_round_info' in ns_handlers:
                original_handlers['next_round_info'] = ns_handlers['next_round_info']
        
        @self.ws.sio.on("deal", namespace=self.ws.ns)
        async def on_deal_enhanced(data):
            """Enhanced deal handler for bot auto-play"""
            print(f"[BOT] Deal received: {data.get('adjective')} + {len(data.get('nouns', []))} nouns")
            
            # Call original handler first to maintain user context
            if 'deal' in original_handlers:
                await original_handlers['deal'](data)
            else:
                # Fallback: manually update user context like original handler
                if self.user and isinstance(data, dict):
                    self.user.round_key = data.get("round_key")
                    self.user.choice = None
                    self.user.nonce = None
                    self.user.adjective = data.get("adjective")
                    self.user.nouns = data.get("nouns", [])
                    self.user.round_phase = "deal"
            
            if self.state == BotState.IN_ROOM:
                self.state = BotState.PLAYING
                
            # Auto-commit after a short random delay (1-3 seconds) if we have nouns
            if self.user.nouns:
                delay = random.uniform(1.0, 3.0)
                task = asyncio.create_task(self._auto_commit(delay))
                self.current_tasks.add(task)
                task.add_done_callback(self.current_tasks.discard)
            else:
                print(f"[BOT] Warning: No nouns received in deal data")
        
        @self.ws.sio.on("next_round_info", namespace=self.ws.ns)
        async def on_next_round_info_enhanced(data):
            """Enhanced next round handler for leave decision"""
            print(f"[BOT] Next round info: {json.dumps(data)}")
            
            # Call original handler first
            if 'next_round_info' in original_handlers:
                await original_handlers['next_round_info'](data)
            else:
                # Fallback: update context manually
                if self.user:
                    self.user.round_phase = "waiting"
                    self.user.next_round_info = data
            
            if self.state == BotState.PLAYING:
                self.state = BotState.WAITING
                
            # Schedule leave decision check
            player_count = data.get("player_count", 0)
            if player_count > self.max_players_threshold:
                # Random wait time to avoid all bots leaving simultaneously
                wait_time = random.uniform(self.min_wait_before_leave, self.max_wait_before_leave)
                
                try:
                    start_time_str = data.get("start_time")
                    if start_time_str:
                        start_time = isoparse(start_time_str)
                        now = datetime.now(timezone.utc)
                        time_until_start = (start_time - now).total_seconds()
                        
                        # Ensure we don't wait longer than time until start
                        wait_time = min(wait_time, max(0, int(time_until_start - 1)))
                        
                except Exception as e:
                    print(f"[BOT] Error parsing start time: {e}")
                    # Fallback to original wait time
                
                print(f"[BOT] Too many players ({player_count}), scheduling leave in {wait_time:.1f}s")
                task = asyncio.create_task(self._schedule_leave(wait_time))
                self.current_tasks.add(task)
                task.add_done_callback(self.current_tasks.discard)
        
        @self.ws.sio.on("removed_from_room", namespace=self.ws.ns)
        async def on_removed_enhanced(data):
            """Handle being removed from room"""
            print(f"[BOT] Removed from room: {data}")
            self.state = BotState.DISCONNECTED
            await self._cleanup_tasks()
        
        @self.ws.sio.on("room_left", namespace=self.ws.ns)
        async def on_room_left_enhanced(data):
            """Handle successful room leave"""
            print(f"[BOT] Left room successfully")
            self.state = BotState.DISCONNECTED
            await self._cleanup_tasks()
    
    async def _auto_commit(self, delay: float):
        """Auto-commit a random choice after delay"""
        try:
            await asyncio.sleep(delay)
            
            if (self.user.nouns and self.state == BotState.PLAYING and 
                self.user.round_key and self.user.player_id):
                
                choice = random.randint(0, len(self.user.nouns) - 1)
                print(f"[BOT] Auto-committing choice {choice} ({self.user.nouns[choice]})")
                
                # Use process_command abstraction instead of direct WebSocket operations
                await process_command(f"ws commit {choice}", self.rest, self.ws, self.user)
                
            else:
                missing_fields = []
                if not self.user.nouns:
                    missing_fields.append("nouns")
                if not self.user.round_key:
                    missing_fields.append("round_key")
                if not self.user.player_id:
                    missing_fields.append("player_id")
                if self.state != BotState.PLAYING:
                    missing_fields.append(f"state={self.state}")
                print(f"[BOT] Cannot auto-commit: missing {missing_fields}")
                
        except Exception as e:
            print(f"[BOT] Error in auto-commit: {e}")
    
    async def _schedule_leave(self, wait_time: float):
        """Schedule leaving the room after wait_time seconds"""
        try:
            await asyncio.sleep(wait_time)
            
            if self.state in [BotState.WAITING, BotState.PLAYING] and self.user.room_key:
                print(f"[BOT] Leaving room due to high player count")
                self.state = BotState.LEAVING
                await process_command("room leave immediate", self.rest, self.ws, self.user)
                
        except Exception as e:
            print(f"[BOT] Error in schedule_leave: {e}")
    
    async def _cleanup_tasks(self):
        """Cancel all current tasks"""
        for task in self.current_tasks.copy():
            if not task.done():
                task.cancel()
        self.current_tasks.clear()
    
    async def initialize(self):
        """Initialize the bot - connect and create player"""
        try:
            print(f"[BOT] Initializing bot: {self.bot_id}")
            
            # Connect WebSocket
            await self.ws.connect_async()
            await asyncio.sleep(1)  # Allow connection to establish
            
            # Create/get player
            await process_command(f"player get {self.bot_id}", self.rest, self.ws, self.user)
            
            if not self.user.player_id:
                raise Exception("Failed to create/get player")
            
            print(f"[BOT] Initialized as player {self.user.player_id} ({self.user.username})")
            return True
            
        except Exception as e:
            print(f"[BOT] Initialization failed: {e}")
            return False
    
    async def join_room(self):
        """Join a game room"""
        try:
            print(f"[BOT] Joining {self.tier} room")
            
            await process_command(f"room join {self.tier}", self.rest, self.ws, self.user)
            
            if self.user.room_key:
                self.state = BotState.IN_ROOM
                print(f"[BOT] Joined room ...{self.user.room_key[-5:]}")
                return True
            else:
                print(f"[BOT] Failed to join room")
                return False
                
        except Exception as e:
            print(f"[BOT] Error joining room: {e}")
            return False
    
    async def run(self):
        """Main bot loop"""
        try:
            # Initialize
            if not await self.initialize():
                return
            
            # Join room
            if not await self.join_room():
                return
            
            # Main game loop
            print(f"[BOT] Starting main game loop")
            while self.running:
                try:
                    # Check if we're still connected and in a room
                    if not self.ws.connected:
                        print(f"[BOT] WebSocket disconnected, attempting reconnect")
                        await self.ws.connect_async()
                        await asyncio.sleep(1)
                        continue
                    
                    # If we left/were removed from room, try to join another
                    if self.state == BotState.DISCONNECTED and not self.user.room_key:
                        await asyncio.sleep(random.uniform(2, 5))  # Wait before rejoining
                        if await self.join_room():
                            continue
                        else:
                            # If we can't join, wait longer and try again
                            await asyncio.sleep(10)
                            continue
                    
                    # Main loop - just wait and let event handlers do the work
                    await asyncio.sleep(1)
                    
                except KeyboardInterrupt:
                    print(f"[BOT] Interrupted by user")
                    break
                except Exception as e:
                    print(f"[BOT] Error in main loop: {e}")
                    await asyncio.sleep(5)  # Wait before retrying
            
        except Exception as e:
            print(f"[BOT] Fatal error: {e}")
        
        finally:
            await self.cleanup()
    
    async def cleanup(self):
        """Clean up bot resources"""
        print(f"[BOT] Cleaning up...")
        self.running = False
        
        # Cancel all tasks
        await self._cleanup_tasks()
        
        # Leave room if still in one
        if self.user.room_key and self.state not in [BotState.LEAVING, BotState.DISCONNECTED]:
            try:
                await process_command("room leave immediate", self.rest, self.ws, self.user)
            except Exception as e:
                print(f"[BOT] Error leaving room during cleanup: {e}")
        
        # Disconnect WebSocket
        if self.ws and self.ws.connected:
            await self.ws.disconnect_async()
        
        print(f"[BOT] Cleanup complete")
    
    def stop(self):
        """Stop the bot"""
        self.running = False


async def main():
    """Main entry point for single bot"""
    parser = argparse.ArgumentParser(
        description="Think Alike AI Bot - Automated player for the Think Alike game",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python ai_cli_bot.py                           # Run single bot in casual tier
  python ai_cli_bot.py --tier competitive        # Run single bot in competitive tier  
  python ai_cli_bot.py --bot-id "MyBot_123"      # Run bot with specific ID
  python ai_cli_bot.py multi                     # Run 3 bots concurrently
  python ai_cli_bot.py multi --count 5           # Run 5 bots concurrently
  python ai_cli_bot.py multi --tier high_stakes  # Run bots in high stakes tier
        """
    )
    
    subparsers = parser.add_subparsers(dest='mode', help='Bot operation mode')
    
    # Single bot mode (default)
    single_parser = subparsers.add_parser('single', help='Run a single bot (default mode)')
    single_parser.add_argument('--tier', '-t', 
                              choices=['casual', 'competitive', 'high_stakes'],
                              default='casual',
                              help='Game tier to join (default: casual)')
    single_parser.add_argument('--bot-id', '-b',
                              help='Specific bot ID to use (default: auto-generated)')
    
    # Multi-bot mode  
    multi_parser = subparsers.add_parser('multi', help='Run multiple bots concurrently')
    multi_parser.add_argument('--count', '-c', type=int, default=3,
                             help='Number of bots to run (default: 3)')
    multi_parser.add_argument('--tier', '-t',
                             choices=['casual', 'competitive', 'high_stakes'], 
                             default='casual',
                             help='Game tier for bots to join (default: casual)')
    
    # Legacy positional arguments for backward compatibility
    parser.add_argument('legacy_mode', nargs='?', 
                       help='Legacy mode: "multi" for multi-bot mode')
    parser.add_argument('legacy_count', nargs='?', type=int,
                       help='Legacy count argument for multi-bot mode')
    parser.add_argument('legacy_tier', nargs='?',
                       help='Legacy tier argument')
    
    args = parser.parse_args()
    
    # Handle legacy argument format for backward compatibility
    if args.mode == 'multi':
        print(f"[MULTI] Starting {args.count} bots in {args.tier} tier")
        await run_multiple_bots(args.count, args.tier)
        return
    else:
        # Single bot mode (default)
        tier = getattr(args, 'tier', 'casual')
        bot_id = getattr(args, 'bot_id', None)
    
    cfg = load_config()
    bot = AIBot(cfg, tier=tier, bot_id=bot_id)
    
    try:
        await bot.run()
    except KeyboardInterrupt:
        print("\n[BOT] Interrupted by user")
    finally:
        await bot.cleanup()


async def run_multiple_bots(count: int = 3, tier: str = "casual"):
    """Run multiple bots concurrently for testing"""
    cfg = load_config()
    
    bots = []
    tasks = []
    
    try:
        # Create and start bots with staggered timing
        for i in range(count):
            bot_id = f"AI_Bot_Multi_{i+1}_{random.randint(100, 999)}"
            bot = AIBot(cfg, tier=tier, bot_id=bot_id)
            bots.append(bot)
            
            # Start bot with slight delay to avoid simultaneous connections
            await asyncio.sleep(random.uniform(0.5, 2.0))
            task = asyncio.create_task(bot.run())
            tasks.append(task)
        
        # Wait for all bots to complete
        await asyncio.gather(*tasks, return_exceptions=True)
        
    except KeyboardInterrupt:
        print(f"\n[MULTI] Interrupted, stopping {len(bots)} bots...")
        
        # Stop all bots
        for bot in bots:
            bot.stop()
        
        # Wait for cleanup
        await asyncio.gather(*tasks, return_exceptions=True)
    
    finally:
        # Ensure cleanup
        for bot in bots:
            await bot.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
