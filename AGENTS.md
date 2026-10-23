# AGENTS.md — SIGAP-NETRA Device (Python / MaixCAM)

> **Status:** living document. Last updated: 2026-10-22.
> **Read order:** `AGENTS.md` → `progress.md` → `docs/cloud_integration.md` →
> relevant read-only docs in the Flutter repo.

This file contains stable instructions for AI agents working on the **device-side**
Python application in this repository. It is not the Flutter companion app.

---

## 1. Project context

SIGAP-NETRA is an assistive smart-glasses project for blind and visually impaired
people. The device is a **Sipeed MaixCAM** (SG2002, MaixPy) with:

- camera + YOLOv11 money detection,
- PP-OCR text/menu reading,
- TF-Luna LiDAR distance gate,
- audio playback through a headset,
- physical button (A23),
- optional local HTTP API on port 8080.

The device must keep working **fully offline**. Cloud is telemetry and remote
monitoring only; it must never be required for detection or speech.

The active companion app is a **Flutter** project in a separate repository:

```text
C:\ngoding_skuy\sigap_netra_app
```

That repository is **read-only reference** for this repo. Do not modify it from a
device-side task. The older `companion_app/` folder in this repository is legacy
Kotlin/WebView and is not the active Flutter app.

---

## 2. Repository layout

```text
C:\ngoding_skuy\sigap-netra\
├── AGENTS.md                  # this file
├── progress.md                # current status, decisions, next action
├── docs\
│   ├── cloud_integration.md   # detailed cloud integration plan
│   ├── pinout.md
│   └── konsep_aplikasi_asistif.md
├── device\                    # ONLY Python folder to modify for device work
│   ├── main.py                # active MaixPy application (legacy, no cloud yet)
│   ├── main_en.py             # English annotated reference; do not edit unless asked
│   ├── sigap_core.py          # pure logic; hardware-independent
│   ├── sigap_api_server.py   # local HTTP REST server
│   ├── app.yaml               # MaixVision app manifest
│   ├── icon.png
│   ├── kata_utuh.py
│   └── models\
├── companion_app\             # legacy Kotlin/WebView; not active Flutter app
├── tools\
└── README.id.md / README.md
```

For cloud integration, the only writable target is:

```text
C:\ngoding_skuy\sigap-netra\device
```

Documentation files in the repo root and `docs/` may be updated only when the task
explicitly asks for documentation updates.

---

## 3. Hard boundaries

- **Do not modify** `C:\ngoding_skuy\sigap_netra_app` (Flutter). Read it only.
- **Do not commit or push.** The user commits manually.
- **Do not commit secrets:** `config.py`, Firebase passwords, tokens, service
  account JSON, `.env` files.
- **Do not add packages or paid services** without asking. Target Firebase Spark
  (free) plan. No Cloud Functions, no server-side FCM, no Blaze-only services.
- **Do not rewrite `sigap_cloud.py` from scratch.** A reference bridge already
  exists in the Flutter repo; port it and fix its known bugs.
- **Do not change** display/touchscreen behaviour, model files, pin mappings, or
  hardware timing as part of cloud work.
- **Do not invent hardware facts.** If LiDAR, audio, battery, pinout, or device
  behaviour is unclear, stop and ask.
- **Do not upload** video, audio, GPS/location, full-resolution images, Wi-Fi
  passwords, or Firebase tokens. OCR text is capped at 500 characters and only
  sent when the device setting allows it.
- **Do not mark readings as validated.** The device creates detections with
  `validationStatus: "pending"` only. Human validation belongs to the app.
- **Do not auto-start cloud** if configuration is missing or contains
  placeholders. Offline mode must remain the safe default.

When in doubt: stop, explain the conflict, and ask the user.

---

## 4. Source of truth (read-only)

Read these before changing cloud behaviour:

1. `C:\ngoding_skuy\sigap_netra_app\AGENTS.md`
2. `C:\ngoding_skuy\sigap_netra_app\PRD.md`
3. `C:\ngoding_skuy\sigap_netra_app\docs\firestore_schema.md`
4. `C:\ngoding_skuy\sigap_netra_app\docs\device_protocol.md`
5. `C:\ngoding_skuy\sigap_netra_app\firebase\firestore.rules`
6. `C:\ngoding_skuy\sigap_netra_app\firebase\firestore.indexes.json`
7. `C:\ngoding_skuy\sigap_netra_app\device\sigap_cloud.py` (reference bridge)
8. `C:\ngoding_skuy\sigap_netra_app\device\config.example.py` (reference config)

Precedence:

1. Explicit user instruction for the current task.
2. Flutter schema/protocol/rules (the canonical cross-project contract).
3. This `AGENTS.md`.
4. `progress.md` and `docs/cloud_integration.md`.
5. Existing Python code (legacy behaviour to preserve).

If two sources conflict, **stop and ask**. Do not silently choose.

---

## 5. Current state (2026-10-22)

- `device/main.py` is the legacy MaixPy app (~142 KB). It has **no cloud code**.
- `device/app.yaml` lists `main.py`, `sigap_core.py`, `sigap_api_server.py`,
  `kata_utuh.py`, and the four YOLO model files. It does **not** list cloud files.
- `device/sigap_cloud.py` and `device/config.py` do **not** exist in this repo.
- The reference bridge `sigap_cloud.py` exists in the Flutter repo, but has known
  bugs (see §10).
- The device is reachable at `10.212.207.1` via USB/NCM. SSH credentials are
  device-default; **never write the password into the repo**.
- The active app path is not yet confirmed: documentation says
  `/maixapp/apps/aplikasi_utama`, but a MaixVision screenshot shows `/root/apps`.
  Verify with SSH before deploying.
- Firebase Auth has an owner app account. A **dedicated device account** still
  needs to be created; do not reuse the owner account as the device identity.
- The Firestore device document still needs to be bootstrapped through the
  Firebase Console, including `authUid`, `members`, and `settings`.
- The Flutter app is still in simulation/stub mode (`firebase_options.dart` is a
  throwing stub). Do not block device work on the app UI; test device telemetry
  through the Firestore Console or REST.

Detailed status and next actions are in `progress.md`.

---

## 6. Target for this phase

Build a safe device-side cloud bridge:

```text
MaixCAM detection/audio/buttons
        │
        ├── (offline core, always works)
        │
        └── sigap_cloud.py
              ├── Firebase Auth REST (email/password)
              ├── heartbeat to devices/{deviceId}
              ├── detection upload to devices/{deviceId}/detections
              └── command polling from devices/{deviceId}/commands
```

Non-goals for this phase:

- no thumbnail/media upload (opt-in, not implemented),
- no video/audio/GPS upload,
- no Cloud Functions/FCM,
- no Flutter app changes,
- no schema/rules changes without explicit approval.

---

## 7. Cloud data contract (summary)

Canonical detail: `docs/firestore_schema.md` and `docs/device_protocol.md`.

### Device document

`devices/{deviceId}` — written only by the device (except owner membership fields
in console/protected flows):

```text
deviceId, authUid, name, model, firmwareVersion, wifiSsid,
lastSeen, bootCount, batteryPct, settings,
members, createdAt, updatedAt
```

`settings`:

```text
uploadThumbnails: boolean (default false)
uploadText: boolean (default false)
```

### Detections

`devices/{deviceId}/detections/{detectionId}` — created by device:

```text
type: "money" | "text"
amount: int | null
currency: "IDR"
confidence: number 0..1
distanceCm: int | null
ocrText: string <=500 | null
thumbnailId: string | null
validationStatus: "pending"
createdAt: timestamp
```

The device must not write `validatedBy` / `validatedAt`; only the app may update
those fields.

### Commands

`devices/{deviceId}/commands/{commandId}` — created by app members, status
advanced by device:

```text
type: "sync_now" | "speak_text" | "set_volume" | "restart" | "reprovision"
status: "pending" -> "sent" -> "acked" -> "done" | "failed"
payload: map
requestedBy: uid
createdAt, updatedAt, resultNote
```

Device may only change `status`, `updatedAt`, `resultNote`.

---

## 8. Cloud design rules

- **Background only.** Detection hooks must enqueue into a bounded queue; the
  worker thread performs auth/HTTP. Hooks must never call `_auth_header()` or
  `PATCH` directly.
- **Never block speech/detection.** If the queue is full, drop oldest/newest and
  log a short message. No retries that block the main loop.
- **Timestamps.** Do not write ISO strings as `stringValue`. Use Firestore
  `timestampValue` (current protocol decision, Opsi B) or a server transform
  (`documents:commit` with `updateTransforms`) if the owner chooses Opsi A.
- **Settings.** Read/cache `settings.uploadText`; default false. Do not upload OCR
  text when false.
- **Distance.** LiDAR returns float; Firestore rules require `distanceCm` as int.
  Cast to `int(round(...))` or send null.
- **Confidence.** Do not pretend a fake `1.0` is real model confidence. If the
  analyzer does not expose confidence yet, document the placeholder and ask.
- **Commands.** Implement `set_volume`, `speak_text`, `sync_now`. Do not mark
  `restart`/`reprovision` as done unless the hardware/firmware actually supports
  them. On unsupported commands, fail with a short `resultNote`.
- **Offline-first.** Missing/broken config disables cloud, not the device.
- **Secrets.** Never log email, password, token, or full SSID if it may contain
  personal data.

---

## 9. `main.py` integration anchors

Use anchors, not only line numbers. Current snapshot (2026-10-22):

| Area | Approx. line | Anchor |
|---|---:|---|
| import cloud | after 44 | end of `try: import cv2 ...` block |
| cloud attribute | 2744–2746 | `self.server_api = None` / `self.minta_proses = False` |
| start cloud | after 2758 | after `log("API", "Server REST ...")`, before `if not ENABLE_LAYAR` |
| money hook | after 2966 | after `self.audio.ucap_nominal(daftar, total)` |
| OCR hook | after 3030 | inside `if t:` after the `ucap` block |
| command handler | before 3461 | before `def tutup(self):` |
| stop cloud | after 3469 | after `self.server_api.stop()` |
| `app.yaml` | 8–18 | `files:` list |

Important:

- Use the local variable `t` for OCR upload. Do **not** use `self.baris_teks` at
  the end of `proses()`; it may contain stale text from a previous session.
- Place cloud start **before** the `if not ENABLE_LAYAR: return True` early return
  in `pemanasan()`, so cloud still runs with the display disabled.
- `tutup()` can be called twice (signal + `finally`). `sigap_cloud.stop()` must be
  idempotent.
- `Audio.setel_volume()` exists around line 1130; `minta_proses` is consumed in
  the main loop around line 3313.

Code snippets and the intended `sigap_cloud.py` interface are in
`docs/cloud_integration.md`.

---

## 10. Known bugs in the reference bridge

The existing Flutter-repo `device/sigap_cloud.py` is a useful starting point, but
do not copy it unmodified:

1. `catat_uang()` / `catat_teks()` perform auth + HTTP inline. This blocks the
   detection thread despite the “background thread” claim. Fix with a real queue
   and worker.
2. `_encode_value()` writes ISO timestamps as `stringValue`; the app expects
   Firestore `Timestamp`. Fix to `timestampValue` (or server transform).
3. `catat_teks()` ignores `settings.uploadText` and always sends `ocrText`.
4. `_heartbeat()` writes `lastSeen` as a string and cannot bootstrap a missing
   device document that satisfies rules.
5. `bootCount` is never incremented.
6. `_set_status()` writes `updatedAt` as a string.
7. There is no `on_command` handler in `main.py`; commands cannot work yet.
8. `distanceCm` may be a float; rules expect int.
9. The device cannot read `commands` unless it is a member or rules are changed
   (see §11).
10. There is no `muat_config()` guard for missing/placeholder `config.py`.

