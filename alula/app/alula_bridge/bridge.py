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
from dataclasses import dataclass
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
from .mqtt_client import Mqtt
from .zones import device_class, is_placeholder, slugify

_LOGGER = logging.getLogger(__name__)
DISC = "homeassistant"
PREFIX = "alula"


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
        )


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
        try:
            await self._stop.wait()
        finally:
            poll.cancel()
            listen.cancel()
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
        if levels["night"]:
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

    def _require_code(self) -> str:
        c = (self.opt.code or "").strip()
        if not (c.isdigit() and len(c) == 4):
            raise AlulaError(
                "A 4-digit panel user code is required. Set it on the add-on Configuration page."
            )
        return c

    async def _on_alarm_cmd(self, topic: str, payload: str) -> None:
        async with self._cmd_lock:
            # Payload is "ARM_AWAY" or "ARM_AWAY,1234" (command_template adds ,code).
            raw = payload.strip()
            if "," in raw:
                cmd_part, code_part = raw.split(",", 1)
                cmd = cmd_part.strip().upper()
                code_override = code_part.strip()
            else:
                cmd = raw.upper()
                code_override = ""
            p = self.client.panels[self._device_id]
            levels = self._levels(p)
            try:
                if cmd == "DISARM":
                    level = LEVEL_DISARM
                elif cmd == "ARM_HOME":
                    level = levels["home"]
                elif cmd == "ARM_AWAY":
                    level = levels["away"]
                elif cmd == "ARM_NIGHT":
                    level = levels["night"]
                else:
                    _LOGGER.warning("Unknown alarm command %r", payload)
                    return
                if not level:
                    raise AlulaError(f"Mode {cmd} is not available on this panel")
                # Prefer a code HA collected via REMOTE_CODE; else the stored options code.
                pin = code_override if (code_override.isdigit() and len(code_override) == 4) else self._require_code()
                # Exact app path: changeArmingLevelUsingCode over wss://api.alula.net/ws/v1
                # (Helix family). The ConnectFlex partition command is tried as a fallback
                # by the client if the panel NAKs this one. No interactiveEnabled check.
                await self.client.async_set_arming_level(
                    self._device_id, level, pin, wait=True
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Alarm command %s failed: %s", cmd, err)
            finally:
                try:
                    await self.client.request_panel_status(self._device_id)
                    await asyncio.sleep(1)
                    await self._publish_state()
                except Exception:  # noqa: BLE001
                    pass

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
        await self.client.request_zone_statuses(self._device_id, 0, top)
        await asyncio.sleep(1)
        await self._publish_state()

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
