"""Jembatan cloud SIGAP-NETRA: MaixCAM -> Cloud Firestore via REST.

Modul ini hanya telemetri: heartbeat, upload deteksi, dan polling perintah.
Fungsi inti (kamera -> YOLO/OCR -> audio -> tombol) TIDAK PERNAH menunggu
modul ini. Hooks hanya memasukkan data ke antrean; semua HTTP berjalan di
thread latar.

Kontrak mengikuti docs/device_protocol.md dan docs/firestore_schema.md:
- Auth Email/Password khusus alat via Identity Toolkit REST.
- `lastSeen`, `createdAt`, `updatedAt` ditulis sebagai Firestore
  `timestampValue` (Opsi B).
- Deteksi dibuat dengan `validationStatus: "pending"`.
- `settings.uploadText` dibaca dari respons heartbeat; bila false, OCR
  tidak pernah dikirim.
- Perintah `restart`/`reprovision` gagal jujur sampai didukung firmware.

Tidak pernah ditulis ke log/Firestore: password, token, email lengkap,
password Wi-Fi, audio, video, gambar penuh, atau lokasi.
"""

import datetime
import importlib.util
import json
import os
import queue
import random
import shutil
import subprocess
import threading
import time
from urllib.parse import quote

try:
    import requests as _requests
except Exception:
    _requests = None

try:
    import urllib.request as _urllib_request
    import urllib.error as _urllib_error
except Exception:
    _urllib_request = None
    _urllib_error = None

FIRESTORE_BASE = "https://firestore.googleapis.com/v1"
IDENTITY_BASE = "https://identitytoolkit.googleapis.com/v1"

HEARTBEAT_DETIK = 60
POLL_PERINTAH_DETIK = 30
UPLOAD_MIN_INTERVAL_DETIK = 5
ANTREAN_MAKS = 32
TIMEOUT_DETIK = 15
BACKOFF_BASE_DETIK = 2
BACKOFF_CAP_DETIK = 300
TAHUN_MINIMAL = 2024
BATAS_FINALISASI = 20


def _log(tag, msg):
    print("[CLOUD %s] %s" % (tag, msg), flush=True)


def _placeholder(v):
    if not isinstance(v, str):
        return True
    s = v.strip()
    if not s:
        return True
    low = s.lower()
    if "??" in s:
        return True
    if low in ("?", "...", "-", "changeme", "change_me", "replace_me",
               "fill_me", "your_password", "password", "secret", "rahasia"):
        return True
    if low.startswith(("your_", "fill_", "replace_", "changeme")):
        return True
    return False


