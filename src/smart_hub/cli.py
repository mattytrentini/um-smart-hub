"""Typer entry point; all hub interaction is implemented by Hub."""

import asyncio
import json
from collections.abc import Callable, Coroutine, Iterator
from contextlib import aclosing, contextmanager
from dataclasses import asdict
from typing import Annotated, Any

import httpx2
import typer
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

from smart_hub import Hub, HubInfo, PortStatus

app = typer.Typer(add_completion=False, no_args_is_help=True)
Port = Annotated[int, typer.Argument(min=1, max=6, help="Port number (1-6).")]
OptionalPort = Annotated[
    int | None, typer.Argument(min=1, max=6, help="Port number; omit for all ports.")
]
JsonOutput = Annotated[bool, typer.Option("--json", help="Output JSON instead of a table.")]
console = Console(markup=False, highlight=False, emoji=False)


@app.callback()
def main(
    ctx: typer.Context,
    host: Annotated[
        str, typer.Option(envvar="HUB_HOST", help="Hub base URL.")
    ] = "http://usb-hub.local",
) -> None:
    """Control the UM Smart USB Hub's ports."""
    ctx.obj = Hub(host)


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except KeyboardInterrupt:
        typer.echo("stopped", err=True)
    except (httpx2.HTTPError, RuntimeError, ValueError) as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(1) from error


def _run(port: int, action: Callable[[], bool | Coroutine[Any, Any, None]]) -> None:
    with _errors():
        result = action()
        if isinstance(result, bool):
            typer.echo(f"port {port} {'on' if result else 'off'}")
        else:
            asyncio.run(result)


def _json(snapshot: HubInfo | PortStatus | list[PortStatus]) -> None:
    data = [asdict(port) for port in snapshot] if isinstance(snapshot, list) else asdict(snapshot)
    typer.echo(json.dumps(data, allow_nan=False))


def _value(value: object, unit: str = "") -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return f"{value}{unit}"


