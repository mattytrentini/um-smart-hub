# um-smart-hub

Python library and `hub` CLI for the
[UM Smart USB Hub](https://github.com/UnexpectedMaker/um_smart_usb_hub_6).
Requires Python 3.14+; uses httpx2 and Typer.

## CLI

From this directory, run `uv run hub ...`. To install the CLI from PyPI:

```sh
uv tool install um-smart-hub
```

To install from a local checkout instead, use `uv tool install .`.

```sh
hub on 1        # turn port 1 on
hub off 1       # turn port 1 off
hub toggle 1    # read the current power state and flip it once
hub toggle 1 5  # flip immediately, then every 5 seconds until Ctrl+C
hub --host http://192.168.2.124 on 1
hub info                  # hub identity, supplies, sensors and hardware health
hub status                # all six ports
hub status 6              # one port's details and next scheduled action
hub identify 6            # flash the port's LED without changing power
hub watch                 # refresh all port statuses every second
hub watch 6 --interval 0.5 # monitor one port until Ctrl+C
hub info --json
hub status 6 --json
hub watch 6 --json        # one JSON snapshot per line
```

The default address is `http://usb-hub.local`. Override it with `--host` (before
any command) or `HUB_HOST`. If mDNS doesn't resolve from WSL, use the IP address.
Looping mode prints a start message rather than each transition. Ctrl+C stops
without switching the port off or restoring its original state.

`info` and `status` emit human-readable tables by default. `watch` refreshes a
table in an interactive terminal, or prints successive tables when redirected.
`--json` uses the library's normalized, unit-labelled fields, not raw firmware
field names: `info` and single-port `status` emit an object; all-port `status`
emits an array. Each `watch --json` line is one object (selected port) or array
(all ports). Diagnostics and the Ctrl+C message go to stderr, keeping stdout
machine-readable.

## Python library

Install into another uv project:

```sh
uv add um-smart-hub
```

For local development, use `uv add /absolute/path/to/hub` instead.

```python
from smart_hub import Hub

hub = Hub()  # defaults to http://usb-hub.local
# hub = Hub("http://192.168.2.124")
hub.on(6)
hub.off(6)
enabled = hub.toggle(6)  # a synchronous, single flip; returns the new bool state
```

`on`, `off`, and single-flip `toggle` are blocking calls returning the confirmed
power state. Synchronous operations close their HTTP clients; `Hub` itself
needs no explicit cleanup or context manager.

### Information, status and identification

```python
from smart_hub import Hub, HubInfo, PortStatus

hub = Hub()
info: HubInfo = hub.info()
ports: list[PortStatus] = hub.ports()
port: PortStatus = hub.status(6)
print(info.firmware, info.temperature_c, info.main_5v)
print(port.enabled, port.occupied, port.current_ma, port.next_action)
hub.identify(6)  # flashes the LED; does not change power
```

`HubInfo`, `PortStatus` and `ScheduledAction` are frozen dataclasses. They are
snapshots, not live views; fetch again to refresh them. Use
`dataclasses.asdict(snapshot)` for a JSON-compatible dictionary.

- `enabled` means the firmware's switch state, not proof that a 5 V supply or
  current is present. Check `HubInfo.main_5v` separately.
- Absent port monitors yield `None` for `voltage_v`, `current_ma` and `power_mw`.
  Real zero readings remain `0.0`.
- Invalid environment readings yield `None` for `temperature_c` and
  `humidity_percent`; an unsynced clock yields `None` for `time_iso`.
- `power_mode` is available from `status(port)`, but is `None` in `ports()` and
  all-port watches because the bulk endpoint doesn't supply it.
- `next_action` is a `ScheduledAction` or `None`. It exposes `delay_seconds`,
  `enabled` (the action's target state), `local_time` and `entry_index`.
  `schedule_count` counts active entries; full schedule editing isn't included.

### Asynchronous monitoring

`watch(port=None, interval=1.0)` is an asynchronous iterator. It fetches
immediately, waits between polls, and yields a fresh `PortStatus` for one port or
a `list[PortStatus]` for all ports. It never changes power.

```python
import asyncio
from contextlib import aclosing
from smart_hub import Hub


async def monitor():
    hub = Hub()
    async with aclosing(hub.watch(6, interval=0.5)) as snapshots:
        async for port in snapshots:
            print(port.voltage_v, port.current_ma, port.fault)
            # Break when your application is finished monitoring.


asyncio.run(monitor())
```

The iterator holds one async HTTP client. Cancellation closes it and propagates
normally. Use `aclosing` as above when you may break out early, so the client is
closed immediately. Polling errors propagate; there are no automatic retries.
Watch intervals must be finite and strictly positive.

### Asynchronous toggling

Supplying an interval to `toggle` returns a coroutine instead. It uses an asynchronous HTTP
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

Ports are numbered 1–6. Toggle intervals must be finite and non-negative; zero
means no intentional delay. Invalid arguments raise `ValueError`; HTTP failures
propagate as `httpx2.HTTPError`. Power writes check the hub's reported state,
raising `RuntimeError` if it doesn't match; `identify` checks the acknowledgement.
CLI errors exit with code 1. Existing hub schedules remain active and can
independently change port power.

### API choice

`hub.on(6)` keeps a small, direct API. `hub[6].on()` would be useful if a retained
port object exposed several operations or measurements, but isn't implemented:
maintaining two equivalent interfaces isn't worthwhile for the current scope.

## Development

```sh
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked ty check
uv run --locked python -m unittest discover -s tests -v
uv build
```

Ruff and ty are uv development dependencies. Ruff checks lint, import ordering,
Python modernization, and formatting; ty checks the library, CLI, and tests
using Python 3.14. To apply Ruff's safe fixes and formatting locally:

```sh
uv run ruff check --fix .
uv run ruff format .
```

`.github/workflows/ci.yml` runs lint, formatting, types, and regression checks on
pull requests and pushes to `main`. The release workflow calls the same CI
workflow before building distributions, so publishing cannot bypass those
checks. No pre-commit hook is required.

Distribution name: `um-smart-hub`; import package: `smart_hub`; CLI command: `hub`.
The CLI delegates all hub controls to the library.

## Releases and PyPI publishing

`.github/workflows/release.yml` runs when a `v*` tag is **pushed**. It first calls
the shared CI workflow to run Ruff, ty, and the regression suite on Python 3.14
with locked dependencies. It then checks that the tag exactly matches `v` plus
`[project].version`, builds a wheel and source distribution, and checks their
metadata with Twine. A separate job publishes those artifacts to PyPI using
Trusted Publishing. Only after publishing succeeds does it create a GitHub
release with generated notes and attach the same distributions.

Publishing uses short-lived GitHub OIDC credentials, not a stored PyPI API token.
Only the publishing job has `id-token: write`; only the GitHub release job has
`contents: write`. Actions are pinned to commit hashes.

### One-time setup before the first release

1. The PyPI distribution name is **`um-smart-hub`** (`[project].name` in
   `pyproject.toml`). The explicit build-backend `module-name = "smart_hub"`
   keeps the import package independent of the distribution name. The CLI
   remains `hub`.
2. In this repository's GitHub settings, create an environment named **`pypi`**.
   Restrict deployments to release tags (`v*`); requiring reviewer approval is
   recommended.
3. In your [PyPI publishing settings](https://pypi.org/manage/account/publishing/),
   add a **pending Trusted Publisher** for `um-smart-hub`:

   | Setting | Value |
   |---|---|
   | PyPI project | `um-smart-hub` |
   | Owner | `mattytrentini` |
   | Repository | `um-smart-hub` |
   | Workflow filename | `release.yml` |
   | Environment | `pypi` |

   For an existing PyPI project you own, add the same publisher in that project's
   publishing settings instead. A pending publisher creates a new project on the
   first successful upload; it **does not reserve its name** in advance. See the
   [PyPI setup guide](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/).
4. Commit and push the workflow, package metadata, and lockfile before pushing
   a release tag.

### Creating a release

For an initial `0.1.0` release, after completing the setup above:

```sh
uv version 0.1.0
uv lock
# Commit any name/version/lockfile changes, then push the release commit.
git push origin main
git tag -a v0.1.0 -m "Release 0.1.0"
git push origin v0.1.0
```

Use a new package version and matching tag for each subsequent release. Creating
a local tag alone does not publish anything. Never reuse a version already
uploaded to PyPI. The workflow deliberately has no manual-dispatch trigger.
