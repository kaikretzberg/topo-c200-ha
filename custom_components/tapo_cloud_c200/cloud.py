"""Tapo account and relay negotiation adapted from filirnd/topo (MIT)."""

import base64
import hashlib
import hmac
import json
from pathlib import Path
import platform
import ssl
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

APP_NAME = "TP-Link_Tapo_Android"
APP_VERSION = "3.21.114"
LOGIN_URL = "https://n-wap-gw.tplinkcloud.com"
CIPC_URL = "https://cipc.api.cloud"
TP_LINK_ROOTS_PATH = Path(__file__).with_name("roots.pem")

# App-level credentials used by the Tapo Android application.  These identify
# the client application, not the user's Tapo account.
ACCESS_KEY = "4d11b6b9d5ea4d19a829adbb9714b057"
ACCESS_SECRET = "6ed7d97f3e73467f8a5bab90b577ba4c"


def error_code(value: object) -> int:
    if not isinstance(value, dict):
        return 0
    for key in ("error_code", "errorCode"):
        if key in value:
            try:
                return int(value[key])
            except (TypeError, ValueError):
                return 1
    return 0


def result_of(response: dict) -> dict:
    value = response.get("result")
    return value if isinstance(value, dict) else {}


def create_verify_context() -> ssl.SSLContext:
    """Build a verified TLS context for the TP-Link cloud endpoints.

    TP-Link serves a private public-facing CA chain that is included by the
    official Tapo Android client but is not present in many Linux trust stores.
    Keep normal system roots and add the two public TP-Link roots shipped with
    that client. Hostname verification and certificate validation stay enabled.
    """
    context = ssl.create_default_context()
    if not TP_LINK_ROOTS_PATH.is_file():
        raise RuntimeError(f"missing TP-Link trust store: {TP_LINK_ROOTS_PATH}")
    context.load_verify_locations(cafile=str(TP_LINK_ROOTS_PATH))
    return context


class SessionExpired(RuntimeError):
    """The account requires a new sign-in."""


