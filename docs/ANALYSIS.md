# Analisis `cheat-clip-pro` dan dasar desain Pro Max

## 1. Apa yang bisa dan tidak bisa saya baca

| Sumber | Status |
|---|---|
| `README.md` | Terbaca penuh |
| Daftar file di root repo | Terbaca |
| `package.json` | Terbaca penuh |
| `backend/*.py` (`main.py`, `video_engine.py`) dan `src/**` (React) | **Tidak terbaca**: GitHub memblokir akses otomatis ke halaman folder/kode |

Konsekuensinya:

- Semua temuan di bawah berasal dari README, daftar file, dan `package.json`. Saya tidak mereview kode sumbernya.
- Pro Max **ditulis ulang dari nol** berdasarkan fitur dan endpoint yang dijanjikan README. Tidak ada kode yang disalin atau di-patch dari repo asli.
- Temuan berlabel **Terkonfirmasi** dapat dilihat langsung di repo. Berlabel **Dugaan** adalah risiko yang masuk akal dari pola yang terdokumentasi dan harus kamu cek di kodenya.

## 2. Temuan pada repo asli

| # | Temuan | Status | Dampak | Di Pro Max |
|---|---|---|---|---|
| 1 | `package.json` memanggil `node scripts/start-backend.js` untuk `dev-backend` (dan `npm run dev` bergantung padanya), tetapi daftar file root tidak menampilkan folder `scripts/` | Terkonfirmasi sebagian | Clone baru bisa gagal menjalankan backend. Catatan: halaman daftar file dan halaman `package.json` menampilkan jumlah star yang berbeda (2 vs 13), jadi salah satunya cache lama. **Cek sendiri apakah `scripts/` ada di `master`** (atau terabaikan oleh `.gitignore`). | Satu perintah `python run.py`; tidak ada skrip peluncur terpisah, tidak ada Node. |
| 2 | README menyuruh `git clone https://github.com/your-username/cheat-clip-pro.git` (placeholder) dan menautkan `LICENSE` yang tidak tampil di daftar file | Terkonfirmasi | Pemula salah clone; klaim lisensi MIT tanpa file lisensi tidak punya kekuatan hukum | `LICENSE` disertakan; langkah instalasi 2 perintah. |
| 3 | `GET /api/models?api_key=...` mengirim API key lewat query string | Terkonfirmasi (README) | Kunci masuk riwayat browser, log server/proksi, header `Referer`. | Header `X-Gemini-Key`; kunci tidak pernah tersimpan di DB atau log (diuji). |
| 4 | README menyebut key disimpan "securely" di `localStorage` | Terkonfirmasi (README) | `localStorage` terbaca oleh skrip apa pun di origin yang sama (XSS). Ini bukan penyimpanan aman. | Tetap di browser (server tidak menyimpannya), tetapi opsi "Ingat di browser ini" bisa dimatikan (pindah ke `sessionStorage`), dan UI tidak lagi mengklaim "aman". |
| 5 | Fallback Gemini ditulis tetap: 2.5 / 2.0 / **1.5** Flash | Terkonfirmasi (README) | Model lama dipensiunkan Google dari waktu ke waktu; daftar tetap cepat basi. Saya tidak bisa memastikan status 1.5 hari ini dari sandbox ini; periksa di akunmu. | Daftar model diambil dari `models.list` milik akun si pengguna, diurutkan versi terbaru dulu; daftar statis hanya cadangan bila pemanggilan gagal. |
| 6 | Backend memakai `cookies.txt`, `exports/`, `temp/` **di dalam** folder `backend/` | Terkonfirmasi (README, struktur proyek) | Data pengguna bercampur dengan kode; `cookies.txt` (sesi login Google!) mudah ikut ter-commit. Isi `.gitignore` tidak bisa saya lihat. | Semua data di `DATA_DIR` (default `./data`, di-ignore), cookies `chmod 600`, API tidak pernah mengembalikan nilai cookie. |
| 7 | Tidak ada penyebutan autentikasi, CORS, atau pembatasan host | Dugaan | Bila server dijalankan di `0.0.0.0`/VPS, siapa pun bisa memakai cookies dan merender di mesinmu. Server lokal pun rentan terhadap situs jahat yang menembak `localhost` (CSRF / DNS rebinding). | Bind default `127.0.0.1`; guard Host+Origin; `APP_TOKEN` dengan cookie `httpOnly`; `run.py` menolak bind jaringan tanpa token. |
| 8 | Endpoint `/api/analyze` berupa satu aliran SSE | Dugaan | Reload halaman atau koneksi putus = analisis hilang dan harus mengulang (dan membayar kuota Gemini lagi). | Job persisten di SQLite; SSE hanya "jendela" ke job; hasil dicache 24 jam. |
| 9 | "Face tracking keeps speaker centered" tanpa penjelasan smoothing | Dugaan | Implementasi naif (crop mengikuti deteksi mentah) menghasilkan kamera bergetar dan melompat saat satu deteksi salah. | `smooth_path`: median 3-sampel, deadzone, batas kecepatan, jump-cut saat adegan berganti, keyframe lewat RDP. Satu bug nyata (deteksi liar lolos filter) ditemukan dan diperbaiki lewat test saat membangunnya (lihat §5). |
| 10 | "OpenCV / MediaPipe" disebut bersamaan | Dugaan | Dua jalur deteksi berarti dua perilaku; MediaPipe menambah dependensi besar. | Satu jalur: Haar cascade bawaan OpenCV (tanpa unduh model). Lebih sederhana, tetapi kurang akurat untuk wajah menyamping; lihat §6. |
| 11 | Di root repo tidak tampak folder tes, workflow CI, maupun Dockerfile (hanya konfigurasi ESLint/TypeScript); isi `backend/` tidak bisa saya lihat | Terkonfirmasi untuk root, dugaan untuk `backend/` | Regresi sulit terdeteksi, terutama pada filtergraph ffmpeg yang sensitif. | 106 test Python, 19 test Node, skrip UI E2E, workflow CI, Dockerfile. |
| 12 | Tidak ada pengingat soal hak cipta / ToS YouTube | Terkonfirmasi (README) | Tool ini mengunduh dan mengunggah ulang konten; pengguna perlu tahu risikonya. | Catatan hukum di README dan di dialog Pengaturan; allowlist host. |

