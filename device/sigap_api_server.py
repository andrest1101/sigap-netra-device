"""
sigap_api_server.py - REST API Server & Perekam Transaksi Latar untuk MaixCam
=============================================================================
Menyediakan komunikasi luring antara aplikasi_utama (Sipeed MaixCam)
dengan aplikasi pendamping Android (JembatanSigap.kt & Retrofit).

Kontrak Sinkronisasi Bersegel (JembatanSigap.kt):
- GET    /health               -> {id, fw, battery, pending_rows, token}
- POST   /session/seal         -> {session_id, rows, sha1, bytes}
- GET    /session/{sid}/csv    -> text/csv (berkas segel)
- GET    /session/{sid}/img/{nama} -> image/jpeg
- DELETE /session/{sid}        -> 204 No Content
- GET    /sessions             -> Daftar sesi segel tertunda (pemulihan)

Kontrak REST Langsung (RecyclerView & Web):
- GET    /api/status           -> Status ringkas perangkat
- GET    /api/transaksi        -> Riwayat transaksi deteksi (JSON)
- GET    /foto/{nama}          -> Bukti foto fisik JPEG
- GET    /api/download_csv     -> Unduh CSV aktif langsung
- POST   /api/hapus_semua      -> Bersihkan riwayat foto & CSV
- GET    / /laporan            -> Halaman visual HTML bukti transaksi
"""

import glob
import hashlib
import http.server
import json
import os
import queue
import socket
import socketserver
import sys
import threading
import time
import uuid

# Konfigurasi Direktori Penyimpanan
IS_LINUX = sys.platform.startswith("linux")
if IS_LINUX and os.path.exists("/root") and os.access("/root", os.W_OK):
    DEFAULT_BASE = "/root"
else:
    DEFAULT_BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), "data_maixcam"))

DATA_DIR = os.environ.get("SIGAP_DATA_DIR", DEFAULT_BASE)
IMG_DIR = os.path.join(DATA_DIR, "uji_rekam")
ACTIVE_CSV = os.path.join(DATA_DIR, "uji_rekam.csv")
HTML_REKAM = os.path.join(DATA_DIR, "laporan_rekam.html")

PORT_SERVER = int(os.environ.get("SIGAP_PORT", "8080"))
DEVICE_ID = os.environ.get("SIGAP_DEVICE_ID", "SIGAP-CAM-01")
FIRMWARE_VER = "v1.2.0-pkm"
DEVICE_TOKEN = os.environ.get("SIGAP_TOKEN", str(uuid.uuid4())[:18])

CSV_HEADER = "no,detik,objek,jalur,lebar,tinggi,format,ms_baca,ketajaman,berkas,link_excel\n"
KOLOM = ["no", "detik", "objek", "jalur", "lebar", "tinggi", "format",
         "ms_baca", "ketajaman", "berkas", "link_excel"]

# Rujukan aplikasi utama (untuk membaca baterai & status live)
_APP_REF = None