class TapoCloud:
    def __init__(self, terminal_uuid):
        self.terminal_uuid = terminal_uuid
        self.token = ""
        self.cipc_url = CIPC_URL
        self._cipc_discovery_base_url = ""
        self.verify_context = create_verify_context()

    @staticmethod
    def _body(payload: dict) -> bytes:
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()

    @staticmethod
    def _content_md5(body: bytes) -> str:
        return base64.b64encode(hashlib.md5(body).digest()).decode("ascii")

    def _headers(self, body: bytes, endpoint: str) -> dict[str, str]:
        nonce = str(uuid.uuid4())
        timestamp = "9999999999"
        content_md5 = self._content_md5(body)
        message = "\n".join((content_md5, timestamp, nonce, endpoint))
        signature = hmac.new(
            ACCESS_SECRET.encode(), message.encode(), hashlib.sha1
        ).hexdigest()
        return {
            "Content-Type": "application/json; charset=UTF-8",
            "Content-MD5": content_md5,
            "X-Authorization": (
                f"Timestamp={timestamp}, Nonce={nonce}, "
                f"AccessKey={ACCESS_KEY}, Signature={signature}"
            ),
            "User-Agent": "okhttp/3.12.13",
        }

    def post(self, base_url: str, endpoint: str, payload: dict, *, token: bool) -> dict:
        body = self._body(payload)
        query = (
            {
                "token": self.token,
            }
            if token
            else {}
        )
        url = base_url.rstrip("/") + endpoint
        if query:
            url += "?" + urlencode(query)
        request = Request(
            url,
            data=body,
            headers=self._headers(body, endpoint),
            method="POST",
        )
        try:
            with urlopen(request, context=self.verify_context, timeout=30) as response:
                raw = response.read()
        except HTTPError as exc:
            raw = exc.read()
            if exc.code == 401 and token:
                raise SessionExpired("Session expired") from None
        except URLError as exc:
            raise RuntimeError(f"network error for {endpoint}: {exc.reason}") from exc
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"non-JSON response from {endpoint}") from exc
        if not isinstance(decoded, dict):
            raise RuntimeError(f"unexpected response from {endpoint}")
        if error_code(decoded) == -20651:
            raise SessionExpired("Session expired")
        return decoded

    def post_cipc(self, endpoint: str, payload: dict, player_id: str) -> dict:
        body = self._body(payload)
        url = self.cipc_url.rstrip("/") + endpoint
        request = Request(
            url,
            data=body,
            headers={
                # CIPC expects the raw cloud token. The ordinary account API
                # uses the separate `ut|` prefix, but the Android media
                # interceptor does not add it for this service.
                "Authorization": self.token,
                "Content-Type": "application/json; charset=UTF-8",
                "User-Agent": (
                    f"Tapo_APP/Linux/{APP_VERSION}_Android/Android {platform.release()}"
                ),
                "X-Client-Id": player_id,
                "X-Source": "tapo-app",
                "X-Brand": "TPLINK",
                "X-Ca-Type": "cloud-self",
                # The Android CIPC client attaches a short per-request
                # signature/trace id to relay URL requests. It is not the
                # account token; it only identifies this media transaction.
                "X-Request-Signature": uuid.uuid4().hex[:10],
            },
            method="POST",
        )
        try:
            with urlopen(request, context=self.verify_context, timeout=30) as response:
                raw = response.read()
        except HTTPError as exc:
            raw = exc.read()
            if exc.code == 401:
                raise SessionExpired("Session expired") from None
        except URLError as exc:
            self._cipc_discovery_base_url = ""
            raise RuntimeError(f"CIPC network error: {exc.reason}") from exc
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("CIPC returned a non-JSON response") from exc
        if not isinstance(decoded, dict):
            raise RuntimeError("unexpected CIPC response")
        if error_code(decoded) == -20651:
            raise SessionExpired("Session expired")
        return decoded

    def discover_cipc_url(self, base_url: str, *, force: bool = False) -> str:
        if not force and self._cipc_discovery_base_url == base_url:
            return self.cipc_url
        response = self.post(
            base_url,
            "/api/v2/common/getAppServiceUrl",
            {"serviceIds": ["cipc.api.cloud"]},
            token=True,
        )
        if error_code(response) != 0:
            raise RuntimeError(
                f"CIPC endpoint discovery rejected: code {error_code(response)}"
            )
        services = result_of(response).get("serviceUrls")
        if not isinstance(services, dict):
            raise RuntimeError("CIPC endpoint response has no serviceUrls")
        service_url = services.get("cipc.api.cloud")
        if not service_url:
            raise RuntimeError("cipc.api.cloud endpoint missing from response")
        self.cipc_url = str(service_url).replace("http://", "https://", 1)
        self._cipc_discovery_base_url = base_url
        return self.cipc_url

    def login(self, username: str, password: str) -> tuple[str, dict]:
        payload = {
            "appType": APP_NAME,
            "appVersion": APP_VERSION,
            "cloudPassword": password,
            "cloudUserName": username,
            "platform": f"Android {platform.release()}",
            "refreshTokenNeeded": False,
            "supportBindAccount": False,
            "terminalMeta": "1",
            "terminalName": "Home Assistant C200",
            "terminalUUID": self.terminal_uuid,
        }
        response = self.post(LOGIN_URL, "/api/v2/account/login", payload, token=False)
        if error_code(response) != 0:
            raise RuntimeError(f"login rejected: code {error_code(response)}")
        result = result_of(response)
        # Accounts outside the global region return the correct regional URL.
        if error_code(result) == -20212 and result.get("appServerUrl"):
            regional_url = str(result["appServerUrl"])
            if urlsplit(regional_url).scheme != "https":
                raise RuntimeError("Invalid regional login endpoint")
            response = self.post(
                regional_url, "/api/v2/account/login", payload, token=False
            )
            if error_code(response) != 0:
                raise RuntimeError(
                    f"regional login rejected: code {error_code(response)}"
                )
            result = result_of(response)
            return regional_url, result
        return LOGIN_URL, result

    def request_mfa(
        self, base_url: str, username: str, password: str, mfa_type: int
    ) -> None:
        """Request an email code or app push for a pending login."""
        if mfa_type not in (1, 2):
            raise ValueError("MFA method is not supported")
        endpoint = (
            "/api/v2/account/getPushVC4TerminalMFA"
            if mfa_type == 1
            else "/api/v2/account/getEmailVC4TerminalMFA"
        )
        request_payload = {
            "appType": APP_NAME,
            "cloudPassword": password,
            "cloudUserName": username,
            "terminalUUID": self.terminal_uuid,
        }
        sent = self.post(base_url, endpoint, request_payload, token=False)
        outer_code = error_code(sent)
        inner_code = error_code(result_of(sent))
        if outer_code != 0 or inner_code != 0:
            code = inner_code if inner_code != 0 else outer_code
            raise RuntimeError(f"MFA code request failed: code {code}")

    def verify_mfa(
        self,
        base_url: str,
        username: str,
        code: str,
        process_id: str,
        mfa_type: int,
    ) -> dict:
        """Complete a pending MFA login with the user-provided code."""
        if not code:
            raise ValueError("MFA code is required")
        checked = self.post(
            base_url,
            "/api/v2/account/checkMFACodeAndLogin",
            {
                "appType": APP_NAME,
                "cloudUserName": username,
                "code": code,
                "MFAProcessId": process_id,
                "MFAType": mfa_type,
                "terminalBindEnabled": True,
            },
            token=False,
        )
        outer_code = error_code(checked)
        inner_code = error_code(result_of(checked))
        if outer_code != 0 or inner_code != 0:
            code = inner_code if inner_code != 0 else outer_code
            raise RuntimeError(f"MFA code rejected: code {code}")
        return result_of(checked)

    def cameras(self, base_url):
        devices = {}
        index = 0
        while True:
            response = self.post(
                base_url,
                "/api/v2/common/getDeviceListByPage",
                {
                    "deviceTypeList": ["SMART.IPCAMERA"],
                    "index": index,
                    "limit": 20,
                },
                token=True,
            )
            if error_code(response):
                raise RuntimeError("Camera list request failed")
            items = result_of(response).get("deviceList", [])
            previous = len(devices)
            devices.update((d["deviceId"], d) for d in items if d.get("deviceId"))
            if len(items) < 20 or len(devices) == previous:
                break
            index += len(items)
        return {
            key: device
            for key, device in devices.items()
            if "C200" in str(device.get("deviceModel", "")).upper()
        }

    def relay_request(self, base_url, device):
        device_id = str(device.get("deviceId") or "")
        if not device_id:
            raise RuntimeError("camera has no deviceId")
        self.discover_cipc_url(base_url)
        player_id = str(uuid.uuid4())
        request_param = {
            "deviceId": device_id,
            "dataTimeoutCount": 0,
            "cloudType": 2,
            "deviceType": device.get("deviceType") or "SMART.IPCAMERA",
            "playerId": player_id,
            "rootCaVer": "1",
            "streamType": 0,
            "preConnection": 0,
            "resolution": "HD",
        }
        response = self.post_cipc("/v2/relay/request", request_param, player_id)
        relay_info = result_of(response)
        if error_code(response) != 0 or not isinstance(relay_info, dict):
            raise RuntimeError(f"relay rejected: code={error_code(response)}")
        relay_url = str(relay_info.get("relayUrl") or "")
        relay_token = str(
            relay_info.get("relayToken") or relay_info.get("sessionToken") or ""
        )
        if not relay_url or not relay_token:
            raise RuntimeError("incomplete relay response")
        parsed = urlsplit(relay_url if "://" in relay_url else "https://" + relay_url)
        host = parsed.hostname
        if not host:
            raise RuntimeError("relay has no hostname")
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query + "&retryTime=0"
        else:
            path += "?retryTime=0"
        headers = {
            "Host": host,
            "User-Agent": "Client=TP-Link_Tapo_Android Android 3.21.114/1.3",
            "Keep-Relay": "3600",
            "Accept": "*/*",
            "Content-Type": "multipart/mixed;boundary=--client-stream-boundary--",
            "Content-Length": "9223372036854775807",
            "X-token": relay_token,
            # The Android relay client sends the camera/client model here;
            # it is not the operating-system name.
            "X-Client-Model": str(device.get("deviceModel") or "C200"),
            "X-Client-UUID": player_id,
            "X-Redirect-Times": "0",
            # PullMode.DIRECT_FORWARDING in the official app is the string 2.
            "X-Pull-Mode": "2",
            "X-Version": "2.0",
            "X-Arrive-Latency": "0",
            "Connection": "Keep-alive",
        }
        if relay_info.get("elbCookie"):
            headers["Cookie"] = str(relay_info["elbCookie"])
        raw_request = (
            f"POST {path} HTTP/1.1\r\n"
            + "".join(f"{key}: {value}\r\n" for key, value in headers.items())
            + "\r\n"
        ).encode("ascii")

        return host, parsed.port or 443, raw_request
