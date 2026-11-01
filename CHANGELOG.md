# Changelog

## 0.3.2

MQTT reliability release.

- Startup no longer falls back to an anonymous broker connection when
  Supervisor hasn't published the MQTT service yet (e.g. Mosquitto still
  starting after a restore). It waits up to 5 minutes, then exits non-zero so
  the Supervisor watchdog restarts it.
- The bridge exits non-zero when the broker rejects its credentials
  (CONNACK 4/5, MQTT 5 reason 134/135) or after 5 minutes of failed
  reconnects, so the add-on restarts and re-reads credentials, instead of
  looping forever while the alarm can't be controlled.
- New **Last poll** diagnostic sensor (`sensor.alula_alarm_last_poll`, a
  timestamp with `expire_after`), updated after every successful Alula poll, so
  stalled polling or a dead link is visible even if a stale retained `online`
  is on the broker.
- Docs: turn on the Watchdog, and restart/alert when the alarm entity is
  unavailable or the last poll is stale.

## 0.3.1

- Automation disarm moved from the MQTT `alarm/auto_disarm` topic to the Home
  Assistant event `alula_auto_disarm`. The add-on accepts it only from an
  automation or script (no user in the event context, local origin) and only
  while armed Away. The MQTT topic was removed, because the Mosquitto add-on's
  ACLs cannot restrict which broker users publish to it.

## 0.3.0

Security and reliability release.

- **Disarm now needs the panel code; arming still does not.** The alarm entity
  uses `code: REMOTE_CODE` with `code_arm_required: false` and
  `code_disarm_required: true`, so HA's keypad appears on disarm only. The add-on
  checks the entered code against the saved code (constant-time) before sending
  anything; 5 wrong codes in 10 min pause HA disarm for 2 min.
- New automation-only disarm path: disarms with the saved code, only while
  armed Away. 0.3.0 used an MQTT topic; 0.3.1 replaced it with an HA event.
- MQTT last-will (`offline`), automatic broker reconnect with backoff, retained
  commands ignored, non-zero exit on an unexpected error so the Supervisor
  watchdog can restart the add-on.
- Commands run as separate tasks; DISARM cancels a pending arm or prompt and
  never waits behind one.
- Alula login backoff (30 s doubling to 30 min, honours `Retry-After`). A
  rejected username/password stops all login attempts, across restarts, until
  the options change, with one notification.
- Phone prompt actions carry a per-prompt one-time id and the tag is checked;
  two simultaneous confirmations can no longer both act.
- `notify_services` names are validated (`^[a-z0-9_]+$`).
- Error notifications no longer include raw cloud responses; token and REST
  errors carry only the HTTP status and OAuth error code.
- Removed the unused `ssl` folder mapping. Tightened dependency floors
  (`aiohttp>=3.12.14`, `aiomqtt>=2.3`).

## 0.2.0

- Manual arming with open zones no longer arms blindly. It holds the request,
  publishes `sensor.*_arm_prompt` (`waiting` with the open zones), creates a
  Home Assistant notification, and optionally sends an actionable phone
  notification. Answer with **Bypass open zones and arm** or **Cancel arm**.
  The request expires after `prompt_timeout` seconds (default 60).
- Automation path: **Arm stay (auto-bypass)** / **Arm away (auto-bypass)**
  buttons, or `code: BYPASS` on the alarm entity, bypass open zones and arm with
  no prompt.
- Instant Stay: new `stay_instant` option (no entry delay on every Stay arm) and
  `night_instant` option (Arm Night = Stay with no entry delay, on by default for
  panels without a Night level). Uses the panel's `noEntryDelay` flag; the
  panel's `armingWithNoDelay` status is shown as `entry_delay_disabled`.
- New options: `prompt_timeout`, `notify_services`. The add-on now requests
  `homeassistant_api` for its prompt notifications.

## 0.1.2

- Fix: actually run the MQTT listen loop so arm/disarm/bypass commands from
  Home Assistant are received (0.1.0/0.1.1 subscribed but never listened).

## 0.1.1

- MQTT alarm panel: when a panel code is configured, publish
  `code_arm_required` / `code_disarm_required` / `code_trigger_required` as
  false and omit the `code` key so Home Assistant no longer prompts for a code
  (the add-on applies the stored code). When no code is configured, publish
  `code: REMOTE_CODE` and accept `ACTION,1234` command payloads.
- Alarm panel entity id is now `alarm_control_panel.alula_alarm` (was the
  doubled `alula_alarm_alula_alarm`).

## 0.1.0

- Initial release: Alula cloud → MQTT discovery bridge.
- Alarm control panel (arm home / away / night, disarm) using the Alula Security
  app's `changeArmingLevelUsingCode` path.
- Per-zone binary sensors, status / trouble sensors (AC failure correctly classed
  as a problem), remote-arming informational sensor.
- Single **Bypass open zones** and **Unbypass all** buttons (no per-zone switches).
- End-user editable options (username, password, panel code, poll interval,
  arming-level overrides, device name, log level).
