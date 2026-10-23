# Progress — SIGAP-NETRA Device

> Living status file. Update after every finished task.
> Last updated: **2026-10-22**
> Current phase: **Cloud bridge implemented and deployed; offline test passed;
> awaiting device Wi-Fi + device Auth password for the first cloud heartbeat**

See `AGENTS.md` for stable rules and `docs/cloud_integration.md` for the detailed
technical plan.

---

## 1. Current status

| Item | Status | Notes |
|---|---|---|
| Device reachable via USB/NCM | DONE | `10.212.207.1`; SSH + SCP verified |
| SSH access verified | DONE | Key `~/.ssh/sigap_maixcam` |
| Active app path verified | DONE | `/maixapp/apps/aplikasi_utama` |
| Device version used as canonical | DONE | Device `main.py`/`sigap_core.py`/`sigap_api_server.py`/`kata_utuh.py`/`app.yaml` synced into repo |
| `device/sigap_cloud.py` | DONE | 900+ lines; real queue + 2 workers; compiles |
| `device/main.py` cloud integration | DONE | Import, cloud start, money/OCR hooks, command handler, idempotent stop |
| `device/config.example.py` | DONE | Placeholder template; no secrets |
| `device/config.py` on device | **PARTIAL** | Known values written; password still `FILL_ME_DEVICE_PASSWORD` |
| `device/app.yaml` cloud files | DONE | `sigap_cloud.py` + `config.example.py`; **not** `config.py` |
| Dedicated device Auth account | DONE | Created in Firebase Console; password only on device |
| Firestore device document | DONE | `devices/sigap-maixcam-01`; `authUid`, `members`, `settings` set |
| Command-read prototype access | DONE | Device UID is in `members` with role `"device"` (prototype; see §6) |
| Firestore rules/indexes | DONE | Rules compiled/released; all 5 indexes deployed |
| Offline regression test | DONE | App boots, camera + touchscreen + API 8080 OK; cloud stays offline |
| Device Wi-Fi/internet | **BLOCKED** | Saved profile is scanning; `wlan0` has `NO-CARRIER`; hotspot/router must be turned on |
| Device clock | **TEMPORARY** | Manually set to 2026-10-22 11:15 UTC; resets on reboot; NTP guard exists |
| Heartbeat test | **TODO** | Needs Wi-Fi + Auth password |
| Detection upload test | **TODO** | Needs Wi-Fi + Auth password; physical money/text test |
| `set_volume` command test | **TODO** | Needs cloud test + headset connected |
| Flutter app Firestore read | **NOT IN SCOPE HERE** | User will run `flutterfire configure`; Flutter repo untouched |

---

## 2. What changed in this repo

### Device source synced to the device version

The device had the newer Indonesian app. The following were copied from
`/maixapp/apps/aplikasi_utama` into `device/`:

- `main.py` (~148 KB base, then cloud patch)
- `sigap_core.py` (147,779 bytes)
- `sigap_api_server.py` (30,622 bytes)
- `kata_utuh.py` (15,789 bytes)
- `app.yaml` (PKM Team; delta int8 + charlie int8 models)

Also added the model files referenced by the device `app.yaml`:

- `device/models/yolo11n_rupiah_charlie_int8.mud`
- `device/models/yolo11n_rupiah_charlie_int8.cvimodel`

`main_en.py` was not edited.

### Cloud bridge (`device/sigap_cloud.py`)

New module. It is telemetry-only and never blocks detection/audio:

- `muat_config()` loads `config.py` by explicit path from the app directory, so
  a stale `/root/config.py` cannot shadow it. Missing/placeholder config returns
  `None`; the device stays offline.
- Hooks (`catat_uang`, `catat_teks`) only enqueue. A bounded queue (32 items) plus
  two daemon workers perform auth, heartbeat, upload, and command polling.
- Firebase Auth REST Email/Password; token cached and refreshed; auth backoff.
- Heartbeat every **60 s**: `lastSeen`, `updatedAt`, `bootCount`, `batteryPct`,
  `model`, `firmwareVersion`; all timestamps are Firestore `timestampValue`.
- Detection upload with a **5 s** minimum interval; excess detections are dropped
  with a short log. `createdAt` is a `timestampValue`; `validationStatus` is
  always `"pending"`; `distanceCm` is int or null; `confidence` is 0..1.
- OCR upload is skipped unless the cached `settings.uploadText` is true. The
  initial value comes from `config.py`; the heartbeat response refreshes it.
- Command polling every **30 s** via `runQuery` on the device document. Status
  transitions: `pending -> sent -> acked -> done|failed`. The query includes
  `acked` so a lost finalization response can be retried without re-executing.