## 3. Pemetaan fitur

| Fitur README asli | Pro Max |
|---|---|
| Heatmap retensi | Dari yt-dlp (`heatmap`), dinormalisasi, ditampilkan sebagai strip film; skor klip memakai rata-rata retensi di rentangnya |
| Gemini multi-model fallback | Dinamis dari akun; 429/5xx/404 → model berikutnya; kunci salah → berhenti dengan pesan jelas; semua gagal → heatmap saja + catatan |
| Studio 9:16 live | Preview memakai matematika crop yang sama dengan backend (diuji silang JS↔Python); frame asli lewat `/api/clip-frame` + posisi wajah |
| Face tracking | Lihat #9 |
| Split / PIP streamer | Ada; area kamera dipilih lewat pojok + ukuran, dengan tampilan sumber yang menandai area |
| Karaoke ASS, 7 preset | 10 preset, satu event per kata (tidak bergantung semantik `\k`), baris dipecah otomatis |
| Sanitasi caption | Tag di dalam `[]`, `()`, `{}`, `♪`, juga yang terpecah beberapa token; escape ASS dinetralkan |
| NVENC + fallback CPU | Deteksi dengan test-encode nyata; fallback otomatis di tengah render bila encoder gagal |
| Batch + ZIP | Antrean persisten, batal/ulang per klip, ZIP + CSV paket publikasi |
| Cookies Netscape | Divalidasi format; hanya ringkasan yang dikembalikan |
| Raw downloader | Progres numerik dari template progres yt-dlp |
| Clear temp | Ditolak bila ada job aktif; folder `exports` tidak pernah disentuh |
| Bilingual EN/ID | Ada; kelengkapan kunci dijaga test |

## 4. Keputusan desain yang perlu kamu ketahui

- **Frontend tanpa build (ES modules), bukan React/Vite.** Saya tidak bisa membaca komponen React asli, dan lingkungan penyusunan tidak punya akses npm untuk menguji build TypeScript. UI tanpa build bisa saya jalankan dan uji di Chromium sungguhan, serta menghilangkan kebutuhan Node untuk pengguna. Bila kamu ingin tetap di React, kontrak API di README cukup untuk menyambungkannya.
- **REST murni ke Gemini, bukan SDK `google-genai`.** Lebih sedikit dependensi, dan mudah diuji dengan HTTP palsu. Konsekuensinya, bila Google mengubah format respons, perubahan ada di satu file (`gemini.py`).
- **Haar cascade, bukan MediaPipe.** Alasan di #10.
- **Satu event ASS per kata.** Jumlah event bertambah, tetapi tampilannya tidak bergantung pada versi libass.
- **`yt-dlp` dipanggil sebagai proses**, bukan sebagai library, supaya bisa dibatalkan dengan andal (kill proses) dan crash-nya terisolasi.

## 5. Bug yang ditemukan test selama pembuatan

Dicatat karena ini contoh kelas masalah yang akan muncul juga di kode asli bila belum diuji:

