# Cove Connect / Alula Cloud API — Reverse‑Engineering Notes

Everything below was recovered statically from `Cove_Connect_4.3.121.333.xapk`
(package `com.covesmart.android`). Cove is a white‑label of the **Alula "Connect"**
platform (internal namespaces `com.securesmart.*` for the app and `com.m2mservices.*`
for the camera/billing SDK). The alarm‑control path is pure cloud HTTP + WebSocket and
touches none of the bundled native video libraries.

The goal is **interoperability for your own account**: log in with your own
credentials, read your panel's state, and arm/disarm with your own PIN — the same
operations the app performs. Nothing here bypasses a PIN or touches other users.

---

## 1. Backend

Single host for everything: **`https://api.alula.net`**

| Surface | Base | Purpose |
|---|---|---|
| OAuth | `/oauth/token` | Login + token refresh |
| REST | `/rest/v1/…` | Account, device list, notifications (JSON:API style) |
| RPC | `/rpc/v1/…` | Alarm ack, PIN validation, user ops (`{id, method?, params}`) |
| WebSocket | `/ws/v1` | **Live panel state + arm/disarm commands** |
| Cameras | `/m2m/CommonAdministrationService/api/v3/…`, `/video/v1/…` | Out of scope here |

`OPEN_API_DOMAIN_PLACE_HOLDER` / `PROALARM_DOMAIN_PLACE_HOLDER` appear unresolved in
the binary; Cove just uses `api.alula.net`.

---

## 2. Authentication — OAuth2 password grant

The mobile client credentials are **hard‑coded in the app and shared across every
install** (they are *not* a per‑user secret). You still need your own
username/password, so possessing them only lets you log into your own account:

```
client_id     = 4ce837c4-08e2-11e7-aa3b-605718912297
client_secret = Uzka3sgLNDTaH3cQ
```

### Login

```
POST https://api.alula.net/oauth/token
Content-Type: application/x-www-form-urlencoded
Accept: application/json
User-Agent: <anything reasonable>

grant_type=password
client_id=4ce837c4-08e2-11e7-aa3b-605718912297
client_secret=Uzka3sgLNDTaH3cQ
username=<your email>
password=<your password>
```

Response is standard OAuth2 JSON:

```json
{ "access_token": "…", "refresh_token": "…", "expires_in": 3600, "token_type": "bearer" }
```

On HTTP 429 the server returns a `Retry-After` header (seconds) — respect it; the app
surfaces it as a `timeout`. The panel also has a `keypad_lockout` state, so do **not**
retry logins or PIN attempts aggressively.

### Refresh

Identical endpoint, swap the grant:

```
grant_type=refresh_token
client_id=4ce837c4-08e2-11e7-aa3b-605718912297
client_secret=Uzka3sgLNDTaH3cQ
refresh_token=<refresh_token>
```

### Using the token

Every `api.alula.net` request **except** `/oauth/token` carries:

```
Authorization: Bearer <access_token>
```

A `401` means the access token expired → refresh once and retry. The app refreshes
proactively when the token is within its expiry window and reconnects the WebSocket
with the new token.

---

## 3. REST — bootstrap & device list

JSON:API envelope throughout: writes are `{"data":{"type":…,"id":…,"attributes":{…}}}`,
reads return `{"data":[ … ]}`.

| Method / Path | Use |
|---|---|
| `GET /rest/v1/self` | Your account; yields your API user id (needed by some commands) |
| `GET /rest/v1/devices` | All devices on the account |
| `GET /rest/v1/devices/{deviceId}` | One device |
| `GET /rest/v1/devices/eventlog/{deviceId}` | Event history |
| `GET /rest/v1/helix/users/` | Helix users / PIN holders |
| `POST /rpc/v1/helix/pin/valid` | Validate a PIN before using it |
| `POST /rpc/v1/alarm/cancel` | Acknowledge / cancel an in‑progress alarm |
| `POST /rpc/v1/alarm/confirm` | Confirm an alarm |

`alarm/cancel` and `alarm/confirm` body:

```json
{ "id": "<uuid>", "params": { "deviceId": "<id>", "partition": 0 } }
```