- `sync_now` sets `minta_proses` and forces an immediate heartbeat.
- `restart` and `reprovision` fail with a short `resultNote`; no fake success.
- `bootCount` increments once per device boot using
  `/proc/sys/kernel/random/boot_id`, persisted in
  `/root/.sigap_cloud_state.json` (fallback: app dir `.cloud_state.json`).
- Clock guard: if the year is before 2024, the worker tries
  `ntpd -q -g -p pool.ntp.org` with a timeout before auth. If the clock is still
  wrong, cloud waits; the app keeps working offline.
- `stop()` is idempotent; `tutup()` can be called twice.

### `main.py` integration

- `import sigap_cloud` after the `cv2` import block.
- `self.sigap_cloud = None` attribute.
- Cloud starts in `pemanasan()` **before** the `ENABLE_LAYAR` early return, so it
  also runs with the display disabled.
- `analisis_uang()` now returns 4 values; `conf` is the average real confidence
  of the selected notes.
- Money hook after `ucap_nominal`; OCR hook uses the local `t` (not stale
  `self.baris_teks`).
- Command handlers: `set_volume`, `speak_text`, `sync_now`; unsupported
  `restart`/`reprovision` raise so the bridge marks them failed.
- Cloud stop in `tutup()` after the local API server stop.

### Manifest, template, gitignore

- `app.yaml` lists `sigap_cloud.py` and `config.example.py`; `config.py` is not
  listed.
- `config.example.py` has only `??` placeholders; no real credentials.
- `.gitignore` ignores `device/config.py`, `device/.cloud_state.json`, and
  `device/cloud_state.json`.

---

## 3. Device deployment

- App dir: `/maixapp/apps/aplikasi_utama`
- Backups of the pre-deploy app files and the stale `/root` modules:
  `/root/sigap_backup_20261022/`
- Deployed `main.py`, `sigap_cloud.py`, `config.example.py`, and `app.yaml` match
  the repo (MD5 verified).
- `config.py` exists only on the device, mode `600`; it contains the known
  project/device values and the placeholder password. It was not committed.
- Stale modules removed from `/root` after backup:
  - `/root/sigap_core.py` (147,176 bytes; older)
  - `/root/sigap_api_server.py` (12,167 bytes; much older)
  - `/root/kata_utuh.py` (15,789 bytes)
  These were shadowing the app dir because `main.py` puts `/root` before
  `APP_DIR` on `sys.path`.
- `__pycache__` in `/root` and the app dir was removed.
- App started with:
  ```bash
  cd /maixapp/apps/aplikasi_utama
  nohup python3 main.py > /tmp/sigap_app2.log 2>&1 &
  ```
  The app is running at the time of this record.

---

## 4. Offline verification (2026-10-22)

| Check | Result | Evidence |
|---|---|---|
| App boots without config password | PASS | `[CLOUD CONFIG] config.py belum valid: device_password` |
| Cloud stays offline | PASS | `[CLOUD] config.py belum diisi; mode offline` |
| No traceback | PASS | No `galat.log`; `/tmp/sigap_app2.log` clean |
| Camera | PASS | `[KAMERA] 1280x720 RGB888 berhasil dibuka` |
| Touchscreen | PASS | `[TOUCHSCREEN] Touchscreen AKTIF` |
| Local API | PASS | `GET http://10.212.207.1:8080/health` → HTTP 200 |
| Display warmup | PASS | `[LAYAR] pemanasan berhasil` |
| Physical button/YOLO/OCR/LiDAR/audio | **NOT TESTED REMOTELY** | User must press the button and present money/text |

Health response observed:

```json
{"id": "SGP-NETRA-0007", "fw": "v1.2.0-pkm", "battery": 85,
 "pending_rows": 319, "token": "...", "waktu": "2026-10-22 18:21:11",
 "ip": "127.0.1.1", "port": 8080}
```

The `battery: 85` is a fallback in `sigap_api_server.py`, not a real reading.
The app currently sets `self.baterai = None`; the cloud heartbeat sends
`batteryPct: null` rather than pretending 85 is real.

---

## 5. Firebase state (no secrets stored here)

- Project: `sigap-netra`
- Device document: `devices/sigap-maixcam-01`
- Device Auth account: created; its UID is recorded in the device document
  `authUid`. The password is only in the device `config.py`.
- Owner app account: exists; do not reuse it as the device identity.
- `members`: owner UID (`owner`) and device UID (`device`, prototype) are set.
- `settings.uploadThumbnails = false`, `settings.uploadText = false`.
- Rules: compiled and released.
- Indexes: all 5 deployed, including `commands` (`status ASC`, `createdAt ASC`).
- The device `lastSeen` is still stale; the app will show offline until the
  first successful heartbeat.
- The command-read prototype gives the device member read access. The proper
  production rule is still a Flutter-repo change requiring owner approval:
  `allow read: if isMember(deviceId) || isDevice(deviceId);`

---

