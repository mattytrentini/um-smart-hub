"""Typer entry point; all port control is implemented by Hub."""

import asyncio
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

import httpx2
import typer

from smart_hub import Hub

app = typer.Typer(add_completion=False, no_args_is_help=True)
Port = Annotated[int, typer.Argument(min=1, max=6, help="Port number (1-6).")]


@app.callback()
def main(
    ctx: typer.Context,
    host: Annotated[
        str, typer.Option(envvar="HUB_HOST", help="Hub base URL.")
    ] = "http://usb-hub.local",
) -> None:
    """Control the UM Smart USB Hub's ports."""
    ctx.obj = Hub(host)


def _run(port: int, action: Callable[[], bool | Coroutine[Any, Any, None]]) -> None:
    try:
        result = action()
        if isinstance(result, bool):
            typer.echo(f"port {port} {'on' if result else 'off'}")
        else:
            asyncio.run(result)
    except KeyboardInterrupt:
        typer.echo("stopped")
    except (httpx2.HTTPError, RuntimeError, ValueError) as error:
        typer.echo(f"error: {error}", err=True)
        raise typer.Exit(1) from error


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
