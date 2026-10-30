# Alula BAT-Connect

## How it works

1. The add-on logs into `https://api.alula.net` with your Alula username and
   password (OAuth2 password grant, the same client the Alula Security app uses).
2. It opens `wss://api.alula.net/ws/v1`, subscribes to `device.status` and
   `device.helix`, and pulls a snapshot (`panelStatus`, zone names / status /
   configuration, arming-level names).
3. State is published to MQTT with Home Assistant discovery. Live zone and arm
   changes arrive as WebSocket pushes; a short poll is only a backstop.
4. Arm / disarm commands from HA are translated into the app's
   `changeArmingLevelUsingCode` WebSocket command (PIN as a digit array). Bypass
   uses `writeMfd` `zoneBypass` / `zoneUnbypass`.

The account's `interactiveEnabled` flag is exposed as a diagnostic sensor only.
It does **not** gate commands: the Alula Security Android app can arm and disarm
accounts where that flag is false, and this add-on mirrors that path exactly.

## Bypass

- **Bypass open zones** bypasses every zone that is currently open and marked
  `bypassAllowed` by the panel. The system must be disarmed; otherwise the
  command is refused locally and nothing is sent.
- **Unbypass all** clears every currently bypassed zone (also only while
  disarmed). On DSC PowerSeries, disarming the panel also clears bypasses —
  that is standard DSC behaviour, independent of this add-on.
- A `sensor.*_bypassed_zones` lists the zones currently bypassed.

## Arming with open zones

Home Assistant's MQTT alarm panel does not tell the add-on *who* sent an arm
command, so the add-on uses two separate paths.

### Manual arming: you get a prompt

Arming from the alarm panel card, the more-info dialog, or the app uses the
normal alarm entity (`alarm_control_panel.alarm_arm_home` / `_away` / `_night`
with no special code). If any zone is open and not bypassed, the add-on does
**not** arm. Instead it:

- sets `sensor.*_arm_prompt` to `waiting` (attributes: `mode`, `open_zones`,
  `cannot_bypass`, `expires_at`, `message`),
- creates a Home Assistant notification listing the open zones,
- sends an actionable phone notification (**Bypass and arm** / **Cancel**) to
  every service in `notify_services`, if you set any. Each prompt's buttons carry
  a one-time id, so an old notification or a forged event cannot arm. Service
  names may only use lowercase letters, digits and `_`.

Answer with **Bypass open zones and arm** (`button.*_bypass_open_zones_and_arm`)
or **Cancel arm** (`button.*_cancel_arm`), or with the phone notification's
buttons. If nobody answers within `prompt_timeout` seconds (default 60), the
request is dropped and the system stays disarmed. Disarming also cancels it.

If every zone is closed, manual arming arms straight away, as before.

### Automations: open zones are bypassed automatically

Automations (and scripts) should use one of these. Each one bypasses every open,
bypassable zone and then arms, with no prompt.

```yaml
# Option 1: the auto-bypass buttons
- action: button.press
  target:
    entity_id: button.alula_alarm_arm_stay_auto_bypass   # or ..._arm_away_auto_bypass

# Option 2: the alarm entity with code BYPASS
- action: alarm_control_panel.alarm_arm_home     # or alarm_arm_away / alarm_arm_night
  target:
    entity_id: alarm_control_panel.alula_alarm
  data:
    code: BYPASS            # BYPASS_INSTANT = bypass and arm with no entry delay
```

Entity ids follow your device name; check **Settings → Devices → Alula Alarm**.
Raw MQTT users can publish `ARM_HOME_BYPASS` / `ARM_AWAY_BYPASS` (or
`ARM_HOME,BYPASS`) to `alula/<device id>/alarm/set`.

A zone the panel marks as not bypassable (for example a 24-hour or fire zone)
blocks arming on both paths, and the add-on raises a notification saying why.

## Instant Stay (no entry delay)

`changeArmingLevelUsingCode` has a `noEntryDelay` flag (the app's
"no entry delay" option), and the panel reports the result as
`armingWithNoDelay` in `panelStatus`. This is DSC "Stay Instant": entry/exit
door zones trip immediately instead of starting the entry delay.

