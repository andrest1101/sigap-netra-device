"""
sigap_core.py - Logika murni Sigap Netra (TANPA dependensi maix)
=================================================================
Semua algoritma yang bisa salah tapi TIDAK butuh hardware ditaruh di sini,
supaya bisa diuji di PC. delta2.py mengimpor modul ini.

Isi:
  - iou / dedup_boxes      : cegah satu lembar uang terhitung dua kali
  - vote_notes             : temporal voting lintas frame
  - baca_urutan            : urutkan kotak OCR kiri->kanan, atas->bawah
  - suku_kata              : pemenggal suku kata Bahasa Indonesia
  - rencana_ucap           : tangga fallback audio (frasa->kata->suku kata->huruf)
  - format_rupiah          : angka -> nama file wav / teks

Jalankan `python sigap_core.py` untuk menjalankan self-test.
"""

# Dinaikkan setiap kali ada fungsi baru. Skrip di device memeriksa ini saat
# start supaya salinan lama ketahuan langsung - bukan lewat AttributeError
# di tengah jalan yang membingungkan.
VERSI = "2026-09-07a"

# Yang boleh diandalkan skrip lain. Kalau menambah fungsi publik baru,
# masukkan ke sini dan naikkan VERSI.
DISEDIAKAN = (
    "vote_kata", "gabung_mirip", "harga_k",
    "baca_baris", "vote_baris", "pisah_harga",
    "dedup_teks", "pecah_kolom", "Kamus", "mutu_bacaan",
    "ketajaman", "pilih_tertajam", "tinggi_teks",
    "baca_baris_info", "kenali_judul", "kelompokkan_bagian",
    "pisah_nama_tempat", "KATA_JUDUL", "bagian_terkaya",
    "bagian_dituju", "nilai_bagian",
    "nominal_dari_label", "eja_angka", "iou", "dedup_boxes", "vote_notes",
    "batas_lembar",
    "baca_urutan", "suku_kata", "rencana_ucap", "format_rupiah",
    "TEKS_NOMINAL", "muat_kamus", "jarak_edit", "perbaiki_angka",
    "koreksi_kata", "koreksi_baris", "vote_teks", "PantauBaterai",
    "PolaTombol",
    "harga_sah", "harga_layak_diucapkan", "kunci_harga", "baris_sampah", "saring_sampah",
    "gabung_baris_kembar", "urut_dari_bagian", "siapkan_ucapan",
    "LAFAL_SERAPAN",
)


def periksa(*wajib):
    """
    Panggil di awal skrip device. Mengembalikan daftar yang HILANG.

    Dipakai supaya pesan gagalnya jelas: 'unggah ulang sigap_core.py',
    bukan AttributeError di baris ke-380.
    """
    return [n for n in wajib if not hasattr(sys.modules[__name__], n)]


import sys  # noqa: E402  (dipakai oleh periksa di atas)


# ======================================================================
# 1. NOMINAL
# ======================================================================
NOMINAL_VALID = [1000, 2000, 5000, 10000, 20000, 50000, 100000]

TEKS_NOMINAL = {
    1000: "seribu rupiah",
    2000: "dua ribu rupiah",
    5000: "lima ribu rupiah",
    10000: "sepuluh ribu rupiah",
    20000: "dua puluh ribu rupiah",
    50000: "lima puluh ribu rupiah",
    100000: "seratus ribu rupiah",
}

SATUAN = ["", "satu", "dua", "tiga", "empat", "lima",
          "enam", "tujuh", "delapan", "sembilan"]


def nominal_dari_label(label):
    """
    Ubah label kelas model jadi angka nominal.

    Mendukung skema 28-kelas:  'rp50000_2022_belakang' -> 50000
    dan skema lama 7-kelas:    '50000'                 -> 50000
    Mengembalikan None kalau label bukan uang (mis. kelas 'teks').
    """
    if label is None:
        return None
    s = str(label).strip().lower()
    if s.startswith("rp"):
        s = s[2:]
    s = s.split("_")[0]
    s = "".join(ch for ch in s if ch.isdigit())
    if not s:
        return None
    n = int(s)
    return n if n in NOMINAL_VALID else None