1. **Outlier deteksi wajah menggerakkan kamera.** Median filter awal bekerja di grid 0,1 dtk, padahal sampel datang tiap 0,4 dtk, sehingga satu deteksi salah lolos. Dipindah ke level sampel.
2. **Style tidak valid → HTTP 500.** `ValueError` mentah dari validator layout tidak ditangkap sebagai kesalahan input. Sekarang selalu 400 dengan pesan.
3. **Flag pembatalan bocor** untuk job yang dibatalkan saat masih antre, sehingga ID-nya tidak bisa diulang. Diperbaiki; "Ulangi" juga memakai ulang baris job yang sama agar antrean tidak menggandakan klip.
4. **Kotak kamera PIP menimpa caption** di pojok bawah. Posisi pojok sekarang menghindari zona judul dan caption.
5. **Label skor di strip bertumpuk** ketika klip berdekatan; sekarang diberi jalur bertingkat dengan tangkai.
6. **Angka waktu terpotong** di input (`675.95` tampil `675.9!`). Sekarang dibulatkan untuk tampilan.
7. **Preview tanpa frame** hanya kotak hitam polos; sekarang memakai placeholder bergambar agar simulasi layout tetap terbaca.
8. **Kebocoran file handle** pada subprocess (terdeteksi dengan `-W error::ResourceWarning`).
9. **Tombol Batal tidak menghentikan proses anak.** `terminate()` hanya mematikan proses induk (yt-dlp); ffmpeg yang dijalankannya tetap hidup dan terus memakai CPU/GPU. Terbukti dengan skrip reproduksi, di Linux maupun (menurut sifat Windows) di sana. Sekarang setiap subprocess dibuat di grup prosesnya sendiri dan dimatikan sebagai pohon (`killpg` + eskalasi SIGKILL di POSIX, `taskkill /T /F` di Windows), termasuk saat server berhenti (karena grup terpisah tidak menerima Ctrl+C). Test sengaja disabotase untuk memastikan ia gagal pada perilaku lama.

## 6. Status verifikasi (jujur)

**Diuji dan lolos**

- Engine video dengan **ffmpeg sungguhan**: kelima layout, caption, judul, 720p/1080p, loudnorm, pan face-track dengan keyframe, progres, pembatalan proses, ekstraksi frame.
- Pipeline analisis dan render end-to-end memakai yt-dlp palsu (data sintetis) dan Gemini palsu: fusi skor, snapping, cache, degradasi bertahap tanpa AI/heatmap/subtitle.
- Job manager: sukses, gagal, batal saat berjalan (proses di-kill), batal saat antre, ulang, restart server.
- Handler HTTP lewat stub FastAPI: validasi input, path traversal, ZIP/CSV, cookies (izin file 0600), guard Host/Origin, token, API key tidak masuk database.
- UI di **Chromium**: alur lengkap, semua layout, ganti bahasa, dialog, unduhan nyata, tanpa error konsol, tanpa overflow horizontal di 390px, label strip tidak bertumpuk.

**Belum diuji, harus kamu coba**

| Hal | Kenapa belum | Risiko |
|---|---|---|
| Unduhan YouTube sungguhan (yt-dlp, `--download-sections`, subtitle json3, heatmap) | Tidak ada internet | Sedang: format output yt-dlp dan kebutuhan runtime JS sering berubah |
| Panggilan Gemini sungguhan | Tidak ada internet | Rendah–sedang: skema respons mengikuti dokumentasi REST publik, diuji dengan HTTP palsu |
| NVENC / encoder GPU lain | Tidak ada GPU | Rendah: deteksi memakai test-encode dan ada fallback CPU otomatis |
| FastAPI asli (`tests/test_http.py`) | Paket tidak terpasang | Rendah–sedang: handler diuji lewat stub, bukan server asli; parameter `Header`/`Query` paling mungkin butuh penyesuaian |
| Akurasi deteksi wajah pada footage nyata | Tidak ada video wajah | Sedang: Haar lemah pada wajah menyamping/tertutup. Bila wajah tidak stabil terdeteksi, renderer jatuh ke crop tengah dan memberi catatan |
| Windows, macOS, browser selain Chromium | Hanya Linux + Chromium | Sedang: escape path ffmpeg, `taskkill`, dan `run.bat` ditulis sesuai kaidah Windows dan divalidasi statis (label, escape, CRLF), tetapi belum pernah dijalankan di sana. Job `windows-latest` di CI disiapkan untuk menguji ini; test pengecekan proses via `/proc` otomatis dilewati di Windows |
| Whisper (`faster-whisper`) | Tidak terpasang | Rendah: jalur opsional, impor terjaga |

## 7. Langkah berikutnya yang disarankan

1. `python -m pip install -r requirements-dev.txt && python -m unittest discover -s tests -t .` di mesinmu, terutama `tests.test_http`.
2. Jalankan satu video nyata dengan cookies dan key Gemini; periksa catatan di bawah strip bila ada fallback.
3. Uji klip dengan satu pembicara dan dua pembicara; bila Haar kurang akurat, titik penggantian detektor hanya satu fungsi (`facetrack.detect_faces`).
4. Ide lanjutan: pemilihan pembicara aktif (diarization / gerak bibir), b-roll otomatis, penjadwalan unggah.
