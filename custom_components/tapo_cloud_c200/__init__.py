"""Tapo C200 cloud camera."""

import logging

from homeassistant.const import Platform, EVENT_HOMEASSISTANT_STOP
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .cloud import SessionExpired, TapoCloud
from .relay import Relay
from .install_blueprint import install

DOMAIN = "tapo_cloud_c200"
PLATFORMS = [Platform.CAMERA]


async def async_setup(hass, config):
    from .frigate import FrigateStream

    try:
        await hass.async_add_executor_job(install, hass.config.config_dir)
    except OSError as err:
        logging.getLogger(__name__).warning("Could not install snapshot blueprint: %s", err)
    hass.http.register_view(FrigateStream(hass))
    return True


async def async_setup_entry(hass, entry):
    data = entry.data
    client = await hass.async_add_executor_job(TapoCloud, data["terminal"])
    client.token = data["token"]
    try:
        devices = await hass.async_add_executor_job(client.cameras, data["region"])
    except SessionExpired as exc:
        raise ConfigEntryAuthFailed("Tapo session expired") from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise ConfigEntryNotReady("Camera list unavailable") from exc
    device = devices.get(entry.unique_id)
    if not device:
        raise ConfigEntryNotReady("Camera missing from Tapo account")
    relay = Relay(
        client, data["region"], device, lambda: entry.async_start_reauth(hass)
    )
    await relay.start()
    entry.runtime_data = relay

    async def stop(_event):
        await relay.close()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop))
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await relay.close()
        raise
    return True


async def async_unload_entry(hass, entry):
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.close()
        return True
    return False