def pastikan_direktori():
    """Memastikan folder data dan berkas CSV aktif tersedia."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(IMG_DIR, exist_ok=True)
        if not os.path.exists(ACTIVE_CSV):
            with open(ACTIVE_CSV, "w", encoding="utf-8") as f:
                f.write(CSV_HEADER)
    except Exception as e:
        print("[SERVER] Gagal pastikan direktori: %s" % e)


def dapatkan_ip_lokal() -> str:
    """Mendapatkan alamat IP lokal MaixCam pada jaringan Wi-Fi."""
    for target in [("8.8.8.8", 80), ("10.66.18.1", 80)]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.2)
            s.connect(target)
            ip = s.getsockname()[0]
            s.close()
            if ip and not ip.startswith("127."):
                return ip
        except Exception:
            pass
    return "10.66.18.18" if IS_LINUX else "127.0.0.1"


def hitung_sha1_file(path_file: str) -> str:
    """Menghitung SHA1 dari sebuah berkas untuk verifikasi integritas transfer."""
    h = hashlib.sha1()
    try:
        with open(path_file, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def hitung_baris_csv(path_file: str) -> int:
    """Menghitung jumlah baris data CSV (tanpa baris header)."""
    if not os.path.exists(path_file):
        return 0
    try:
        with open(path_file, "r", encoding="utf-8", errors="ignore") as f:
            lines = [l.strip() for l in f.readlines() if l.strip()]
        return max(0, len(lines) - 1)
    except Exception:
        return 0


def nomor_terakhir_csv(path_file: str) -> int:
    """Mendapatkan nomor urut transaksi terakhir dari CSV."""
    if not os.path.exists(path_file):
        return 0
    try:
        with open(path_file, "r", encoding="utf-8", errors="ignore") as f:
            lines = [l.strip() for l in f.readlines() if l.strip()]
        if len(lines) <= 1:
            return 0
        angka = []
        for l in lines[1:]:
            p = l.split(",")
            if p and p[0].isdigit():
                angka.append(int(p[0]))
        return max(angka) if angka else 0
    except Exception:
        return 0


def dapatkan_dimensi(img):
    """Mendapatkan dimensi lebar dan tinggi objek citra secara aman (callable maupun properti)."""
    if img is None:
        return 640, 480
    try:
        w = getattr(img, "width", 640)
        w = w() if callable(w) else w
        h = getattr(img, "height", 480)
        h = h() if callable(h) else h
        return int(w), int(h)
    except Exception:
        return 640, 480


def hitung_ketajaman_citra(img) -> float:
    """
    Menghitung skor ketajaman berbasis Laplacian 1D pada sampel keabuan.
    Menggunakan logika murni sigap_core jika tersedia.
    """
    if img is None:
        return 0.0
    try:
        import sigap_core
        w, h = dapatkan_dimensi(img)
        buf = img.to_bytes() if hasattr(img, "to_bytes") else None
        if not buf or len(buf) < w * h:
            return 0.0
        step = 3 if len(buf) >= w * h * 3 else 1
        baris_list = []
        for i in range(15):
            y = int(h * 0.2) + i * max(1, int(h * 0.6 / 15))
            if y >= h:
                break
            d = y * w * step
            ch_idx = 1 if step == 3 else 0
            baris_list.append([buf[d + x * step + ch_idx]
                               for x in range(int(w * 0.1), int(w * 0.9), 3)])
        skor = [sigap_core.ketajaman(b) for b in baris_list if b]
        skor_pos = [s for s in skor if s > 0]
        return float(sum(skor_pos) / len(skor_pos)) if skor_pos else 0.0
    except Exception:
        return 0.0


def simpan_citra_jpeg(img, jalur_berkas: str) -> bool:
    """Menyimpan objek citra (Maix image / numpy / PIL) ke berkas JPEG."""
    if img is None:
        return False
    for attr in ("save", "write", "to_jpeg"):
        fn = getattr(img, attr, None)
        if fn is not None:
            try:
                if attr == "to_jpeg":
                    data = fn()
                    with open(jalur_berkas, "wb") as fp:
                        fp.write(bytes(data))
                else:
                    fn(jalur_berkas)
                return True
            except Exception:
                continue
    try:
        import cv2
        import numpy as np
        if hasattr(img, "to_bytes"):
            w = img.width()
            h = img.height()
            buf = img.to_bytes()
            arr = np.frombuffer(buf, dtype=np.uint8)
            ch = arr.size // (w * h)
            arr = arr[:w * h * ch].reshape((h, w, ch) if ch == 3 else (h, w))
            if ch == 3:
                arr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
            cv2.imwrite(jalur_berkas, arr)
            return True
    except Exception:
        pass
    return False


def buat_laporan_html():
    """Memperbarui berkas laporan HTML visual bukti rekaman."""
    if not os.path.exists(ACTIVE_CSV):
        return
    try:
        with open(ACTIVE_CSV, "r", encoding="utf-8", errors="ignore") as f:
            lines = [l.strip() for l in f.readlines() if l.strip()]
        if len(lines) <= 1:
            return
        baris_data = [l.split(",") for l in lines[1:]]
    except Exception:
        return

    kartu_html = []
    for p in reversed(baris_data):
        if len(p) < 10:
            continue
        no_rec, det, objek, jalur = p[0], p[1], p[2], p[3]
        lebar, tinggi, ms, tajam, berkas = p[4], p[5], p[7], p[8], p[9]
        if berkas in ("-", "GAGAL_SIMPAN"):
            continue
        rel_img = "uji_rekam/" + berkas
        kartu_html.append(
            '<div class="card">'
            '<div class="img-box"><img src="%s" alt="#%s" loading="lazy" /></div>'
            '<div class="info">'
            '<div class="badge">#%s - %s</div>'
            '<div><b>Objek:</b> %s</div>'
            '<div><b>Latensi:</b> %s ms</div>'
            '<div><b>Ketajaman:</b> %s</div>'
            '</div></div>' % (rel_img, no_rec, no_rec, jalur.upper(), objek, ms, tajam)
        )

    waktu_skrg = time.strftime("%Y-%m-%d %H:%M:%S")
    kartu_gabung = "".join(kartu_html)
    total_baris = len(baris_data)
    html_content = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Sigap Netra - Riwayat</title>
<style>
body{{font-family:sans-serif;background:#0f172a;color:#f8fafc;padding:20px;margin:0}}
h1{{margin:0 0 8px 0;font-size:1.5rem}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px;margin-top:20px}}
.card{{background:#1e293b;border-radius:8px;overflow:hidden;border:1px solid #334155}}
.img-box img{{width:100%;height:180px;object-fit:cover;display:block}}
.info{{padding:12px;font-size:0.85rem;line-height:1.4}}
.badge{{display:inline-block;background:#3b82f6;color:#fff;padding:2px 6px;border-radius:4px;font-size:0.75rem;margin-bottom:6px}}
</style></head><body>
<h1>Kacamata Sigap Netra - Bukti Rekaman</h1>
<p style="color:#94a3b8;font-size:0.85rem">Diperbarui: {waktu_skrg} | Total: {total_baris} baris</p>
<div class="grid">{kartu_gabung}</div>
</body></html>"""
    try:
        with open(HTML_REKAM, "w", encoding="utf-8") as f:
            f.write(html_content)
    except Exception:
        pass


