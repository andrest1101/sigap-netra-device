# Contoh konfigurasi jembatan cloud SIGAP-NETRA.
#
# SALIN file ini menjadi `config.py` DI ALAT SAJA (folder aplikasi):
#
#   cp config.example.py config.py
#   chmod 600 config.py
#
# ATURAN KERAS:
# - `config.py` berisi kredensial asli. Jangan pernah di-commit, di-share,
#   atau di-log. Hanya hidup di filesystem MaixCAM.
# - Yang di-commit ke repo HANYA file contoh ini (placeholder `??`).
# - Password Wi-Fi, password akun alat, dan token tidak pernah masuk Firestore.
# - `config.py` sengaja TIDAK dicantumkan di app.yaml.

CONFIG = {
    # Firebase Console -> Project settings -> Project ID.
    "project_id": "??",

    # Firebase Console -> Project settings -> General -> Web API Key.
    # Identifier publik, tetapi tetap hanya disimpan di alat.
    "api_key": "??",

    # ID dokumen `devices/{device_id}`. Final dan stabil.
    "device_id": "??",

    # Akun Firebase Auth (Email/Password) khusus alat ini.
    # UID akun ini harus sama dengan field `authUid` di dokumen device.
    "device_email": "??",
    "device_password": "??",

    # Metadata tampilan. Tidak wajib diubah.
    "name": "Kacamata Cerdas 01",
    "model": "MaixCAM SG2002",
    "firmware_version": "v1.2.0-pkm",

    # Privasi: OCR hanya diunggah bila `upload_text` true DAN
    # `settings.uploadText` di dokumen device juga true.
    "upload_text": False,

    # Thumbnail belum diimplementasikan pada fase ini.
    "upload_thumbnails": False,
}
