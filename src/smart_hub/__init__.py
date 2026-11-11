"""Typed hub snapshots, host-derived location, and synchronous/async controls."""

import asyncio
import math
from collections.abc import AsyncGenerator, Coroutine
from typing import Any, overload

import httpx2

from ._location import _host_location
from .models import HubInfo, Location, PortStatus, PowerMode, ScheduledAction

__all__ = ["Hub", "HubInfo", "Location", "PortStatus", "PowerMode", "ScheduledAction"]


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


def _settings_group(client: httpx2.Client, name: str, *, error: str) -> dict[str, Any]:
    """Read required group values without including schema secrets in errors."""
    response = client.get("/api/settings/schema")
    response.raise_for_status()
    try:
        schema = response.json()
    except ValueError:
        raise RuntimeError(error) from None
    groups = schema.get("groups") if isinstance(schema, dict) else None
    group = (
        next(
            (
                group
                for group in groups
                if isinstance(group, dict) and group.get("name") == name
            ),
            None,
        )
        if isinstance(groups, list)
        else None
    )
    values = group.get("values") if group is not None else None
    if isinstance(values, dict):
        return values
    raise RuntimeError(error)


def _port_setting(client: httpx2.Client, field: str) -> list[str]:
    """Read a required six-port string array without exposing the schema."""
    error = f"ports.{field} setting is unavailable or invalid"
    values = _settings_group(client, "ports", error=error)
    array = values.get(field)
    if (
        not isinstance(array, list)
        or len(array) != 6
        or not all(isinstance(value, str) for value in array)
        or (field == "power_mode" and any(value not in PowerMode for value in array))
    ):
        raise RuntimeError(error)
    return array


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

    def sync_location(
        self, *, city: str | None = None, country: str | None = None
    ) -> Location:
        """Persist host-derived location metadata and confirm it by read-back.

        The default city represents the configured host timezone, not actual
        geolocation; the default country is its ISO code. Nonempty overrides
        are stripped, and both are required for an unlisted timezone.
        Only whole-hour offsets from UTC-12 through UTC+14 are supported.
        The current offset includes DST but is fixed: rerun after transitions.
        This changes no clock directly and preserves other location settings.
        """
        expected = _host_location(city=city, country=country)
        with httpx2.Client(base_url=self.base_url, timeout=5.0) as client:
            response = client.post(
                "/api/settings",
                json={
                    "location": {
                        "city": expected.city,
                        "country": expected.country,
                        "utc_offset": expected.utc_offset_hours,
                    }
                },
            )
            response.raise_for_status()
            error = "location setting was not confirmed"
            values = _settings_group(client, "location", error=error)
            actual_city = values.get("city")
            actual_country = values.get("country")
            actual_offset = values.get("utc_offset")
            if (
                not isinstance(actual_city, str)
                or not isinstance(actual_country, str)
                or not isinstance(actual_offset, int)
                or isinstance(actual_offset, bool)
                or actual_city != expected.city
                or actual_country != expected.country
                or actual_offset != expected.utc_offset_hours
            ):
                raise RuntimeError(error)
            return Location(actual_city, actual_country, actual_offset)

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

    def _update_port_setting(self, port: int, field: str, value: str) -> str:
        """Write one field and confirm all entries; external writers can race."""
        with httpx2.Client(base_url=self.base_url, timeout=5.0) as client:
            expected = _port_setting(client, field)
            expected[port - 1] = value
            response = client.post("/api/settings", json={"ports": {field: expected}})
            response.raise_for_status()
            confirmed = _port_setting(client, field)
            if confirmed != expected:
                raise RuntimeError(f"ports.{field} setting was not confirmed")
            return confirmed[port - 1]

    def rename(self, port: int, name: str) -> str:
        """Set a name, returning its firmware-normalized, confirmed value.

        ASCII controls become spaces; the first 24 Unicode code points are kept
        before stripping ASCII spaces at either end. An empty name clears it.
        Only names are written, and the whole array is confirmed by read-back.
        This read-modify-write is not atomic with other settings writers.
        """
        _validate_port(port)
        if not isinstance(name, str):
            raise TypeError("name must be a string")
        normalized = "".join(
            " " if ord(char) < 0x20 or char == "\x7f" else char for char in name[:24]
        ).strip(" ")
        return self._update_port_setting(port, "names", normalized)

    def set_power_mode(self, port: int, mode: PowerMode | str) -> PowerMode:
        """Set a power mode and return its confirmed enum value.

        A changed boot mode powers on immediately; changed connect powers on if
        occupied. Manual leaves power alone. No explicit power command is sent.
        Only power_mode is written, then its whole array is confirmed by read-back.
        This read-modify-write is not atomic with other settings writers.
        """
        _validate_port(port)
        mode = PowerMode(mode)
        return PowerMode(self._update_port_setting(port, "power_mode", mode.value))

    @overload
    def watch(self, port: int, interval: float = 1.0) -> AsyncGenerator[PortStatus]: ...

    @overload
    def watch(
        self, port: None = None, interval: float = 1.0
    ) -> AsyncGenerator[list[PortStatus]]: ...

    def watch(
        self, port: int | None = None, interval: float = 1.0
    ) -> AsyncGenerator[PortStatus | list[PortStatus]]:
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
    ) -> AsyncGenerator[PortStatus | list[PortStatus]]:
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

    async def cycle(self, port: int, seconds: float = 2.0) -> bool:
        """Turn off, wait seconds, then turn on, confirming both power changes.

        Success ends on regardless of the initial state, without changing settings
        or schedules. Cancellation while waiting propagates and leaves power off;
        no cleanup power command is sent. An interrupted in-flight HTTP request
        may already have reached the hub, leaving its power state uncertain.
        """
        _validate_port(port)
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("seconds must be finite and non-negative")
        async with httpx2.AsyncClient(base_url=self.base_url, timeout=5.0) as client:
            _confirmed(
                await client.post("/api/port", params={"n": port, "on": 0}),
                port,
                False,
            )
            await asyncio.sleep(seconds)
            return _confirmed(
                await client.post("/api/port", params={"n": port, "on": 1}),
                port,
                True,
            )

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
