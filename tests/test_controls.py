"""Stateful regressions for one-shot cycles and partial settings updates."""

import asyncio
import json
import logging
import unittest
from copy import deepcopy
from io import StringIO
from typing import Any
from unittest.mock import patch

import httpx2
from test_reads import port_data
from typer.testing import CliRunner

from smart_hub import Hub, PowerMode
from smart_hub.cli import app


class Firmware:
    """Keep independent port/settings state behind real HTTP client factories."""

    def __init__(self, test: unittest.TestCase):
        self.enabled = [False] * 6
        self.occupied = [False, True, True, True, False, True]
        self.settings: dict[str, Any] = {
            "wifi": {"ssid": "private-network", "password": "schema-secret-7c9"},
            "ports": {
                "names": ["One", "Two", "Three", "Four", "Five", "Six"],
                "power_mode": [
                    "manual",
                    "manual",
                    "connect",
                    "manual",
                    "manual",
                    "boot",
                ],
                "reverse_order": True,
            },
            "leds": {"enabled": True, "brightness": 73},
        }
        self.requests: list[httpx2.Request] = []
        self.power_writes: list[tuple[int, bool]] = []
        self.settings_writes: list[dict[str, Any]] = []
        self.ignore_power = False
        self.ignore_settings = False
        self.settings_status = 200
        self.corrupt_peer: str | None = None
        self.schema_override: dict[str, Any] | None = None
        transport = httpx2.MockTransport(self.handle_request)
        client = httpx2.Client
        async_client = httpx2.AsyncClient

        test.enterContext(
            patch(
                "smart_hub.httpx2.Client",
                side_effect=lambda **kwargs: client(transport=transport, **kwargs),
            )
        )
        test.enterContext(
            patch(
                "smart_hub.httpx2.AsyncClient",
                side_effect=lambda **kwargs: async_client(
                    transport=transport, **kwargs
                ),
            )
        )

    def schema(self) -> dict[str, Any]:
        if self.schema_override is not None:
            return deepcopy(self.schema_override)
        # The ports group deliberately isn't at its firmware-example position.
        return {
            "groups": [
                {"name": name, "values": deepcopy(values), "fields": []}
                for name, values in self.settings.items()
            ]
        }

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET" and path == "/api/settings/schema":
            return httpx2.Response(200, json=self.schema())
        if request.method == "POST" and path == "/api/settings":
            update = json.loads(request.content)
            self.settings_writes.append(deepcopy(update))
            if self.settings_status != 200:
                return httpx2.Response(
                    self.settings_status, json={"error": "unavailable"}
                )
            if not self.ignore_settings:
                old_modes = self.settings["ports"]["power_mode"][:]
                for group, values in update.items():
                    self.settings[group].update(deepcopy(values))
                modes = self.settings["ports"]["power_mode"]
                for index, (old, new) in enumerate(zip(old_modes, modes, strict=True)):
                    if old != new and (
                        new == "boot" or new == "connect" and self.occupied[index]
                    ):
                        self.enabled[index] = True
                if self.corrupt_peer is not None:
                    self.settings["ports"][self.corrupt_peer][0] = (
                        "External writer" if self.corrupt_peer == "names" else "connect"
                    )
            return httpx2.Response(200, json={"ok": True})
        if request.method == "POST" and path == "/api/port":
            number = int(request.url.params["n"])
            enabled = request.url.params["on"] == "1"
            self.power_writes.append((number, enabled))
            if not self.ignore_power:
                self.enabled[number - 1] = enabled
            return httpx2.Response(200, json={"ports": self.ports()})
        if request.method == "GET" and path == "/api/ports":
            return httpx2.Response(200, json={"ports": self.ports()})
        if request.method == "GET" and path.startswith("/api/ports/"):
            number = int(path.rsplit("/", 1)[1])
            return httpx2.Response(200, json=self.ports()[number - 1])
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    def ports(self):
        return [
            port_data(
                number=index + 1,
                enabled=enabled,
                occupied=self.occupied[index],
                name=self.settings["ports"]["names"][index],
                power_mode=self.settings["ports"]["power_mode"][index],
            )
            for index, enabled in enumerate(self.enabled)
        ]


class ControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.firmware = Firmware(self)
        self.hub = Hub("http://example.test")
        self.logs = StringIO()
        handler = logging.StreamHandler(self.logs)
        root = logging.getLogger()
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)

    def assert_redacted(self, error: BaseException):
        secret = self.firmware.settings["wifi"]["password"]
        self.assertNotIn(secret, str(error))
        self.assertNotIn(secret, self.logs.getvalue())

    async def test_cycle_off_wait_on_from_either_state_and_zero_duration(self):
        real_sleep = asyncio.sleep
        for initially_on, seconds in ((False, 2.0), (True, 2.0), (True, 0.0)):
            with self.subTest(initially_on=initially_on, seconds=seconds):
                self.firmware.enabled[5] = initially_on
                self.firmware.power_writes.clear()
                self.firmware.requests.clear()
                before_settings = deepcopy(self.firmware.settings)

                async def wait_while_off(delay):
                    self.assertFalse(self.firmware.enabled[5])
                    await real_sleep(0)

                with patch("smart_hub.asyncio.sleep", side_effect=wait_while_off):
                    result = await self.hub.cycle(6, seconds)
                self.assertIs(result, True)
                self.assertTrue(self.firmware.enabled[5])
                self.assertEqual(self.firmware.power_writes, [(6, False), (6, True)])
                self.assertEqual(self.firmware.settings, before_settings)
                self.assertTrue(
                    all(
                        request.url.path == "/api/port"
                        for request in self.firmware.requests
                    )
                )

    async def test_cycle_wait_is_cooperative_and_cancellation_leaves_off(self):
        self.firmware.enabled[5] = True
        waiting = asyncio.Event()
        other_work_done = asyncio.Event()
        never_release = asyncio.Event()

        async def wait_while_off(seconds):
            self.assertFalse(self.firmware.enabled[5])
            waiting.set()
            await never_release.wait()

        async def other_work():
            await waiting.wait()
            other_work_done.set()

        with patch("smart_hub.asyncio.sleep", side_effect=wait_while_off):
            cycle = asyncio.create_task(self.hub.cycle(6, 3600))
            worker = asyncio.create_task(other_work())
            try:
                await asyncio.wait_for(other_work_done.wait(), timeout=2)
                self.assertFalse(cycle.done())
                self.assertEqual(self.firmware.power_writes, [(6, False)])
            finally:
                cycle.cancel()
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
                with self.assertRaises(asyncio.CancelledError):
                    await cycle
        self.assertFalse(self.firmware.enabled[5])
        self.assertEqual(self.firmware.power_writes, [(6, False)])
        self.assertEqual(self.firmware.settings_writes, [])

    async def test_rejected_off_does_not_switch_on(self):
        self.firmware.enabled[5] = True
        self.firmware.ignore_power = True
        with self.assertRaises(RuntimeError):
            await self.hub.cycle(6, 0)
        self.assertTrue(self.firmware.enabled[5])
        self.assertEqual(self.firmware.power_writes, [(6, False)])

    async def test_cycle_invalid_inputs_make_no_requests(self):
        invalid_ports: tuple[Any, ...] = (0, 7, True, 1.5)
        for port in invalid_ports:
            with self.subTest(port=port), self.assertRaises(ValueError):
                await self.hub.cycle(port, 0)
        for seconds in (-1, float("nan"), float("inf"), -float("inf")):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                await self.hub.cycle(6, seconds)
        self.assertEqual(self.firmware.requests, [])
        self.assertEqual(self.firmware.power_writes, [])

    def test_rename_preserves_peers_settings_and_an_off_boot_port(self):
        before = deepcopy(self.firmware.settings)
        expected = deepcopy(before)
        expected["ports"]["names"][5] = "Logger"
        self.assertEqual(self.hub.rename(6, "Logger"), "Logger")
        self.assertEqual(self.firmware.settings, expected)
        self.assertFalse(self.firmware.enabled[5])
        self.assertEqual(self.firmware.power_writes, [])

    def test_names_follow_firmware_unicode_cleaning_and_truncation_order(self):
        cases = (
            ("  Café\x00\t🙂\x1f\x7fX \r\n", "Café  🙂  X"),
            ("\u00a0Desk\u0080\u00a0", "\u00a0Desk\u0080\u00a0"),
            ("🙂" * 24 + "tail", "🙂" * 24),
            (" " * 23 + "AB", "A"),
            ("\x00 \t", ""),
        )
        for name, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(self.hub.rename(3, name), expected)
                self.assertEqual(self.firmware.settings["ports"]["names"][2], expected)
        self.assertEqual(self.firmware.power_writes, [])

    def test_modes_accept_enum_and_string_and_preserve_other_settings(self):
        for number, mode, initially_on, expected_on in (
            (3, PowerMode.MANUAL, True, True),
            (3, "boot", False, True),
            (4, PowerMode.CONNECT, False, True),
            (5, "connect", False, False),
            (6, "boot", False, False),
        ):
            with self.subTest(number=number, mode=mode):
                self.firmware.enabled[number - 1] = initially_on
                self.firmware.requests.clear()
                self.firmware.settings_writes.clear()
                expected = deepcopy(self.firmware.settings)
                expected["ports"]["power_mode"][number - 1] = str(mode)
                self.assertIs(self.hub.set_power_mode(number, mode), PowerMode(mode))
                self.assertEqual(self.firmware.settings, expected)
                self.assertIs(self.firmware.enabled[number - 1], expected_on)
                self.assertEqual(self.firmware.power_writes, [])

    def test_settings_validate_ports_and_values_before_network(self):
        invalid_ports: tuple[Any, ...] = (0, 7, True, 1.5)
        for port in invalid_ports:
            for action, value in (
                (self.hub.rename, "Logger"),
                (self.hub.set_power_mode, "manual"),
            ):
                with (
                    self.subTest(port=port, action=action.__name__),
                    self.assertRaises(ValueError),
                ):
                    action(port, value)
        invalid_name: Any = 123
        with self.assertRaises((TypeError, ValueError)):
            self.hub.rename(6, invalid_name)
        invalid_modes: tuple[Any, ...] = ("invalid", 1)
        for mode in invalid_modes:
            with self.subTest(mode=mode), self.assertRaises((TypeError, ValueError)):
                self.hub.set_power_mode(6, mode)
        self.assertEqual(self.firmware.requests, [])

    def test_ignored_settings_ack_is_not_success(self):
        self.firmware.ignore_settings = True
        before = deepcopy(self.firmware.settings)
        for action, value in (
            (self.hub.rename, "Logger"),
            (self.hub.set_power_mode, "manual"),
        ):
            with self.subTest(action=action.__name__):
                self.firmware.requests.clear()
                with self.assertRaises(RuntimeError) as caught:
                    action(6, value)
                self.assert_redacted(caught.exception)
                self.assertEqual(self.firmware.settings, before)
                self.assertEqual(self.firmware.power_writes, [])

    def test_readback_confirms_whole_array_not_only_selected_port(self):
        for field, action, value in (
            ("names", self.hub.rename, "Logger"),
            ("power_mode", self.hub.set_power_mode, "manual"),
        ):
            with self.subTest(field=field):
                self.firmware.requests.clear()
                self.firmware.corrupt_peer = field
                with self.assertRaises(RuntimeError) as caught:
                    action(6, value)
                self.assertEqual(self.firmware.settings["ports"][field][5], value)
                self.assertNotEqual(
                    self.firmware.settings["ports"][field],
                    self.firmware.settings_writes[-1]["ports"][field],
                )
                self.assert_redacted(caught.exception)
                self.assertEqual(self.firmware.power_writes, [])

    def test_missing_or_invalid_settings_array_never_posts_or_exposes_schema(self):
        for field, action, value in (
            ("names", self.hub.rename, "Logger"),
            ("power_mode", self.hub.set_power_mode, "manual"),
        ):
            valid = self.firmware.settings["ports"][field]
            invalid_arrays: tuple[Any, ...] = ({}, valid[:5], [*valid[:5], 9])
            for invalid in invalid_arrays:
                with self.subTest(field=field, invalid=invalid):
                    self.firmware.requests.clear()
                    schema = self.firmware.schema()
                    schema["groups"][1]["values"][field] = invalid
                    self.firmware.schema_override = schema
                    try:
                        with self.assertRaises(RuntimeError) as caught:
                            action(6, value)
                        self.assert_redacted(caught.exception)
                        self.assertEqual(self.firmware.settings_writes, [])
                        self.assertTrue(self.firmware.requests)
                        self.assertTrue(
                            all(
                                request.method == "GET"
                                for request in self.firmware.requests
                            )
                        )
                    finally:
                        self.firmware.schema_override = None

    def test_unavailable_settings_group_values_or_field_never_posts(self):
        for field, action, value in (
            ("names", self.hub.rename, "Logger"),
            ("power_mode", self.hub.set_power_mode, "manual"),
        ):
            for missing in ("group", "values", "field"):
                with self.subTest(field=field, missing=missing):
                    schema = self.firmware.schema()
                    if missing == "group":
                        schema["groups"].pop(1)
                    elif missing == "values":
                        schema["groups"][1].pop("values")
                    else:
                        schema["groups"][1]["values"].pop(field)
                    self.firmware.schema_override = schema
                    try:
                        with self.assertRaises(RuntimeError) as caught:
                            action(6, value)
                        self.assert_redacted(caught.exception)
                        self.assertEqual(self.firmware.settings_writes, [])
                    finally:
                        self.firmware.schema_override = None
        self.assertTrue(
            all(request.method == "GET" for request in self.firmware.requests)
        )

    def test_settings_post_http_failure_propagates_without_successful_readback(self):
        self.firmware.settings_status = 503
        before = deepcopy(self.firmware.settings)
        with self.assertRaises(httpx2.HTTPStatusError):
            self.hub.rename(6, "Logger")
        self.assertEqual(self.firmware.settings, before)

    def test_cli_cycle_returns_with_port_on(self):
        runner = CliRunner()
        result = runner.invoke(
            app, ["--host", "http://example.test", "cycle", "6", "0"]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.stderr, "")
        self.assertTrue(self.firmware.enabled[5])
        self.assertEqual(self.firmware.power_writes, [(6, False), (6, True)])

    def test_cli_name_mode_and_empty_name_change_real_settings(self):
        runner = CliRunner()
        before = deepcopy(self.firmware.settings)
        for args in (
            ["name", "6", "  Logger\t🙂  "],
            ["mode", "3", "boot"],
            ["name", "2", ""],
        ):
            with self.subTest(args=args):
                result = runner.invoke(app, args)
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(result.stderr, "")
        expected = deepcopy(before)
        expected["ports"]["names"][5] = "Logger 🙂"
        expected["ports"]["names"][1] = ""
        expected["ports"]["power_mode"][2] = "boot"
        self.assertEqual(self.firmware.settings, expected)
        self.assertTrue(self.firmware.enabled[2])
        self.assertFalse(self.firmware.enabled[5])
        self.assertEqual(self.firmware.power_writes, [])

    def test_cli_invalid_mode_rejected_without_requests(self):
        result = CliRunner().invoke(app, ["mode", "6", "invalid"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.firmware.requests, [])


if __name__ == "__main__":
    unittest.main()
