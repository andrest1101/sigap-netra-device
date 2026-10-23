# Cloud Integration Plan — SIGAP-NETRA Device

> Detailed technical handoff for the device-side Python application.
> Last updated: **2026-10-22**
> Status: **planning / pre-implementation**
>
> Read `AGENTS.md` and `progress.md` first.

---

## 1. Objective

Connect the MaixCAM device to Firebase so the companion app can monitor it from
any network, while preserving the device’s offline core:

```text
camera → YOLO/OCR → LiDAR gate → audio + button
                                  │
                                  └── optional telemetry → Cloud Firestore
```

The device must still detect, speak, and respond to buttons with **zero internet
and zero Firebase configuration**. Cloud is best-effort telemetry only.

### In scope

- Firebase Auth REST login using a dedicated device account.
- Heartbeat to `devices/{deviceId}`.
- Upload money/text detections.
- Poll `commands` and handle remote volume/text/sync.
- Safe background worker that never blocks detection or audio.
- Config template; real config only on the device.

### Out of scope

- Flutter app changes.
- Thumbnail/media upload (opt-in, not implemented yet).
- Video/audio/GPS/location upload.
- Cloud Functions, FCM sending server, Blaze-only services.
- Schema/rules changes without explicit owner approval.
- Hardware changes (pinout, LiDAR, battery, audio path).

---

## 2. Current state

### Device repo

```text
C:\ngoding_skuy\sigap-netra
└── device\
    ├── main.py                # legacy, no cloud
    ├── main_en.py             # reference; do not edit unless asked
    ├── sigap_core.py          # pure logic
    ├── sigap_api_server.py    # local HTTP API, port 8080
    ├── app.yaml               # no cloud files yet
    ├── kata_utuh.py
    └── models\
```

Missing:

- `device/sigap_cloud.py`
- `device/config.example.py`
- `device/config.py`

### Flutter repo (read-only)

```text
C:\ngoding_skuy\sigap_netra_app
```

Relevant files:

- `docs/firestore_schema.md`
- `docs/device_protocol.md`
- `firebase/firestore.rules`
- `firebase/firestore.indexes.json`
- `device/sigap_cloud.py` — reference bridge
- `device/config.example.py` — reference config template

The Flutter app is still in simulation/stub mode. Device telemetry must be
verified via Firebase Console or REST first.

### Device facts

- MaixCAM IP via USB/NCM: `10.212.207.1`.
- SSH: `ssh root@10.212.207.1` (device-default credentials; never commit them).
- Active app path is unresolved:
  - documentation: `/maixapp/apps/aplikasi_utama`
  - MaixVision screenshot: `/root/apps`
- Verify before deploying:
  ```bash
  ps -ef | grep -E "main.py|maixpy" | grep -v grep
  find /root /maixapp -maxdepth 4 -name app.yaml 2>/dev/null
  ```

---

## 3. Target architecture

```text
main.py (detection/audio/button)
   │
   ├── offline path: always runs
   │
   └── sigap_cloud.py
         ├── bounded queue (hooks only enqueue)
         ├── upload worker thread
         │     ├── Firebase Auth REST
         │     ├── detection PATCH
         │     └── heartbeat PATCH
         └── command poller thread
               ├── runQuery commands
               ├── on_command callback
               └── status updates
```

Key invariants:

- Hooks (`catat_uang`, `catat_teks`) do **no network I/O**.
- If the queue is full, drop items; never block the detection loop.
- Missing config or no Wi-Fi disables cloud silently; device still works.
- No secrets in logs.
- Heartbeat and polling stop cleanly in `tutup()`.

---

## 4. File plan

| File | Action | Notes |
|---|---|---|
| `device/sigap_cloud.py` | create | Port reference bridge + fixes |
| `device/config.example.py` | create | Placeholders only |
| `device/config.py` | create on device only | Never commit; `chmod 600` |
| `device/main.py` | patch | Import, start, hooks, command handler, stop |
| `device/app.yaml` | patch | Add `sigap_cloud.py`, `config.example.py` |
| `progress.md` | update | After each finished task |

Do not modify:

- `main_en.py`
- `sigap_core.py` unless a pure helper is genuinely required
- `sigap_api_server.py` unless a command handler needs a safe existing API
- model files
- Flutter repo

---

## 5. Firebase setup

