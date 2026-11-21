"""Live dashboard, snapshots and recordings via Home Assistant Stream."""

from homeassistant.components.camera import Camera, CameraEntityFeature


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([CloudCamera(entry)])


class CloudCamera(Camera):
    _attr_supported_features = CameraEntityFeature.STREAM
    _attr_brand = "TP-Link"
    _attr_model = "Tapo C200"

    def __init__(self, entry):
        super().__init__()
        self._attr_unique_id = entry.unique_id
        self._attr_name = entry.title
        self.relay = entry.runtime_data

    @property
    def use_stream_for_stills(self):
        return True

    async def stream_source(self):
        return self.relay.url

    async def async_refresh_providers(self, *, write_state=True):
        """Use HA's HLS worker. An external WebRTC server cannot reach loopback."""
        return

    async def async_will_remove_from_hass(self):
        if self.stream:
            self.stream.dynamic_stream_settings.preload_stream = False
            await self.stream.stop()
        await super().async_will_remove_from_hass()