- `stay_instant: true` makes **every** Arm Home / Stay instant.
- `night_instant: true` (default) adds **Arm Night** to the alarm panel when the
  panel has no Night level of its own, and uses it for Stay with no entry
  delay. Plain Arm Home keeps the entry delay. While armed that way the alarm
  entity shows `armed_night`.
- The alarm entity's `entry_delay_disabled` attribute shows what the panel
  actually reports.
- Automations can add instant to any arm with `code: INSTANT` or
  `code: BYPASS_INSTANT`.

## Codes: arm without a code, disarm with one

- **Arming** never asks for a code. The add-on sends the saved `code` to the panel.
- **Disarming** asks for the 4-digit code in Home Assistant (more-info dialog,
  alarm panel card, tile keypad). The add-on compares it with the saved `code`
  and sends nothing to the panel if it does not match. Five wrong codes within
  10 minutes pause disarming from Home Assistant for 2 minutes (the keypad and
  the Alula app still work). A rejected code raises an "Alarm" notification in
  Home Assistant; codes are never logged or published.
- With no saved `code`, arming is refused and a 4-digit disarm code is passed to
  the panel, which checks it.

### Disarming from an automation (arrival)

Don't put the code in an automation. Publish to the add-on's automation-only
disarm topic instead:

```yaml
- action: mqtt.publish
  data:
    topic: alula/<device id>/alarm/auto_disarm
    payload: DISARM
```

The add-on then disarms with its saved code, but **only while the alarm is armed
Away**; it ignores this topic in Stay / Night (someone is inside) or when
disarmed. Protect the topic with a broker ACL so that only Home Assistant
(`homeassistant`) and add-ons (`addons`) may publish to `alula/#`; see
"Securing the broker" below. The device id is in the topics shown on
**Settings → Devices → Alula Alarm → MQTT info**.

## Securing the broker

Anyone who can publish to `alula/#` can arm, bypass and (via `auto_disarm`, Away
only) disarm. With the Mosquitto add-on, every Home Assistant user can log in to
the broker by default, so add an ACL (Mosquitto **customize** → `/share/mosquitto`):

```text
# /share/mosquitto/acl.conf
acl_file /share/mosquitto/accesscontrollist
```

```text
# /share/mosquitto/accesscontrollist
user homeassistant
topic readwrite #

user addons
topic readwrite #

# every other broker user: deny the alarm, then allow what that client needs
user my_other_mqtt_user
topic deny alula/#
topic readwrite my_device_topics/#
```

Note that `addons` is shared by every add-on that uses the Supervisor's MQTT
service.

## Reliability

- The add-on sets an MQTT last-will, so if it dies the broker marks every
  entity unavailable instead of HA showing stale alarm state.
- It reconnects to the broker on its own (1 s backoff, doubling to 60 s) and
  ignores retained command messages.
- An unexpected internal error exits the add-on with a non-zero status; turn on
  **Watchdog** on the add-on's Info tab so the Supervisor restarts it.
- Commands run independently, and **Disarm always goes first**: it cancels an
  arm that is still waiting on the cloud and any open bypass prompt.
- Alula logins back off from 30 s, doubling to 30 min, and obey `Retry-After`.
  If Alula rejects the username or password, the add-on stops logging in
  (also across restarts), raises one notification, and waits until you change
  the username or password.

## Options

See the Configuration tab. `code` is stored by the Supervisor as a password
field and is never logged or published.

Level overrides (`level_home` / `level_away` / `level_night`) map HA arm modes
to the panel's `armingLevelValue`. Leave them at `0` to use the panel's own
labels (`armingLevelName`). On a typical DSC via BAT-Connect that is Stay=2 and
Away=4, with no Night level.

`homeassistant_api` is enabled only so the add-on can create and dismiss its own
notifications and hear the phone notification's button presses. Error
notifications are generic; details go to the add-on log.

## Support

Open an issue at the GitHub repository. Include the add-on log (set `log_level`
to `debug` first) with credentials redacted.