def eja_angka(n):
    """
    Angka -> daftar kata Indonesia. Dipakai untuk mengucapkan TOTAL
    yang tidak punya file wav sendiri (mis. 17000 -> tujuh belas ribu).
    """
    if n == 0:
        return ["nol"]
    kata = []

    def ratusan(x):
        out = []
        if x >= 100:
            r = x // 100
            out += ["seratus"] if r == 1 else [SATUAN[r], "ratus"]
            x %= 100
        if x >= 20:
            out += [SATUAN[x // 10], "puluh"]
            x %= 10
            if x:
                out += [SATUAN[x]]
        elif x >= 12:
            out += [SATUAN[x - 10], "belas"]
        elif x == 11:
            out += ["sebelas"]
        elif x == 10:
            out += ["sepuluh"]
        elif x >= 1:
            out += [SATUAN[x]]
        return out

    if n >= 1_000_000:
        juta = n // 1_000_000
        kata += ratusan(juta) + ["juta"]
        n %= 1_000_000
    if n >= 1000:
        ribu = n // 1000
        kata += (["seribu"] if ribu == 1 else ratusan(ribu) + ["ribu"])
        n %= 1000
    if n:
        kata += ratusan(n)
    return kata


# ======================================================================
# 2. ANTI DOUBLE-COUNT
# ======================================================================
def iou(a, b):
    """Intersection-over-Union dua kotak (x, y, w, h)."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x1, y1 = max(ax, bx), max(ay, by)
    x2, y2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if x2 <= x1 or y2 <= y1:
        return 0.0
    inter = (x2 - x1) * (y2 - y1)
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def dedup_boxes(dets, iou_th=0.45, iou_beda_th=0.70):
    """
    Buang kotak yang saling tumpang tindih pada SATU frame.

    - Kotak dengan NOMINAL SAMA: gunakan iou_th (0.45) ketat untuk mencegah
      satu lembar terlipat/lecek dihitung dua kali (phantom money).
    - Kotak dengan NOMINAL BERBEDA: gunakan iou_beda_th (0.70) longgar karena
      dua lembar nominal berbeda yang beririsan/berdampingan adalah dua
      lembar fisik yang sah dan harus dijumlahkan.

    dets: list dict {nominal, conf, box}
    """
    out = []
    for d in sorted(dets, key=lambda x: -x["conf"]):
        lolos = True
        for k in out:
            nilai_iou = iou(d["box"], k["box"])
            ambang = iou_th if d.get("nominal") == k.get("nominal") else iou_beda_th
            if nilai_iou >= ambang:
                lolos = False
                break
        if lolos:
            out.append(d)
    return out


def batas_lembar(frames, min_setuju=3):
    """
    Paling banyak berapa lembar yang pernah terlihat SERENTAK dalam satu
    frame, dengan dukungan minimal min_setuju frame. -> int

    INI PENJAGA YANG DULU TIDAK ADA, DAN AKIBATNYA FATAL.

    Log perangkat 26 Agu, penekanan ke-34, sesudah dedup:

        frame  1 4 5 6 7 8 9  ->  50k  -  100k 50k 100k 100k 50k
        jumlah per frame      ->  [1,0,0,1,1,1,1,1,1]

        50000  didukung 3 frame
        100000 didukung 4 frame
        keduanya TIDAK PERNAH serentak dalam satu frame

        Alat mengumumkan "Rp 100.000 + Rp 50.000 = Rp 150.000"
        padahal yang ada SATU lembar yang labelnya berkedip.

    Bagi pengguna tunanetra, itu mode kegagalan paling berbahaya yang
    mungkin: bukan diam, bukan salah sebut satu nominal, melainkan
    MENGARANG UANG YANG TIDAK ADA - dengan nada yakin.

    Aturannya sesederhana kalimat ini: kalau tidak ada satu frame pun
    yang pernah memuat N lembar sekaligus, jawabannya tidak boleh
    N lembar.
    """
    frames = [f for f in (frames or []) if f is not None]
    if not frames:
        return 0
    terbesar = max(len(f) for f in frames)
    for n in range(terbesar, 0, -1):
        if sum(1 for f in frames if len(f) >= n) >= min_setuju:
            return n
    return 0


def vote_notes(frames, min_setuju=3):
    """
    Gabungkan hasil beberapa frame jadi satu keputusan.

    frames: list per frame, tiap frame list dict {nominal, conf, box}

    DUA SYARAT, DAN KEDUANYA WAJIB

      1. Tiap nominal harus muncul di minimal min_setuju frame.
         (syarat lama - menyaring kilatan sesaat)

      2. Jumlah lembar yang dilaporkan tidak boleh melebihi jumlah
         yang pernah terlihat SERENTAK dalam satu frame.
         (syarat baru - lihat batas_lembar)

    Syarat 2 ditambahkan 26 Agu setelah alat mengumumkan
    "Rp 150.000" untuk satu lembar Rp50.000 yang labelnya berkedip
    antara 50000 dan 100000. Syarat 1 saja meloloskannya, karena
    kedua label memang masing-masing muncul di cukup banyak frame.

    KALAU HARUS MEMANGKAS, YANG DIBUANG ADALAH YANG PALING LEMAH.
    Bukan yang pertama, bukan yang terakhir - yang confidence
    rata-ratanya terendah, karena itu yang paling mungkin hantu.
    """
    if not frames:
        return None

    rekam = []
    for f in frames:
        c = {}
        for d in f:
            c[d["nominal"]] = c.get(d["nominal"], 0) + 1
        rekam.append(c)

    semua_nominal = set(nom for c in rekam for nom in c)
    hasil = []

    for nom in semua_nominal:
        for jumlah_lembar in range(5, 0, -1):
            dukungan = sum(1 for c in rekam if c.get(nom, 0) >= jumlah_lembar)
            if dukungan >= min_setuju:
                confs = []
                for f in frames:
                    lembar_nom = [d["conf"] for d in f if d["nominal"] == nom]
                    if len(lembar_nom) >= jumlah_lembar:
                        confs.extend(
                            sorted(lembar_nom, reverse=True)[:jumlah_lembar])
                avg_conf = sum(confs) / len(confs) if confs else 0.0
                for _ in range(jumlah_lembar):
                    # 'dukungan' ikut dicatat KHUSUS untuk pemangkasan.
                    # Uji menangkap bahwa mengurutkan hanya dengan conf
                    # memecah seri secara sembarang - padahal label yang
                    # muncul di lebih banyak frame jelas lebih layak
                    # dipercaya daripada yang cuma sesekali berkilat.
                    hasil.append({"nominal": nom, "conf": avg_conf,
                                  "dukungan": dukungan})
                break

    if not hasil:
        return None

    # PENJAGA BARU. Jangan pernah melaporkan lebih banyak lembar
    # daripada yang pernah benar-benar terlihat bersamaan.
    batas = batas_lembar(frames, min_setuju)
    if batas and len(hasil) > batas:
        hasil = sorted(hasil,
                       key=lambda d: (d.get("dukungan", 0), d["conf"]),
                       reverse=True)[:batas]

    for d in hasil:
        d.pop("dukungan", None)      # tidak diperlukan pemanggil
    return hasil


def baca_urutan(boxes, toleransi=0.6):
    """
    Urutkan kotak teks jadi urutan baca manusia: kiri->kanan, atas->bawah.

    boxes: list dict minimal punya 'box' (x, y, w, h) dan 'teks'

    Menyortir murni berdasarkan y akan mengacak kata-kata dalam satu baris
    yang sedikit miring. Jadi: kelompokkan dulu jadi baris memakai ambang
    relatif terhadap tinggi huruf, baru urutkan tiap baris berdasarkan x.
    """
    if not boxes:
        return []
    tinggi = sorted(b["box"][3] for b in boxes)
    h_med = tinggi[len(tinggi) // 2] or 1
    ambang = h_med * toleransi

    sisa = sorted(boxes, key=lambda b: b["box"][1] + b["box"][3] / 2)
    baris, kini = [], [sisa[0]]
    y_kini = sisa[0]["box"][1] + sisa[0]["box"][3] / 2

    for b in sisa[1:]:
        yc = b["box"][1] + b["box"][3] / 2
        if abs(yc - y_kini) <= ambang:
            kini.append(b)
            y_kini = sum(k["box"][1] + k["box"][3] / 2 for k in kini) / len(kini)
        else:
            baris.append(kini)
            kini, y_kini = [b], yc
    baris.append(kini)

    hasil = []
    for r in baris:
        hasil += sorted(r, key=lambda b: b["box"][0])
    return hasil


def _kelompok_baris(boxes, toleransi=0.6):
    """Kelompokkan kotak jadi baris berdasarkan tinggi. -> list of list."""
    if not boxes:
        return []
    tinggi = sorted(b["box"][3] for b in boxes)
    h_med = tinggi[len(tinggi) // 2] or 1
    ambang = h_med * toleransi

    sisa = sorted(boxes, key=lambda b: b["box"][1] + b["box"][3] / 2)
    baris, kini = [], [sisa[0]]
    y_kini = sisa[0]["box"][1] + sisa[0]["box"][3] / 2
    for b in sisa[1:]:
        yc = b["box"][1] + b["box"][3] / 2
        if abs(yc - y_kini) <= ambang:
            kini.append(b)
            y_kini = sum(k["box"][1] + k["box"][3] / 2 for k in kini) / len(kini)
        else:
            baris.append(kini)
            kini, y_kini = [b], yc
    baris.append(kini)
    return baris


def baca_baris_info(boxes, toleransi=0.6):
    """
    Seperti baca_baris, tapi tinggi huruf dan posisi TIDAK dibuang.

    -> [{"teks", "tinggi", "y", "harga"}, ...]

    baca_baris membuang tinggi kotak begitu selesai mengelompokkan. Untuk
    mengenali JUDUL, tinggi itu justru bukti utamanya - judul dicetak
    lebih besar, dan itulah alasan ia satu-satunya teks yang terbaca
    benar pada menu padat.
    """
    hasil = []
    for r in _kelompok_baris(boxes, toleransi):
        for seg in pecah_kolom(sorted(r, key=lambda b: b["box"][0])):
            teks = " ".join(b["teks"] for b in seg)
            # Pakai tinggi_asli kalau tersedia (dihitung dari 4 sudut
            # miring). Kalau tidak ada, mundur ke tinggi kotak tegak -
            # pemanggil lama dan uji lama tetap jalan.
            # GEOMETRI MENDATAR IKUT DISIMPAN, bukan cuma tegak.
            #
            # Versi sebelumnya menyimpan y dan tinggi saja, karena yang
            # dibutuhkan waktu itu cuma pengenalan judul. Tapi pertanyaan
            # 'bagian mana yang sedang DITUJU kamera' tidak bisa dijawab
            # tanpa tahu seberapa besar tiap baris mengisi bingkai -
            # jumlah kata tidak mengukur itu sama sekali.
            #
            # Kunci lama tidak diubah, jadi seluruh pemanggil dan uji
            # lama tetap jalan apa adanya.
            x0 = min(b["box"][0] for b in seg)
            hasil.append({
                "teks": teks,
                "tinggi": max(b.get("tinggi_asli") or b["box"][3]
                              for b in seg),
                "tinggi_kotak": max(b["box"][3] for b in seg),
                "y": min(b["box"][1] for b in seg),
                "x": x0,
                "lebar": max(b["box"][0] + b["box"][2] for b in seg) - x0,
                "luas": sum(b["box"][2] * b["box"][3] for b in seg),
                "harga": any(any(c.isdigit() for c in b["teks"]) for b in seg),
            })
    return hasil


# Kata yang benar-benar dipakai sebagai KEPALA BAGIAN di menu Indonesia.
# Sengaja SEMPIT dan tertutup.
#
# KENAPA BUKAN KAMUS UMUM
#
#     Aturan lama menyebut sebuah baris judul kalau semua katanya ada di
#     kamus. Log perangkat membuktikan itu tidak bisa membedakan apa pun:
#
#         ? nusantara        sebab=bersih
#         ? makanan          sebab=bersih
#         ? indomie rendang  sebab=bersih     <- ini ITEM, bukan judul
#         ? indomie aceh     sebab=bersih     <- ini ITEM juga
#
#     'indomie', 'rendang', dan 'aceh' semuanya kata sah - jadi begitu
#     harganya gagal terbaca, item menu tidak bisa dibedakan dari
#     kategori. Dan memperbanyak kamus justru MEMPERPARAH: makin banyak
#     kata dikenal, makin banyak item yang lolos jadi judul.
#
#     Akibatnya nyata: pengguna menekan BUKA pada 'indomie aceh' dan
#     mendengar dua baris acak, sementara 'Minuman' terlewat.
#
#     Nama kategori datang dari himpunan kecil yang jarang berubah;
#     nama hidangan tidak terbatas. Jadi yang didaftar kategorinya.
#
# Judul di luar daftar ini tidak hilang - ia masih bisa dikenali lewat
# ukuran huruf (aturan 'besar'), yang memang jalur utamanya.
KATA_JUDUL = set("""
menu daftar bagian kategori jenis varian pilihan level
makanan minuman cemilan camilan kudapan snack gorengan
hidangan sajian aneka macam paket promo istimewa favorit
andalan unggulan rekomendasi tambahan pelengkap topping toping
pembuka penutup utama sarapan dessert appetizer
terlaris terbaru sayuran taburan signature
""".split())

# KATA KEPALA YANG SENGAJA TIDAK DIMASUKKAN, walaupun terbukti dipakai
# sebagai judul di 9 menu sampel:
#
#     sambal   ikan   udang   cumi   tea   coffee   spesial   pedas
#
# Semuanya memang kepala bagian di salah satu menu - 'SAMBAL' di Borcelle,
# 'IKAN'/'UDANG'/'CUMI' di menu seafood, 'Tea' di Cafe Larana. Tapi
# semuanya JUGA nama item, dan jauh lebih sering:
#
#     Sambal bajak, Sambal terasi      <- item
#     Ikan Bakar Jimbaran              <- item
#     Udang Saus Padang                <- item
#     Chai Tea, Green Tea, Lemon Tea   <- item
#
# Ini persis perangkap 'bakso' yang sudah menghukum kita sekali: item
# yang harganya gagal terbaca naik jadi judul, memecah bagiannya, dan
# separuh isinya hilang dari jangkauan pengguna.
#
# Kepala semacam itu tetap terjaring lewat UKURAN HURUF - di kertas
# aslinya 'SAMBAL' dicetak di dalam kotak merah, jauh lebih besar
# daripada itemnya. Itu jalur yang benar untuknya.

# NAMA HIDANGAN SENGAJA TIDAK ADA DI SINI. Percobaan pertama memasukkan
# 'nasi', 'mie', 'ayam', 'bakso', 'teh', 'susu', 'sambal' dan seterusnya
# dengan alasan menu memang punya kepala 'ANEKA BAKSO'. Perangkat langsung
# menunjukkan itu salah:
#
#     ? topping   tinggi 97 (median 51) sebab=besar   -> 2 isi
#     ? bakso     tinggi 51 (median 51) sebab=bersih  -> 2 isi
#
# 'Bakso' adalah ITEM topping seharga 2K yang harganya gagal terbaca. Ia
# naik jadi judul, memecah bagian Topping, dan pengguna yang membuka
# 'topping' cuma mendengar 2 dari 4 item - dua sisanya tersembunyi di
# bawah judul palsu.
#
# Nama hidangan muncul sebagai ITEM ribuan kali lebih sering daripada
# sebagai kepala bagian. Kepala 'ANEKA BAKSO' tetap terjaring lewat
# ukuran huruf, yang memang jalur utamanya.
#
# Kata seperti 'spesial', 'hemat', 'komplit', 'porsi', 'rasa' juga
# dikeluarkan: ketiganya lebih sering jadi bagian NAMA item ('Nasi Goreng
# Spesial') daripada judul berdiri sendiri.


def kenali_judul(info, kamus=None, faktor=1.25, kata_judul=None):
    """
    Tandai baris mana JUDUL bagian, mana ISI. -> info + kunci "judul"

    KENAPA JUDUL LAYAK DIPERLAKUKAN KHUSUS

        Pada menu A4 padat, hampir semua isi terbaca kacau - tapi judul
        justru terbaca BENAR. Bukan kebetulan: judul dicetak paling besar,
        jadi satu-satunya teks yang lolos batas ~10 piksel PP-OCR.

        Dari log perangkat ini, di tengah 106 kotak yang isinya rusak,
        yang benar adalah MAKANAN, SAYURAN, MINUMAN, SAMBAL, MENU PAKET,
        WARMINDO, NUSANTARA, TOPPING.

        Artinya navigasi berbasis judul bekerja SEARAH dengan batas
        perangkat, bukan melawannya.

    DUA SINYAL

        besar    tinggi huruf >= faktor x median, dan tidak berharga
        bersih   tanpa harga, <= 3 kata, dan semua katanya dikenal kamus

        Sinyal kedua menangkap judul yang ukurannya tidak menonjol -
        misalnya menu yang seluruh hurufnya seragam. Tanpa itu, menu
        semacam itu tidak punya judul sama sekali.

    CATATAN

        faktor 1.25 adalah tebakan awal yang MASUK AKAL, bukan hasil
        pengukuran - log kami hanya memuat teks, tidak memuat tinggi
        kotak. Tinggi tiap baris dicetak saat dipakai supaya angkanya
        bisa dilihat dan ambangnya disetel dari kenyataan.
    """
    kamus = kamus or set()
    # Kamus JUDUL, bukan kamus umum. Lihat catatan di KATA_JUDUL.
    kata_judul = KATA_JUDUL if kata_judul is None else kata_judul
    if not info:
        return []
    tinggi = sorted(b["tinggi"] for b in info)
    med = tinggi[len(tinggi) // 2] or 1

    for b in info:
        # Huruf dikecilkan sebelum dicocokkan: judul menu hampir selalu
        # ditulis KAPITAL ("MINUMAN"), sedangkan kamus berisi huruf kecil.
        # Tanpa ini, sinyal kedua tidak pernah menyala sama sekali.
        kata = [k.lower() for k in b["teks"].split()
                if not any(c.isdigit() for c in k)]
        besar = (not b["harga"]) and b["tinggi"] >= med * faktor
        bersih = (not b["harga"] and 1 <= len(kata) <= 3
                  and all(k in kata_judul for k in kata))
        b["judul"] = bool(kata) and (besar or bersih)
        # SEBAB dicatat, bukan cuma hasilnya.
        #
        # Di log perangkat, 'mie bangladesh' dan 'indomie aceh a' muncul
        # sebagai judul dan tidak ada cara tahu aturan mana yang memicu -
        # jadi tidak ada cara memperbaikinya tanpa menebak. Dengan sebab
        # dan tingginya tercatat, satu kali jalan sudah cukup untuk tahu.
        b["sebab"] = ("besar" if besar else "bersih") if b["judul"] else ""
        b["med"] = med
    return info


def kelompokkan_bagian(info):
    """
    Kumpulkan isi di bawah tiap judul. -> [(judul, [isi, ...]), ...]

    Baris sebelum judul pertama masuk bagian tanpa nama ("") - biasanya
    nama restoran atau hiasan, dan pengguna tetap berhak mendengarnya.
    """
    bagian, judul, isi = [], "", []
    for b in info:
        if b.get("judul"):
            if judul or isi:
                bagian.append((judul, isi))
            judul, isi = b["teks"], []
        else:
            isi.append(b["teks"])
    if judul or isi:
        bagian.append((judul, isi))
    return bagian


def pisah_nama_tempat(bagian, min_isi=1):
    """
    Pisahkan nama tempat dari kategori sungguhan. -> (nama, kategori)

    MASALAHNYA

        Kepala menu berbunyi 'MENU / WARMINDO / NUSANTARA' - tiga baris
        besar tanpa harga, jadi ketiganya lolos sebagai judul. Padahal
        itu nama warung, bukan kategori yang bisa dibuka.

        Akibatnya pengguna tunanetra harus menekan tombol tiga kali
        melewati bagian kosong sebelum sampai ke 'Makanan'.

    ATURANNYA SEDERHANA DAN MASUK AKAL

        Kategori punya ISI di bawahnya. Nama tempat tidak.

        Bukan lewat kamus atau daftar kata terlarang - nama warung bisa
        apa saja, dan kategori bisa tidak umum ('MENU PAKET SPESIAL
        PEDAS'). Yang membedakan bukan katanya, melainkan ada tidaknya
        sesuatu di bawahnya.

    Nama tempat TIDAK dibuang - dikembalikan terpisah supaya tetap bisa
    diucapkan sekali di awal. Pengguna berhak tahu ia sedang di mana.
    """
    nama = [j for j, isi in bagian if j and len(isi) < min_isi]
    kategori = [(j, isi) for j, isi in bagian if len(isi) >= min_isi]
    return nama, kategori


def bagian_terkaya(bagian):
    """
    Bagian mana yang sebaiknya dibuka LEBIH DULU? -> indeks

    URUTAN DOKUMEN BUKAN URUTAN YANG BERGUNA

        Diukur pada 5 bidikan menu Warmindo yang sama:

            bidikan 1  mulai di 'warmindo' (1 isi)   -> terdengar 'niasanaa'
            bidikan 2  mulai di '(tanpa judul)' (1)  -> terdengar 'mena'
            bidikan 4  mulai di '(tanpa judul)' (1)  -> terdengar 'nusantara'
            bidikan 7  mulai di '(tanpa judul)' (1)  -> terdengar 'nusantara'
            bidikan 5  mulai di 'makanan' (8 isi)    -> benar

        Empat dari lima kali, hal PERTAMA yang didengar pengguna adalah
        satu baris sampah. Bagian bersampah hampir selalu berisi satu
        baris - sisa nama warung atau potongan judul yang salah baca -
        sedangkan bagian sungguhan berisi 7-8 item.

        Bagi pengguna awas ini cuma gangguan kecil: matanya langsung
        melompat ke daftar yang benar. Pengguna tunanetra tidak punya
        lompatan itu - ia harus menunggu, mendengarkan sampah, lalu
        menebak apakah alatnya rusak.

    TIDAK ADA YANG DIBUANG

        Bagian lain tetap ada dan tetap bisa dijelajahi lewat tombol
        BAGIAN, dalam urutan dokumen seperti biasa. Yang berubah hanya
        titik mulainya. Menghapus bagian kecil akan menghilangkan
        kategori sah yang kebetulan cuma punya satu item.
    """
    if not bagian:
        return 0
    terbaik, n_terbaik = 0, -1
    for i, (_, isi) in enumerate(bagian):
        if len(isi) > n_terbaik:          # '>' bukan '>=': kalau seri,
            terbaik, n_terbaik = i, len(isi)   # yang lebih awal menang
    return terbaik


def nilai_bagian(bagian, info, tinggi_bingkai=None,
                 pusat=0.6, bobot_tepi=0.5, bobot_tanpa_judul=0.4):
    """
    Seberapa besar tiap bagian mengisi bingkai. -> [nilai, ...]

    Dipisahkan dari bagian_dituju supaya angkanya bisa DILIHAT dan
    dicatat di log, bukan cuma keputusannya. Tiga vonis otomatis saya
    sebelumnya salah justru karena hanya kesimpulannya yang tercetak.
    """
    nilai = [0.0] * len(bagian)
    if not bagian or not info:
        return nilai

    # Peta teks -> baris. Teks bisa berulang ('5.000' muncul di banyak
    # baris), jadi yang disimpan daftar dan dipakai sekali-pakai supaya
    # satu baris tidak dihitung untuk dua bagian.
    peta = {}
    for b in info:
        peta.setdefault(b["teks"], []).append(b)
    dipakai = set()

    def ambil(teks):
        for b in peta.get(teks, ()):
            if id(b) not in dipakai:
                dipakai.add(id(b))
                return b
        return None

    # TINGGI BINGKAI: kalau tidak diberi, disimpulkan dari sebaran teks.
    #
    # Ini BUKAN sekadar kemudahan. Pada jalur letterbox, teks cuma
    # mengisi baris 140-500 dari kanvas 640 - sisanya hitam. Memakai 640
    # sebagai bingkai membuat 'pita tengah' mencakup hampir seluruh teks,
    # jadi gerbang kepusatannya tidak menyaring apa pun. Disimpulkan dari
    # isinya, pita itu ikut menyesuaikan diri ke jalur mana pun yang
    # dipakai - letterbox maupun kanal ISP.
    if not tinggi_bingkai:
        atas = min(b["y"] for b in info)
        bawah = max(b["y"] + b.get("tinggi_kotak", 0) for b in info)
        tinggi_bingkai = max(1, bawah - atas)
        asal = atas
    else:
        asal = 0
    p0 = asal + tinggi_bingkai * (1 - pusat) / 2.0
    p1 = asal + tinggi_bingkai * (1 + pusat) / 2.0

    for i, (judul, isi) in enumerate(bagian):
        total = 0.0
        for teks in ([judul] if judul else []) + list(isi):
            b = ambil(teks)
            if b is None:
                continue
            luas = b.get("luas") or (b.get("lebar", 0)
                                     * b.get("tinggi_kotak", 0))
            tengah = b["y"] + b.get("tinggi_kotak", 0) / 2.0
            total += luas * (1.0 if p0 <= tengah <= p1 else bobot_tepi)
        # Bagian tanpa judul adalah isi YATIM - judulnya ada di luar
        # bingkai. Justru itu tanda bahwa ia BUKAN yang sedang dituju.
        # Dihukum, bukan dibuang: menu tanpa judul sama sekali harus
        # tetap punya pemenang.
        nilai[i] = total * (1.0 if judul else bobot_tanpa_judul)
    return nilai


def bagian_dituju(bagian, info, tinggi_bingkai=None, pusat=0.6):
    """
    Bagian mana yang sedang DITUJU kamera? -> indeks

    BEDANYA DENGAN bagian_terkaya

        bagian_terkaya memilih yang KATANYA PALING BANYAK. Itu memang
        memperbaiki masalah lamanya (mulai di bagian bersampah), tapi ia
        menjawab pertanyaan yang berbeda: 'bagian mana yang paling
        berisi di seluruh dokumen', bukan 'bagian mana yang sedang
        dilihat pengguna'.

        Kalau alat diarahkan ke Minuman sementara Makanan kebetulan
        punya lebih banyak item, bagian_terkaya akan membacakan Makanan -
        padahal Makanan cuma tersisa beberapa potong di tepi bingkai.

    SINYAL YANG DIPAKAI, DAN KENAPA

        Saat Anda mengarahkan alat ke satu bagian, dua hal terjadi
        sekaligus pada bagian itu: ia mengisi bingkai lebih banyak
        (LUAS kotaknya besar), dan ia duduk di TENGAH. Bagian tetangga
        terpotong jadi sisa kecil di tepi.

        Jadi nilainya = jumlah luas kotak, dengan baris di luar pita
        tengah dihitung setengah. Luas, bukan jumlah baris - delapan
        baris kecil di tepi harus kalah dari lima baris besar di tengah.

    KENAPA MENGHUKUM, BUKAN MENGGUGURKAN

        Godaannya adalah membuang bagian yang di luar pita tengah. Saya
        tidak melakukannya: pita tengah bisa meleset kalau pengguna
        memegang alat agak miring, dan kegagalan 'tidak membacakan
        apa-apa' jauh lebih buruk daripada 'membacakan bagian tetangga'.
        Bobot setengah membuat kepusatan jadi PEMECAH SERI yang kuat,
        bukan gerbang yang bisa mengunci semuanya.

    ANGKANYA BELUM DIUKUR

        pusat=0.6, bobot tepi 0.5, dan hukuman tanpa-judul 0.4 adalah
        tebakan yang beralasan, BUKAN hasil pengukuran - persis seperti
        faktor 1.25 di kenali_judul. Semuanya jadi parameter supaya bisa
        disetel dari rekaman nanti, dan nilai tiap bagian dicetak ke log
        supaya ada yang bisa disetel.

    Kalau geometri tidak tersedia (info kosong, atau teks bagian tidak
    cocok dengan info mana pun), fungsi ini MUNDUR ke bagian_terkaya -
    perilaku lama, bukan tebakan baru.
    """
    if not bagian:
        return 0
    nilai = nilai_bagian(bagian, info, tinggi_bingkai, pusat)
    if not any(n > 0 for n in nilai):
        return bagian_terkaya(bagian)
    terbaik, n_terbaik = 0, -1.0
    for i, n in enumerate(nilai):
        if n > n_terbaik:                # seri -> yang lebih awal menang
            terbaik, n_terbaik = i, n
    return terbaik


def baca_baris(boxes, toleransi=0.6):
    """
    Kelompokkan kotak jadi baris bacaan. -> [["Sosis", "2K"], ...]

    Struktur barisnya DIPERTAHANKAN. baca_urutan menggabung semuanya jadi
    satu deret, dan itu menghancurkan pasangan nama-harga: pada uji menu
    cetak, empat item berharga sama menyusut jadi dua token tanpa pemilik.

    Baris yang sebenarnya memuat beberapa KOLOM dipecah oleh pecah_kolom.
    """
    hasil = []
    for r in _kelompok_baris(boxes, toleransi):
        for seg in pecah_kolom(sorted(r, key=lambda b: b["box"][0])):
            hasil.append([b["teks"] for b in seg])
    return hasil


def mutu_bacaan(baris, kamus, n_kotak=0):
    """
    Seberapa layak hasil OCR ini diucapkan? -> (tingkat, skor, saran)

    KENAPA INI PENTING, DAN BUKAN SEKADAR PENGHALUSAN

        Pengguna tunanetra TIDAK BISA melihat bahwa bidikannya meleset.
        Kalau alat membacakan 'marmnoo, makaan, iga o', mereka akan
        menganggap menu itu memang berisi kata-kata tersebut. Tidak ada
        cara bagi mereka memverifikasi.

        Diam, atau menyuruh mendekat, jauh lebih jujur daripada
        membacakan 25 baris omong kosong dengan nada yakin.

    DUA SINYAL, KARENA SATU TIDAK CUKUP

        skor    persen kata yang ada di kamus
        pendek  persen kata <= 2 huruf - pecahan sampah OCR

        Persentase sendirian menipu: satu bidikan buruk pernah memberi
        33% hanya karena kebetulan 1 dari 3 katanya dikenal. Karena itu
        jumlah kata minimum ikut dijaga.

        Sebaliknya 'pendek' menangkap yang lolos persentase: bidikan
        terburuk menghasilkan 43% kata sepanjang 1-2 huruf ('os', 'ak',
        'y oy a'), sementara bidikan bagus nyaris 0%.

    AMBANGNYA DARI DATA, BUKAN KARANGAN

        Diukur pada lima bidikan nyata dari perangkat ini:

            baik    menu dekat huruf besar   83% dikenal,  0% pendek
            baik    bidikan bagus            60%,          0%
            sedang  bidikan sedang           31%,         15%
            buruk   menu kecil di frame      11%,         43%
            sedikit hanya 3 kata terbaca     33%,          0%

    CATATAN YANG AKAN MENUA

        Kamus belum memuat kosakata menu (nugget, topping, seblak...),
        jadi bacaan yang BENAR pun tertahan skornya. Begitu kosakata itu
        ditambahkan, semua skor naik dan ambang di bawah perlu ditinjau
        ulang - kalau tidak, alat jadi terlalu longgar.
    """
    kata = [k for b in baris for k in str(b).split()
            if not any(c.isdigit() for c in k)]
    if len(kata) < 4:
        return ("sedikit", 0, "arahkan ke teks")

    skor = int(100 * sum(1 for k in kata if k in kamus) / len(kata))
    pendek = int(100 * sum(1 for k in kata if len(k) <= 2) / len(kata))

    if skor >= AMBANG_BAIK and pendek < 25:
        return ("baik", skor, "")
    if skor >= AMBANG_SEBAGIAN and pendek < 35:
        return ("sebagian", skor, "sebagian tidak terbaca")

    # Banyak kotak tapi tak satu pun terbaca = teksnya TERLIHAT tapi
    # terlalu kecil. Kotak sedikit = memang tidak ada teks yang terjangkau.
    #
    # AMBANGNYA DINAIKKAN DARI 20 KE 30, dan itu memperbaiki NASIHAT yang
    # salah. Jumlah kotak adalah ukuran langsung seberapa banyak halaman
    # yang masuk frame - yaitu jarak. Dari 35 bidikan nyata:
    #
    #     kotak     n   rata skor   lulus
    #     <10       5       23%      40%     terlalu dekat / meleset
    #     10-19    13       52%      53%
    #     20-29    10       62%      70%     <- paling berhasil
    #     >=30      7       19%      14%     seluruh menu masuk frame
    #
    # Jadi 20-29 kotak justru zona TERBAIK. Menyuruh "dekatkan" di situ
    # menyuruh pengguna menjauh dari titik yang paling berhasil - dan
    # pengguna tunanetra tidak punya cara memeriksa bahwa nasihat itu
    # keliru. Kegagalan di zona itu sebabnya buram atau goyang, bukan
    # jarak, jadi yang benar adalah menyuruh mengulang.
    if n_kotak >= 30:
        return ("buruk", skor, "terlalu jauh, dekatkan")
    return ("buruk", skor, "teks tidak terbaca")


def tinggi_teks(xs, ys):
    """
    Tinggi huruf sebenarnya dari 4 titik sudut kotak OCR.

    KENAPA BUKAN max(y) - min(y)

        PP-OCR mengembalikan SEGI EMPAT MIRING yang mengikuti kemiringan
        barisnya. Memerasnya jadi kotak tegak lurus membuat tingginya
        melar mengikuti PANJANG baris:

            tinggi_kotak_tegak = h*cos(t) + W*sin(t)

        Untuk baris selebar 300 px setinggi 16 px yang miring 5 derajat:

            16*0,996 + 300*0,087 = 16 + 26 = 42 px      (2,6x!)

        Dan melarnya SEBANDING DENGAN PANJANG BARIS. Jadi baris item
        terpanjang tampak paling "besar", lalu kenali_judul menyangka ia
        judul. Itu persis yang terjadi di log: 'mie bangladesh' - salah
        satu baris terpanjang - jadi judul yang memuat 7 item di
        bawahnya, sementara 'Makanan' yang pendek justru terlempar jadi
        nama tempat.

        Foto menu jarang tegak lurus. Pengguna tunanetra TIDAK BISA
        meluruskan bidikannya - jadi memerlukan foto lurus bukan syarat
        yang boleh dipasang pada alat ini.

    CARANYA

        Sisi pendek segi empat itulah tinggi hurufnya, berapa pun
        miringnya. Diambil rata-rata dua sisi berhadapan, lalu dipilih
        pasangan yang lebih pendek.
    """
    if not xs or not ys or len(xs) < 4 or len(ys) < 4:
        h = (max(ys) - min(ys)) if ys else 0
        return float(h)
    t = [(xs[i], ys[i]) for i in range(4)]

    def jarak(a, b):
        return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5

    sisi_a = (jarak(t[0], t[1]) + jarak(t[2], t[3])) / 2.0
    sisi_b = (jarak(t[1], t[2]) + jarak(t[3], t[0])) / 2.0
    return float(min(sisi_a, sisi_b))


def ketajaman(sampel):
    """
    Seberapa tajam sederet sampel keabuan? Makin besar makin tajam.

    KENAPA INI YANG DIUKUR, BUKAN JARAK

        Log perangkat memperlihatkan sesuatu yang tidak cocok dengan
        dugaan "terlalu jauh":

            bidikan  kotak  skor
                1      17     7%
                2      18     4%
                3      16     9%
                6      20    88%
                7      15    86%

        Jumlah kotak hampir SAMA (15-20), skornya beda 10 kali lipat.
        Kalau teksnya terlalu kecil, detektor akan menemukan lebih
        SEDIKIT kotak - itu yang terjadi di bidikan 4 dan 5 (3-4 kotak).
        Di sini detektor melihat teks sebanyak biasanya; yang gagal
        hanya pengenalnya.

        Itu tanda khas BURAM - entah karena fokus tetap lensa meleset,
        entah karena tangan bergoyang. Bukan soal jauh-dekat.

    KENAPA BERGUNA

        Buram bisa diukur TANPA NPU dan tanpa menjalankan OCR. Alat
        sudah mengambil 5 frame tiap kali tapi hanya memakai yang
        pertama; empat sisanya dibuang. Memilih yang paling tajam di
        antaranya gratis - tidak ada foto tambahan, tidak ada inferensi
        tambahan.

    CARANYA

        Laplacian 1 dimensi (x[i-1] - 2x[i] + x[i+1]), dikuadratkan lalu
        dirata-rata. Tepi huruf yang tajam menghasilkan nilai besar;
        tepi yang melandai menghasilkan nilai kecil.

        Dibagi kuadrat kecerahan rata-rata supaya frame yang lebih
        terang tidak otomatis terlihat lebih tajam. Tanpa pembagian ini
        yang terukur adalah pencahayaan, bukan fokus.

    SUDAH DIUKUR DI PERANGKAT - DAN TIDAK DIPASANG. JANGAN DIULANG.

        uji_fokus.py, dua pengambilan, tiap kali 5 frame:

            korelasi peringkat ketajaman vs mutu OCR:  1,00 dan 0,70

        Jadi metriknya BEKERJA - buram memang terukur tanpa NPU. Tapi
        keuntungannya nol, karena frame paling tajam SELALU frame 0:

            frame   tajam A   mutu A  |  tajam B   mutu B
              0     0,0088     96%    |  0,0160     59%
              1     0,0047     83%    |  0,0078     42%
              2     0,0054     90%    |  0,0104     65%
              3     0,0068     95%    |  0,0141     54%
              4     0,0033     37%    |  0,0061     21%

        Frame 0 tertajam di kedua pengambilan, frame 4 terburuk di
        kedua-duanya. Ketajaman menurun sepanjang deret - tangan
        bergeser sesudah tombol ditekan. main.py sudah memakai frame 0,
        jadi memilih tertajam memberi +0 poin dengan ongkos ~950 ms.

        Fungsi ini ditahan karena murah, teruji, dan berguna kalau nanti
        ada pemicu selain tangan (mis. tombol fisik dengan jeda). Untuk
        sekarang: JANGAN dipasang ke jalur baca.
    """
    n = len(sampel)
    if n < 3:
        return 0.0
    rata = sum(sampel) / n
    if rata < 1.0:
        return 0.0
    total = 0.0
    for i in range(1, n - 1):
        lap = sampel[i - 1] - 2.0 * sampel[i] + sampel[i + 1]
        total += lap * lap
    return (total / (n - 2)) / (rata * rata)


def pilih_tertajam(daftar_sampel):
    """
    -> (indeks_terbaik, daftar_skor)

    Kalau semuanya sama-sama buram, yang terbaik tetap dikembalikan -
    memilih yang paling tidak buruk masih lebih baik daripada memilih
    yang pertama secara buta. Penjaga mutu tetap yang memutuskan layak
    dibacakan atau tidak.
    """
    skor = [ketajaman(s) for s in daftar_sampel]
    if not skor:
        return 0, []
    return skor.index(max(skor)), skor


def dedup_teks(kotak, iou_th=0.35):
    """
    Buang kotak teks yang saling tumpang tindih.

    Dibutuhkan saat frame dipotong jadi petak yang sengaja saling
    menimpa: teks di perbatasan terbaca dua kali, dan hasilnya
    'borc ecellel sayur yuran' - tiap kata muncul dua versi.

    Yang dipertahankan adalah teks TERPANJANG, karena bacaan dari petak
    yang memuat kata itu secara utuh hampir selalu lebih lengkap
    daripada bacaan dari petak yang cuma menangkap potongannya.
    """
    out = []
    for k in sorted(kotak, key=lambda d: (-len(d.get("teks", "")),
                                          d["box"][1], d["box"][0])):
        if all(iou(k["box"], o["box"]) < iou_th for o in out):
            out.append(k)
    return out


def pecah_kolom(baris):
    """
    Pecah satu baris yang sebenarnya memuat BEBERAPA KOLOM.

    baris: list kotak, sudah terurut kiri->kanan

    MASALAHNYA

        Menu warung sering tiga kolom. Mengelompokkan kotak murni
        berdasarkan tinggi membuat item dari kolom berbeda yang kebetulan
        sejajar melebur jadi satu baris:

            cah kangkung Rp.15.000 tumis toge Rp.20.000 es teh Rp.5.000

        Pasangan nama-harga jadi kacau lagi, persis masalah yang baru
        kita selesaikan.

    CARA MEMISAHNYA

        Bukan lewat celah geometris - jarak antara nama dan harganya
        sendiri juga lebar, jadi celah tidak bisa membedakan keduanya.

        Yang dipakai aturan isi: SATU BARIS MENU HANYA PUNYA SATU HARGA.
        Jadi begitu sudah ada harga lalu muncul kata biasa lagi, itu
        pasti awal kolom berikutnya.
    """
    segmen, kini, sudah_harga = [], [], False
    for b in baris:
        ada_angka = any(c.isdigit() for c in str(b.get("teks", "")))
        if sudah_harga and not ada_angka and kini:
            segmen.append(kini)
            kini, sudah_harga = [], False
        kini.append(b)
        if ada_angka:
            sudah_harga = True
    if kini:
        segmen.append(kini)
    return segmen


def pisah_harga(baris):
    """'sosis 2000' -> ('sosis', ['2000'])"""
    tok = str(baris).split()
    harga = [t for t in tok if any(c.isdigit() for c in t)]
    nama = [t for t in tok if t not in harga]
    return " ".join(nama), harga


def vote_baris(riwayat, min_frame=2, kamus=None):
    """
    Voting PER BARIS lintas frame -> daftar baris yang bertahan.

    riwayat: list per frame, tiap frame = list baris (string, sudah lewat
             koreksi_baris)

    CARA MENCOCOKKAN BARIS ANTAR FRAME

        Bukan lewat nomor urut - jumlah kotak berubah tiap frame (12, 12,
        13, 13, 13 pada uji Anda), jadi baris ke-3 di satu frame belum
        tentu baris ke-3 di frame lain.

        Kuncinya NAMA ITEM, yaitu bagian baris yang bukan angka. Harga
        boleh goyang; nama item jauh lebih stabil. Ejaan nama yang
        berbeda-beda disatukan memakai gabung_mirip, alat yang sama yang
        menyelamatkan 'maixhub' dari lima ejaan.

    HARGA DIPILIH TERBANYAK

        Satu baris bisa terbaca '2K' di empat frame dan 'ZK' di satu.
        Yang terbanyak menang. Kalau tidak ada harga yang muncul minimal
        min_frame kali, barisnya tetap disebut TANPA harga - menyebut
        nama tanpa harga masih berguna, menyebut harga yang salah tidak.
    """
    kamus = kamus or set()
    frames = [f for f in riwayat if f]
    if not frames:
        return []

    hitung, harga, posisi = {}, {}, {}
    for f in frames:
        lihat = set()
        for i, baris in enumerate(f):
            nama, hrg = pisah_harga(baris)
            if not nama or nama in lihat:
                continue
            lihat.add(nama)
            hitung[nama] = hitung.get(nama, 0) + 1
            posisi.setdefault(nama, []).append(i / max(1, len(f) - 1))
            if hrg:
                harga.setdefault(nama, {})
                kunci = " ".join(hrg)
                harga[nama][kunci] = harga[nama].get(kunci, 0) + 1

    # Satukan ejaan nama yang berbeda sebelum ambang dihitung.
    peta = gabung_mirip(hitung, kamus)
    gab_h, gab_p, gab_n = {}, {}, {}
    for nama, n in hitung.items():
        w = peta.get(nama, nama)
        gab_n[w] = gab_n.get(w, 0) + n
        gab_p.setdefault(w, []).extend(posisi[nama])
        for k, c in harga.get(nama, {}).items():
            gab_h.setdefault(w, {})
            gab_h[w][k] = gab_h[w].get(k, 0) + c

    hasil = []
    for nama in sorted(gab_n, key=lambda w: sum(gab_p[w]) / len(gab_p[w])):
        if gab_n[nama] < min_frame:
            continue
        pilih = ""
        kandidat = gab_h.get(nama, {})
        if kandidat:
            terbaik = max(kandidat, key=lambda k: (kandidat[k], k))
            if kandidat[terbaik] >= min_frame:
                pilih = terbaik
        hasil.append((nama + " " + pilih).strip())
    return hasil


# ======================================================================
# 4. SUKU KATA BAHASA INDONESIA
# ======================================================================
VOKAL = set("aiueo")
DIGRAF = ("ng", "ny", "sy", "kh")

# Diftong Bahasa Indonesia. Hanya berlaku di AKHIR kata.
#   pantai -> pan-tai      (ai di akhir = diftong)
#   laut   -> la-ut        (au di tengah = dua vokal terpisah)
#   kalau  -> ka-lau       daun -> da-un       amboi -> am-boi
DIFTONG = ("ai", "au", "oi")


def _is_vokal(s, i):
    return i < len(s) and s[i] in VOKAL


def _panjang_konsonan(s, i):
    """1, atau 2 kalau posisi i memulai digraf (ng/ny/sy/kh)."""
    if i + 1 < len(s) and s[i:i + 2] in DIGRAF:
        return 2
    return 1


def suku_kata(kata):
    """
    Penggal kata Indonesia jadi suku kata.

    Pola dasar: V, VK, KV, KVK, KKV, KKVK
    Aturan yang dipakai (fonotaktik standar Bahasa Indonesia):
      V-KV    satu konsonan antar vokal -> ikut ke suku berikutnya
      VK-KV   dua konsonan antar vokal  -> dipisah
      VK-KKV  tiga konsonan             -> pisah setelah yang pertama
    Digraf ng/ny/sy/kh selalu diperlakukan sebagai satu konsonan.

    'ayam'  -> ['a', 'yam']
    'kosek' -> ['ko', 'sek']
    'bakso' -> ['bak', 'so']
    'nasi'  -> ['na', 'si']
    """
    s = "".join(ch for ch in str(kata).lower() if ch.isalpha())
    if not s:
        return []

    hasil, buf, i = [], "", 0
    while i < len(s):
        if not _is_vokal(s, i):
            # kumpulkan onset konsonan
            n = _panjang_konsonan(s, i)
            buf += s[i:i + n]
            i += n
            continue

        # diftong di akhir kata -> satu nukleus utuh (pan-TAI, ka-LAU)
        if s[i:i + 2] in DIFTONG and i + 2 == len(s):
            buf += s[i:i + 2]
            hasil.append(buf)
            buf = ""
            i += 2
            continue

        # ketemu vokal -> tutup nukleus
        buf += s[i]
        i += 1

        # hitung deret konsonan setelah vokal
        j, gugus = i, []
        while j < len(s) and not _is_vokal(s, j):
            n = _panjang_konsonan(s, j)
            gugus.append(s[j:j + n])
            j += n

        if j >= len(s):
            buf += "".join(gugus)          # akhir kata: semua jadi koda
            hasil.append(buf)
            buf = ""
            i = j
        elif len(gugus) == 0:
            hasil.append(buf)              # vokal ketemu vokal
            buf = ""
        elif len(gugus) == 1:
            hasil.append(buf)              # V-KV
            buf = ""
        else:
            buf += gugus[0]                # VK-KV / VK-KKV
            hasil.append(buf)
            buf = ""
            i += len(gugus[0])
    if buf:
        if hasil:
            hasil[-1] += buf
        else:
            hasil.append(buf)
    return hasil


# ======================================================================
# 5. TANGGA FALLBACK AUDIO
# ======================================================================
NAMA_HURUF = {
    "a": "a", "b": "b", "c": "c", "d": "d", "e": "e", "f": "f", "g": "g",
    "h": "h", "i": "i", "j": "j", "k": "k", "l": "l", "m": "m", "n": "n",
    "o": "o", "p": "p", "q": "q", "r": "r", "s": "s", "t": "t", "u": "u",
    "v": "v", "w": "w", "x": "x", "y": "y", "z": "z",
}


# Kata serapan: ejaan Indonesia untuk kata asing yang SUDAH mapan.
#
# Ditambahkan 26 Agu setelah log perangkat memperlihatkan alat mengeja
# "special" sebagai S-p-e-c-i-a-l. Sebabnya bukan cacat rencana_ucap:
# tangganya bekerja benar, tapi anak tangga ketiga (suku kata) memakai
# penyuku-kata BAHASA INDONESIA, dan kata Inggris tidak terpotong jadi
# suku kata yang wav-nya tersedia. Akibatnya jatuh ke ejaan huruf.
#
# Pemetaan ini BUKAN terjemahan. 'special' memang dibaca orang Indonesia
# sebagai 'spesial'; yang diucapkan tetap kata yang sama.
#
# SENGAJA PENDEK, dan hanya berisi serapan yang sudah baku. Menebak lafal
# kata Inggris yang belum jadi serapan beresiko terdengar salah, dan
# pengguna tunanetra tidak punya cara memeriksanya. Kata di luar daftar
# ini sebaiknya dibangkitkan wav-nya sungguhan - lihat cek_wav_kamus.py.
LAFAL_SERAPAN = {
    "special": "spesial",
    "specials": "spesial",
    "original": "orisinal",
    "coffee": "kopi",
    "juice": "jus",
    "sauce": "saus",
    "cream": "krim",
    "combo": "kombo",
    "extra": "ekstra",
    "vanilla": "vanila",
    "chocolate": "cokelat",
}


def rencana_ucap(teks, punya):
    """
    Susun daftar file wav untuk mengucapkan `teks`.

    punya(nama) -> bool, mengecek apakah /root/audio/<nama>.wav ada.

    Tangga fallback, per kata, dari yang paling enak didengar:
      1. frasa utuh    'nasi_goreng.wav'
      2. kata          'nasi.wav' + 'goreng.wav'
      3. suku kata     'ko.wav' + 'sek.wav'
      4. huruf         'k.wav' 'o.wav' 's.wav' 'e.wav' 'k.wav'

    Mengeja huruf per huruf adalah pilihan TERAKHIR, bukan yang pertama.
    "K-O-S-E-K" hampir tidak bisa dipahami saat didengar; "ko-sek" langsung
    kedengaran seperti kata aslinya. Bahasa Indonesia sangat fonetis, jadi
    penggabungan suku kata terdengar wajar.
    """
    kata = [w for w in "".join(
        c if (c.isalnum() or c.isspace()) else " " for c in str(teks).lower()
    ).split() if w]
    if not kata:
        return []

    # 1. frasa utuh (coba yang terpanjang dulu)
    for n in range(len(kata), 1, -1):
        for mulai in range(0, len(kata) - n + 1):
            frasa = "_".join(kata[mulai:mulai + n])
            if punya(frasa):
                return (rencana_ucap(" ".join(kata[:mulai]), punya)
                        + [frasa]
                        + rencana_ucap(" ".join(kata[mulai + n:]), punya))

    urut = []
    for w in kata:
        if punya(w):                                   # 2. kata utuh
            urut.append(w)
            continue
        alias = LAFAL_SERAPAN.get(w)                   # 2b. ejaan serapan
        if alias and punya(alias):
            urut.append(alias)
            continue
        sk = suku_kata(w)
        if sk and all(punya(s) for s in sk):           # 3. suku kata
            urut += sk
            continue

        # 4. eja huruf - HANYA kalau SEMUA hurufnya ada.
        # Mengeja sebagian ("y" untuk kata "xyz") lebih buruk daripada diam:
        # pengguna mendengar sesuatu yang terdengar lengkap padahal bukan.
        huruf = [NAMA_HURUF.get(ch) for ch in w]
        if huruf and all(h and punya(h) for h in huruf):
            urut += huruf
        elif punya("tidak_terbaca"):
            urut.append("tidak_terbaca")
    return urut


def format_rupiah(nominal_list):
    """
    Ubah daftar nominal jadi (teks_layar, total).
    [10000, 2000] -> ('Rp 10.000 + Rp 2.000 = Rp 12.000', 12000)
    """
    if not nominal_list:
        return "Tidak ada uang terdeteksi", 0
    total = sum(nominal_list)
    bagian = " + ".join(f"Rp {n:,}".replace(",", ".") for n in sorted(nominal_list, reverse=True))
    if len(nominal_list) == 1:
        return bagian, total
    return f"{bagian} = Rp {total:,}".replace(",", "."), total


# ======================================================================
# 6. KOREKSI OCR  (kamus + voting temporal)
# ======================================================================
# Diukur pada 29 bacaan nyata papan "Menu Restoran" dari perangkat:
#     mentah                 4/29  (14%)
#     + koreksi kamus       23/29  (79%)
#     + voting 15 frame     "menu restoran"  BENAR
#
# PP-OCR bawaan memakai model recognition Mandarin (ch_PP-OCRv4_rec,
# 6623 karakter). Untuk Bahasa Indonesia ia meleset satu-dua huruf pada
# kata yang sebenarnya kita kenal - bukan buta, hanya tidak tahu bahasanya.

# ----------------------------------------------------------------------
# KAMUS DUA LAPIS
# ----------------------------------------------------------------------
# Ada dua pekerjaan berbeda yang dulu dikerjakan satu daftar yang sama:
#
#   MENGENALI   kata tercetak benar -> lolos apa adanya.
#               Tidak ada tebakan di sini, jadi tidak ada risiko.
#               Kata yang TIDAK terdaftar dibuang diam-diam - pengguna
#               tidak pernah mendengar bahwa menu itu ada.
#
#   MENEBAK     kata salah baca -> dicocokkan ke kata terdekat.
#               Di sinilah semua kerusakan terjadi: 'bus' jadi 'jus',
#               'petani' jadi 'petai', 'riji' jadi 'biji'.
#
# GARIS PEMISAHNYA: KOSAKATA MENU vs KOSAKATA UMUM
#
# Bukan "sering vs jarang". Percobaan pertama memakai garis itu - inti
# hanya 149 kata yang dianggap paling sering - dan hasilnya buruk begitu
# diuji dengan bahan yang jujur (SELURUH kosakata menu, bukan hanya isi
# inti sendiri):
#
#     inti 149 kata     pulih 111   salah 63    <- 36% tebakan MELESET
#     inti 627 kata     pulih 458   salah 52    <- 10%
#     satu lapis 850    pulih 448   salah 53
#
# Sebabnya terlihat di log perangkat: 'NUSANTASO' berhasil jadi
# 'nusantara' waktu kamus masih satu lapis, dan GAGAL setelah 'nusantara'
# dipindah ke lapisan luar. Kata menu yang dikeluarkan dari kolam tebakan
# tidak jadi diam - ia dibelokkan ke kata inti lain yang kebetulan dekat.
#
# Jadi SELURUH kosakata menu masuk kolam tebakan. Yang dikeluarkan hanya
# bank kata audio (kata_utuh.py): 'ayah', 'sekolah', 'kendaraan',
# 'berjalan' - kata umum yang dikumpulkan supaya bisa DIUCAPKAN, bukan
# karena muncul di menu. Kata itulah yang menangkapi sampah OCR.
#
#     inti 627 vs satu lapis 850:  palsu 24->22  sampah 57->54
#     dan bonus nyata dari log:    'Ayan' -> 'ayam'
#     (dulu gagal karena bersaing dengan 'ayah' dari bank audio)
#
# Menambah kata menu ke daftar ini aman dan dianjurkan. Yang TIDAK boleh
# ikut jadi bahan tebakan adalah kosakata umum non-menu.
KAMUS_INTI = """
menu restoran warung kedai rumah makan kantin cafe kafe daftar harga
makanan minuman camilan cemilan tambahan pilihan tersedia
nasi goreng ayam bakar rebus kukus penyet geprek bebek sapi kambing ikan
daging panggang tumis siram matah hitam
lele nila gurame kakap cumi udang telur tahu tempe sayur kangkung bayam
mie bakmi kwetiau bihun soun bakso soto sop gulai rendang opor semur
indomie kremes batagor siomay pempek bakwan capcay seblak
sambal saus kecap terasi kerupuk lalapan acar pete jengkol perkedel
topping nugget sosis keju gula coklat cokelat
teh kopi susu jus sirup soda es jeruk lemon alpukat mangga kelapa
air mineral hangat dingin panas manis pedas asin gurih tawar
porsi paket komplit spesial jumbo besar sedang kecil setengah
ribu rupiah gratis promo diskon hemat murah tambah ekstra total jumlah
buka tutup pagi siang sore malam senin selasa rabu kamis jumat sabtu minggu
pesan bayar tunai kembali antar bungkus dibungkus kasir pajak
"""

# Lanjutan kosakata menu. Dipisah hanya supaya enak dibaca - isinya
# sama-sama masuk kolam tebakan. Kata di sini datang dari menu cetak
# nyata yang sudah diuji di perangkat.
KAMUS_LUAS = """
kaldu bangladesh warmindo toping hype telor

sundae taburan meses koin biskuit warna kul pisang stroberi nanas
kue tart bolu pie puding vla waffle brownies tiramissu salad
parmesan lodho maryam premium bucket calamari jimbaran madu
bajak rempelo wuluh plecing gudangan kampung bunga yoghurt
gejrot taliwang madura mendoan special khas meresap tea
signature dish appetizer course main non
"""

# MEREK. Wajib terdaftar, bukan opsional.
#
# Log perangkat menulis 'obeng obeng dingh 6000' untuk baris yang di
# menu berbunyi 'Beng beng dingin 6K'. OCR-nya SUDAH BENAR - kamus kita
# yang merusaknya: 'beng' tidak terdaftar, dan tetangga terdekatnya
# 'obeng' (dari 'teh obeng', minuman khas Batam yang saya tambahkan
# sendiri). Alat menyebut nama obeng di daftar minuman.
#
# Merek tidak bisa ditebak dari bahasa. Satu-satunya obatnya mendaftar.
#
# CARA MENULIS - KESALAHAN NYATA, JANGAN DIULANG
#
#     Blok komentar ini dulu berada DI DALAM tanda kutip KAMUS_LUAS.
#     Tanda '#' tidak berarti apa-apa di dalam string, jadi seluruh
#     prosanya ikut terpecah jadi kosakata - 43 kata: 'wajib', 'saya',
#     'tetangga', 'merusaknya', 'opsional', 'perangkat'.
#
#     Yang terparah 'dingh'. Itu salah baca OCR yang saya tulis sendiri
#     sebagai CONTOH KESALAHAN, dan begitu ia terdaftar sebagai kata
#     sah, koreksi 'dingh' -> 'dingin' berhenti bekerja. Komentar yang
#     menjelaskan sebuah bug malah memasang bug itu.
#
#     Kata umum seperti 'dan', 'dari', 'tidak', 'yang' juga ikut masuk,
#     dan kata pendek adalah yang paling berbahaya di kolam tebakan:
#     makin pendek, makin banyak kata lain yang jaraknya dekat.
#
#     Komentar di kamus harus di LUAR tanda kutip. Ada uji yang
#     mengunci ini sekarang.
KAMUS_LUAS += """
beng chocolatos nutrisari
bagian hidangan sajian aneka macam istimewa favorit andalan rekomendasi
unggulan pelengkap kudapan gorengan pembuka penutup utama sarapan ringan
berat pemesanan antrian nota struk halal stok

restaurant resto warteg bistro angkringan lesehan pujasera foodcourt
gerai outlet cabang pusat dapur kitchen bakery toko kios lapak tenda
pondok saung dapoer waroeng depot

uduk kuning liwet campur putih tim kebuli biryani briyani bubur lontong
ketupat buras ketan jagung singkong kentang sagu roti bakpau mantau
kanji lemang nasgor

misoa ramen udon spaghetti spageti pasta makaroni yamin
tektek lamian carbonara

entok itik burung dara domba iga buntut babat usus ampela kepala paha
dada sayap ceker fillet filet sate satai tusuk dendeng empal rica balado
kalio tongseng bistik steak rawon kikil gepuk krecek

patin mujair bawal kerapu tenggiri tuna tongkol cakalang teri
bandeng kembung sarden salmon dori sotong kepiting rajungan kerang remis
tiram gonggong lokan cencaru belanak baronang selar peda jambal seafood

dadar ceplok orak arik puyuh pindang abon kornet naget tekwan cireng cilok pentol oncom sukro

oseng pecel crispy krispi katsu teriyaki lada mentega bumbu kuah kering
basah betutu woku arsik pallu bakakak lodeh brongkos garang asem tumpang bacem ungkep suwir mercon

sayuran sawi kubis brokoli buncis kacang panjang toge tauge terong terung
labu oyong gambas timun ketimun tomat wortel jamur enoki daun pepaya
nangka rebung pakis genjer selada kemangi seledri bawang bombay prei

cabe cabai rawit hijau merah dabu roa garam merica ketumbar kunyit jahe
lengkuas serai sereh kemiri santan nipis limau mayo mayones mayonaise
cheese barbeque blackpepper padang aren

emping rempeyek peyek lalap petai bala tempura risoles pastel
lumpia martabak terang bulan pisang kismis

tarik tubruk celup coffee latte cappuccino capucino espresso americano
mocca mocha milo oreo matcha taro vanilla vanila karamel caramel
hazelnut boba

juice avocado melon semangka apel sirsak strawberry stroberi naga
markisa nanas belimbing jambu muda degan lime peras

teler doger cendol dawet kopyor krim serut batu kristal gembira aqua
yakult yogurt kental wedang bandrek bajigur sekoteng pletok milkshake
smoothie float lemonade lychee leci

pahit segar renyah empuk lembut encer original level super mantap enak
lezat nikmat lengkap mini reguler large medium small double
single

piring mangkok mangkuk gelas cangkir botol potong iris ekor biji butir
buah kotak pack lusin kilo gram liter mili seperempat lembar helai sendok

sale potongan mulai hanya saja kembalian uang kartu debit kredit transfer
qris service charge termasuk belum sudah nett bersih harganya

pesanan delivery takeaway dine tempat dibawa pulang tunggu siap saji
ready kosong habis libur setiap tanggal bulan tahun

dengan tanpa untuk dari pada yang ini itu semua tiap bisa boleh tidak
ada ambil pilih silakan silahkan terima kasih selamat datang mohon maaf
wifi password toilet meja nomor telepon telp alamat jalan blok lantai

batam kepri lendir luti gendang prata canai obeng dagang melayu jawa
sunda minang aceh medan palembang bangka manado makassar bali lombok
solo jogja yogya semarang surabaya bandung betawi nusantara oriental
chinese western korea korean jepang japanese kapal selam malang
"""

# SATUAN, UKURAN, DAN KETERANGAN PESANAN.
#
# Kelompok yang selama ini bolong: menu penuh kata seperti 'per porsi',
# 'gelas besar', 'tanpa sambal', tapi kosakatanya tidak pernah didaftar
# karena bukan nama makanan.
#
# DIUKUR SEBELUM DIPASANG, pada bahan yang jujur - seluruh 683 kata
# kosakata menu, bukan hanya kata yang enak:
#
#     kamus sekarang    pulih 566   salah 38   diam 79
#     + 29 kata ini     pulih 566   salah 38   diam 79   <- tidak ada korban
#
#     23 dari 29 kata baru itu sendiri jadi bisa dipulihkan.
#
# DUA KATA SENGAJA DIBUANG dari usulan awal, karena terukur memakan
# korban - keduanya kata langka yang menabrak kata umum:
#
#     'takar'  -> 'taar' jadi ambigu antara takar dan TAWAR
#                 ('teh tawar' jauh lebih sering daripada takar)
#     'sepat'  -> 'depat' jadi ambigu antara sepat dan DEPOT
#
# Penjaga ambiguitas bekerja benar di kedua kasus: ia memilih DIAM
# daripada menebak. Tapi diam pada 'tawar' tetap kerugian, dan kata
# yang membelinya tidak sepadan. Menambah kosakata itu ada harganya;
# yang bisa dilakukan adalah menolak yang harganya lebih mahal.
KAMUS_LUAS += """
porsi gelas cangkir piring mangkuk mangkok botol kaleng bungkus cup
potong tusuk ekor iris tangkup sendok satuan
jumbo reguler regular large medium small mini extra single double
triple full half
kurang lebih pakai tambah mulai
anget sejuk beku asam asap matang mentah
krimer sambel saos pedes
komplet bonus combo bundling gratis
sangrai dinein
"""

# Nama lama dipertahankan supaya berkas lain yang mengimpornya tidak
# ikut berubah. Isinya sekarang gabungan kedua lapis.
KAMUS_DASAR = KAMUS_INTI + KAMUS_LUAS

MIRIP_ANGKA = {"o": "0", "O": "0", "l": "1", "I": "1", "i": "1",
               "S": "5", "s": "5", "B": "8", "Z": "2", "z": "2",
               "g": "9", "q": "9"}

# Ambang penjaga mutu. DISETEL ULANG setelah kamus diperbesar jadi 855
# kata - dan itu perlu, walaupun saya sempat menyimpulkan sebaliknya.
#
# Kesimpulan lama ("tidak perlu disetel") datang dari bidikan uji buatan,
# yang skornya memang terpisah jauh: baik 69-83, buruk 0. Bidikan
# SUNGGUHAN dari perangkat mengisi ruang di antaranya, dan di situlah
# ambang lama bocor:
#
#     skor  isi yang diucapkan ke pengguna
#      35%  "miounon pusan loe", "sirup ongn 5X 50515 2000"
#      26%  "sodomt goreng 70", "eose kesh koloo ayoe7A"
#
# Keduanya lolos ambang lama (20%) lalu dibacakan dengan nada yakin.
# Dari 26 bidikan nyata, ada celah bersih: <=35% selalu sampah, >=50%
# selalu memuat isi yang benar. Ambangnya ditaruh di celah itu.
AMBANG_BAIK = 65        # dibacakan tanpa peringatan
AMBANG_SEBAGIAN = 50    # dibacakan, tapi pengguna diberi tahu

MAKS_JARAK_OCR = 2      # jarak edit maksimum saat mencocokkan ke kamus
MIN_SENDIRI = 2         # kandidat harus terbaca utuh minimal sekian kali


class Kamus(set):
    """
    Himpunan kata + indeks pencari kandidat.

    KENAPA PERLU INDEKS

        koreksi_kata menghitung jarak edit terhadap SELURUH kamus untuk
        tiap kata tak dikenal. Dengan 564 kata dan ~100 kotak teks per
        halaman menu, itu puluhan ribu perbandingan - diukur di perangkat:
        5985 ms, hampir sama mahalnya dengan OCR-nya sendiri (6320 ms).

    DASAR PENYARINGNYA

        Kalau jarak edit dua kata <= 1, keduanya PASTI berbagi dua huruf
        DEPAN atau dua huruf BELAKANG. Satu perubahan tidak bisa merusak
        keduanya sekaligus pada kata >= 4 huruf.

        Dibuktikan lewat pencarian menyeluruh 20.000 pasangan: nol
        pelanggaran untuk batas 1.

    KENAPA HANYA UNTUK batas 1

        Pada batas 2 penyaring yang sama BOCOR - uji yang sama menemukan
        569 pasangan berjarak 2 yang tidak berbagi depan maupun belakang.
        Dua perubahan memang bisa merusak kedua ujungnya. Jadi kata >= 8
        huruf (satu-satunya yang memakai batas 2) tetap ditelusuri penuh.

        Cepat tapi salah lebih buruk daripada lambat tapi benar.
    """

    def __init__(self, iterable=(), inti=None):
        super().__init__(iterable)
        # inti = kata yang boleh jadi TEBAKAN. Sisanya boleh dikenali
        # tapi tidak pernah ditawarkan sebagai koreksi. Kalau tidak
        # disebut, seluruh isi jadi inti - perilaku lama, dipertahankan
        # supaya pemanggil lama tidak berubah artinya.
        self.inti = set(inti) & set(self) if inti is not None else set(self)
        self.bangun_indeks()

    def bangun_indeks(self):
        # SEMUA kata inti diindeks, termasuk yang pendek. Versi pertama
        # hanya mengindeks kata >= 4 huruf, dan akibatnya 'teh', 'jus',
        # 'mie' tidak pernah jadi kandidat: 'tegh' gagal dikoreksi jadi
        # 'teh', dan 'jaus' salah dikoreksi jadi 'saus' karena pesaingnya
        # 'jus' tidak terlihat. Uji pembanding yang menangkapnya.
        self.depan, self.belakang = {}, {}
        for k in self.inti:
            self.depan.setdefault(k[:2], []).append(k)
            self.belakang.setdefault(k[-2:], []).append(k)
        return self

    def kandidat(self, kata, batas):
        """
        Kata yang MUNGKIN berjarak <= batas.

        Selalu mengembalikan daftar, tidak pernah None - kalau None,
        koreksi_kata akan jatuh ke telusur seluruh kamus dan lapisan
        luas ikut jadi bahan tebakan, persis yang mau dihindari.
        """
        if batas == 1 and len(kata) >= 4:
            hasil = list(self.depan.get(kata[:2], ()))
            hasil += self.belakang.get(kata[-2:], ())
            return hasil
        return list(self.inti)


def muat_kamus(kata_utuh=None):
    """
    Bangun kamus dari KAMUS_DASAR + bank kata audio (kata_utuh.py).

    kata_utuh: dict {"nasi.wav": "nasi", ...} atau None.
    Bank audio dan kamus OCR sengaja berbagi kosakata yang sama - kalau
    sebuah kata bisa diucapkan, ia juga bisa dikoreksi, dan sebaliknya.

    Mengembalikan Kamus (turunan set), jadi pemakai lama tidak perlu
    diubah: `k in kamus`, `len(kamus)`, iterasi, semuanya tetap jalan.
    """
    kata = set(KAMUS_DASAR.split())
    if kata_utuh:
        for k in kata_utuh:
            kata.add(k.rsplit(".", 1)[0].lower())
    sah = {k for k in kata if k.isalpha() and len(k) >= 2}
    # SELURUH kosakata menu jadi bahan tebakan; bank audio TIDAK.
    #
    # Bank audio dikumpulkan supaya kata bisa DIUCAPKAN, bukan karena
    # muncul di menu - isinya 'ayah', 'sekolah', 'kendaraan', 'berjalan'.
    # Kata umum itulah yang menangkapi sampah OCR, dan yang membuat
    # 'Ayan' gagal jadi 'ayam' di log perangkat: ia bersaing dengan
    # 'ayah' yang sama dekatnya, jadi dianggap ambigu lalu dibuang.
    return Kamus(sah, inti={k for k in KAMUS_DASAR.split() if k in sah})


def jarak_edit(a, b, batas=99):
    """Levenshtein dengan pemotongan dini."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > batas:
        return batas + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        if min(cur) > batas:
            return batas + 1
        prev = cur
    return prev[-1]


def perbaiki_angka(tok):
    """
    Token yang jelas angka -> balikkan huruf mirip-angka.
    '15.OOO' -> '15.000',  'l0000' -> '10000',  '5OOO' -> '5000'

    Huruf mirip-angka ikut dihitung sebagai bukti; tanpa itu '5OOO'
    (1 digit, 3 huruf) tidak akan pernah lolos ambang - padahal itu bentuk
    kesalahan harga yang paling sering.
    """
    isi = [c for c in tok if c.isalnum()]
    if not isi:
        return tok
    n_angka = sum(1 for c in isi if c.isdigit() or c in MIRIP_ANGKA)
    if n_angka < len(isi) * 0.8 or not any(c.isdigit() for c in isi):
        return tok
    return "".join(c if c.isdigit() else MIRIP_ANGKA.get(c, c) for c in tok)


def harga_k(tok):
    """
    '5K' / 'SK' / '15k' -> '5000' / '5000' / '15000'.  None kalau bukan.

    KENAPA PERLU

        Menu warung Indonesia hampir selalu menulis harga sebagai '5K',
        bukan 'Rp 5.000'. Dan OCR membaca angka 5 sebagai huruf S hampir
        setiap kali. Pada uji menu cetak, seluruh harga hilang begitu
        saja: 'Nutrisari SK' dan 'SusuDingin SR' - pengguna mendengar
        nama minuman tanpa harga, yang justru bagian paling dibutuhkan.

        perbaiki_angka() tidak menolongnya karena 'SK' tidak punya satu
        digit pun, jadi ia tidak pernah lolos ambang "jelas angka".

    DIKEMBALIKAN SEBAGAI RUPIAH PENUH

        '5000', bukan '5K', supaya pengucapnya memakai jalur angka yang
        sudah ada - eja_angka() tahu cara menyebut 5000, tidak tahu 'K'.

    PENGAMAN

        - hanya 1..3 karakter sebelum K, jadi 'masak' (depan 'masa')
          tidak ikut terseret
        - setiap karakter depan harus benar-benar memetakan ke digit
        - hasilnya harus 1..999 ribu, di luar itu ditolak
        - dipanggil SESUDAH pencocokan kamus, jadi kata sah seperti
          'sok' atau 'kok' aman lebih dulu
    """
    t = "".join(c for c in str(tok) if c.isalnum())
    if len(t) < 2 or t[-1] not in "Kk":
        return None
    depan = t[:-1]
    if len(depan) > 3:
        return None
    angka = ""
    for c in depan:
        if c.isdigit():
            angka += c
        elif c in MIRIP_ANGKA:
            angka += MIRIP_ANGKA[c]
        elif c.lower() in MIRIP_ANGKA:
            angka += MIRIP_ANGKA[c.lower()]
        else:
            return None
    if not angka.isdigit():
        return None
    n = int(angka)
    if not 1 <= n <= 999:
        return None
    return str(n * 1000)


def koreksi_kata(w, kamus, hanya_kamus=True):
    """
    Cocokkan satu kata ke kamus.

    hanya_kamus=True: kata tak dikenal DIBUANG. Untuk alat baca tunanetra
    ini wajib - pengguna tidak bisa memverifikasi dengan mata, jadi apa pun
    yang keluar dari speaker dianggap benar. Diam lebih baik daripada
    mengucapkan "meukamng ayacong sopanbina".
    """
    w = "".join(c for c in w if ord(c) < 128)      # buang halusinasi Han
    if not w.strip():
        return ""
    bersih = "".join(c for c in w.lower() if c.isalnum())
    if not bersih:
        return ""

    # Kamus lebih dulu, supaya kata sah tidak diseret jadi harga:
    # 'sok' -> 's'=5 'o'=0 -> 50000 kalau urutannya terbalik.
    if bersih in kamus:
        return bersih
    rp = harga_k(w)
    if rp:
        return rp
    if any(c.isdigit() for c in bersih):
        return perbaiki_angka(w)
    # Kata < 4 huruf tidak pernah ditebak, hanya dicocokkan persis di atas.
    #
    # Pada kata 3 huruf, satu perubahan adalah SEPERTIGA kata - itu bukan
    # mengoreksi, itu mengganti. Diukur: menaikkan lantai dari 3 ke 4
    # memotong sampah OCR yang lolos dari 31 jadi 18, dengan ongkos 7
    # pemulihan. Contoh yang hilang dari keluaran: 'sip'->'sop',
    # 'pir'->'air', 'sag'->'sagu' - tak satu pun benar.
    if len(bersih) < 4:
        return "" if hanya_kamus else bersih

    # Ambang jarak edit HARUS ikut panjang kata.
    #
    # Dulu 2 untuk apa pun di atas 4 huruf. Pada kata 5 huruf itu berarti
    # 40% isinya boleh berubah - bukan koreksi lagi, tapi penggantian.
    # Pada uji menu cetak, 'Indom' (potongan Indomie yang terbaca BENAR)
    # diganti jadi 'oncom', dan pengguna diberi tahu ada makanan yang
    # tidak ada di menu. Salah dengan yakin lebih buruk daripada
    # membiarkan kata aslinya lewat.
    batas = 1 if len(bersih) < 8 else MAKS_JARAK_OCR

    # Kalau kamusnya punya indeks, ambil hanya kandidat yang mungkin.
    # Kamus biasa (set polos) tetap didukung - telusur penuh.
    telusur = None
    if hasattr(kamus, "kandidat"):
        telusur = kamus.kandidat(bersih, batas)
    if telusur is None:
        telusur = kamus

    kand = []
    for k in telusur:
        if abs(len(k) - len(bersih)) > batas:
            continue
        d = jarak_edit(bersih, k, batas)
        if d <= batas:
            kand.append((d, k))
    if telusur is not kamus:
        kand = sorted(set(kand))    # depan+belakang bisa memuat kata sama
    if not kand:
        return "" if hanya_kamus else bersih
    kand.sort()
    if len(kand) > 1 and kand[0][0] == kand[1][0]:
        return "" if hanya_kamus else bersih       # ambigu, jangan tebak
    return kand[0][1]


def koreksi_baris(teks, kamus, hanya_kamus=True):
    return " ".join(x for x in (koreksi_kata(w, kamus, hanya_kamus)
                                for w in str(teks).split()) if x)


def _bisa_dipecah(kata, kamus_kata):
    """True kalau `kata` bisa disusun penuh dari kata-kata lain di daftar."""
    n = len(kata)
    bisa = [False] * (n + 1)
    bisa[0] = True
    for i in range(1, n + 1):
        for j in range(i):
            if bisa[j] and kata[j:i] in kamus_kata:
                bisa[i] = True
                break
    return bisa[n]


def bersihkan_kata(kata, kamus=None):
    """
    Rapikan hasil voting sebelum diucapkan. Dua pembersihan:

    1. HURUF TUNGGAL. OCR sering memuntahkan satu huruf nyasar dari
       bayangan atau tepi objek - "acome o". Angka tunggal dipertahankan
       karena bisa jadi bagian harga.

    2. KATA TERGABUNG. Deteksi teks kadang memotong "FOR THE YOUNG" jadi
       satu kotak, kadang tiga. Voting menyimpan kedua bentuk sehingga
       keluar "for the fortheyoung young".

       Yang dibuang ditentukan oleh kamus, bukan panjang. Tanpa itu,
       "restoran" ikut terbuang hanya karena kebetulan ada pecahan
       "rest" + "oran" - kata yang benar justru hilang:

         kata panjang ADA di kamus     -> pertahankan, buang pecahannya
         kata panjang TIDAK di kamus   -> buang, pecahannya yang benar
    """
    kamus = kamus or set()
    simpan = [w for w in kata if w and (len(w) > 1 or w.isdigit())]
    lain = set(simpan)

    buang = set()
    for w in simpan:
        sisa = lain - {w} - buang
        if len(w) <= 5 or not sisa or not _bisa_dipecah(w, sisa):
            continue
        if w in kamus:
            # kata utuh menang: pecahan penyusunnya yang dibuang
            for p in list(sisa):
                if p in w and p not in kamus:
                    buang.add(p)
        else:
            buang.add(w)
    return [w for w in simpan if w not in buang]


def _boleh_gabung(a, b, kamus):
    """
    Apakah dua kata layak dianggap ejaan yang sama?

    Syaratnya sengaja ketat, karena salah gabung lebih berbahaya
    daripada gagal gabung:

      - tidak mengandung angka. '1000' dan '2000' berjarak 1, dan
        menggabungkannya akan menyebut harga yang salah.
      - minimal 4 huruf. Kata pendek yang berjarak 1 biasanya memang
        kata yang berbeda ('nasi' / 'sisa' bukan, tapi 'ayam' / 'ayah' ya).
      - tidak keduanya ada di kamus. Kalau dua-duanya kata sah,
        keduanya berhak berdiri sendiri.
      - selisih panjang <= 2, dan jarak edit <= 1 (<=6 huruf) atau 2.
    """
    if any(c.isdigit() for c in a) or any(c.isdigit() for c in b):
        return False
    if len(a) < 4 or len(b) < 4:
        return False
    if a in kamus and b in kamus:
        return False
    if abs(len(a) - len(b)) > 2:
        return False
    batas = 1 if max(len(a), len(b)) <= 6 else 2
    return jarak_edit(a, b, batas) <= batas


def gabung_mirip(hitung, kamus=None):
    """
    Satukan ejaan-ejaan berbeda dari kata yang sama -> {kata: wakil}

    MASALAH YANG DISELESAIKAN

        Voting per kata menghitung kecocokan PERSIS. Kalau OCR membaca
        satu kata dengan lima ejaan berbeda, tiap ejaan cuma dapat 1
        suara dan semuanya gugur - kata yang sebenarnya terbaca di lima
        frame malah hilang sama sekali:

            maixhub 1 | moixhub 1 | mafxhub 1 | malxhub 1 | maihub 1

        Digabung, kata itu punya 5 suara dan lolos.

    MEMILIH WAKILNYA

        Kalau ada anggota yang terdaftar di kamus, itu yang dipakai.
        Kalau tidak, dipakai MEDOID - anggota dengan jumlah jarak edit
        terkecil ke semua anggota lain. Ini bukan tebakan sembarangan:
        kesalahan OCR menyebar di sekitar bacaan yang benar, jadi ejaan
        yang paling 'di tengah' adalah kandidat terbaik.

            maixhub -> 1+1+1+1 = 4   <- terpilih
            mafxhub -> 1+2+2+2 = 7
            moixhub -> 1+2+2+2 = 7
    """
    kamus = kamus or set()
    urut = sorted(hitung, key=lambda w: (-hitung[w], -len(w), w))
    kelompok = []                       # daftar berisi daftar anggota
    for w in urut:
        for g in kelompok:
            if any(_boleh_gabung(w, m, kamus) for m in g):
                g.append(w)
                break
        else:
            kelompok.append([w])

    peta = {}
    for g in kelompok:
        if len(g) == 1:
            peta[g[0]] = g[0]
            continue
        di_kamus = [m for m in g if m in kamus]
        if di_kamus:
            wakil = max(di_kamus, key=lambda m: hitung[m])
        else:
            wakil = min(g, key=lambda m: (sum(jarak_edit(m, o) for o in g),
                                          -hitung[m], m))
        for m in g:
            peta[m] = wakil
    return peta


def vote_kata(riwayat, min_frame=2, kamus=None):
    """
    Voting per KATA, bukan per kalimat -> (teks, dukungan_tertinggi)

    KENAPA INI PERLU

    Filter kamus membuang kata tak dikenal supaya sampah OCR tidak ikut
    diucapkan. Tapi itu juga membuang kata yang SUNGGUHAN ada namun bukan
    kata Indonesia: nama merek, nama warung, singkatan. "ACOME" terbaca
    dengan benar lalu lenyap, dan pengguna dikira alat tidak melihat apa-apa.

    Yang membedakan kata asli dari sampah bukan ada-tidaknya di kamus,
    melainkan KEKONSISTENAN:

        "ACOME"     terbaca sama di 5 dari 5 frame  -> nyata
        "meukamng"  muncul di 1 frame saja          -> sampah

    Jadi kata tak dikenal tetap dipertahankan asal muncul di beberapa frame.
    Urutannya dijaga memakai rata-rata posisi relatifnya.
    """
    frames = [r.split() for r in riwayat if r]
    if not frames:
        return "", 0
    kasar, posisi = {}, {}
    for f in frames:
        lihat = set()
        for i, w in enumerate(f):
            if w in lihat:
                continue
            lihat.add(w)
            kasar[w] = kasar.get(w, 0) + 1
            rel = i / (len(f) - 1) if len(f) > 1 else 0.0
            posisi.setdefault(w, []).append(rel)

    # Satukan ejaan berbeda dari kata yang sama SEBELUM ambang dihitung,
    # supaya kata yang terbaca lima kali dengan lima ejaan tidak gugur.
    peta = gabung_mirip(kasar, kamus)
    hitung, tempat = {}, {}
    for w, c in kasar.items():
        wakil = peta.get(w, w)
        hitung[wakil] = hitung.get(wakil, 0) + c
        tempat.setdefault(wakil, []).extend(posisi[w])
    # Dua ejaan bisa muncul di frame yang SAMA; tanpa batas ini dukungannya
    # bisa melebihi jumlah frame yang ada.
    hitung = {w: min(c, len(frames)) for w, c in hitung.items()}
    posisi = tempat

    lolos = [w for w, c in hitung.items() if c >= min_frame]
    lolos = bersihkan_kata(lolos, kamus)
    if not lolos:
        return "", (max(hitung.values()) if hitung else 0)
    urut = sorted(lolos, key=lambda w: sum(posisi[w]) / len(posisi[w]))
    return " ".join(urut), max(hitung.values())


def vote_teks(riwayat, min_sendiri=MIN_SENDIRI):
    """
    Voting bacaan OCR lintas frame -> (teks, dukungan).

    Bacaan pendek adalah PENGAMATAN SEBAGIAN dari bacaan panjang, bukan
    saingannya: sebagian frame cuma menangkap separuh papan. Jadi dukungan
    tiap kandidat = jumlah frame yang kata-katanya himpunan bagian darinya.
    Frame 'menu' ikut mendukung 'menu restoran', tidak sebaliknya.

    Syarat min_sendiri mencegah satu bacaan sampah yang sangat panjang
    memanen semua suara hanya karena ia superset dari segalanya.
    """
    isi = [x for x in riwayat if x]
    if not isi:
        return "", 0
    cacah = {}
    for t in isi:
        cacah[t] = cacah.get(t, 0) + 1

    layak = {t: n for t, n in cacah.items() if n >= min_sendiri} or cacah
    skor = []
    for kand in layak:
        set_k = set(kand.split())
        dukung = sum(n for t, n in cacah.items() if set(t.split()) <= set_k)
        skor.append((dukung, len(set_k), kand))
    skor.sort(reverse=True)
    dukung, _, kand = skor[0]
    return kand, dukung


# ======================================================================
# 7. PEMANTAU BATERAI  (histeresis + anti-spam)
# ======================================================================
# Tiga masalah yang membuat peringatan baterai naif tidak bisa dipakai:
#
#  1. BERKEDIP DI AMBANG. Baterai yang bertahan di 3,55 V akan naik-turun
#     beberapa milivolt dan memicu peringatan berulang-ulang. Solusinya
#     histeresis: turun memicu di 3,55 tapi baru dianggap pulih di 3,63.
#
#  2. KEDIP BEBAN. Saat NPU menjalankan inferensi, arus melonjak dan
#     tegangan anjlok sesaat. Tanpa perataan, itu terbaca sebagai baterai
#     lemah padahal cuma beban sesaat. Solusinya EMA + syarat bertahan
#     beberapa detik sebelum berpindah level.
#
#  3. SPAM SUARA. "Baterai lemah" yang diucapkan tiap 20 detik akan membuat
#     alat tidak bisa dipakai. Peringatan diulang berkala saja - kritis
#     lebih sering karena taruhannya lebih besar.

# (turun_ke, pulih_di) - jarak 0,08 V, jauh di atas resolusi ADC 0,006 V
AMBANG_KRITIS = (3.40, 3.48)
AMBANG_LEMAH = (3.55, 3.63)
AMBANG_PENUH = 4.15

# Jeda pengulangan MEMANJANG tiap kali diucapkan, tidak tetap.
#
# Dengan jeda tetap 60 detik, simulasi pengosongan menghasilkan 111 kali
# "Baterai kritis" berturut-turut - menyiksa, dan justru membuat alat tidak
# bisa dipakai persis saat baterai menipis. Peringatan pertama mendesak,
# sisanya cukup mengingatkan.
ULANG_KRITIS = [60.0, 60.0, 120.0, 300.0, 600.0]     # terakhir dipakai terus
ULANG_LEMAH = [300.0, 600.0, 900.0]
TAHAN_DETIK = 3.0        # level harus bertahan sekian detik sebelum diakui


class PantauBaterai:
    """
    Masukkan tegangan mentah, dapatkan nama peringatan saat perlu diucapkan.

    perbarui(volt, sekarang) -> "kritis" | "lemah" | "penuh" | None
    """

    def __init__(self, alfa=0.2):
        self.alfa = alfa
        self.halus = None
        self.level = "normal"
        self._calon = None
        self._sejak = 0.0
        self._terakhir_ucap = {}
        self._jml_ucap = {}          # berapa kali tiap level sudah diucapkan

    def _level_dari(self, v):
        """Level berikutnya, dengan histeresis relatif terhadap level kini."""
        if self.level == "kritis":
            if v >= AMBANG_LEMAH[1]:
                return "normal"
            if v >= AMBANG_KRITIS[1]:
                return "lemah"
            return "kritis"
        if self.level == "lemah":
            if v <= AMBANG_KRITIS[0]:
                return "kritis"
            if v >= AMBANG_LEMAH[1]:
                return "normal"
            return "lemah"
        # normal / penuh
        if v <= AMBANG_KRITIS[0]:
            return "kritis"
        if v <= AMBANG_LEMAH[0]:
            return "lemah"
        if v >= AMBANG_PENUH:
            return "penuh"
        return "normal"

    def perbarui(self, volt, sekarang):
        if volt is None or volt <= 0.5:
            return None
        self.halus = volt if self.halus is None else \
            (1 - self.alfa) * self.halus + self.alfa * volt

        baru = self._level_dari(self.halus)

        # level harus bertahan TAHAN_DETIK sebelum diakui -> kebal kedip beban
        if baru != self.level:
            if baru != self._calon:
                self._calon, self._sejak = baru, sekarang
                return None
            if sekarang - self._sejak < TAHAN_DETIK:
                return None
            lama, self.level = self.level, baru
            self._calon = None
            # naik ke kondisi lebih baik: reset hitungan agar peringatan
            # berikutnya kembali mendesak sejak awal
            if baru in ("normal", "penuh"):
                for k in ("lemah", "kritis"):
                    self._terakhir_ucap.pop(k, None)
                    self._jml_ucap.pop(k, None)
            if baru == "normal":
                return None
        else:
            self._calon = None

        if self.level not in ("kritis", "lemah", "penuh"):
            return None
        if self.level == "penuh":
            if "penuh" in self._terakhir_ucap:
                return None
            self._terakhir_ucap["penuh"] = sekarang
            return "penuh"

        tabel = ULANG_KRITIS if self.level == "kritis" else ULANG_LEMAH
        n = self._jml_ucap.get(self.level, 0)
        jeda = tabel[min(n, len(tabel) - 1)]      # jeda memanjang lalu tetap
        lalu = self._terakhir_ucap.get(self.level)
        if lalu is None or sekarang - lalu >= jeda:
            self._terakhir_ucap[self.level] = sekarang
            self._jml_ucap[self.level] = n + 1
            return self.level
        return None


# ======================================================================
# 8. POLA TOMBOL  (satu tombol, tiga perintah)
# ======================================================================
# Pengguna tunanetra tidak bisa memakai tombol layar. Satu tombol fisik
# harus cukup untuk semua perintah, jadi dibedakan lewat POLA tekanan:
#
#     1x tekan        -> proses (baca uang / teks)
#     2x tekan cepat  -> ganti mode, diucapkan
#     tahan >=1,2 dtk -> ulangi hasil terakhir
#
# Ini menghilangkan kebutuhan klasifikasi gestur tangan untuk memilih mode:
# satu tombol sudah cukup, dan tidak menambah beban NPU maupun titik gagal.

TAHAN_LAMA = 1.2         # detik, dianggap "tahan"
JEDA_GANDA = 0.45        # jarak maksimum antar tekan untuk dianggap ganda
DEBOUNCE = 0.05


class PolaTombol:
    """
    perbarui(ditekan, sekarang) -> "tunggal" | "ganda" | "tahan" | None

    'tunggal' sengaja tertunda sampai JEDA_GANDA berlalu - itu satu-satunya
    cara membedakannya dari tekanan pertama sebuah 'ganda'. Penundaan 0,45
    detik tidak terasa mengganggu, dan hasil salah jauh lebih mengganggu.
    """

    def __init__(self):
        self._lalu = False
        self._mulai = 0.0
        self._lepas = None
        self._tunggu = 0
        self._abaikan = False

    def perbarui(self, ditekan, sekarang):
        ditekan = bool(ditekan)
        turun = ditekan and not self._lalu
        naik = (not ditekan) and self._lalu
        self._lalu = ditekan

        if turun:
            self._mulai = sekarang
            self._abaikan = False
            if self._tunggu == 1 and self._lepas is not None \
                    and sekarang - self._lepas <= JEDA_GANDA:
                self._tunggu = 0
                self._lepas = None
                self._abaikan = True         # jangan hitung lagi saat dilepas
                return "ganda"

        if naik and not self._abaikan:
            lama = sekarang - self._mulai
            if lama < DEBOUNCE:
                return None
            if lama >= TAHAN_LAMA:
                self._tunggu = 0
                self._lepas = None
                return "tahan"
            self._tunggu = 1
            self._lepas = sekarang

        # tidak ada tekanan kedua dalam jendela -> memang tunggal
        if self._tunggu == 1 and self._lepas is not None \
                and not ditekan and sekarang - self._lepas > JEDA_GANDA:
            self._tunggu = 0
            self._lepas = None
            return "tunggal"
        return None


# ======================================================================
# SELF-TEST
# ======================================================================
# ======================================================================
# 9. PENYARINGAN BARIS SEBELUM DIUCAPKAN
# ======================================================================
# Ditambahkan 26 Agu, seluruhnya dari log perangkat sungguhan saat
# membaca Sampel Menu/20260813_140428.jpg. Tiga cacat terlihat di sana,
# dan ketiganya sampai ke telinga pengguna:
#
#   1. Baris sampah ikut dibacakan
#        "beka setiap hn greaygrnatste"
#        "boa eorp hun 9eyyeah?"
#      Itu alamat dan jam buka yang terbaca kacau. Bagi pengguna yang
#      tidak bisa melihat, bunyi itu TERDENGAR SEPERTI ISI MENU.
#
#   2. Baris kembar lolos dua kali
#        "mie ayam special Rp12.000"  dan  "mie ayam sptolal Rp12000"
#      Satu baris fisik, dua hasil baca, keduanya diucapkan.
#
#   3. Bagian sasaran diumumkan tapi pembacaan tetap dari atas
#      Log: "mulai di bagian 2/2 ('minuman')" lalu baris 1 = gado-gado.
#      Janji yang tidak ditepati - dan itu membingungkan.

def harga_sah(tok):
    """
    Apakah token ini harga yang wajar? -> True/False

    Menyaring "Rpin.000", "R018.000", "Res000" yang muncul di log nyata.
    Harga yang rusak lebih baik TIDAK diucapkan sama sekali - prinsip
    yang sudah dipakai vote_baris: nama tanpa harga masih berguna,
    harga yang salah tidak.
    """
    t = str(tok).strip().lower()
    for awalan in ("rp.", "rp"):
        if t.startswith(awalan):
            t = t[len(awalan):]
            break
    t = t.strip(".,:;-")
    if not t or not all(c.isdigit() or c == "." for c in t):
        return False
    angka = t.replace(".", "")
    if not angka.isdigit():
        return False
    n = int(angka)
    # Batas dari kenyataan menu warung, bukan karangan.
    if not (500 <= n <= 500000):
        return False
    # KELIPATAN 100. Ditambahkan 27 Agu dari log perangkat: alat
    # mengucapkan "seratus satu ribu tiga ratus enam puluh rupiah"
    # untuk token 'Rp101.360' - angka yang tidak mungkin jadi harga
    # menu. Tidak ada warung memasang harga 101.360.
    #
    # Aturan ini menangkap seluruh contoh salah baca di log itu
    # (Rp101.360, R12.40, R15.50, '45', '0,000', '.000') sambil
    # meloloskan semua harga sungguhan (17.000, 5.000, 9000, 12.500).
    return n % 100 == 0


def harga_layak_diucapkan(nama, kamus):
    """
    Bolehkah harga baris ini diucapkan? -> True/False

    ATURAN: TANPA NAMA, TIDAK ADA HARGA.

    Dari log perangkat 27 Agu:

        [BACA] bobsk gnrerg 20.000
        [UCAP] 0/2 kata dikenali -> nama DITEKAN
        [UCAP] dua -> puluh -> ribu -> rupiah      <- HARGA TETAP KELUAR

    Pengguna mendengar "dua puluh ribu rupiah" tanpa tahu itu harga
    APA. Angka telanjang lebih buruk daripada diam: ia terdengar
    seperti keterangan yang lengkap, padahal menggantung.

    Kalau namanya sendiri tidak layak diucapkan, harganya juga tidak.
    """
    kamus = kamus or set()
    kata = [w for w in str(nama).split()
            if not any(c.isdigit() for c in w)]
    if not kata:
        return False
    return any(str(w).lower().strip(".,:;-!?'\"") in kamus for w in kata)


def kunci_harga(tok):
    """Harga -> angka polos tanpa nol depan, untuk dibandingkan. -> str."""
    t = "".join(c for c in str(tok) if c.isdigit())
    return t.lstrip("0") or ("0" if t else "")


def _token_aneh(t, kamus):
    """
    Token yang tidak mungkin sebuah kata. -> True/False

    Tiga tanda, semuanya diambil dari sampah nyata di log:
      - campur huruf dan angka   "9eyyeah", "2000WB", "Rpin.000"
      - satu-dua huruf asing     "hn", "os"
      - panjang dan tak dikenal  "greaygrnatste" (13 huruf)
    """
    t = str(t).lower().strip(".,:;-!?'\"")
    if not t:
        return True
    if t in kamus:
        return False
    ada_huruf = any(c.isalpha() for c in t)
    ada_angka = any(c.isdigit() for c in t)
    if ada_huruf and ada_angka:
        return True
    if not ada_huruf:
        return True
    if len(t) <= 2:
        return True
    if len(t) >= 12:
        return True
    return False


def baris_sampah(teks, kamus, ambang_kenal=0.5, ambang_aneh=0.5):
    """
    Apakah baris ini layak DIBUANG sebelum diucapkan? -> True/False

    SENGAJA BERAT SEBELAH KE ARAH MENYIMPAN. Membuang satu item menu
    yang benar lebih merugikan daripada membiarkan satu baris sampah -
    pengguna bisa menekan tombol lagi untuk sampah, tapi tidak akan
    pernah tahu ada item yang hilang.

    Diuji pada baris nyata dari log:

        SIMPAN  "menu makan"            2/2 kata dikenal
        SIMPAN  "warung larana"         1/2 - nama tempat, tetap aman
        SIMPAN  "gadogado Rp15.000"     harga sah, nama tak dikenal
        SIMPAN  "nas goreng Rpin.000"   1/2 dikenal
        BUANG   "beka setiap hn greaygrnatste"
        BUANG   "boa eorp hun 9eyyeah?"
        BUANG   "+1234567890"
        BUANG   "0.00S/D 2000WB"
    """
    kamus = kamus or set()
    tok = str(teks).split()
    if not tok:
        return True

    harga = [t for t in tok if harga_sah(t)]
    kata = [t for t in tok if t not in harga]

    if not kata:
        # Hanya angka. Sah kalau memang harga, sampah kalau bukan.
        return not harga

    kenal = sum(1 for t in kata
                if str(t).lower().strip(".,:;-!?'\"") in kamus)
    aneh = sum(1 for t in kata if _token_aneh(t, kamus))

    # Ada harga sah + minimal satu kata dikenal = hampir pasti item menu.
    if harga and kenal >= 1:
        return False
    if kenal / float(len(kata)) >= ambang_kenal:
        return False
    if aneh / float(len(kata)) >= ambang_aneh:
        return True
    if kenal == 0 and len(kata) >= 3:
        return True
    return False


def saring_sampah(baris, kamus, maks_buang=0.5):
    """
    Buang baris sampah. -> (baris_bersih, jumlah_dibuang)

    PENGAMAN YANG TIDAK BOLEH DIHAPUS

        Kalau lebih dari maks_buang bagian baris dianggap sampah, yang
        rusak bukan beberapa baris - melainkan SELURUH bidikan. Dalam
        keadaan itu penyaring mengembalikan apa adanya dan menyerahkan
        keputusan ke mutu_bacaan, yang memang bertugas menyuruh
        pengguna membidik ulang.

        Tanpa pengaman ini, bidikan buruk akan menghasilkan dua-tiga
        baris sisa yang terdengar rapi dan meyakinkan - persis mode
        kegagalan paling berbahaya untuk pengguna tunanetra.
    """
    baris = list(baris or [])
    if not baris:
        return [], 0
    simpan = [b for b in baris if not baris_sampah(b, kamus)]
    dibuang = len(baris) - len(simpan)
    if dibuang > maks_buang * len(baris):
        return baris, 0
    return simpan, dibuang


def _kata_cocok(a, b):
    """Dua kata dianggap ejaan berbeda dari kata yang sama. -> bool.

    Ambangnya SENGAJA KETAT (jarak <= panjang//3). Longgar sedikit saja,
    'soto' dan 'sate' ikut tergabung - keduanya menu nyata, dan
    menggabungkannya berarti satu item hilang dari telinga pengguna.
    """
    if a == b:
        return True
    n = min(len(a), len(b))
    if n < 4:
        return False
    batas = max(1, n // 3)
    return jarak_edit(a, b, batas + 1) <= batas


def _mirip_nama(a, b, ambang=0.6):
    """Seberapa mirip dua nama item? -> bool."""
    wa, wb = str(a).split(), str(b).split()
    if not wa or not wb:
        return False
    sisa = list(wb)
    sama = 0
    for w in wa:
        for i, x in enumerate(sisa):
            if _kata_cocok(w, x):
                sama += 1
                sisa.pop(i)
                break
    return sama / float(min(len(wa), len(wb))) >= ambang


def _nilai_nama(nama, kamus):
    """Nama mana yang lebih layak diucapkan? -> skor, makin besar makin baik."""
    tok = str(nama).split()
    if not tok:
        return -99
    kenal = sum(1 for t in tok
                if str(t).lower().strip(".,:;-!?'\"") in (kamus or set()))
    aneh = sum(1 for t in tok if _token_aneh(t, kamus or set()))
    return kenal * 2 - aneh


def gabung_baris_kembar(baris, kamus=None):
    """
    Satukan baris yang sebenarnya satu item. -> list baris

    Dari log nyata:
        "mie ayam special Rp12.000" + "mie ayam sptolal Rp12000"
        -> "mie ayam special Rp12.000"

    Nama diambil dari varian dengan kata paling banyak dikenal kamus;
    harga diambil dari varian yang harganya SAH. Jadi hasil gabungan
    bisa lebih baik daripada kedua asalnya - nama dari yang satu, harga
    dari yang lain.

    PENGGABUNGANNYA KONSERVATIF. Baris yang tidak jelas kembar
    dibiarkan berdua. Mengucapkan satu item dua kali cuma mengganggu;
    menggabungkan dua item berbeda menghilangkan satu dari menu, dan
    pengguna tidak punya cara mengetahuinya.
    """
    kamus = kamus or set()
    kelompok = []          # [[(nama, [harga...]), ...], ...]
    for b in baris or []:
        nama, harga = pisah_harga(b)
        if not nama:
            kelompok.append([(str(b), [])])
            continue
        taruh = None
        for g in kelompok:
            if any(_mirip_nama(nama, n) for n, _ in g):
                taruh = g
                break
        if taruh is None:
            kelompok.append([(nama, harga)])
        else:
            taruh.append((nama, harga))

    hasil = []
    for g in kelompok:
        if len(g) == 1 and not g[0][1]:
            # baris tanpa nama/harga - kembalikan apa adanya
            nama, harga = g[0]
            hasil.append((nama + " " + " ".join(harga)).strip())
            continue
        nama = max(g, key=lambda x: (_nilai_nama(x[0], kamus), len(x[0])))[0]
        # Harga dipilih dua tahap: NILAI dulu, baru EJAANNYA.
        #
        # Versi pertama saya langsung memakai max() pada string, dan pada
        # data nyata itu memilih "Rp12000" mengalahkan "Rp12.000" hanya
        # karena '0' > '.' dalam urutan karakter. Nilainya sama, tapi
        # ejaan bertitik lebih mungkin hasil bacaan yang utuh.
        nilai = {}
        ejaan = {}
        for _, hs in g:
            for h in hs:
                if not harga_sah(h):
                    continue
                k = kunci_harga(h)
                nilai[k] = nilai.get(k, 0) + 1
                ejaan.setdefault(k, []).append(h)
        pilih = ""
        if nilai:
            k = max(nilai, key=lambda x: (nilai[x], x))
            pilih = max(ejaan[k],
                        key=lambda h: ("." in h,
                                       h.lower().startswith("rp"),
                                       -len(h)))
        hasil.append((nama + " " + pilih).strip())
    return hasil


def urut_dari_bagian(bagian, idx):
    """
    Susun ulang supaya bagian sasaran dibaca DULUAN. -> list baris

    bagian: [(judul, [isi, ...]), ...] dari kelompokkan_bagian()
    idx   : indeks bagian sasaran dari bagian_dituju()

    MASALAH YANG DIPERBAIKI - terlihat langsung di log perangkat:

        [JUDUL] mulai di bagian 2/2 ('minuman', 1 isi)
        [JUDUL] sasaran diucapkan: 'minuman'
        [BACA] baris 1-3 dari 13
        [BACA]   gadogado Rp15.000        <- ini isi MAKANAN

        Alat mengumumkan "minuman" lalu membaca makanan. Bagi pengguna
        yang tidak bisa melihat halamannya, itu bukan sekadar janggal -
        ia menghapus satu-satunya petunjuk tentang di mana ia berada.

    Isi bagian lain TIDAK dibuang, hanya digeser ke belakang. Pengguna
    yang terus menekan tombol tetap mendengar seluruh halaman.
    """
    bagian = list(bagian or [])
    if not bagian:
        return []
    if not (0 <= idx < len(bagian)):
        idx = 0
    urutan = [bagian[idx]] + [b for i, b in enumerate(bagian) if i != idx]
    baris = []
    for judul, isi in urutan:
        if judul:
            baris.append(judul)
        baris.extend(isi)
    return baris


def siapkan_ucapan(bagian, idx, kamus=None):
    """
    Satu pintu: urutkan dari sasaran, gabung kembar, buang sampah.
    -> (baris_siap, jumlah_dibuang)

    Urutannya penting dan tidak boleh ditukar:

        1. urut dulu  - supaya penomoran bagian masih utuh
        2. gabung     - supaya sampah kembar dihitung sekali
        3. saring     - supaya pengaman maks_buang melihat jumlah benar
    """
    baris = urut_dari_bagian(bagian, idx)
    baris = gabung_baris_kembar(baris, kamus)
    return saring_sampah(baris, kamus or set())


if __name__ == "__main__":
    ok = gagal = 0

    def cek(nama, dapat, harap):
        global ok, gagal
        if dapat == harap:
            ok += 1
            print(f"  OK   {nama}")
        else:
            gagal += 1
            print(f"  GAGAL {nama}\n        dapat : {dapat}\n        harap : {harap}")

    print("\n--- nominal_dari_label ---")
    cek("28-kelas", nominal_dari_label("rp50000_2022_belakang"), 50000)
    cek("7-kelas", nominal_dari_label("100000"), 100000)
    cek("bukan uang", nominal_dari_label("teks"), None)
    cek("nominal palsu", nominal_dari_label("rp3000_2022_depan"), None)

    print("\n--- iou / dedup ---")
    cek("iou identik", round(iou((0, 0, 10, 10), (0, 0, 10, 10)), 3), 1.0)
    cek("iou terpisah", iou((0, 0, 10, 10), (50, 50, 10, 10)), 0.0)
    tumpuk = [
        {"nominal": 10000, "conf": 0.91, "box": (10, 10, 100, 50)},
        {"nominal": 10000, "conf": 0.72, "box": (14, 12, 100, 50)},   # duplikat
        {"nominal": 2000,  "conf": 0.88, "box": (200, 10, 100, 50)},  # lembar lain
    ]
    cek("dedup buang duplikat", len(dedup_boxes(tumpuk)), 2)
    tumpuk_beda = [
        {"nominal": 50000, "conf": 0.91, "box": (10, 10, 100, 50)},
        {"nominal": 20000, "conf": 0.88, "box": (30, 10, 100, 50)},  # overlap ~66.7% tapi beda lembar
    ]
    cek("dedup simpan nominal beda berhimpitan", len(dedup_boxes(tumpuk_beda)), 2)

    print("\n--- vote_notes ---")
    stabil = [[{"nominal": 10000, "conf": .9, "box": (0, 0, 1, 1)},
               {"nominal": 2000, "conf": .85, "box": (5, 0, 1, 1)}] for _ in range(5)]
    v = vote_notes(stabil)
    cek("konsisten -> lolos", sorted(d["nominal"] for d in v), [2000, 10000])

    goyah = [
        [{"nominal": 10000, "conf": .9, "box": (0, 0, 1, 1)}],
        [{"nominal": 50000, "conf": .9, "box": (0, 0, 1, 1)}],
        [{"nominal": 20000, "conf": .9, "box": (0, 0, 1, 1)}],
        [{"nominal": 1000, "conf": .9, "box": (0, 0, 1, 1)}],
        [{"nominal": 2000, "conf": .9, "box": (0, 0, 1, 1)}],
    ]
    cek("goyah -> ditolak", vote_notes(goyah), None)

    print("\n--- baca_urutan ---")
    kacau = [
        {"teks": "goreng", "box": (120, 12, 60, 20)},
        {"teks": "nasi",   "box": (10, 10, 50, 20)},
        {"teks": "es",     "box": (10, 60, 30, 20)},
        {"teks": "teh",    "box": (50, 62, 40, 20)},
    ]
    cek("kiri-kanan atas-bawah",
        [b["teks"] for b in baca_urutan(kacau)],
        ["nasi", "goreng", "es", "teh"])

    print("\n--- suku_kata ---")
    for kata, harap in [("ayam", ["a", "yam"]), ("kosek", ["ko", "sek"]),
                        ("nasi", ["na", "si"]), ("bakso", ["bak", "so"]),
                        ("goreng", ["go", "reng"]), ("minum", ["mi", "num"]),
                        ("sambal", ["sam", "bal"]),
                        # diftong akhir kata
                        ("pantai", ["pan", "tai"]), ("kalau", ["ka", "lau"]),
                        ("amboi", ["am", "boi"]), ("pulau", ["pu", "lau"]),
                        # au/ai di TENGAH kata bukan diftong
                        ("laut", ["la", "ut"]), ("daun", ["da", "un"]),
                        ("air", ["a", "ir"]), ("kain", ["ka", "in"])]:
        cek(f"suku_kata({kata})", suku_kata(kata), harap)

    print("\n--- eja_angka ---")
    cek("12000", " ".join(eja_angka(12000)), "dua belas ribu")
    cek("17000", " ".join(eja_angka(17000)), "tujuh belas ribu")
    cek("125000", " ".join(eja_angka(125000)), "seratus dua puluh lima ribu")

    print("\n--- rencana_ucap ---")
    bank = {"nasi_goreng", "nasi", "goreng", "ko", "sek", "a", "y", "m",
            "k", "o", "s", "e"}
    cek("frasa utuh menang", rencana_ucap("nasi goreng", bank.__contains__),
        ["nasi_goreng"])
    cek("mundur ke suku kata", rencana_ucap("kosek", bank.__contains__),
        ["ko", "sek"])
    cek("eja penuh kalau semua huruf ada",
        rencana_ucap("ayam", {"a", "y", "m"}.__contains__), ["a", "y", "a", "m"])
    cek("eja sebagian ditolak (diam)",
        rencana_ucap("xyz", bank.__contains__), [])
    cek("eja sebagian -> penanda kalau tersedia",
        rencana_ucap("xyz", (bank | {"tidak_terbaca"}).__contains__),
        ["tidak_terbaca"])

    print("\n--- format_rupiah ---")
    cek("dua lembar", format_rupiah([10000, 2000]),
        ("Rp 10.000 + Rp 2.000 = Rp 12.000", 12000))
    cek("satu lembar", format_rupiah([50000]), ("Rp 50.000", 50000))

    # ---------------- OCR ----------------
    K = muat_kamus()
    print(f"\n--- koreksi OCR (kamus dasar {len(K)} kata) ---")
    cek("kata benar lolos", koreksi_baris("Menu Restoran", K), "menu restoran")
    cek("Restoron -> restoran", koreksi_baris("Restoron", K), "restoran")
    cek("Meiu -> menu", koreksi_baris("Meiu", K), "menu")
    cek("Rastoran -> restoran", koreksi_baris("Rastoran", K), "restoran")
    cek("sampah dibuang", koreksi_baris("Menu Restoran mmo MAT o", K),
        "menu restoran")
    cek("aksara Han dibuang", koreksi_baris("Restoran 中 究", K), "restoran")

    print("\n--- perbaikan harga ---")
    cek("15.OOO", perbaiki_angka("15.OOO"), "15.000")
    cek("l0000", perbaiki_angka("l0000"), "10000")
    cek("5OOO", perbaiki_angka("5OOO"), "5000")
    cek("kata biasa utuh", perbaiki_angka("Restoran"), "Restoran")
    cek("harga di kalimat", koreksi_baris("Nasi Goreng 15.OOO", K),
        "nasi goreng 15.000")

    print("\n--- vote_kata (kata tak dikenal tetap hidup) ---")
    cek("merek ACOME bertahan",
        vote_kata(["acome", "acome o", "acome", "acomel", "acome 8"], 2)[0],
        "acome")
    cek("sampah sekali lewat dibuang",
        vote_kata(["menu restoran", "menu restoran mat", "menu restoran",
                   "menu restoran", "menu restoran o"], 2)[0],
        "menu restoran")
    cek("urutan kata terjaga",
        vote_kata(["nasi goreng ayam"] * 3, 2)[0], "nasi goreng ayam")
    cek("sampah panjang 1 frame dibuang",
        vote_kata(["menu restoran meukamng ayacong", "menu restoran",
                   "menu restoran", "menu restoran"], 2)[0],
        "menu restoran")
    cek("riwayat kosong", vote_kata([], 2), ("", 0))

    print("\n--- gabung_mirip (ejaan sempal disatukan) ---")
    kamus_uji = muat_kamus({"restoran", "menu", "ayam", "ayah", "nasi"})

    # Kasus nyata dari MaixCam: satu kata, lima ejaan, semuanya gugur.
    keluarga = {"maixhub": 1, "moixhub": 1, "mafxhub": 1,
                "malxhub": 1, "maihub": 1}
    peta = gabung_mirip(keluarga, kamus_uji)
    cek("lima ejaan jadi satu kelompok", len(set(peta.values())), 1)
    cek("medoid terpilih, bukan abjad", peta["mafxhub"], "maixhub")

    cek("angka TIDAK pernah digabung",
        gabung_mirip({"1000": 3, "2000": 2}, kamus_uji)["2000"], "2000")
    cek("dua kata sah tetap terpisah",
        gabung_mirip({"ayam": 3, "ayah": 2}, kamus_uji)["ayah"], "ayah")
    cek("kata pendek tidak digabung",
        gabung_mirip({"sate": 2, "sale": 2}, kamus_uji)["sale"], "sale")
    cek("kamus menang atas medoid",
        gabung_mirip({"restoran": 2, "restaran": 1, "resturan": 1},
                     kamus_uji)["restaran"], "restoran")
    cek("selisih panjang >2 ditolak",
        gabung_mirip({"maixhub": 2, "hub": 2}, kamus_uji)["hub"], "hub")

    # Log MaixCam: '&MaixHub' terbaca 5x dengan 5 ejaan lalu HILANG.
    log_maixcam = [
        "ssipeed maixcam easy riscv edgeal camero support maixpy maixhub",
        "ssipeed maixcam easy risc edgeal camerd support maixpy moixhub",
        "ssipeed maixcam easy risc edgeal camero support mahpy mafxhub",
        "ssipeed maixcam easy risc edgeal camero support maixpy maihub",
        "ssipeed maixcam easy risc edgeal camera support maixpy malxhub",
    ]
    cek("kata 5-ejaan tidak lagi hilang",
        "maixhub" in vote_kata(log_maixcam, 2, kamus_uji)[0], True)
    cek("dukungan tidak melebihi jumlah frame",
        vote_kata(log_maixcam, 2, kamus_uji)[1] <= len(log_maixcam), True)

    print("\n--- harga_k (menu warung menulis '5K', OCR membaca 'SK') ---")
    cek("5K", harga_k("5K"), "5000")
    cek("SK -> 5000", harga_k("SK"), "5000")
    cek("15K", harga_k("15K"), "15000")
    cek("1SK -> 15000", harga_k("1SK"), "15000")
    cek("l5K -> 15000", harga_k("l5K"), "15000")
    cek("huruf K sendirian bukan harga", harga_k("K"), None)
    cek("terlalu panjang bukan harga", harga_k("1234K"), None)
    cek("nol ditolak", harga_k("0K"), None)
    cek("tanpa K bukan harga", harga_k("15"), None)
    for w in ("masak", "enak", "balik", "rusak", "gudeg"):
        cek("kata '%s' tidak diseret jadi harga" % w, harga_k(w), None)

    print("\n--- koreksi_kata: ambang ikut panjang kata ---")
    k5 = muat_kamus({"oncom", "restoran", "nasi", "goreng", "kentang"})
    # 'indom' -> 'oncom' berjarak 2. Pada kata 5 huruf itu 40% berubah:
    # penggantian, bukan koreksi. Ini merusak bacaan yang sudah BENAR.
    cek("kata pendek TIDAK diganti sejauh 2", koreksi_kata("indom", k5, False),
        "indom")
    cek("kata panjang boleh jarak 2", koreksi_kata("restoram", k5, False),
        "restoran")
    cek("salah satu huruf tetap dikoreksi", koreksi_kata("nasl", k5, False),
        "nasi")
    cek("sampah tetap lewat apa adanya", koreksi_kata("meukamng", k5, False),
        "meukamng")

    print("\n--- baca_baris + vote_baris (menu = baris, bukan kantong) ---")

    def _kk(t, x, y):
        return {"teks": t, "box": (x, y, 8 * len(t), 18)}

    kmenu = muat_kamus({"topping", "sosis", "nugget", "bakso", "telur"})
    satu = [_kk("Sosis", 20, 70), _kk("2K", 250, 70),
            _kk("Nugget", 20, 100), _kk("2K", 250, 100),
            _kk("Bakso", 20, 130), _kk("2k", 250, 130),
            _kk("Telor", 20, 160), _kk("3K", 250, 160)]
    cek("kotak dikelompokkan jadi 4 baris", len(baca_baris(satu)), 4)
    cek("baris pertama utuh", baca_baris(satu)[0], ["Sosis", "2K"])

    riw = []
    for _ in range(5):
        riw.append([koreksi_baris(" ".join(r), kmenu, hanya_kamus=False)
                    for r in baca_baris(satu)])
    hasil = vote_baris(riw, 2, kmenu)
    cek("empat item bertahan", len(hasil), 4)
    cek("harga kembar TIDAK hilang",
        [h for h in hasil if h.endswith("2000")],
        ["sosis 2000", "nugget 2000", "bakso 2000"])
    # 'telor' TIDAK lagi dipaksa jadi 'telur', dan itu memang benar:
    # sejak 'teler' (es teler) masuk kosakata, keduanya sama-sama
    # berjarak 1 dari 'telor'. Menebak salah satunya berarti berpeluang
    # menyebut minuman padahal yang tertulis telur. Jadi 'telor'
    # didaftarkan sebagai kata sah dan diucapkan apa adanya - ejaan
    # sehari-hari yang tetap dimengerti pendengar.
    cek("pasangan nama-harga benar", hasil[3], "telor 3000")

    # Bandingkan dengan cara lama: harga kembar lenyap.
    lama = vote_kata([" ".join(f) for f in riw], 2, kmenu)[0]
    cek("cara lama memang kehilangan harga",
        len([t for t in lama.split() if t == "2000"]), 1)

    cek("harga goyang -> yang terbanyak menang",
        vote_baris([["sosis 2000"], ["sosis 2000"], ["sosis 9000"],
                    ["sosis 2000"], ["sosis 2000"]], 2, kmenu),
        ["sosis 2000"])
    cek("harga tak pernah stabil -> sebut nama saja",
        vote_baris([["sosis 2000"], ["sosis 3000"], ["sosis 4000"],
                    ["sosis 5000"], ["sosis 6000"]], 2, kmenu),
        ["sosis"])
    cek("baris sekali lewat dibuang",
        vote_baris([["sosis 2000"], ["sosis 2000"], ["xyzq 9000"]], 2, kmenu),
        ["sosis 2000"])
    cek("riwayat kosong", vote_baris([], 2, kmenu), [])
    cek("pisah_harga", pisah_harga("sosis 2000"), ("sosis", ["2000"]))

    # KEBERSIHAN KAMUS. Sebuah blok komentar pernah berada di DALAM
    # tanda kutip KAMUS_LUAS; '#' tidak berarti apa-apa di dalam string,
    # jadi 43 kata prosa jadi kosakata - termasuk 'dingh', salah baca
    # OCR yang ditulis sebagai contoh dan malah jadi kata sah sehingga
    # koreksi 'dingh' -> 'dingin' berhenti bekerja.
    #
    # Ujinya ke sumbernya, bukan ke daftar kata terlarang: kamus tidak
    # boleh memuat '#' sama sekali. Itu menangkap SEMUA komentar yang
    # salah tempat, termasuk yang belum pernah ditulis.
    print("\n--- kebersihan sumber kamus ---")
    cek("KAMUS_INTI tanpa komentar nyasar", "#" in KAMUS_INTI, False)
    cek("KAMUS_LUAS tanpa komentar nyasar", "#" in KAMUS_LUAS, False)
    _kd = set(KAMUS_DASAR.split())
    cek("tidak ada kata berhuruf besar", [w for w in _kd if not w.islower()], [])
    cek("semua kata murni huruf", [w for w in _kd if not w.isalpha()], [])
    cek("'dingh' TIDAK terdaftar (contoh galat, bukan kata)",
        "dingh" in _kd, False)
    cek("'dingin' tetap terdaftar", "dingin" in _kd, True)
    cek("'beng' tetap terdaftar (merek)", "beng" in _kd, True)

    print("\n--- Kamus berindeks (hasil HARUS identik dengan telusur penuh) ---")
    import random as _rnd
    _kt = set(("nasi goreng ayam bakso mie soto sambal menu minuman makanan "
               "sayuran teh susu jus tahu tempe telur bebek udang cumi "
               "kangkung tauge pepaya gudangan lalapan pecel daun singkong "
               "terong paket spesial pedas dingin hangat jeruk coklat "
               "kampung paha dada restoran warung es air").split())
    _idx = muat_kamus({k + ".wav": k for k in _kt})
    _pol = set(_idx.inti)
    cek("muat_kamus mengembalikan Kamus", isinstance(_idx, Kamus), True)
    cek("Kamus tetap berperilaku set", "nasi" in _idx and len(_idx) >= len(_pol),
        True)
    # Kata bank audio non-menu: boleh dikenali, tidak boleh jadi bahan
    # tebakan.
    #
    # Dulu uji ini memakai 'gudangan'. Kata itu berhenti cocok begitu
    # menu Borcelle terbaca - 'Gudangan' memang hidangan sayur, jadi ia
    # naik jadi kosakata menu, dan ujinya gagal karena BENAR. Sekarang
    # dipakai kata yang tidak mungkin muncul di menu.
    _bank_saja = muat_kamus({"berjalan.wav": "berjalan",
                             "sekolah.wav": "sekolah"})
    cek("kata bank audio dikenali tapi TIDAK ditebak",
        [(w in _bank_saja, w in _bank_saja.inti)
         for w in ("berjalan", "sekolah")],
        [(True, False), (True, False)])
    cek("kosakata menu SELALU boleh ditebak",
        all(w in _idx.inti for w in ("nasi", "goreng", "nusantara", "kaldu")),
        True)

    # Kata pendek WAJIB ikut terindeks. Versi pertama hanya mengindeks
    # kata >=4 huruf, dan 'tegh' gagal jadi 'teh', 'jaus' salah jadi
    # 'saus' karena pesaingnya 'jus' tak terlihat.
    cek("kata pendek ikut terindeks",
        all(k in _idx.depan.get(k[:2], []) for k in ("teh", "jus", "mie", "es")),
        True)

    _rnd.seed(3)
    _huruf = "abcdefghijklmnopqrstuvwxyz"
    _uji = list(_kt) + ["tegh", "jaus", "wmie", "tehh", "gorng", "nasl",
                        "restoram", "meukamng", "axmp", "tenhainaap"]
    for _w in list(_kt):                   # rusak 1 huruf: ganti/hapus/sisip
        _i = _rnd.randrange(len(_w))
        _uji += [_w[:_i] + _rnd.choice(_huruf) + _w[_i + 1:],
                 _w[:_i] + _w[_i + 1:],
                 _w[:_i] + _rnd.choice(_huruf) + _w[_i:]]
    for _ in range(500):                   # sampah acak
        _uji.append("".join(_rnd.choice(_huruf)
                            for _ in range(_rnd.randint(2, 12))))
    # Yang harus dibuktikan: indeks depan/belakang TIDAK PERNAH
    # menjatuhkan kandidat yang seharusnya ketemu.
    #
    # Versi lama menguji ini dengan membandingkan koreksi_kata memakai
    # Kamus vs set polos. Itu berhenti sahih sejak kamus dua lapis:
    # perbedaannya lalu didominasi jalur PENGENALAN ('gudangan' ada di
    # set besar, tidak ada di inti), bukan jalur pencarian kandidat yang
    # sebenarnya mau diuji. Sekarang invariannya diperiksa langsung.
    _bocor = []
    for _w in _uji:
        _b = "".join(c for c in _w.lower() if c.isalnum())
        if len(_b) < 4:
            continue
        _harus = {k for k in _idx.inti
                  if abs(len(k) - len(_b)) <= 1 and jarak_edit(_b, k, 1) <= 1}
        if not _harus <= set(_idx.kandidat(_b, 1)):
            _bocor.append(_w)
    cek("%d kata: indeks tidak menjatuhkan kandidat" % len(_uji), _bocor[:3], [])

    print("\n--- mutu_bacaan (bidikan buruk jangan dibacakan) ---")
    _km = muat_kamus({k + ".wav": k for k in
                      ("nasi goreng ayam bakso mie soto sambal menu minuman "
                       "makanan sayuran teh susu jus tahu tempe telur bebek "
                       "udang cumi kangkung tauge pepaya gudangan lalapan "
                       "pecel daun singkong terong paket spesial pedas "
                       "dingin hangat jeruk coklat kampung paha dada topping "
                       "sosis nugget restoran warung es air lada tunai "
                       "sale jamu bunga sayur").split()})

    # Lima bidikan NYATA dari perangkat, disalin apa adanya dari log.
    _baik1 = ["topping", "sosis 2000", "nugget 2000", "bakso 2000",
              "telur 3000", "oburuainorderl"]
    _baik2 = ["makanan borcelle sayuran", "ayan kampung paha 25.000",
              "cah kangkag jamu 15.000", "ayam hampung daa 25.000",
              "tumis tauge bunga pepaya 15.000", "ayan paha 25.000",
              "gudangan 15.000", "lalapan 15.000", "sayur pecel 15.000",
              "daun singkong pedas 15.000"]
    _sedang = ["makanan borcelle sayuran", "ayam isapg paha 25.000",
               "wo chgagaaar 15000", "aam hapg d 25.000",
               "tuaatagbogapaia 15000", "ayapa 25.000"]
    _buruk = ["menu", "marmnoo", "dene", "makaan", "iga o", "mdona gg",
              "oke wh oa yen 7L", "memig s on", "moneotr", "imdama h",
              "pare ye 3", "puan lada", "tah ap 1N", "trek R94", "os",
              "nusan tunai a", "sraa 00", "shaangn 80", "toppp", "es te 0",
              "ak", "norea k", "nuaenhoauee da sale 3", "y oy a",
              "oeaedyo e agmuasoa"]
    _sedikit = ["sesalpedas", "hnuhan", "sambal"]

    cek("bidikan bagus -> baik", mutu_bacaan(_baik1, _km)[0], "baik")
    cek("menu terbaca jelas -> baik", mutu_bacaan(_baik2, _km)[0], "baik")
    # DULU diharapkan "sebagian" dan DIBACAKAN. Itu keliru, dan baru
    # ketahuan setelah bidikan sungguhan diperiksa isinya, bukan cuma
    # skornya: pada 30% yang terucap adalah 'tuaatagbogapaia 15000' dan
    # 'wo chgagaaar 15000'. Menyebut harga di sebelah nama yang tidak ada
    # artinya lebih berbahaya daripada diam - pengguna mengira itu memang
    # nama menunya.
    cek("bidikan sedang ternyata SAMPAH -> tolak",
        mutu_bacaan(_sedang, _km)[0], "buruk")

    # Bidikan yang benar-benar 'sebagian': sebagian besar baris masuk
    # akal, sebagian kecil rusak. Inilah yang layak dibacakan dengan
    # peringatan.
    _sbg = ["topping", "sosis 2000", "nugget 2000", "bakso 2000",
            "telur 3000", "menu spesial", "ayapa 25.000",
            "chgagaaar 15000", "tuaatag bogapaia", "hapgd narsroe"]
    cek("sebagian benar -> sebagian", mutu_bacaan(_sbg, _km)[0], "sebagian")
    cek("skor sebagian ada di antara ambang",
        AMBANG_SEBAGIAN <= mutu_bacaan(_sbg, _km)[1] < AMBANG_BAIK, True)
    cek("menu kecil di frame -> buruk", mutu_bacaan(_buruk, _km)[0], "buruk")
    cek("banyak kotak + buruk -> suruh dekatkan",
        mutu_bacaan(_buruk, _km, n_kotak=42)[2], "terlalu jauh, dekatkan")
    cek("kotak sedikit -> jangan suruh dekatkan",
        mutu_bacaan(_buruk, _km, n_kotak=5)[2], "teks tidak terbaca")

    # 20-29 kotak adalah zona PALING BERHASIL (70% lulus dari 35 bidikan
    # nyata). Menyuruh "dekatkan" di situ mendorong pengguna menjauh dari
    # titik terbaiknya - dan ia tidak punya cara memeriksa nasihat itu.
    cek("zona terbaik 20-29 kotak: JANGAN suruh dekatkan",
        mutu_bacaan(_buruk, _km, n_kotak=24)[2], "teks tidak terbaca")
    cek("30+ kotak baru disebut terlalu jauh",
        mutu_bacaan(_buruk, _km, n_kotak=30)[2], "terlalu jauh, dekatkan")

    # Persentase SENDIRIAN menipu: 1 dari 3 kata dikenal = 33%, terlihat
    # sama seperti bidikan sedang. Jumlah kata minimum yang menahannya.
    cek("terlalu sedikit kata -> jangan dinilai",
        mutu_bacaan(_sedikit, _km)[0], "sedikit")
    cek("daftar kosong", mutu_bacaan([], _km)[0], "sedikit")
    cek("skor buruk memang rendah", mutu_bacaan(_buruk, _km)[1] < 20, True)
    cek("skor baik memang tinggi", mutu_bacaan(_baik2, _km)[1] >= 40, True)

    print("\n--- tinggi_teks (baris miring jangan dikira judul) ---")
    import math as _m
    # Baris 300x16 px, dimiringkan 5 derajat mengelilingi titik tengahnya.
    def _miring(W, Hh, drjt):
        r = _m.radians(drjt)
        c, s = _m.cos(r), _m.sin(r)
        sudut = [(-W / 2, -Hh / 2), (W / 2, -Hh / 2),
                 (W / 2, Hh / 2), (-W / 2, Hh / 2)]
        xs = [x * c - y * s + 500 for x, y in sudut]
        ys = [x * s + y * c + 300 for x, y in sudut]
        return xs, ys

    _xs, _ys = _miring(300, 16, 5)
    _aabb = max(_ys) - min(_ys)
    cek("tinggi benar meski miring", round(tinggi_teks(_xs, _ys)), 16)
    cek("kotak tegak MELAR jauh (inilah bugnya)", _aabb > 35, True)

    # Baris PENDEK dengan kemiringan SAMA melar jauh lebih sedikit -
    # itu sebabnya baris terpanjang yang salah dikira judul.
    _xs2, _ys2 = _miring(80, 16, 5)
    cek("baris pendek melar lebih sedikit",
        (max(_ys2) - min(_ys2)) < _aabb, True)
    cek("tinggi benar tetap sama untuk baris pendek",
        round(tinggi_teks(_xs2, _ys2)), 16)

    _xs3, _ys3 = _miring(300, 16, 0)
    cek("tanpa kemiringan tetap benar", round(tinggi_teks(_xs3, _ys3)), 16)
    cek("judul yang memang besar tetap besar",
        round(tinggi_teks(*_miring(120, 36, 5))), 36)
    cek("titik kurang dari 4 -> pakai kotak tegak",
        tinggi_teks([0, 10], [5, 25]), 20.0)

    # Kasus nyata: baris item panjang vs judul pendek, sama-sama miring.
    _item = tinggi_teks(*_miring(300, 16, 5))
    _judul = tinggi_teks(*_miring(120, 30, 5))
    cek("judul > item pakai tinggi sebenarnya", _judul > _item * 1.25, True)
    _item_aabb = max(_miring(300, 16, 5)[1]) - min(_miring(300, 16, 5)[1])
    _judul_aabb = max(_miring(120, 30, 5)[1]) - min(_miring(120, 30, 5)[1])
    cek("pakai kotak tegak, ITEM malah terlihat lebih besar dari JUDUL",
        _item_aabb > _judul_aabb, True)

    print("\n--- ketajaman (buram terukur tanpa NPU) ---")
    # Baris huruf: hitam-putih bergantian, tepi tegas.
    _tajam = ([30, 30, 220, 220] * 40)
    # Frame yang sama setelah diburamkan (rata-rata 3 tetangga).
    _buram = list(_tajam)
    for _ in range(4):
        _buram = [_buram[0]] + [
            (_buram[i - 1] + _buram[i] + _buram[i + 1]) / 3.0
            for i in range(1, len(_buram) - 1)] + [_buram[-1]]
    cek("tajam > buram", ketajaman(_tajam) > ketajaman(_buram), True)
    cek("bidang rata -> nol", ketajaman([120] * 50), 0.0)
    cek("sampel terlalu sedikit", ketajaman([10, 20]), 0.0)
    cek("gelap gulita tidak dibagi nol", ketajaman([0] * 50), 0.0)

    # Kecerahan TIDAK boleh menyamar jadi ketajaman. Frame yang sama
    # persis, cuma 2x lebih terang, harus dinilai sama.
    cek("kecerahan tidak menipu",
        round(ketajaman([v * 2 for v in _tajam]), 6),
        round(ketajaman(_tajam), 6))

    _i, _s = pilih_tertajam([_buram, _tajam, _buram])
    cek("frame tertajam terpilih", _i, 1)
    cek("skor tiap frame ikut dilaporkan", len(_s), 3)
    cek("daftar kosong", pilih_tertajam([]), (0, []))

    print("\n--- dedup_teks (petak bertindih membaca teks dua kali) ---")
    kembar = [{"teks": "borcelle", "box": (10, 10, 80, 20)},
              {"teks": "borc", "box": (10, 10, 40, 20)},
              {"teks": "ecellel", "box": (40, 10, 50, 20)},
              {"teks": "sayuran", "box": (200, 10, 70, 20)}]
    d = dedup_teks(kembar)
    cek("bacaan terpanjang menang",
        sorted(x["teks"] for x in d), ["borcelle", "sayuran"])
    cek("kotak berjauhan tidak dibuang",
        len(dedup_teks([{"teks": "a", "box": (0, 0, 10, 10)},
                        {"teks": "b", "box": (500, 0, 10, 10)}])), 2)
    cek("daftar kosong", dedup_teks([]), [])

    print("\n--- pecah_kolom (menu tiga kolom sejajar) ---")

    def _b(t, x):
        return {"teks": t, "box": (x, 0, 8 * len(t), 18)}

    tiga = [_b("cah", 10), _b("Rp.15.000", 60), _b("tumis", 200),
            _b("Rp.20.000", 260), _b("es", 400), _b("Rp.5.000", 440)]
    seg = pecah_kolom(tiga)
    cek("tiga kolom terpisah", len(seg), 3)
    cek("kolom 1 utuh", [b["teks"] for b in seg[0]], ["cah", "Rp.15.000"])
    cek("kolom 3 utuh", [b["teks"] for b in seg[2]], ["es", "Rp.5.000"])
    cek("satu item tetap satu",
        len(pecah_kolom([_b("sosis", 10), _b("2K", 200)])), 1)
    cek("nama dua kata tidak dipecah",
        len(pecah_kolom([_b("nasi", 10), _b("goreng", 50), _b("2K", 200)])), 1)
    cek("tanpa harga tetap satu",
        len(pecah_kolom([_b("topping", 10)])), 1)

    # Menu tiga kolom yang tadinya melebur - sekarang lewat baca_baris.
    kt = [{"teks": t, "box": (x, y, 8 * len(t), 18)} for t, x, y in
          (("cah", 10, 50), ("Rp.15.000", 60, 50),
           ("tumis", 200, 50), ("Rp.20.000", 260, 50),
           ("es", 400, 50), ("Rp.5.000", 440, 50))]
    cek("baca_baris memecah kolom", len(baca_baris(kt)), 3)
    cek("pasangan kolom tengah benar",
        baca_baris(kt)[1], ["tumis", "Rp.20.000"])

    print("\n--- kenali_judul (judul = teks besar tanpa harga) ---")
    kj = muat_kamus({k + ".wav": k for k in
                     ("makanan minuman sayuran sambal topping menu paket "
                      "nasi goreng ayam bakso telur sosis nugget").split()})

    def _kk2(t, x, y, h):
        return {"teks": t, "box": (x, y, 8 * len(t), h)}

    # Judul dicetak 2x lebih besar - persis kenapa cuma judul yang
    # terbaca benar pada menu padat di perangkat ini.
    menu = [_kk2("MAKANAN", 10, 10, 36),
            _kk2("nasi goreng", 10, 60, 18), _kk2("15000", 200, 60, 18),
            _kk2("ayam bakar", 10, 90, 18), _kk2("20000", 200, 90, 18),
            _kk2("MINUMAN", 10, 130, 36),
            _kk2("es teh", 10, 180, 18), _kk2("5000", 200, 180, 18),
            _kk2("kopi", 10, 210, 18), _kk2("8000", 200, 210, 18)]
    info = kenali_judul(baca_baris_info(menu), kj)
    judul = [b["teks"] for b in info if b["judul"]]
    cek("dua judul dikenali", judul, ["MAKANAN", "MINUMAN"])
    cek("baris berharga BUKAN judul",
        [b["teks"] for b in info if b["judul"] and b["harga"]], [])
    cek("tinggi baris ikut terbawa", info[0]["tinggi"], 36)

    bagian = kelompokkan_bagian(info)
    cek("dua bagian terbentuk", [b[0] for b in bagian],
        ["MAKANAN", "MINUMAN"])
    cek("isi masuk ke judul yang benar", bagian[0][1],
        ["nasi goreng 15000", "ayam bakar 20000"])
    cek("bagian kedua utuh", bagian[1][1], ["es teh 5000", "kopi 8000"])

    # Menu berhuruf SERAGAM - tidak ada yang menonjol ukurannya.
    # Sinyal kedua (tanpa harga + kata dikenal) yang menyelamatkan.
    seragam = [_kk2("MINUMAN", 10, 10, 18),
               _kk2("es teh", 10, 40, 18), _kk2("5000", 200, 40, 18),
               _kk2("kopi", 10, 70, 18), _kk2("8000", 200, 70, 18)]
    i2 = kenali_judul(baca_baris_info(seragam), kj)
    cek("judul tanpa beda ukuran tetap ketemu",
        [b["teks"] for b in i2 if b["judul"]], ["MINUMAN"])

    # BATAS YANG DIKETAHUI, ditulis sebagai uji supaya tidak terlupa.
    #
    # Sampah OCR yang kebetulan tercetak besar dan tanpa harga AKAN
    # dikira judul. Tinggi huruf tidak tahu apa-apa soal arti.
    #
    # Ini dibiarkan, bukan diperbaiki, karena obatnya lebih buruk:
    # menuntut judul harus ada di kamus akan membuang judul sah yang
    # tidak umum ("MENU PAKET SPESIAL PEDAS", nama warung). Kalau nanti
    # terbukti mengganggu di lapangan, mutu_bacaan sudah lebih dulu
    # menolak bidikan yang isinya memang sampah.
    sampah = [_kk2("marmnoo", 10, 10, 40),
              _kk2("nasi goreng", 10, 60, 18), _kk2("15000", 200, 60, 18),
              _kk2("ayam bakar", 10, 90, 18), _kk2("20000", 200, 90, 18),
              _kk2("es teh", 10, 120, 18), _kk2("5000", 200, 120, 18)]
    i3 = kenali_judul(baca_baris_info(sampah), kj)
    cek("sampah besar MASIH dikira judul - batas yang disadari",
        [b["teks"] for b in i3 if b["judul"]], ["marmnoo"])

    # Kepala menu 'MENU / WARMINDO / NUSANTARA' - tiga judul TANPA isi.
    # Tanpa pemisahan ini, pengguna tunanetra menekan tombol tiga kali
    # melewati bagian kosong sebelum sampai ke 'Makanan'.
    # Ketiga baris kepala dicetak SAMA BESAR, seperti menu sungguhan.
    # Versi pertama uji ini memberi NUSANTARA tinggi 30 sementara dua
    # lainnya 34 - tanpa alasan - dan ia jatuh di bawah ambang lalu
    # menjadi ISI dari WARMINDO. Data uji yang tidak realistis
    # menghasilkan kegagalan yang tidak realistis juga.
    kepala = [_kk2("MENU", 60, 5, 34), _kk2("WARMINDO", 40, 40, 34),
              _kk2("NUSANTARA", 30, 75, 34),
              _kk2("Makanan", 20, 120, 26),
              _kk2("nasi goreng", 20, 160, 16), _kk2("15000", 220, 160, 16),
              _kk2("Minuman", 20, 210, 26),
              _kk2("es teh", 20, 250, 16), _kk2("5000", 220, 250, 16)]
    bg = kelompokkan_bagian(kenali_judul(baca_baris_info(kepala), kj))
    nama, kategori = pisah_nama_tempat(bg)
    cek("nama tempat dipisah", nama, ["MENU", "WARMINDO", "NUSANTARA"])
    cek("kategori sungguhan tersisa", [k[0] for k in kategori],
        ["Makanan", "Minuman"])
    cek("isi kategori utuh", kategori[0][1], ["nasi goreng 15000"])
    cek("nama tempat TIDAK dibuang, cuma dipisah",
        len(nama) + len(kategori), len(bg))
    cek("tanpa nama tempat -> semua kategori",
        pisah_nama_tempat([("Makanan", ["a"]), ("Minuman", ["b"])])[0], [])
    cek("bagian kosong", pisah_nama_tempat([]), ([], []))

    # bagian_terkaya: 4 dari 5 bidikan nyata mulai di bagian bersampah.
    print("\n--- bagian_terkaya (jangan mulai dari sampah) ---")
    _b1 = [("warmindo", ["niasanaa"]),
           ("makanan", ["a", "b", "c", "d", "e", "f", "g"]),
           ("piibanlexe", ["x"])]
    cek("lewati bagian 1-baris", bagian_terkaya(_b1), 1)
    cek("bagian terpilih memang yang berisi", _b1[bagian_terkaya(_b1)][0],
        "makanan")
    _b2 = [("", ["mena"]), ("warmindo", ["z"]),
           ("makanan", ["a", "b", "c", "d", "e", "f", "g"]),
           ("minuman pilihan level", ["q"])]
    cek("lewati 'tanpa judul' bersampah", bagian_terkaya(_b2), 2)
    cek("satu bagian saja -> tetap 0",
        bagian_terkaya([("makanan", ["a", "b"])]), 0)
    cek("seri -> yang lebih awal menang",
        bagian_terkaya([("a", ["1", "2"]), ("b", ["3", "4"])]), 0)
    cek("daftar kosong -> 0", bagian_terkaya([]), 0)
    cek("semua kosong -> 0",
        bagian_terkaya([("a", []), ("b", [])]), 0)

    # bagian_dituju: yang MENGISI BINGKAI, bukan yang paling banyak isinya.
    #
    # Adegannya: kamera diarahkan ke MINUMAN. Ekor MAKANAN tersisa di
    # tepi atas - lebih banyak barisnya (5 lawan 4), tapi hurufnya kecil
    # dan letaknya di pinggir. Persis kasus yang ditanyakan: 'kalau
    # dominan diarahkan ke minuman, abaikan makanan'.
    print("\n--- bagian_dituju (yang mengisi bingkai) ---")

    def _kt(t, x, y, w, h):
        return {"teks": t, "box": (x, y, w, h)}

    def _adegan(judul_makanan=True):
        b = [_kt("MAKANAN", 40, 0, 100, 30)] if judul_makanan else []
        for i, y in enumerate((20, 44, 68, 92, 116)):
            b.append(_kt("mk%d 5000" % i, 40, y, 100, 12))
        b.append(_kt("MINUMAN", 40, 200, 100, 30))
        for i, y in enumerate((260, 290, 320, 350)):
            b.append(_kt("mn%d 6000" % i, 40, y, 100, 16))
        return b

    _i1 = kenali_judul(baca_baris_info(_adegan(True)))
    _g1 = kelompokkan_bagian(_i1)
    cek("adegan terbentuk benar", [(j, len(s)) for j, s in _g1],
        [("MAKANAN", 5), ("MINUMAN", 4)])
    cek("terkaya salah sasaran (5 baris menang)", bagian_terkaya(_g1), 0)
    cek("dituju memilih yang di tengah", bagian_dituju(_g1, _i1, 640), 1)
    cek("nilai tengah > nilai tepi",
        nilai_bagian(_g1, _i1, 640)[1] > nilai_bagian(_g1, _i1, 640)[0], True)

    # Kasus yatim: judul MAKANAN sudah di luar bingkai, sisanya jadi
    # bagian tanpa judul. Justru itu tanda ia bukan yang dituju.
    _i2 = kenali_judul(baca_baris_info(_adegan(False)))
    _g2 = kelompokkan_bagian(_i2)
    cek("yatim: terkaya tetap salah sasaran", bagian_terkaya(_g2), 0)
    cek("yatim: dituju melewatinya", bagian_dituju(_g2, _i2, 640), 1)
    cek("hukuman tanpa-judul terasa",
        nilai_bagian(_g2, _i2, 640)[0] < nilai_bagian(_g1, _i1, 640)[0], True)

    # Tanpa tinggi bingkai, pita disimpulkan dari sebaran teks. Marginnya
    # menipis (lihat catatan letterbox di nilai_bagian) tapi arahnya tetap.
    cek("tinggi bingkai disimpulkan sendiri", bagian_dituju(_g1, _i1), 1)

    # MUNDUR KE PERILAKU LAMA kalau geometri tidak ada. Ini yang membuat
    # perubahan ini aman: kalau sinyal barunya bisu, yang lama jalan.
    cek("info kosong -> sama dengan terkaya",
        bagian_dituju(_g1, []), bagian_terkaya(_g1))
    cek("teks tidak cocok -> sama dengan terkaya",
        bagian_dituju([("a", ["x"]), ("b", ["y", "z"])], _i1),
        bagian_terkaya([("a", ["x"]), ("b", ["y", "z"])]))
    cek("bagian kosong -> 0", bagian_dituju([], _i1), 0)
    cek("nilai_bagian tanpa info -> nol semua",
        nilai_bagian([("a", ["x"]), ("b", ["y"])], []), [0.0, 0.0])

    # Satu baris tidak boleh dihitung untuk dua bagian sekaligus.
    _dua = [("A", ["sama"]), ("B", ["sama"])]
    _isi = kenali_judul(baca_baris_info([_kt("sama", 0, 0, 40, 10)]))
    cek("teks kembar dipakai sekali saja",
        sum(1 for n in nilai_bagian(_dua, _isi) if n > 0), 1)

    # KEGAGALAN NYATA DARI PERANGKAT, dikunci sebagai uji.
    #
    # Log mencetak sebabnya sendiri:
    #     ? indomie rendang   tinggi 35 (median 32)  sebab=bersih
    #     ? indomie aceh      tinggi 31 (median 32)  sebab=bersih
    #     ? nusantara         tinggi 32 (median 32)  sebab=bersih
    #
    # Ketiganya ITEM atau nama tempat, bukan kategori. Semua katanya ada
    # di kamus umum, dan harganya gagal terbaca - jadi aturan lama
    # meloloskannya. Angka tinggi di bawah disalin apa adanya dari log.
    _nyata = [("nusantara", 32), ("makanan", 36),
              ("mie bangladesh 12000", 32), ("indomie goreng 7000", 32),
              ("indomie kuah kaldu ayam 7000", 32),
              ("indomie soto medan 7000", 32), ("indomie rendang", 35),
              ("indomie aceh", 31), ("indomie hype habis 8000", 32),
              ("pilihan level", 45), ("n dingin 4000", 32)]
    _kn = muat_kamus({w + ".wav": w for w in
                      ("indomie rendang aceh nusantara makanan minuman "
                       "goreng soto medan kuah kaldu ayam pilihan level "
                       "bangladesh mie dingin hype").split()})
    _inf = [{"teks": t, "tinggi": h, "y": i * 30,
             "harga": any(c.isdigit() for c in t)}
            for i, (t, h) in enumerate(_nyata)]
    kenali_judul(_inf, _kn)
    cek("menu Warmindo: judul yang benar saja",
        [b["teks"] for b in _inf if b["judul"]], ["makanan", "pilihan level"])
    cek("item ber-nama-sah TIDAK jadi judul",
        [b["teks"] for b in _inf
         if b["judul"] and b["teks"].startswith("indomie")], [])

    # Kamus umum yang makin besar TIDAK boleh menambah judul palsu.
    _inf2 = [dict(b) for b in _inf]
    kenali_judul(_inf2, muat_kamus())        # kamus penuh 855 kata
    # Bagian Topping dari perangkat: 'Bakso' adalah ITEM yang harganya
    # gagal terbaca. Kalau ia naik jadi judul, bagian Topping terbelah
    # dan pengguna cuma mendengar separuh isinya.
    _top = [("level", 40), ("topping", 97), ("sosis 1", 51),
            ("nugget 12", 51), ("bakso", 51), ("telor 4", 51),
            ("ayoduruam", 51)]
    _it = [{"teks": t, "tinggi": h, "y": i * 30,
            "harga": any(c.isdigit() for c in t)}
           for i, (t, h) in enumerate(_top)]
    kenali_judul(_it, muat_kamus())
    cek("nama hidangan TIDAK memecah bagian",
        [b["teks"] for b in _it if b["judul"]], ["level", "topping"])
    _bg = kelompokkan_bagian(_it)
    # 5 isi: sosis, nugget, bakso, telor, ayoduruam. Dulu terbelah jadi
    # 2 + 2, dan dua item terakhir hilang dari jangkauan pengguna.
    cek("Topping utuh, tidak terbelah oleh 'bakso'",
        len(dict(_bg).get("topping", [])), 5)

    cek("kamus besar tidak menambah judul palsu",
        [b["teks"] for b in _inf2 if b["judul"]],
        [b["teks"] for b in _inf if b["judul"]])

    cek("daftar kosong", kenali_judul([], kj), [])
    cek("tanpa judul -> satu bagian tanpa nama",
        kelompokkan_bagian(kenali_judul(baca_baris_info(
            [_kk2("nasi goreng", 10, 10, 18), _kk2("15000", 200, 10, 18)]),
            kj))[0][0], "")

    print("\n--- vote_teks ---")
    cek("bacaan sebagian mendukung yang lengkap",
        vote_teks(["menu restoran"] * 5 + ["menu"] * 4 + ["restoran"] * 2)[0],
        "menu restoran")
    cek("sampah 1x tidak menang",
        vote_teks(["menu restoran"] * 6 + ["menu"] * 4
                  + ["menu restoran xx yy zz"] * 1)[0],
        "menu restoran")
    cek("bacaan panjang stabil boleh menang",
        vote_teks(["menu restoran"] * 3 + ["menu restoran nasi goreng"] * 5)[0],
        "menu restoran nasi goreng")
    cek("riwayat kosong", vote_teks([]), ("", 0))

    # ---------------- baterai ----------------
    print("\n--- PantauBaterai ---")

    def jalankan(seri, dt=1.0):
        """seri tegangan -> daftar (detik, peringatan)"""
        p = PantauBaterai()
        keluar, t = [], 0.0
        for v in seri:
            e = p.perbarui(v, t)
            if e:
                keluar.append((t, e))
            t += dt
        return keluar, p

    # pengosongan pelan 4,2 -> 3,3
    turun = [4.2 - i * 0.002 for i in range(500)]
    ev, p = jalankan(turun)
    nama = [e for _, e in ev]
    cek("pengosongan: lemah sebelum kritis",
        nama.index("lemah") < nama.index("kritis"), True)
    cek("berakhir di level kritis", p.level, "kritis")

    # derau di ambang - TIDAK boleh berkedip
    import random as _r
    _r.seed(1)
    derau = [3.56 + _r.uniform(-0.008, 0.008) for _ in range(400)]
    ev2, _ = jalankan(derau)
    cek("derau di ambang tidak spam", len(ev2) <= 2, True)

    # kedip beban: NPU menyebabkan anjlok 0,25 V selama 1 detik
    beban = []
    for i in range(200):
        beban.append(3.75 - (0.25 if i % 40 == 0 else 0.0))
    ev3, p3 = jalankan(beban)
    cek("kedip beban diabaikan", ev3, [])
    cek("level tetap normal", p3.level, "normal")

    # kritis diulang, tapi jedanya memanjang - tidak boleh menyiksa
    ev4, _ = jalankan([3.35] * 3600)            # 1 jam
    n_kritis = len([e for _, e in ev4 if e == "kritis"])
    cek("kritis diulang beberapa kali", n_kritis >= 4, True)
    cek("kritis TIDAK menyiksa dalam 1 jam", n_kritis <= 10, True)
    jarak = [ev4[i + 1][0] - ev4[i][0] for i in range(len(ev4) - 1)]
    cek("jeda kritis memanjang", jarak == sorted(jarak), True)

    ev5, _ = jalankan([3.50] * 3600)
    n_lemah = len([e for _, e in ev5 if e == "lemah"])
    cek("lemah lebih jarang dari kritis", n_lemah < n_kritis, True)
    cek("lemah wajar dalam 1 jam", n_lemah <= 6, True)

    # diisi ulang lalu habis lagi -> harus memperingatkan lagi
    siklus = [3.50] * 60 + [4.10] * 60 + [3.50] * 60
    ev6, _ = jalankan(siklus)
    cek("peringatkan lagi setelah diisi",
        len([e for _, e in ev6 if e == "lemah"]) >= 2, True)

    # penuh cuma sekali
    ev7, _ = jalankan([4.18] * 300)
    cek("penuh diucapkan sekali", [e for _, e in ev7], ["penuh"])

    # ---------------- pola tombol ----------------
    print("\n--- PolaTombol ---")

    def tekan(pola, dt=0.02):
        """pola: [(ditekan, durasi_detik), ...] -> daftar kejadian"""
        p = PolaTombol()
        keluar, t = [], 0.0
        for ditekan, lama in pola:
            n = max(1, int(lama / dt))
            for _ in range(n):
                e = p.perbarui(ditekan, t)
                if e:
                    keluar.append(e)
                t += dt
        for _ in range(60):                  # biarkan jendela ganda berakhir
            e = p.perbarui(False, t)
            if e:
                keluar.append(e)
            t += dt
        return keluar

    cek("tekan sekali", tekan([(1, 0.15), (0, 1.0)]), ["tunggal"])
    cek("tekan dua kali cepat",
        tekan([(1, 0.12), (0, 0.15), (1, 0.12), (0, 1.0)]), ["ganda"])
    cek("tahan lama", tekan([(1, 1.5), (0, 1.0)]), ["tahan"])
    cek("dua tekan berjarak jauh = dua tunggal",
        tekan([(1, 0.12), (0, 1.2), (1, 0.12), (0, 1.2)]),
        ["tunggal", "tunggal"])
    cek("pantulan kontak diabaikan", tekan([(1, 0.02), (0, 1.0)]), [])
    cek("tanpa tekanan", tekan([(0, 2.0)]), [])
    cek("tahan lalu tekan sekali",
        tekan([(1, 1.5), (0, 0.9), (1, 0.12), (0, 1.0)]), ["tahan", "tunggal"])


    # ==================================================================
    # PENYARINGAN BARIS - seluruh bahan dari log perangkat 26 Agu
    # membaca Sampel Menu/20260813_140428.jpg
    # ==================================================================
    print("\n-- harga sah --")
    cek("Rp17.000 sah", harga_sah("Rp17.000"), True)
    cek("Rp12000 sah (tanpa titik)", harga_sah("Rp12000"), True)
    cek("15.000 sah (tanpa Rp)", harga_sah("15.000"), True)
    cek("Rpin.000 TIDAK sah", harga_sah("Rpin.000"), False)
    cek("R018.000 TIDAK sah", harga_sah("R018.000"), False)
    cek("Res000 TIDAK sah", harga_sah("Res000"), False)
    cek("R0.000 TIDAK sah", harga_sah("R0.000"), False)
    cek("+1234567890 TIDAK sah (di luar rentang)",
        harga_sah("+1234567890"), False)
    cek("Rp50 TIDAK sah (terlalu kecil)", harga_sah("Rp50"), False)
    cek("kunci harga samakan R018.000 dan Rp18.000",
        kunci_harga("R018.000") == kunci_harga("Rp18.000"), True)

    km = {"menu", "makan", "makanan", "minuman", "warung", "nasi", "goreng",
          "mie", "ayam", "special", "es", "teh", "jeruk", "campur", "tahu",
          "soto", "madura", "uduk", "pecel", "lele", "rawon", "setiap",
          "sate", "gado", "cincau"}


    print("\n-- harga: kelipatan 100 dan rentang menu --")
    for t in ("Rp17.000", "Rp5.000", "20.000", "9000", "Rp12.500"):
        cek("sah: %s" % t, harga_sah(t), True)
    for t, sebab in (("Rp101.360", "diucapkan alat, bukan harga menu"),
                     ("R12.40", "huruf R bukan Rp"),
                     ("R15.50", "huruf R bukan Rp"),
                     ("45", "di bawah 500"),
                     ("0,000", "nol"),
                     (".000", "nol")):
        cek("TOLAK %s (%s)" % (t, sebab), harga_sah(t), False)

    print("\n-- tanpa nama, tidak ada harga --")
    km2 = {"nasi", "goreng", "es", "teh", "mie", "ayam"}
    cek("nama dikenali -> harga boleh",
        harga_layak_diucapkan("nasi goreng", km2), True)
    cek("satu kata dikenali sudah cukup",
        harga_layak_diucapkan("nasi pocol lald", km2), True)
    cek("nama ngawur -> harga DITAHAN",
        harga_layak_diucapkan("bobsk gnrerg", km2), False)
    cek("nama kosong -> DITAHAN", harga_layak_diucapkan("", km2), False)
    cek("hanya angka -> DITAHAN", harga_layak_diucapkan("20.000", km2), False)

    print("\n-- baris sampah: yang harus DISIMPAN --")
    for b in ("menu makan", "warung larana", "makanan", "minuman",
              "gadogado Rp15.000", "nas goreng Rpin.000",
              "es cincau Rp8.000", "mie ayam special Rp12.000"):
        cek("simpan %r" % b, baris_sampah(b, km), False)

    print("\n-- baris sampah: yang harus DIBUANG --")
    for b in ("beka setiap hn greaygrnatste", "boa eorp hun 9eyyeah?",
              "+1234567890", "0.00S/D 2000WB", "T0 00 5D 20.00 03"):
        cek("buang %r" % b, baris_sampah(b, km), True)

    print("\n-- pengaman maks_buang --")
    campur = ["menu makan", "nasi goreng Rp17.000", "boa eorp hun 9eyyeah?",
              "beka setiap hn greaygrnatste"]
    bersih, dibuang = saring_sampah(campur, km)
    cek("2 sampah dibuang dari 4 baris", dibuang, 2)
    cek("2 baris baik bertahan", len(bersih), 2)
    semua_sampah = ["+1234567890", "0.00S/D 2000WB", "boa eorp hun 9eyyeah?"]
    bersih2, dibuang2 = saring_sampah(semua_sampah, km)
    cek("bidikan rusak TOTAL dikembalikan utuh", len(bersih2), 3)
    cek("bidikan rusak total: dibuang = 0 (serahkan ke mutu_bacaan)",
        dibuang2, 0)

    print("\n-- gabung baris kembar --")
    g = gabung_baris_kembar(
        ["mie ayam special Rp12.000", "mie ayam sptolal Rp12000"], km)
    cek("dua varian jadi satu baris", len(g), 1)
    cek("nama terbaik yang dipakai", "special" in g[0], True)
    cek("ejaan harga bertitik menang atas Rp12000", "Rp12.000" in g[0], True)
    cek("nilai harga sama -> ejaan kanonik dipilih",
        gabung_baris_kembar(["es teh Rp5000", "es teh Rp5.000"], km)[0],
        "es teh Rp5.000")

    cek("soto dan sate TIDAK digabung",
        len(gabung_baris_kembar(
            ["soto ayam Rp20.000", "sate ayam Rp20.000"], km)), 2)
    cek("es teh dan es jeruk TIDAK digabung",
        len(gabung_baris_kembar(
            ["es teh Rp5.000", "es jeruk Rp6.000"], km)), 2)
    cek("harga rusak tidak ikut terucap",
        "R018" not in gabung_baris_kembar(["napl pecel R018.000"], km)[0],
        True)

    print("\n-- urut dari bagian sasaran --")
    bg = [("makanan", ["nasi goreng Rp17.000", "gadogado Rp15.000"]),
          ("minuman", ["es teh Rp5.000"])]
    u = urut_dari_bagian(bg, 1)
    cek("sasaran diucapkan lebih dulu", u[0], "minuman")
    cek("isi sasaran menyusul", u[1], "es teh Rp5.000")
    cek("bagian lain TIDAK hilang", "makanan" in u, True)
    cek("seluruh baris tetap ada", len(u), 5)
    cek("indeks di luar jangkauan tidak meledak",
        urut_dari_bagian(bg, 99)[0], "makanan")

    print("\n-- lafal serapan --")
    bank = {"spesial", "mie", "ayam", "s", "p", "e", "c", "i", "a", "l"}
    cek("special -> spesial.wav, BUKAN dieja",
        rencana_ucap("mie ayam special", lambda n: n in bank),
        ["mie", "ayam", "spesial"])
    cek("tanpa spesial.wav, tetap jatuh ke ejaan (tidak diam)",
        rencana_ucap("special", lambda n: n in (bank - {"spesial"})),
        ["s", "p", "e", "c", "i", "a", "l"])
    cek("kata utuh tetap menang atas serapan",
        rencana_ucap("special", lambda n: n in (bank | {"special"})),
        ["special"])

    print("\n-- siapkan_ucapan: satu pintu --")
    bg2 = [("makanan", ["mie ayam special Rp12.000",
                        "mie ayam sptolal Rp12000",
                        "boa eorp hun 9eyyeah?"]),
           ("minuman", ["es teh Rp5.000"])]
    siap, buang = siapkan_ucapan(bg2, 1, km)
    cek("mulai dari minuman", siap[0], "minuman")
    cek("kembar sudah menyatu", sum(1 for b in siap if "mie ayam" in b), 1)
    cek("sampah dibuang", any("9eyyeah" in b for b in siap), False)
    cek("jumlah dibuang tercatat", buang, 1)


    # ==================================================================
    # PENJAGA JUMLAH LEMBAR - dari log perangkat 26 Agu
    # Semua kasus di bawah disalin dari deretan frame yang SUNGGUHAN
    # ==================================================================
    def _f(*nominal_per_frame):
        """[('50000',), (), ('100000',)] -> bentuk frames untuk vote_notes."""
        return [[{"nominal": n, "conf": 0.9, "box": (0, 0, 10, 10)}
                 for n in fr] for fr in nominal_per_frame]

    print("\n-- batas_lembar --")
    # log penekanan ke-34: [1,0,0,1,1,1,1,1,1] -> maksimum serentak = 1
    p34 = _f(("50000",), (), (), ("100000",), ("100000",),
             ("50000",), ("100000",), ("100000",), ("50000",))
    cek("penekanan 34: batas = 1 lembar", batas_lembar(p34, 3), 1)

    # log penekanan ke-3: [1,2,2,2,2,2,2,2,2] -> dua lembar sungguhan
    p03 = _f(("50000",)) + _f(*[("50000", "100000")] * 8)
    cek("penekanan 3: batas = 2 lembar", batas_lembar(p03, 3), 2)

    cek("frames kosong -> 0", batas_lembar([], 3), 0)
    cek("dukungan kurang -> turun ke 1",
        batas_lembar(_f(("a", "b"), ("a",), ("a",), ("a",)), 3), 1)

    print("\n-- vote_notes TIDAK BOLEH mengarang lembar --")
    h = vote_notes(p34, 3)
    cek("penekanan 34 -> HANYA SATU lembar", len(h), 1)
    cek("yang bertahan yang paling didukung", h[0]["nominal"], "100000")

    h3 = vote_notes(p03, 3)
    cek("dua lembar sungguhan TETAP dua", len(h3), 2)
    cek("keduanya nominal berbeda",
        sorted(d["nominal"] for d in h3), ["100000", "50000"])

    print("\n-- dukungan frame mengalahkan confidence --")
    campur = [
        [{"nominal": "A", "conf": 0.99, "box": (0, 0, 9, 9)}],
        [{"nominal": "B", "conf": 0.80, "box": (0, 0, 9, 9)}],
        [{"nominal": "B", "conf": 0.80, "box": (0, 0, 9, 9)}],
        [{"nominal": "A", "conf": 0.99, "box": (0, 0, 9, 9)}],
        [{"nominal": "B", "conf": 0.80, "box": (0, 0, 9, 9)}],
        [{"nominal": "A", "conf": 0.99, "box": (0, 0, 9, 9)}],
        [{"nominal": "B", "conf": 0.80, "box": (0, 0, 9, 9)}],
    ]
    hc = vote_notes(campur, 3)
    cek("hanya satu lembar bertahan", len(hc), 1)
    cek("B (4 frame) menang atas A (3 frame) meski conf lebih rendah",
        hc[0]["nominal"], "B")
    cek("kunci 'dukungan' tidak bocor ke pemanggil",
        "dukungan" in hc[0], False)

    print("\n-- kasus yang dulu LOLOS dan berbahaya --")
    # satu lembar, label berkedip 50k/100k, tidak pernah bersamaan
    kedip = _f(("50000",), ("100000",), ("50000",),
               ("100000",), ("50000",), ("100000",))
    hk = vote_notes(kedip, 3)
    cek("label berkedip -> tetap SATU lembar", len(hk), 1)

    # tumpukan sungguhan: tiga lembar terlihat serentak
    tiga = _f(*[("1000", "2000", "5000")] * 5)
    ht = vote_notes(tiga, 3)
    cek("tiga lembar serentak -> tetap tiga", len(ht), 3)

    print(f"\n{'=' * 46}\n  {ok} lulus, {gagal} gagal\n{'=' * 46}")
    raise SystemExit(1 if gagal else 0)