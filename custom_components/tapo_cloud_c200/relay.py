"""Private, on-demand MPEG-TS source for Home Assistant's stream worker."""

import asyncio
from contextlib import aclosing
import logging
import secrets

from aiohttp import web

from .cloud import SessionExpired

_LOGGER = logging.getLogger(__name__)


async def media_parts(reader):
    """Read the length-delimited video parts returned by the Tapo relay."""
    while True:
        async with asyncio.timeout(30):
            while True:
                line = await reader.readline()
                if not line:
                    return
                if b"--device-stream-boundary--" in line:
                    break
            headers = {}
            for _ in range(64):
                line = await reader.readuntil(b"\r\n")
                if line == b"\r\n":
                    break
                key, value = line.decode("latin1").split(":", 1)
                headers[key.strip().lower()] = value.strip()
            else:
                raise ValueError("Too many relay headers")
            length = int(headers.get("content-length", 0))
            if length < 0 or length > 16 * 1024 * 1024:
                raise ValueError("Invalid media length")
            if headers.get("x-if-encrypt") == "1":
                raise RuntimeError("Encrypted relay payload is unsupported")
            if not length:
                continue
            payload = await reader.readexactly(length)
        yield payload


class Relay:
    def __init__(self, client, base_url, device, reauthenticate):
        self.client = client
        self.base_url = base_url
        self.device = device
        self.reauthenticate = reauthenticate
        self.url = None
        self.runner = None

    async def start(self):
        app = web.Application()
        path = "/" + secrets.token_urlsafe(32)
        app.router.add_get(path, self.handle, allow_head=False)
        self.runner = web.AppRunner(
            app, access_log=None, handler_cancellation=True, shutdown_timeout=2
        )
        await self.runner.setup()
        try:
            await web.TCPSite(self.runner, "127.0.0.1", 0).start()
            self.url = f"http://127.0.0.1:{self.runner.addresses[0][1]}{path}"
        except BaseException:
            await self.close()
            raise

    async def close(self):
        if self.runner:
            await self.runner.cleanup()
            self.runner = None

    async def media(self):
        host, port, request = await asyncio.to_thread(
            self.client.relay_request, self.base_url, self.device
        )
        async with asyncio.timeout(20):
            reader, writer = await asyncio.open_connection(
                host, port, ssl=self.client.verify_context, server_hostname=host
            )
        try:
            async with asyncio.timeout(20):
                writer.write(request)
                await writer.drain()
                headers = await reader.readuntil(b"\r\n\r\n")
                if headers.split(b"\r\n", 1)[0].split()[1:2] != [b"200"]:
                    raise RuntimeError("Relay refused the media connection")
            async for payload in media_parts(reader):
                yield payload
        finally:
            writer.close()

    async def handle(self, request):
        response = web.StreamResponse(
            headers={"Content-Type": "video/mp2t", "Cache-Control": "no-store"}
        )
        try:
            async with aclosing(self.media()) as stream:
                async for payload in stream:
                    if not response.prepared:
                        await response.prepare(request)
                    await response.write(payload)
        except SessionExpired:
            self.reauthenticate()
        except (
            OSError,
            EOFError,
            ValueError,
            RuntimeError,
            asyncio.LimitOverrunError,
        ) as err:
            # Relay URLs and tokens must never appear in the journal.
            _LOGGER.warning("C200 cloud stream interrupted (%s)", type(err).__name__)
        if not response.prepared:
            raise web.HTTPServiceUnavailable(text="Cloud stream unavailable")
        return response