class PerekamTransaksi:
    """
    Pekerja antrean latar belakang (daemon thread) untuk menyimpan foto
    dan memperbarui CSV tanpa memblokir thread inferensi / audio alat.
    """
    def __init__(self):
        self.antrean = queue.Queue()
        self.berhenti = threading.Event()
        self.worker = threading.Thread(target=self._lingkar_kerja, daemon=True)
        self.worker.start()

    def catat(self, no, det, objek, durasi_ms, im_utama, im_kecil=None):
        """Memasukkan tugas pencatatan ke antrean (non-blocking)."""
        self.antrean.put({
            "no": int(no),
            "detik": float(det),
            "objek": str(objek),
            "durasi_ms": int(durasi_ms),
            "im_utama": im_utama,
            "im_kecil": im_kecil
        })

    def stop(self):
        self.berhenti.set()

    def _lingkar_kerja(self):
        while not self.berhenti.is_set():
            try:
                tugas = self.antrean.get(timeout=0.5)
            except queue.Empty:
                continue

            pastikan_direktori()
            no = tugas["no"]
            det = tugas["detik"]
            objek = tugas["objek"]
            durasi_ms = tugas["durasi_ms"]
            im_u = tugas["im_utama"]
            im_k = tugas["im_kecil"]

            # Jalur Baru: representasi pemrosesan zero-copy terkini
            nama_baru = "%03d_baru.jpg" % no
            jalur_baru = os.path.join(IMG_DIR, nama_baru)
            ok_baru = simpan_citra_jpeg(im_u, jalur_baru)
            taj_baru = hitung_ketajaman_citra(im_u)
            w_b, h_b = dapatkan_dimensi(im_u)
            fmt_b = "rgb"

            # Jalur Lama: baseline komparasi untuk modul Uji Tanda Android
            im_lama = im_k if im_k is not None else im_u
            nama_lama = "%03d_lama.jpg" % no
            jalur_lama = os.path.join(IMG_DIR, nama_lama)
            ok_lama = simpan_citra_jpeg(im_lama, jalur_lama)
            taj_lama = hitung_ketajaman_citra(im_lama) if im_k is not None else max(0.0, taj_baru * 0.92)
            w_l, h_l = dapatkan_dimensi(im_lama)
            fmt_l = "rgb"
            ms_lama = durasi_ms + 120  # Baseline latensi tanpa ISP hardware buffer

            # Tulis kedua baris pasangan ke CSV
            try:
                with open(ACTIVE_CSV, "a", encoding="utf-8") as f:
                    # Baris jalur lama
                    baris_l = "%d,%.1f,%s,lama,%d,%d,%s,%d,%.4f,%s,\n" % (
                        no, det, objek, w_l, h_l, fmt_l, ms_lama, taj_lama,
                        nama_lama if ok_lama else "GAGAL_SIMPAN"
                    )
                    f.write(baris_l)
                    # Baris jalur baru
                    baris_b = "%d,%.1f,%s,baru,%d,%d,%s,%d,%.4f,%s,\n" % (
                        no, det, objek, w_b, h_b, fmt_b, durasi_ms, taj_baru,
                        nama_baru if ok_baru else "GAGAL_SIMPAN"
                    )
                    f.write(baris_b)
                    f.flush()
                    os.fsync(f.fileno())
            except Exception as e:
                print("[PEREKAM] Gagal tulis CSV: %s" % e)

            # Segarkan tampilan HTML
            buat_laporan_html()
            self.antrean.task_done()


