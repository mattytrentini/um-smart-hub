"""Host-derived location regressions through real library and CLI boundaries."""

import json
import logging
import unittest
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime, timedelta, tzinfo
from io import StringIO
from typing import Any, Self
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx2
from typer.testing import CliRunner

from smart_hub import Hub, Location
from smart_hub.cli import app


def fixed_clock(instant: datetime) -> type[datetime]:
    """Keep timezone conversion real while making the current instant explicit."""

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(instant.timestamp(), tz=tz or UTC)

    return Clock


class LocationFirmware:
    """Model partial settings updates, including firmware's misleading OK reply."""

    def __init__(self, test: unittest.TestCase):
        self.settings: dict[str, Any] = {
            "wifi": {"ssid": "private-network", "password": "location-secret-6b2"},
            "ports": {"names": ["Desk"] * 6, "reverse_order": True},
            "leds": {"enabled": True, "brightness": 73},
            "location": {
                "city": "Auckland",
                "country": "NZ",
                "utc_offset": 12,
                "ntp_server": "private.ntp.example",
                "time_24h": False,
            },
        }
        self.requests: list[httpx2.Request] = []
        self.settings_writes: list[dict[str, Any]] = []
        self.ignore_settings = False
        self.rewrite_location: dict[str, Any] = {}
        self.post_status = 200
        self.schema_status = 200
        self.schema_override: dict[str, Any] | None = None
        self.schema_content: bytes | None = None
        transport = httpx2.MockTransport(self.handle_request)
        client = httpx2.Client
        test.enterContext(
            patch(
                "smart_hub.httpx2.Client",
                side_effect=lambda **kwargs: client(transport=transport, **kwargs),
            )
        )

    def schema(self) -> dict[str, Any]:
        if self.schema_override is not None:
            return deepcopy(self.schema_override)
        return {
            "groups": [
                {"name": name, "values": deepcopy(values), "fields": []}
                for name, values in self.settings.items()
            ]
        }

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if request.method == "GET" and request.url.path == "/api/settings/schema":
            if self.schema_content is not None:
                return httpx2.Response(self.schema_status, content=self.schema_content)
            return httpx2.Response(self.schema_status, json=self.schema())
        if request.method == "POST" and request.url.path == "/api/settings":
            update: dict[str, Any] = json.loads(request.content)
            self.settings_writes.append(deepcopy(update))
            if self.post_status != 200:
                return httpx2.Response(self.post_status, json={"error": "unavailable"})
            if not self.ignore_settings:
                for name, values in update.items():
                    self.settings[name].update(deepcopy(values))
                self.settings["location"].update(self.rewrite_location)
            return httpx2.Response(200, json={"ok": True})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")