### 5.1 Auth accounts

1. **Owner app account** — already exists; UID is used in `members`.
   The actual UID is intentionally not written in this repo.
2. **Dedicated device account** — create in Firebase Console:
   - Authentication → Users → Add user.
   - Email/password, strong password.
   - Copy the UID (`<UID_DEVICE>`).
   - Do not reuse the owner account as the device identity.

Why separate accounts:

- `isDevice()` binds the device to `devices/{deviceId}.authUid`.
- If the owner and device share a UID, a compromised device compromises the
  owner account and the device can act as a member/owner.
- Separate accounts preserve least privilege.

### 5.2 Device document

Create through the **Firebase Console** (rules `create` cannot set `members`):

```text
devices/{deviceId}
{
  "deviceId": "<deviceId>",
  "authUid": "<UID_DEVICE>",
  "name": "<display name>",
  "model": "MaixCAM SG2002",
  "firmwareVersion": "<version>",
  "wifiSsid": "",
  "lastSeen": <timestamp now>,
  "bootCount": 0,
  "batteryPct": null,
  "settings": {
    "uploadThumbnails": false,
    "uploadText": false
  },
  "members": {
    "<UID_OWNER>": "owner"
  },
  "createdAt": <timestamp now>,
  "updatedAt": <timestamp now>
}
```

Notes:

- `settings.uploadThumbnails` and `settings.uploadText` must be booleans.
- `members` is required for app reads. The device `create` rule does not allow
  writing `members`; console bootstrap is the reliable path.
- Do not add `isOnline` or `status`; online is computed client-side.
- Do not add GPS/location/IP/MAC/serial/token fields.

### 5.3 Command-read blocker

Current rules:

```text
match /commands/{commandId} {
  allow read: if isMember(deviceId);
  ...
}
```

The device is not a member, so `runQuery` on `commands` will be denied.

Options:

1. **Prototype workaround:** add `<UID_DEVICE>` to `members`:

   ```text
   members: {
     "<UID_OWNER>": "owner",
     "<UID_DEVICE>": "device"
   }
   ```

   Trade-off: the device gets member read/delete privileges on detections and
   events. Acceptable only for early testing; document it.

2. **Production fix:** change rules in the Flutter repo:

   ```text
   allow read: if isMember(deviceId) || isDevice(deviceId);
   ```

   This requires explicit owner approval because it touches the Flutter repo.

Do not silently choose. Record the decision in `progress.md`.

### 5.4 Indexes and API key

- Deploy the `commands` composite index:
  `status ASC` + `createdAt ASC`.
- Use the Firebase **Web API key** for Identity Toolkit.
- If the key is restricted to Android/iOS apps, REST auth will fail. Allow
  Identity Toolkit and Firestore REST APIs.
- Ensure the device clock is correct; TLS and timestamps depend on it.

---

## 6. Data contract

Canonical detail: `docs/firestore_schema.md` and `docs/device_protocol.md` in the
Flutter repo.

### 6.1 Device document

```text
deviceId, authUid, name, model, firmwareVersion, wifiSsid,
lastSeen, bootCount, batteryPct, settings,
members, createdAt, updatedAt
```

Device may update only the allowed telemetry/settings fields. App membership is
owner-managed.

### 6.2 Detection

```text
devices/{deviceId}/detections/{detectionId}
{
  "type": "money" | "text",
  "amount": int | null,
  "currency": "IDR",
  "confidence": 0.0..1.0,
  "distanceCm": int | null,
  "ocrText": string <=500 | null,
  "thumbnailId": null,
  "validationStatus": "pending",
  "createdAt": <timestamp>
}
```

Rules:

- Device creates with `validationStatus: "pending"`.
- Device never writes `validatedBy` / `validatedAt`.
- App members may update only the three validation fields.
- `distanceCm` must be int or null; cast LiDAR float.
- `ocrText` for text only when upload consent is enabled.

### 6.3 Commands

```text
devices/{deviceId}/commands/{commandId}
{
  "type": "sync_now" | "speak_text" | "set_volume" | "restart" | "reprovision",
  "status": "pending" | "sent" | "acked" | "done" | "failed",
  "payload": { ... },
  "requestedBy": "<UID_APP_USER>",
  "createdAt": <timestamp>,
  "updatedAt": <timestamp>,
  "resultNote": string <=200 | absent
}
```