## 6. Manual steps remaining (outside the assistant’s capability)

Do these in order.

### 6.1 Fill the device Auth password

On the device, edit:

```text
/maixapp/apps/aplikasi_utama/config.py
```

Replace:

```python
"device_password": "FILL_ME_DEVICE_PASSWORD",
```

with the real password of the dedicated device Firebase Auth account. Keep the
file mode `600`. Do not paste the password into chat, the repo, or Firestore.

### 6.2 Turn on the saved Wi-Fi hotspot/router

The device already has a saved Wi-Fi profile and is scanning, but `wlan0` has
`NO-CARRIER`. Turn on that hotspot/router; the device should connect
automatically and `udhcpc` should get an IP and default route.

Verify on the device:

```bash
ip addr show wlan0
ip route
ping -c 3 8.8.8.8
```

If the clock is still 1970 after connecting, run:

```bash
ntpd -q -g -p pool.ntp.org
date -u
```

The bridge also tries this automatically before auth.

### 6.3 Restart the app

The running app still has the placeholder password loaded (and the previous
`sigap_cloud.py` in memory); restart it after filling the password so the new
config and the latest bridge code are loaded:

```bash
kill -TERM <pid>   # or use the device UI / MaixVision stop
cd /maixapp/apps/aplikasi_utama
nohup python3 main.py > /tmp/sigap_app.log 2>&1 &
```

Watch for:

```text
[CLOUD CONFIG] config.py valid untuk device sigap-maixcam-01
[CLOUD AUTH] login OK
[CLOUD HEARTBEAT] OK
```

### 6.4 Verify cloud telemetry

In the Firebase Console (or REST), check:

- `devices/sigap-maixcam-01.lastSeen` is a **timestamp**, updated every ~60 s.
- `bootCount` incremented; `model` and `firmwareVersion` are present.
- `batteryPct` is `null` until the battery monitor is re-enabled.
- Press the physical button with money/text in front of the camera:
  - money detections: `type: "money"`, `amount`, real `confidence`,
    `distanceCm` int or null, `validationStatus: "pending"`, timestamp
    `createdAt`;
  - text detections: **not uploaded** while `settings.uploadText` is false.
- Create a `commands` document from the Flutter app (or a REST/Admin call that
  writes a **server timestamp** to `createdAt`; the Firestore Console’s client
  timestamp may be rejected by `createdAt == request.time`):
  - `set_volume` with `payload.level`; verify ALSA volume and status `done`;
  - `speak_text`; status `done` only if audio was actually queued; otherwise
    `failed` with a note;
  - `sync_now`; forces an immediate heartbeat;
  - `restart`/`reprovision`; should end `failed` with a short note.

### 6.5 Flutter app

In `C:\ngoding_skuy\sigap_netra_app` (read-only from this repo’s perspective):

1. Run `flutterfire configure` yourself.
2. Sign in as the owner account.
3. Disable developer/simulation mode.
4. Verify the device list, online status, detections, and commands.

Do not modify the Flutter repo from a device-side task.

### 6.6 Optional: enable OCR upload

Only if the user explicitly wants OCR telemetry:

1. Set `settings.uploadText: true` in the device document (Firebase Console).
2. Set `"upload_text": True` in the device `config.py` (so a restart before the
   first heartbeat does not drop text).
3. Restart the app and re-test. OCR text is capped at 500 characters.

Thumbnail upload remains unimplemented.

---

## 7. Known limitations / notes

- Battery monitor is disabled (`self.baterai = None`); `batteryPct` is null.
  The local API’s `battery: 85` is a fallback, not a real measurement.
- OCR confidence uses the documented placeholder `0.9`. Money confidence is the
  real average of the selected detections.
- `speak_text` fails honestly if the text cannot be mapped to available audio.
- `set_volume` calls the existing `Audio.setel_volume()`; actual ALSA mixer
  behavior still needs a hardware test with the headset connected.
- `config.py` is not in `app.yaml`. Reinstalling/updating the app may overwrite
  it; keep a local backup and recopy it, mode `600`.
- `restart`/`reprovision` are intentionally unsupported until firmware/hardware
  support is confirmed.
- The device clock resets on reboot without an RTC/NTP. The manual time set
  during this session is temporary; the bridge’s NTP guard will retry when
  Wi-Fi is available.
- The repo changes are uncommitted. The AI did not commit or push.
- Suggested commit message when the user is ready:

  ```text
  feat(device): integrate Firebase cloud telemetry and commands
  ```

---

## 8. Next action

**User:** fill `device_password` in the device `config.py`, turn on the saved
Wi-Fi hotspot/router, restart the app, and report whether `[CLOUD AUTH] login OK`
and `[CLOUD HEARTBEAT] OK` appear. Then verify `lastSeen` in the Firestore
Console and test one `sync_now` command.