A device's top‑level `attributes` mirror the local `devices` table: at minimum
`is_panel`, `is_camera`, `online`, `arming_level` (string, default `"disarm"`),
`name`, `serial_number`. The panel is the device with `is_panel == true`.

`arming_level` here is a coarse string. The **full** panel state (zones, delays,
troubles) arrives over the WebSocket — see §5.

---

## 4. WebSocket — live state + commands

```
GET wss://api.alula.net/ws/v1
Authorization: Bearer <access_token>
User-Agent: <same as login>
```

(The app builds it as an `https://api.alula.net/ws/v1` OkHttp request and lets the
client upgrade to `wss`.) Each message is a JSON object serialized to text with a
trailing `\r\n`.

**Outbound envelope** (built by `AlulaRequest.sendSocketRequestFinal`):

```json
{ "channel": "<channel>", "id": "<uuid>", "<verb>": <payload> }
```

For Helix panel traffic the channel is `device.helix` and the verb is `send`. The
inner payload (built by `HelixRequest.sendSocketRequest`) is:

```json
{
  "deviceId": "<id>",
  "cmdrsp":   "<command>",
  "payload":  { … command-specific … },
  "requestId":"<uuid>"
}
```

So a full Helix command on the wire looks like:

```json
{ "channel": "device.helix", "id": "<uuid>",
  "send": { "deviceId": "<id>", "cmdrsp": "<command>",
            "payload": { … }, "requestId": "<uuid>" } }
```

Responses come back referencing the same `requestId`; live status pushes arrive on the
same `device.helix` channel without a request you sent.

### 4.0 Connect handshake + subscriptions — **required before any state arrives**

This is the part that's easy to miss: **opening the socket and waiting does nothing.**
The panel does not stream state to you until you subscribe, and the app waits for a
connect‑ready frame before subscribing.

1. **Connect‑ready.** Right after the upgrade, the server sends a frame the app treats as
   "ready" via `SharedWebSocket.isConnectSuccessResponse`: `channel == "*"` (and/or a
   `sessionId` field, and/or `message == "ready"`). Only then does the app call
   `setConnected()` → `subscribeToChannels()`.
2. **Subscribe.** For a Helix panel, `SharedWebSocket.subscribeToHelix(deviceId)` sends a
   subscribe frame on **both** channels:

   ```json
   { "channel": "device.status", "id": "<uuid>", "subscribe": { "deviceId": "<id>" } }
   { "channel": "device.helix",  "id": "<uuid>", "subscribe": { "deviceId": "<id>" } }
   ```

   (It also subscribes to `event` channels `device.chat.new` / `device.signal.new`, which
   are chat/signal noise irrelevant to alarm state.) The subscribe verb is `subscribe`
   with inner `{deviceId}`; unsubscribe is verb `unsubscribe` with `{consumerTag}`.
3. **Snapshot.** When the `device.helix` subscribe is confirmed (`processSubscription`),
   the app calls `getCurrentHelixData(deviceId)` to pull the current state. You can do the
   same explicitly with the MFD reads in §4.3 (`panelStatus`, `systemStatus`,
   `partitionStatus`). Without this you only see *changes*, never the current value.

After that, status pushes + your MFD responses both arrive on `device.helix` and carry
the panel fields. (The reference client in `covealula.py` implements exactly this:
`async_subscribe_device()` waits for ready then subscribes; `async_refresh_state()` adds
the snapshot reads.)

### 4.1 Arm / disarm — `changeArmingLevelUsingCode`

This single command does **both** arming and disarming; the level is selected by
`armingLevelValue`. The PIN is sent as an **array of single‑character strings**
(`"1234"` → `["1","2","3","4"]`).

```json
{ "channel": "device.helix", "id": "<uuid>",
  "send": {
    "deviceId": "<panel device id>",
    "cmdrsp": "changeArmingLevelUsingCode",
    "requestId": "<uuid>",
    "payload": {
      "armingLevelValue": 2,
      "armSilent": false,
      "noEntryDelay": false,
      "pin": ["1","2","3","4"]
    }
  } }
```

`forceArm` (arm past open zones, no PIN, identified by user number) also exists if you
need it: `{"name":"forceArm","value":{"armingLevelValue":N,"userNumber":U}}` via the
write‑MFD path.

### 4.2 Arming levels — **corrected mapping**