Allowed transitions:

```text
pending -> sent -> acked -> done | failed
```

Device may update only `status`, `updatedAt`, `resultNote`.

Payload constraints:

- `sync_now`: `{}`
- `speak_text`: `{ "text": string 1..200 }`
- `set_volume`: `{ "level": int 0..100 }`
- `restart`: `{}` (not implemented yet)
- `reprovision`: `{}` (no QR pairing mode in current firmware)

---

## 7. `sigap_cloud.py` design

### 7.1 Public interface

```python
# target interface; port/adapt the reference implementation
class SigapCloud:
    def __init__(self, config: dict, hooks: dict): ...
    def start(self): ...
    def stop(self): ...
    def catat_uang(self, daftar_nominal, conf=1.0, jarak_cm=None): ...
    def catat_teks(self, baris_list, conf=1.0, jarak_cm=None): ...

def muat_config():
    """Return CONFIG dict if valid; None if missing/placeholder."""
```

Hooks expected by the bridge:

```python
{
    "get_battery_pct": callable() -> int | None,
    "get_wifi_ssid": callable() -> str | None,
    "on_command": callable(type: str, payload: dict) -> str | None,
}
```

### 7.2 Config safety

`muat_config()` must:

- import `config` inside a try/except;
- return `None` if `config.py` is missing;
- return `None` if required values are missing or `"??"`;
- never print the password or full email.

Required keys:

```text
project_id, api_key, device_id, device_email, device_password
```

Optional display keys:

```text
name, model, firmware_version, upload_text, upload_thumbnails
```

### 7.3 Queue and worker

Do not perform HTTP in `catat_uang` / `catat_teks`.

```python
# hooks: enqueue only
def catat_uang(self, daftar, conf=1.0, jarak_cm=None):
    if not daftar:
        return
    self._masukkan({
        "type": "money",
        "amount": sum(int(n) for n in daftar),
        "currency": "IDR",
        "confidence": clamp(conf, 0.0, 1.0),
        "distanceCm": to_int_or_none(jarak_cm),
        "ocrText": None,
    })
```

Worker:

```text
loop:
  item = queue.get(timeout=1)
  if stop: break
  if throttle window not elapsed: drop
  headers = auth_header()
  if headers is None: drop (best effort)
  upload_detection(item, headers)
```

Queue rules:

- max size ~32;
- drop oldest/newest on overflow;
- no blocking `put`;
- log only a short counter/message.

### 7.4 Auth

REST endpoint:

```text
POST https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=<API_KEY>
{
  "email": "<device_email>",
  "password": "<device_password>",
  "returnSecureToken": true
}
```

Use `idToken` as `Authorization: Bearer <token>`. Refresh before expiry. Backoff
on failures; do not hammer the endpoint.

### 7.5 Heartbeat

- Interval: 60 seconds (pending owner decision; 30 s is the AGENTS value).
- `PATCH devices/{deviceId}` with `updateMask` for telemetry fields.
- Write `lastSeen` and `updatedAt` as Firestore `timestampValue`.
- Include `bootCount`, `batteryPct` (int or null), `wifiSsid` if available.
- Do not overwrite `authUid`, `members`, `settings` unintentionally.
- Cache `settings.uploadText` from the PATCH response when available.
- If the device document is missing, heartbeat cannot satisfy `members`
  bootstrap; create the document via console first.

Timestamp example:

```json
{"timestampValue": "2026-10-22T12:00:00Z"}
```

Do not use:

```json
{"stringValue": "2026-10-22T12:00:00+00:00"}
```

### 7.6 Detection upload

Create a document under:

```text
projects/{project}/databases/(default)/documents/devices/{deviceId}/detections/{detectionId}
```

Use `PATCH` with `updateMask` (or `createDocument`) and include all required
fields. `createdAt` must be a Firestore timestamp.

ID suggestion: `det_<epoch_ms>` is acceptable but must not contain `/`.

Throttle: minimum ~5 seconds between uploads; drop excess to protect quota and
avoid queue growth.

### 7.7 Command polling

Query the device’s `commands` subcollection:

```text
POST .../documents:runQuery
parent = projects/{project}/databases/(default)/documents/devices/{deviceId}
{
  "structuredQuery": {
    "from": [{"collectionId": "commands"}],
    "where": {
      "fieldFilter": {
        "field": {"fieldPath": "status"},
        "op": "IN",
        "value": {
          "arrayValue": {
            "values": [
              {"stringValue": "pending"},
              {"stringValue": "sent"}
            ]
          }
        }
      }
    },
    "orderBy": [{"field": {"fieldPath": "createdAt"}, "direction": "ASCENDING"}],
    "limit": 5
  }
}
```

Poll every ~30 seconds. Process each command:

```text
pending -> sent       (before execution)
sent -> acked         (handler accepted)
acked -> done         (success)
acked -> failed       (handler raised / unsupported)
```

`updatedAt` must be a timestamp. `resultNote` max 200 chars, no credentials.

### 7.8 Command handling

`main.py` must provide `on_command`:

- `set_volume` → `self.audio.setel_volume(level)`; clamp 0..100.
- `speak_text` → `self.audio.ucap(text[:200])`.
- `sync_now` → `self.minta_proses = True`.
- `restart` → raise until a safe restart path is confirmed.
- `reprovision` → raise until QR pairing mode exists.
- Unknown → raise.

If the bridge marks unsupported commands as `done`, that is a bug. They must end
as `failed` with a short note.

### 7.9 Stop

- Set a stop event.
- Wake the worker with a sentinel/timeout.
- Join threads with a short timeout.
- `stop()` must be idempotent; `tutup()` can be called twice.

---

## 8. `main.py` integration

### 8.1 Anchors (snapshot 2026-10-22)

| Area | Approx. line | Anchor |
|---|---:|---|
| import | after 44 | end of `try: import cv2 ...` |
| attribute | 2744–2746 | `self.server_api = None` / `self.minta_proses = False` |
| start | after 2758 | after `log("API", "Server REST ...")`, before `if not ENABLE_LAYAR` |
| money hook | after 2966 | after `self.audio.ucap_nominal(daftar, total)` |
| OCR hook | after 3030 | inside `if t:` after the `ucap` block |
| command handler | before 3461 | before `def tutup(self):` |
| stop | after 3469 | after `self.server_api.stop()` |
| `app.yaml` | 8–18 | `files:` list |

Line numbers will drift. Always search the anchor text.

### 8.2 Import

```python
# after the optional cv2 import block
try:
    import sigap_cloud
except Exception:
    sigap_cloud = None
```

### 8.3 Attribute

```python
# near self.server_api = None / self.minta_proses = False
self.sigap_cloud = None
```

### 8.4 Start cloud

Place in `pemanasan()` **before** the `if not ENABLE_LAYAR: return True` early
return:

```python
if sigap_cloud is not None:
    try:
        cfg = sigap_cloud.muat_config()
        if cfg:
            self.sigap_cloud = sigap_cloud.SigapCloud(
                cfg,
                {
                    "get_battery_pct": self._cloud_baterai,
                    "get_wifi_ssid": lambda: None,
                    "on_command": self._cloud_perintah,
                },
            )
            self.sigap_cloud.start()
            log("CLOUD", "jembatan cloud aktif")
        else:
            log("CLOUD", "config.py belum diisi; mode offline")
    except Exception as e:
        log("CLOUD", "gagal start: %s" % e)
        self.sigap_cloud = None
```

`get_wifi_ssid` can return `None` until a safe read-only implementation exists.
`self.baterai` is currently `None` in `main.py`; the battery hook returns `None`
until hardware monitoring is enabled.

### 8.5 Money hook

After `self.audio.ucap_nominal(daftar, total)`:

```python
if getattr(self, "sigap_cloud", None):
    self.sigap_cloud.catat_uang(
        daftar,
        conf=1.0,  # temporary; see confidence note
        jarak_cm=self._cloud_jarak(),
    )
```

### 8.6 OCR hook

Inside `if t:`, after the `if len(t) > BARIS_SEKALI` / `else` block:

```python
if getattr(self, "sigap_cloud", None):
    self.sigap_cloud.catat_teks(
        t,
        conf=1.0,  # temporary
        jarak_cm=self._cloud_jarak(),
    )
```

Use local `t`, not `self.baris_teks`; the latter can be stale from a previous
session.

### 8.7 Helper methods

Add before `tutup()`:

```python
def _cloud_jarak(self):
    try:
        if (hasattr(self, "lidar") and self.lidar.aktif
                and self.lidar.terakhir_baca is not None):
            return int(round(self.lidar.terakhir_baca))
    except Exception:
        pass
    return None

def _cloud_baterai(self):
    try:
        if (self.baterai and self.baterai.aktif
                and self.baterai.persen is not None):
            return int(self.baterai.persen)
    except Exception:
        pass
    return None

def _cloud_perintah(self, tipe, payload):
    if tipe == "set_volume":
        level = int(payload.get("level", 75))
        self.audio.setel_volume(level)
        return "volume %d%%" % level
    if tipe == "speak_text":
        teks = str(payload.get("text", ""))[:200]
        if teks:
            self.audio.ucap(teks)
            return "teks diucapkan"
        return "teks kosong"
    if tipe == "sync_now":
        self.minta_proses = True
        return "sinkronisasi dipicu"
    raise RuntimeError("perintah %s belum didukung" % tipe)
```

### 8.8 Stop cloud

In `tutup()`, after `self.server_api.stop()`:

```python
if getattr(self, "sigap_cloud", None):
    try:
        self.sigap_cloud.stop()
        self.sigap_cloud = None
    except Exception:
        pass
```

---

## 9. `app.yaml`

Add after `sigap_api_server.py`:

```yaml
  - sigap_cloud.py
  - config.example.py
```

Do **not** add `config.py`.

Why:

- `config.py` contains real credentials.
- It is not in the repo; listing a missing file can break installation.
- An app update may overwrite it.
- Keep it on the device only, `chmod 600`.

If the app folder is wiped during reinstall, recopy `config.py` afterward. A
future improvement can load the real config from a path outside the app folder
(e.g. `/root/sigap_cloud_config.py`).

---

## 10. `config.example.py`

```python
# Copy to config.py ON THE DEVICE ONLY. Never commit config.py.
CONFIG = {
    "project_id": "??",
    "api_key": "??",
    "device_id": "??",
    "device_email": "??",
    "device_password": "??",
    "name": "??",
    "model": "MaixCAM SG2002",
    "firmware_version": "??",
    "upload_text": False,
    "upload_thumbnails": False,
}
```

Rules:

- `device_id` must match the Firestore document ID.
- `device_email` / `device_password` are the dedicated device account.
- `api_key` is the Firebase Web API key (identifier, not a password).
- Do not log these values.
- `muat_config()` must reject `"??"` placeholders and disable cloud.

---

## 11. Deployment

### 11.1 Verify path and back up

```bash
ssh root@10.212.207.1
ps -ef | grep -E "main.py|maixpy" | grep -v grep
find /root /maixapp -maxdepth 4 -name app.yaml 2>/dev/null
```

Then, in the actual app directory:

```bash
cp main.py main.py.bak
```

### 11.2 Copy files

Using MaixVision Install App (recommended) or manual SCP:

```bash
scp device/main.py device/sigap_cloud.py device/config.example.py \
    device/app.yaml root@10.212.207.1:/<ACTIVE_APP_DIR>/
```

Create `config.py` on the device:

```bash
scp config.py root@10.212.207.1:/<ACTIVE_APP_DIR>/config.py
ssh root@10.212.207.1 "chmod 600 /<ACTIVE_APP_DIR>/config.py"
```

Clean stale bytecode:

```bash
ssh root@10.212.207.1 "rm -rf /<ACTIVE_APP_DIR>/__pycache__ /root/__pycache__"
```

Also check the README warning: a stale `/root/sigap_core.py` can shadow the app’s
module. Remove or update it if present.

### 11.3 Restart

Do not run two instances (camera/audio conflict). Stop the current app first,
then start via MaixVision or the launcher.

---

## 12. Test plan

### 12.1 Offline regression (required first)

- Rename/remove `config.py` temporarily.
- Start app.
- Verify:
  - camera preview/read works;
  - YOLO money detection works;
  - OCR works;
  - LiDAR gate works;
  - audio playback works;
  - button single/double/long press works;
  - no cloud exception crashes the app.
- Expected log: cloud disabled/offline, not a traceback.

### 12.2 Auth and heartbeat

- Fill `config.py` with the device account.
- Start app.
- Verify logs: auth success, no token/password logged.
- In Firestore Console, `devices/{deviceId}.lastSeen` updates about every 60 s
  and is a **Timestamp**, not a string.
