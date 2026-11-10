"""Transport-backed regressions for hub snapshots, identification, and polling."""

import asyncio
import unittest
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict
from typing import Any
from unittest.mock import patch

import httpx2

from smart_hub import Hub, HubInfo, PortStatus, ScheduledAction

HUB_DATA: dict[str, Any] = {
    "name": "Smart USB Hub",
    "fw": "1.0.0",
    "uptime_s": 838,
    "reset_reason": "POWERON",
    "power": {
        "main_5v": True,
        "upstream_5v": True,
        "total_ma": 452.0,
        "total_mw": 2260.0,
    },
    "sensors": {"temp_c": 30.8, "humidity": 29.6, "valid": True},
    "clock": {
        "synced": True,
        "iso": "2026-08-02T16:33:38+10:00",
        "time": "16:33",
        "date": "Sun 02 Aug 2026",
        "wday": 0,
        "secs": 59618,
        "utc_offset": 10,
    },
    "network": {
        "connected": True,
        "ip": "192.168.1.100",
        "ssid": "MyNetwork",
        "rssi": -54,
        "hostname": "usb-hub",
    },
    "devices": {"expander": True, "ina_1": True, "ina_2": True, "aht20": True},
    "memory": {"heap_free": 209800, "heap_max_block": 163828},
    "ports_summary": {"count": 6, "powered": 4, "occupied": 3, "faulted": 1},
}


def port_data(number=6, **overrides):
    """Return an independent, complete single-port firmware response."""
    data = {
        "n": number,
        "enabled": True,
        "occupied": True,
        "fault": False,
        "state": "green",
        "name": "Raspberry Pi" if number == 6 else "",
        "monitor": True,
        "volts": 5.048,
        "ma": 120.4,
        "mw": 607.9,
        "power_mode": "manual",
        "schedule": {
            "count": 2,
            "next": {"in_s": 82, "on": False, "at": "Sun 16:35", "entry": 0},
            "entries": [
                {
                    "days": 127,
                    "time": "07:20",
                    "mode": "on_for",
                    "dur": 60,
                    "until": "17:00",
                    "every": 30,
                    "allday": True,
                    "once": False,
                    "enabled": True,
                    "invalid": False,
                },
                {
                    "days": 8,
                    "time": "22:00",
                    "mode": "off",
                    "dur": 60,
                    "until": "17:00",
                    "every": 30,
                    "allday": True,
                    "once": False,
                    "enabled": True,
                    "invalid": True,
                    "invalid_reason": "clash",
                },
            ],
        },
    }
    data.update(deepcopy(overrides))
    return data


def bulk_port_data():
    """Return the six distinct port states without detail-only fields."""
    ports = [
        port_data(1, enabled=False, occupied=False, state="off", volts=0, ma=0, mw=0),
        port_data(2, occupied=False, state="pulse", volts=5.072, ma=0.4, mw=2.0),
        port_data(3, fault=True, state="fault", volts=4.608, ma=1980.2, mw=9129.1),
        port_data(4, enabled=False, state="blue", monitor=False, volts=0, ma=0, mw=0),
        port_data(5, occupied=False, state="pinkpulse", volts=0.024, ma=0, mw=0),
        port_data(),
    ]
    for port in ports:
        del port["power_mode"]
        if port["n"] < 5:
            port["schedule"] = {"count": 0, "next": None}
        else:
            del port["schedule"]["entries"]
    ports[4]["schedule"] = {
        "count": 1,
        "next": {"in_s": 3542, "on": False, "at": "Sun 17:30", "entry": 0},
    }
    return {"ports": ports}


class ClosingTransport(httpx2.MockTransport):
    """Expose async resource release, including on cancellation and close."""

    def __init__(self, handler):
        super().__init__(handler)
        self.async_closes = 0

    async def aclose(self):
        self.async_closes += 1
        await super().aclose()


class ReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.hub_data = deepcopy(HUB_DATA)
        self.details = {number: port_data(number) for number in range(1, 7)}
        self.bulk_data = bulk_port_data()
        self.requests = []
        self.http_status = 200
        self.identify_ack = {"ok": True}
        self.transport = ClosingTransport(self.handle_request)
        client = httpx2.Client
        async_client = httpx2.AsyncClient
        self.enterContext(
            patch(
                "smart_hub.httpx2.Client",
                side_effect=lambda **kwargs: client(transport=self.transport, **kwargs),
            )
        )
        self.enterContext(
            patch(
                "smart_hub.httpx2.AsyncClient",
                side_effect=lambda **kwargs: async_client(
                    transport=self.transport, **kwargs
                ),
            )
        )
        self.hub = Hub("http://example.test")

    def handle_request(self, request):
        self.requests.append(request)
        if self.http_status != 200:
            return httpx2.Response(self.http_status, json={"error": "unavailable"})
        if request.method == "GET":
            if request.url.path == "/api/hub":
                data = self.hub_data
            elif request.url.path == "/api/ports":
                data = self.bulk_data
            elif request.url.path.startswith("/api/ports/"):
                data = self.details[int(request.url.path.rsplit("/", 1)[1])]
            else:
                raise AssertionError(f"Unexpected read: {request.url}")
            return httpx2.Response(200, json=data)
        if request.method == "POST" and request.url.path == "/api/identify":
            if request.url.params.get("n") not in {str(n) for n in range(1, 7)}:
                return httpx2.Response(400, json={"error": "bad param"})
            return httpx2.Response(200, json=self.identify_ack)
        raise AssertionError(f"Unexpected write: {request.method} {request.url}")

    def assert_only_reads(self):
        self.assertTrue(self.requests)
        self.assertTrue(all(request.method == "GET" for request in self.requests))

    def test_snapshots_are_frozen(self):
        status = self.hub.status(6)
        snapshots = (
            (self.hub.info(), "temperature_c", 12.0),
            (status, "enabled", False),
            (status.next_action, "enabled", True),
        )
        # Dynamic writes exercise the runtime immutability contract.
        for snapshot, field, value in snapshots:
            with (
                self.subTest(model=type(snapshot).__name__),
                self.assertRaises(FrozenInstanceError),
            ):
                setattr(snapshot, field, value)

    def test_invalid_environment_readings_are_not_real_zero(self):
        self.hub_data["sensors"] = {"valid": False, "temp_c": 0.0, "humidity": 0.0}
        unavailable = self.hub.info()
        self.assertIsInstance(unavailable, HubInfo)
        self.assertIsNone(unavailable.temperature_c)
        self.assertIsNone(unavailable.humidity_percent)
        self.hub_data["sensors"]["valid"] = True
        zero = self.hub.info()
        self.assertEqual((zero.temperature_c, zero.humidity_percent), (0.0, 0.0))
        self.assertIsNone(unavailable.temperature_c)
        self.assertIsNone(asdict(unavailable)["humidity_percent"])

    def test_unsynced_clock_has_no_iso_timestamp(self):
        self.hub_data["clock"].update(synced=False, iso="")
        unsynced = self.hub.info()
        self.assertFalse(unsynced.clock_synced)
        self.assertIsNone(unsynced.time_iso)
        self.assertIsNone(asdict(unsynced)["time_iso"])
        self.hub_data["clock"] = deepcopy(HUB_DATA["clock"])
        synced = self.hub.info()
        self.assertTrue(synced.clock_synced)
        self.assertEqual(synced.time_iso, "2026-08-02T16:33:38+10:00")

    def test_missing_monitor_is_unavailable_but_measured_zero_is_valid(self):
        for present in (False, True):
            with self.subTest(monitor=present):
                data = port_data(6, monitor=present, volts=0.0, ma=0.0, mw=0.0)
                self.details[6] = data
                bulk = deepcopy(data)
                del bulk["power_mode"]
                del bulk["schedule"]["entries"]
                self.bulk_data["ports"][5] = bulk
                for status in (self.hub.status(6), self.hub.ports()[5]):
                    self.assertIsInstance(status, PortStatus)
                    expected = (0.0, 0.0, 0.0) if present else (None, None, None)
                    self.assertEqual(
                        (status.voltage_v, status.current_ma, status.power_mw), expected
                    )
                    self.assertIs(status.monitor_present, present)

    def test_schedule_is_normalized_and_does_not_require_a_next_action(self):
        status = self.hub.status(6)
        next_action = status.next_action
        assert isinstance(next_action, ScheduledAction)
        self.assertEqual(
            asdict(status)["next_action"],
            {
                "delay_seconds": 82,
                "enabled": False,
                "local_time": "Sun 16:35",
                "entry_index": 0,
            },
        )
        self.assertEqual(status.schedule_count, 2)
        self.assertNotIn("entries", asdict(status))
        for count in (2, 0):
            with self.subTest(count=count):
                self.details[6]["schedule"] = {
                    "count": count,
                    "next": None,
                    "entries": [],
                }
                self.bulk_data["ports"][5]["schedule"] = {"count": count, "next": None}
                for no_next in (self.hub.status(6), self.hub.ports()[5]):
                    self.assertEqual(no_next.schedule_count, count)
                    self.assertIsNone(no_next.next_action)
        self.assertFalse(next_action.enabled)

    def test_bulk_does_not_guess_detail_only_power_mode(self):
        bulk = self.hub.ports()
        detail = self.hub.status(6)
        self.assertEqual([port.number for port in bulk], list(range(1, 7)))
        self.assertTrue(all(port.power_mode is None for port in bulk))
        self.assertEqual(detail.power_mode, "manual")
        self.assertEqual(bulk[5].next_action, detail.next_action)
        self.assertFalse(bulk[0].enabled)
        self.assertTrue(bulk[2].fault)
        self.assertTrue(bulk[3].occupied)
        self.assertIsNone(bulk[3].current_ma)

    def test_identify_requires_positive_acknowledgement(self):
        before = self.hub.status(6)
        self.assertIsNone(self.hub.identify(6))
        for acknowledgement in ({"ok": False}, {}, {"ok": 1}, {"ok": "true"}):
            with self.subTest(acknowledgement=acknowledgement):
                self.identify_ack = acknowledgement
                with self.assertRaises(RuntimeError):
                    self.hub.identify(6)
        self.assertEqual(self.hub.status(6), before)

    def test_http_errors_propagate_from_reads_and_identify(self):
        self.http_status = 503
        for operation in (
            self.hub.info,
            self.hub.ports,
            lambda: self.hub.status(6),
            lambda: self.hub.identify(6),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(httpx2.HTTPStatusError) as caught:
                    operation()
                self.assertEqual(caught.exception.response.status_code, 503)

    async def test_invalid_ports_are_rejected_without_contacting_hub(self):
        invalid_ports: tuple[Any, ...] = (0, 7, True, 1.5, "6")
        for port in invalid_ports:
            with self.subTest(port=port):
                for operation in (self.hub.status, self.hub.identify):
                    with self.assertRaises(ValueError):
                        operation(port)
                stream = None
                try:
                    with self.assertRaises(ValueError):
                        stream = self.hub.watch(port)
                        await anext(stream)
                finally:
                    if stream is not None:
                        await stream.aclose()
        self.assertEqual(self.requests, [])

    async def test_watch_interval_must_be_finite_and_strictly_positive(self):
        for interval in (0, -0.01, float("nan"), float("inf"), float("-inf")):
            with self.subTest(interval=interval):
                stream = None
                try:
                    with self.assertRaises(ValueError):
                        stream = self.hub.watch(interval=interval)
                        await anext(stream)
                finally:
                    if stream is not None:
                        await stream.aclose()
        self.assertEqual(self.requests, [])

    async def test_watch_selected_port_fetches_fresh_immutable_snapshots(self):
        stream = self.hub.watch(6, interval=0.001)
        try:
            first = await asyncio.wait_for(anext(stream), timeout=2)
            self.details[6].update(
                enabled=False, state="blue", volts=0.0, ma=0.0, mw=0.0
            )
            self.details[6]["schedule"]["next"]["in_s"] = 81
            second = await asyncio.wait_for(anext(stream), timeout=2)
            self.assertIsInstance(first, PortStatus)
            self.assertIsInstance(second, PortStatus)
            self.assertTrue(first.enabled)
            self.assertFalse(second.enabled)
            assert first.next_action is not None
            assert second.next_action is not None
            self.assertEqual(first.next_action.delay_seconds, 82)
            self.assertEqual(second.next_action.delay_seconds, 81)
            self.assertEqual(second.led_state, "blue")
            self.assertEqual(self.transport.async_closes, 0)
        finally:
            await stream.aclose()
        self.assertEqual(self.transport.async_closes, 1)
        self.assert_only_reads()

    async def test_watch_all_allows_other_tasks_and_reports_fresh_lists(self):
        entered = asyncio.Event()
        gate = asyncio.Event()

        async def gated_request(request):
            entered.set()
            await gate.wait()
            return self.handle_request(request)

        self.transport = ClosingTransport(gated_request)
        stream = self.hub.watch(interval=0.001)
        pending = asyncio.create_task(anext(stream))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            self.assertFalse(pending.done())

            async def release_request():
                await asyncio.sleep(0)
                gate.set()

            sibling = asyncio.create_task(release_request())
            first = await asyncio.wait_for(pending, timeout=2)
            await sibling
            self.bulk_data["ports"][0].update(enabled=True, state="pulse")
            second = await asyncio.wait_for(anext(stream), timeout=2)
            self.assertIsInstance(first, list)
            self.assertTrue(all(isinstance(port, PortStatus) for port in first))
            self.assertEqual([port.number for port in first], list(range(1, 7)))
            self.assertFalse(first[0].enabled)
            self.assertTrue(second[0].enabled)
            self.assertIsNone(second[0].power_mode)
            self.assertEqual(self.transport.async_closes, 0)
        finally:
            if not pending.done():
                pending.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await pending
            await stream.aclose()
        self.assertEqual(self.transport.async_closes, 1)
        self.assert_only_reads()

    async def test_watch_first_fetch_is_immediate_and_sleep_cancellation_never_writes(
        self,
    ):
        stream = self.hub.watch(6, interval=3600)
        pending = None
        try:
            first = await asyncio.wait_for(anext(stream), timeout=2)
            self.assertTrue(first.enabled)
            pending = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            self.assertEqual(len(self.requests), 1)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await pending
            await stream.aclose()
        self.assertEqual(self.transport.async_closes, 1)
        self.assertTrue(self.details[6]["enabled"])
        self.assert_only_reads()

    async def test_cancel_during_watch_request_closes_transport(self):
        entered = asyncio.Event()
        gate = asyncio.Event()

        async def blocked_request(request):
            self.requests.append(request)
            entered.set()
            await gate.wait()
            raise AssertionError("Cancelled request unexpectedly resumed")

        self.transport = ClosingTransport(blocked_request)
        stream = self.hub.watch(6)
        pending = asyncio.create_task(anext(stream))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
        finally:
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            await stream.aclose()
        self.assertEqual(self.transport.async_closes, 1)
        self.assert_only_reads()

    async def test_watch_http_failure_propagates_and_closes_transport(self):
        stream = self.hub.watch(6, interval=0.001)
        try:
            first = await asyncio.wait_for(anext(stream), timeout=2)
            self.assertTrue(first.enabled)
            self.http_status = 503
            with self.assertRaises(httpx2.HTTPStatusError) as caught:
                await asyncio.wait_for(anext(stream), timeout=2)
            self.assertEqual(caught.exception.response.status_code, 503)
        finally:
            await stream.aclose()
        self.assertEqual(self.transport.async_closes, 1)
        self.assert_only_reads()


if __name__ == "__main__":
    unittest.main()
