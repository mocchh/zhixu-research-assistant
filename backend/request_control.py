"""Cancel local upstream work when its HTTP client goes away."""
import asyncio
from contextlib import suppress

from fastapi import HTTPException
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse


class LocalWriteGuard:
    """Guard local writes without BaseHTTPMiddleware masking disconnects."""
    def __init__(self, app, allowed_origins):
        self.app = app
        self.allowed_origins = frozenset(allowed_origins)

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        origin = Headers(scope=scope).get('origin')
        if scope['method'] in {'POST', 'PUT', 'PATCH', 'DELETE'} and origin and origin not in self.allowed_origins:
            response = JSONResponse({'detail': '不允许来自其他站点的写入请求'}, status_code=403)
            return await response(scope, receive, send)

        async def guarded_send(message):
            if message['type'] == 'http.response.start':
                MutableHeaders(scope=message)['X-Content-Type-Options'] = 'nosniff'
            await send(message)
        await self.app(scope, receive, guarded_send)


async def while_connected(request, awaitable, poll_seconds=.15):
    work = asyncio.ensure_future(awaitable)
    try:
        while True:
            done, _ = await asyncio.wait({work}, timeout=poll_seconds)
            if done:
                return work.result()
            if await request.is_disconnected():
                raise HTTPException(499, "请求已停止")
    finally:
        if not work.done():
            work.cancel()
            with suppress(asyncio.CancelledError):
                await work