- `bootCount` increments on boot.

### 12.3 Detection upload

- Money detection:
  - `type: money`
  - `amount` correct
  - `currency: IDR`
  - `confidence` number 0..1
  - `distanceCm` int or null
  - `ocrText: null`
  - `validationStatus: pending`
  - `createdAt` Timestamp
- Text detection with `settings.uploadText: false`:
  - no OCR text leaves the device.
- Text detection with `settings.uploadText: true`:
  - `ocrText` <= 500 chars; no validation fields.
- Confirm no image/audio/location fields exist.

### 12.4 Commands

Create a command through the app or Firestore Console:

```text
type: set_volume
status: pending
payload: { level: 40 }
```

Verify:

- device logs command received;
- actual ALSA volume changes;
- status transitions `pending → sent → acked → done`;
- `updatedAt` is a Timestamp;
- `resultNote` is short and contains no credentials.

Repeat for `speak_text` and `sync_now`.

For `restart`/`reprovision`, verify they end as `failed` until implemented, not
fake `done`.

### 12.5 Offline again

- Disable Wi-Fi.
- Verify detection/audio/buttons still work.
- Verify cloud failures are quiet and do not block the main loop.

---

## 13. Privacy and security checklist

- [ ] No password/token/email in logs.
- [ ] `config.py` not in git, not in `app.yaml`.
- [ ] `config.py` chmod 600.
- [ ] No video/audio/GPS/full-image upload.
- [ ] OCR text only with opt-in; max 500 chars.
- [ ] No thumbnail implementation in this phase.
- [ ] Device only creates `validationStatus: pending`.
- [ ] No Cloud Functions/Blaze-only services.
- [ ] API key restrictions reviewed.
- [ ] Flutter repo untouched.

---

## 14. Open decisions

Record the final answer in `progress.md`.

1. **Timestamp:** Opsi B `timestampValue` (current protocol decision) vs Opsi A
   server transform via `documents:commit`.
2. **Heartbeat:** 60 s (recommended for quota) vs 30 s (AGENTS value).
3. **`uploadText`:** skip text detections when false, or upload `type: text` with
   null text. Recommended: skip.
4. **Confidence:** instrument `analisis_uang` to return real confidence, or use a
   documented placeholder.
5. **Command read:** temporary `members` workaround vs rules change in the
   Flutter repo.
6. **`restart` / `reprovision`:** leave unsupported until hardware/QR pairing is
   confirmed.
7. **Active app path:** `/root/apps` vs `/maixapp/apps`; verify via SSH.
8. **Config location:** app dir vs external `/root` path.

---

## 15. Acceptance criteria

- [ ] Offline detection/audio/buttons unchanged.
- [ ] Missing config never crashes the app.
- [ ] Auth works with a dedicated device account.
- [ ] Heartbeat updates a Firestore Timestamp.
- [ ] Money/text detections match schema and rules.
- [ ] `settings.uploadText` is honored.
- [ ] `distanceCm` is int or null.
- [ ] `set_volume` changes the actual volume.
- [ ] Unsupported commands fail honestly.
- [ ] No secrets in repo/logs.
- [ ] No Flutter repo changes.
- [ ] `progress.md` updated; no commit.

---

## 16. References

Read-only Flutter repo:

- `C:\ngoding_skuy\sigap_netra_app\AGENTS.md`
- `C:\ngoding_skuy\sigap_netra_app\PRD.md`
- `C:\ngoding_skuy\sigap_netra_app\docs\firestore_schema.md`
- `C:\ngoding_skuy\sigap_netra_app\docs\device_protocol.md`
- `C:\ngoding_skuy\sigap_netra_app\firebase\firestore.rules`
- `C:\ngoding_skuy\sigap_netra_app\firebase\firestore.indexes.json`
- `C:\ngoding_skuy\sigap_netra_app\device\sigap_cloud.py`
- `C:\ngoding_skuy\sigap_netra_app\device\config.example.py`

Device repo:

- `C:\ngoding_skuy\sigap-netra\device\main.py`
- `C:\ngoding_skuy\sigap-netra\device\sigap_api_server.py`
- `C:\ngoding_skuy\sigap-netra\device\sigap_core.py`
- `C:\ngoding_skuy\sigap-netra\README.id.md`
- `C:\ngoding_skuy\sigap-netra\docs\pinout.md`