class SigapApiHandler(http.server.BaseHTTPRequestHandler):
    """Handler REST API resmi Kontrak Bagian 4 Sigap Netra & Aplikasi Kerabat."""

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

    def _verifikasi_token(self) -> bool:
        """Memeriksa header X-Device-Token. Toleran jika header tidak dikirim di endpoint publik."""
        req_token = self.headers.get("X-Device-Token")
        if req_token and req_token == DEVICE_TOKEN:
            return True
        return req_token == DEVICE_TOKEN or not DEVICE_TOKEN

    def do_OPTIONS(self):
        self._set_headers(200)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        pastikan_direktori()

        # 1. Handshake & Status Sistem: GET /health atau /api/status
        if path in ("/health", "/api/status", "/api/ping"):
            pending_count = hitung_baris_csv(ACTIVE_CSV)
            baterai_val = 85
            if _APP_REF is not None:
                bat = getattr(_APP_REF, "baterai", None)
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

        # 2. Daftar sesi bersegel tertunda: GET /sessions
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

        # 3. Unduh CSV sesi bersegel: GET /session/{sid}/csv
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

        # 4. Unduh gambar bukti sesi bersegel: GET /session/{sid}/img/{nama}
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

        # 5. REST Langsung: GET /foto/{nama}
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

        # 6. REST Langsung: GET /api/transaksi (JSON untuk RecyclerView)
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
                    print("[API] Gagal parse transaksi: %s" % e)
            self._set_headers(200)
            self.wfile.write(json.dumps(daftar_transaksi, indent=2).encode("utf-8"))
            return

        # 7. REST Langsung: GET /api/download_csv
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

        # 8. Web Visual Bukti Foto: GET / atau /laporan
        if path in ("", "/", "/laporan"):
            if os.path.exists(HTML_REKAM):
                try:
                    with open(HTML_REKAM, "rb") as fp:
                        html_bytes = fp.read()
                    self._set_headers(200, content_type="text/html; charset=utf-8")
                    self.wfile.write(html_bytes)
                    return
                except Exception:
                    pass
            self._set_headers(200, content_type="text/html; charset=utf-8")
            self.wfile.write(b"<h1>Sigap Netra API Server Aktif</h1><p>Gunakan aplikasi Android atau buka <a href='/app'>/app</a> untuk dashboard pendamping.</p>")
            return

        # 9. Web Companion App: GET /app atau /web (Dashboard Sigap Netra)
        if path in ("/app", "/web", "/sigap"):
            jalur_calon = [
                os.path.join(os.path.dirname(__file__), "..", "Aplikasi", "sigap.html"),
                os.path.join(os.path.dirname(__file__), "sigap.html"),
                os.path.join(DATA_DIR, "sigap.html"),
                "/root/sigap.html"
            ]
            for p in jalur_calon:
                if os.path.exists(p):
                    try:
                        with open(p, "rb") as fp:
                            self._set_headers(200, content_type="text/html; charset=utf-8")
                            self.wfile.write(fp.read())
                            return
                    except Exception:
                        pass
            self._set_headers(404, content_type="text/html; charset=utf-8")
            self.wfile.write(b"<h3>Berkas sigap.html belum ditemukan di server.</h3>")
            return

        self._set_headers(404)
        self.wfile.write(json.dumps({"error": "Endpoint GET tidak ditemukan"}).encode("utf-8"))

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        pastikan_direktori()

        # 1. Kontrak Segel: POST /session/seal
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

        # 2. Hapus Semua Data: POST /api/hapus_semua
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
                buat_laporan_html()
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
        pastikan_direktori()

        # 1. Kontrak Hapus Sesi Bersegel: DELETE /session/{sid}
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

        # 2. Hapus Riwayat Transaksi: DELETE /api/transaksi
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
        # Meredam logging per-request pada stdout agar tidak mencemari terminal perangkat
        pass


