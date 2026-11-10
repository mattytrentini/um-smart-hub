"""CLI serialization and streaming behavior using the real library parsers."""

import copy
import json
import unittest
from unittest.mock import patch

import httpx2
from typer.testing import CliRunner

from smart_hub.cli import app
from test_reads import HUB_DATA, port_data


class CliTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.fail_after = None
        self.runner = CliRunner()
        transport = httpx2.MockTransport(self.handle_request)
        client = httpx2.Client
        async_client = httpx2.AsyncClient
        self.enterContext(patch(
            "smart_hub.httpx2.Client",
            side_effect=lambda **kwargs: client(transport=transport, **kwargs),
        ))
        self.enterContext(patch(
            "smart_hub.httpx2.AsyncClient",
            side_effect=lambda **kwargs: async_client(transport=transport, **kwargs),
        ))

    def handle_request(self, request):
        self.requests.append(request)
        if self.fail_after is not None and len(self.requests) > self.fail_after:
            raise httpx2.ConnectError("connection dropped", request=request)
        if request.url.path == "/api/hub":
            data = copy.deepcopy(HUB_DATA)
        elif request.url.path == "/api/ports":
            data = {"ports": [port_data(number=n) for n in range(1, 7)]}
        else:
            # Change the state across polls to detect accidentally cached output.
            data = port_data(number=6, enabled=len(self.requests) % 2 == 1)
        return httpx2.Response(200, json=data)

    def test_json_output_is_a_document_with_selected_or_all_shape(self):
        for args, expected_type in (
            (["info", "--json"], dict),
            (["status", "--json"], list),
            (["status", "6", "--json"], dict),
        ):
            with self.subTest(args=args):
                result = self.runner.invoke(app, args)
                self.assertEqual(result.exit_code, 0, result.output)
                document = json.loads(result.stdout)
                self.assertIsInstance(document, expected_type)
                self.assertEqual(result.stderr, "")
        self.assertTrue(all(request.method == "GET" for request in self.requests))

    def test_json_watch_keeps_error_messages_off_snapshot_stream(self):
        self.fail_after = 2
        result = self.runner.invoke(app, ["watch", "6", "--interval", "0.001", "--json"])
        self.assertEqual(result.exit_code, 1)
        documents = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([document["enabled"] for document in documents], [True, False])
        self.assertIn("connection dropped", result.stderr)
        self.assertTrue(all(request.method == "GET" for request in self.requests))

    def test_failed_read_does_not_emit_a_success_document(self):
        self.fail_after = 0
        result = self.runner.invoke(app, ["info", "--json"])
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("connection dropped", result.stderr)

    def test_watch_invalid_intervals_never_reach_hub(self):
        for interval in ("0", "nan", "inf", "-1"):
            with self.subTest(interval=interval):
                result = self.runner.invoke(app, ["watch", "--interval", interval, "--json"])
                self.assertNotEqual(result.exit_code, 0)
                self.assertEqual(result.stdout, "")
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
