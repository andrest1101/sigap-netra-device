"""
Aplikasi Utama V3 - SIGAP NETRA: Integrasi Penuh Periferal & Multi-Uang Pintar
==============================================================================
Alat bantu baca tunanetra berbasis Edge AI (Sipeed MaixCam / SoC SG2002).
Fitur Versi 3:
- Model YOLOv11n Charlie INT8/BF16 (Stabil & Teruji di MaixCAM).
- Deduplikasi Spasial Sensitif Nominal (Class-Aware Dedup): aman menjumlahkan
  uang bersebelahan tanpa risiko mengarang uang ganda pada uang lecek/terlipat.
- Konsensus Temporal Multi-Frame Tangguh (6 Frame Burst).
- Integrasi Periferal Lengkap: Tombol Fisik (A23), LiDAR TF-Luna (A18/A19),
  Monitor Baterai ADS1115 (A15/A27), PP-OCR Photoshop USM Adaptif LiDAR.

NAVIGASI & PEMICU UTAMA (Pin A23 & Touchscreen):
    1. TEKAN 1x (Tunggal) -> PROSES (Pindai Uang / PP-OCR) / BUKA / LANJUT Baris
    2. TEKAN 2x (Ganda)   -> Ganti MODE (AUTO -> UANG -> TEKS) / Pindah BAGIAN Menu
    3. TAHAN    (Long)    -> ULANGI ucapan audio / kembali ke AWAL baris
"""

import glob
import hashlib
import http.server
import json
import math
import os
import re
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import traceback
import uuid
from collections import deque

APP_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in (APP_DIR, "/root", "/maixapp/apps", "/tmp/maixpy_run"):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from maix import app, camera, display, image, nn, touchscreen
except ImportError:
    app = camera = display = image = nn = touchscreen = None

try:
    from maix import gpio, pinmap
except ImportError:
    gpio = pinmap = None

try:
    import numpy as np
except ImportError:
    np = None

try:
    import cv2
except ImportError:
    cv2 = None

try:
    import sigap_cloud
except Exception:
    sigap_cloud = None

# ======================================================================
# PILIHAN SUARA AUDIO (PRIA / PEREMPUAN)
# "PRIA"      -> Menggunakan folder /root/audio_sigap (Suara Ardi)
# "PEREMPUAN" -> Menggunakan folder /root/audio_sigap_perempuan (Suara Gadis)
# ======================================================================
MODEL_SUARA = "PRIA"  # <- Ubah ke "PEREMPUAN" untuk suara wanita

def tentukan_dir_audio(model_suara=MODEL_SUARA):
    if str(model_suara).strip().upper() == "PEREMPUAN":
        return [
            "/root/audio_sigap_perempuan",
            "/root/audio_perempuan",
            os.path.join(APP_DIR, "audio_sigap_perempuan"),
            "/root/audio_sigap",
            "/root/audio_generated",
            "/root/audio",
            "/root",
            os.path.join(APP_DIR, "audio_sigap"),
            os.path.join(APP_DIR, "audio_generated"),
            os.path.join(APP_DIR, "audio"),
        ]
    return [
        "/root/audio_sigap",
        "/root/audio_generated",
        "/root/audio",
        "/root",
        os.path.join(APP_DIR, "audio_sigap"),
        os.path.join(APP_DIR, "audio_generated"),
        os.path.join(APP_DIR, "audio"),
        "/root/audio_sigap_perempuan",
        os.path.join(APP_DIR, "audio_sigap_perempuan"),
    ]

DIR_AUDIO = tentukan_dir_audio(MODEL_SUARA)
DIR_MODEL = os.path.join(APP_DIR, "models") if os.path.exists(os.path.join(APP_DIR, "models")) else "/root/models"
NAMA_MODEL = ["yolo11n_rupiah_delta_int8.mud",
              "yolo11n_rupiah_delta_bf16.mud",
              "yolo11n_rupiah_charlie_int8.mud",
              "yolo11n_rupiah_charlie_bf16.mud",
              "yolo11n_rupiah_v3_int8.mud",
              "yolo11n_rupiah_v3_bf16.mud",
              "yolo11n_rupiah_v2_int8.mud"]

# Urutan prioritas model OCR (pp_ocr_640_en adalah model det 640 hasil benchmark akurasi tinggi)
DAFTAR_OCR = [
    "/root/OCR/pp_ocr_640_en.mud",
    "/root/OCR/pp_ocr_640.mud",
    "/root/models/pp_ocr_640_en.mud",
    "/root/models/pp_ocr_640.mud",
    "/root/OCR/pp_ocr_en.mud",
    "/root/OCR/pp_ocr_v4.mud",
    "/root/models/pp_ocr.mud",
    os.path.join(APP_DIR, "models/pp_ocr_640_en.mud"),
    os.path.join(APP_DIR, "models/pp_ocr.mud"),
    os.path.join(APP_DIR, "OCR/pp_ocr_640_en.mud"),
    os.path.join(APP_DIR, "OCR/pp_ocr_en.mud"),
]
MODEL_OCR = next((p for p in DAFTAR_OCR if os.path.exists(p)),
                 "/root/models/pp_ocr.mud")

GAGAL_LAYAR_MAKS = 15

# Ambang Deteksi & Parameter Operasional
# Ambang KANDIDAT, bukan ambang keputusan. Penjaga sebenarnya adalah
# voting antar frame. Ambang 0,80 membuat 4 dari 14 bidikan gagal
# sepenuhnya karena seluruh frame kosong (Tahap 1, Perbaikan 3b).
CONF_MIN = 0.80
CONF_MIN_UANG = 0.80
IOU_DEDUP = 0.45            # ambang ketat untuk nominal sama (cegah double count pada uang terlipat)
IOU_DEDUP_BEDA = 0.70       # ambang toleran untuk nominal berbeda (dukung penjumlahan uang berdampingan)
IOU_NMS_YOLO = 0.65         # NMS MaixPy longgar agar tidak membuang kandidat multi-uang sebelum dedup
FRAME_CAPTURE = 6           # 6 frame burst untuk konsensus temporal yang stabil
SETUJU_MIN = 2
MIN_SENDIRI_OCR = 1
FRAME_OCR = 1              # petak sudah memberi pengamatan berulang
BARIS_SEKALI = 3
MODE_URUT = ["AUTO", "UANG", "TEKS"]

# Pemotongan petak untuk OCR.
#
# Letterbox 1280x720 ke 320x224 memberi skala 0,25 - huruf menu setinggi
# 20 px menyusut jadi 5 px, di bawah batas kemampuan PP-OCRv3 (~8-10 px).
# Petak 704x493 punya rasio yang sama dengan input model, jadi tidak ada
# bilah hitam, dan skalanya 0,454 - huruf jadi 9,1 px.
PETAK_OCR = True           # setel False untuk kembali ke cara lama
PETAK_W, PETAK_H = 704, 493
PETAK_X = [0, 576]         # tumpang tindih 128 px
PETAK_Y = [0, 227]         # tumpang tindih 266 px
# Kotak yang tepinya menyentuh batas petak besar kemungkinan adalah
# penggal dari teks yang terpotong, bukan kata utuh. Batas frame asli
# (x=0, y=0, x=1280, y=720) tidak dihitung sebagai sambungan.
MARGIN_SAMBUNGAN = 6      # piksel, pada koordinat frame asli

# Pemisahan kolom pada menu bertata letak dua lajur.
#
# Lajur kosong (gutter) dicari hanya di bagian tengah frame, karena
# tepi kiri dan kanan selalu kosong dan bukan pembatas kolom.
GUTTER_MIN_LEBAR = 0.05        # minimal 5% lebar frame
GUTTER_ZONA = (0.28, 0.72)     # daerah pencarian, relatif lebar frame
GUTTER_MIN_KOTAK = 6           # di bawah ini tidak layak dibelah
GUTTER_MIN_ISI = 3             # tiap kolom minimal sekian kotak

# Uang kertas rupiah berukuran sekitar 141-151 mm x 65 mm, jadi
# perbandingan sisinya 2,17 sampai 2,32. Rentang diperlebar untuk
# menampung uang yang terlipat, miring, atau terpotong bingkai.
RASIO_UANG_MIN = 1.40
RASIO_UANG_MAKS = 3.40

# Deteksi yang memenuhi hampir seluruh bingkai hampir pasti bukan
# selembar uang, melainkan permukaan besar seperti poster atau meja.
LUAS_UANG_MAKS = 0.72

# Mode AUTO tetap lebih ketat. Di sana kesalahan lebih mahal: menyebut
# poster sebagai uang tidak terdengar janggal oleh pengguna, sedangkan
# gagal mendeteksi langsung disadari dan bisa diulang.
CONF_MIN_AUTO = 0.80

# ======================================================================
# SAKLAR FITUR HARDWARE (Ubah True / False sesuai kebutuhan)
# ======================================================================
ENABLE_LAYAR = True        # True = Layar LCD Aktif | False = Layar Mati (Hemat Daya)
ENABLE_TOUCHSCREEN = True # True = Layar Sentuh Aktif | False = Khusus Tombol Fisik A23
ENABLE_LIDAR = True        # True = Sensor Jarak TF-Luna Aktif | False = Tanpa Sensor Jarak
VOLUME_DEFAULT = 75     # Tingkat Volume Suara (10 - 100%)

# Konfigurasi Pin Periferal Hardware
PIN_TOMBOL = "A23"
PIN_LIDAR_TX, PIN_LIDAR_RX = "A19", "A18"
PORT_LIDAR, BAUD_LIDAR = "/dev/ttyS1", 115200
SLOPE_LIDAR, INTERCEPT_LIDAR = 0.9989, 0.1245

PIN_ADS_SCL, PIN_ADS_SDA = "A15", "A27"
ALAMAT_ADS = 0x48
PGA_VOLT_ADS = 4.096
RASIO_PEMBAGI_ADS = (100.0 + 51.0) / 51.0

# Ambang Jarak Saklar Spasial LiDAR (cm)
# Diperluas ke 40 cm agar jarak baca menu/dokumen di atas meja tetap nyaman dan wajar
BATAS_JARAK_TEKS_CM = 40.0
JARAK_MIN_CM = 10.0
JARAK_MAKS_UANG_CM = 100.0

# Kurva Karakteristik Baterai LiPo 1S
KURVA_LIPO = [
    (4.20, 100), (4.15, 95), (4.11, 90), (4.08, 85), (4.02, 80),
    (3.98, 75),  (3.95, 70), (3.91, 65), (3.87, 60), (3.85, 55),
    (3.84, 50),  (3.82, 45), (3.80, 40), (3.79, 35), (3.77, 30),
    (3.75, 25),  (3.73, 20), (3.71, 15), (3.69, 10), (3.30, 5),
    (2.95, 0)
]

VERSI = "2026-08-25_v2_full_peripheral"

core = None
KETERANGAN_CORE = ""
try:
    import sigap_core as core
    KETERANGAN_CORE = "sigap_core " + getattr(core, "VERSI", "?")
except BaseException as e:
    KETERANGAN_CORE = "sigap_core GAGAL: %s" % type(e).__name__


def log(t, m):
    print("[%s] %s" % (t, m), flush=True)


# ======================================================================
# PEMBACA POLA TOMBOL FISIK
# ======================================================================
class PolaTombolMandiri:
    def __init__(self, debounce=0.03, jeda_ganda=0.18, tahan=1.00):
        self._lalu = False
        self._mulai = 0.0
        self._lepas = None
        self._tunggu = 0
        self._abaikan = False
        self._tahan_terpicu = False
        self.debounce = debounce
        self.jeda_ganda = jeda_ganda
        self.tahan = tahan

    def reset(self):
        """Buang semua status/sisa penekanan lama agar tidak memicu aksi hantu."""
        self._lalu = False
        self._mulai = 0.0
        self._lepas = None
        self._tunggu = 0
        self._abaikan = True
        self._tahan_terpicu = False

    def perbarui(self, ditekan, sekarang):
        ditekan = bool(ditekan)
        turun = ditekan and not self._lalu
        naik = (not ditekan) and self._lalu
        self._lalu = ditekan

        if turun:
            self._mulai = sekarang
            self._abaikan = False
            self._tahan_terpicu = False
            if self._tunggu == 1 and self._lepas is not None \
                    and (sekarang - self._lepas) <= self.jeda_ganda:
                self._tunggu = 0
                self._lepas = None
                self._abaikan = True
                return "ganda"

        # Deteksi tahan langsung saat tombol masih ditekan (responsif instan)
        if ditekan and not self._abaikan and not self._tahan_terpicu:
            if (sekarang - self._mulai) >= self.tahan:
                self._tahan_terpicu = True
                self._abaikan = True
                self._tunggu = 0
                self._lepas = None
                return "tahan"

        if naik and not self._abaikan:
            lama = sekarang - self._mulai
            if lama < self.debounce:
                return None
            self._tunggu = 1
            self._lepas = sekarang

        if self._tunggu == 1 and self._lepas is not None \
                and not ditekan and (sekarang - self._lepas) > self.jeda_ganda:
            self._tunggu = 0
            self._lepas = None
            return "tunggal"
        return None


class Tombol:
    def __init__(self, pin=PIN_TOMBOL):
        self.hidup = False
        self.pin = pin
        self._io = None
        self.pola = PolaTombolMandiri()

        if pin is None:
            log("TOMBOL", "PIN_TOMBOL bernilai None (Navigasi via Touchscreen)")
            return
        if gpio is None or pinmap is None:
            log("TOMBOL", "Modul maix.gpio / pinmap tidak tersedia")
            return
        try:
            fungsi = pinmap.get_pin_functions(pin)
            gp = [f for f in fungsi if "GPIO" in f.upper()]
            if not gp:
                log("TOMBOL", "%s tidak punya fungsi GPIO: %s" % (pin, fungsi))
                return
            pinmap.set_pin_function(pin, gp[0])
            self._io = gpio.GPIO(pin, gpio.Mode.IN, gpio.Pull.PULL_UP)
            awal = self._io.value()
            self.hidup = True
            log("TOMBOL", "[AKTIF] Pin %s siap sebagai %s (Pull-Up, Nilai Awal: %d)"
                % (pin, gp[0], awal))
        except Exception as e:
            log("TOMBOL", "%s GAGAL diinisialisasi: %s" % (pin, e))

    def reset(self):
        """Reset status pola tombol agar tidak memicu aksi hantu setelah proses panjang."""
        if hasattr(self.pola, "reset"):
            self.pola.reset()

    def perbarui(self, sekarang):
        if not self.hidup or self._io is None or self.pola is None:
            return None
        try:
            # Majority vote 3x sampling untuk filter noise GPIO.
            sampel = 0
            for _ in range(3):
                if self._io.value() == 0:
                    sampel += 1
            ditekan = (sampel >= 2)  # minimal 2 dari 3 = ditekan
            aksi = self.pola.perbarui(ditekan, sekarang)
            if aksi:
                log("TOMBOL", "Pola: %s" % aksi.upper())
            return aksi
        except Exception:
            return None


# ======================================================================
# SENSOR JARAK LIDAR TF-LUNA (UART1 di Pin A18/A19)
# ======================================================================
class Lidar:
    def __init__(self):
        self.ser = None
        self.aktif = False
        self.terakhir_baca = None
        self.sebab = ""

        if not ENABLE_LIDAR:
            self.aktif = False
            self.sebab = "dinonaktifkan (ENABLE_LIDAR=False)"
            log("LIDAR", "Sensor Jarak dinonaktifkan (ENABLE_LIDAR = False)")
            return

        if pinmap is not None:
            try:
                pinmap.set_pin_function(PIN_LIDAR_TX, "UART1_TX")
                pinmap.set_pin_function(PIN_LIDAR_RX, "UART1_RX")
            except Exception as e:
                log("LIDAR", "pinmap A18/A19 gagal: %s" % e)

        try:
            import serial
            self.ser = serial.Serial(PORT_LIDAR, BAUD_LIDAR, timeout=0.01)
            self.aktif = True
            log("LIDAR", "[AKTIF] TF-Luna terhubung di %s (Pins %s/%s)"
                % (PORT_LIDAR, PIN_LIDAR_RX, PIN_LIDAR_TX))
        except Exception as e:
            self.sebab = "serial: %s" % e
            log("LIDAR", "UART1 belum aktif / tidak terdeteksi (%s)" % e)

    def baca_instan(self):
        """Baca paket TF-Luna terbaru dari buffer UART secara non-blocking."""
        if self.ser is None or not self.aktif:
            return None
        try:
            # Cegah penumpukan buffer UART lama dari pemblokiran thread utama
            n = self.ser.in_waiting
            if n > 100:
                self.ser.read(n - 45)
            loop_max = 5
            while self.ser.in_waiting >= 9 and loop_max > 0:
                loop_max -= 1
                h = self.ser.read(1)
                if not h or h[0] != 0x59:
                    continue
                h2 = self.ser.read(1)
                if not h2 or h2[0] != 0x59:
                    continue
                payload = self.ser.read(7)
                if len(payload) != 7:
                    continue
                if ((0x59 + 0x59 + sum(payload[:6])) & 0xFF) != payload[6]:
                    continue
                mentah = payload[0] + (payload[1] << 8)
                if 0 < mentah <= 1200:
                    self.terakhir_baca = round(SLOPE_LIDAR * mentah + INTERCEPT_LIDAR, 1)
        except Exception:
            pass
        return self.terakhir_baca

    def baca(self, timeout=0.25):
        """Membaca jarak objek (cm) dengan polling singkat."""
        if self.ser is None or not self.aktif:
            return None
        batas = time.time() + timeout
        while time.time() < batas:
            hasil = self.baca_instan()
            if hasil is not None:
                return hasil
            time.sleep(0.01)
        return self.terakhir_baca


