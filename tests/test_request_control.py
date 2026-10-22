import asyncio

import pytest
from fastapi import HTTPException

from backend.request_control import while_connected


class Request:
    def __init__(self, disconnected=False):
        self.disconnected = disconnected

    async def is_disconnected(self):
        return self.disconnected


def test_disconnect_cancels_upstream_and_does_not_produce_result():
    async def run():
        cancelled = asyncio.Event()

        async def upstream():
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.set()

        with pytest.raises(HTTPException) as exc:
            await while_connected(Request(True), upstream(), poll_seconds=.001)
        assert exc.value.status_code == 499
        assert cancelled.is_set()
    asyncio.run(run())


def test_connected_request_returns_actual_result_and_error():
    async def run():
        async def success():
            return {"answer": "actual response"}

        async def failure():
            raise ValueError("upstream failure")

        assert await while_connected(Request(), success()) == {"answer": "actual response"}
        with pytest.raises(ValueError, match="upstream failure"):
            await while_connected(Request(), failure())
    asyncio.run(run())