def _info_table(snapshot: HubInfo) -> Table:
    table = Table(title=Text(snapshot.name), show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    rows = [
        ("Firmware", snapshot.firmware),
        ("Uptime", _value(snapshot.uptime_seconds, " s")),
        ("Reset reason", snapshot.reset_reason),
        ("Main / upstream 5 V", f"{_value(snapshot.main_5v)} / {_value(snapshot.upstream_5v)}"),
        ("Total current", _value(snapshot.total_current_ma, " mA")),
        ("Total power", _value(snapshot.total_power_mw, " mW")),
        ("Temperature", _value(snapshot.temperature_c, " °C")),
        ("Humidity", _value(snapshot.humidity_percent, " %")),
        ("Clock synced", _value(snapshot.clock_synced)),
        ("Time", _value(snapshot.time_iso)),
        ("UTC offset", _value(snapshot.utc_offset_hours, " h")),
        ("Network connected", _value(snapshot.network_connected)),
        ("IP address", snapshot.ip_address),
        ("Hostname", snapshot.hostname),
        ("WiFi SSID", snapshot.ssid),
        ("WiFi RSSI", _value(snapshot.rssi_dbm, " dBm")),
        ("Expander present", _value(snapshot.expander_present)),
        ("Current monitors present", f"{_value(snapshot.current_monitor_1_present)} / {_value(snapshot.current_monitor_2_present)}"),
        ("Environment sensor present", _value(snapshot.environment_sensor_present)),
        ("Free heap / largest block", f"{snapshot.heap_free_bytes} / {snapshot.heap_largest_block_bytes} bytes"),
        ("Ports enabled / occupied / faulted", f"{snapshot.ports_enabled} / {snapshot.ports_occupied} / {snapshot.ports_faulted} of {snapshot.port_count}"),
    ]
    for label, value in rows:
        table.add_row(label, value)
    return table


def _next_action(port: PortStatus) -> str:
    action = port.next_action
    if action is None:
        return "—"
    return f"{'on' if action.enabled else 'off'} {action.local_time} ({action.delay_seconds}s)"


def _status_table(snapshot: PortStatus | list[PortStatus]) -> Table:
    if isinstance(snapshot, PortStatus):
        table = Table(title=f"Port {snapshot.number}", show_header=False)
        table.add_column("Field", style="bold")
        table.add_column("Value")
        rows = [
            ("Name", snapshot.name or "—"),
            ("Enabled", _value(snapshot.enabled)),
            ("Device present", _value(snapshot.occupied)),
            ("Fault", _value(snapshot.fault)),
            ("LED state", snapshot.led_state),
            ("Monitor present", _value(snapshot.monitor_present)),
            ("Voltage", _value(snapshot.voltage_v, " V")),
            ("Current", _value(snapshot.current_ma, " mA")),
            ("Power", _value(snapshot.power_mw, " mW")),
            ("Power mode", _value(snapshot.power_mode)),
            ("Active schedule entries", str(snapshot.schedule_count)),
            ("Next action", _next_action(snapshot)),
        ]
        for label, value in rows:
            table.add_row(label, value)
        return table
    table = Table(title="Ports — enabled is a switch state, not a supply check")
    for column in ("Port", "Name", "Enabled", "Device", "Fault", "V", "mA", "mW", "Next"):
        table.add_column(column)
    for port in snapshot:
        table.add_row(
            str(port.number), port.name or "—", _value(port.enabled),
            _value(port.occupied), _value(port.fault),
            *(_value(value) if value is not None else "—" for value in (
                port.voltage_v, port.current_ma, port.power_mw
            )),
            _next_action(port),
        )
    table.caption = "Device = plugged in; — measurement = unavailable"
    return table


@app.command()
def info(ctx: typer.Context, json_output: JsonOutput = False) -> None:
    """Show hub identity, supply, sensors, network and hardware health."""
    with _errors():
        snapshot = ctx.obj.info()
        if json_output:
            _json(snapshot)
        else:
            console.print(_info_table(snapshot), markup=False)


@app.command()
def status(
    ctx: typer.Context, port: OptionalPort = None, json_output: JsonOutput = False
) -> None:
    """Show one port's details, or a table of all ports."""
    with _errors():
        snapshot = ctx.obj.ports() if port is None else ctx.obj.status(port)
        if json_output:
            _json(snapshot)
        else:
            console.print(_status_table(snapshot), markup=False)


@app.command()
def identify(ctx: typer.Context, port: Port) -> None:
    """Flash PORT's LED without switching its power."""
    with _errors():
        ctx.obj.identify(port)
        typer.echo(f"identifying port {port}")


async def _watch(hub: Hub, port: int | None, interval: float, json_output: bool) -> None:
    async with aclosing(hub.watch(port, interval=interval)) as snapshots:
        if not json_output and console.is_terminal:
            with Live(console=console, auto_refresh=False) as live:
                async for snapshot in snapshots:
                    live.update(_status_table(snapshot), refresh=True)
        else:
            async for snapshot in snapshots:
                if json_output:
                    _json(snapshot)
                else:
                    console.print(_status_table(snapshot), markup=False)


@app.command()
def watch(
    ctx: typer.Context,
    port: OptionalPort = None,
    interval: Annotated[
        float, typer.Option(min=0, help="Polling interval in seconds; must be positive.")
    ] = 1.0,
    json_output: JsonOutput = False,
) -> None:
    """Monitor port status until Ctrl+C. --json emits one snapshot per line.

    Monitoring is read-only. Omit PORT to watch all ports.
    """
    with _errors():
        asyncio.run(_watch(ctx.obj, port, interval, json_output))


@app.command()
def on(ctx: typer.Context, port: Port) -> None:
    """Turn PORT on."""
    _run(port, lambda: ctx.obj.on(port))


@app.command()
def off(ctx: typer.Context, port: Port) -> None:
    """Turn PORT off."""
    _run(port, lambda: ctx.obj.off(port))


@app.command()
def toggle(
    ctx: typer.Context,
    port: Port,
    seconds: Annotated[
        float | None,
        typer.Argument(min=0, help="Toggle again every SECONDS until Ctrl+C."),
    ] = None,
) -> None:
    """Flip PORT's power. With SECONDS, keep flipping until Ctrl+C.

    Ctrl+C leaves the port in whatever state it was in.
    """
    if seconds is not None:
        typer.echo(f"toggling port {port} every {seconds:g} seconds; Ctrl+C to stop")
    _run(port, lambda: ctx.obj.toggle(port, seconds))
