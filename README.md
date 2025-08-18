# Think Alike CLI

Minimal text-based CLI for a single player instance.  
Handles REST + WebSocket (Socket.IO).  
Prints every request/response and event.

## Usage

```bash
pip install requests python-socketio
python cli.py
```

Then type commands like:
- `login alice`
- `jc` (quick join casual)
- `jo` (quick join competitive)
- `l` (list rooms)
- `start`, `pick 2`, `reveal`
- `ws on` / `ws emit join_room {"room_id":"abc"}`

## Typical flow

```
> pa tal            # make/fetch player
> l                 # list rooms
> jc                # quick-join casual (HTTP)
> ws on             # connect socket
> wsjp              # emit join_player
> wsjr              # emit join_room (uses token from HTTP join)
> commit 3          # your pick
> reveal            # if allowed
```