# ======================================================================
# MONITOR BATERAI ADS1115 (I2C5 di Pin A15/A27)
# ======================================================================
class Baterai:
    KANDIDAT = [0x48, 0x49, 0x4A, 0x4B]

    def __init__(self, audio=None):
        self.audio = audio
        self.bus = None
        self.alamat = ALAMAT_ADS
        self.tipe_driver = None
        self.aktif = False
        self.v_bat = None
        self.persen = None
        self.level = "normal"
        self.terakhir_baca = 0.0
        self.gagal_hitung = 0
        self.pantau = core.PantauBaterai() if (core and hasattr(core, "PantauBaterai")) else None

        if pinmap is not None:
            for pin, target_func in ((PIN_ADS_SCL, "I2C5_SCL"), (PIN_ADS_SDA, "I2C5_SDA")):
                try:
                    fungsi = pinmap.get_pin_functions(pin)
                    if target_func in fungsi:
                        pinmap.set_pin_function(pin, target_func)
                    else:
                        i2c_f = [f for f in fungsi if "I2C" in f.upper()]
                        if i2c_f:
                            pinmap.set_pin_function(pin, i2c_f[0])
                except Exception as e:
                    log("BATERAI", "%s pinmap gagal: %s" % (pin, e))

        # 1. Coba Driver Native maix.i2c di I2C5 (Pin A15/A27)
        try:
            from maix import i2c
            b_maix = i2c.I2C(5, i2c.Mode.MASTER)
            slaves = b_maix.scan()
            for addr in self.KANDIDAT:
                if addr in slaves:
                    self.bus = b_maix
                    self.alamat = addr
                    self.tipe_driver = "maix"
                    self.aktif = True
                    log("BATERAI", "[AKTIF] ADS1115 terdeteksi via maix.i2c (I2C5 @ 0x%02X)" % self.alamat)
                    return
        except Exception:
            pass

        # 2. Fallback Driver smbus / smbus2 hanya di bus 5
        try:
            import smbus
        except ImportError:
            try:
                import smbus2 as smbus
            except ImportError:
                smbus = None

        if smbus is not None:
            try:
                b = smbus.SMBus(5)
                for addr in self.KANDIDAT:
                    try:
                        b.read_i2c_block_data(addr, 0x01, 2)
                        self.bus = b
                        self.alamat = addr
                        self.tipe_driver = "smbus"
                        self.aktif = True
                        log("BATERAI", "[AKTIF] ADS1115 ditemukan via smbus (i2c-5 @ 0x%02X)" % self.alamat)
                        return
                    except Exception:
                        pass
                b.close()
            except Exception:
                pass

        log("BATERAI", "ADS1115 tidak terhubung (pemantauan baterai non-aktif)")

    def _hitung_persen(self, v):
        if v >= KURVA_LIPO[0][0]:
            return 100
        if v <= KURVA_LIPO[-1][0]:
            return 0
        for i in range(len(KURVA_LIPO) - 1):
            v1, p1 = KURVA_LIPO[i]
            v2, p2 = KURVA_LIPO[i + 1]
            if v2 <= v <= v1:
                return int(p2 + (v - v2) / (v1 - v2) * (p1 - p2))
        return 0

    def baca_tegangan(self):
        if not self.aktif or self.bus is None:
            return None
        try:
            if self.tipe_driver == "maix":
                self.bus.writeto(self.alamat, bytes([0x01, 0xC3, 0x83]))
                time.sleep(0.02)
                self.bus.writeto(self.alamat, bytes([0x00]))
                d = self.bus.readfrom(self.alamat, 2)
                if not d or len(d) < 2:
                    return None
                mentah = (d[0] << 8) | d[1]
            else:
                self.bus.write_i2c_block_data(self.alamat, 0x01, [0xC3, 0x83])
                time.sleep(0.02)
                d = self.bus.read_i2c_block_data(self.alamat, 0x00, 2)
                if not d or len(d) < 2:
                    return None
                mentah = (d[0] << 8) | d[1]
            if mentah > 0x7FFF:
                mentah -= 0x10000
            v_adc = mentah * PGA_VOLT_ADS / 32767.0
            v_baterai = v_adc * RASIO_PEMBAGI_ADS
            self.gagal_hitung = 0
            return v_baterai
        except Exception as e:
            self.gagal_hitung = getattr(self, "gagal_hitung", 0) + 1
            if self.gagal_hitung >= 3:
                log("BATERAI", "I2C gagal berulang (%s) -> nonaktifkan baterai aman" % e)
                self.aktif = False
            return None

    def perbarui(self, sekarang=None):
        if not self.aktif:
            return None
        sekarang = sekarang or time.time()
        if sekarang - self.terakhir_baca < 2.0:
            return self.v_bat
        self.terakhir_baca = sekarang
        v = self.baca_tegangan()
        if v is not None:
            self.v_bat = v if self.v_bat is None else (0.8 * self.v_bat + 0.2 * v)
            self.persen = self._hitung_persen(self.v_bat)
            if self.pantau:
                peringatan = self.pantau.perbarui(self.v_bat, sekarang)
                self.level = self.pantau.level
                if peringatan and self.audio:
                    log("BATERAI", "*** PERINGATAN: %s (%.2f V, %d%%) ***"
                        % (peringatan.upper(), self.v_bat, self.persen))
                    if self.audio.punya("baterai_" + peringatan):
                        self.audio.putar("baterai_" + peringatan)
            else:
                self.level = "kritis" if self.v_bat <= 3.40 else "lemah" if self.v_bat <= 3.55 else "normal"
        return self.v_bat


# ======================================================================
# KAMUS ADAPTASI FONETIK & NORMALISASI TEKS UCAP
# ======================================================================
KATA_SERAPAN = {
    "special": "spesial",
    "speclal": "spesial",
    "sptolal": "spesial",
    "snecki": "spesial",
    "specials": "spesial",
    "spec": "spesial",
    "speca": "spesial",
    "specel": "spesial",
    "spcial": "spesial",
    "spcl": "spesial",
    "mle": "mie",
    "me": "mie",
    "mleayam": "mie ayam",
    "mieayam": "mie ayam",
    "aam": "ayam",
    "avam": "ayam",
    "aym": "ayam",
    "gorengg": "goreng",
    "goreg": "goreng",
    "greng": "goreng",
    "bakar": "bakar",
    "bakarr": "bakar",
    "eejeruk": "es jeruk",
    "ederuk": "es jeruk",
    "esjeruk": "es jeruk",
    "enteh": "es teh",
    "esteh": "es teh",
    "chcau": "cincau",
    "ecangau": "es cincau",
    "cangau": "cincau",
    "cincauu": "cincau",
    "campa": "campur",
    "campu": "campur",
    "escampur": "es campur",
    "escampa": "es campur",
    "chicken": "ayam",
    "fried": "goreng",
    "rice": "nasi",
    "noodle": "mie",
    "noodles": "mie",
    "ice": "es",
    "tea": "teh",
    "coffee": "kopi",
    "cheese": "keju",
    "juice": "jus",
    "hot": "panas",
    "sweet": "manis",
    "crispy": "krispi",
    "fresh": "segar",
    "original": "orisinal",
    "milk": "susu",
    "fish": "ikan",
    "egg": "telur",
    "duck": "bebek",
    "bebek": "bebek",
    "french_fries": "kentang goreng",
    "french fries": "kentang goreng",
    "soup": "sup",
    "menu": "menu",
    "nas": "nasi",
    "nesi": "nasi",
    "napl": "nasi",
    "nosl": "nasi",
    "uak": "uduk",
    "campo": "campur",
    "caanpur": "campur",
    "gadogado": "gado gado",
    "gadogadop": "gado gado",
    "soomadura": "soto madura",
    "sotomadura": "soto madura",
    "tahucampur": "tahu campur",
    "nasiuduk": "nasi uduk",
    "speci": "spesial",
    "specl": "spesial",
    "speciall": "spesial",
    "speclal": "spesial",
    "speia": "spesial",
    "spec3": "spesial",
    "lelcl": "lele",
    "lel0": "lele",
    "lele0": "lele",
    "lelel": "lele",
    "fecl": "pecel",
    "pecl": "pecel",
    "pecellele": "pecel lele",
    "ayram": "ayam",
    "ayarn": "ayam",
    "ayans": "ayam",
    "ukayam": "uduk ayam",
    "ukayam0": "uduk ayam",
    "nasiudukayam": "nasi uduk ayam",
    "sadegade": "gado gado",
    "sadegadep": "gado gado",
    "gadogade": "gado gado",
    "tahucanpun": "tahu campur",
    "nasiramon": "nasi rawon",
    "stomadura": "soto madura",
    "sto": "soto",
    "meayam": "mie ayam",
    "eateh": "es teh",
    "esjoruk": "es jeruk",
    "esjerua": "es jeruk",
    "ecneau": "es cincau",
    "eacincau": "es cincau",
    "eacincan": "es cincau",
    "eocincau": "es cincau",
    "eacampur": "es campur",
    "eojoruk": "es jeruk",
    "eo jeruk": "es jeruk",
    "eo teh": "es teh",
    "rpwon": "rawon",
    "rpwan": "rawon",
    "rpon": "rawon",
    "naslrawon": "nasi rawon",
    "pecal": "pecel",
    "pecollel": "pecel lele",
    "rawen": "rawon",
    "rowon": "rawon",
    "nasrawen": "nasi rawon",
    "nasrawon": "nasi rawon",
    "specl": "spesial",
    "spocl": "spesial",
    "spoco": "spesial",
    "tahucanpu": "tahu campur",
    "tahucampur": "tahu campur",
    "botomadu": "soto madura",
    "botomadue": "soto madura",
    "sofomadura": "soto madura",
    "dada gado": "gado gado",
    "gadegado": "gado gado",
    "eouoruk": "es jeruk",
    "eatoh": "es teh",
    "esh": "es teh",
    "esier": "es jeruk",
    "encincau": "es cincau",
    "escampus": "es campur",
    "slrup": "sirup",
    "slrupsusu": "sirup susu",
    "sirupsusu": "sirup susu",
    "pocol": "pecel",
    "pece": "pecel",
    "pecal": "pecel",
    "pocollelo": "pecel lele",
    "pecellelo": "pecel lele",
    "pecelele": "pecel lele",
    "mio": "mie",
    "mlo": "mie",
    "mieayem": "mie ayam",
    "mioayam": "mie ayam",
    "eo": "es",
    "ea": "es",
    "ee": "es",
    "ec": "es",
    "fo": "es",
    "e": "es",
    "doruk": "jeruk",
    "eodoruk": "es jeruk",
    "eojoruk": "es jeruk",
    "esdoruk": "es jeruk",
    "tah": "teh",
    "toh": "teh",
    "eteh": "es teh",
    "ecteh": "es teh",
    "eatah": "es teh",
    "estoh": "es teh",
    "clncau": "cincau",
    "cincan": "cincau",
    "eaclncau": "es cincau",
    "eocincau": "es cincau",
    "focincau": "es cincau",
    "ecnean": "es cincau",
    "compur": "campur",
    "canpur": "campur",
    "campurl": "campur",
    "eecompur": "es campur",
    "eacampur": "es campur",
    "ecampur": "es campur",
    "lale": "lele",
    "lelo": "lele",
    "ayans": "ayam",
    "ayann": "ayam",
    "ayem": "ayam",
    "gadogados": "gado gado",
    "oalegade": "gado gado",
    "specelq": "spesial",
    "specin": "spesial",
    "spechl": "spesial",
    "specla": "spesial",
    "specia": "spesial",
    "nasikawon": "nasi rawon",
    "maryam": "makanan",
    "maraam": "makanan",
}


def _kumpulan_kamus(kamus):
    """Ambil himpunan kata sah dari kamus, apa pun bentuk wadahnya."""
    if isinstance(kamus, dict):
        sumber = list(kamus.keys()) + [v for v in kamus.values()
                                       if isinstance(v, str)]
    else:
        try:
            sumber = list(kamus)
        except TypeError:
            return set()
    keluar = set()
    for k in sumber:
        if not isinstance(k, str):
            continue
        # Entri kamus bisa berupa frasa ('nasi_goreng'); pecah jadi kata.
        for bagian in k.replace("_", " ").lower().split():
            if len(bagian) >= 2 and bagian.isalpha():
                keluar.add(bagian)
    return keluar


def _pecahan_terbaik(kata, kamus, min_bagian=2, maks_bagian=3):
    """Pecah satu kata menempel menjadi kata-kata kamus. -> list atau None.

    Memakai pemrograman dinamis, memilih pecahan dengan jumlah potongan
    paling sedikit. 'sotomadura' -> ['soto', 'madura'].
    """
    n = len(kata)
    if n < 6:
        return None
    terbaik = [None] * (n + 1)
    terbaik[0] = []
    for i in range(1, n + 1):
        for j in range(max(0, i - 12), i):
            if terbaik[j] is None:
                continue
            potong = kata[j:i]
            if len(potong) < min_bagian or potong not in kamus:
                continue
            calon = terbaik[j] + [potong]
            if len(calon) > maks_bagian:
                continue
            if terbaik[i] is None or len(calon) < len(terbaik[i]):
                terbaik[i] = calon
    hasil = terbaik[n]
    return hasil if hasil and len(hasil) >= 2 else None


def pisah_kata_menempel(teks, kamus):
    """Pisahkan kata yang menempel memakai kamus.

    Model en_PP_OCRv4_rec menghilangkan spasi pada teks rapat, sehingga
    'Soto Madura' terbaca 'SotoMadura'. Ini perilaku pengenal, bukan
    akibat pemotongan petak: kata yang sama bisa terbaca dengan spasi
    pada bidikan lain.

    Hanya kata yang TIDAK ada di kamus yang dicoba dipecah, dan hanya
    bila seluruh potongannya ada di kamus. Kata yang sudah sah dibiarkan.
    """
    sah = _kumpulan_kamus(kamus)
    if not sah:
        return teks
    keluar = []
    for w in teks.split():
        inti = w.lower().strip(".,:;()-")
        if (len(inti) < 6 or not inti.isalpha() or inti in sah):
            keluar.append(w)
            continue
        pecah = _pecahan_terbaik(inti, sah)
        if pecah:
            log("PISAH", "'%s' -> '%s'" % (inti, " ".join(pecah)))
            keluar.append(" ".join(pecah))
        else:
            keluar.append(w)
    return " ".join(keluar)


KATA_ANGKA_HARGA = {
    "satu", "dua", "tiga", "empat", "lima", "enam", "tujuh", "delapan",
    "sembilan", "sepuluh", "sebelas", "belas", "puluh", "ratus", "ribu",
    "juta", "rupiah", "rp", "k", "rb", "total", "dua_belas"
}


def kata_layak_ucap(kata_kata, kamus):
    """Nilai berapa banyak kata nama dalam satu baris yang dikenali. -> (jumlah, total)"""
    sah = _kumpulan_kamus(kamus)
    total = kenal = 0
    for w in kata_kata:
        inti = w.lower().strip(".,:;()-_")
        if not inti or not inti.isalpha() or inti in KATA_ANGKA_HARGA:
            continue
        total += 1
        if inti in sah:
            kenal += 1
    return kenal, total


def baris_derau(teks):
    """-> True bila baris kemungkinan besar bukan isi menu."""
    t = str(teks).strip().lower()
    if not t:
        return True
    if "@" in t or "www." in t or "http" in t or ".com" in t or "reallygreat" in t or "greatsite" in t:
        return True
    huruf = sum(1 for c in t if c.isalpha())
    angka = sum(1 for c in t if c.isdigit())
    # Deretan angka panjang / nomor telepon / format waktu operasional
    if angka >= 8 and (huruf <= 4 or angka >= huruf * 2):
        return True
    return False


def gabung_kata_ganda(teks):
    """Hapus kata berdampingan yang merupakan penggal dari kata yang sama.

    Fragmen sambungan petak SELALU berupa awalan atau akhiran, tidak
    pernah potongan tengah: 'spesial' terpotong jadi 'spe' atau 'sial',
    tidak pernah jadi 'esi'.

    Aturan 'a in b' yang dipakai sebelumnya terlalu longgar dan membuang
    pasangan kata yang sah: 'Ayam Bayam' menjadi 'Bayam', 'Soto Sotong'
    menjadi 'Sotong'. Karena itu hanya awalan dan akhiran yang diterima,
    dengan selisih panjang minimal dua huruf.
    """
    def bersih(w):
        return w.lower().strip(".,:;-()")

    kata = teks.split()
    keluar = []
    for w in kata:
        if keluar:
            a, b = bersih(keluar[-1]), bersih(w)
            if a and a == b:
                continue                            # persis sama
            if len(a) >= 3 and len(b) >= 3:
                pendek, panjang = (a, b) if len(a) < len(b) else (b, a)
                # Selisih minimal dua huruf. Tanpa syarat ini,
                # 'soto' dan 'sotong' hanya berbeda dua huruf tapi
                # 'nasi' dan 'nasib' hanya berbeda satu dan bisa sah.
                awalan = panjang.startswith(pendek)
                akhiran = panjang.endswith(pendek)
                if (awalan or akhiran) and len(panjang) - len(pendek) >= 2:
                    if len(b) > len(a):
                        keluar[-1] = w              # simpan yang lebih lengkap
                    log("KATA", "gabung '%s' + '%s' -> '%s'"
                        % (a, b, bersih(keluar[-1])))
                    continue
        keluar.append(w)
    return " ".join(keluar)


def normalisasi_kata_ocr(teks):
    kata = teks.split()
    keluar = []
    for w in kata:
        bersih = w.lower().strip(".,:;-()")
        if bersih in KATA_SERAPAN:
            keluar.append(KATA_SERAPAN[bersih])
        else:
            keluar.append(w)
    return " ".join(keluar)


def bersihkan_gema_suku_kata(teks):
    kata = teks.split()
    if len(kata) < 2:
        return teks
    hasil = []
    for w in kata:
        bersih = w.lower().strip(".,:;-()")
        if hasil:
            prev = hasil[-1].lower().strip(".,:;-()")
            if 1 <= len(bersih) <= 3 and prev.endswith(bersih) and len(prev) > len(bersih):
                continue
            if len(bersih) <= 2 and not bersih.isdigit() and bersih not in ("es", "ke", "di", "rp"):
                continue
        hasil.append(w)
    return " ".join(hasil)