class LocationTests(unittest.TestCase):
    def setUp(self):
        self.firmware = LocationFirmware(self)
        self.hub = Hub("http://example.test")
        self.runner = CliRunner()
        self.enterContext(
            patch(
                "smart_hub._location.get_localzone",
                return_value=ZoneInfo("Australia/Sydney"),
            )
        )
        self.enterContext(
            patch(
                "smart_hub._location.datetime",
                fixed_clock(datetime(2026, 1, 15, 12, tzinfo=UTC)),
            )
        )
        self.logs = StringIO()
        handler = logging.StreamHandler(self.logs)
        logger = logging.getLogger()
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)

    def assert_redacted(self, text: object):
        secret = self.firmware.settings["wifi"]["password"]
        self.assertNotIn(secret, str(text))
        self.assertNotIn(secret, self.logs.getvalue())

    def assert_location_only(self, before: dict[str, Any], expected: Location):
        after = deepcopy(before)
        location = {
            "city": expected.city,
            "country": expected.country,
            "utc_offset": expected.utc_offset_hours,
        }
        after["location"].update(location)
        self.assertEqual(self.firmware.settings, after)
        self.assertTrue(self.firmware.settings_writes)
        for update in self.firmware.settings_writes:
            self.assertEqual(set(update), {"location"})
            self.assertEqual(set(update["location"]), {"city", "country", "utc_offset"})

    def test_sydney_uses_current_dst_not_standard_offset(self):
        for month, offset in ((1, 11), (7, 10)):
            with self.subTest(month=month):
                before = deepcopy(self.firmware.settings)
                self.firmware.settings_writes.clear()
                with patch(
                    "smart_hub._location.datetime",
                    fixed_clock(datetime(2026, month, 15, 12, tzinfo=UTC)),
                ):
                    result = self.hub.sync_location()
                self.assertEqual(result, Location("Sydney", "AU", offset))
                self.assertIs(type(result.utc_offset_hours), int)
                self.assert_location_only(before, result)

    def test_zone_mapping_uses_representative_city_with_spaces(self):
        with patch(
            "smart_hub._location.get_localzone",
            return_value=ZoneInfo("America/New_York"),
        ):
            result = self.hub.sync_location()
        self.assertEqual(result, Location("New York", "US", -5))

    def test_overrides_are_trimmed_and_remaining_field_is_inferred(self):
        cases = (
            ({"city": "  Wollongong  "}, Location("Wollongong", "AU", 11)),
            ({"country": "  NZ  "}, Location("Sydney", "NZ", 11)),
            (
                {"city": "  Actual city  ", "country": "  Actual country  "},
                Location("Actual city", "Actual country", 11),
            ),
        )
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                before = deepcopy(self.firmware.settings)
                self.firmware.settings_writes.clear()
                result = self.hub.sync_location(**overrides)
                self.assertEqual(result, expected)
                self.assert_location_only(before, result)

    def test_unmapped_utc_requires_both_overrides_before_network(self):
        with patch("smart_hub._location.get_localzone", return_value=ZoneInfo("UTC")):
            for overrides in ({}, {"city": "Greenwich"}, {"country": "GB"}):
                with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                    self.hub.sync_location(**overrides)
        self.assertEqual(self.firmware.requests, [])

    def test_explicit_city_and_country_allow_utc(self):
        before = deepcopy(self.firmware.settings)
        with patch("smart_hub._location.get_localzone", return_value=ZoneInfo("UTC")):
            result = self.hub.sync_location(city=" Greenwich ", country=" GB ")
        self.assertEqual(result, Location("Greenwich", "GB", 0))
        self.assert_location_only(before, result)

    def test_fractional_hour_offsets_are_rejected_without_rounding_or_requests(self):
        for key in ("Asia/Kathmandu", "Australia/Adelaide"):
            with (
                self.subTest(zone=key),
                patch("smart_hub._location.get_localzone", return_value=ZoneInfo(key)),
                self.assertRaises(ValueError),
            ):
                self.hub.sync_location(city="Explicit city", country="Explicit country")
        self.assertEqual(self.firmware.requests, [])

    def test_blank_and_invalid_overrides_are_rejected_before_requests(self):
        invalid_values: tuple[Any, ...] = ("", " \t\n ", 123, True, [], {})
        for value in invalid_values:
            for field in ("city", "country"):
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaises((TypeError, ValueError)),
                ):
                    self.hub.sync_location(**{field: value})
        self.assertEqual(self.firmware.requests, [])

    def test_supported_offset_boundaries_remain_integer_hours(self):
        for key, offset in (("Etc/GMT+12", -12), ("Pacific/Kiritimati", 14)):
            with (
                self.subTest(zone=key),
                patch("smart_hub._location.get_localzone", return_value=ZoneInfo(key)),
            ):
                result = self.hub.sync_location(city="Explicit city", country="XX")
                self.assertEqual(result.utc_offset_hours, offset)
                self.assertIs(type(result.utc_offset_hours), int)

    def test_missing_and_out_of_range_offsets_fail_before_requests(self):
        for offset in (None, timedelta(hours=-13), timedelta(hours=15)):
            with self.subTest(offset=offset):

                class Clock(datetime):
                    @classmethod
                    def now(cls, tz: tzinfo | None = None) -> Self:
                        return cls(2026, 1, 15, 12, tzinfo=tz)

                    def utcoffset(
                        self, value: timedelta | None = offset
                    ) -> timedelta | None:
                        return value

                with (
                    patch("smart_hub._location.datetime", Clock),
                    self.assertRaises(ValueError),
                ):
                    self.hub.sync_location()
        self.assertEqual(self.firmware.requests, [])

    def test_schema_group_is_selected_by_name_not_position(self):
        for names in (
            ("location", "leds", "wifi", "ports"),
            ("ports", "wifi", "location", "leds"),
        ):
            with self.subTest(names=names):
                self.firmware.settings = {
                    name: self.firmware.settings[name] for name in names
                }
                result = self.hub.sync_location(city="Wollongong")
                self.assertEqual(result, Location("Wollongong", "AU", 11))
                self.assert_redacted(result)

    def test_ignored_ok_reply_is_not_success(self):
        self.firmware.ignore_settings = True
        before = deepcopy(self.firmware.settings)
        with self.assertRaises(RuntimeError) as caught:
            self.hub.sync_location()
        self.assertEqual(self.firmware.settings, before)
        self.assert_redacted(caught.exception)

    def test_readback_confirms_each_persisted_field_and_rejects_clamping(self):
        for field, value in (
            ("city", "Ignored"),
            ("country", "NZ"),
            ("utc_offset", 10),
        ):
            with self.subTest(field=field):
                self.firmware.rewrite_location = {field: value}
                with self.assertRaises(RuntimeError) as caught:
                    self.hub.sync_location()
                self.assertEqual(self.firmware.settings["location"][field], value)
                self.assert_redacted(caught.exception)

    def test_missing_or_malformed_schema_never_exposes_wifi_secret(self):
        secret = self.firmware.settings["wifi"]["password"]
        bad_schemas: list[dict[str, Any]] = [
            {"wifi": secret},
            {"groups": {"wifi": secret}},
            {"groups": [{"name": "wifi", "values": {"password": secret}}]},
            {"groups": [{"name": "location", "values": [secret]}]},
            {"groups": [{"name": "location", "values": {"password": secret}}]},
        ]
        for schema in bad_schemas:
            with self.subTest(schema=schema):
                self.firmware.schema_override = schema
                with self.assertRaises(RuntimeError) as caught:
                    self.hub.sync_location()
                self.assert_redacted(caught.exception)

    def test_malformed_location_readback_is_not_coerced_or_accepted(self):
        invalid_fields: tuple[tuple[str, Any], ...] = (
            ("city", None),
            ("city", ["Sydney"]),
            ("country", 1),
            ("country", {"value": "AU"}),
            ("utc_offset", "11"),
            ("utc_offset", 11.0),
            ("utc_offset", True),
            ("utc_offset", None),
        )
        for field, value in invalid_fields:
            with self.subTest(field=field, value=value):
                self.firmware.schema_override = None
                schema = self.firmware.schema()
                group = next(
                    group for group in schema["groups"] if group["name"] == "location"
                )
                group["values"].update(city="Sydney", country="AU", utc_offset=11)
                group["values"][field] = value
                self.firmware.schema_override = schema
                with self.assertRaises(RuntimeError) as caught:
                    self.hub.sync_location()
                self.assert_redacted(caught.exception)

    def test_missing_each_location_field_fails_readback(self):
        for field in ("city", "country", "utc_offset"):
            with self.subTest(field=field):
                self.firmware.schema_override = None
                schema = self.firmware.schema()
                group = next(
                    group for group in schema["groups"] if group["name"] == "location"
                )
                group["values"].pop(field)
                self.firmware.schema_override = schema
                with self.assertRaises(RuntimeError) as caught:
                    self.hub.sync_location()
                self.assert_redacted(caught.exception)

    def test_invalid_json_readback_is_a_redacted_runtime_failure(self):
        secret = self.firmware.settings["wifi"]["password"]
        self.firmware.schema_content = f'{{"wifi": "{secret}",'.encode()
        with self.assertRaises(RuntimeError) as caught:
            self.hub.sync_location()
        self.assert_redacted(caught.exception)

    def test_http_failures_propagate_without_confirming_success(self):
        before = deepcopy(self.firmware.settings)
        self.firmware.post_status = 503
        with self.assertRaises(httpx2.HTTPStatusError) as caught:
            self.hub.sync_location()
        self.assertEqual(self.firmware.settings, before)
        self.assert_redacted(caught.exception)
        self.firmware.post_status = 200
        self.firmware.schema_status = 503
        with self.assertRaises(httpx2.HTTPStatusError) as caught:
            self.hub.sync_location()
        self.assert_redacted(caught.exception)

    def test_cli_sync_updates_real_settings_and_prints_human_receipt(self):
        before = deepcopy(self.firmware.settings)
        result = self.runner.invoke(
            app, ["--host", "http://example.test", "location", "sync"]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.stderr, "")
        self.assertIn("Sydney", result.stdout)
        self.assertIn("AU", result.stdout)
        self.assertRegex(result.stdout, r"UTC\s*\+11")
        self.assert_location_only(before, Location("Sydney", "AU", 11))
        self.assert_redacted(result.output)

    def test_cli_json_has_normalized_location_and_integer_offset(self):
        before = deepcopy(self.firmware.settings)
        with patch("smart_hub._location.get_localzone", return_value=ZoneInfo("UTC")):
            result = self.runner.invoke(
                app,
                [
                    "location",
                    "sync",
                    "--city",
                    "  Greenwich  ",
                    "--country",
                    "  GB  ",
                    "--json",
                ],
            )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.stderr, "")
        document: dict[str, Any] = json.loads(result.stdout)
        expected = Location("Greenwich", "GB", 0)
        self.assertEqual(document, asdict(expected))
        self.assertIs(type(document["utc_offset_hours"]), int)
        self.assert_location_only(before, expected)
        self.assert_redacted(result.output)

    def test_cli_rejected_write_has_stderr_and_no_success_document(self):
        self.firmware.ignore_settings = True
        before = deepcopy(self.firmware.settings)
        result = self.runner.invoke(app, ["location", "sync", "--json"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, "")
        self.assertTrue(result.stderr)
        self.assertEqual(self.firmware.settings, before)
        self.assert_redacted(result.output)

    def test_cli_unsupported_host_has_stderr_and_no_requests_or_document(self):
        for key in ("UTC", "Asia/Kathmandu"):
            with (
                self.subTest(zone=key),
                patch("smart_hub._location.get_localzone", return_value=ZoneInfo(key)),
            ):
                result = self.runner.invoke(app, ["location", "sync", "--json"])
                self.assertNotEqual(result.exit_code, 0)
                self.assertEqual(result.stdout, "")
                self.assertTrue(result.stderr)
                self.assert_redacted(result.output)
        self.assertEqual(self.firmware.requests, [])


if __name__ == "__main__":
    unittest.main()