`ArmingLevel.getByteCode()` is what goes in `armingLevelValue`. Verified against the
decompiled enum (`com.securesmart.common.enums.helix.ArmingLevel`) **and** against
`requestDisarmWithPin()`, which sends `LEVEL_1.getByteCode()` for disarm:

| byteCode | enum (jsonString) | display strings | meaning |
|---|---|---|---|
| 0 | LEVEL_0 (`unknown`) | "unknown" | **placeholder / unknown state — never a command** |
| 1 | LEVEL_1 (`level1`) | "disarmed" / can_disarm | **Disarm** |
| 2 | LEVEL_2 (`level2`) | "armed" / can_arm | first armed level (typically **Stay / Home**) |
| 3 | LEVEL_3 (`level3`) | "armed" | second armed level (typically **Away**) |
| 4–8 | LEVEL_4…8 (`level4`…`level8`) | "armed" | extra / custom armed levels (e.g. Night) |
| 255 | ANY (`any`) | "any change" | wildcard (status filtering only) |

> ⚠️ **Disarm is byte 1, not 0.** Byte 0 (`LEVEL_0`/"unknown") is the null/unknown
> placeholder and is *not* a valid command target — sending 0 does nothing. An earlier
> draft of this doc had 0=disarm; that was wrong.

The byteCode equals the enum ordinal for 0–8. Which *armed* number means Stay vs Away vs
Night is **per‑panel**; the app reads the real labels with the `armingLevelName` MFD read
(`requestMfd`, payload `[{"name":"armingLevelName","indexFirst":0,"indexLast":7}]`). For a
single‑partition Cope/Cove panel the table above is the usual layout — confirm with the
`names` CLI command before trusting Stay/Away/Night.

There is also a newer partition API, `requestSetPartitionArmingLevelWithPin`, which uses
`cmdrsp:"partitionArmingLevelChange"` and a different payload
(`{authType:"pin", armingLevel:<jsonString e.g. "level1">, pin:[…], partitions:[…],
armSilent, noEntryDelay, silentProtest, forceArm}`). The `changeArmingLevelUsingCode`
path above is simpler and is what the app's main arm/disarm buttons use.

### 4.3 Reading panel config + zones over the socket

Config/status reads use `cmdrsp:"requestMfd"` (with `bypassCache:true`) and a payload
**array** of `{"name": <field>, "indexFirst": …, "indexLast": …}`. The cloud builders
(`HelixRequest.request*` → `sendReadMFDSocketRequestInArray`) confirm these exact field
names:

| What | MFD `name` | indexed? |
|---|---|---|
| Friendly system name ("My Home") | `panelName` | no |
| Firmware/gateway versions | `gatewayVersions` | no |
| How many zones/users/etc. exist | `highestUsedIndexes` | no |
| Panel status snapshot | `panelStatus` | no |
| System status snapshot | `systemStatus` | no |
| Partition status | `partitionStatus` | yes |
| Arming-level labels | `armingLevelName` | yes (0–7) |
| **Zone names** | `zoneName` | yes |
| **Zone live status** (open/bypass/alarm/tamper/batt) | `zoneStatus` | yes |
| Zone configuration (type/profile) | `zoneConfiguration` | yes |
| Zone options | `zoneOptions` | yes |

So a zone-status read is:

```json
{ "channel": "device.helix", "id": "<uuid>",
  "send": { "deviceId": "<id>", "cmdrsp": "requestMfd", "bypassCache": true,
            "payload": [ { "name": "zoneStatus", "indexFirst": 0, "indexLast": 63 } ],
            "requestId": "<uuid>" } }
```

Request a generous index range (e.g. 0–63); the panel only returns zones that exist, and
`highestUsedIndexes` gives the real ceiling (`highest_index_zone`). The per-zone fields
that come back mirror the app's `helix_zones` model — the useful ones for HA are `name`,
`open` (live open/closed), `bypassed`, `alarm`, `tamper`, `low_battery_trouble`,
`general_trouble`/`supervisory_trouble`, `signal_level`, `device_type`, and `ui_type`
(type hints used to pick door/window/motion). The reference client parses these into
`PanelState.zones[index]` (a `Zone`), tolerant of both array-of-objects and index-keyed
shapes — confirm the exact runtime shape with the `zones` CLI command and adjust
`_collect_zones`/`_ZONE_ALIASES` if a field name differs.