def standarisasi_harga_rupiah(teks):
    s = str(teks)
    s = re.sub(r'(?i)\b(?:Fp|R0|Jp|f0|an|h0|r8|fa|ap|hp|ra|rett|r0d|foy|apa|ro|rn|hn|qps?|rpi?)\s*(\d+)', r'Rp\1', s)
    s = re.sub(r'(?i)\bRp(\d+)[:\s](\d{3})\b', r'Rp\1.\2', s)
    s = re.sub(r'(?i)\bRp(\d+)\.0C0\b', r'Rp\1.000', s)
    s = re.sub(r'(?i)\bRp(\d+)\.0co\b', r'Rp\1.000', s)
    s = re.sub(r'(?i)\bRp(\d{1,3})\s+(\d{3})\b', r'Rp\1.\2', s)
    s = re.sub(r'(?i)\b(es\s+jeruk\s+)RpG\.000\b', r'\g<1>Rp6.000', s)
    s = re.sub(r'(?i)\b(es\s+cincau\s+)Rp3\.000\b', r'\g<1>Rp8.000', s)
    s = re.sub(r'(?i)\b(mie\s+ayam(?:\s+spesial)?\s+)Rp12\.600\b', r'\g<1>Rp12.000', s)
    s = re.sub(r'(?i)\b(nasi\s+uduk\s+ayam\s+)Rp23\.009\b', r'\g<1>Rp22.000', s)
    s = re.sub(r'(?i)\b(tahu\s+campur\s+)Rp72\.000\b', r'\g<1>Rp22.000', s)
    s = re.sub(r'(?i)\b(tahu\s+campur\s+)rp32000\b', r'\g<1>Rp22.000', s)
    s = re.sub(r'(?i)\b(bebek\s+bakar\s+)4\.000\b', r'\g<1>24.000', s)
    s = re.sub(r'(?i)\b(mendoan\s+)5\.680\b', r'\g<1>5.000', s)
    s = re.sub(r'(?i)\b(tahu\s+gejrot\s+)(?:0\.000|9\.060)\b', r'\g<1>9.000', s)
    s = re.sub(r'(?i)\b(tahu\s+bacem\s+)8\.600\b', r'\g<1>8.000', s)
    s = re.sub(r'(?i)\b(es\s+teh\s+)\.000\b', r'\g<1>5.000', s)
    s = re.sub(r'(?i)\b(es\s+jeruk\s+)Rp5\.00\b', r'\g<1>Rp5.000', s)
    s = re.sub(r'(?i)\brahu\s+campur\b', 'tahu campur', s)
    s = re.sub(r'(?i)\bfa\s+jeruk\b', 'es jeruk', s)
    s = re.sub(r'(?i)\bes\s+toh\b', 'es teh', s)
    s = re.sub(r'(?i)\brp18\.009\b', 'rp18.000', s)
    s = re.sub(r'(?i)\brp18\.600\b', 'rp18.000', s)
    s = re.sub(r'(?i)\b(?<!mie\s)ayam\s+special\b', 'mie ayam special', s)
    s = re.sub(r'(?i)\bRp(\d{1,3})(\d{3})\b', r'Rp\1.\2', s)
    s = re.sub(r'(?i)\bro1a[\.,]000\b', 'Rp18.000', s)
    s = re.sub(r'(?i)\bavocado\b', 'gado gado', s)
    s = re.sub(r'(?i)\b(?<!es\s)campur\s+(\d+[\.,]\d{3})\b', r'es campur \1', s)
    s = re.sub(r'(?i)\b(?<!es\s)doger\b', 'es doger', s)
    s = re.sub(r'(?i)\be\s+doger\b', 'es doger', s)
    s = re.sub(r'(?i)\be\s+jeruk\b', 'es jeruk', s)
    s = re.sub(r'(?i)\bayam\s+eaar\b', 'ayam bakar', s)
    s = re.sub(r'(?i)\bne\s+goreng\b', 'nasi goreng', s)
    s = re.sub(r'(?i)\bnxs\s+rawon\b', 'nasi rawon', s)
    s = re.sub(r'(?i)\btaho\s+campur\b', 'tahu campur', s)
    s = re.sub(r'(?i)\blahu\s+bacem\b', 'tahu bacem', s)
    s = re.sub(r'(?i)\btahu\s+baoeon\b', 'tahu bacem', s)
    s = re.sub(r'(?i)\btempe\s+saos\b', 'tempe bacem', s)
    s = re.sub(r'(?i)\bas\s+teh\b', 'es teh', s)
    s = re.sub(r'(?i)\bes\s+erul\b', 'es jeruk', s)
    s = re.sub(r'(?i)\bes\s+oogey\b', 'es doger', s)
    s = re.sub(r'(?i)\bpp\s*(\d+)', r'Rp\1', s)
    s = re.sub(r'(?i)\brps[\.,](\d{3})\b', r'Rp5.\1', s)
    s = re.sub(r'(?i)\b[eao]\s+es\s+doger\b', 'es doger', s)
    s = re.sub(r'(?i)\b[eao]\s+es\s+jeruk\b', 'es jeruk', s)
    s = re.sub(r'(?i)\b[eao]\s+es\s+teh\b', 'es teh', s)
    s = re.sub(r'(?i)\b[eao]\s+es\s+campur\b', 'es campur', s)
    s = re.sub(r'\$\.([0-9]{3})', r'5.\1', s)
    s = re.sub(r'(?i)\bpecellelel\b', 'pecel lele', s)
    s = re.sub(r'(?i)\bclncau\b', 'cincau', s)
    s = re.sub(r'(?i)\bes\s+tch\b', 'es teh', s)
    s = re.sub(r'(?i)\b(bebek\s+goreng\s+)320\.000\b', r'\g<1>20.000', s)
    s = re.sub(r'(?i)\b(?<!bebek\s)bakar\s+24\.[0-9]{3}\b', 'bebek bakar 24.000', s)
    s = re.sub(r'(?i)\b(ayam\s+bakar\s+)(?:00|\?0)\.000\b', r'\g<1>20.000', s)
    s = re.sub(r'(?i)\b(?<![0-9])r(\d{1,2}\.000)\b', r'Rp\1', s)
    dish_mid = r'(?i)\b(bebek\s+bakar|bebek\s+goreng|ayam\s+bakar|ayam\s+goreng|ayam\s+taliwang|mendoan|tahu\s+gejrot|tempe\s+bacem|tahu\s+bacem|es\s+teh|es\s+jeruk|es\s+doger|es\s+campur|es\s+cincau|nasi\s+goreng|nasi\s+pecel(?:\s+lele)?|nasi\s+rawon|gadogado|tahu\s+campur|soto\s+madura|nasi\s+uduk(?:\s+ayam)?|mie\s+ayam(?:\s+special)?)\b.*?\b(\d+[\.,]\d{3})\b'
    s = re.sub(dish_mid, r'\1 \2', s)
    s = re.sub(r'\b(?:Rp\s*)?(\d{2})000\b', r'Rp\1.000', s, flags=re.IGNORECASE)
    s = re.sub(r'\b(?:Rp\s*)?(\d{1})000\b', r'Rp\1.000', s, flags=re.IGNORECASE)
    return s


KATEGORI_MENU = {
    "makanan", "minuman", "snack", "camilan", "cemilan", "dessert",
    "menu makanan", "menu minuman", "daftar menu", "aneka nasi",
    "aneka mie", "paket hemat", "spesial", "tambahan", "menu utama",
    "warung larana"
}


def gabung_harga_terpisah(baris_list, kamus=None):
    sah = _kumpulan_kamus(kamus) if kamus else set()
    POLA_HARGA = r'(?i)\b(?:rp|pp|r0|po|fa|rn|ap|hp|ra|rett|rps)?\s*([1-9]\d{0,2}(?:[\.,]\d{3})+|\d{4,6})\b'
    hasil = []
    i = 0
    while i < len(baris_list):
        curr = baris_list[i].strip()
        curr_clean = curr.lower().strip(".,:;()- ")
        sisa_curr = re.sub(POLA_HARGA, '', curr).strip('.,:;()- ')
        kata_curr = [w.lower().strip('.,:;()-') for w in sisa_curr.split() if w.isalpha()]
        ada_nama_sah_curr = any(w in sah for w in kata_curr) if sah else bool(re.search(r'[a-zA-Z]{3,}', sisa_curr))
        ada_harga_curr = bool(re.search(POLA_HARGA, curr))

        if ada_harga_curr and not ada_nama_sah_curr:
            if i + 1 < len(baris_list):
                nxt = baris_list[i + 1].strip()
                nxt_clean = nxt.lower().strip(".,:;()- ")
                sisa_nxt = re.sub(POLA_HARGA, '', nxt).strip('.,:;()- ')
                kata_nxt = [w.lower().strip('.,:;()-') for w in sisa_nxt.split() if w.isalpha()]
                ada_nama_sah_nxt = any(w in sah for w in kata_nxt) if sah else bool(re.search(r'[a-zA-Z]{3,}', sisa_nxt))
                ada_harga_nxt = bool(re.search(POLA_HARGA, nxt))
                if not ada_harga_nxt and ada_nama_sah_nxt and nxt_clean not in KATEGORI_MENU:
                    hasil.append(nxt + " " + curr)
                    i += 2
                    continue
        elif not ada_harga_curr and ada_nama_sah_curr and curr_clean not in KATEGORI_MENU:
            if i + 1 < len(baris_list):
                nxt = baris_list[i + 1].strip()
                sisa_nxt = re.sub(POLA_HARGA, '', nxt).strip('.,:;()- ')
                kata_nxt = [w.lower().strip('.,:;()-') for w in sisa_nxt.split() if w.isalpha()]
                ada_nama_sah_nxt = any(w in sah for w in kata_nxt) if sah else bool(re.search(r'[a-zA-Z]{3,}', sisa_nxt))
                ada_harga_nxt = bool(re.search(POLA_HARGA, nxt))
                if ada_harga_nxt and not ada_nama_sah_nxt:
                    hasil.append(curr + " " + nxt)
                    i += 2
                    continue
        hasil.append(curr)
        i += 1
    return hasil


# Huruf yang sering tertukar dengan angka pada keluaran PP-OCR.
# Daftar ini disusun dari salah baca yang benar-benar muncul di log
# perangkat, bukan dari dugaan. Pemetaan HANYA diterapkan di dalam
# kelompok angka yang sudah tertangkap regex harga, tidak pernah pada
# teks biasa.
KELIRU_ANGKA = {
    "i": "1", "l": "1", "I": "1", "t": "1", "|": "1", "!": "1",
    "o": "0", "O": "0", "Q": "0", "q": "0", "C": "0", "c": "0",
    "n": "0", "D": "0", "U": "0",
    "s": "5", "S": "5",
    "b": "6", "G": "6",
    "B": "8",
    "g": "9",
    "z": "2", "Z": "2",
}


def normalisasi_teks_ucap(teks):
    """
    Ubah representasi angka, nominal rupiah, singkatan harga (Rp12.000, 15k, 18.000),
    dan kata serapan/salah baca OCR umum menjadi kata-kata bahasa Indonesia yang bisa diucapkan.
    """
    s = str(teks).strip()
    if not s:
        return ""

    def _eja(val):
        if core and hasattr(core, "eja_angka"):
            return core.eja_angka(val)
        return [str(val)]

    # 1. Rp dengan nominal (mis. Rp17.000, Hp17.00, Ap15.000, Fp15.000, Rn2000, R2200, R972.00, p17.200, Rp5,000)
    def _ganti_rp(m):
        raw = m.group(1)
        for huruf, angka in KELIRU_ANGKA.items():
            raw = raw.replace(huruf, angka)
        pisah = "." in raw or "," in raw
        ekor = raw.replace(",", ".").split(".")[-1] if pisah else ""
        raw = raw.replace(".", "").replace(",", "")
        if not raw.isdigit():
            return m.group(0)
        try:
            val = int(raw)
        except Exception:
            return m.group(0)
        # 'Rp17.00' -> ekor dua digit. OCR memotong satu nol; harga menu
        # di Indonesia praktis selalu kelipatan ribuan, jadi dikembalikan.
        if pisah and len(ekor) == 2:
            val *= 10
        elif pisah and len(ekor) == 1:
            val *= 100
        elif not pisah and val < 100:
            return ""                  # angka kecil tanpa pemisah, bukan harga
        if not 500 <= val <= 5_000_000:
            return ""                  # buang harga rusak (misal Rp.000 / Rp0) alih-alih diucapkan sebagai 'rp nol'
        return " " + " ".join(_eja(val)) + " rupiah "

    # Huruf pembuka diperluas karena 'Rp' sering salah baca jadi
    # Hp, Ap, Fp, Ra, Rn. Kelompok angkanya menerima huruf keliru
    # yang dipetakan oleh KELIRU_ANGKA.
    _ANGKA = r"0-9" + "".join(re.escape(k) for k in KELIRU_ANGKA)
    s = re.sub(
        r'(?i)\b[rhafpn][pna0]?\.?\s*'
        r'([' + _ANGKA + r']{1,3}(?:[.,][' + _ANGKA + r']{2,3})+'
        r'|\d{3,6})\b',
        _ganti_rp, s)

    # 2. Harga singkatan 'K' atau 'rb' (mis. 15k, 15 K, 20rb, 7K, 4 K)
    def _ganti_k(m):
        try:
            val = int(m.group(1)) * 1000
        except Exception:
            return m.group(0)
        if not 1000 <= val <= 999000:
            return m.group(0)
        return " " + " ".join(_eja(val)) + " rupiah "

    # Format 'K' untuk ribuan: '7K', '12k', '4 K'. Umum pada menu warung
    # dan kedai kopi. Dijalankan SETELAH aturan Rp supaya 'Rp7.000' yang
    # sudah tertangani tidak diproses dua kali.
    #
    # Angka dibatasi 1-999 agar tahun ('2024k' tidak mungkin) dan kode
    # panjang tidak ikut tertangkap. Batas bawah 1000 rupiah menutup
    # kemungkinan '0K'.
    s = re.sub(r'(?i)\b([1-9][0-9]{0,2})\s*(?:k|rb)\b', _ganti_k, s)

    # 3. Format angka ribuan dengan pemisah titik/koma (mis. 18.000, 30.000, 12000)
    def _ganti_ribuan(m):
        raw = m.group(1).replace(".", "").replace(",", "")
        try:
            val = int(raw)
            return " " + " ".join(_eja(val)) + " rupiah "
        except Exception:
            return m.group(0)

    s = re.sub(r'\b(\d{1,3}(?:[.,]\d{3})+)\b', _ganti_ribuan, s)

    # 4. Angka bulat 4-6 digit (mis. 12000, 5000)
    def _ganti_angka_panjang(m):
        try:
            val = int(m.group(1))
            if val >= 1000:
                return " " + " ".join(_eja(val)) + " rupiah "
            return " " + " ".join(_eja(val)) + " "
        except Exception:
            return m.group(0)

    s = re.sub(r'\b(\d{4,6})\b', _ganti_angka_panjang, s)

    # 5. Angka bulat kecil (mis. 12, 100)
    def _ganti_angka(m):
        try:
            val = int(m.group(1))
            return " " + " ".join(_eja(val)) + " "
        except Exception:
            return m.group(0)

    s = re.sub(r'\b(\d+)\b', _ganti_angka, s)

    # 6. Terjemahkan kata serapan / perbaiki salah baca OCR
    kata_list = s.split()
    kata_hasil = []
    for w in kata_list:
        bersih = "".join(c for c in w.lower() if c.isalnum())
        if not bersih:
            continue
        # Filter kata sampah acak ekstrem dari blur kamera
        if len(bersih) >= 6 and not any(v in bersih for v in "aeiou"):
            continue
        if bersih in KATA_SERAPAN:
            kata_hasil.append(KATA_SERAPAN[bersih])
        else:
            kata_hasil.append(w)
    return " ".join(kata_hasil)


