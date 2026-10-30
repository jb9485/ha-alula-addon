"""Alula cloud <-> MQTT discovery bridge.

Uses the same api.alula.net OAuth + WebSocket command path as the Alula Security
mobile app (changeArmingLevelUsingCode for arm/disarm; writeMfd zoneBypass /
zoneUnbypass for bypass). The account's interactiveEnabled flag is reported as
an informational sensor only and NEVER blocks a command: the Android app can
arm/disarm this account regardless of that flag.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
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
    AlulaError,
    PanelState,
    Zone,
)
from .ha_core import HaCore
from .mqtt_client import Mqtt
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
            if svc:
                out.append(svc)
        return out


def _mqtt_opts() -> dict:
    # Supervisor injects these when services: mqtt:need is set in config.yaml
    return {
        "host": os.environ.get("MQTT_HOST") or os.environ.get("MQTTHOST") or "core-mosquitto",
        "port": int(os.environ.get("MQTT_PORT") or os.environ.get("MQTTPORT") or 1883),
        "username": os.environ.get("MQTT_USERNAME") or os.environ.get("MQTTUSER") or None,
        "password": os.environ.get("MQTT_PASSWORD") or os.environ.get("MQTTPASS") or None,
    }


class Bridge:
    def __init__(self, options: Options, mqtt: Mqtt) -> None:
        self.opt = options
        self.mqtt = mqtt
        self.client = AlulaClient(
            username=options.username,
            password=options.password,
            update_callback=self._on_push,
        )
        self._device_id: Optional[str] = None
        self._published_zones: set[int] = set()
        self._stop = asyncio.Event()
        self._cmd_lock = asyncio.Lock()
        self.ha = HaCore()
        # Pending manual-arm request waiting for "bypass and arm" / "cancel".
        self._prompt: Optional[dict] = None
        self._prompt_task: Optional[asyncio.Task] = None

    # ---- lifecycle -------------------------------------------------------

    async def run(self) -> None:
        await self.client.async_login(self.opt.username, self.opt.password)
        panels = await self.client.async_get_panels()
        if not panels:
            raise AlulaError("No alarm panel found on this Alula account")
        self._device_id = panels[0].device_id
        await self.client.async_connect_ws()
        await self.client.async_subscribe_device(self._device_id)
        await self.client.async_refresh_state(self._device_id, budget=45.0)
        await asyncio.sleep(1)
        await self._publish_discovery()
        await self._publish_state()
        await self._subscribe_commands()
        poll = asyncio.create_task(self._poll_loop())
        # Must listen for MQTT commands (arm/disarm/bypass). Without this task,
        # button presses and alarm panel commands are never received.
        listen = asyncio.create_task(self.mqtt.listen(), name="alula_mqtt_listen")
        ha_events = asyncio.create_task(
            self.ha.listen_events("mobile_app_notification_action", self._on_ha_action, self._stop),
            name="alula_ha_events",
        )
        if not self.ha.available:
            _LOGGER.warning("No Supervisor token: arm prompts will use the dashboard "
                            "sensor/buttons only (no HA notifications)")
        try:
            await self._stop.wait()
        finally:
            poll.cancel()
            listen.cancel()
            ha_events.cancel()
            if self._prompt_task:
                self._prompt_task.cancel()
            await self.ha.close()
            await self.client.async_close()

    def request_stop(self) -> None:
        self._stop.set()

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
        return "/".join((PREFIX, self._device_id, *parts))

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
        # Alarm control panel.
        # When a 4-digit code is stored in add-on options, tell HA that no code is
        # required at the UI — the add-on applies the stored code on the wire
        # (changeArmingLevelUsingCode). When no code is configured, ask HA for a
        # REMOTE_CODE so the more-info keypad appears and the entered digits are
        # passed through the MQTT command payload.
        has_code = bool(self.opt.code and self.opt.code.isdigit() and len(self.opt.code) == 4)
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
            "code_arm_required": not has_code,
            "code_disarm_required": not has_code,
            "code_trigger_required": False,
        }
        if has_code:
            # Explicitly omit the `code` key so HA does not treat this as a
            # code-gated panel. (Leaving code_arm_required false is not enough
            # on some HA versions if `code` is also present.)
            pass
        else:
            panel_cfg["code"] = "REMOTE_CODE"
        await self.mqtt.publish(
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
            await self.mqtt.publish(f"{DISC}/binary_sensor/alula_{self._device_id}_{key}/config", cfg)

        await self.mqtt.publish(
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
            await self.mqtt.publish(
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
            await self.mqtt.publish(
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
        await self.mqtt.publish(
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
        await self._publish_prompt()
        await self._discover_zones()
        await self.mqtt.publish(self._topic("availability"), "online", retain=True)

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
        await self.mqtt.publish(
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
        await self.mqtt.publish(self._topic("alarm", "state"), self._alarm_state(p))
        await self.mqtt.publish(
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
            await self.mqtt.publish(self._topic("status", key), "ON" if val else "OFF")

        await onoff("ready", p.ready)
        await onoff("alarm", p.alarm)
        await onoff("tamper", p.tamper)
        await onoff("ac_failure", p.ac_failure)
        await onoff("low_battery", p.low_battery)
        # Informational only — does NOT gate commands (Android app can arm despite False).
        await onoff("remote_arming", p.interactive_enabled)

        bypassed = [z for z in p._configured_zones() if z.bypassed]
        await self.mqtt.publish(
            self._topic("status", "bypassed"),
            ", ".join(z.name or f"zone {z.index + 1}" for z in bypassed) or "none",
        )
        await self.mqtt.publish(
            self._topic("status", "bypassed_attrs"),
            {"zones": [{"index": z.index, "number": z.index + 1, "name": z.name} for z in bypassed]},
        )
        for idx in list(self._published_zones):
            z = p.zones.get(idx)
            if z is None:
                continue
            await self.mqtt.publish(
                self._topic("zone", str(idx), "state"),
                "ON" if z.open else "OFF",
            )
            await self.mqtt.publish(
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
            await self.mqtt.publish(
                self._topic("zone", str(idx), "state"), "ON" if z.open else "OFF"
            )

    async def _on_push(self, panel: PanelState) -> None:
        try:
            await self._publish_state()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("failed to publish push update")

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
            except AlulaAuthError:
                _LOGGER.error("Alula authentication failed; will retry")
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

    def _require_code(self) -> str:
        c = (self.opt.code or "").strip()
        if not (c.isdigit() and len(c) == 4):
            raise AlulaError(
                "A 4-digit panel user code is required. Set it on the add-on Configuration page."
            )
        return c

    @staticmethod
    def _pressed(payload: str) -> bool:
        return payload.strip().upper() in ("PRESS", "ON", "1")

    @staticmethod
    def _parse_alarm_payload(payload: str) -> tuple[str, str, bool, Optional[bool]]:
        """-> (command, pin, auto_bypass, instant_override).

        Accepts "ARM_HOME", "ARM_HOME,1234", "ARM_HOME,BYPASS", "ARM_HOME,BYPASS_INSTANT",
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
        for tok in extra.replace("+", " ").replace("_", " ").replace(",", " ").split():
            t = tok.strip().upper()
            if t.isdigit():
                pin = t
            elif t in CODE_TOKENS_BYPASS or t == "BYPASS":
                bypass = True
            elif t in CODE_TOKENS_INSTANT or t == "INSTANT":
                instant = True
        # also accept the joined forms (AUTO_BYPASS, NO_DELAY) before token split
        joined = extra.strip().upper()
        if joined in CODE_TOKENS_BYPASS:
            bypass = True
        if joined in CODE_TOKENS_INSTANT:
            instant = True
        return cmd, pin, bypass, instant

    async def _on_alarm_cmd(self, topic: str, payload: str) -> None:
        cmd, pin, bypass, instant = self._parse_alarm_payload(payload)
        if cmd == "DISARM":
            await self._clear_prompt("disarm requested")
            async with self._cmd_lock:
                try:
                    await self.client.async_set_arming_level(
                        self._device_id, LEVEL_DISARM, self._pin(pin), wait=True
                    )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.error("Disarm failed: %s", err)
                finally:
                    await self._refresh_publish()
            return
        mode = {"ARM_HOME": "home", "ARM_AWAY": "away", "ARM_NIGHT": "night"}.get(cmd)
        if mode is None:
            _LOGGER.warning("Unknown alarm command %r", cmd)
            return
        # HA's alarm panel gives no caller context over MQTT, so the plain entity
        # command is treated as MANUAL (prompt if zones are open). Automations use
        # code BYPASS or the "Arm ... (auto-bypass)" buttons.
        await self._arm(mode, auto_bypass=bypass, instant=instant, pin=pin,
                        source="automation" if bypass else "manual")

    def _pin(self, override: str = "") -> str:
        if override.isdigit() and len(override) == 4:
            return override
        return self._require_code()

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

    async def _arm(self, mode: str, *, auto_bypass: bool, instant: Optional[bool] = None,
                   pin: str = "", source: str = "manual") -> None:
        async with self._cmd_lock:
            p = self.client.panels[self._device_id]
            level, no_delay = self._resolve_mode(p, mode)
            if instant is not None:
                no_delay = no_delay or instant
            try:
                if not level:
                    raise AlulaError(f"Mode {mode} is not available on this panel")
                code = self._pin(pin)
                disarmed = p.arming_level in (None, LEVEL_DISARM)
                if disarmed:
                    ok, bad = await self._open_zones_now()
                    if (ok or bad) and not auto_bypass:
                        await self._start_prompt(mode, level, no_delay, ok, bad, pin)
                        return
                    if bad:
                        names = ", ".join(self._zname(z) for z in bad)
                        raise AlulaError(f"Open zone(s) cannot be bypassed: {names}")
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
                    raise AlulaError(f"Panel refused arming: {reason}")
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Arm %s failed: %s", mode, err)
                await self._notify_error(f"Arm {MODE_LABEL.get(mode, mode)} failed: {err}")
            finally:
                await self._refresh_publish()

    @staticmethod
    def _zname(z: Zone) -> str:
        return z.name or f"Zone {z.index + 1}"

    # ---- arm prompt ----------------------------------------------------------

    async def _start_prompt(self, mode: str, level: int, no_delay: bool,
                            ok: list[Zone], bad: list[Zone], pin: str) -> None:
        if self._prompt_task:
            self._prompt_task.cancel()
        timeout = self.opt.prompt_timeout
        expires = datetime.now(timezone.utc) + timedelta(seconds=timeout)
        zones = [self._zname(z) for z in ok]
        blocked = [self._zname(z) for z in bad]
        self._prompt = {
            "mode": mode, "level": level, "no_delay": no_delay, "pin": pin,
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
                        {"action": ACTION_CONFIRM, "title": "Bypass and arm"},
                        {"action": ACTION_CANCEL, "title": "Cancel"},
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

    async def _clear_prompt(self, reason: str) -> None:
        if self._prompt is None:
            return
        self._prompt = None
        task, self._prompt_task = self._prompt_task, None
        if task and task is not asyncio.current_task():
            task.cancel()
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
            await self.mqtt.publish(self._topic("prompt", "state"), "idle")
            await self.mqtt.publish(self._topic("prompt", "attrs"),
                                    {"mode": None, "open_zones": [], "last_result": last or None})
            return
        await self.mqtt.publish(self._topic("prompt", "state"), "waiting")
        await self.mqtt.publish(self._topic("prompt", "attrs"), {
            "mode": pr["mode"],
            "mode_label": MODE_LABEL.get(pr["mode"], pr["mode"]),
            "no_entry_delay": pr["no_delay"],
            "open_zones": pr["zones"],
            "cannot_bypass": pr["blocked"],
            "expires_at": pr["expires_at"],
            "message": pr["message"],
        })

    async def _confirm_prompt(self) -> None:
        pr = self._prompt
        if pr is None or time.monotonic() > pr["deadline"]:
            _LOGGER.info("No pending arm request to confirm")
            await self._clear_prompt("expired")
            return
        _LOGGER.info("Arm prompt confirmed: bypass open zones and arm %s", pr["mode"])
        await self._arm(pr["mode"], auto_bypass=True, instant=pr["no_delay"] or None,
                        pin=pr["pin"], source="confirmed")
        await self._clear_prompt("confirmed")

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
        action = (event.get("data") or {}).get("action")
        if action == ACTION_CONFIRM:
            await self._confirm_prompt()
        elif action == ACTION_CANCEL:
            _LOGGER.info("Arm prompt cancelled from phone")
            await self._clear_prompt("cancelled")

    async def _notify_error(self, text: str) -> None:
        await self.ha.call_service("persistent_notification", "create", {
            "notification_id": "alula_arm_error", "title": "Alarm", "message": text,
        })

    async def _on_bypass_open(self, topic: str, payload: str) -> None:
        if payload.strip().upper() not in ("PRESS", "ON", "1"):
            return
        async with self._cmd_lock:
            try:
                await self._bypass_open()
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Bypass open zones failed: %s", err)

    async def _on_unbypass_all(self, topic: str, payload: str) -> None:
        if payload.strip().upper() not in ("PRESS", "ON", "1"):
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


async def main(options_path: str = "/data/options.json") -> None:
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
    stop = asyncio.get_event_loop().create_future()

    def _sig(*_):
        if not stop.done():
            stop.set_result(None)

    for s in (signal.SIGTERM, signal.SIGINT):
        try:
            asyncio.get_event_loop().add_signal_handler(s, _sig)
        except NotImplementedError:
            signal.signal(s, lambda *_: _sig())

    async with Mqtt(**mqtt_opts, client_id="alula_bridge") as mqtt:
        bridge = Bridge(options, mqtt)
        task = asyncio.create_task(bridge.run())
        await stop
        bridge.request_stop()
        try:
            await asyncio.wait_for(task, timeout=15)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
        try:
            await mqtt.publish(bridge._topic("availability"), "offline", retain=True)
        except Exception:  # noqa: BLE001
            pass
