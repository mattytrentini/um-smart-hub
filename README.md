# smart-hub

Python library and `hub` CLI for the
[UM Smart USB Hub](https://github.com/UnexpectedMaker/um_smart_usb_hub_6).
Requires Python 3.11+; uses httpx2 and Typer. Not yet published.

## CLI

From this directory, run `uv run hub ...`. To install the CLI locally:

```sh
uv tool install .
```

```sh
hub on 1        # turn port 1 on
hub off 1       # turn port 1 off
hub toggle 1    # read the current power state and flip it once
hub toggle 1 5  # flip immediately, then every 5 seconds until Ctrl+C
hub --host http://192.168.2.124 on 1
```

The default address is `http://usb-hub.local`. Override it with `--host` (before
any command) or `HUB_HOST`. If mDNS doesn't resolve from WSL, use the IP address.
Looping mode prints a start message rather than each transition. Ctrl+C stops
without switching the port off or restoring its original state.

## Python library

Install into another uv project using a local path:

```sh
uv add /absolute/path/to/hub
```

```python
from smart_hub import Hub

hub = Hub()  # defaults to http://usb-hub.local
# hub = Hub("http://192.168.2.124")
hub.on(6)
hub.off(6)
enabled = hub.toggle(6)  # a synchronous, single flip; returns the new bool state
```

`on`, `off`, and single-flip `toggle` are blocking calls returning the confirmed
power state. Each operation closes its HTTP client; `Hub` needs no explicit
cleanup or context manager.

Supplying an interval returns a coroutine instead. It uses an asynchronous HTTP
client and `asyncio.sleep`, so it doesn't block other asyncio tasks:

```python
import asyncio
from smart_hub import Hub

async def main():
    hub = Hub()
    await hub.toggle(6, 2)  # flip immediately, then every 2 seconds indefinitely

asyncio.run(main())
```

Inside an existing async application, cancel the task when finished:

```python
from contextlib import suppress

task = asyncio.create_task(hub.toggle(6, 2))
# ... other application work ...
task.cancel()
with suppress(asyncio.CancelledError):
    await task
```

Cancellation propagates normally and doesn't send a final power command. If a
request is interrupted in flight, it may already have reached the hub.

Ports are numbered 1–6. Intervals must be finite and non-negative; zero means no
intentional delay. Invalid arguments raise `ValueError`; HTTP failures propagate
as `httpx2.HTTPError`. Each write checks the hub's reported power state, raising
`RuntimeError` if it doesn't match. CLI errors exit with code 1. Existing hub
schedules remain active and can independently change port power.

### API choice

`hub.on(6)` keeps a small, direct API. `hub[6].on()` would be useful if a retained
port object exposed several operations or measurements, but isn't implemented:
maintaining two equivalent interfaces isn't worthwhile for the current scope.

## Development

```sh
uv sync
uv run python -m unittest discover -s tests -v
uv build
```

Distribution name: `smart-hub`; import package: `smart_hub`; CLI command: `hub`.
The CLI delegates all hub controls to the library.
