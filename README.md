<div align="center">

# 👓 Sigap Netra

### Offline Edge-AI Smart Glasses for the Visually Impaired

_Recognises Indonesian Rupiah banknotes and reads printed text aloud, entirely on-device._

[![Platform](<https://img.shields.io/badge/Platform-Sipeed%20MaixCam%20(SG2002)-0F9B8E?style=for-the-badge&logo=linux>)](https://wiki.sipeed.com/maixpy/)
[![YOLOv11n](https://img.shields.io/badge/YOLOv11n%20Delta-98.57%25%20%40%20conf%200.80-success?style=for-the-badge)](#-experimental-results)
[![Quantization](https://img.shields.io/badge/TPU--MLIR-INT8%202.88%20MB-orange?style=for-the-badge)](device/models/)
[![OCR](https://img.shields.io/badge/OCR-PP--OCR%20%2B%20Lexicon-blue?style=for-the-badge)](device/sigap_core.py)
[![Tests](https://img.shields.io/badge/Unit%20Tests-284%20passing-brightgreen?style=for-the-badge)](device/sigap_core.py)
[![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)](LICENSE)

Mechatronics Engineering · Politeknik Negeri Batam
PKM-KC (Program Kreativitas Mahasiswa - Karsa Cipta) & Final Project

<br/>

**Languages:** **English** | [Bahasa Indonesia](README.id.md)

</div>

---

## 📑 Table of Contents

1. [Overview](#-overview)
2. [System Architecture](#-system-architecture)
3. [Key Features](#-key-features)
4. [Experimental Results](#-experimental-results)
5. [Hardware & Pinout](#-hardware--pinout)
6. [Repository Structure](#-repository-structure)
7. [Sipeed MaixCam Usage Guide](#-sipeed-maixcam-usage-guide)
8. [Companion App](#-companion-app)
9. [REST API Reference](#-rest-api-reference)
10. [Author](#-author)
11. [License](#-license)

---

## 📌 Overview

**Sigap Netra** is a wearable assistive device that lets visually impaired users
count money and read printed text (menus, labels, documents) independently.
Everything runs **offline** on a **Sipeed MaixCam** (Sophgo SG2002, RISC-V C906,
1 TOPS NPU). No cloud, no internet, no smartphone required for daily use.

Output is spoken Indonesian, built by concatenating pre-recorded `.wav`
snippets, so speech is natural and works without any TTS engine on the device.

> **Design principle: silence is safer than a wrong answer.**
> A blind user cannot verify what the speaker says. The system is tuned so that
> when it is unsure, it stays silent and lets the user retry, instead of
> announcing a wrong denomination.

---

## 🧭 System Architecture

```text
 ┌───────────────────────────────┐
 │  SENSORS & PERIPHERALS        │
 │  • MaixCam camera (ISP)       │
 │  • TF-Luna LiDAR   UART1      │
 │  • ADS1115 ADC     I2C5       │
 │  • Push button     GPIO A23   │
 │  • Audio DAC  ->  earphone    │
 └───────────────┬───────────────┘
                 ▼
 ┌───────────────────────────────┐
 │  EDGE DEVICE (MaixCam SG2002) │
 │  • YOLOv11n "Delta" INT8      │
 │  • PP-OCR + 2x2 tiling        │
 │  • Lexicon correction engine  │
 │  • WAV concatenation speech   │
 │  • HTTP server :8080          │
 └───────────────┬───────────────┘
                 │ Wi-Fi (same network)
                 ▼
 ┌───────────────────────────────┐
 │  COMPANION APP (optional)     │
 │  • Android (Kotlin + WebView) │
 │  • Log sync with SHA-1 check  │
 │  • Human validation & stats   │
 └───────────────────────────────┘
```

---

## 🌟 Key Features

### 1. Banknote recognition (YOLOv11n "Delta")

- Custom dataset of 7 Rupiah denominations (Rp1.000 to Rp100.000), quantised
  with **TPU-MLIR** to INT8 (**2.88 MB**) with a BF16 fallback (**5.76 MB**).
- **Burst voting**: 6 frames per press (`FRAME_CAPTURE = 6`); a note is only
  announced when at least 2 frames agree (`SETUJU_MIN = 2`).
- **Two-level deduplication**: `IOU_DEDUP = 0.45` for the same denomination
  and `IOU_DEDUP_BEDA = 0.70` for overlapping notes of different value, so
  stacked notes are summed correctly.
- **Geometry filter**: rejects boxes with implausible aspect ratio
  (`1.40` to `3.40`) or that fill more than 72% of the frame.

### 2. Printed text & menu reading (PP-OCR + lexicon)

- **2x2 overlapping tiling** (`704x493` tiles) so small menu fonts are detected.
- **Sharpest-frame selection** using Laplacian variance before OCR.
- **LiDAR-aware sharpening**: unsharp-mask strength follows the measured distance.
- **Lexicon engine** in `sigap_core.py`: dictionary correction, glued-word
  splitting, loanword pronunciation (`speclal` to `spesial`), two-column menus,
  item-to-price pairing, section titles, and junk-line rejection.
- A price is **never** spoken unless its item name is recognised.

### 3. Sensor fusion

- **TF-Luna LiDAR** acts as a distance gate in text mode: valid range is
  **10 to 40 cm**; outside it the device says _"terlalu dekat"_ or _"terlalu jauh"_.
  If the LiDAR is missing, the app keeps working without the gate.
- **ADS1115** monitors the 1S LiPo battery with hysteresis: _low_ warning
  around 3.55 V and _critical_ around 3.40 V, without spamming the user.
- **Single button** state machine: tap, double-tap, and long-press.

---

## 📊 Experimental Results

Official comparison on **210 hand-held test images** (7 denominations x 30,
5 lighting/pose conditions, never used for training), evaluated at the
**operational threshold `CONF_MIN = 0.80`** that the device actually uses.

| Model          |   Correct   |  Accuracy  |    Wilson 95% CI    | Wrong value | Silent | Status       |
| :------------- | :---------: | :--------: | :-----------------: | :---------: | :----: | :----------- |
| Beta (v7)      |   182/210   |   86.67%   |   81.40% - 90.64%   |      7      |   21   | Retired      |
| Charlie (v8)   |   195/210   |   92.86%   |   88.55% - 95.62%   |      0      |   15   | Retired      |
| **Delta (v9)** | **207/210** | **98.57%** | **95.88% - 99.51%** |    **0**    | **3**  | **Deployed** |
| Epsilon (v10)  |   204/210   |   97.14%   |   93.91% - 98.68%   |      0      |   6    | Evaluated    |

- **Zero wrong denominations** since v8: every remaining error is the device
  staying silent, which the user can simply retry.
- Source: `Laporan/hasil_komparasi_semua_model.txt` in the main project.

> [!NOTE]
> **Known limitation.** All 210 test images contain a banknote. False alarms on
> scenes _without_ money have not been measured yet. Do not lower `CONF_MIN`
> based on these numbers alone.

---

## 🔌 Hardware & Pinout

| Component     | Interface | MaixCam pin | Notes                                              |
| :------------ | :-------- | :---------- | :------------------------------------------------- |
| TF-Luna LiDAR | UART1 RX  | **A18**     | Connect to LiDAR TX. `/dev/ttyS1`, 115200 8N1      |
| TF-Luna LiDAR | UART1 TX  | **A19**     | Connect to LiDAR RX                                |
| ADS1115 ADC   | I2C5 SCL  | **A15**     | Address `0x48`, divider 100k / 51k                 |
| ADS1115 ADC   | I2C5 SDA  | **A27**     |                                                    |
| Push button   | GPIO      | **A23**     | Internal pull-up, other leg to **GND**             |
| Audio         | ALSA      | -           | PCM5102 or USB audio, auto-detected via `aplay -l` |

> [!WARNING]
> **Do not use A16/A17.** They are taken by the MaixPy launcher. A wrong pin
> fails **silently**, so never guess: verify with a test script first.
> Detailed diagram: [docs/pinout.md](docs/pinout.md).

---

## 📂 Repository Structure

```text
Sigap-Netra/
├── device/                    # Runs on the MaixCam
│   ├── app.yaml               # MaixPy app manifest (id: aplikasi_utama)
│   ├── icon.png
│   ├── main.py                # Main application (Indonesian comments)
│   ├── main_en.py             # Same app, English comments (reference)
│   ├── sigap_core.py          # Pure logic, no maix import, 284 unit tests
│   ├── sigap_api_server.py    # Standalone HTTP server (reference)
│   ├── kata_utuh.py           # Word bank for the dictionary
│   ├── models/                # YOLOv11n Delta INT8 + BF16 (.cvimodel + .mud)
│   └── README.md
├── companion_app/
│   ├── android/               # Android Studio project (Kotlin + WebView)
│   ├── web/                   # Same UI as a standalone HTML page
│   └── README.md
├── tools/
│   └── periksa_main.py        # Static checker for main.py (run on PC)
├── docs/
│   ├── konsep_aplikasi_asistif.md   # Companion app concept & handover
│   └── pinout.md
├── LICENSE
└── README.md
```

---

## 🛠️ Sipeed MaixCam Usage Guide

This section walks through everything from a blank MaixCam to daily use.

### Step 0 - What you need

| Item                                                     | Purpose                    |
| :------------------------------------------------------- | :------------------------- |
| Sipeed MaixCam + microSD (8 GB or more)                  | Main device                |
| USB Type-C cable                                         | Power, first connection    |
| PC with [MaixVision](https://wiki.sipeed.com/maixvision) | Install, run, view logs    |
| Python 3.8+ on the PC                                    | Run tests before deploying |
| TF-Luna, ADS1115, push button, audio DAC, earphone       | Peripherals (see pinout)   |
| 1S LiPo battery                                          | Portable power             |

Files that are **not** in this repository because of size:

| Asset                    | Location on device                                | Notes                                |
| :----------------------- | :------------------------------------------------ | :----------------------------------- |
| Audio bank, male voice   | `/root/audio_sigap/`                              | ~3,200 `.wav`, ~175 MB, **required** |
| Audio bank, female voice | `/root/audio_sigap_perempuan/`                    | Optional                             |
| PP-OCR model             | `/root/models/pp_ocr.mud` + its `.cvimodel` files | Official Sipeed MaixHub model        |

Ask the project maintainer for the audio bank archive.

### Step 1 - Prepare the MaixCam

1. Flash the latest **MaixPy system image** to the microSD card, following the
   [official MaixPy guide](https://wiki.sipeed.com/maixpy/doc/en/basic/os.html).
2. Insert the card, power on via USB-C, and wait for the launcher screen.
3. On the device touchscreen open **Settings -> WiFi** and join the same
   network as your PC (a phone or laptop hotspot works well in the field).
4. Note the device IP address (shown in Settings or in MaixVision).
5. Test SSH from the PC. Default credentials are `root` / `root`:
   ```bash
   ssh root@<DEVICE_IP>
   ```

### Step 2 - Wire the peripherals

Wire according to the [pinout table](#-hardware--pinout). Checklist:

- LiDAR **TX goes to A18** and **RX goes to A19** (crossed).
- The button goes between **A23** and **GND**; no external resistor needed.
- Plug the earphone into the audio DAC before booting so ALSA detects it.

### Step 3 - Verify on the PC first

`sigap_core.py` has no `maix` dependency, so all logic can be tested on a PC:

```bash
cd device
python sigap_core.py                          # expected: 284 passed, 0 failed
cd ..
python tools/periksa_main.py device/main.py   # expected: 0 issues found (clean)
```

Do not deploy if either check fails.

### Step 4 - Copy the large assets

From the PC, copy the audio bank and the OCR model once:

```bash
scp -r audio_sigap root@<DEVICE_IP>:/root/
ssh root@<DEVICE_IP> "mkdir -p /root/models"
scp pp_ocr.mud *.cvimodel ppocr_keys_v1.txt root@<DEVICE_IP>:/root/models/
```

The app searches for the OCR model in this order:
`/root/OCR/pp_ocr_640_en.mud`, `/root/OCR/pp_ocr_640.mud`,
`/root/models/pp_ocr_640_en.mud`, `/root/models/pp_ocr_640.mud`,
`/root/models/pp_ocr.mud`, then `<app>/models/`.

### Step 5 - Install the application

**Option A - MaixVision (recommended for first install)**

1. Open MaixVision and connect to the device.
2. Open the `device/` folder as the project.
3. Use **Install App**. MaixVision packages files listed in `app.yaml` and
   registers the app in the launcher as **Sigap Netra**.

**Option B - Update an installed app over SSH**

The app id is `aplikasi_utama`, so the install path is
`/maixapp/apps/aplikasi_utama/`:

```bash
scp -r device/* root@<DEVICE_IP>:/maixapp/apps/aplikasi_utama/
ssh root@<DEVICE_IP> "rm -rf /maixapp/apps/aplikasi_utama/__pycache__ /root/__pycache__"
```

> [!IMPORTANT]
> **Stale `sigap_core.py` copies win.** Python searches `/root` before the app
> folder. If an old `/root/sigap_core.py` exists, it is loaded instead of the
> new one, causing `AttributeError` at runtime. Remove or update it, and always
> delete `__pycache__` after copying.

### Step 6 - Run the application

| Method                          | When to use            | Notes                                           |
| :------------------------------ | :--------------------- | :---------------------------------------------- |
| Launcher icon **Sigap Netra**   | Daily use              | Runs the installed copy                         |
| MaixVision **Run** on `main.py` | Development, live logs | See note below                                  |
| **Settings -> Auto Start**      | Production             | App starts on boot (label may vary by firmware) |

> [!NOTE]
> MaixVision **Run** copies only `main.py` to `/tmp/maixpy_run/`. In that case
> the app loads models from `/root/models/` and `sigap_core.py` from `/root/`.
> Running from MaixVision does **not** update the installed launcher app.

On start, the device warms up the models, then says **"kamera siap"**
(camera ready). The default mode is **AUTO**.

To stop: press Stop in MaixVision, or over SSH find the process with
`ps | grep main.py` and `kill <PID>` (the app closes the camera and audio
cleanly on `SIGTERM`). The board's own KEY button is ignored on purpose,
because the glasses casing can press it by accident.

### Step 7 - Operating the glasses

#### Modes

Double-tap cycles **AUTO -> UANG -> TEKS** and announces the new mode.

| Mode     | Behaviour                                            |
| :------- | :--------------------------------------------------- |
| **AUTO** | Looks for money first. If none is found, reads text. |
| **UANG** | Money only. Announces each note and the total.       |
| **TEKS** | Text only. LiDAR gate active (10 to 40 cm).          |

#### Button controls

Timing: a tap is a short press; a **double-tap** needs the second press within
**0.18 s**; a **long press** is held for **1 s**.

| State                                            | Tap                     | Double-tap             | Long press (1 s)       |
| :----------------------------------------------- | :---------------------- | :--------------------- | :--------------------- |
| **Ready** (camera preview)                       | Capture and process     | Next mode              | Stop audio, stay ready |
| **Reading** (long text, 3 lines at a time)       | Skip to next 3 lines    | Repeat current 3 lines | Stop, back to camera   |
| **Menu sections** (menu with 2+ titled sections) | Enter announced section | Next section           | Stop, back to camera   |

In the Reading state, the next group is read automatically when the current
audio finishes, so the user only needs to press when they want to skip.

#### Spoken Audio Cues (Indonesian Speech Engine)

| Spoken Cue (Indonesian) | Meaning (English)      | Trigger Condition                                    |
| :---------------------- | :--------------------- | :--------------------------------------------------- |
| _"kamera siap"_         | Camera ready           | System initialised or returned to idle preview state |
| _"mohon tunggu"_        | Please wait            | OCR processing pipeline executing on NPU             |
| _"terlalu dekat"_       | Too close              | LiDAR measured target distance < 10 cm               |
| _"terlalu jauh"_        | Too far                | LiDAR measured target distance > 40 cm               |
| _"deteksi gagal"_       | Detection failed       | No valid banknote or text found in capture           |
| _(Silence)_             | No confident detection | No detection reached the `CONF_MIN = 0.80` threshold |

#### Tips for best results

- **Money:** hold one or more notes flat in front of the lens with the whole
  note visible. Do not hold so close that one note fills the entire view.
- **Text:** hold the page 10 to 40 cm away, facing the camera squarely.
- Keep still for about half a second after pressing; 6 frames are captured.
- Good, even lighting matters more than distance.

### Step 8 - Configuration reference

All tunables are at the top of `device/main.py`:

| Constant              | Default               | Meaning                                                       |
| :-------------------- | :-------------------- | :------------------------------------------------------------ |
| `CONF_MIN`            | `0.80`                | Minimum confidence for money. Keep at 0.80 (see results note) |
| `FRAME_CAPTURE`       | `6`                   | Frames captured per press                                     |
| `SETUJU_MIN`          | `2`                   | Frames that must agree before speaking                        |
| `MODE_URUT`           | `AUTO, UANG, TEKS`    | Mode cycle order                                              |
| `BATAS_JARAK_TEKS_CM` | `40.0`                | Max text distance                                             |
| `JARAK_MIN_CM`        | `10.0`                | Min text distance                                             |
| `VOICE_PROFILE`       | `"MALE"`              | `"FEMALE"` uses `/root/audio_sigap_perempuan/`                |
| `VOLUME_DEFAULT`      | `75`                  | Speaker volume (%)                                            |
| `ENABLE_LIDAR`        | `True`                | Disable if no LiDAR fitted                                    |
| `ENABLE_TOUCHSCREEN`  | `False`               | Touch controls (off: button only)                             |
| `PIN_TOMBOL`          | `"A23"`               | Button pin. Change only after testing                         |
| `NAMA_MODEL`          | Delta INT8, then BF16 | Model files tried in order                                    |

Environment variables: `SIGAP_PORT` (default `8080`), `SIGAP_DEVICE_ID`,
`SIGAP_TOKEN`, `SIGAP_DATA_DIR` (default `/root`).

### Step 9 - Logs and stored data

| What                                                             | Where                         |
| :--------------------------------------------------------------- | :---------------------------- |
| Live log (tags: `TOMBOL`, `LIDAR`, `YOLO`, `AUDIO`, `HASIL` ...) | MaixVision terminal / stdout  |
| Crash traceback                                                  | `galat.log` in the app folder |
| Active capture log                                               | `/root/uji_rekam.csv`         |
| Captured images                                                  | `/root/uji_rekam/`            |
| Sealed sessions (for the companion app)                          | `/root/segel_<timestamp>.csv` |

### Step 10 - Troubleshooting

| Symptom                           | Check                                                                                                                                            |
| :-------------------------------- | :----------------------------------------------------------------------------------------------------------------------------------------------- |
| No sound at all                   | `aplay -l` shows the DAC? Is `/root/audio_sigap/` present? Look for `AUDIO` in the log                                                           |
| Button does nothing               | Log must show `[AKTIF] Pin A23`. Button wired to GND?                                                                                            |
| No distance warnings              | Log indicates `"LiDAR tidak aktif"` (inactive): TX/RX wires swapped or 5V disconnected. The app falls back to safe scanning without LiDAR gating |
| Model fails to load               | `.cvimodel` and `.mud` both present in `models/` or `/root/models/`?                                                                             |
| OCR never runs                    | PP-OCR files in one of the search paths (Step 4)?                                                                                                |
| `AttributeError` in `sigap_core`  | Old copy in `/root`. Update it and delete `__pycache__`                                                                                          |
| Continuous "terlalu jauh" in TEKS | Hold text within 10–40 cm, or set `ENABLE_LIDAR = False` to test                                                                                 |
| Companion app cannot connect      | Same Wi-Fi? Open `http://<DEVICE_IP>:8080/health` in a browser                                                                                   |
| Changes not visible after Run     | MaixVision Run does not update the installed app (Step 5B)                                                                                       |

---

## 📱 Companion App

An optional Android app for caregivers and researchers, **not** needed for
daily use. It syncs the device logs, verifies them, and supports human
validation and camera A/B statistics (sign test).

1. Open `companion_app/android/` in Android Studio and let Gradle sync.
2. Build and install on a phone with **Android 7.0 (API 24)** or newer.
3. Connect the phone to the **same Wi-Fi** as the MaixCam.
4. In the app, enter the device IP address, then tap **"Sinkronkan"** (Sync).

Sync flow: `GET /health` -> `POST /session/seal` -> `GET /session/<id>/csv`
-> SHA-1 check -> download images -> `DELETE /session/<id>`.
Data is only deleted from the device after the checksum matches.

More: [companion_app/README.md](companion_app/README.md) and
[docs/konsep_aplikasi_asistif.md](docs/konsep_aplikasi_asistif.md).

---

## 📡 REST API Reference

Served by `main.py` on port **8080** while the app is running.

| Method   | Endpoint                              | Description                                      |
| :------- | :------------------------------------ | :----------------------------------------------- |
| `GET`    | `/health`, `/api/status`, `/api/ping` | Device id, firmware, battery %, pending rows, IP |
| `GET`    | `/sessions`                           | List sealed sessions with row count and SHA-1    |
| `GET`    | `/session/<id>/csv`                   | Download a sealed session CSV                    |
| `GET`    | `/session/<id>/img/<file>`            | Download a captured image                        |
| `GET`    | `/foto/<file>`                        | Same image, short path                           |
| `GET`    | `/api/transaksi`                      | Active log as JSON                               |
| `GET`    | `/api/download_csv`                   | Active log as CSV                                |
| `POST`   | `/session/seal`                       | Seal the active log into a new session           |
| `POST`   | `/api/capture`, `/api/proses`         | Trigger a capture remotely                       |
| `POST`   | `/api/hapus_semua`                    | Clear active log and images                      |
| `DELETE` | `/session/<id>`                       | Delete a sealed session                          |
| `DELETE` | `/api/transaksi`                      | Clear the active log                             |

Example:

```bash
curl http://<DEVICE_IP>:8080/health
```

```json
{
  "id": "SIGAP-GHTW-0007",
  "fw": "v1.2.0-ghtw",
  "battery": 82,
  "pending_rows": 12,
  "ip": "192.168.137.81",
  "port": 8080,
  "...": "..."
}
```

> [!CAUTION]
> The server has no authentication and is meant for a private local network
> only. Do not expose port 8080 to the internet.

---

## 👤 Author

**Gusti Hakim Thoriq Wicaksono**

|                 |                                      |
| :-------------- | :----------------------------------- |
| **NIM**         | 4212301085                           |
| **Program**     | Mechatronics Engineering, Semester 7 |
| **Institution** | Politeknik Negeri Batam, Indonesia   |

Special thanks to the PKM-KC advisors, Politeknik Negeri Batam, and the
open-source communities behind MaixPy, Ultralytics YOLO, and PaddleOCR.

---

## 📄 License

Released under the [MIT License](LICENSE).
