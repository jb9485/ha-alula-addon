# Changelog

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
