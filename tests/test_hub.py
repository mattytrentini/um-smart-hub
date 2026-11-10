"""Consumer-visible state changes, rejected writes, and cancellation semantics."""

import asyncio
import unittest
from typing import Any
from unittest.mock import patch

import httpx2

from smart_hub import Hub


class HubTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enabled = False
        self.writes = []
        self.ignore_writes = False
        self.status = 200
        self.transitions = asyncio.Queue()
        transport = httpx2.MockTransport(self.handle_request)
        client = httpx2.Client
        async_client = httpx2.AsyncClient
        self.enterContext(
            patch(
                "smart_hub.httpx2.Client",
                side_effect=lambda **kwargs: client(transport=transport, **kwargs),
            )
        )
        self.enterContext(
            patch(
                "smart_hub.httpx2.AsyncClient",
                side_effect=lambda **kwargs: async_client(
                    transport=transport, **kwargs
                ),
            )
        )
        self.hub = Hub("http://example.test")

    def handle_request(self, request):
        if self.status != 200:
            return httpx2.Response(self.status)
        if request.method == "GET":
            return httpx2.Response(200, json={"enabled": self.enabled})
        state = request.url.params["on"] == "1"
        self.writes.append(state)
        if not self.ignore_writes:
            self.enabled = state
        self.transitions.put_nowait(self.enabled)
        return httpx2.Response(
            200, json={"ports": [{"enabled": self.enabled} for _ in range(6)]}
        )

    def test_controls_and_single_toggle(self):
        self.assertIs(self.hub.on(6), True)
        self.assertIs(self.hub.toggle(6), False)
        self.assertIs(self.hub.toggle(6), True)
        self.assertIs(self.hub.off(6), False)
        self.assertEqual(self.writes, [True, False, True, False])
        self.assertFalse(self.enabled)

    def test_rejected_write_is_not_success(self):
        self.ignore_writes = True
        with self.assertRaises(RuntimeError):
            self.hub.on(6)
        self.assertFalse(self.enabled)

    def test_http_failure_propagates(self):
        self.status = 503
        with self.assertRaises(httpx2.HTTPStatusError):
            self.hub.toggle(6)
        self.assertEqual(self.writes, [])

    def test_port_bounds_before_requests(self):
        invalid_ports: tuple[Any, ...] = (0, 7, True, 1.5)
        for port in invalid_ports:
            for action in (self.hub.on, self.hub.off, self.hub.toggle):
                with (
                    self.subTest(port=port, action=action.__name__),
                    self.assertRaises(ValueError),
                ):
                    action(port)
        self.assertEqual(self.writes, [])

    def test_invalid_intervals_before_coroutine_creation(self):
        for seconds in (-1, float("nan"), float("inf")):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                self.hub.toggle(6, seconds)
        self.assertEqual(self.writes, [])

    async def test_async_loop_repeats_and_cancel_preserves_on(self):
        task = asyncio.create_task(self.hub.toggle(6, 0.01))
        try:
            for expected in (True, False, True):
                state = await asyncio.wait_for(self.transitions.get(), timeout=2)
                self.assertIs(state, expected)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(self.enabled)
        self.assertEqual(self.writes, [True, False, True])

    async def test_async_cancel_preserves_off(self):
        self.enabled = True
        task = asyncio.create_task(self.hub.toggle(6, 3600))
        try:
            state = await asyncio.wait_for(self.transitions.get(), timeout=2)
            self.assertIs(state, False)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(self.enabled)
        self.assertEqual(self.writes, [False])

    async def test_async_rejected_write_propagates(self):
        self.ignore_writes = True
        with self.assertRaises(RuntimeError):
            await self.hub.toggle(6, 1)
        self.assertFalse(self.enabled)


if __name__ == "__main__":
    unittest.main()
