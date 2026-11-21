"""Stable HLS entry point for Frigate, scoped to one camera."""

import base64
import binascii
import hashlib
import hmac

from aiohttp import web
from homeassistant.components.camera.helper import (
    async_stream_endpoint_url,
    get_camera_from_entity_id,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.http import HomeAssistantView


def digest(password):
    return hashlib.sha256(password.encode()).hexdigest()


class FrigateStream(HomeAssistantView):
    url = "/api/tapo_cloud_c200/{entity_id}/live.m3u8"
    name = "api:tapo_cloud_c200:frigate"
    # This route verifies its own, read-only, per-camera credential.
    requires_auth = False

    def __init__(self, hass):
        self.hass = hass

    async def get(self, request, entity_id):
        row = er.async_get(self.hass).async_get(entity_id)
        if not row or row.platform != "tapo_cloud_c200" or not row.config_entry_id:
            raise web.HTTPNotFound()
        entry = self.hass.config_entries.async_get_entry(row.config_entry_id)
        if not entry or not (expected := entry.options.get("frigate_digest")):
            raise web.HTTPNotFound()
        header = request.headers.get("Authorization", "")
        challenge = {"WWW-Authenticate": 'Basic realm="C200 stream"'}
        if not header:
            # FFmpeg's normal first request asks for the authentication scheme.
            return web.Response(status=401, headers=challenge)
        try:
            scheme, encoded = header.split(" ", 1)
            user, password = (
                base64.b64decode(encoded, validate=True).decode().split(":", 1)
            )
        except (ValueError, UnicodeError, binascii.Error):
            raise web.HTTPUnauthorized(headers=challenge) from None
        if (
            scheme.lower() != "basic"
            or user != "frigate"
            or not hmac.compare_digest(digest(password), expected)
        ):
            raise web.HTTPUnauthorized(headers=challenge)
        if entry.state is not ConfigEntryState.LOADED:
            raise web.HTTPServiceUnavailable()
        try:
            camera = get_camera_from_entity_id(self.hass, entity_id)
            playlist = await async_stream_endpoint_url(self.hass, camera, "hls")
        except HomeAssistantError:
            raise web.HTTPServiceUnavailable() from None
        # The HLS player follows HA's session URL for the active stream.
        raise web.HTTPTemporaryRedirect(
            location=playlist, headers={"Cache-Control": "no-store"}
        )