def muat_config():
    """Muat config.py dari folder aplikasi. Return dict valid atau None.

    File dibaca lewat path eksplisit supaya tidak tertimpa config.py lama di
    /root yang bisa menang karena urutan sys.path main.py.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")
    if not os.path.isfile(path):
        _log("CONFIG", "config.py tidak ditemukan; mode offline")
        return None
    try:
        spec = importlib.util.spec_from_file_location("_sigap_cloud_config", path)
        if spec is None or spec.loader is None:
            _log("CONFIG", "config.py tidak bisa dibaca; mode offline")
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        _log("CONFIG", "config.py gagal dimuat (%s); mode offline"
             % type(e).__name__)
        return None

    cfg = getattr(mod, "CONFIG", None)
    if not isinstance(cfg, dict):
        _log("CONFIG", "CONFIG tidak ditemukan di config.py; mode offline")
        return None

    for key in ("project_id", "api_key", "device_id",
                "device_email", "device_password"):
        if _placeholder(cfg.get(key)):
            _log("CONFIG", "config.py belum valid: %s" % key)
            return None

    project_id = str(cfg["project_id"]).strip()
    device_id = str(cfg["device_id"]).strip()
    email = str(cfg["device_email"]).strip()
    password = str(cfg["device_password"])
    api_key = str(cfg["api_key"]).strip()

    if not project_id or "/" in project_id:
        _log("CONFIG", "project_id tidak valid")
        return None
    if not device_id or "/" in device_id or device_id in (".", ".."):
        _log("CONFIG", "device_id tidak valid")
        return None
    if "@" not in email:
        _log("CONFIG", "device_email tidak valid")
        return None
    if len(password) < 6:
        _log("CONFIG", "device_password terlalu pendek")
        return None
    if len(api_key) < 20:
        _log("CONFIG", "api_key tidak valid")
        return None

    name = cfg.get("name")
    if _placeholder(name):
        name = device_id
    model = cfg.get("model")
    if _placeholder(model):
        model = "MaixCAM SG2002"
    firmware = cfg.get("firmware_version")
    if _placeholder(firmware):
        firmware = "v1.2.0-pkm"

    out = {
        "project_id": project_id,
        "api_key": api_key,
        "device_id": device_id,
        "device_email": email,
        "device_password": password,
        "name": str(name).strip(),
        "model": str(model).strip(),
        "firmware_version": str(firmware).strip(),
        "upload_text": bool(cfg.get("upload_text", False)),
        "upload_thumbnails": bool(cfg.get("upload_thumbnails", False)),
    }
    _log("CONFIG", "config.py valid untuk device %s" % device_id)
    return out


class _Http(object):
    """POST/PATCH JSON minimal: requests bila ada, urllib bila tidak."""

    def __init__(self, timeout=TIMEOUT_DETIK):
        self.timeout = timeout

    def post(self, url, payload, headers=None):
        return self._kirim("POST", url, payload, headers)

    def patch(self, url, payload, headers=None):
        return self._kirim("PATCH", url, payload, headers)

    def _kirim(self, method, url, payload, headers):
        body = json.dumps(payload).encode("utf-8")
        heads = dict(headers or {})
        heads["Content-Type"] = "application/json"
        if _requests is not None:
            resp = _requests.request(
                method, url, data=body, headers=heads, timeout=self.timeout
            )
            return resp.status_code, resp.text
        if _urllib_request is None:
            raise RuntimeError("tidak ada pustaka HTTP")
        req = _urllib_request.Request(
            url, data=body, headers=heads, method=method
        )
        try:
            with _urllib_request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "ignore")
        except _urllib_error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "ignore")


def _sekarang_dt():
    return datetime.datetime.now(datetime.timezone.utc)


def _timestamp_value(dt=None):
    if dt is None:
        dt = _sekarang_dt()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return {"timestampValue": dt.astimezone(
        datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}


def _encode_value(v):
    if v is None:
        return {"nullValue": None}
    if isinstance(v, bool):
        return {"booleanValue": v}
    if isinstance(v, int):
        return {"integerValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, str):
        return {"stringValue": v}
    if isinstance(v, datetime.datetime):
        return _timestamp_value(v)
    if isinstance(v, dict):
        return {"mapValue": {
            "fields": {k: _encode_value(x) for k, x in v.items()}}}
    if isinstance(v, (list, tuple)):
        return {"arrayValue": {
            "values": [_encode_value(x) for x in v]}}
    raise TypeError("tipe tidak didukung: %s" % type(v).__name__)


def _decode_value(x):
    if not isinstance(x, dict):
        return None
    if "stringValue" in x:
        return x["stringValue"]
    if "integerValue" in x:
        try:
            return int(x["integerValue"])
        except Exception:
            return None
    if "doubleValue" in x:
        try:
            return float(x["doubleValue"])
        except Exception:
            return None
    if "booleanValue" in x:
        return bool(x["booleanValue"])
    if "nullValue" in x:
        return None
    if "timestampValue" in x:
        return x["timestampValue"]
    if "mapValue" in x:
        return _decode_map(x["mapValue"])
    if "arrayValue" in x:
        vals = (x["arrayValue"] or {}).get("values") or []
        return [_decode_value(v) for v in vals]
    return None


def _decode_map(v):
    if not isinstance(v, dict):
        return {}
    fields = (v.get("mapValue") or {}).get("fields", {})
    return {k: _decode_value(x) for k, x in fields.items()}


def _doc_name(project_id, device_id, *parts):
    base = "projects/%s/databases/(default)/documents/devices/%s" % (
        project_id, device_id)
    if parts:
        base += "/" + "/".join(str(p) for p in parts)
    return base


def _to_int_or_none(v, lo=None, hi=None):
    if v is None:
        return None
    try:
        n = int(round(float(v)))
    except Exception:
        return None
    if lo is not None and n < lo:
        return None
    if hi is not None and n > hi:
        return None
    return n


def _clamp01(v):
    try:
        f = float(v)
    except Exception:
        return 0.0
    if f < 0.0:
        return 0.0
    if f > 1.0:
        return 1.0
    return f


def _backoff(n):
    try:
        n = max(0, min(int(n), 10))
    except Exception:
        n = 0
    delay = min(BACKOFF_CAP_DETIK, BACKOFF_BASE_DETIK * (2 ** n))
    return random.uniform(0, delay)


class SigapCloud(object):
    """Satu instance per perangkat. Semua method publik aman dipanggil
    dari thread utama karena hooks hanya menyentuh antrean."""

    def __init__(self, config, hooks):
        self.cfg = dict(config)
        self.hooks = dict(hooks or {})
        self.http = _Http()
        self._stop = threading.Event()
        self._kunci = threading.Lock()
        self._kunci_auth = threading.Lock()
        self._kunci_id = threading.Lock()
        self._kunci_ntp = threading.Lock()
        self._antrean = queue.Queue(maxsize=ANTREAN_MAKS)
        self._thread_utama = None
        self._thread_upload = None
        self._id_token = None
        self._token_exp = 0.0
        self._auth_gagal = 0
        self._auth_next = 0.0
        self._upload_terakhir = 0.0
        self._upload_counter = 0
        self._heartbeat_terakhir = 0.0
        self._poll_terakhir = 0.0
        self._ntp_next = 0.0
        self._upload_text = bool(self.cfg.get("upload_text", False))
        self._upload_thumbnails = bool(
            self.cfg.get("upload_thumbnails", False))
        self._boot_count = 0
        self._state_path = self._tentukan_state_path()
        self._hasil_lokal = {}

    @staticmethod
    def _tentukan_state_path():
        app_dir = os.path.dirname(os.path.abspath(__file__))
        for p in ("/root/.sigap_cloud_state.json",
                  os.path.join(app_dir, ".cloud_state.json")):
            try:
                d = os.path.dirname(p)
                if d and not os.path.isdir(d):
                    continue
                return p
            except Exception:
                pass
        return os.path.join(app_dir, ".cloud_state.json")

    def _muat_state(self):
        try:
            with open(self._state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    def _simpan_state(self, data):
        try:
            tmp = self._state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._state_path)
        except Exception:
            pass

    @staticmethod
    def _baca_boot_id():
        try:
            with open("/proc/sys/kernel/random/boot_id", "r") as f:
                return f.read().strip() or None
        except Exception:
            return None

    @staticmethod
    def _baca_uptime():
        try:
            with open("/proc/uptime", "r") as f:
                return float(f.read().split()[0])
        except Exception:
            return None

    def _siapkan_boot_count(self):
        state = self._muat_state()
        boot_id = self._baca_boot_id()
        uptime = self._baca_uptime()
        try:
            count = int(state.get("boot_count", 0) or 0)
        except Exception:
            count = 0
        state_boot = state.get("boot_id")
        if boot_id:
            if boot_id != state_boot:
                count += 1
                state["boot_id"] = boot_id
        else:
            if state_boot != "uptime" or (
                    uptime is not None and uptime < 120):
                count += 1
                state["boot_id"] = "uptime"
        if count < 1:
            count = 1
        self._boot_count = count
        state["boot_count"] = count
        self._simpan_state(state)

    # ---------- siklus hidup ----------

    def start(self):
        if self._thread_utama is not None and self._thread_utama.is_alive():
            return
        self._stop.clear()
        self._siapkan_boot_count()
        self._thread_upload = threading.Thread(
            target=self._worker_upload, name="sigap-cloud-upload", daemon=True
        )
        self._thread_utama = threading.Thread(
            target=self._worker_utama, name="sigap-cloud-main", daemon=True
        )
        self._thread_upload.start()
        self._thread_utama.start()
        _log("INIT", "thread cloud aktif (device=%s, boot=%d)"
             % (self.cfg.get("device_id"), self._boot_count))

    def stop(self):
        if self._thread_utama is None and self._thread_upload is None:
            self._stop.set()
            return
        self._stop.set()
        try:
            self._antrean.put_nowait(None)
        except queue.Full:
            try:
                self._antrean.get_nowait()
                self._antrean.task_done()
            except Exception:
                pass
            try:
                self._antrean.put_nowait(None)
            except Exception:
                pass
        for t in (self._thread_upload, self._thread_utama):
            if t is not None:
                try:
                    t.join(timeout=2.0)
                except Exception:
                    pass
        self._thread_upload = None
        self._thread_utama = None
        _log("STOP", "thread cloud dihentikan")

    # ---------- API untuk main.py ----------

    def catat_uang(self, daftar_nominal, conf=1.0, jarak_cm=None):
        """Hook setelah YOLO sukses + bicara. Tidak pernah melempar."""
        try:
            if not daftar_nominal:
                return
            total = sum(int(n) for n in daftar_nominal)
            if total <= 0:
                return
            self._masukkan({
                "type": "money",
                "amount": total,
                "currency": "IDR",
                "confidence": _clamp01(conf),
                "distanceCm": _to_int_or_none(jarak_cm, 0, 100000),
                "ocrText": None,
                "ts": time.time(),
            })
        except Exception as e:
            _log("HOOK", "catat_uang diabaikan (%s)" % type(e).__name__)

    def catat_teks(self, baris_list, conf=0.9, jarak_cm=None):
        """Hook setelah OCR sukses + bicara. Tidak pernah melempar."""
        try:
            if not self._upload_text:
                return
            baris = []
            for x in (baris_list or []):
                if x is None:
                    continue
                s = str(x).strip()
                if s:
                    baris.append(s)
            teks = "\n".join(baris).strip()
            if not teks:
                return
            self._masukkan({
                "type": "text",
                "amount": None,
                "currency": "IDR",
                "confidence": _clamp01(conf),
                "distanceCm": _to_int_or_none(jarak_cm, 0, 100000),
                "ocrText": teks[:500],
                "ts": time.time(),
            })
        except Exception as e:
            _log("HOOK", "catat_teks diabaikan (%s)" % type(e).__name__)

    def _masukkan(self, item):
        if self._stop.is_set():
            return
        try:
            self._antrean.put_nowait(item)
            return
        except queue.Full:
            pass
        try:
            self._antrean.get_nowait()
            self._antrean.task_done()
        except Exception:
            pass
        try:
            self._antrean.put_nowait(item)
        except Exception:
            _log("QUEUE", "antrean penuh; deteksi dibuang")

    # ---------- auth ----------

    @staticmethod
    def _jam_waras():
        try:
            return time.gmtime().tm_year >= TAHUN_MINIMAL
        except Exception:
            return False

    def _auth_header(self):
        now = time.time()
        with self._kunci:
            if self._id_token and now < self._token_exp - 60:
                return {"Authorization": "Bearer " + self._id_token}
            if now < self._auth_next:
                return None
        if not self._jam_waras():
            self._coba_ntp()
            if not self._jam_waras():
                return None
        with self._kunci_auth:
            now = time.time()
            with self._kunci:
                if self._id_token and now < self._token_exp - 60:
                    return {"Authorization": "Bearer " + self._id_token}
                if now < self._auth_next:
                    return None
            return self._login()

    def _login(self):
        url = "%s/accounts:signInWithPassword?key=%s" % (
            IDENTITY_BASE, self.cfg.get("api_key", ""))
        try:
            code, text = self.http.post(url, {
                "email": self.cfg.get("device_email"),
                "password": self.cfg.get("device_password"),
                "returnSecureToken": True,
            })
        except Exception as e:
            self._catat_auth_gagal()
            _log("AUTH", "gagal koneksi (%s)" % type(e).__name__)
            return None
        if code != 200:
            self._catat_auth_gagal()
            _log("AUTH", "gagal HTTP %s" % code)
            return None
        try:
            data = json.loads(text or "{}")
            token = data["idToken"]
            expires = int(data.get("expiresIn", "3600"))
        except Exception:
            self._catat_auth_gagal()
            _log("AUTH", "respons login tidak valid")
            return None
        with self._kunci:
            self._id_token = token
            self._token_exp = time.time() + expires
            self._auth_gagal = 0
            self._auth_next = 0.0
        _log("AUTH", "login OK")
        return {"Authorization": "Bearer " + token}

    def _catat_auth_gagal(self):
        with self._kunci:
            self._auth_gagal += 1
            n = self._auth_gagal
            wait = _backoff(n)
            self._auth_next = time.time() + wait

    def _coba_ntp(self):
        if time.time() < self._ntp_next:
            return
        if not self._kunci_ntp.acquire(False):
            return
        try:
            if time.time() < self._ntp_next:
                return
            self._ntp_next = time.time() + 300.0
            path = shutil.which("ntpd") or "/usr/sbin/ntpd"
            if not path or not os.path.exists(path):
                _log("JAM", "ntpd tidak tersedia; cloud menunggu jam benar")
                return
            _log("JAM", "jam belum sinkron; mencoba NTP")
            try:
                subprocess.run(
                    [path, "-q", "-g", "-p", "pool.ntp.org"],
                    timeout=12,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except subprocess.TimeoutExpired:
                _log("JAM", "NTP timeout")
            except Exception as e:
                _log("JAM", "NTP gagal (%s)" % type(e).__name__)
            if self._jam_waras():
                _log("JAM", "jam tersinkron")
            else:
                _log("JAM", "jam belum sinkron; cloud ditunda")
        finally:
            try:
                self._kunci_ntp.release()
            except Exception:
                pass

    # ---------- heartbeat ----------

    def _serap_settings(self, text):
        try:
            data = json.loads(text or "{}")
            fs = data.get("fields") or {}
            settings = ((fs.get("settings") or {}).get(
                "mapValue") or {}).get("fields", {})
            ut = (settings.get("uploadText") or {}).get("booleanValue")
            if isinstance(ut, bool):
                self._upload_text = ut
            utb = (settings.get("uploadThumbnails") or {}).get(
                "booleanValue")
            if isinstance(utb, bool):
                self._upload_thumbnails = utb
        except Exception:
            pass

    def _heartbeat(self):
        if self._stop.is_set():
            return
        heads = self._auth_header()
        if not heads:
            return
        sekarang = _sekarang_dt()
        fields = {
            "lastSeen": _timestamp_value(sekarang),
            "updatedAt": _timestamp_value(sekarang),
            "bootCount": {"integerValue": str(int(self._boot_count))},
        }
        fields["model"] = {
            "stringValue": self.cfg.get("model", "MaixCAM SG2002")}
        fields["firmwareVersion"] = {
            "stringValue": self.cfg.get("firmware_version", "v1.2.0-pkm")}
        bat = None
        try:
            fn = self.hooks.get("get_battery_pct")
            if fn:
                bat = _to_int_or_none(fn(), 0, 100)
        except Exception:
            bat = None
        fields["batteryPct"] = ({"integerValue": str(bat)}
                                if bat is not None else {"nullValue": None})
        ssid = None
        try:
            fn = self.hooks.get("get_wifi_ssid")
            if fn:
                ssid = fn()
        except Exception:
            ssid = None
        if isinstance(ssid, str) and ssid.strip() and "??" not in ssid:
            fields["wifiSsid"] = {"stringValue": ssid.strip()[:64]}
        name = _doc_name(self.cfg["project_id"], self.cfg["device_id"])
        url = "%s/%s?%s" % (
            FIRESTORE_BASE, name,
            "&".join("updateMask.fieldPaths=%s" % k
                     for k in sorted(fields)),
        )
        try:
            code, text = self.http.patch(url, {"fields": fields}, heads)
        except Exception as e:
            _log("HEARTBEAT", "gagal koneksi (%s)" % type(e).__name__)
            return
        if code in (200, 201):
            self._serap_settings(text)
            self._heartbeat_terakhir = time.time()
            _log("HEARTBEAT", "OK")
            return
        if code == 401:
            with self._kunci:
                self._id_token = None
                self._token_exp = 0.0
                self._auth_next = 0.0
            _log("HEARTBEAT", "token ditolak; login ulang")
            return
        if code == 403:
            _log("HEARTBEAT", "ditolak 403 (cek authUid/members/settings)")
            return
        if code == 404:
            _log("HEARTBEAT", "dokumen device tidak ditemukan")
            return
        _log("HEARTBEAT", "gagal HTTP %s" % code)

    # ---------- upload deteksi ----------

    @staticmethod
    def _tahun_waras(ts):
        try:
            return time.gmtime(float(ts)).tm_year >= TAHUN_MINIMAL
        except Exception:
            return False

    def _upload_detection(self, item):
        if self._stop.is_set():
            return
        heads = self._auth_header()
        if not heads:
            return
        now = time.time()
        ts = item.get("ts") or now
        if not self._tahun_waras(ts):
            ts = now
        dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
        fields = {
            "type": {"stringValue": item["type"]},
            "amount": ({"integerValue": str(int(item["amount"]))}
                       if item.get("amount") is not None
                       else {"nullValue": None}),
            "currency": {"stringValue": "IDR"},
            "confidence": {"doubleValue": _clamp01(
                item.get("confidence", 0.0))},
            "distanceCm": ({"integerValue": str(int(item["distanceCm"]))}
                           if item.get("distanceCm") is not None
                           else {"nullValue": None}),
            "ocrText": ({"stringValue": str(item["ocrText"])[:500]}
                        if item.get("ocrText") is not None
                        else {"nullValue": None}),
            "thumbnailId": {"nullValue": None},
            "validationStatus": {"stringValue": "pending"},
            "createdAt": _timestamp_value(dt),
        }
        with self._kunci_id:
            self._upload_counter = (self._upload_counter + 1) % 10000
            det_id = "det_%d_%04d" % (
                int(now * 1000), self._upload_counter)
        parent = _doc_name(self.cfg["project_id"], self.cfg["device_id"],
                           "detections")
        url = "%s/%s?documentId=%s" % (
            FIRESTORE_BASE, parent, quote(det_id, safe=""))
        try:
            code, text = self.http.post(url, {"fields": fields}, heads)
        except Exception as e:
            _log("UPLOAD", "gagal koneksi (%s)" % type(e).__name__)
            return
        if code in (200, 201):
            _log("UPLOAD", "%s OK" % item.get("type"))
            return
        if code == 409:
            return
        if code == 401:
            with self._kunci:
                self._id_token = None
                self._token_exp = 0.0
                self._auth_next = 0.0
            _log("UPLOAD", "token ditolak")
            return
        if code == 403:
            _log("UPLOAD", "ditolak 403 (cek authUid/rules)")
            return
        _log("UPLOAD", "gagal HTTP %s" % code)

    # ---------- polling perintah ----------

    def _poll_perintah(self):
        if self._stop.is_set():
            return
        heads = self._auth_header()
        if not heads:
            return
        parent = _doc_name(self.cfg["project_id"], self.cfg["device_id"])
        url = "%s/%s:runQuery" % (FIRESTORE_BASE, parent)
        query = {
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
                                    {"stringValue": "sent"},
                                    {"stringValue": "acked"},
                                ]
                            }
                        },
                    }
                },
                "orderBy": [{
                    "field": {"fieldPath": "createdAt"},
                    "direction": "ASCENDING",
                }],
                "limit": 5,
            }
        }
        try:
            code, text = self.http.post(url, query, heads)
        except Exception as e:
            _log("POLL", "gagal koneksi (%s)" % type(e).__name__)
            return
        if code == 401:
            with self._kunci:
                self._id_token = None
                self._token_exp = 0.0
                self._auth_next = 0.0
            _log("POLL", "token ditolak")
            return
        if code == 403:
            _log("POLL", "ditolak 403 (device harus member untuk baca commands)")
            return
        if code != 200:
            _log("POLL", "gagal HTTP %s" % code)
            return
        try:
            rows = json.loads(text or "[]")
        except Exception:
            _log("POLL", "respons tidak valid")
            return
        if not isinstance(rows, list):
            _log("POLL", "respons bukan list")
            return
        prefix = "/devices/%s/commands/" % self.cfg["device_id"]
        for row in rows:
            try:
                doc = row.get("document") or {}
                doc_name = doc.get("name", "")
                if prefix not in doc_name:
                    continue
                fs = doc.get("fields", {})
                status = _decode_value(fs.get("status"))
                if status not in ("pending", "sent", "acked"):
                    continue
                ctype = _decode_value(fs.get("type"))
                payload = _decode_map(fs.get("payload"))
                self._tangani_perintah(doc_name, status, ctype, payload, heads)
            except Exception as e:
                _log("POLL", "baris gagal (%s)" % type(e).__name__)
        self._poll_terakhir = time.time()

    # ---------- status perintah ----------

    def _set_status(self, doc_name, status_lama, status_baru, note, heads):
        urutan = {"pending": 0, "sent": 1, "acked": 2,
                  "done": 3, "failed": 4}
        if status_baru not in urutan:
            return False
        if urutan.get(status_lama, -1) >= urutan[status_baru]:
            return False
        fields = {
            "status": {"stringValue": status_baru},
            "updatedAt": _timestamp_value(_sekarang_dt()),
        }
        if note is not None:
            teks = str(note).replace("\n", " ").replace("\r", " ").strip()[:200]
            if teks:
                fields["resultNote"] = {"stringValue": teks}
        if status_baru == "failed" and "resultNote" not in fields:
            fields["resultNote"] = {"stringValue": "gagal tanpa keterangan"}
        url = "%s/%s?%s" % (
            FIRESTORE_BASE, doc_name,
            "&".join("updateMask.fieldPaths=%s" % k
                     for k in sorted(fields)),
        )
        try:
            code, text = self.http.patch(url, {"fields": fields}, heads)
        except Exception as e:
            _log("CMD", "status %s gagal koneksi (%s)"
                 % (status_baru, type(e).__name__))
            return False
        if code == 200:
            return True
        if code == 401:
            with self._kunci:
                self._id_token = None
                self._token_exp = 0.0
                self._auth_next = 0.0
            return False
        _log("CMD", "status %s gagal HTTP %s" % (status_baru, code))
        return False

    def _tangani_perintah(self, doc_name, status, ctype, payload, heads):
        _log("CMD", "diterima %s (%s)" % (ctype, status))

        # Status acked: hasil sudah dihitung sebelumnya. Finalisasi ulang
        # bila respons sebelumnya hilang; jangan eksekusi dua kali.
        if status == "acked":
            hasil = self._hasil_lokal.pop(doc_name, None)
            if hasil is None:
                self._set_status(
                    doc_name, "acked", "failed",
                    "hasil tidak diketahui (perangkat restart)", heads)
                return
            status_baru, note = hasil
            if self._set_status(doc_name, "acked", status_baru, note, heads):
                return
            self._hasil_lokal[doc_name] = hasil
            return

        if status == "pending":
            if not self._set_status(doc_name, "pending", "sent",
                                    None, heads):
                return
        if not self._set_status(doc_name, "sent", "acked", None, heads):
            return

        fn = self.hooks.get("on_command")
        if not fn:
            note = "handler tidak tersedia"
            self._hasil_lokal[doc_name] = ("failed", note)
            if self._set_status(doc_name, "acked", "failed", note, heads):
                self._hasil_lokal.pop(doc_name, None)
            return
        try:
            note = fn(ctype, payload)
            if ctype == "sync_now":
                self._heartbeat()
            self._hasil_lokal[doc_name] = ("done", note)
            if self._set_status(doc_name, "acked", "done", note, heads):
                self._hasil_lokal.pop(doc_name, None)
        except Exception as e:
            pesan = str(e).strip() or type(e).__name__
            note = "gagal: %s" % pesan[:180]
            self._hasil_lokal[doc_name] = ("failed", note)
            if self._set_status(doc_name, "acked", "failed", note, heads):
                self._hasil_lokal.pop(doc_name, None)
        while len(self._hasil_lokal) > BATAS_FINALISASI:
            try:
                self._hasil_lokal.pop(next(iter(self._hasil_lokal)))
            except Exception:
                break

    # ---------- worker ----------

    def _worker_utama(self):
        hb_next = 0.0
        poll_next = 0.0
        while not self._stop.is_set():
            now = time.time()
            if now >= hb_next:
                self._heartbeat()
                hb_next = time.time() + HEARTBEAT_DETIK
            if self._stop.is_set():
                break
            if now >= poll_next:
                self._poll_perintah()
                poll_next = time.time() + POLL_PERINTAH_DETIK
            self._stop.wait(1.0)

    def _worker_upload(self):
        while not self._stop.is_set():
            try:
                item = self._antrean.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                if item is None:
                    break
                if self._stop.is_set():
                    break
                now = time.time()
                if now - self._upload_terakhir < UPLOAD_MIN_INTERVAL_DETIK:
                    _log("THROTTLE", "deteksi dijatuhkan (interval 5 dtk)")
                    continue
                self._upload_terakhir = now
                self._upload_detection(item)
            except Exception as e:
                _log("UPLOAD", "error (%s)" % type(e).__name__)
            finally:
                try:
                    self._antrean.task_done()
                except Exception:
                    pass