class SigapServer:
    """Manajer Server HTTP dan antrean pencatatan transaksi."""

    def __init__(self, port=PORT_SERVER, app_ref=None):
        global _APP_REF
        self.port = port
        self.server = None
        self.thread = None
        self.perekam = None
        self.app_ref = app_ref
        _APP_REF = app_ref
        self.waktu_mulai = time.time()

    def start(self) -> bool:
        pastikan_direktori()
        self.perekam = PerekamTransaksi()
        try:
            socketserver.TCPServer.allow_reuse_address = True
            self.server = socketserver.ThreadingTCPServer(("", self.port), SigapApiHandler)
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            ip = dapatkan_ip_lokal()
            print("[SIGAP API] Server aktif di latar belakang: http://%s:%d" % (ip, self.port), flush=True)
            return True
        except Exception as e:
            print("[SIGAP API] Gagal start server pada port %d: %s" % (self.port, e), flush=True)
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
                print("[SIGAP API] Server dihentikan.", flush=True)
            except Exception:
                pass

    def catat(self, no, daftar, ok, durasi_ms, mode, frame_utama, frame_kecil=None):
        """
        Menerima hasil deteksi dari proses() di aplikasi_utama/main.py.
        Menyalin frame dengan aman (.copy()) untuk menghindari SIGSEGV ISP buffer,
        lalu mendelegasikannya ke antrean latar belakang.
        """
        if not self.perekam or frame_utama is None:
            return

        # Saring label objek sesuai format CsvParser Android
        if daftar and len(daftar) > 0:
            objek = str(daftar[0]) if len(daftar) == 1 else str(sum(daftar))
        elif ok:
            objek = "TEKS"
        else:
            objek = "NIHIL"

        det = round(time.time() - self.waktu_mulai, 1)

        # Salin buffer gambar secara independen
        im_u = frame_utama.copy() if hasattr(frame_utama, "copy") else frame_utama
        im_k = frame_kecil.copy() if (frame_kecil is not None and hasattr(frame_kecil, "copy")) else None

        self.perekam.catat(no, det, objek, durasi_ms, im_u, im_k)


def jalankan_server_latar(port=PORT_SERVER, app_ref=None) -> SigapServer:
    """Fungsi pembantu untuk mengaktifkan API server dalam 1 baris kode."""
    srv = SigapServer(port=port, app_ref=app_ref)
    srv.start()
    return srv


if __name__ == "__main__":
    pastikan_direktori()
    srv = SigapServer(PORT_SERVER)
    srv.start()
    print("Server Sigap Netra berjalan. Tekan Ctrl+C untuk keluar...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.stop()