---

## 11. Firestore setup prerequisites

The code alone is not enough. Before cloud test:

1. **Create a dedicated Firebase Auth user for the device** (Email/Password).
   Do not use the owner’s main account as the device identity. Record the UID.
2. **Create `devices/{deviceId}` through the Firebase Console.** The device
   `create` rule allows only specific keys and does not allow `members`; the app
   cannot read the device until `members` contains its UID.
3. Add the owner app UID:

   ```text
   members: { "<UID_OWNER>": "owner" }
   ```

4. Set `settings`:

   ```text
   settings: { uploadThumbnails: false, uploadText: false }
   ```

5. **Command-read blocker:** rules currently allow `commands` read only for
   `isMember(deviceId)`, and the device is not a member. Choose one:
   - temporary: add device UID to `members` with a non-owner role (prototype
     only; gives device member read/delete privileges), or
   - proper: change rules to
     `allow read: if isMember(deviceId) || isDevice(deviceId);`
     This is a Flutter-repo change and requires explicit owner approval.
6. Ensure the composite index for `commands` (`status ASC`, `createdAt ASC`) is
   deployed.
7. Verify the Web API key allows Identity Toolkit and Firestore REST. If the key
   is restricted to Android/iOS apps, REST calls will fail.

Do not store the device password in the repo. It belongs only in `config.py` on
the device.

---

## 12. Workflow

1. Read `AGENTS.md`, `progress.md`, and `docs/cloud_integration.md`.
2. Read the relevant Flutter schema/protocol/rules.
3. Verify the active app path on the device via SSH before editing/deploying.
4. Make the smallest change that satisfies the task, only under `device/`.
5. Run syntax checks (`python -m py_compile`) and relevant pure tests.
6. Test offline first; then test cloud with the Firestore Console/REST.
7. Update `progress.md` with what changed, what was verified, and the next action.
8. Stop. Do not commit or push.

One feature/task per turn. Do not refactor unrelated code.

---

## 13. Testing / definition of done

Minimum for the cloud phase:

- Device boots and detects with no `config.py` and no Wi-Fi.
- Camera, YOLO, OCR, LiDAR, audio, and button behaviour are unchanged offline.
- With valid config: auth succeeds, `lastSeen` updates as a Firestore timestamp.
- Money detection creates a valid `detections` document (`type: money`,
  `validationStatus: pending`, `distanceCm` int or null, `createdAt` timestamp).
- Text detection respects `settings.uploadText`; no OCR text when false.
- `set_volume` command changes the actual ALSA volume and status reaches `done`.
- Unsupported commands end as `failed` with a short note, not fake success.
- No secrets appear in logs or repo.
- No changes in the Flutter repository.

---

## 14. Security and privacy

- Device credentials belong only in `config.py` on the device; `chmod 600`.
- `config.py` must not be listed in `app.yaml` and must not be committed.
- Never log tokens, passwords, or full email addresses.
- No video, audio, GPS/location, full images, Wi-Fi passwords, or MAC addresses.
- OCR text is sensitive: cap at 500 characters; upload only with opt-in.
- Thumbnail upload is opt-in and **not implemented** in this phase.
- If the repo becomes public, restrict the Firebase API key in Google Cloud
  Console to the required APIs.

---

## 15. Git policy

- AI must **never** run `git commit`, `git push`, amend, or force-push.
- AI may inspect `git status` / `git diff` and may stage files only if the user
  explicitly asks for `git add` (without commit).
- Provide conventional commit message suggestions when asked, for example:

  ```text
  feat(device): integrate Firebase cloud telemetry
  docs(device): add cloud integration handoff
  chore(device): update app manifest for cloud files
  ```

---

## 16. New-session handoff prompt

When starting a new session in this repository, read:

```text
AGENTS.md
progress.md
docs/cloud_integration.md
```

Then continue from the **Next action** in `progress.md`. Do not modify the
Flutter repo. Do not commit. Verify the device path and offline behaviour first.
