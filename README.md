# Alula BAT-Connect Home Assistant add-on

Bridge an [Alula](https://www.alula.com/) BAT-Connect (DSC PowerSeries, Honeywell
Vista, etc.) alarm panel into Home Assistant over MQTT.

Uses the same `api.alula.net` cloud API and WebSocket command path as the Alula
Security mobile app (and dealer-branded white labels such as Cove). Arm / disarm
go through `changeArmingLevelUsingCode`; zone bypass goes through
`writeMfd zoneBypass` / `zoneUnbypass`. Live state arrives as WebSocket pushes.

> Independent interoperability project. Not affiliated with, endorsed by, or
> supported by Alula. A security system is safety-critical — put the account on
> **test mode** with your monitoring company before the first live arm / disarm /
> bypass test.

## Install from the Add-on Store

1. In Home Assistant go to **Settings → Add-ons → Add-on Store → ⋮ → Repositories**.
2. Add `https://github.com/jb9485/ha-alula-addon` and close.
3. Refresh the store, open **Alula BAT-Connect**, **Install**.
4. Open the **Configuration** tab, fill in your Alula username / password and
   (optionally) the 4-digit panel user code, then **Save**.
5. Start the add-on. Entities appear under the MQTT integration after a few
   seconds (the Mosquitto broker add-on must already be running; this add-on
   declares `mqtt:need` so the Supervisor injects the broker credentials).

## What you get

| Entity | Purpose |
|---|---|
| `alarm_control_panel.*` | Arm Home / Away (/ Night if the panel has it) and Disarm. The stored code is applied by the add-on so HA does not prompt. |
| `binary_sensor.*` (one per zone) | Open / closed for every named zone. |
| Status sensors | Ready to arm, Alarm, Tamper, **AC failure**, Low battery, Remote arming enabled (informational only — does **not** block commands). |
| `button.*_bypass_open_zones` | Bypass every currently open, bypassable zone (refused while armed). |
| `button.*_unbypass_all` | Clear every bypass (DSC also clears bypasses on the next disarm). |
| `sensor.*_bypassed_zones` | Human-readable list of currently bypassed zones. |

## Configuration

All credentials are end-user editable on the add-on's Configuration page — nothing
is hard-coded.

| Option | Meaning |
|---|---|
| `username` / `password` | Alula Security app login |
| `code` | 4-digit DSC user code (optional; required to arm/disarm) |
| `poll_interval` | Fallback poll seconds (default 30; live updates are push) |
| `level_home` / `level_away` / `level_night` | Override the panel's arming-level numbers (`0` = auto) |
| `device_name` | Friendly name for the HA device |
| `log_level` | `debug` / `info` / `warning` / `error` |

## Requirements

- Home Assistant OS / Supervised with the Mosquitto broker add-on (or any MQTT
  broker the Supervisor advertises).
- An Alula account that can already arm / disarm from the Alula Security app.

## Disclaimer

Use at your own risk on your own account and equipment. Keep the official app
installed. Do not rely solely on this add-on for life-safety.
