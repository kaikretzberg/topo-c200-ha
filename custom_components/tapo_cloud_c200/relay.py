"""Private, on-demand MPEG-TS source for Home Assistant's stream worker."""

import asyncio
from contextlib import aclosing
import logging
import re
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
        app.router.add_get(path + "/raw", self.handle, allow_head=False)
        app.router.add_get(path, self.video_only, allow_head=False)
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
                status = headers.split(b"\r\n", 1)[0].split()[1:2]
                if status != [b"200"]:
                    code = (
                        status[0].decode("ascii")
                        if status and status[0].isdigit()
                        else "unknown"
                    )
                    raise RuntimeError(
                        f"Relay refused the media connection (HTTP {code})"
                    )
            async for payload in media_parts(reader):
                yield payload
        finally:
            writer.close()

    async def video_only(self, request):
        """Remove C200 private audio tracks before HA packages the first keyframe."""
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-fflags",
            "+genpts+discardcorrupt",
            "-dts_delta_threshold",
            "1",
            "-probesize",
            "128k",
            "-analyzeduration",
            "500000",
            "-f",
            "mpegts",
            "-i",
            self.url + "/raw",
            "-map",
            "0:v:0",
            "-an",
            "-c:v",
            "copy",
            "-muxdelay",
            "0",
            "-muxpreload",
            "0",
            "-f",
            "mpegts",
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        response = web.StreamResponse(
            headers={"Content-Type": "video/mp2t", "Cache-Control": "no-store"}
        )
        try:
            while chunk := await process.stdout.read(65536):
                if not response.prepared:
                    await response.prepare(request)
                await response.write(chunk)
            if not response.prepared:
                _LOGGER.warning(
                    "C200 video remuxer returned no packets (exit %s)",
                    await process.wait(),
                )
                raise web.HTTPServiceUnavailable(text="No video from cloud relay")
            return response
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            # Drain the pipe after cancellation so process.wait cannot deadlock.
            await process.communicate()

    async def handle(self, request):
        response = web.StreamResponse(
            headers={"Content-Type": "video/mp2t", "Cache-Control": "no-store"}
        )
        retry_delay = 1
        while True:
            try:
                async with aclosing(self.media()) as stream:
                    async for payload in stream:
                        if not response.prepared:
                            await response.prepare(request)
                        await response.write(payload)
                        retry_delay = 1
            except SessionExpired:
                self.reauthenticate()
                if not response.prepared:
                    raise web.HTTPServiceUnavailable(text="Cloud session expired") from None
                break
            except (OSError, EOFError, ValueError, RuntimeError,
                    asyncio.LimitOverrunError) as err:
                # Relay URLs and tokens must never appear in the journal.
                reason = type(err).__name__
                if isinstance(err, RuntimeError) and re.fullmatch(
                    r"relay rejected: code=-?\d+|Relay refused the media connection(?: \(HTTP \d{3}\))?|"
                    r"Encrypted relay payload is unsupported|incomplete relay response|"
                    r"CIPC endpoint discovery rejected: code -?\d+",
                    str(err),
                ):
                    reason = str(err)
                _LOGGER.warning("C200 cloud stream interrupted (%s)", reason)
            if not response.prepared:
                raise web.HTTPServiceUnavailable(text="Cloud stream unavailable")
            if request.transport is None or request.transport.is_closing():
                break
            # Reopen the cloud session inside the existing local MPEG-TS response.
            # Ending HTTP here sends HA's stream worker into increasing retry delays.
            await asyncio.sleep(retry_delay)
            retry_delay = min(5, retry_delay * 2)
        return response
