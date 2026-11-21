"""Interactive account sign-in, MFA, and cloud camera selection."""

import uuid

try:
    import probatio as vol
except ImportError:  # Older HA releases use Voluptuous.
    import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from .frigate import digest
from homeassistant.helpers import selector

from . import DOMAIN
from .cloud import TapoCloud


def choice(options):
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[{"value": key, "label": label} for key, label in options.items()],
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


class TapoFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return FrigateOptions()

    def __init__(self):
        self.terminal = uuid.uuid4().hex.upper()
        self.previous = None
        self.password = None

    async def async_step_reauth(self, entry_data):
        self.previous = self._get_reauth_entry()
        self.terminal = self.previous.data["terminal"]
        return await self.async_step_user()

    async def async_step_reconfigure(self, user_input=None):
        self.previous = self._get_reconfigure_entry()
        self.terminal = self.previous.data["terminal"]
        return await self.async_step_user(user_input)

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input:
            self.email = user_input["email"].strip()
            self.client = await self.hass.async_add_executor_job(
                TapoCloud, self.terminal
            )
            try:
                self.region, result = await self.hass.async_add_executor_job(
                    self.client.login, self.email, user_input["password"]
                )
                if result.get("MFAProcessId"):
                    self.process = result["MFAProcessId"]
                    self.methods = {
                        str(m) for m in result.get("supportedMFATypes") or [2]
                    }
                    self.password = user_input["password"]
                    return await self.async_step_method()
                if result.get("token"):
                    self.client.token = result["token"]
                    return await self.async_step_camera()
                errors["base"] = "invalid_auth"
            except (OSError, RuntimeError, ValueError):
                errors["base"] = "login_failed"
        return self.async_show_form(
            step_id="user",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required("email"): str,
                    vol.Required("password"): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    ),
                }
            ),
        )

    async def async_step_method(self, user_input=None):
        errors = {}
        methods = {
            k: v
            for k, v in {"2": "E-Mail", "1": "Tapo App"}.items()
            if k in self.methods
        }
        if not methods:
            return self.async_abort(reason="unsupported_mfa")
        if user_input:
            self.method = int(user_input["method"])
            try:
                await self.hass.async_add_executor_job(
                    self.client.request_mfa,
                    self.region,
                    self.email,
                    self.password,
                    self.method,
                )
            except (OSError, RuntimeError, ValueError):
                errors["base"] = "send_failed"
            else:
                self.password = None
                return await self.async_step_code()
        return self.async_show_form(
            step_id="method",
            errors=errors,
            data_schema=vol.Schema(
                {vol.Required("method", default=next(iter(methods))): choice(methods)}
            ),
        )

    async def async_step_code(self, user_input=None):
        errors = {}
        if user_input:
            try:
                result = await self.hass.async_add_executor_job(
                    self.client.verify_mfa,
                    self.region,
                    self.email,
                    user_input["code"].strip(),
                    self.process,
                    self.method,
                )
                self.client.token = result["token"]
            except (OSError, RuntimeError, ValueError, KeyError):
                errors["base"] = "code_failed"
            else:
                return await self.async_step_camera()
        return self.async_show_form(
            step_id="code",
            errors=errors,
            data_schema=vol.Schema({vol.Required("code"): str}),
        )

    async def async_step_camera(self, user_input=None):
        if user_input is None:
            try:
                self.devices = await self.hass.async_add_executor_job(
                    self.client.cameras, self.region
                )
            except (OSError, RuntimeError, ValueError):
                return self.async_abort(reason="list_failed")
        options = {
            key: f"{d.get('alias') or 'Tapo C200'} (…{key[-6:]})"
            for key, d in self.devices.items()
            if self.previous is None or key == self.previous.unique_id
        }
        if not options:
            return self.async_abort(reason="no_camera")
        if user_input:
            key = user_input["camera"]
            if key not in options:
                return self.async_abort(reason="no_camera")
            data = {
                "token": self.client.token,
                "region": self.region,
                "terminal": self.terminal,
            }
            if self.previous:
                return self.async_update_reload_and_abort(self.previous, data=data)
            await self.async_set_unique_id(key)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=str(self.devices[key].get("alias") or "Tapo C200"), data=data
            )
        return self.async_show_form(
            step_id="camera",
            data_schema=vol.Schema(
                {vol.Required("camera", default=next(iter(options))): choice(options)}
            ),
        )


class FrigateOptions(config_entries.OptionsFlowWithReload):
    async def async_step_init(self, user_input=None):
        errors = {}
        existing = self.config_entry.options.get("frigate_digest")
        if user_input is not None:
            enabled = user_input["enable_frigate"]
            password = user_input.get("password", "")
            if not enabled:
                return self.async_create_entry(title="", data={})
            if not password and existing:
                return self.async_create_entry(
                    title="", data={"frigate_digest": existing}
                )
            if 20 <= len(password) <= 128:
                return self.async_create_entry(
                    title="", data={"frigate_digest": digest(password)}
                )
            errors["password"] = "invalid_stream_password"
        return self.async_show_form(
            step_id="init",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required("enable_frigate", default=bool(existing)): bool,
                    vol.Optional("password"): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    ),
                }
            ),
        )
