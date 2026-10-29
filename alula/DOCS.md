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

## Options

See the Configuration tab. `code` is stored by the Supervisor as a password
field and is never logged. Leave it blank until you are ready; arm / disarm will
then fail with a clear error, while zone status still works.

Level overrides (`level_home` / `level_away` / `level_night`) map HA arm modes
to the panel's `armingLevelValue`. Leave them at `0` to use the panel's own
labels (`armingLevelName`). On a typical DSC via BAT-Connect that is Stay=2 and
Away=4, with no Night level.

## Support

Open an issue at the GitHub repository. Include the add-on log (set `log_level`
to `debug` first) with credentials redacted.
