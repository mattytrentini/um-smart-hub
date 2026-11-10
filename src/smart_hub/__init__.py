"""Typed hub snapshots, port identification, and synchronous/async controls."""

import asyncio
import math
from collections.abc import AsyncIterator, Coroutine
from typing import Any, overload

import httpx2

from .models import HubInfo, PortStatus, ScheduledAction

__all__ = ["Hub", "HubInfo", "PortStatus", "ScheduledAction"]


def _validate_port(port: int) -> None:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 6:
        raise ValueError("port must be an integer from 1 to 6")


def _confirmed(response: httpx2.Response, port: int, enabled: bool) -> bool:
    response.raise_for_status()
    # Firmware can silently ignore a write and still return HTTP 200.
    actual = response.json()["ports"][port - 1]["enabled"]
    if actual is not enabled:
        raise RuntimeError(f"port {port} reports enabled={actual}, expected {enabled}")
    return actual


class Hub:
    """Control a hub at base_url (default http://usb-hub.local).

    HTTP failures raise httpx2.HTTPError. Rejected state changes raise RuntimeError.
    Clients are scoped to each operation; watch holds one until its iterator closes.
    """

    def __init__(self, base_url: str = "http://usb-hub.local") -> None:
        self.base_url = base_url

    def info(self) -> HubInfo:
        """Fetch an immutable hub-wide snapshot, not a live view."""
        with httpx2.Client(base_url=self.base_url, timeout=5.0) as client:
            response = client.get("/api/hub")
            response.raise_for_status()
            return HubInfo.from_api(response.json())

    def ports(self) -> list[PortStatus]:
        """Fetch all port snapshots; bulk data does not include power modes."""
        with httpx2.Client(base_url=self.base_url, timeout=5.0) as client:
            response = client.get("/api/ports")
            response.raise_for_status()
            return [PortStatus.from_api(data) for data in response.json()["ports"]]

    def status(self, port: int) -> PortStatus:
        """Fetch one immutable port snapshot, including its power mode."""
        _validate_port(port)
        with httpx2.Client(base_url=self.base_url, timeout=5.0) as client:
            response = client.get(f"/api/ports/{port}")
            response.raise_for_status()
            return PortStatus.from_api(response.json())

    def identify(self, port: int) -> None:
        """Flash a port's identification LED without changing its power state."""
        _validate_port(port)
        with httpx2.Client(base_url=self.base_url, timeout=5.0) as client:
            response = client.post("/api/identify", params={"n": port})
            response.raise_for_status()
            if response.json().get("ok") is not True:
                raise RuntimeError(f"port {port} identification was not acknowledged")

    def watch(
        self, port: int | None = None, interval: float = 1.0
    ) -> AsyncIterator[PortStatus | list[PortStatus]]:
        """Poll fresh snapshots immediately, then sleep interval between polls.

        One async client lives for the iterator's lifetime. Cancellation and HTTP
        errors propagate and close it without changing power. Close the iterator
        with aclose() (or contextlib.aclosing) when stopping consumption early.
        Each yielded value is a separate snapshot, not an updating live view;
        all-port snapshots omit power modes just like ports().
        """
        if port is not None:
            _validate_port(port)
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("interval must be finite and strictly positive")
        return self._watch_loop(port, interval)

    async def _watch_loop(
        self, port: int | None, interval: float
    ) -> AsyncIterator[PortStatus | list[PortStatus]]:
        """Keep a single read-only client until cancellation or iterator closure."""
        path = "/api/ports" if port is None else f"/api/ports/{port}"
        async with httpx2.AsyncClient(base_url=self.base_url, timeout=5.0) as client:
            while True:
                response = await client.get(path)
                response.raise_for_status()
                data = response.json()
                if port is None:
                    yield [PortStatus.from_api(item) for item in data["ports"]]
                else:
                    yield PortStatus.from_api(data)
                await asyncio.sleep(interval)

    def _set(self, port: int, enabled: bool) -> bool:
        _validate_port(port)
        with httpx2.Client(base_url=self.base_url, timeout=5.0) as client:
            return _confirmed(
                client.post("/api/port", params={"n": port, "on": int(enabled)}),
                port,
                enabled,
            )

    def on(self, port: int) -> bool:
        """Turn port on and return its confirmed power state (True)."""
        return self._set(port, True)

    def off(self, port: int) -> bool:
        """Turn port off and return its confirmed power state (False)."""
        return self._set(port, False)

    @overload
    def toggle(self, port: int, seconds: None = None) -> bool: ...

    @overload
    def toggle(self, port: int, seconds: float) -> Coroutine[Any, Any, None]: ...

    def toggle(
        self, port: int, seconds: float | None = None
    ) -> bool | Coroutine[Any, Any, None]:
        """Flip once synchronously, or return an async loop when seconds is given.

        The loop flips immediately, then waits seconds between changes. Cancel
        its task to stop; cancellation never deliberately changes the final state.
        A request interrupted in flight may already have reached the hub.
        """
        _validate_port(port)
        if seconds is not None:
            if not math.isfinite(seconds) or seconds < 0:
                raise ValueError("seconds must be finite and non-negative")
            return self._toggle_loop(port, seconds)
        with httpx2.Client(base_url=self.base_url, timeout=5.0) as client:
            response = client.get(f"/api/ports/{port}")
            response.raise_for_status()
            enabled = not response.json()["enabled"]
            return _confirmed(
                client.post("/api/port", params={"n": port, "on": int(enabled)}),
                port,
                enabled,
            )

    async def _toggle_loop(self, port: int, seconds: float) -> None:
        async with httpx2.AsyncClient(base_url=self.base_url, timeout=5.0) as client:
            response = await client.get(f"/api/ports/{port}")
            response.raise_for_status()
            enabled = not response.json()["enabled"]
            while True:
                _confirmed(
                    await client.post(
                        "/api/port", params={"n": port, "on": int(enabled)}
                    ),
                    port,
                    enabled,
                )
                await asyncio.sleep(seconds)
                enabled = not enabled