**Zone bypass** is a *write* MFD (`sendWriteMFDSocketRequestInArray`), name `zoneBypass`
or `zoneUnbypass`, carrying the target index and the acting user number:

```json
{ "name": "zoneBypass", "indexFirst": 5, "indexLast": 5,
  "items": [ { "index": 5, "value": { "userNumber": 0 } } ] }
```

---

## 5. Panel state model

The app caches panel state in a SQLite `helixes` table; those columns are the complete
set of fields the cloud reports and the most you could surface in Home Assistant. The
useful ones:

| Field | Meaning |
|---|---|
| `arming_level` | current level (int, maps as in §4.2) |
| `arming_level_names`, `arming_level_options` | per‑panel labels / allowed options |
| `ready_to_arm` | all zones closed / ready |
| `open_zones`, `bypassed_zones` | bitmaps of zone state |
| `alarm`, `alarm_type`, `alarm_zones` | alarm in progress + cause |
| `in_entry_delay`, `in_exit_delay`, `entry_delay` (30), `exit_delay` (60) | delay timers |
| `arm_fail_reason` | why an arm attempt failed |
| `chime_mode` | door chime on/off |
| `low_battery`, `ac_failure`, `missing_battery`, `low_battery_zones` | power/battery troubles |
| `tamper_zones`, `alarm_panel_cover_tamper`, `alarm_panel_wall_tamper` | tamper |
| `*_trouble` (many), `cs_comm_fail`, `server_comm_fail`, `receiver_jam` | comms / supervisory troubles |
| `partitions` (`[true]`), `max_partitions` (1) | partition map (Cove is single‑partition) |
| `panel_name`, `firmware_version`, `mac_address`, `serial_number` | identity |

A reasonable Home Assistant mapping:

* `alarm_control_panel` ← `arming_level` (+ `in_exit_delay`/`in_entry_delay`/`alarm` for
  the `arming`/`pending`/`triggered` transitional states).
* `binary_sensor` per trouble: `low_battery`, `ac_failure`, `cs_comm_fail`, tamper, etc.
* `binary_sensor` "ready to arm" ← `ready_to_arm`; "open zones" ← `open_zones != 0`.

HA `AlarmControlPanelState` ↔ Cove:

| HA state | Cove armingLevelValue |
|---|---|
| `disarmed` | 1 |
| `armed_home` | 2 |
| `armed_night` | 3 |
| `armed_away` | 4 |
| `arming` | command sent / `in_exit_delay` |
| `pending` | `in_entry_delay` |
| `triggered` | `alarm` set |

---

## 6. Local (no‑cloud) path — exists, harder

The app also speaks to the panel directly over **LAN and BLE**
(`com.securesmart.common.network.local`: `LanPanelScanner`, `LanSocketConnection`,
`BlePanelScanner`, `HelixConnection`, `HelixProtocol`, `HelixEncryption`). The
`assembleRequestSetPartitionArmingLevelWithPin` / `dcp/v2/helix` machinery lives there —
it's a custom **encrypted binary protocol**, not the cloud JSON. A fully local Home
Assistant integration is possible but is a much larger reversing effort. Start with the
cloud path; treat local as a later optimization if you want to drop the cloud
dependency or cut latency.

---

## 7. Things to keep in mind

* **This is a professionally‑monitored system.** Arming, disarming, and especially any
  alarm/cancel/confirm calls generate **real central‑station events**. Test carefully —
  put the system on test with your monitoring provider first, or expect real
  notifications/dispatch behavior.
* **Don't hammer auth or PIN.** 429 + `Retry-After` on login, and a panel‑side
  `keypad_lockout` exist. Back off.
* **Token storage:** persist `refresh_token` so a HAOS restart doesn't force a fresh
  password login every time.
* The embedded `client_id`/`client_secret` could change in a future app version; if
  login starts returning `invalid_client`, re‑extract them from the current APK or
  capture one login with mitmproxy.
* Firebase project `589563068342` ("covesmart") and the `AIza…` keys in the app are for
  push notifications / installations only — not needed for control.
