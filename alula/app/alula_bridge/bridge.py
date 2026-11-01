"""Alula cloud <-> MQTT discovery bridge.

Uses the same api.alula.net OAuth + WebSocket command path as the Alula Security
mobile app (changeArmingLevelUsingCode for arm/disarm; writeMfd zoneBypass /
zoneUnbypass for bypass). The account's interactiveEnabled flag is reported as
an informational sensor only and NEVER blocks a command: the Android app can
arm/disarm this account regardless of that flag.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .api import (
    LEVEL_AWAY,
    LEVEL_DISARM,
    LEVEL_NIGHT,
    LEVEL_STAY,
    AlulaAuthError,
    AlulaClient,
    AlulaCredentialsRejected,
    AlulaError,
    PanelState,
    Zone,
)
from .ha_core import HaCore
from .mqtt_client import Mqtt, MqttCodeError, MqttError
from .zones import device_class, is_placeholder, slugify

_LOGGER = logging.getLogger(__name__)
DISC = "homeassistant"
PREFIX = "alula"
PROMPT_TAG = "alula_arm_prompt"
ACTION_CONFIRM = "ALULA_BYPASS_AND_ARM"
ACTION_CANCEL = "ALULA_CANCEL_ARM"
# Codes an automation can pass to alarm_control_panel.alarm_arm_* (code: ...)
# to pick the automation path. Digits are still treated as a panel PIN.
CODE_TOKENS_BYPASS = {"BYPASS", "AUTO", "AUTO_BYPASS", "AUTOBYPASS"}
CODE_TOKENS_INSTANT = {"INSTANT", "NO_DELAY", "NODELAY"}
MODE_LABEL = {"home": "Stay", "away": "Away", "night": "Night (stay, instant)"}
NOTIFY_NAME = re.compile(r"^[a-z0-9_]+$")
AUTH_BLOCK_PATH = os.environ.get("ALULA_AUTH_BLOCK", "/data/auth_block.json")
# Wrong disarm codes: this many within the window locks HA-side disarm for a while.
BAD_CODE_LIMIT = 5
BAD_CODE_WINDOW = 600.0
BAD_CODE_LOCKOUT = 120.0
STARTUP_BACKOFF_MAX = 1800.0
# MQTT CONNACK codes for rejected credentials (MQTT 3.1.1: 4, 5; MQTT 5: 134, 135).
MQTT_AUTH_CODES = {4, 5, 134, 135}
# Give up and exit (so Supervisor restarts the add-on and it re-reads the broker
# credentials) after this long without a working broker connection.
MQTT_GIVE_UP_SECONDS = float(os.environ.get("ALULA_MQTT_GIVE_UP", "300"))
# Pause before exiting on rejected broker credentials, so a restart loop is slow.
MQTT_AUTH_EXIT_DELAY = float(os.environ.get("ALULA_MQTT_AUTH_EXIT_DELAY", "30"))


class MqttFatal(Exception):
    """The broker connection cannot recover in-process; exit non-zero."""


def _mqtt_rc(err: BaseException) -> Optional[int]:
    if not isinstance(err, MqttCodeError):
        return None
    rc = getattr(err, "rc", None)
    value = getattr(rc, "value", rc)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_mqtt_auth_error(err: BaseException) -> bool:
    return _mqtt_rc(err) in MQTT_AUTH_CODES


class UserError(AlulaError):
    """An error whose text is safe and useful to show in a notification."""


def _valid_pin(code: str) -> bool:
    # DSC PowerSeries user codes are 4 digits.
    return bool(code) and code.isdigit() and len(code) == 4


@dataclass
class Options:
    username: str
    password: str
    code: str = ""
    poll_interval: int = 30
    level_home: int = 0   # 0 = derive from panel armingLevelName
    level_away: int = 0
    level_night: int = 0
    device_name: str = "Alula Alarm"
    log_level: str = "info"
    stay_instant: bool = False        # Arm Home always with no entry delay
    night_instant: bool = True        # Arm Night = Stay + no entry delay (if no native Night)
    prompt_timeout: int = 60          # seconds a manual-arm bypass prompt stays open
    notify_services: str = ""         # e.g. "mobile_app_my_phone, mobile_app_other"

    @classmethod
    def from_options_file(cls, path: str) -> "Options":
        with open(path) as f:
            raw = json.load(f)
        return cls(
            username=str(raw["username"]).strip(),
            password=str(raw["password"]),
            code=str(raw.get("code") or "").strip(),
            poll_interval=max(10, int(raw.get("poll_interval", 30))),
            level_home=int(raw.get("level_home", 0)),
            level_away=int(raw.get("level_away", 0)),
            level_night=int(raw.get("level_night", 0)),
            device_name=str(raw.get("device_name") or "Alula Alarm").strip() or "Alula Alarm",
            log_level=str(raw.get("log_level") or "info").lower(),
            stay_instant=bool(raw.get("stay_instant", False)),
            night_instant=bool(raw.get("night_instant", True)),
            prompt_timeout=min(300, max(15, int(raw.get("prompt_timeout", 60)))),
            notify_services=str(raw.get("notify_services") or "").strip(),
        )

    @property
    def notify_list(self) -> list[str]:
        out = []
        for part in self.notify_services.replace(";", ",").split(","):
            svc = part.strip()
            if svc.startswith("notify."):
                svc = svc[len("notify."):]
            if not svc:
                continue
            if not NOTIFY_NAME.match(svc):
                _LOGGER.warning("Ignoring invalid notify service name %r "
                                "(allowed: lowercase letters, digits, _)", svc[:64])
                continue
            out.append(svc)
        return out

    def credentials_fingerprint(self) -> str:
        # Salted hash so the block file never holds the credentials themselves.
        return hashlib.sha256(
            ("alula-block\0" + self.username + "\0" + self.password).encode()
        ).hexdigest()


def _mqtt_opts() -> dict:
    # Supervisor injects these when services: mqtt:need is set in config.yaml
    return {
        "host": os.environ.get("MQTT_HOST") or os.environ.get("MQTTHOST") or "core-mosquitto",
        "port": int(os.environ.get("MQTT_PORT") or os.environ.get("MQTTPORT") or 1883),
        "username": os.environ.get("MQTT_USERNAME") or os.environ.get("MQTTUSER") or None,
        "password": os.environ.get("MQTT_PASSWORD") or os.environ.get("MQTTPASS") or None,
    }


class Bridge:
    def __init__(self, options: Options, mqtt_opts: Optional[dict] = None) -> None:
        self.opt = options
        self.mqtt_opts = mqtt_opts or _mqtt_opts()
        self.mqtt: Optional[Mqtt] = None
        self.client = AlulaClient(
            username=options.username,
            password=options.password,
            update_callback=self._on_push,
        )
        self._device_id: Optional[str] = None
        self._published_zones: set[int] = set()
        self._stop = asyncio.Event()
        self._cmd_lock = asyncio.Lock()
        self._disarm_lock = asyncio.Lock()
        self._arm_tasks: set[asyncio.Task] = set()
        self._bg_tasks: set[asyncio.Task] = set()
        self._bad_codes: list[float] = []
        self._disarm_locked_until = 0.0
        self._auth_blocked = False
        self.ha = HaCore()
        # Pending manual-arm request waiting for "bypass and arm" / "cancel".
        self._prompt: Optional[dict] = None
        self._prompt_task: Optional[asyncio.Task] = None
        self._last_poll: Optional[datetime] = None

    # ---- lifecycle -------------------------------------------------------

    async def _sleep(self, seconds: float) -> bool:
        """Sleep unless stopping. Returns True if a stop was requested."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    # Rejected-credential block, persisted so an add-on/HA restart does not retry
    # the same bad password. Changing the username or password clears it.
    def _read_block(self) -> Optional[dict]:
        try:
            with open(AUTH_BLOCK_PATH) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        if data.get("fingerprint") == self.opt.credentials_fingerprint():
            return data
        with contextlib.suppress(OSError):
            os.remove(AUTH_BLOCK_PATH)  # options changed -> clear
        return None

    def _write_block(self, notified: bool) -> None:
        data = {"fingerprint": self.opt.credentials_fingerprint(),
                "since": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "notified": notified}
        try:
            fd = os.open(AUTH_BLOCK_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
        except OSError as err:
            _LOGGER.warning("could not save the login block marker: %s", err)

    async def _credentials_rejected(self, block: Optional[dict] = None) -> None:
        """Stop all logins until the options change; notify once; idle."""
        self._auth_blocked = True
        notified = bool(block and block.get("notified"))
        _LOGGER.error("Alula rejected the username or password. The add-on will not try "
                      "again until you change them on the Configuration tab.")
        if not notified:
            ok = await self.ha.call_service("persistent_notification", "create", {
                "notification_id": "alula_login",
                "title": "Alula alarm: login rejected",
                "message": ("Alula rejected the username or password. The add-on has "
                            "stopped trying so the account is not locked out. Update them "
                            "on the add-on's Configuration tab to try again."),
            })
            notified = ok
        self._write_block(notified)
        with contextlib.suppress(Exception):
            await self.client.async_close()
        await self._pub(self._topic("availability"), "offline")

    async def _startup(self) -> bool:
        """Log in and load the panel, retrying with backoff. False = stop/blocked."""
        block = self._read_block()
        if block:
            await self._credentials_rejected(block)
            return False
        delay = 30.0
        while not self._stop.is_set():
            try:
                await self.client.async_login(self.opt.username, self.opt.password)
                panels = await self.client.async_get_panels()
                if not panels:
                    raise UserError("No alarm panel found on this Alula account")
                self._device_id = panels[0].device_id
                await self.client.async_connect_ws()
                await self.client.async_subscribe_device(self._device_id)
                await self.client.async_refresh_state(self._device_id, budget=45.0)
                await asyncio.sleep(1)
                self._last_poll = datetime.now(timezone.utc)
                return True
            except AlulaCredentialsRejected:
                await self._credentials_rejected()
                return False
            except (AlulaError, OSError, asyncio.TimeoutError) as err:
                wait = max(delay, self.client.login_wait)
                _LOGGER.warning("Alula startup failed (%s); retrying in %ds", err, int(wait))
                if await self._sleep(wait):
                    return False
                delay = min(STARTUP_BACKOFF_MAX, delay * 2)
        return False

    async def run(self) -> None:
        if not await self._startup():
            await self._stop.wait()  # blocked or stopping: stay up, do nothing
            await self._shutdown(None)
            return
        poll = asyncio.create_task(self._poll_loop(), name="alula_poll")
        ha_events = asyncio.create_task(
            self.ha.listen_events("mobile_app_notification_action", self._on_ha_action, self._stop),
            name="alula_ha_events",
        )
        ha_disarm = asyncio.create_task(
            self.ha.listen_events("alula_auto_disarm", self._on_ha_auto_disarm, self._stop),
            name="alula_ha_auto_disarm",
        )
        if not self.ha.available:
            _LOGGER.warning("No Supervisor token: arm prompts will use the dashboard "
                            "sensor/buttons only (no HA notifications)")
        try:
            await self._mqtt_loop(poll)
        finally:
            poll.cancel()
            ha_events.cancel()
            ha_disarm.cancel()
            await self._shutdown(None)

    async def _mqtt_loop(self, poll: asyncio.Task) -> None:
        """Connect to the broker, serve commands, and reconnect with backoff (1-60 s)."""
        backoff = 1.0
        failing_since: Optional[float] = None
        while not self._stop.is_set():
            try:
                async with Mqtt(**self.mqtt_opts, client_id="alula_bridge",
                                will_topic=self._topic("availability")) as mqtt:
                    self.mqtt = mqtt
                    await self._publish_discovery()
                    await self._publish_state()
                    await self._subscribe_commands()
                    await self._pub(self._topic("availability"),
                                    "offline" if self._auth_blocked else "online")
                    await self._publish_last_poll()
                    _LOGGER.info("MQTT connected")
                    backoff = 1.0
                    failing_since = None
                    listen = asyncio.create_task(mqtt.listen(), name="alula_mqtt_listen")
                    stop = asyncio.create_task(self._stop.wait())
                    done, _ = await asyncio.wait({listen, stop, poll},
                                                 return_when=asyncio.FIRST_COMPLETED)
                    stop.cancel()
                    if poll in done and not self._stop.is_set():
                        listen.cancel()
                        poll.result()  # re-raise an unexpected poll-loop crash
                        raise RuntimeError("poll loop ended unexpectedly")
                    if listen in done:
                        listen.result()  # raises MqttError on a dropped connection
                        raise MqttError("MQTT message stream ended")
                    listen.cancel()
                    with contextlib.suppress(Exception):
                        await self._pub(self._topic("availability"), "offline")
                    return
            except MqttError as err:
                self.mqtt = None
                if self._stop.is_set():
                    return
                if _is_mqtt_auth_error(err):
                    _LOGGER.error(
                        "MQTT broker rejected the add-on's credentials (%s); exiting in %ds "
                        "so the add-on restarts and fetches fresh credentials from Supervisor",
                        err, int(MQTT_AUTH_EXIT_DELAY))
                    await self._sleep(MQTT_AUTH_EXIT_DELAY)
                    raise MqttFatal(f"MQTT authentication failed: {err}") from err
                now = time.monotonic()
                if failing_since is None:
                    failing_since = now
                elif now - failing_since >= MQTT_GIVE_UP_SECONDS:
                    _LOGGER.error("MQTT broker unreachable for %ds (%s); exiting so the "
                                  "add-on restarts", int(now - failing_since), err)
                    raise MqttFatal(f"MQTT unavailable: {err}") from err
                _LOGGER.warning("MQTT connection lost (%s); reconnecting in %ds", err, int(backoff))
                if await self._sleep(backoff):
                    return
                backoff = min(60.0, backoff * 2)
            finally:
                self.mqtt = None

    async def _shutdown(self, _exc) -> None:
        for t in list(self._arm_tasks):
            t.cancel()
        if self._prompt_task:
            self._prompt_task.cancel()
        with contextlib.suppress(Exception):
            await self.ha.close()
        with contextlib.suppress(Exception):
            await self.client.async_close()

    def request_stop(self) -> None:
        self._stop.set()

    async def _pub(self, topic: str, payload: Any, *, retain: bool = True) -> None:
        """Publish if connected; drop silently while the broker is away (state is
        republished in full on reconnect)."""
        if self.mqtt is None:
            return
        try:
            await self.mqtt.publish(topic, payload, retain=retain)
        except MqttError as err:
            _LOGGER.debug("publish to %s dropped: %s", topic, err)

    # ---- discovery -------------------------------------------------------

    def _dev(self) -> dict:
        p = self.client.panels[self._device_id]
        return {
            "identifiers": [f"alula_{self._device_id}"],
            "name": self.opt.device_name,
            "manufacturer": "Alula",
            "model": f"BAT-Connect ({p.connected_panel or 'panel'})",
            "sw_version": p.firmware_version or "",
            "serial_number": p.serial_number or "",
        }

    def _topic(self, *parts: str) -> str:
        return "/".join((PREFIX, self._device_id or "unknown", *parts))

    async def _publish_discovery(self) -> None:
        assert self._device_id
        p = self.client.panels[self._device_id]
        levels = self._levels(p)
        modes = []
        if levels["home"]:
            modes.append("arm_home")
        if levels["away"]:
            modes.append("arm_away")
        if levels["night"] or self._night_is_stay_instant(p):
            modes.append("arm_night")
        # Alarm control panel: arming needs no code; DISARM needs the panel code.
        # `code: REMOTE_CODE` makes HA report code_format=number, so the more-info
        # dialog, the alarm panel card and tile keypads ask for a code on disarm
        # only (code_arm_required=false). The entered digits arrive in the command
        # payload ("DISARM,<code>") and the add-on checks them against the saved
        # code before anything is sent to the panel.
        panel_cfg: dict[str, Any] = {
            "name": None,  # use the device name (avoids "Alula Alarm Alula Alarm")
            "object_id": "alula_alarm",
            "unique_id": f"alula_{self._device_id}_panel",
            "state_topic": self._topic("alarm", "state"),
            "command_topic": self._topic("alarm", "set"),
            "command_template": "{{ action }}{% if code %},{{ code }}{% endif %}",
            "payload_arm_home": "ARM_HOME",
            "payload_arm_away": "ARM_AWAY",
            "payload_arm_night": "ARM_NIGHT",
            "payload_disarm": "DISARM",
            "supported_features": modes,
            "availability_topic": self._topic("availability"),
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": self._dev(),
            "json_attributes_topic": self._topic("alarm", "attrs"),
            "code": "REMOTE_CODE",
            "code_arm_required": False,
            "code_disarm_required": True,
            "code_trigger_required": False,
        }
        await self._pub(
            f"{DISC}/alarm_control_panel/alula_{self._device_id}/config",
            panel_cfg,
        )
        # Status / trouble sensors. "AC failure" uses device_class=problem so
        # "Off" means no failure (the previous "AC power" / power class inverted it).
        for key, name, dc in (
            ("ready", "Ready to arm", None),
            ("alarm", "Alarm", "safety"),
            ("tamper", "Tamper", "tamper"),
            ("ac_failure", "AC failure", "problem"),
            ("low_battery", "Low battery", "battery"),
            ("remote_arming", "Remote arming enabled", None),
        ):
            cfg: dict[str, Any] = {
                "name": name,
                "unique_id": f"alula_{self._device_id}_{key}",
                "state_topic": self._topic("status", key),
                "payload_on": "ON",
                "payload_off": "OFF",
                "availability_topic": self._topic("availability"),
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": self._dev(),
            }
            if dc:
                cfg["device_class"] = dc
            if key != "ready" and key != "alarm":
                cfg["entity_category"] = "diagnostic"
            await self._pub(f"{DISC}/binary_sensor/alula_{self._device_id}_{key}/config", cfg)

        await self._pub(
            f"{DISC}/sensor/alula_{self._device_id}_bypassed/config",
            {
                "name": "Bypassed zones",
                "unique_id": f"alula_{self._device_id}_bypassed",
                "state_topic": self._topic("status", "bypassed"),
                "json_attributes_topic": self._topic("status", "bypassed_attrs"),
                "availability_topic": self._topic("availability"),
                "payload_available": "online",
                "payload_not_available": "offline",
                "icon": "mdi:shield-off-outline",
                "device": self._dev(),
            },
        )
        # Single "Bypass open zones" / "Unbypass all" buttons (not per-zone switches)
        for key, name, icon in (
            ("bypass_open", "Bypass open zones", "mdi:shield-off"),
            ("unbypass_all", "Unbypass all", "mdi:shield-check"),
        ):
            await self._pub(
                f"{DISC}/button/alula_{self._device_id}_{key}/config",
                {
                    "name": name,
                    "unique_id": f"alula_{self._device_id}_{key}",
                    "command_topic": self._topic("button", key),
                    "payload_press": "PRESS",
                    "availability_topic": self._topic("availability"),
                    "payload_available": "online",
                    "payload_not_available": "offline",
                    "icon": icon,
                    "device": self._dev(),
                },
            )
        # Arm prompt (manual arm with open zones) + automation auto-bypass arm buttons.
        for key, name, icon in (
            ("prompt_confirm", "Bypass open zones and arm", "mdi:shield-lock-open"),
            ("prompt_cancel", "Cancel arm", "mdi:close-octagon"),
            ("arm_home_bypass", "Arm stay (auto-bypass)", "mdi:shield-home"),
            ("arm_away_bypass", "Arm away (auto-bypass)", "mdi:shield-lock"),
        ):
            await self._pub(
                f"{DISC}/button/alula_{self._device_id}_{key}/config",
                {
                    "name": name,
                    "unique_id": f"alula_{self._device_id}_{key}",
                    "command_topic": self._topic("button", key),
                    "payload_press": "PRESS",
                    "availability_topic": self._topic("availability"),
                    "payload_available": "online",
                    "payload_not_available": "offline",
                    "icon": icon,
                    "device": self._dev(),
                },
            )
        await self._pub(
            f"{DISC}/sensor/alula_{self._device_id}_arm_prompt/config",
            {
                "name": "Arm prompt",
                "unique_id": f"alula_{self._device_id}_arm_prompt",
                "state_topic": self._topic("prompt", "state"),
                "json_attributes_topic": self._topic("prompt", "attrs"),
                "availability_topic": self._topic("availability"),
                "payload_available": "online",
                "payload_not_available": "offline",
                "icon": "mdi:shield-alert-outline",
                "device": self._dev(),
            },
        )
        # Last successful Alula poll (UTC timestamp), published after each poll
        # that reached the panel and on connect. It expires (goes unavailable)
        # if polling stops, and its value shows staleness even when the broker
        # still holds an old retained "online" (e.g. after restoring a backup),
        # so an HA automation can restart the add-on.
        await self._pub(
            f"{DISC}/sensor/alula_{self._device_id}_last_poll/config",
            {
                "name": "Last poll",
                "object_id": "alula_alarm_last_poll",
                "unique_id": f"alula_{self._device_id}_last_poll",
                "state_topic": self._topic("last_poll"),
                "device_class": "timestamp",
                "entity_category": "diagnostic",
                "expire_after": self._last_poll_expiry(),
                "availability_topic": self._topic("availability"),
                "payload_available": "online",
                "payload_not_available": "offline",
                "icon": "mdi:cloud-check-outline",
                "device": self._dev(),
            },
        )
        await self._publish_prompt()
        await self._discover_zones()
        await self._pub(self._topic("availability"), "online", retain=True)

    async def _discover_zones(self) -> None:
        assert self._device_id
        p = self.client.panels[self._device_id]
        for idx, z in p.zones.items():
            if p.highest_zone_index is not None and idx > p.highest_zone_index:
                continue
            if z.installed is False:
                continue
            if is_placeholder(z.name):
                continue
            await self._discover_zone(z)
            self._published_zones.add(idx)

    async def _discover_zone(self, z: Zone) -> None:
        name = z.name or f"Zone {z.index + 1}"
        slug = slugify(name)
        uid = f"alula_{self._device_id}_z{z.index}"
        await self._pub(
            f"{DISC}/binary_sensor/{uid}/config",
            {
                "name": name,
                "unique_id": uid,
                "state_topic": self._topic("zone", str(z.index), "state"),
                "json_attributes_topic": self._topic("zone", str(z.index), "attrs"),
                "payload_on": "ON",
                "payload_off": "OFF",
                "device_class": device_class(z.name, z.ui_type),
                "availability_topic": self._topic("availability"),
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": self._dev(),
                "object_id": f"alula_{slug}",
            },
        )

    # ---- state -----------------------------------------------------------

    def _levels(self, p: PanelState) -> dict[str, int]:
        levels = {"home": LEVEL_STAY, "away": LEVEL_AWAY, "night": 0}
        names = p.arming_level_names or {}
        if names:
            derived = {"home": 0, "away": 0, "night": 0}
            for idx, label in names.items():
                try:
                    lvl = int(idx) + 1
                except (TypeError, ValueError):
                    continue
                lab = str(label or "").strip().lower()
                if lab in ("stay", "home", "arm stay", "armed stay"):
                    derived["home"] = lvl
                elif lab in ("away", "arm away", "armed away"):
                    derived["away"] = lvl
                elif lab in ("night", "arm night", "armed night"):
                    derived["night"] = lvl
            if derived["home"] or derived["away"]:
                levels = derived
        # Non-zero option overrides win. Zero means "use derived / default".
        if self.opt.level_home:
            levels["home"] = self.opt.level_home
        if self.opt.level_away:
            levels["away"] = self.opt.level_away
        if self.opt.level_night:
            levels["night"] = self.opt.level_night
        return levels

    def _night_is_stay_instant(self, p: PanelState) -> bool:
        """Night maps to Stay + no entry delay when the panel has no Night level."""
        return bool(self.opt.night_instant and not self._levels(p)["night"])

    def _resolve_mode(self, p: PanelState, mode: str) -> tuple[int, bool]:
        """(armingLevelValue, no_entry_delay) for an HA mode home/away/night."""
        levels = self._levels(p)
        if mode == "home":
            return levels["home"], bool(self.opt.stay_instant)
        if mode == "away":
            return levels["away"], False
        if mode == "night":
            if levels["night"]:
                return levels["night"], False
            if self._night_is_stay_instant(p):
                return levels["home"], True
        return 0, False

    def _alarm_state(self, p: PanelState) -> str:
        if p.alarm:
            return "triggered"
        if p.in_entry_delay:
            return "pending"
        if p.in_exit_delay:
            return "arming"
        lvl = p.arming_level
        if lvl == LEVEL_DISARM:
            return "disarmed"
        levels = self._levels(p)
        if (lvl == levels["home"] and p.arming_no_delay and not self.opt.stay_instant
                and self._night_is_stay_instant(p)):
            return "armed_night"  # stay armed with no entry delay == our Night
        for mode, state in (("away", "armed_away"), ("home", "armed_home"), ("night", "armed_night")):
            if levels[mode] and lvl == levels[mode]:
                return state
        if lvl and lvl > LEVEL_DISARM:
            return "armed_custom_bypass"
        return "unknown"

    async def _publish_state(self) -> None:
        assert self._device_id
        p = self.client.panels[self._device_id]
        await self._pub(self._topic("alarm", "state"), self._alarm_state(p))
        await self._pub(
            self._topic("alarm", "attrs"),
            {
                "arming_level": p.arming_level,
                "arming_level_names": p.arming_level_names,
                "level_mapping": self._levels(p),
                "ready_to_arm": p.ready,
                "entry_delay_disabled": p.arming_no_delay,
                "stay_instant": self.opt.stay_instant,
                "night_is_stay_instant": self._night_is_stay_instant(p),
                "open_zones": [z.name or f"zone {z.index + 1}" for z in p.not_ready_zones],
                "alarm_type": p.alarm_type,
                "remote_arming_enabled": p.interactive_enabled,
                "interactive_service_selected": p.interactive_service_selected,
                "last_armed_at": p.last_armed_at,
                "last_disarmed_at": p.last_disarmed_at,
            },
        )
        async def onoff(key: str, val: Optional[bool]) -> None:
            if val is None:
                return
            await self._pub(self._topic("status", key), "ON" if val else "OFF")

        await onoff("ready", p.ready)
        await onoff("alarm", p.alarm)
        await onoff("tamper", p.tamper)
        await onoff("ac_failure", p.ac_failure)
        await onoff("low_battery", p.low_battery)
        # Informational only — does NOT gate commands (Android app can arm despite False).
        await onoff("remote_arming", p.interactive_enabled)

        bypassed = [z for z in p._configured_zones() if z.bypassed]
        await self._pub(
            self._topic("status", "bypassed"),
            ", ".join(z.name or f"zone {z.index + 1}" for z in bypassed) or "none",
        )
        await self._pub(
            self._topic("status", "bypassed_attrs"),
            {"zones": [{"index": z.index, "number": z.index + 1, "name": z.name} for z in bypassed]},
        )
        for idx in list(self._published_zones):
            z = p.zones.get(idx)
            if z is None:
                continue
            await self._pub(
                self._topic("zone", str(idx), "state"),
                "ON" if z.open else "OFF",
            )
            await self._pub(
                self._topic("zone", str(idx), "attrs"),
                {
                    "zone_index": z.index,
                    "panel_zone_number": z.index + 1,
                    "bypassed": z.bypassed,
                    "alarm": z.alarm,
                    "tamper": z.tamper,
                    "low_battery": z.low_battery,
                    "trouble": z.trouble,
                    "bypass_allowed": z.bypass_allowed,
                    "device_type": z.device_type,
                },
            )
        # Late-arriving zones
        for idx, z in p.zones.items():
            if idx in self._published_zones:
                continue
            if p.highest_zone_index is not None and idx > p.highest_zone_index:
                continue
            if z.installed is False or is_placeholder(z.name):
                continue
            await self._discover_zone(z)
            self._published_zones.add(idx)
            await self._pub(
                self._topic("zone", str(idx), "state"), "ON" if z.open else "OFF"
            )

    async def _on_push(self, panel: PanelState) -> None:
        try:
            await self._publish_state()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("failed to publish push update")

    def _last_poll_expiry(self) -> int:
        return int(max(300, 4 * self.opt.poll_interval))

    async def _publish_last_poll(self) -> None:
        """Publish the time of the last successful poll (retained; HA compares
        its age, so a stale retained value is detected rather than trusted)."""
        if self._last_poll is None:
            return
        await self._pub(self._topic("last_poll"),
                        self._last_poll.isoformat(timespec="seconds"), retain=True)

    async def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.opt.poll_interval)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self.client.async_ensure_authenticated()
                await self.client.async_connect_ws()
                await self.client.async_reconcile(self._device_id)
                await self._publish_state()
                self._last_poll = datetime.now(timezone.utc)
                await self._publish_last_poll()
            except AlulaCredentialsRejected:
                await self._credentials_rejected()
                await self._stop.wait()
                return
            except AlulaAuthError as err:
                _LOGGER.warning("Alula authentication failed (%s); will retry", err)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("poll failed")

    # ---- commands (app path; no interactiveEnabled gate) -----------------

    async def _subscribe_commands(self) -> None:
        assert self._device_id
        await self.mqtt.subscribe(self._topic("alarm", "set"), self._on_alarm_cmd)
        await self.mqtt.subscribe(self._topic("button", "bypass_open"), self._on_bypass_open)
        await self.mqtt.subscribe(self._topic("button", "unbypass_all"), self._on_unbypass_all)
        await self.mqtt.subscribe(self._topic("button", "prompt_confirm"), self._on_prompt_confirm)
        await self.mqtt.subscribe(self._topic("button", "prompt_cancel"), self._on_prompt_cancel)
        await self.mqtt.subscribe(self._topic("button", "arm_home_bypass"), self._on_arm_home_bypass)
        await self.mqtt.subscribe(self._topic("button", "arm_away_bypass"), self._on_arm_away_bypass)

    def _saved_code(self) -> str:
        c = (self.opt.code or "").strip()
        return c if _valid_pin(c) else ""

    def _arm_code(self) -> str:
        """Arming needs no code from the user: the saved code goes on the wire."""
        c = self._saved_code()
        if not c:
            raise UserError("Set the 4-digit panel code on the add-on Configuration "
                            "page to arm from Home Assistant.")
        return c

    def _record_bad_code(self) -> None:
        now = time.monotonic()
        self._bad_codes = [t for t in self._bad_codes if now - t < BAD_CODE_WINDOW]
        self._bad_codes.append(now)
        if len(self._bad_codes) >= BAD_CODE_LIMIT:
            self._disarm_locked_until = now + BAD_CODE_LOCKOUT
            self._bad_codes.clear()
            _LOGGER.warning("Too many wrong disarm codes; HA disarm locked for %ds",
                            int(BAD_CODE_LOCKOUT))

    def _check_disarm_code(self, entered: str) -> str:
        """Return the code to send for DISARM, or raise UserError. The entered code
        must match the saved add-on code (constant-time compare). With no saved
        code, a 4-digit entry is passed through and the panel checks it."""
        now = time.monotonic()
        if now < self._disarm_locked_until:
            raise UserError("Too many wrong codes. Disarming from Home Assistant is paused "
                            f"for {int(self._disarm_locked_until - now) + 1} s. Use the "
                            "keypad or the Alula app.")
        entered = (entered or "").strip()
        if not entered:
            raise UserError("Disarm needs the 4-digit panel code.")
        saved = self._saved_code()
        if saved:
            if not hmac.compare_digest(entered.encode(), saved.encode()):
                self._record_bad_code()
                raise UserError("Wrong code. The alarm was not disarmed.")
            self._bad_codes.clear()
            return saved
        if not _valid_pin(entered):
            self._record_bad_code()
            raise UserError("Wrong code. The alarm was not disarmed.")
        return entered

    @staticmethod
    def _pressed(payload: str) -> bool:
        return payload.strip().upper() in ("PRESS", "ON", "1")

    @staticmethod
    def _parse_alarm_payload(payload: str) -> tuple[str, str, bool, Optional[bool]]:
        """-> (command, pin, auto_bypass, instant_override).

        Accepts "ARM_HOME", "DISARM,1234", "ARM_HOME,BYPASS", "ARM_HOME,BYPASS_INSTANT",
        and the direct MQTT forms "ARM_HOME_BYPASS" / "ARM_AWAY_BYPASS".
        """
        raw = payload.strip()
        cmd, _, extra = raw.partition(",")
        cmd = cmd.strip().upper()
        bypass = False
        instant: Optional[bool] = None
        if cmd.endswith("_BYPASS"):
            cmd = cmd[: -len("_BYPASS")]
            bypass = True
        pin = ""
        extra_s = extra.strip()
        if extra_s.isdigit():
            pin = extra_s
        else:
            for tok in extra.replace("+", " ").replace("_", " ").replace(",", " ").split():
                t = tok.strip().upper()
                if t.isdigit():
                    pin = t
                elif t in CODE_TOKENS_BYPASS or t == "BYPASS":
                    bypass = True
                elif t in CODE_TOKENS_INSTANT or t == "INSTANT":
                    instant = True
            joined = extra_s.upper()
            if joined in CODE_TOKENS_BYPASS:
                bypass = True
            if joined in CODE_TOKENS_INSTANT:
                instant = True
            if not pin and extra_s and not (bypass or instant):
                pin = extra_s  # non-numeric code text: still checked (and rejected)
        return cmd, pin, bypass, instant

    async def _on_alarm_cmd(self, topic: str, payload: str) -> None:
        cmd, pin, bypass, instant = self._parse_alarm_payload(payload)
        if cmd == "DISARM":
            await self._disarm(pin, source="manual")
            return
        mode = {"ARM_HOME": "home", "ARM_AWAY": "away", "ARM_NIGHT": "night"}.get(cmd)
        if mode is None:
            _LOGGER.warning("Unknown alarm command %r", cmd[:32])
            return
        # HA's alarm panel gives no caller context over MQTT, so the plain entity
        # command is treated as MANUAL (prompt if zones are open). Automations use
        # code BYPASS or the "Arm ... (auto-bypass)" buttons.
        await self._arm(mode, auto_bypass=bypass, instant=instant,
                        source="automation" if bypass else "manual")

    def _is_away_armed(self) -> bool:
        p = self.client.panels[self._device_id]
        away = self._levels(p)["away"]
        return bool(away) and p.arming_level == away

    async def _on_ha_auto_disarm(self, event: dict) -> None:
        """Automation-only disarm (arrival), via the HA event `alula_auto_disarm`.

        Not an MQTT command: the broker can't restrict who publishes. The event must
        come from this HA instance with no user in its context, which is how events
        fired by an automation's `event:` action look. Events fired by a person or a
        phone (websocket/REST/mobile-app webhook) carry a user id and are ignored.
        The add-on then uses its saved code, and only while armed AWAY, so it can
        never drop a Stay/Night arm (someone is inside)."""
        ctx = event.get("context") or {}
        if event.get("origin") not in (None, "LOCAL") or ctx.get("user_id"):
            _LOGGER.warning("alula_auto_disarm ignored: not fired by an automation")
            return
        t = asyncio.create_task(self._auto_disarm(), name="alula_auto_disarm")
        self._bg_tasks.add(t)
        t.add_done_callback(self._bg_tasks.discard)

    async def _auto_disarm(self) -> None:
        if not self._is_away_armed():
            _LOGGER.info("auto_disarm ignored: the alarm is not armed away")
            return
        code = self._saved_code()
        if not code:
            await self._notify_error("Auto-disarm failed: no panel code is saved in the add-on.")
            return
        await self._disarm(code, source="automation", checked=True)

    async def _cancel_arms(self) -> None:
        current = asyncio.current_task()
        pending = [t for t in self._arm_tasks if t is not current and not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            _LOGGER.info("Disarm cancelled %d pending arm command(s)", len(pending))
            await asyncio.wait(pending, timeout=5)

    async def _disarm(self, entered: str, *, source: str, checked: bool = False) -> None:
        # DISARM has priority: it never waits behind an arm, bypass or prompt.
        try:
            code = entered if checked else self._check_disarm_code(entered)
        except UserError as err:
            _LOGGER.warning("Disarm refused: %s", err)
            await self._notify_error(f"Disarm refused: {err}")
            await self._refresh_publish()
            return
        await self._cancel_arms()
        await self._clear_prompt("disarm requested")
        async with self._disarm_lock:
            try:
                _LOGGER.info("%s disarm", source)
                resp = await self.client.async_set_arming_level(
                    self._device_id, LEVEL_DISARM, code, wait=True
                )
                reason = self.client.nak_reason(resp)
                if reason:
                    _LOGGER.error("Panel refused disarm: %s", reason)
                    await self._notify_error("Disarm failed: the panel refused the command.")
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Disarm failed: %s", err)
                await self._notify_error(self._safe_error("Disarm", err))
            finally:
                await self._refresh_publish()

    async def _refresh_publish(self) -> None:
        try:
            await self.client.request_panel_status(self._device_id)
            await asyncio.sleep(1)
            await self._publish_state()
        except Exception:  # noqa: BLE001
            pass

    async def _open_zones_now(self) -> tuple[list[Zone], list[Zone]]:
        """Fresh read of open, unbypassed zones -> (bypassable, not_bypassable)."""
        p = self.client.panels[self._device_id]
        top = int(p.highest_zone_index) if p.highest_zone_index is not None else 63
        try:
            await self.client.request_zone_statuses(self._device_id, 0, top)
            await asyncio.sleep(1.2)
        except AlulaError as err:
            _LOGGER.debug("zone status refresh failed: %s", err)
        p = self.client.panels[self._device_id]
        opened = [z for z in p.not_ready_zones if not is_placeholder(z.name)]
        ok = [z for z in opened if z.bypass_allowed is not False]
        bad = [z for z in opened if z.bypass_allowed is False]
        return ok, bad

    @staticmethod
    def _safe_error(what: str, err: Exception) -> str:
        if isinstance(err, UserError):
            return f"{what} failed: {err}"
        return (f"{what} failed: the panel or the Alula cloud did not accept the "
                "command. See the add-on log for details.")

    async def _arm(self, mode: str, *, auto_bypass: bool, instant: Optional[bool] = None,
                   source: str = "manual") -> None:
        task = asyncio.current_task()
        self._arm_tasks.add(task)
        try:
            await self._arm_locked(mode, auto_bypass=auto_bypass, instant=instant,
                                   source=source)
        except asyncio.CancelledError:
            _LOGGER.info("Arm %s cancelled (disarm requested)", mode)
        finally:
            self._arm_tasks.discard(task)

    async def _arm_locked(self, mode: str, *, auto_bypass: bool,
                          instant: Optional[bool], source: str) -> None:
        async with self._cmd_lock:
            p = self.client.panels[self._device_id]
            level, no_delay = self._resolve_mode(p, mode)
            if instant is not None:
                no_delay = no_delay or instant
            try:
                if not level:
                    raise UserError(f"Mode {MODE_LABEL.get(mode, mode)} is not available on this panel")
                code = self._arm_code()
                disarmed = p.arming_level in (None, LEVEL_DISARM)
                if disarmed:
                    ok, bad = await self._open_zones_now()
                    if (ok or bad) and not auto_bypass:
                        await self._start_prompt(mode, level, no_delay, ok, bad)
                        return
                    if bad:
                        names = ", ".join(self._zname(z) for z in bad)
                        raise UserError(f"Open zone(s) cannot be bypassed: {names}")
                    if ok:
                        _LOGGER.info("%s arm %s: bypassing %d open zone(s)", source, mode, len(ok))
                        await self._bypass_zones(ok)
                        await asyncio.sleep(1.0)
                await self._clear_prompt("armed")
                _LOGGER.info("%s arm %s (level %s, no entry delay=%s)", source, mode, level, no_delay)
                resp = await self.client.async_set_arming_level(
                    self._device_id, level, code, no_entry_delay=no_delay, wait=True
                )
                reason = self.client.nak_reason(resp)
                if reason:
                    _LOGGER.error("Panel refused arming: %s", reason)
                    raise UserError("the panel refused the command (is a zone still open?)")
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Arm %s failed: %s", mode, err)
                await self._notify_error(self._safe_error(f"Arm {MODE_LABEL.get(mode, mode)}", err))
            finally:
                await self._refresh_publish()

    @staticmethod
    def _zname(z: Zone) -> str:
        return z.name or f"Zone {z.index + 1}"

    # ---- arm prompt ----------------------------------------------------------

    def _action_ids(self, nonce: str) -> tuple[str, str]:
        return f"{ACTION_CONFIRM}_{nonce}", f"{ACTION_CANCEL}_{nonce}"

    async def _start_prompt(self, mode: str, level: int, no_delay: bool,
                            ok: list[Zone], bad: list[Zone]) -> None:
        if self._prompt_task:
            self._prompt_task.cancel()
        timeout = self.opt.prompt_timeout
        expires = datetime.now(timezone.utc) + timedelta(seconds=timeout)
        zones = [self._zname(z) for z in ok]
        blocked = [self._zname(z) for z in bad]
        nonce = secrets.token_hex(8)  # per-prompt: old/forged phone actions don't match
        self._prompt = {
            "mode": mode, "level": level, "no_delay": no_delay, "nonce": nonce,
            "zones": zones, "blocked": blocked,
            "expires_at": expires.isoformat(timespec="seconds"),
            "deadline": time.monotonic() + timeout,
        }
        label = MODE_LABEL.get(mode, mode)
        lines = "\n".join(f"- {n}" for n in zones)
        msg = f"Arm {label} was not sent because these zones are open:\n{lines}"
        if blocked:
            msg += "\n\nThese open zones cannot be bypassed:\n" + "\n".join(f"- {n}" for n in blocked)
        msg += (f"\n\nPress **Bypass open zones and arm** to bypass them and arm, or **Cancel arm**. "
                f"This request expires in {timeout} seconds.")
        self._prompt["message"] = msg
        _LOGGER.info("Manual arm %s held: %d open zone(s); waiting %ss for confirmation",
                     mode, len(zones) + len(blocked), timeout)
        await self._publish_prompt()
        await self.ha.call_service("persistent_notification", "create", {
            "notification_id": PROMPT_TAG, "title": "Alarm: open zones", "message": msg,
        })
        short = ", ".join(zones + blocked)
        confirm_id, cancel_id = self._action_ids(nonce)
        for svc in self.opt.notify_list:
            await self.ha.call_service("notify", svc, {
                "title": "Alarm: open zones",
                "message": f"Arm {label} held. Open: {short}. Bypass and arm?",
                "data": {
                    "tag": PROMPT_TAG,
                    "timeout": timeout,
                    "priority": "high",
                    "ttl": 0,
                    "actions": [
                        {"action": confirm_id, "title": "Bypass and arm"},
                        {"action": cancel_id, "title": "Cancel"},
                    ],
                },
            })
        self._prompt_task = asyncio.create_task(self._expire_prompt(timeout))

    async def _expire_prompt(self, timeout: float) -> None:
        try:
            await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            return
        if self._prompt and time.monotonic() >= self._prompt["deadline"] - 0.5:
            _LOGGER.info("Arm prompt expired; not armed")
            self._prompt_task = None
            await self._clear_prompt("expired")

    def _take_prompt(self) -> Optional[dict]:
        """Atomically claim the pending prompt (no await), so two confirmations
        arriving together cannot both act on it."""
        pr, self._prompt = self._prompt, None
        task, self._prompt_task = self._prompt_task, None
        if task and task is not asyncio.current_task():
            task.cancel()
        return pr

    async def _clear_prompt(self, reason: str) -> None:
        if self._take_prompt() is None:
            return
        await self._dismiss_prompt(reason)

    async def _dismiss_prompt(self, reason: str) -> None:
        _LOGGER.debug("arm prompt cleared (%s)", reason)
        await self._publish_prompt(last=reason)
        await self.ha.call_service("persistent_notification", "dismiss",
                                   {"notification_id": PROMPT_TAG})
        for svc in self.opt.notify_list:
            await self.ha.call_service("notify", svc, {
                "message": "clear_notification", "data": {"tag": PROMPT_TAG},
            })

    async def _publish_prompt(self, last: str = "") -> None:
        pr = self._prompt
        if pr is None:
            await self._pub(self._topic("prompt", "state"), "idle")
            await self._pub(self._topic("prompt", "attrs"),
                            {"mode": None, "open_zones": [], "last_result": last or None})
            return
        await self._pub(self._topic("prompt", "state"), "waiting")
        await self._pub(self._topic("prompt", "attrs"), {
            "mode": pr["mode"],
            "mode_label": MODE_LABEL.get(pr["mode"], pr["mode"]),
            "no_entry_delay": pr["no_delay"],
            "open_zones": pr["zones"],
            "cannot_bypass": pr["blocked"],
            "expires_at": pr["expires_at"],
            "message": pr["message"],
        })

    async def _confirm_prompt(self) -> None:
        pr = self._take_prompt()
        if pr is None or time.monotonic() > pr["deadline"]:
            _LOGGER.info("No pending arm request to confirm")
            if pr is not None:
                await self._dismiss_prompt("expired")
            return
        _LOGGER.info("Arm prompt confirmed: bypass open zones and arm %s", pr["mode"])
        await self._dismiss_prompt("confirmed")
        await self._arm(pr["mode"], auto_bypass=True, instant=pr["no_delay"] or None,
                        source="confirmed")

    async def _on_prompt_confirm(self, topic: str, payload: str) -> None:
        if self._pressed(payload):
            await self._confirm_prompt()

    async def _on_prompt_cancel(self, topic: str, payload: str) -> None:
        if self._pressed(payload):
            _LOGGER.info("Arm prompt cancelled")
            await self._clear_prompt("cancelled")

    async def _on_arm_home_bypass(self, topic: str, payload: str) -> None:
        if self._pressed(payload):
            await self._arm("home", auto_bypass=True, source="automation")

    async def _on_arm_away_bypass(self, topic: str, payload: str) -> None:
        if self._pressed(payload):
            await self._arm("away", auto_bypass=True, source="automation")

    async def _on_ha_action(self, event: dict) -> None:
        data = event.get("data") or {}
        action = str(data.get("action") or "")
        if not action.startswith("ALULA_"):
            return
        pr = self._prompt
        if pr is None:
            _LOGGER.info("Phone action ignored: no pending arm request")
            return
        # The tag (when the app reports it) must be ours, and the action must carry
        # this prompt's nonce, so a stale notification or a forged event can't arm.
        tag = data.get("tag")
        if tag is not None and tag != PROMPT_TAG:
            _LOGGER.warning("Phone action ignored: unexpected notification tag")
            return
        confirm_id, cancel_id = self._action_ids(pr["nonce"])
        if hmac.compare_digest(action, confirm_id):
            # Own task: the arm must be cancellable by DISARM without touching the
            # HA event listener that delivered this action.
            t = asyncio.create_task(self._confirm_prompt(), name="alula_prompt_confirm")
            self._arm_tasks.add(t)
            t.add_done_callback(self._arm_tasks.discard)
        elif hmac.compare_digest(action, cancel_id):
            _LOGGER.info("Arm prompt cancelled from phone")
            await self._clear_prompt("cancelled")
        else:
            _LOGGER.warning("Phone action ignored: it does not match the pending request")

    async def _notify_error(self, text: str) -> None:
        await self.ha.call_service("persistent_notification", "create", {
            "notification_id": "alula_arm_error", "title": "Alarm", "message": text,
        })

    async def _on_bypass_open(self, topic: str, payload: str) -> None:
        if not self._pressed(payload):
            return
        async with self._cmd_lock:
            try:
                await self._bypass_open()
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Bypass open zones failed: %s", err)

    async def _on_unbypass_all(self, topic: str, payload: str) -> None:
        if not self._pressed(payload):
            return
        async with self._cmd_lock:
            try:
                await self._unbypass_all()
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Unbypass all failed: %s", err)

    async def _bypass_open(self) -> None:
        p = self.client.panels[self._device_id]
        if p.arming_level not in (None, LEVEL_DISARM):
            raise AlulaError("Bypass only works while the system is disarmed")
        # Refresh so we know what's currently open.
        top = int(p.highest_zone_index) if p.highest_zone_index is not None else 63
        await self.client.request_zone_statuses(self._device_id, 0, top)
        await asyncio.sleep(1.2)
        p = self.client.panels[self._device_id]
        targets = [
            z for z in p.not_ready_zones
            if z.bypass_allowed is not False and not is_placeholder(z.name)
        ]
        if not targets:
            _LOGGER.info("No open bypassable zones")
            return
        await self._bypass_zones(targets)
        await self.client.request_zone_statuses(self._device_id, 0, top)
        await asyncio.sleep(1)
        await self._publish_state()

    async def _bypass_zones(self, targets: list[Zone]) -> None:
        num, _ = await self.client.async_get_bypass_user(self._device_id)
        user = int(num) if num is not None else 0
        for z in targets:
            # Exact app path: writeMfd zoneBypass {index, userNumber}
            resp = await self.client.async_set_zone_bypass(
                self._device_id, z.index, True, user_number=user, wait=True
            )
            reason = self.client.nak_reason(resp)
            if reason:
                raise AlulaError(f"Panel refused bypass of zone {z.index + 1}: {reason}")

    async def _unbypass_all(self) -> None:
        p = self.client.panels[self._device_id]
        if p.arming_level not in (None, LEVEL_DISARM):
            raise AlulaError("Unbypass only works while the system is disarmed")
        top = int(p.highest_zone_index) if p.highest_zone_index is not None else 63
        await self.client.request_zone_statuses(self._device_id, 0, top)
        await asyncio.sleep(1.2)
        p = self.client.panels[self._device_id]
        targets = [z for z in p._configured_zones() if z.bypassed]
        if not targets:
            _LOGGER.info("No bypassed zones")
            return
        num, _ = await self.client.async_get_bypass_user(self._device_id)
        user = int(num) if num is not None else 0
        for z in targets:
            resp = await self.client.async_set_zone_bypass(
                self._device_id, z.index, False, user_number=user, wait=True
            )
            reason = self.client.nak_reason(resp)
            if reason:
                raise AlulaError(f"Panel refused unbypass of zone {z.index + 1}: {reason}")
        await self.client.request_zone_statuses(self._device_id, 0, top)
        await asyncio.sleep(1)
        await self._publish_state()


async def main(options_path: str = "/data/options.json") -> int:
    options = Options.from_options_file(options_path)
    logging.basicConfig(
        level=getattr(logging, options.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    mqtt_opts = _mqtt_opts()
    _LOGGER.info(
        "Starting Alula bridge (MQTT %s:%s, poll %ss)",
        mqtt_opts["host"], mqtt_opts["port"], options.poll_interval,
    )
    bridge = Bridge(options, mqtt_opts)
    loop = asyncio.get_running_loop()
    for s in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(s, bridge.request_stop)
        except NotImplementedError:
            signal.signal(s, lambda *_: bridge.request_stop())
    try:
        await bridge.run()
    except MqttFatal as err:
        _LOGGER.error("Alula bridge exiting: %s", err)
        return 1
    except Exception:  # noqa: BLE001
        # Exit non-zero so the Supervisor watchdog restarts the add-on; the broker
        # has already published our last-will "offline".
        _LOGGER.exception("Alula bridge stopped on an unexpected error; exiting")
        return 1
    return 0
