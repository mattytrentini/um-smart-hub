"""Synchronous port controls and asynchronous toggling for the UM Smart USB Hub."""

import asyncio
import math
from collections.abc import Coroutine
from typing import Any, overload

import httpx2

__all__ = ["Hub"]


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
    Clients are scoped to each operation, so no explicit close() is needed.
    """

    def __init__(self, base_url: str = "http://usb-hub.local") -> None:
        self.base_url = base_url

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