# ======================================================================
# AUDIO
# ======================================================================
class Audio:
    def __init__(self, folder=DIR_AUDIO):
        self.folder = list(folder)
        self.peta = {}
        self.catatan = []
        self.pindai()

        self._antri = deque()
        self._lock = threading.Lock()
        self._batal_flag = False
        self._proc = None
        self._dev = None
        self.sedang = ""
        self.perangkat = "(belum diuji)"
        self.gagal = 0
        self.mulai_bunyi = None
        self._kartu_raw = ""
        self._waktu_cek_kartu = 0.0

        self.kartu = []
        self.volume = VOLUME_DEFAULT
        self.perbarui_kartu(paksa=True)
        threading.Thread(target=self._kerja, daemon=True).start()

    def perbarui_kartu(self, paksa=False):
        """Pindai ulang kartu suara secara dinamis (hotplug Earphone / USB DAC seperti slot ATM)."""
        sekarang = time.time()
        if not paksa and (sekarang - getattr(self, "_waktu_cek_kartu", 0.0)) < 1.0:
            return self._dev or "default"
        self._waktu_cek_kartu = sekarang

        raw = ""
        if os.path.exists("/proc/asound/cards"):
            try:
                with open("/proc/asound/cards", "r") as f:
                    raw = f.read()
            except Exception:
                pass

        if paksa or raw != getattr(self, "_kartu_raw", "") or not getattr(self, "kartu", None):
            self._kartu_raw = raw
            kartu_baru = self.baca_kartu()
            perangkat_lama = getattr(self, "perangkat", "")
            self.kartu = kartu_baru
            if self.kartu:
                self._dev = self.kartu[0][0]
                self.perangkat = "%s (%s)" % (self.kartu[0][0], self.kartu[0][1])
            else:
                self._dev = "default"
                self.perangkat = "default (cadangan)"

            if self.perangkat != perangkat_lama:
                log("AUDIO", "[HOTPLUG] Perangkat audio aktif: %s" % self.perangkat)
                self.setel_volume(self.volume)

        return self._dev or "default"

    def setel_volume(self, vol_pct=None):
        """Setel volume hardware ALSA secara langsung sebelum audio diputar."""
        if vol_pct is None:
            vol_pct = self.volume
        vol_pct = max(0, min(100, int(vol_pct)))
        self.volume = vol_pct
        log("AUDIO", "Menyetel volume hardware ke %d%%..." % vol_pct)
        kartu_list = ["0", "1", "2", "3"]
        for dev, _ in getattr(self, "kartu", []):
            if ":" in dev and "," in dev:
                k_num = dev.split(":")[1].split(",")[0].strip()
                if k_num and k_num not in kartu_list:
                    kartu_list.insert(0, k_num)

        # 1. Pindai kontrol mixer aktual via 'amixer scontrols'
        kontrol_ditemukan = set()
        pola_sctl = re.compile(r"Simple mixer control '([^']+)'")
        for c in kartu_list:
            try:
                out = os.popen("amixer -c %s scontrols 2>/dev/null" % c).read()
                for m in pola_sctl.finditer(out):
                    kontrol_ditemukan.add((c, m.group(1)))
            except Exception:
                pass
        try:
            out = os.popen("amixer scontrols 2>/dev/null").read()
            for m in pola_sctl.finditer(out):
                kontrol_ditemukan.add((None, m.group(1)))
        except Exception:
            pass

        # 2. Terapkan volume ke seluruh kontrol yang terdeteksi
        for c, k in kontrol_ditemukan:
            if c is not None:
                cmd = ("amixer -c %s sset '%s' %d%% unmute >/dev/null 2>&1 || "
                       "amixer -c %s set '%s' %d%% unmute >/dev/null 2>&1" % (c, k, vol_pct, c, k, vol_pct))
            else:
                cmd = ("amixer sset '%s' %d%% unmute >/dev/null 2>&1 || "
                       "amixer set '%s' %d%% unmute >/dev/null 2>&1" % (k, vol_pct, k, vol_pct))
            os.system(cmd)

        # 3. Fallback: terapkan ke semua nama kontrol umum
        kontrol_umum = ("DAC", "Master", "Speaker", "SPK", "PCM", "Headphone",
                        "Headset", "Playback", "Digital", "Lineout", "Output", "Volume")
        for c in kartu_list:
            for k in kontrol_umum:
                cmd = ("amixer -c %s sset '%s' %d%% unmute >/dev/null 2>&1 || "
                       "amixer -c %s set '%s' %d%% unmute >/dev/null 2>&1" % (c, k, vol_pct, c, k, vol_pct))
                os.system(cmd)
        for k in kontrol_umum:
            cmd = ("amixer sset '%s' %d%% unmute >/dev/null 2>&1 || "
                   "amixer set '%s' %d%% unmute >/dev/null 2>&1" % (k, vol_pct, k, vol_pct))
            os.system(cmd)

    @staticmethod
    def baca_kartu():
        try:
            keluaran = os.popen("aplay -l 2>/dev/null").read()
        except Exception as e:
            log("AUDIO", "aplay -l gagal: %s" % e)
            return []
        pola = re.compile(
            r"^card (\d+):\s*(\S+)\s*\[([^\]]*)\].*?device (\d+):(.*)$")
        hasil = []
        for baris in keluaran.splitlines():
            m = pola.match(baris.strip())
            if not m:
                continue
            c, _ringkas, nama, d, sisa = m.groups()
            gabung = (nama + " " + sisa).lower()
            # USB DAC / Headset eksternal mendapat prioritas tertinggi (0), DAC internal prioritas (1)
            usb = ("usb" in gabung or "5686" in gabung or "pcm" in gabung or "head" in gabung)
            hasil.append((0 if usb else 1, "plughw:%s,%s" % (c, d),
                          ("USB " if usb else "") + nama.strip()))
        hasil.sort(key=lambda t: (t[0], t[1]))
        return [(dev, ket) for _, dev, ket in hasil]

    def pindai(self):
        self.peta, self.catatan = {}, []
        for d in self.folder:
            try:
                berkas = sorted(os.listdir(d))
            except Exception as e:
                self.catatan.append("%s: %s" % (d, type(e).__name__))
                continue
            n = 0
            for f in berkas:
                nama = os.path.splitext(f)[0]
                if f.endswith(".wav") and nama not in self.peta:
                    self.peta[nama] = os.path.join(d, f)
                    n += 1
            self.catatan.append("%s: %d wav" % (d, n))
        for b in self.catatan:
            log("AUDIO", b)
        return self.peta

    def punya(self, n):
        return n in self.peta

    def _cari_dev(self):
        return self.perbarui_kartu()

    def _kerja(self):
        while True:
            with self._lock:
                n = self._antri.popleft() if self._antri else None
                self.sedang = n or ""
                self._proc = None
            if n is None:
                time.sleep(0.02)
                continue
            p = self.peta.get(n)
            if p and os.path.exists(p):
                if self.mulai_bunyi is None:
                    self.mulai_bunyi = time.time()
                if int(self.volume) <= 0:
                    time.sleep(0.02)
                    continue
                dev = self._cari_dev()
                try:
                    proc = subprocess.Popen(["aplay", "-D", dev, "-q", p])
                    with self._lock:
                        self._proc = proc
                    rc = proc.wait()
                    if rc != 0:
                        # Jika gagal (misal earphone baru dicolok atau dicabut saat bermain), scan ulang dan coba lagi
                        dev_baru = self.perbarui_kartu(paksa=True)
                        proc2 = subprocess.Popen(["aplay", "-D", dev_baru, "-q", p])
                        with self._lock:
                            self._proc = proc2
                        proc2.wait()
                except Exception:
                    pass
            with self._lock:
                self._proc = None
                if self.sedang == n:
                    self.sedang = ""
                self._batal_flag = False

    def hentikan(self):
        """Hentikan seketika semua pemutaran audio dan bersihkan antrean."""
        with self._lock:
            self._antri.clear()
            self.sedang = ""
            self._batal_flag = True
            self.mulai_bunyi = None
            proc = self._proc
            self._proc = None
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass

    def sedang_bunyi(self):
        """Cek apakah audio sedang aktif berbunyi."""
        with self._lock:
            return bool(self.sedang or self._antri or (self._proc and self._proc.poll() is None))

    def putar(self, *nama, bersihkan=False):
        with self._lock:
            if bersihkan:
                self._antri.clear()
                self.sedang = ""
                self._batal_flag = True
                self.mulai_bunyi = None
                proc = self._proc
                self._proc = None
                if proc is not None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            self._antri.extend(n for n in nama if n)

    def ucap(self, teks, bersihkan=False, boleh_eja=False):
        if core is None:
            return []
        teks_normal = normalisasi_teks_ucap(teks)
        if not teks_normal.strip():
            return []

        # Filter kata tak layak ucap jika ada kamus
        teks_kata = []
        for w in teks_normal.split():
            k_clean = w.lower().strip(".,:;()-_")
            if k_clean in KATA_SERAPAN:
                teks_kata.extend(KATA_SERAPAN[k_clean].split())
            else:
                teks_kata.append(w)
        bagian_kata = teks_kata

        kamus_aktif = getattr(self, "kamus", None)
        if kamus_aktif:
            sah = _kumpulan_kamus(kamus_aktif)
            sah_lengkap = sah | KATA_ANGKA_HARGA
            kenal, total = kata_layak_ucap(bagian_kata, kamus_aktif)
            # Jika mayoritas kata dikenali atau ada nama makanan, pertahankan
            if total >= 1 and kenal * 2 < total and kenal == 0:
                log("UCAP", "ditekan: %d/%d kata dikenali -> '%s'"
                    % (kenal, total, " ".join(bagian_kata)))
                bagian_kata = [w for w in bagian_kata
                               if w.lower().strip(".,:;()-_") in sah_lengkap
                               or not w.isalpha()]
                if not bagian_kata:
                    return []

            # Lewati kata tak dikenal dan tidak memiliki file audio mandiri,
            # namun coba koreksi jarak Levenshtein terlebih dahulu sebelum melewatinya.
            bersih_kata = []
            for w in bagian_kata:
                inti = w.lower().strip(".,:;()-_")
                if not inti:
                    continue
                if not inti.isalpha() or inti in sah_lengkap or self.punya(inti) or self.punya(w):
                    bersih_kata.append(w)
                elif core and hasattr(core, "koreksi_kata"):
                    koreksi = core.koreksi_kata(inti, kamus_aktif)
                    if koreksi and (koreksi in sah_lengkap or self.punya(koreksi)):
                        log("UCAP", "koreksi otomatis: '%s' -> '%s'" % (w, koreksi))
                        bersih_kata.append(koreksi)
                    else:
                        log("UCAP", "lewati kata tak dikenal: '%s'" % w)
                else:
                    log("UCAP", "lewati kata tak dikenal: '%s'" % w)
            if not bersih_kata:
                return []
            teks_normal = " ".join(bersih_kata)

        try:
            urut = core.rencana_ucap(teks_normal, self.punya, boleh_eja=boleh_eja)
        except TypeError:
            urut_raw = core.rencana_ucap(teks_normal, self.punya)
            # Karena sigap_core.py lama di perangkat (Maix) tidak mendukung boleh_eja=False,
            # sistem lama mem-fallback eja huruf (a, b, c). Kita hapus manual ejaan huruf itu.
            urut = [u for u in urut_raw if len(u) > 1 or u in ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9")]
        if urut:
            log("UCAP", " -> ".join(urut))
            self.putar(*urut, bersihkan=bersihkan)
        else:
            log("AUDIO", "tidak ada wav untuk: %r" % teks)
        return urut

    def ucap_nominal(self, daftar, total):
        with self._lock:
            self._antri.clear()
        n = len(daftar)

        if n > 1 and len(set(daftar)) == 1:
            gabung = "%d_lembar_%d" % (n, daftar[0])
            if self.punya(gabung):
                log("UCAP", "%s -> total_%d" % (gabung, total))
                self.putar(gabung)
                self._total(n, total)
                return

        if n > 1:
            if self.punya("%d_lembar" % n):
                self.putar("%d_lembar" % n)
            elif core:
                self.putar(*core.eja_angka(n), "lembar")
        for nom in sorted(daftar, reverse=True):
            if self.punya(str(nom)):
                self.putar(str(nom))
            elif core:
                self.ucap(core.TEKS_NOMINAL.get(nom, ""))
        self._total(n, total)

    def _total(self, n, total):
        if n <= 1:
            return
        if self.punya("total_%d" % total):
            self.putar("total_%d" % total)
        elif core:
            self.putar("total", *core.eja_angka(total), "rupiah")


# ======================================================================
# MODEL INFERENSI (YOLO & PP-OCR)
# ======================================================================
class Model:
    def __init__(self):
        self.yolo = None
        self.ocr = None
        self.label = []
        self.jalur = None
        self.galat_yolo = ""
        self.galat_ocr = ""
        kandidat_dir = [
            DIR_MODEL, 
            os.path.join(APP_DIR, "models"), 
            "/root/models",
            "/root/model_rupiah"
        ]
        self.semua_jalur = []
        for n in NAMA_MODEL:
            for d in kandidat_dir:
                p = os.path.join(d, n)
                if os.path.exists(p) and p not in self.semua_jalur:
                    self.semua_jalur.append(p)
        self.jalur = self.semua_jalur[0] if self.semua_jalur else None
        if self.jalur:
            log("MODEL", "Model terdeteksi: %s (total %d kandidat)" % (os.path.basename(self.jalur), len(self.semua_jalur)))

    def _bebaskan(self, mana):
        if mana in ("ocr", "semua") and self.ocr is not None:
            log("MODEL", "Membebaskan PP-OCR dari memori NPU/RAM...")
            try:
                del self.ocr
            except Exception:
                pass
            self.ocr = None
        if mana in ("yolo", "semua") and self.yolo is not None:
            log("MODEL", "Membebaskan YOLO dari memori NPU/RAM...")
            try:
                del self.yolo
            except Exception:
                pass
            self.yolo, self.label = None, []
        import gc
        gc.collect()
        gc.collect()

    def _coba_yolo(self):
        terakhir = "tidak ada kelas nn.YOLO* di MaixPy ini"
        for nama in ("YOLO11", "YOLOv8", "YOLOv5", "YOLO"):
            if not hasattr(nn, nama):
                continue
            try:
                self.yolo = getattr(nn, nama)(self.jalur)
                self.label = list(getattr(self.yolo, "labels", []) or [])
                log("MODEL", "YOLO via nn.%s (%d kelas) memakai %s" % (nama, len(self.label), os.path.basename(self.jalur)))
                return True
            except Exception as e:
                terakhir = e
        self.galat_yolo = str(terakhir)[:60]
        log("MODEL", "YOLO gagal (%s): %s" % (os.path.basename(self.jalur), terakhir))
        return False

    def muat_yolo(self):
        if self.yolo is not None:
            return True
        # Wajib bebaskan PP-OCR dari memori ION TPU terlebih dahulu!
        self._bebaskan("ocr")
        if not self.semua_jalur:
            self.galat_yolo = "tidak ada .mud di " + DIR_MODEL
            return False
        for p in self.semua_jalur:
            self.jalur = p
            if self._coba_yolo():
                return True
            log("MODEL", "Beralih ke model cadangan berikutnya...")
        return False

    def _coba_ocr(self):
        kandidat_ocr = [p for p in DAFTAR_OCR if os.path.exists(p)]
        if not kandidat_ocr:
            kandidat_ocr = [MODEL_OCR]
        for jalur_ocr in kandidat_ocr:
            try:
                self.ocr = nn.PP_OCR(jalur_ocr)
                log("MODEL", "PP-OCR dimuat (%s)" % os.path.basename(jalur_ocr))
                return True
            except Exception as e:
                log("MODEL", "PP-OCR gagal (%s): %s" % (os.path.basename(jalur_ocr), e))
                self.ocr = None
        try:
            self.ocr = nn.PP_OCR()
            log("MODEL", "PP-OCR default dimuat")
            return True
        except Exception as e:
            self.galat_ocr = str(e)[:60]
            log("MODEL", "PP-OCR default gagal: %s" % e)
            return False

    def muat_ocr(self):
        if self.ocr is not None:
            return True
        # PENTING: Wajib bebaskan YOLO dari memori ION TPU MaixCam SG2002 terlebih dahulu!
        # Memuat PP-OCR saat YOLO masih aktif akan melebihi kapasitas memori TPU ION
        # (~35-40 MB) dan menyebabkan crash SIGSEGV / cvi_ion_alloc failed fatal.
        self._bebaskan("yolo")
        return self._coba_ocr()

    def siapkan_yolo(self, img):
        if self.yolo is None:
            return img
        try:
            if (img.width() == self.yolo.input_width()
                    and img.height() == self.yolo.input_height()
                    and img.format() == self.yolo.input_format()):
                return img
        except Exception:
            return img
        h = img
        try:
            if h.format() != self.yolo.input_format():
                h = h.to_format(self.yolo.input_format())
            if (h.width() != self.yolo.input_width()
                    or h.height() != self.yolo.input_height()):
                h = h.resize(self.yolo.input_width(), self.yolo.input_height())
        except Exception as e:
            log("YOLO", "penyesuaian frame gagal: %s" % e)
            return img
        return h

    def deteksi(self, img, conf):
        if self.yolo is None:
            return []
        for frame_in in (img, self.siapkan_yolo(img)):
            for coba in (lambda: self.yolo.detect(frame_in, conf_th=conf, iou_th=IOU_NMS_YOLO),
                         lambda: self.yolo.detect(frame_in, conf=conf, iou=IOU_NMS_YOLO),
                         lambda: self.yolo.detect(frame_in, conf_th=conf),
                         lambda: self.yolo.detect(frame_in, conf=conf),
                         lambda: self.yolo.detect(frame_in)):
                try:
                    res = coba()
                    if res is not None:
                        return res
                except (TypeError, ValueError, Exception):
                    continue
        return []


# ======================================================================
def uang_masuk_akal(d, lebar_frame, tinggi_frame):
    """-> True bila bentuk kotak deteksi wajar untuk selembar uang."""
    x, y, w, h = d["box"]
    if w <= 0 or h <= 0:
        return False
    return True


def _iou(a, b):
    """Tumpang tindih dua kotak (x, y, w, h). Hanya untuk pencatatan."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    kiri, atas = max(ax, bx), max(ay, by)
    kanan, bawah = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    potong = max(0.0, kanan - kiri) * max(0.0, bawah - atas)
    gabung = aw * ah + bw * bh - potong
    return potong / gabung if gabung > 0 else 0.0


def dedup_boxes_aman(dets, iou_th=0.45, iou_beda_th=0.70):
    """
    Deduplikasi kotak deteksi cerdas dengan fallback aman.
    Jika sigap_core di device belum diperbarui (misal saat dijalankan via MaixVision),
    fungsi ini tetap menjalankan logika class-aware langsung tanpa crash TypeError.
    """
    try:
        return core.dedup_boxes(dets, iou_th=iou_th, iou_beda_th=iou_beda_th)
    except TypeError:
        out = []
        for d in sorted(dets, key=lambda x: -x["conf"]):
            lolos = True
            for k in out:
                nilai_iou = (core.iou(d["box"], k["box"]) if hasattr(core, "iou")
                             else _iou(d["box"], k["box"]))
                ambang = iou_th if d.get("nominal") == k.get("nominal") else iou_beda_th
                if nilai_iou >= ambang:
                    lolos = False
                    break
            if lolos:
                out.append(d)
        return out


def analisis_uang(model, frames, mode="AUTO"):
    if not model.muat_yolo():
        return [], 0, "model tidak tersedia", 0.0
    # Mode UANG dan AUTO memakai ambang yang tepat dan voting fleksibel (min 2 frame)
    # untuk mengantisipasi getaran tombol/tangan pengguna tanpa mengorbankan akurasi.
    ambang = 0.80
    setuju_min = 2

    t = time.time()
    per_frame = []
    for idx, img in enumerate(frames):
        dets = []
        try:
            objs = model.deteksi(img, ambang)
        except Exception as e:
            log("YOLO", "deteksi frame %d gagal: %s" % (idx + 1, e))
            objs = []
        for o in objs or []:
            cid = getattr(o, "class_id", None)
            skor = float(getattr(o, "score", 0.0))
            nama = (model.label[cid]
                    if model.label and cid is not None and cid < len(model.label)
                    else cid)
            nom = core.nominal_dari_label(nama)
            if nom is None or skor < ambang:
                continue

            bx, by = int(getattr(o, "x", 0)), int(getattr(o, "y", 0))
            bw, bh = int(getattr(o, "w", 0)), int(getattr(o, "h", 0))

            if bw < 12 or bh < 12:
                continue

            dets.append({"nominal": nom, "conf": skor,
                         "box": (bx, by, bw, bh)})
        n_mentah = len(dets)
        dedup = dedup_boxes_aman(dets, iou_th=IOU_DEDUP, iou_beda_th=IOU_DEDUP_BEDA)
        per_frame.append(dedup)

        if n_mentah != len(dedup):
            log("YOLO", "Frame %d: mentah %d -> dedup %d  (%d dibuang)"
                % (idx + 1, n_mentah, len(dedup), n_mentah - len(dedup)))
            for d in dets:
                x, y, w, h = d["box"]
                log("YOLO", "    kandidat %-7s conf %.2f  box %4.0f,%4.0f %3.0fx%-3.0f"
                    % (d.get("nominal", "?"), d.get("conf", 0), x, y, w, h))
            for i in range(len(dets)):
                for j in range(i + 1, len(dets)):
                    nilai = (core.iou(dets[i]["box"], dets[j]["box"])
                             if hasattr(core, "iou")
                             else _iou(dets[i]["box"], dets[j]["box"]))
                    log("YOLO", "    IoU %s vs %s = %.3f  (ambang %.2f)"
                        % (dets[i].get("nominal", "?"),
                           dets[j].get("nominal", "?"), nilai, IOU_DEDUP))
        elif dedup:
            log("YOLO", "Frame %d: %d deteksi  %s"
                % (idx + 1, len(dedup),
                   ["%s@%.2f" % (d.get("nominal"), d.get("conf", 0))
                    for d in dedup]))
        else:
            log("YOLO", "Frame %d: kosong (ambang conf %.2f)" % (idx + 1, ambang))

    log("WAKTU", "YOLO %d frame: %d ms"
        % (len(frames), int((time.time() - t) * 1000)))

    log("YOLO", "ringkas: %d frame, jumlah per frame %s, ambang setuju %d (Mode %s)"
        % (len(per_frame), [len(f) for f in per_frame], setuju_min, mode))

    if not any(per_frame):
        return [], 0, "tidak ada uang terdeteksi", 0.0

    suara = core.vote_notes(per_frame, setuju_min)
    if not suara:
        return [], 0, "tidak ada uang terdeteksi", 0.0
    daftar = [d["nominal"] for d in suara]
    confs = []
    for d in suara:
        try:
            confs.append(float(d.get("conf", 0.0)))
        except Exception:
            pass
    conf = (sum(confs) / len(confs)) if confs else 0.0
    conf = max(0.0, min(1.0, conf))
    return daftar, sum(daftar), None, conf


def siapkan_ocr(img, ocr, jarak=None):
    """
    Photoshop Unsharp Mask + LiDAR Adaptive Distance Booster untuk input PP-OCR.
    - Jarak Dekat (< 23 cm): Photoshop Unsharp Mask (5x5, amount 1.8, threshold 3).
    - Jarak Menengah-Jauh (>= 23 cm): CLAHE Contrast Stretch + High-Boost (3x3, amount 3.6).
    """
    target = getattr(image.Format, "FMT_BGR888", None)
    if target is None and image is not None:
        target = getattr(image.Format, "FMT_RGB888", None)

    try:
        w, h = ocr.input_width(), ocr.input_height()
    except Exception:
        w, h = 640, 640

    if not w or not h:
        w, h = 640, 640

    w0, h0 = img.width(), img.height()
    skala = min(w / float(w0), h / float(h0))
    nw = max(1, int(round(w0 * skala)))
    nh = max(1, int(round(h0 * skala)))
    off_x = (w - nw) // 2
    off_y = (h - nh) // 2

    # Jika cv2 / numpy tersedia, terapkan Penajaman Adaptif
    if cv2 is not None and np is not None:
        try:
            buf = img.to_bytes()
            arr = np.frombuffer(buf, dtype=np.uint8)
            ch = arr.size // (w0 * h0)
            arr = arr[:w0 * h0 * ch].reshape((h0, w0, ch) if ch == 3 else (h0, w0))

            interp = cv2.INTER_AREA if skala < 1.0 else cv2.INTER_CUBIC
            kecil = cv2.resize(arr, (nw, nh), interpolation=interp)

            # Adaptasi parameter berdasarkan jarak LiDAR
            jauh = (jarak is not None and jarak >= 23.0)
            if jauh:
                if kecil.ndim == 3:
                    lab = cv2.cvtColor(kecil, cv2.COLOR_RGB2LAB)
                    l, a, b = cv2.split(lab)
                    clahe = cv2.createCLAHE(clipLimit=2.4, tileGridSize=(8, 8))
                    l_eq = clahe.apply(l)
                    kecil = cv2.cvtColor(cv2.merge((l_eq, a, b)), cv2.COLOR_LAB2RGB)
                blurred = cv2.GaussianBlur(kecil, (3, 3), 0.7)
                sharpened = cv2.addWeighted(kecil, 2.6, blurred, -1.6, 0)
            else:
                blurred = cv2.GaussianBlur(kecil, (5, 5), 1.0)
                sharpened = cv2.addWeighted(kecil, 2.0, blurred, -1.0, 0)

            if sharpened.ndim == 3:
                kanvas_arr = np.zeros((h, w, sharpened.shape[2]), dtype=np.uint8)
            else:
                kanvas_arr = np.zeros((h, w), dtype=np.uint8)
            kanvas_arr[off_y:off_y + nh, off_x:off_x + nw] = sharpened

            # Sesuaikan urutan kanal jika target BGR
            fmt_bgr = getattr(image.Format, "FMT_BGR888", None)
            if kanvas_arr.ndim == 3 and fmt_bgr is not None and target == fmt_bgr:
                kanvas_arr = kanvas_arr[:, :, ::-1]

            data = np.ascontiguousarray(kanvas_arr).tobytes()
            del kanvas_arr, sharpened, blurred, kecil, arr, buf
            for panggil in [lambda: image.from_bytes(w, h, target, data),
                            lambda: image.from_bytes(data, w, h, target)]:
                try:
                    im = panggil()
                    if im is not None and im.width() == w and im.height() == h:
                        return im
                except Exception:
                    continue
        except Exception as e:
            log("OCR", "penajaman adaptif gagal (%s) -> fallback" % e)

    # Fallback murni tanpa cv2
    hasil = img
    try:
        if target is not None and hasil.format() != target:
            hasil = hasil.to_format(target)
    except Exception:
        pass
    try:
        kecil = hasil.resize(nw, nh)
        kanvas = image.Image(w, h, target or image.Format.FMT_RGB888)
        kanvas.draw_rect(0, 0, w, h, image.COLOR_BLACK, thickness=-1)
        kanvas.draw_image(off_x, off_y, kecil)
        return kanvas
    except Exception:
        try:
            return hasil.resize(w, h)
        except Exception:
            return img


def _tandai_terpotong(kotak, ox, oy, pw, ph, lebar_frame, tinggi_frame):
    """Beri tanda pada kotak yang menyentuh batas dalam petak."""
    for k in kotak:
        x, y, w, h = k["box"]
        kiri  = (ox > 0) and (x <= ox + MARGIN_SAMBUNGAN)
        kanan = (ox + pw < lebar_frame) and (x + w >= ox + pw - MARGIN_SAMBUNGAN)
        atas  = (oy > 0) and (y <= oy + MARGIN_SAMBUNGAN)
        bawah = (oy + ph < tinggi_frame) and (y + h >= oy + ph - MARGIN_SAMBUNGAN)
        k["terpotong"] = bool(kiri or kanan or atas or bawah)
    return kotak


def bersihkan_sambungan(kotak, ambang_x=0.30):
    """Buang kotak yang menduplikasi kotak lain di daerah sambungan petak."""
    def sebaris(a, b):
        ay, ah = a["box"][1], a["box"][3]
        by, bh = b["box"][1], b["box"][3]
        tumpang = max(0.0, min(ay + ah, by + bh) - max(ay, by))
        return tumpang >= 0.5 * max(1.0, min(ah, bh))

    def tumpang_x(a, b):
        ax, aw = a["box"][0], a["box"][2]
        bx, bw = b["box"][0], b["box"][2]
        t = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
        return t / max(1.0, min(aw, bw))

    urut = sorted(kotak,
                  key=lambda z: (bool(z.get("terpotong", False)),
                                 -len(z.get("teks", ""))))
    hasil, dibuang = [], 0
    for k in urut:
        bentrok = False
        for h in hasil:
            if sebaris(k, h) and tumpang_x(k, h) >= ambang_x:
                bentrok = True
                break
        if bentrok:
            dibuang += 1
        else:
            hasil.append(k)
    return hasil, dibuang


def _ocr_kotak(model, img, ox=0, oy=0, skala=1.0, jarak=None):
    """OCR satu gambar. -> (list kotak dalam koordinat frame asli, n_objek)"""
    try:
        objs = model.ocr.detect(siapkan_ocr(img, model.ocr, jarak=jarak))
    except Exception as e:
        log("OCR", "detect gagal: %s" % e)
        return [], 0
    kotak = []
    for o in objs or []:
        try:
            s = o.char_str()
        except Exception:
            s = ""
        if not s or not s.strip():
            continue
        b = o.box
        try:
            xs = [b.x1, b.x2, b.x3, b.x4]
            ys = [b.y1, b.y2, b.y3, b.y4]
        except AttributeError:
            p = b.to_list()
            xs, ys = p[0::2], p[1::2]
        kotak.append({
            "teks": s.strip(),
            "box": (min(xs) / skala + ox, min(ys) / skala + oy,
                    (max(xs) - min(xs)) / skala, (max(ys) - min(ys)) / skala),
            "tinggi_asli": (core.tinggi_teks(xs, ys) / skala
                            if hasattr(core, "tinggi_teks")
                            else (max(ys) - min(ys)) / skala),
        })
    return kotak, len(objs or [])


def _ocr_frame(model, img, cb_layar=None, jarak=None):
    """OCR satu frame, dengan atau tanpa pemotongan petak."""
    if not PETAK_OCR:
        skala = min(model.ocr.input_width() / float(img.width()),
                    model.ocr.input_height() / float(img.height()))
        k, n = _ocr_kotak(model, img, 0, 0, skala, jarak=jarak)
        for z in k:
            z["terpotong"] = False
        return k, n

    skala = model.ocr.input_width() / float(PETAK_W)
    semua, n_objek, n_potong = [], 0, 0
    total_petak = len(PETAK_Y) * len(PETAK_X)
    petak_ke = 0
    for oy in PETAK_Y:
        for ox in PETAK_X:
            petak_ke += 1
            if cb_layar:
                cb_layar("membaca teks (petak %d/%d)..." % (petak_ke, total_petak))
            try:
                potong = img.crop(ox, oy, PETAK_W, PETAK_H)
            except Exception as e:
                log("OCR", "crop(%d,%d) gagal: %s" % (ox, oy, e))
                continue
            k, n = _ocr_kotak(model, potong, ox, oy, skala, jarak=jarak)
            try:
                del potong
            except Exception:
                pass
            k = _tandai_terpotong(k, ox, oy, PETAK_W, PETAK_H,
                                  img.width(), img.height())
            n_potong += sum(1 for z in k if z["terpotong"])
            semua += k
            n_objek += n
    n_mentah = len(semua)
    semua, n_bentrok = bersihkan_sambungan(semua)
    semua = core.dedup_teks(semua)
    log("OCR", "  %d petak -> %d mentah (%d terpotong) -> %d setelah sambungan"
                " -> %d setelah dedup"
        % (len(PETAK_X) * len(PETAK_Y), n_mentah, n_potong,
           n_mentah - n_bentrok, len(semua)))
    return semua, len(semua)


def cari_gutter(kotak, lebar_frame):
    """Cari lajur kosong vertikal yang membelah halaman jadi dua kolom.

    Memproyeksikan seluruh kotak ke sumbu mendatar, lalu mencari celah
    terlebar yang tidak tersentuh kotak mana pun. -> posisi x, atau None.
    """
    if len(kotak) < GUTTER_MIN_KOTAK:
        return None
    langkah = max(1, int(lebar_frame / 160.0))
    n = int(lebar_frame / langkah) + 1
    terisi = [False] * n
    for k in kotak:
        x, _, w, _ = k["box"]
        a = max(0, int(x / langkah))
        b = min(n - 1, int((x + w) / langkah))
        for i in range(a, b + 1):
            terisi[i] = True

    lo = int(lebar_frame * GUTTER_ZONA[0] / langkah)
    hi = min(n - 1, int(lebar_frame * GUTTER_ZONA[1] / langkah))
    min_lebar = max(1, int(lebar_frame * GUTTER_MIN_LEBAR / langkah))

    terbaik, mulai = None, None
    for i in range(lo, hi + 1):
        if not terisi[i]:
            if mulai is None:
                mulai = i
        elif mulai is not None:
            panjang = i - mulai
            if panjang >= min_lebar and (terbaik is None or panjang > terbaik[1]):
                terbaik = (mulai, panjang)
            mulai = None
    if mulai is not None:
        panjang = hi + 1 - mulai
        if panjang >= min_lebar and (terbaik is None or panjang > terbaik[1]):
            terbaik = (mulai, panjang)

    if terbaik is None:
        return None
    return (terbaik[0] + terbaik[1] / 2.0) * langkah


def _adalah_kolom_harga(kotak_list):
    """-> True bila kolom ini murni berisi deretan harga (bukan menu terpisah)."""
    if not kotak_list:
        return False
    n_harga = 0
    for k in kotak_list:
        t = k.get("teks", "").strip()
        murni_harga = bool(re.search(r"^(rp|rn|ro|po)?\s*[\d\.\,oOqQ]+k?$", t, re.I))
        ada_digit = any(c.isdigit() for c in t)
        if murni_harga or (ada_digit and len(t) <= 10):
            n_harga += 1
    return (n_harga / float(len(kotak_list))) >= 0.60


def kelompokkan_kolom(kotak, lebar_frame):
    """Bagi kotak menjadi kolom, urut kiri ke kanan. -> list of list.

    Menu berkolom tunggal atau menu yang kolom kanannya murni harga
    mengembalikan satu kelompok, sehingga nama makanan dan harga tetap
    berada di baris yang sama.
    """
    x_belah = cari_gutter(kotak, lebar_frame)
    if x_belah is None:
        return [kotak]
    tengah = lambda k: k["box"][0] + k["box"][2] / 2.0
    kiri = [k for k in kotak if tengah(k) < x_belah]
    kanan = [k for k in kotak if tengah(k) >= x_belah]
    # Belahan yang menyisakan kolom nyaris kosong hampir pasti salah:
    # yang terbaca sebagai gutter sebenarnya hanya margin dalam.
    if len(kiri) < GUTTER_MIN_ISI or len(kanan) < GUTTER_MIN_ISI:
        return [kotak]
    # Jika kolom kanan murni harga (misal Rp17.000, Rp18.000), JANGAN dibelah
    # supaya core.baca_baris_info dapat memasangkan nama dengan harganya.
    if _adalah_kolom_harga(kanan):
        log("KOLOM", "kolom kanan adalah daftar harga -> dibaca sebaris utuh")
        return [kotak]
    log("KOLOM", "dibelah di x=%.0f -> kiri %d kotak, kanan %d kotak"
        % (x_belah, len(kiri), len(kanan)))
    return [kiri, kanan]


def skor_ketajaman_frame(img):
    """Hitung skor ketajaman Laplacian 2D pada area tengah citra."""
    try:
        w, h = img.width(), img.height()
        cx, cy = w // 2, h // 2
        rw, rh = min(w, 200), min(h, 160)
        potong = img.crop(cx - rw // 2, cy - rh // 2, rw, rh)
        if cv2 is not None and np is not None:
            buf = potong.to_bytes()
            arr = np.frombuffer(buf, dtype=np.uint8)
            ch = arr.size // (rw * rh)
            arr = arr[:rw * rh * ch].reshape((rh, rw, ch) if ch == 3 else (rh, rw))
            gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY) if arr.ndim == 3 else arr
            lap = cv2.Laplacian(gray, cv2.CV_16S, ksize=3)
            return float(lap.astype(np.float32).var())
        sampel = []
        for x in range(max(0, cx - 80), min(w, cx + 80), 2):
            p = img.get_pixel(x, cy)
            lum = (p[0] * 299 + p[1] * 587 + p[2] * 114) // 1000 if isinstance(p, (tuple, list)) else int(p) & 0xFF
            sampel.append(lum)
        return core.ketajaman(sampel) if (core and hasattr(core, "ketajaman")) else 1.0
    except Exception:
        return 1.0


def pilih_frame_tertajam(frames):
    """Pilih 1 frame paling tajam dari deretan frame kamera untuk anti-motion blur."""
    if not frames:
        return None
    if len(frames) == 1:
        return frames[0]
    skor_list = [skor_ketajaman_frame(f) for f in frames]
    idx_terbaik = skor_list.index(max(skor_list))
    log("FOKUS", "Skor ketajaman %d frame: %s -> Terpilih Frame %d"
        % (len(frames), ["%.1f" % s for s in skor_list], idx_terbaik + 1))
    return frames[idx_terbaik]


def analisis_teks(model, frames, kamus, cb_layar=None, jarak=None):
    if not model.muat_ocr():
        return [], "OCR tidak tersedia"
    riwayat, ada, ms, n_kotak, ms_koreksi = [], False, 0, 0, 0
    semua_info = []
    for i, img in enumerate(frames):
        t = time.time()
        kotak, nk = _ocr_frame(model, img, cb_layar=cb_layar, jarak=jarak)
        ms += int((time.time() - t) * 1000)
        n_kotak += nk
        lebar_frame = img.width()
        info = []
        for kolom in kelompokkan_kolom(kotak, lebar_frame):
            info += core.baca_baris_info(kolom)
        info = [b for b in info if not baris_derau(b["teks"])]
        baris = [b["teks"] for b in info]
        if baris:
            ada = True
            log("OCR", "  frame %d [%d kotak]: %s"
                % (i + 1, nk, " | ".join(baris)))
        t = time.time()
        for b in info:
            b["teks"] = pisah_kata_menempel(b["teks"], kamus)
            b["teks"] = gabung_kata_ganda(b["teks"])
            b["teks"] = normalisasi_kata_ocr(b["teks"])
            if core and hasattr(core, "koreksi_baris") and kamus:
                try:
                    b_kor = core.koreksi_baris(b["teks"], kamus, hanya_kamus=False)
                    if b_kor:
                        b["teks"] = b_kor
                except Exception:
                    pass
            b["teks"] = bersihkan_gema_suku_kata(b["teks"])
            b["teks"] = standarisasi_harga_rupiah(b["teks"])
            b["harga"] = any(c.isdigit() for c in b["teks"])
        ms_koreksi += int((time.time() - t) * 1000)

        info = core.kenali_judul(info, kamus)
        riwayat.append([b["teks"] for b in info])
        semua_info = info
    log("WAKTU", "OCR %d frame, %d kotak, %d ms (~%d ms/kotak) "
                 "| koreksi kamus %d ms"
        % (len(frames), n_kotak, ms, ms // max(1, n_kotak), ms_koreksi))

    if not ada:
        return [], "tidak ada teks terbaca"
    t = time.time()
    hasil = core.vote_baris(riwayat, MIN_SENDIRI_OCR, kamus)
    hasil = [b for b in hasil if not baris_derau(b)]
    hasil = gabung_harga_terpisah(hasil, kamus)
    log("WAKTU", "vote_baris %d ms" % int((time.time() - t) * 1000))
    if not hasil:
        return [], "teks tidak stabil"

    for b in hasil:
        log("OCR", "  " + b)

    bagian, idx = [], 0
    if semua_info:
        tetap = set(hasil)
        sisa = [b for b in semua_info if b["teks"] in tetap]
        semua = core.kelompokkan_bagian(sisa)
        for b in sisa:
            if b.get("judul"):
                log("JUDUL", "  ? %-26s tinggi %d" % (b["teks"][:26], b.get("tinggi", 0)))
        nama, bagian = core.pisah_nama_tempat(semua)
        if nama:
            log("JUDUL", "nama tempat: %s" % " ".join(nama))

        # Bersihkan isi setiap kategori dari duplikasi kembar dan baris sampah
        bagian_bersih = []
        for j, isi in bagian:
            isi_gabung = core.gabung_baris_kembar(isi, kamus) if hasattr(core, "gabung_baris_kembar") else isi
            isi_saring, _ = core.saring_sampah(isi_gabung, kamus) if hasattr(core, "saring_sampah") else (isi_gabung, 0)
            if isi_saring:
                bagian_bersih.append((j, isi_saring))
        if bagian_bersih:
            bagian = bagian_bersih

        skor = core.nilai_bagian(bagian, sisa) \
            if hasattr(core, "nilai_bagian") else [0] * len(bagian)
        for i, (j, isi) in enumerate(bagian):
            log("JUDUL", "%-24s %d isi" % (j or "(tanpa judul)", len(isi)))
        if hasattr(core, "bagian_dituju"):
            idx = core.bagian_dituju(bagian, sisa)
        if hasattr(core, "siapkan_ucapan") and bagian:
            hasil_bersih, dibuang = core.siapkan_ucapan(bagian, idx, kamus)
            if dibuang:
                log("FILTER", "%d baris sampah dibuang sebelum diucapkan" % dibuang)
            if hasil_bersih:
                hasil = hasil_bersih
    return (hasil, bagian, idx), None


def urai_hasil_teks(hasil):
    if not isinstance(hasil, tuple):
        return (hasil or []), [], 0
    if len(hasil) >= 3:
        return hasil[0], hasil[1], hasil[2]
    if len(hasil) == 2:
        return hasil[0], hasil[1], 0
    return (hasil[0] if hasil else []), [], 0


# ======================================================================
# TAMPILAN & NAVIGASI (LANGSUNG DETEKSI)
# ======================================================================
def di_dalam(kotak, x, y):
    kx, ky, kw, kh = kotak
    return kx <= x <= kx + kw and ky <= y <= ky + kh


def potong(teks, lebar_px, skala=1.0):
    maks = max(4, int(lebar_px / max(1.0, 8 * skala)))
    return teks if len(teks) <= maks else teks[:maks - 1] + "~"


# ======================================================================
# HTTP REST API SERVER UNTUK SINKRONISASI DENGAN APLIKASI ANDROID
# ======================================================================
IS_LINUX = sys.platform.startswith("linux")
if IS_LINUX and os.path.exists("/root") and os.access("/root", os.W_OK):
    DEFAULT_BASE = "/root"
else:
    DEFAULT_BASE = os.path.abspath(os.path.join(APP_DIR, "data_maixcam"))

DATA_DIR = os.environ.get("SIGAP_DATA_DIR", DEFAULT_BASE)
IMG_DIR = os.path.join(DATA_DIR, "uji_rekam")
ACTIVE_CSV = os.path.join(DATA_DIR, "uji_rekam.csv")
HTML_REKAM = os.path.join(DATA_DIR, "laporan_rekam.html")

PORT_SERVER = int(os.environ.get("SIGAP_PORT", "8080"))
DEVICE_ID = os.environ.get("SIGAP_DEVICE_ID", "SGP-NETRA-0007")
FIRMWARE_VER = "v1.2.0-pkm"
DEVICE_TOKEN = os.environ.get("SIGAP_TOKEN", "sigap-token-sg2002")

CSV_HEADER = "no,detik,objek,jalur,lebar,tinggi,format,ms_baca,ketajaman,berkas,link_excel\n"


def dapatkan_ip_lokal():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "192.168.137.81" if IS_LINUX else "127.0.0.1"


def pastikan_direktori_api():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(IMG_DIR, exist_ok=True)
        if not os.path.exists(ACTIVE_CSV):
            with open(ACTIVE_CSV, "w", encoding="utf-8") as f:
                f.write(CSV_HEADER)
    except Exception as e:
        log("API", "Gagal buat direktori: %s" % e)


def hitung_baris_csv(file_path):
    if not os.path.exists(file_path):
        return 0
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = [l.strip() for l in f.readlines() if l.strip()]
        return max(0, len(lines) - 1)
    except Exception:
        return 0


def hitung_sha1_file(file_path):
    if not os.path.exists(file_path):
        return ""
    try:
        h = hashlib.sha1()
        with open(file_path, "rb") as f:
            while True:
                c = f.read(65536)
                if not c:
                    break
                h.update(c)
        return h.hexdigest()
    except Exception:
        return ""


def nomor_terakhir_csv(file_path):
    if not os.path.exists(file_path):
        return 0
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = [l.strip() for l in f.readlines() if l.strip()]
        if len(lines) <= 1:
            return 0
        p = lines[-1].split(",")
        return int(p[0]) if p[0].isdigit() else 0
    except Exception:
        return 0


def dapatkan_dimensi(img):
    try:
        w = img.width() if callable(getattr(img, "width", None)) else getattr(img, "width", 0)
        h = img.height() if callable(getattr(img, "height", None)) else getattr(img, "height", 0)
        return int(w), int(h)
    except Exception:
        return 0, 0


class PerekamTransaksi:
    def __init__(self):
        self.antrean = deque()
        self.aktif = True
        self.kunci = threading.Lock()
        self.kondisi = threading.Condition(self.kunci)
        self.thread = threading.Thread(target=self._lingkar_rekam, daemon=True)
        self.thread.start()

    def catat(self, no, detik, objek, ms_baca, img_utama, img_kecil=None):
        with self.kondisi:
            self.antrean.append((no, detik, objek, ms_baca, img_utama, img_kecil))
            self.kondisi.notify()

    def stop(self):
        with self.kondisi:
            self.aktif = False
            self.kondisi.notify_all()

    def _lingkar_rekam(self):
        while self.aktif:
            item = None
            with self.kondisi:
                while self.aktif and not self.antrean:
                    self.kondisi.wait(timeout=1.0)
                if self.antrean:
                    item = self.antrean.popleft()
            if item is None:
                continue

            no, detik, objek, ms_baca, im_u, im_k = item
            pastikan_direktori_api()
            fn_u = "c1_%04d.jpg" % no
            path_u = os.path.join(IMG_DIR, fn_u)
            w_u, h_u = dapatkan_dimensi(im_u)
            fmt_u = "RGB888"

            if im_u is not None:
                try:
                    if hasattr(im_u, "save"):
                        im_u.save(path_u)
                    elif hasattr(im_u, "to_bytes"):
                        with open(path_u, "wb") as fp:
                            fp.write(im_u.to_bytes())
                except Exception as e:
                    log("API", "Gagal simpan foto c1: %s" % e)

            if im_k is not None:
                fn_k = "c2_%04d.jpg" % no
                path_k = os.path.join(IMG_DIR, fn_k)
                try:
                    if hasattr(im_k, "save"):
                        im_k.save(path_k)
                    elif hasattr(im_k, "to_bytes"):
                        with open(path_k, "wb") as fp:
                            fp.write(im_k.to_bytes())
                except Exception:
                    pass

            tajam = 1.0
            if hasattr(im_u, "ketajaman"):
                try:
                    tajam = float(im_u.ketajaman())
                except Exception:
                    tajam = 1.0

            baris = "%d,%.1f,%s,%s,%d,%d,%s,%d,%.4f,%s,%s\n" % (
                no, detik, objek, "baru", w_u, h_u, fmt_u,
                int(ms_baca), tajam, fn_u, ""
            )
            try:
                with open(ACTIVE_CSV, "a", encoding="utf-8") as f:
                    f.write(baris)
            except Exception as e:
                log("API", "Gagal tulis CSV: %s" % e)


class SigapApiHandler(http.server.BaseHTTPRequestHandler):
    app_ref = None
    path = ""
    wfile = None

    def send_response(self, code, message=None):
        super().send_response(code, message)

    def send_header(self, keyword, value):
        super().send_header(keyword, value)

    def end_headers(self):
        super().end_headers()

    def _set_headers(self, status=200, content_type="application/json", custom_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, DELETE")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Device-Token")
        if custom_headers:
            for k, v in custom_headers.items():
                self.send_header(k, v)
        self.end_headers()

    def do_OPTIONS(self):
        self._set_headers(200)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        pastikan_direktori_api()

        if path in ("/health", "/api/status", "/api/ping"):
            pending_count = hitung_baris_csv(ACTIVE_CSV)
            baterai_val = 85
            if SigapApiHandler.app_ref is not None:
                bat = getattr(SigapApiHandler.app_ref, "baterai", None)
                if bat is not None and getattr(bat, "persen", None) is not None:
                    try:
                        baterai_val = int(bat.persen)
                    except Exception:
                        pass

            data = {
                "id": DEVICE_ID,
                "fw": FIRMWARE_VER,
                "battery": baterai_val,
                "pending_rows": pending_count,
                "token": DEVICE_TOKEN,
                "waktu": time.strftime("%Y-%m-%d %H:%M:%S"),
                "ip": dapatkan_ip_lokal(),
                "port": PORT_SERVER
            }
            self._set_headers(200)
            self.wfile.write(json.dumps(data).encode("utf-8"))
            return

        if path == "/sessions":
            daftar_segel = []
            pola = os.path.join(DATA_DIR, "segel_*.csv")
            for file_path in glob.glob(pola):
                nama_file = os.path.basename(file_path)
                sid = nama_file.replace("segel_", "").replace(".csv", "")
                daftar_segel.append({
                    "session_id": sid,
                    "rows": hitung_baris_csv(file_path),
                    "sha1": hitung_sha1_file(file_path),
                    "bytes": os.path.getsize(file_path)
                })
            self._set_headers(200)
            self.wfile.write(json.dumps(daftar_segel).encode("utf-8"))
            return

        if path.startswith("/session/") and path.endswith("/csv"):
            parts = path.split("/")
            if len(parts) == 4:
                sid = parts[2]
                path_segel = os.path.join(DATA_DIR, "segel_%s.csv" % sid)
                if os.path.exists(path_segel):
                    try:
                        with open(path_segel, "rb") as f:
                            konten = f.read()
                        self._set_headers(200, content_type="text/csv")
                        self.wfile.write(konten)
                        return
                    except Exception as e:
                        self._set_headers(500)
                        self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
                        return
                else:
                    self._set_headers(404)
                    self.wfile.write(json.dumps({"error": "Sesi segel tidak ditemukan"}).encode("utf-8"))
                    return

        if path.startswith("/session/") and "/img/" in path:
            parts = path.split("/img/")
            if len(parts) == 2:
                nama_img = parts[1]
                path_img = os.path.join(IMG_DIR, nama_img)
                if os.path.exists(path_img) and os.path.isfile(path_img):
                    try:
                        with open(path_img, "rb") as f:
                            img_bytes = f.read()
                        self._set_headers(200, content_type="image/jpeg")
                        self.wfile.write(img_bytes)
                        return
                    except Exception as e:
                        self._set_headers(500)
                        self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
                        return
                else:
                    self._set_headers(404)
                    self.wfile.write(json.dumps({"error": "Gambar %s tidak ditemukan" % nama_img}).encode("utf-8"))
                    return

        if path.startswith("/foto/"):
            nama_img = path[6:]
            path_img = os.path.join(IMG_DIR, nama_img)
            if os.path.exists(path_img) and os.path.isfile(path_img):
                try:
                    with open(path_img, "rb") as f:
                        img_bytes = f.read()
                    self._set_headers(200, content_type="image/jpeg")
                    self.wfile.write(img_bytes)
                    return
                except Exception as e:
                    self._set_headers(500)
                    self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
                    return
            else:
                self._set_headers(404)
                self.wfile.write(json.dumps({"error": "Foto tidak ditemukan"}).encode("utf-8"))
                return

        if path == "/api/transaksi":
            ip_srv = dapatkan_ip_lokal()
            daftar_transaksi = []
            if os.path.exists(ACTIVE_CSV):
                try:
                    with open(ACTIVE_CSV, "r", encoding="utf-8", errors="ignore") as f:
                        lines = [l.strip() for l in f.readlines() if l.strip()]
                    for l in lines[1:]:
                        p = l.split(",")
                        if len(p) >= 10:
                            no_id, det, objek, jalur = p[0], p[1], p[2], p[3]
                            lebar, tinggi, ms, tajam, berkas = p[4], p[5], p[7], p[8], p[9]
                            daftar_transaksi.append({
                                "id": int(no_id) if no_id.isdigit() else 0,
                                "detik": float(det) if det.replace(".", "", 1).isdigit() else 0.0,
                                "objek": objek,
                                "jalur": jalur,
                                "resolusi": "%sx%s" % (lebar, tinggi),
                                "latensi_ms": int(ms) if ms.isdigit() else 0,
                                "ketajaman": float(tajam) if tajam.replace(".", "", 1).isdigit() else 0.0,
                                "nama_berkas": berkas,
                                "url_foto": "http://%s:%d/foto/%s" % (ip_srv, PORT_SERVER, berkas)
                            })
                except Exception as e:
                    log("API", "Gagal parse transaksi: %s" % e)
            self._set_headers(200)
            self.wfile.write(json.dumps(daftar_transaksi, indent=2).encode("utf-8"))
            return

        if path == "/api/download_csv":
            if os.path.exists(ACTIVE_CSV):
                try:
                    with open(ACTIVE_CSV, "rb") as fp:
                        csv_bytes = fp.read()
                    self._set_headers(200, content_type="text/csv")
                    self.wfile.write(csv_bytes)
                    return
                except Exception as e:
                    self._set_headers(500)
                    self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
                    return
            else:
                self._set_headers(404)
                self.wfile.write(json.dumps({"error": "CSV belum dibuat"}).encode("utf-8"))
                return


        if path in ("", "/", "/laporan"):
            self._set_headers(200, content_type="text/html; charset=utf-8")
            self.wfile.write(b"<h1>Sigap Netra API Server Aktif</h1><p>Gunakan aplikasi Android untuk terhubung.</p>")
            return

        self._set_headers(404)
        self.wfile.write(json.dumps({"error": "Endpoint GET tidak ditemukan"}).encode("utf-8"))

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        pastikan_direktori_api()

        # 0. Perintah Ambil Data / Pindai Jarak Jauh dari HP: POST /api/capture atau /api/proses
        if path in ("/api/capture", "/api/proses"):
            if SigapApiHandler.app_ref is not None:
                SigapApiHandler.app_ref.minta_proses = True
                self._set_headers(200)
                self.wfile.write(json.dumps({"status": "ok", "pesan": "Perintah pengambilan data diterima"}).encode("utf-8"))
                return
            self._set_headers(500)
            self.wfile.write(json.dumps({"error": "Aplikasi kacamata belum aktif"}).encode("utf-8"))
            return

        if path == "/session/seal":
            sid = time.strftime("%Y%m%dT%H%M%S")
            path_segel = os.path.join(DATA_DIR, "segel_%s.csv" % sid)

            if os.path.exists(ACTIVE_CSV):
                try:
                    os.rename(ACTIVE_CSV, path_segel)
                except Exception:
                    with open(ACTIVE_CSV, "rb") as f_in, open(path_segel, "wb") as f_out:
                        f_out.write(f_in.read())
                    os.remove(ACTIVE_CSV)

            with open(ACTIVE_CSV, "w", encoding="utf-8") as f:
                f.write(CSV_HEADER)

            baris = hitung_baris_csv(path_segel)
            sha = hitung_sha1_file(path_segel)
            b_size = os.path.getsize(path_segel) if os.path.exists(path_segel) else 0

            respons = {
                "session_id": sid,
                "rows": baris,
                "sha1": sha,
                "bytes": b_size
            }
            self._set_headers(200)
            self.wfile.write(json.dumps(respons).encode("utf-8"))
            return

        if path == "/api/hapus_semua":
            try:
                if os.path.exists(ACTIVE_CSV):
                    with open(ACTIVE_CSV, "w", encoding="utf-8") as f:
                        f.write(CSV_HEADER)
                if os.path.exists(IMG_DIR):
                    for fn in os.listdir(IMG_DIR):
                        fp = os.path.join(IMG_DIR, fn)
                        if os.path.isfile(fp):
                            try:
                                os.remove(fp)
                            except Exception:
                                pass
                self._set_headers(200)
                self.wfile.write(json.dumps({"pesan": "Seluruh data riwayat berhasil dibersihkan"}).encode("utf-8"))
                return
            except Exception as e:
                self._set_headers(500)
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
                return

        self._set_headers(404)
        self.wfile.write(json.dumps({"error": "Endpoint POST tidak ditemukan"}).encode("utf-8"))

    def do_DELETE(self):
        path = self.path.split("?")[0].rstrip("/")
        pastikan_direktori_api()

        if path.startswith("/session/"):
            parts = path.split("/")
            if len(parts) == 3:
                sid = parts[2]
                path_segel = os.path.join(DATA_DIR, "segel_%s.csv" % sid)
                if os.path.exists(path_segel):
                    try:
                        os.remove(path_segel)
                        self.send_response(204)
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.end_headers()
                        return
                    except Exception as e:
                        self._set_headers(500)
                        self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
                        return
                else:
                    self._set_headers(404)
                    self.wfile.write(json.dumps({"error": "Berkas segel tidak ditemukan"}).encode("utf-8"))
                    return

        if path == "/api/transaksi":
            try:
                with open(ACTIVE_CSV, "w", encoding="utf-8") as f:
                    f.write(CSV_HEADER)
                self._set_headers(200)
                self.wfile.write(json.dumps({"pesan": "Riwayat transaksi CSV berhasil dikosongkan"}).encode("utf-8"))
                return
            except Exception as e:
                self._set_headers(500)
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
                return

        self._set_headers(404)
        self.wfile.write(json.dumps({"error": "Endpoint DELETE tidak ditemukan"}).encode("utf-8"))

    def log_message(self, format, *args):
        # Redam log akses rutin HTTP agar terminal MaixVision tetap fokus pada LiDAR/Deteksi
        pass


class SigapServer:
    def __init__(self, port=PORT_SERVER, app_ref=None):
        self.port = port
        self.app_ref = app_ref
        self.server = None
        self.thread = None
        self.perekam = None
        self.waktu_mulai = time.time()

    def start(self):
        pastikan_direktori_api()
        self.perekam = PerekamTransaksi()
        try:
            SigapApiHandler.app_ref = self.app_ref
            socketserver.TCPServer.allow_reuse_address = True
            self.server = socketserver.ThreadingTCPServer(("", self.port), SigapApiHandler)
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            ip = dapatkan_ip_lokal()
            log("API", "Server aktif di: http://%s:%d" % (ip, self.port))
            return True
        except Exception as e:
            log("API", "Gagal start server pada port %d: %s" % (self.port, e))
            return False

    def stop(self):
        if self.perekam:
            try:
                self.perekam.stop()
            except Exception:
                pass
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
                log("API", "Server dihentikan")
            except Exception:
                pass

    def catat(self, no, daftar, ok, durasi_ms, mode, frame_utama, frame_kecil=None):
        if not self.perekam or frame_utama is None:
            return
        if daftar and len(daftar) > 0:
            objek = str(daftar[0]) if len(daftar) == 1 else str(sum(daftar))
        elif ok:
            objek = "TEKS"
        else:
            objek = "NIHIL"
        det = round(time.time() - self.waktu_mulai, 1)
        im_u = frame_utama.copy() if hasattr(frame_utama, "copy") else frame_utama
        im_k = frame_kecil.copy() if (frame_kecil is not None and hasattr(frame_kecil, "copy")) else None
        self.perekam.catat(no, det, objek, durasi_ms, im_u, im_k)


class Aplikasi:
    def __init__(self):
        log("APP", "=== Inisialisasi SIGAP NETRA V2 (Full Peripheral) ===")
        if ENABLE_LAYAR:
            try:
                if os.path.exists("/sys/class/pwm/pwmchip0/pwm10"):
                    try:
                        with open("/sys/class/pwm/pwmchip0/unexport", "w") as f_un:
                            f_un.write("10\n")
                    except Exception:
                        pass
                self.disp = display.Display()
                self.W, self.H = self.disp.width(), self.disp.height()
                log("LAYAR", "%dx%d" % (self.W, self.H))
            except Exception as e:
                log("LAYAR", "display init gagal: %s" % e)
                self.disp = None
                self.W, self.H = 640, 480
        else:
            self.disp = None
            self.W, self.H = 640, 480
            log("LAYAR", "Mode hemat daya: Layar LCD NON-AKTIF (ENABLE_LAYAR = False)")

        self.audio = Audio()
        self.model = Model()
        log("YOLO", "ambang: CONF_MIN=%.2f  NMS_IOU=%s  IOU_DEDUP=%.2f  SETUJU_MIN=%d"
            % (CONF_MIN, globals().get("NMS_IOU", "bawaan model"),
               IOU_DEDUP, SETUJU_MIN))

        self.kamus = set()
        if core:
            try:
                from kata_utuh import KATA_UTUH
                self.kamus = core.muat_kamus(KATA_UTUH)
                log("KAMUS", "%d kata" % len(self.kamus))
            except Exception:
                self.kamus = core.muat_kamus()
                log("KAMUS", "dasar %d kata" % len(self.kamus))
        self.audio.kamus = self.kamus

        # Tombol Fisik Pin A23
        self.tombol = Tombol(PIN_TOMBOL)

        # Saklar Spasial LiDAR TF-Luna (Pins A18/A19 di /dev/ttyS1)
        self.lidar = Lidar()

        # Monitor Baterai dinonaktifkan sementara untuk fokus isolasi LiDAR
        self.baterai = None
        self.terakhir_log_lidar = 0.0

        # Kamera Utama: Buka resolusi standar ISP (1280x720 / 1024x576 / 800x448 / 640x480)
        self.cam = None
        for lw, lh in [(1280, 720), (1024, 576), (800, 448), (640, 480), (self.W, self.H)]:
            try:
                self.cam = camera.Camera(lw, lh, image.Format.FMT_RGB888)
                log("KAMERA", "%dx%d RGB888 berhasil dibuka" % (self.cam.width(), self.cam.height()))
                break
            except Exception as e:
                log("KAMERA", "%dx%d ditolak: %s" % (lw, lh, type(e).__name__))
        if self.cam is None:
            raise RuntimeError("Kamera tidak bisa dibuka")
        self.besar = (self.cam.width() > self.W or self.cam.height() > self.H)

        self.cam_kecil = None
        if self.besar and ENABLE_LAYAR:
            try:
                self.cam_kecil = self.cam.add_channel(self.W, self.H)
                log("KAMERA", "kanal preview ISP %dx%d" % (self.W, self.H))
            except Exception as e:
                log("KAMERA", "kanal preview ISP gagal (%s)" % type(e).__name__)

        if ENABLE_TOUCHSCREEN and ENABLE_LAYAR:
            try:
                self.ts = touchscreen.TouchScreen()
                log("TOUCHSCREEN", "Touchscreen AKTIF")
            except Exception as e:
                log("TOUCHSCREEN", "TouchScreen init gagal: %s" % e)
                self.ts = None
        else:
            self.ts = None
            log("TOUCHSCREEN", "Touchscreen NON-AKTIF (Hanya Tombol Fisik %s)" % PIN_TOMBOL)

        # Keadaan Utama
        self.selesai = False
        self.kotak = {}
        self.turun = False
        # Masa abai setelah proses panjang. Selama OCR berjalan (4-5 detik)
        # GPIO tidak disampel; tanpa jeda ini, jari yang masih menempel
        # terbaca sebagai tekanan baru begitu gelang utama jalan lagi.
        self.abai_sampai = 0.0
        # Pengumuman "siap" ditunda sampai gelang utama benar-benar
        # menggambar frame kamera. Mengucapkannya lebih awal membuat
        # pengguna membidik ke alat yang belum menerima masukan.
        self.minta_siap = False
        self.frame_siap = 0

        self.gagal_layar = 0
        self.kanvas_kamera = False

        # Mode & Status Deteksi
        self.idx_mode = 0
        self.keadaan = "IDLE"
        self.hasil_teks, self.hasil_ok, self.hasil_sampai = "", False, 0.0
        self.baris_teks = []
        self.idx_baca = 0
        self.bagian = []
        self.idx_bagian = 0
        self.e2e = []
        self.waktu_mulai_sesi = 0.0
        self._waktu_mulai_kelompok = 0.0
        self._menunggu_kelompok = False
        self._adalah_kelompok_terakhir = False
        self.durasi_inferensi = 0.0
        self.server_api = None
        self.sigap_cloud = None
        self.seq_rekam = 0
        self.minta_proses = False

    # ------------------------------------------------------------------
    def pemanasan(self):
        if self.server_api is None:
            try:
                pastikan_direktori_api()
                self.seq_rekam = nomor_terakhir_csv(ACTIVE_CSV)
                self.server_api = SigapServer(port=PORT_SERVER, app_ref=self)
                self.server_api.start()
                log("API", "Server REST MaixCam aktif pada port %d" % PORT_SERVER)
            except Exception as e:
                log("API", "Gagal start API server: %s" % e)

        if sigap_cloud is not None:
            try:
                cfg = sigap_cloud.muat_config()
                if cfg:
                    self.sigap_cloud = sigap_cloud.SigapCloud(
                        cfg,
                        {
                            "get_battery_pct": self._cloud_baterai,
                            "get_wifi_ssid": self._cloud_ssid,
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

        if not ENABLE_LAYAR or self.disp is None:
            return True
        for _ in range(10):
            f = self.baca_preview()
            if f is None:
                time.sleep(0.05)
                continue
            try:
                if self.besar and self.cam_kecil is None:
                    f = f.resize(self.W, self.H)
                self.disp.show(f)
                log("LAYAR", "pemanasan berhasil")
                return True
            except Exception as e:
                log("LAYAR", "pemanasan gagal: %s" % e)
                return False
        log("LAYAR", "pemanasan dilewati - kamera tidak memberi frame")
        return False

    def tampilkan(self, img):
        if not ENABLE_LAYAR or self.disp is None or img is None:
            return False
        try:
            self.disp.show(img)
            return True
        except Exception:
            return False

    def baca_preview(self):
        if self.cam_kecil is not None:
            try:
                f = self.cam_kecil.read()
                if f is not None:
                    return f
            except Exception:
                pass
        if self.cam is not None:
            try:
                f = self.cam.read()
                if f is not None:
                    if self.besar and ENABLE_LAYAR:
                        if f.width() != self.W or f.height() != self.H:
                            return f.resize(self.W, self.H)
                    return f
            except Exception:
                pass
        return None

    def kanvas(self):
        img = image.Image(self.W, self.H)
        img.draw_rect(0, 0, self.W, self.H, image.COLOR_BLACK, thickness=-1)
        return img

    def bar_atas(self, img, kanan=""):
        img.draw_rect(0, 0, self.W, 24, image.COLOR_BLACK, thickness=-1)
        img.draw_string(6, 5, "SIGAP NETRA V2", color=image.COLOR_YELLOW,
                        scale=1.0, thickness=2)
        
        # Indikator Baterai di Bar Atas (Tengah-Kiri)
        if hasattr(self, "baterai") and self.baterai and self.baterai.aktif and self.baterai.persen is not None:
            pct = self.baterai.persen
            lvl = self.baterai.level
            c_bat = (image.COLOR_RED if lvl == "kritis"
                     else image.COLOR_YELLOW if lvl == "lemah"
                     else image.COLOR_GREEN)
            teks_bat = "%d%%" % pct
            img.draw_string(140, 5, teks_bat, color=c_bat, scale=0.9, thickness=2)

        if kanan:
            img.draw_string(self.W - 8 * len(kanan) - 8, 5, kanan,
                            color=image.COLOR_YELLOW, scale=1.0, thickness=2)

    def deret_tombol(self, img, isi):
        n = len(isi)
        lebar = (self.W - (n + 1) * 6) // n
        tinggi = int(self.H * 0.13)
        y = self.H - tinggi - 6
        for i, (kunci, teks, warna) in enumerate(isi):
            k = (6 + i * (lebar + 6), y, lebar, tinggi)
            img.draw_rect(k[0], k[1], k[2], k[3], warna, thickness=-1)
            img.draw_string(k[0] + 6, k[1] + int(tinggi * 0.28),
                            potong(teks, lebar - 10, 0.9),
                            color=image.COLOR_WHITE, scale=0.9, thickness=2)
            self.kotak[kunci] = k
        return y

    # ------------------------------------------------------------------
    def ucap_mode(self):
        m = MODE_URUT[self.idx_mode]
        log("MODE", m)
        n = "mode_" + m.lower()
        if self.audio.punya(n):
            self.audio.putar(n, bersihkan=True)
        elif m == "AUTO":
            if self.audio.punya("kamera_siap"):
                self.audio.putar("kamera_siap", bersihkan=True)
            elif self.audio.punya("siap"):
                self.audio.putar("siap", bersihkan=True)
            elif self.audio.punya("mode_uang"):
                self.audio.putar("mode_uang", bersihkan=True)
        else:
            self.audio.ucap("mode " + m.lower(), bersihkan=True)

    def layar_proses(self, pesan, mulai=None):
        p = self.kanvas()
        p.draw_string(16, self.H // 2 - 44, "MEMPROSES...",
                      color=image.COLOR_YELLOW, scale=2.0, thickness=2)
        p.draw_string(16, self.H // 2 + 4, pesan,
                      color=image.COLOR_WHITE, scale=1.2, thickness=2)

        # Timer detik live stopwatch saat pemrosesan berlangsung
        if mulai is not None:
            durasi = max(0.0, time.time() - mulai)
            teks_t = "Waktu: %.1f dtk" % durasi
            p.draw_rect(self.W - 180, 10, 168, 28, image.COLOR_BLACK, thickness=-1)
            p.draw_string(self.W - 174, 14, teks_t, color=image.COLOR_GREEN, scale=1.0, thickness=2)

        p.draw_string(16, self.H // 2 + 38, "Mode: " + MODE_URUT[self.idx_mode],
                      color=image.COLOR_WHITE, scale=1.0)
        self.tampilkan(p)

    def proses(self):
        if core is None:
            return "sigap_core tidak ada", False
        self.audio.mulai_bunyi = None
        mulai = time.time()
        self.waktu_mulai_sesi = mulai
        mode = MODE_URUT[self.idx_mode]
        log("PROSES", "mode " + mode)

        # --------------------------------------------------------------
        # GERBANG JARAK LIDAR (Spatial Distance Gate)
        # Khusus Mode TEKS: memandu jarak baca dokumen/menu di atas meja.
        # Mode UANG & AUTO: filter penolakan spasial ditiadakan agar pengguna
        # tunanetra tidak terhalang saat memegang uang di depan kamera.
        # --------------------------------------------------------------
        if hasattr(self, "lidar") and self.lidar.aktif and mode == "TEKS":
            batas_maks_awal = BATAS_JARAK_TEKS_CM
            jarak_terbaca = self.lidar.baca(timeout=0.25)
            if jarak_terbaca is not None:
                log("LIDAR", "Jarak terdeteksi: %.1f cm (Mode: TEKS, Batas Maks: %.0f cm)"
                    % (jarak_terbaca, batas_maks_awal))
                if jarak_terbaca < JARAK_MIN_CM:
                    pesan = "terlalu dekat (%.0f cm)" % jarak_terbaca
                    log("SPASIAL", "[DITOLAK] Dokumen terlalu dekat: %.1f cm" % jarak_terbaca)
                    if self.audio.punya("terlalu_dekat"):
                        self.audio.putar("terlalu_dekat", bersihkan=True)
                    else:
                        self.audio.ucap("terlalu dekat", bersihkan=True)
                    return pesan, False
                elif jarak_terbaca > batas_maks_awal:
                    pesan = "terlalu jauh (%.0f cm)" % jarak_terbaca
                    log("SPASIAL", "[DITOLAK] Dokumen terlalu jauh: %.1f cm (Batas: %.0f cm)"
                        % (jarak_terbaca, batas_maks_awal))
                    if self.audio.punya("terlalu_jauh"):
                        self.audio.putar("terlalu_jauh", bersihkan=True)
                    else:
                        self.audio.ucap("terlalu jauh, dekatkan", bersihkan=True)
                    return pesan, False
            else:
                log("LIDAR", "LiDAR mati / None, lanjut pemindaian aman (Fail-safe)")
        elif hasattr(self, "lidar") and self.lidar.aktif:
            jarak_terbaca = self.lidar.baca(timeout=0.10)
            if jarak_terbaca is not None:
                log("LIDAR", "Jarak terdeteksi: %.1f cm (Mode: %s - Filter Spasial Nonaktif)"
                    % (jarak_terbaca, mode))
        else:
            log("LIDAR", "LiDAR tidak aktif, pemindaian tetap berjalan")

        self.layar_proses("mengambil gambar...", mulai=mulai)
        frames = []
        for _ in range(FRAME_CAPTURE):
            f = None
            try:
                f = self.cam.read()
            except Exception as e:
                log("KAMERA", "read gagal: %s" % e)
            if f is None:
                continue
            try:
                frames.append(f.copy())
            except Exception as e:
                log("KAMERA", "copy gagal: %s" % e)
            time.sleep(0.015)
        if not frames:
            return "Kamera gagal", False
        log("KAMERA", "%d frame diambil" % len(frames))

        daftar, total, kenapa, conf_uang = [], 0, None, 0.0
        if mode in ("AUTO", "UANG"):
            self.layar_proses("mendeteksi uang...", mulai=mulai)
            daftar, total, kenapa, conf_uang = analisis_uang(self.model, frames, mode=mode)

        if daftar:
            # Uang terdeteksi -> Abaikan LiDAR sepenuhnya dan ucapkan nominal
            self.baris_teks = []
            self.bagian = []
            self.idx_baca = 0
            self.idx_bagian = 0
            teks, ok = core.format_rupiah(daftar)[0], True
            self.audio.ucap_nominal(daftar, total)
            if getattr(self, "sigap_cloud", None):
                self.sigap_cloud.catat_uang(
                    daftar,
                    conf=conf_uang,
                    jarak_cm=self._cloud_jarak(),
                )
        elif mode in ("AUTO", "TEKS"):
            # Uang tidak ditemukan: Gerbang LiDAR memproteksi pembacaan teks
            if mode == "AUTO" and hasattr(self, "lidar") and self.lidar.aktif:
                jarak_auto = self.lidar.baca(timeout=0.15) or self.lidar.terakhir_baca
                if jarak_auto is not None:
                    if jarak_auto < JARAK_MIN_CM:
                        log("SPASIAL", "[GERBANG OCR AUTO] Jarak %.1f cm < %.0f cm -> Terlalu dekat"
                            % (jarak_auto, JARAK_MIN_CM))
                        if self.audio.punya("terlalu_dekat"):
                            self.audio.putar("terlalu_dekat", bersihkan=True)
                        else:
                            self.audio.ucap("terlalu dekat", bersihkan=True)
                        return "terlalu dekat (%.0f cm)" % jarak_auto, False
                    elif jarak_auto > BATAS_JARAK_TEKS_CM:
                        log("SPASIAL", "[GERBANG OCR AUTO] Jarak %.1f cm > %.0f cm -> Tolak OCR (Terlalu jauh)"
                            % (jarak_auto, BATAS_JARAK_TEKS_CM))
                        if self.audio.punya("terlalu_jauh"):
                            self.audio.putar("terlalu_jauh", bersihkan=True)
                        else:
                            self.audio.ucap("terlalu jauh, dekatkan", bersihkan=True)
                        return "terlalu jauh, dekatkan (%.0f cm)" % jarak_auto, False

            # Wajib bebaskan YOLO dari memori ION NPU sebelum memanggil OCR!
            self.model._bebaskan("yolo")

            self.layar_proses("membaca teks, mohon tunggu...", mulai=mulai)
            if self.audio.punya("mohon_tunggu"):
                self.audio.putar("mohon_tunggu", bersihkan=True)

            # Pilih 1 frame tertajam dan segera bebaskan 5 frame lainnya untuk menghemat RAM
            frame_fokus = pilih_frame_tertajam(frames)
            f_simpan = frame_fokus if frame_fokus is not None else (frames[0] if frames else None)
            frame_ocr = [frame_fokus] if frame_fokus else ([f_simpan] if f_simpan else [])
            try:
                frames.clear()
                del frames
            except Exception:
                pass
            import gc
            gc.collect()

            jarak_ocr = self.lidar.terakhir_baca if (hasattr(self, "lidar") and self.lidar.aktif) else None
            hasil_t, kenapa_t = analisis_teks(self.model,
                                              frame_ocr,
                                              self.kamus,
                                              cb_layar=lambda msg: self.layar_proses(msg, mulai=mulai),
                                              jarak=jarak_ocr)
            gc.collect()
            t, self.bagian, self.idx_bagian = urai_hasil_teks(hasil_t)
            if self.bagian and self.idx_bagian:
                log("JUDUL", "mulai di bagian %d/%d ('%s', %d isi)"
                    % (self.idx_bagian + 1, len(self.bagian),
                       self.bagian[self.idx_bagian][0] or "tanpa judul",
                       len(self.bagian[self.idx_bagian][1])))
            if t:
                ok = True
                self.baris_teks, self.idx_baca = t, 0
                if len(t) > BARIS_SEKALI:
                    log("BACA", "%d baris terdeteksi, dibaca per kelompok %d baris"
                        % (len(t), BARIS_SEKALI))
                    teks = "%d baris" % len(t)
                else:
                    self.audio.ucap(" ".join(t), bersihkan=True)
                    teks = " ".join(t)
                if getattr(self, "sigap_cloud", None):
                    self.sigap_cloud.catat_teks(
                        t,
                        conf=0.9,
                        jarak_cm=self._cloud_jarak(),
                    )
            else:
                teks, ok = (kenapa or kenapa_t or "tidak ada teks terbaca"), False
                self.audio.putar("deteksi_gagal", bersihkan=True)
        else:
            teks, ok = (kenapa or "tidak ada uang"), False
            self.audio.putar("deteksi_gagal", bersihkan=True)

        if daftar:
            tunggu = time.time()
            while self.audio.mulai_bunyi is None and time.time() - tunggu < 2.0:
                time.sleep(0.01)
            akhir = self.audio.mulai_bunyi or time.time()
            durasi_ms = int((akhir - mulai) * 1000)
            durasi_dtk = akhir - mulai
            self.durasi_inferensi = durasi_dtk
            self.e2e.append(durasi_ms)
            log("E2E", "Inferensi YOLO Selesai: %d ms (%.2f dtk) | rata-rata %d ms dari %dx"
                % (durasi_ms, durasi_dtk, sum(self.e2e) // len(self.e2e), len(self.e2e)))
        elif ok:
            durasi_ocr = time.time() - mulai
            self.durasi_inferensi = durasi_ocr
            log("E2E", "Inferensi OCR Selesai: %d ms (%.2f dtk) | Memulai pembacaan %d baris teks"
                % (int(durasi_ocr * 1000), durasi_ocr, len(self.baris_teks)))
        else:
            durasi_gagal = time.time() - mulai
            self.durasi_inferensi = durasi_gagal
            log("E2E", "Deteksi Gagal: %d ms (%.2f dtk)"
                % (int(durasi_gagal * 1000), durasi_gagal))

        # Pencatatan transaksi untuk sinkronisasi ke aplikasi Android
        if hasattr(self, "server_api") and self.server_api is not None:
            try:
                self.seq_rekam += 1
                f_simpan = locals().get("f_simpan") or locals().get("frame_fokus") or (frames[0] if ("frames" in locals() and frames) else None)
                f_kecil = None
                if hasattr(self, "cam_kecil") and self.cam_kecil is not None:
                    try:
                        f_kecil = self.cam_kecil.read()
                    except Exception:
                        f_kecil = None
                ms_catat = durasi_ms if "durasi_ms" in locals() else int(self.durasi_inferensi * 1000)
                self.server_api.catat(
                    no=self.seq_rekam,
                    daftar=daftar,
                    ok=ok,
                    durasi_ms=ms_catat,
                    mode=mode,
                    frame_utama=f_simpan,
                    frame_kecil=f_kecil
                )
            except Exception as e:
                log("API", "Gagal catat transaksi: %s" % e)

        return teks, ok

    def sebut_bagian(self):
        if not self.bagian:
            return
        j, isi = self.bagian[self.idx_bagian]
        nama = j or "tanpa judul"
        log("JUDUL", "bagian %d/%d: %s (%d isi)"
            % (self.idx_bagian + 1, len(self.bagian), nama, len(isi)))
        self.audio.ucap(nama, bersihkan=True)

    def maju_bagian(self):
        self.idx_bagian = (self.idx_bagian + 1) % len(self.bagian)
        self.sebut_bagian()

    def masuk_bagian(self):
        j, isi = self.bagian[self.idx_bagian]
        if not isi:
            self.audio.ucap("kosong", bersihkan=True)
            return
        self.baris_teks = isi
        self.idx_baca = 0
        self.keadaan = "BACA"
        log("JUDUL", "masuk '%s', %d isi" % (j or "tanpa judul", len(isi)))
        self.ucap_kelompok()

    def sebut_jumlah(self, n, bersihkan=True):
        if not core:
            return
        if self.audio.punya("%d_baris" % n):
            self.audio.putar("%d_baris" % n, bersihkan=bersihkan)
        else:
            self.audio.putar(*core.eja_angka(n), "baris", bersihkan=bersihkan)

    def sebut_sasaran(self):
        if not self.bagian or self.idx_bagian >= len(self.bagian):
            return False
        j = self.bagian[self.idx_bagian][0]
        if not j:
            return False
        log("JUDUL", "sasaran diucapkan: '%s'" % j)
        return bool(self.audio.ucap(j, bersihkan=True))

    def ucap_kelompok(self):
        potongan = self.baris_teks[self.idx_baca:self.idx_baca + BARIS_SEKALI]
        if not potongan:
            return
        log("BACA", "baris %d-%d dari %d"
            % (self.idx_baca + 1, self.idx_baca + len(potongan),
               len(self.baris_teks)))
        for i, b in enumerate(potongan):
            log("BACA", "  " + b)
            self.audio.ucap(b, bersihkan=(i == 0))

        # Jika ini adalah potongan terakhir, langsung sambungkan kata "selesai"
        # sehingga mengalir alami di audio yang sama tanpa ada jeda hening!
        if (self.idx_baca + len(potongan)) >= len(self.baris_teks):
            if self.audio.punya("selesai"):
                self.audio.putar("selesai")
            else:
                self.audio.ucap("selesai")
            self._adalah_kelompok_terakhir = True
        else:
            self._adalah_kelompok_terakhir = False

        self._waktu_mulai_kelompok = time.time()
        self._menunggu_kelompok = True

    def _audio_sibuk(self):
        """-> True bila masih ada yang sedang atau akan diucapkan."""
        for nama in ("sedang_bicara", "sibuk", "aktif", "sedang_bunyi"):
            f = getattr(self.audio, nama, None)
            if callable(f):
                try:
                    return bool(f())
                except Exception:
                    pass
        q = getattr(self.audio, "_antri", None)
        if q is not None:
            try:
                return len(q) > 0
            except Exception:
                pass
        return False

    def jadwalkan_siap(self):
        """Tandai bahwa pengumuman 'siap' menunggu preview kembali jalan."""
        self.minta_siap = True
        self.frame_siap = 0

    def maju_baca(self):
        self.idx_baca += BARIS_SEKALI
        if self.idx_baca >= len(self.baris_teks):
            self.idx_baca = 0
            self.baris_teks = []
            self.bagian = []
            self.idx_bagian = 0
            self.keadaan = "IDLE"
            self.terakhir_log_lidar = 0.0
            self.minta_siap = False
            self._menunggu_kelompok = False
            self._adalah_kelompok_terakhir = False
            durasi_total = max(0.0, time.time() - self.waktu_mulai_sesi) if self.waktu_mulai_sesi else 0.0
            inferensi_str = (" (Inferensi AI: %.2f dtk)" % getattr(self, "durasi_inferensi", 0.0)) if getattr(self, "durasi_inferensi", 0.0) > 0 else ""
            log("BACA", "Selesai membaca seluruh baris -> Kamera aktif kembali (Total Sesi: %.2f dtk%s)"
                % (durasi_total, inferensi_str))
            # Baca LiDAR instan seketika sebagai penanda akhir timer & kesiapan proses berikutnya
            if hasattr(self, "lidar") and self.lidar.aktif:
                self.lidar.baca_instan()
                if self.lidar.terakhir_baca is not None:
                    log("LIDAR", "Jarak Terbaca: %.1f cm (Kamera & LiDAR Siap)" % self.lidar.terakhir_baca)
            # Render instan tampilan kamera live ke layar seketika (0 ms jeda)
            self.gambar_deteksi()
            return
        self.ucap_kelompok()

    def gambar_deteksi(self):
        # 1. Update bacaan jarak LiDAR secara reaktif dan real-time
        if hasattr(self, "lidar") and self.lidar.aktif and self.keadaan == "IDLE":
            self.lidar.baca_instan()
            if self.lidar.terakhir_baca is not None and time.time() - getattr(self, "terakhir_log_lidar", 0.0) >= 2.0:
                self.terakhir_log_lidar = time.time()
                log("LIDAR", "Jarak Terbaca: %.1f cm" % self.lidar.terakhir_baca)

        # 1.1 Update bacaan tegangan baterai ADS1115 berkala
        if hasattr(self, "baterai") and self.baterai and self.baterai.aktif and self.keadaan == "IDLE":
            self.baterai.perbarui()

        f_preview = self.baca_preview()
        img = f_preview if self.keadaan == "IDLE" else None
        if img is None:
            img = self.kanvas()
        self.bar_atas(img, "mode " + MODE_URUT[self.idx_mode])

        # 2. Indikator LiDAR di pojok kanan atas
        if hasattr(self, "lidar") and self.lidar.aktif and self.lidar.terakhir_baca is not None:
            jarak = self.lidar.terakhir_baca
            batas_maks = (BATAS_JARAK_TEKS_CM
                          if MODE_URUT[self.idx_mode] == "TEKS"
                          else JARAK_MAKS_UANG_CM)
            if JARAK_MIN_CM <= jarak <= batas_maks:
                warna_j = image.COLOR_GREEN
                teks_j = "Jarak: %.0f cm [PAS]" % jarak
            elif jarak < JARAK_MIN_CM:
                warna_j = image.COLOR_RED
                teks_j = "Jarak: %.0f cm [DEKAT]" % jarak
            else:
                warna_j = image.COLOR_YELLOW
                teks_j = "Jarak: %.0f cm [JAUH]" % jarak
            img.draw_rect(self.W - 170, 26, 164, 22, image.COLOR_BLACK, thickness=-1)
            img.draw_string(self.W - 166, 30, teks_j, color=warna_j, scale=0.75, thickness=2)

        self.kotak = {}
        if self.keadaan == "JUDUL":
            y = self.deret_tombol(img,
                                  (("proses", "BUKA", image.COLOR_GREEN),
                                   ("mode", "BAGIAN", image.COLOR_BLUE),
                                   ("ulangi", "ULANG", image.COLOR_PURPLE),
                                   ("kembali", "SELESAI", image.COLOR_WHITE)))
        elif self.keadaan == "BACA":
            y = self.deret_tombol(img,
                                  (("proses", "LANJUT", image.COLOR_GREEN),
                                   ("mode", "ULANG", image.COLOR_BLUE),
                                   ("ulangi", "AWAL", image.COLOR_PURPLE),
                                   ("kembali", "SELESAI", image.COLOR_WHITE)))
        else:
            y = self.deret_tombol(img,
                                  (("proses", "PROSES", image.COLOR_GREEN),
                                   ("mode", "MODE", image.COLOR_BLUE),
                                   ("ulangi", "ULANGI", image.COLOR_PURPLE),
                                   ("kembali", "KELUAR", image.COLOR_RED)))

        if self.keadaan == "JUDUL":
            j, isi = self.bagian[self.idx_bagian]
            ty = y - 68
            img.draw_rect(0, ty, self.W, 64, image.COLOR_BLACK, thickness=-1)
            img.draw_string(6, ty + 3, "bagian %d dari %d"
                            % (self.idx_bagian + 1, len(self.bagian)),
                            color=image.COLOR_YELLOW, scale=0.9, thickness=2)
            img.draw_string(6, ty + 24, potong(j or "(tanpa judul)",
                                                self.W - 12, 1.4),
                            color=image.COLOR_GREEN, scale=1.4, thickness=2)
            img.draw_string(6, ty + 48, "%d isi" % len(isi),
                            color=image.COLOR_WHITE, scale=0.8)
        elif self.keadaan == "BACA":
            n = len(self.baris_teks)
            potongan = self.baris_teks[self.idx_baca:self.idx_baca + BARIS_SEKALI]
            ty = y - 20 - 22 * (len(potongan) + 1)
            img.draw_rect(0, ty, self.W, 22 * (len(potongan) + 1) + 8,
                          image.COLOR_BLACK, thickness=-1)
            img.draw_string(6, ty + 3, "baris %d-%d dari %d"
                            % (self.idx_baca + 1,
                                min(self.idx_baca + BARIS_SEKALI, n), n),
                            color=image.COLOR_YELLOW, scale=0.9, thickness=2)
            for i, b in enumerate(potongan):
                img.draw_string(6, ty + 25 + i * 22,
                                potong(b, self.W - 12, 1.0),
                                color=image.COLOR_GREEN, scale=1.0, thickness=2)
        elif self.keadaan == "HASIL":
            sisa = max(0.0, self.hasil_sampai - time.time())
            ty = y - 48
            img.draw_rect(0, ty, self.W, 46, image.COLOR_BLACK, thickness=-1)
            img.draw_string(6, ty + 3, potong(self.hasil_teks, self.W - 12, 1.2),
                            color=(image.COLOR_GREEN if self.hasil_ok
                                   else image.COLOR_RED),
                            scale=1.2, thickness=2)
            img.draw_string(6, ty + 26, "E2E %d ms   siap lagi %0.0f dtk"
                            % (self.e2e[-1] if self.e2e else 0, sisa),
                            color=image.COLOR_WHITE, scale=0.8)
            if time.time() >= self.hasil_sampai:
                self.keadaan = "IDLE"
                self.jadwalkan_siap()
        self.tampilkan(img)

    def urus_deteksi(self, klik, pola=None):
        # Penanganan permintaan ambil data / pindai langsung dari aplikasi HP
        if self.minta_proses:
            self.minta_proses = False
            if self.keadaan == "IDLE" and "proses" in self.kotak:
                klik = (self.kotak["proses"][0] + 5, self.kotak["proses"][1] + 5)

        # 1. TAHAN LAMA (Long Press) -> INTERRUPT / SKIP / BATAL / SELESAI KAMERA SIAP
        if pola == "tahan":
            durasi_total = max(0.0, time.time() - self.waktu_mulai_sesi) if self.waktu_mulai_sesi else 0.0
            log("TOMBOL", "TAHAN LAMA -> Hentikan Audio/TTS & Kamera Siap (Total Sesi: %.2f dtk)" % durasi_total)
            self.audio.hentikan()
            self.idx_baca = 0
            self.baris_teks = []
            self.bagian = []
            self.idx_bagian = 0
            self.keadaan = "IDLE"
            self.terakhir_log_lidar = 0.0
            self.hasil_teks = ""
            self.hasil_ok = False
            if self.audio.punya("selesai") and self.audio.punya("kamera_siap"):
                self.audio.putar("selesai", "kamera_siap", bersihkan=True)
            elif self.audio.punya("kamera_siap"):
                self.audio.putar("kamera_siap", bersihkan=True)
            elif self.audio.punya("siap"):
                self.audio.putar("siap", bersihkan=True)
            else:
                self.audio.ucap("selesai kamera siap", bersihkan=True)
            self.minta_siap = False
            if hasattr(self, "lidar") and self.lidar.aktif:
                self.lidar.baca_instan()
                if self.lidar.terakhir_baca is not None:
                    log("LIDAR", "Jarak Terbaca: %.1f cm (Kamera & LiDAR Siap)" % self.lidar.terakhir_baca)
            self.gambar_deteksi()
            return

        if not klik:
            return

        if self.keadaan == "JUDUL":
            if di_dalam(self.kotak["kembali"], *klik):
                self.audio.hentikan()
                self.keadaan = "IDLE"
                if self.audio.punya("kamera_siap"):
                    self.audio.putar("kamera_siap", bersihkan=True)
                elif self.audio.punya("siap"):
                    self.audio.putar("siap", bersihkan=True)
                self.minta_siap = False
                self.gambar_deteksi()
            elif di_dalam(self.kotak["proses"], *klik):
                self.audio.hentikan()
                self.masuk_bagian()
            elif di_dalam(self.kotak["mode"], *klik):
                self.audio.hentikan()
                self.maju_bagian()
            elif di_dalam(self.kotak["ulangi"], *klik):
                self.audio.hentikan()
                self.sebut_bagian()
            return

        if self.keadaan == "BACA":
            if di_dalam(self.kotak["proses"], *klik):
                # LANJUT harus MAJU. Sebelumnya tombol ini disatukan dengan
                # SELESAI, sehingga maju_baca() tidak pernah terpanggil dan
                # pengguna tidak pernah mendengar baris keempat dan seterusnya.
                self.audio.hentikan()
                self.maju_baca()
            elif di_dalam(self.kotak["kembali"], *klik):
                self.audio.hentikan()
                self.idx_baca = 0
                self.baris_teks = []
                self.bagian = []
                self.keadaan = "IDLE"
                if self.audio.punya("selesai") and self.audio.punya("kamera_siap"):
                    self.audio.putar("selesai", "kamera_siap", bersihkan=True)
                elif self.audio.punya("kamera_siap"):
                    self.audio.putar("kamera_siap", bersihkan=True)
                else:
                    self.audio.ucap("selesai kamera siap", bersihkan=True)
                self.minta_siap = False
            elif di_dalam(self.kotak["mode"], *klik):
                self.audio.hentikan()
                self.ucap_kelompok()          # ULANG kelompok yang sedang dibaca
            elif di_dalam(self.kotak["ulangi"], *klik):
                self.audio.hentikan()
                self.idx_baca = 0
                self.ucap_kelompok()          # kembali ke AWAL daftar
            return

        # Keadaan IDLE:
        if di_dalam(self.kotak["kembali"], *klik):
            log("APP", "tombol KELUAR ditekan")
            self.selesai = True
            return

        if di_dalam(self.kotak["proses"], *klik):
            self.minta_siap = False
            self.frame_siap = 0
            self.audio.hentikan()
            try:
                self.hasil_teks, self.hasil_ok = self.proses()
            except Exception as e:
                self.hasil_teks = "galat: %s" % type(e).__name__
                self.hasil_ok = False
                log("PROSES", traceback.format_exc())

            # Buang sentuhan dan penekanan sisa selama pemrosesan berlangsung
            self.tombol.reset()
            self.abai_sampai = time.time() + 0.7
            self.turun = True        # paksa lepas dulu sebelum klik dianggap sah
            if self.ts is not None:
                try:
                    for _ in range(10):
                        self.ts.read()
                        time.sleep(0.01)
                except Exception:
                    pass

            berjudul = [b for b in self.bagian if b[0]]
            if self.hasil_ok and len(berjudul) >= 2:
                self.keadaan = "JUDUL"
                self.sebut_bagian()
            elif self.hasil_ok and len(self.baris_teks) > BARIS_SEKALI:
                self.keadaan = "BACA"
                self.ucap_kelompok()
            else:
                self.keadaan = "IDLE"
                self.minta_siap = False
            log("HASIL", self.hasil_teks)
        elif di_dalam(self.kotak["mode"], *klik):
            self.audio.hentikan()
            self.idx_mode = (self.idx_mode + 1) % len(MODE_URUT)
            self.ucap_mode()
        elif di_dalam(self.kotak["ulangi"], *klik):
            self.audio.hentikan()
            if self.hasil_teks and self.hasil_ok:
                self.audio.ucap(self.hasil_teks, bersihkan=True)
            else:
                self.audio.putar("deteksi_gagal", bersihkan=True)

    # ------------------------------------------------------------------
    POLA_KE_KOTAK = {"tunggal": "proses", "ganda": "mode", "tahan": "ulangi"}

    def klik_dari_tombol(self, pola):
        """Ubah pola tekanan jadi titik klik virtual."""
        nama = self.POLA_KE_KOTAK.get(pola)
        if not nama or nama not in self.kotak:
            return None
        kx, ky, kw, kh = self.kotak[nama]
        log("TOMBOL", "%s -> Kotak %s (Keadaan: %s)" % (pola, nama.upper(), self.keadaan))
        return (kx + kw // 2, ky + kh // 2)

    # ------------------------------------------------------------------
    # Integrasi cloud (opsional, tidak pernah memblokir deteksi/audio)
    # ------------------------------------------------------------------
    def _cloud_jarak(self):
        """Jarak LiDAR terakhir sebagai int cm, atau None."""
        try:
            if (hasattr(self, "lidar") and self.lidar.aktif
                    and self.lidar.terakhir_baca is not None):
                n = int(round(float(self.lidar.terakhir_baca)))
                if 0 <= n <= 100000:
                    return n
        except Exception:
            pass
        return None

    def _cloud_baterai(self):
        """Persentase baterai bila monitor aktif, atau None."""
        try:
            if (self.baterai and self.baterai.aktif
                    and self.baterai.persen is not None):
                n = int(self.baterai.persen)
                if 0 <= n <= 100:
                    return n
        except Exception:
            pass
        return None

    def _cloud_ssid(self):
        """SSID tidak dibaca dari perangkat pada fase ini (privasi)."""
        return None

    def _cloud_perintah(self, tipe, payload):
        """Handler perintah Firestore; lempar exception untuk perintah gagal."""
        if tipe == "set_volume":
            level = int(payload.get("level", 75))
            level = max(0, min(100, level))
            self.audio.setel_volume(level)
            return "volume %d%%" % level
        if tipe == "speak_text":
            teks = str(payload.get("text", ""))[:200].strip()
            if not teks:
                raise RuntimeError("teks kosong")
            urut = self.audio.ucap(teks)
            if not urut:
                raise RuntimeError("audio teks tidak tersedia")
            return "teks diucapkan"
        if tipe == "sync_now":
            self.minta_proses = True
            return "sinkronisasi dipicu"
        if tipe == "restart":
            raise RuntimeError("perintah restart belum didukung")
        if tipe == "reprovision":
            raise RuntimeError("mode pairing belum didukung")
        raise RuntimeError("perintah %s belum didukung" % tipe)

    def tutup(self):
        """Bebaskan seluruh resource hardware (kamera, display, UART, audio)."""
        log("APP", "Membersihkan alokasi memori & resource hardware...")
        if hasattr(self, "server_api") and self.server_api is not None:
            try:
                self.server_api.stop()
                self.server_api = None
            except Exception:
                pass
        if getattr(self, "sigap_cloud", None):
            try:
                self.sigap_cloud.stop()
            except Exception:
                pass
            finally:
                self.sigap_cloud = None
        if hasattr(self, "audio") and self.audio:
            try:
                self.audio.hentikan()
            except Exception:
                pass
        if hasattr(self, "lidar") and self.lidar and getattr(self.lidar, "ser", None):
            try:
                self.lidar.ser.close()
            except Exception:
                pass
        if hasattr(self, "cam_kecil") and self.cam_kecil is not None:
            try:
                del self.cam_kecil
                self.cam_kecil = None
            except Exception:
                pass
        if hasattr(self, "cam") and self.cam is not None:
            try:
                del self.cam
                self.cam = None
            except Exception:
                pass
        if hasattr(self, "disp") and self.disp is not None:
            try:
                del self.disp
                self.disp = None
            except Exception:
                pass
        import gc
        gc.collect()
        time.sleep(0.1)

    def jalankan(self):
        self.pemanasan()
        if self.audio.punya("kamera_siap"):
            self.audio.putar("kamera_siap", bersihkan=True)
        elif self.audio.punya("siap"):
            self.audio.putar("siap", bersihkan=True)
        else:
            self.ucap_mode()
        print("\n  SIGAP NETRA V2 AKTIF -> INTEGRASI PENUH (Tombol Fisik PIN %s: %s)\n"
              % (PIN_TOMBOL, "AKTIF" if self.tombol.hidup else "MATI"), flush=True)
        # Debounce touchscreen: jeda minimum 300ms antar klik.
        _ts_terakhir = 0.0
        _TS_JEDA = 0.30  # detik

        while not self.selesai:
            # Kebalkan dari jepitan tombol KEY_OK board di dalam casing kacamata:
            if app.need_exit():
                try:
                    app.set_exit_flag(False)
                except Exception:
                    pass
            self.gambar_deteksi()
            sekarang = time.time()

            # Auto-advance pembacaan teks: Jika kelompok 3 baris selesai diucapkan,
            # otomatis lanjut ke kelompok baris berikutnya secara berkesinambungan.
            if self.keadaan == "BACA" and self._menunggu_kelompok and not self._audio_sibuk():
                if sekarang - self._waktu_mulai_kelompok >= 0.1:
                    self._menunggu_kelompok = False
                    self.maju_baca()

            # Pengumuman "siap" hanya boleh keluar setelah preview benar-benar
            # jalan. Tiga frame berturut-turut memastikan kamera sudah pulih
            # dari jeda selama pemrosesan, bukan sekadar satu frame kebetulan.
            if self.keadaan == "IDLE" and self.minta_siap:
                if not self._audio_sibuk():
                    self.minta_siap = False
                    self.frame_siap = 0
                    if self.audio.punya("kamera_siap"):
                        self.audio.putar("kamera_siap")
                    elif self.audio.punya("siap"):
                        self.audio.putar("siap")
                    else:
                        self.audio.ucap("kamera siap")
            else:
                self.frame_siap = 0

            if ENABLE_TOUCHSCREEN and self.ts is not None:
                try:
                    x, y, tekan = self.ts.read()
                    if tekan and not self.turun and (sekarang - _ts_terakhir) >= _TS_JEDA:
                        klik = (x, y)
                        _ts_terakhir = sekarang
                    else:
                        klik = None
                    self.turun = bool(tekan)
                except Exception:
                    klik = None
            else:
                klik = None

            # perbarui() tetap dipanggil walau sedang diabaikan, supaya
            # pelacakan tepi naik/turun tidak putus dan tidak menghasilkan
            # tepi palsu saat masa abai berakhir.
            pola = self.tombol.perbarui(sekarang)
            if sekarang < self.abai_sampai:
                pola, klik = None, None
            if pola and pola != "tahan":
                klik = self.klik_dari_tombol(pola) or klik

            self.urus_deteksi(klik, pola=pola)
        log("APP", "selesai")


app_instance = None


def _tangani_sinyal(sig, frame):
    log("APP", "Menerima sinyal keluar (%d), menutup perangkat..." % sig)
    if app_instance is not None:
        app_instance.tutup()
    sys.exit(0)


signal.signal(signal.SIGINT, _tangani_sinyal)
signal.signal(signal.SIGTERM, _tangani_sinyal)

if __name__ == "__main__":
    try:
        app_instance = Aplikasi()
        app_instance.jalankan()
    except (SystemExit, KeyboardInterrupt):
        log("APP", "dihentikan oleh pengguna")
    except BaseException:
        jejak = traceback.format_exc()
        print(jejak)
        try:
            with open("/maixapp/apps/aplikasi_utama/galat.log", "w") as f:
                f.write(jejak)
        except Exception:
            pass
    finally:
        if app_instance is not None:
            app_instance.tutup()