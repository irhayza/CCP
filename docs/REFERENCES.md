# Referensi: proyek serupa dan apa yang diambil darinya

Dokumen ini mencatat proyek lain yang dipelajari untuk memperbaiki Cheat Clip Pro Max, apa yang dipakai, dan apa yang sengaja tidak dipakai.

## Cara riset dan batasannya

- Yang dibaca adalah **halaman README, dokumentasi, dan artikel** tiap proyek (lewat pencarian web). **Kode sumber mereka tidak dibaca dan tidak ada yang disalin.** Semua yang diambil berupa *ide* yang saya tulis sendiri dari nol, atau (untuk YuNet) file model yang diunduh pengguna sendiri.
- Isi tiap halaman saya ringkas apa adanya. Bila halaman tidak menyebut sesuatu (mis. algoritma internal), saya tidak berasumsi.
- Lisensi di bawah adalah yang tertera pada halaman/daftar lisensi repo saat riset. Periksa file `LICENSE` repo masing-masing sebelum memakai kodenya langsung.

## Yang diadopsi

| Referensi | Lisensi | Ide yang diambil | Di mana di proyek ini | Bukti |
|---|---|---|---|---|
| **YuNet** di [opencv_zoo](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet) | MIT | Detektor wajah CNN kecil (`cv2.FaceDetectorYN`) menggantikan Haar. Artikel OpenCV melaporkan, pada gambar uji mereka, YuNet menemukan lebih banyak wajah menyamping/terhalang (10 vs 7) dan sekitar 5× lebih cepat (≈5 ms vs ≈25 ms pada 320×320, i7-10710U). Angka itu dari satu artikel, bukan ukuran akurasi. | `backend/facetrack.py` (`YuNetDetector`, `load_detector`), `backend/models.py` (`python -m backend.models`: hanya https, SHA-256 dan ukuran dicek, file `.part` lalu `os.replace`) | 15 test unduhan (jaringan palsu), test pembungkus detektor, dan render nyata dengan model YuNet asli pada video sintetis |
| **AutoFlip** ([MediaPipe](https://github.com/google-ai-edge/mediapipe/blob/master/docs/solutions/autoflip.md)) | Apache-2.0 | Per adegan, kamera **diam** bila semua wajah tetap dalam batas gerak kecil, selain itu **mengikuti**. Dokumennya juga menyebut deteksi batas adegan sebagai langkah tersendiri. Rumus internalnya tidak ada di halaman itu; yang dipakai di sini adalah gagasannya, dengan ambang buatan sendiri. | `facetrack.static_hold`, `smooth_path(..., hold_static=True)` | 9 test `StaticHoldTests`; render nyata: goyangan kecil → kamera diam, gerak besar → tetap diikuti |
| **PySceneDetect** ([scenedetect.com](https://www.scenedetect.com)) | BSD-3-Clause | Pergantian adegan harus dideteksi supaya kamera **melompat** di cut, bukan menggeser pelan melintasi dua adegan. Ide ini diwujudkan dengan filter `scene` ffmpeg yang sudah ada, **tanpa menambah dependensi**. | `backend/scenes.py`, parameter `cuts` di `smooth_path` | `tests/test_scenes.py` dengan ffmpeg sungguhan (video dengan cut di 2,0 dan 4,0 dtk) |
| **OpenShorts** ([mutonby/openshorts](https://github.com/mutonby/openshorts); yang dibaca: [artikel ini](https://themenonlab.blog/blog/openshorts-open-source-ai-clip-generator), bukan kodenya) | MIT | Dua orang di layar → **tumpuk keduanya** (atas/bawah), caption di sambungan. Kembali ke satu wajah bila hanya satu orang terlihat. | Layout `duo`: `layouts.py`, `facetrack.split_two`, `render.py`, preview studio, opsi "Caption di sambungan" | 33 test `tests/test_duo.py` (render ffmpeg sungguhan, fallback, caption), test JS preview, E2E Chromium, render nyata dengan YuNet pada video dua orang |

Tambahan di luar referensi (permintaan langsung): **kredit kanal YouTube sumber klip**: tampil di header video, kartu antrean, dan kolom `source_channel*` di paket publikasi `.csv`.

## Dipelajari, sengaja tidak diadopsi

| Referensi | Lisensi | Yang dibaca | Alasan tidak dipakai (sekarang) |
|---|---|---|---|
| [Light-ASD](https://github.com/Junhua-Liao/Light-ASD) / LR-ASD (deteksi pembicara aktif) | MIT | README melaporkan mAP 94,06% pada validasi AVA-ActiveSpeaker; butuh file video `.mp4`/`.avi` dan bobot model terlatih. | Menambah model dan dependensi berat. Layout `duo` menghindari kebutuhan menebak siapa yang bicara karena tiap orang punya panel sendiri. Kandidat pengembangan berikutnya untuk memilih satu pembicara saat banyak orang terlihat. |
| [ClipsAI](https://github.com/ClipsAI/clipsai) | MIT | Memilih klip dari transkrip (WhisperX), reframing mengikuti pembicara lewat diarization Pyannote (butuh token Hugging Face). | Diarization butuh token dan model tambahan. Proyek ini sudah memilih klip dari transkrip + heatmap + Gemini. |
| [SamurAIGPT/AI-Youtube-Shorts-Generator](https://github.com/SamurAIGPT/AI-Youtube-Shorts-Generator) | tidak diperiksa | Alur serupa: transkrip, LLM memberi skor, potongan video >30 menit dipecah per jendela, deduplikasi kandidat yang tumpang tindih, OpenCV untuk face tracking. Halaman tidak menjelaskan caption bergaya maupun algoritma tracking-nya. | Fitur-fitur ini sudah ada di sini (jendela waktu, NMS, smoothing, caption karaoke ASS). Tidak ada ide baru yang dibutuhkan. |
| [pycaps](https://github.com/francozanardi/pycaps) dan [PupCaps](https://github.com/hosuaby/PupCaps) (caption beranimasi) | MIT dan Apache-2.0 | Hanya hasil pencarian dan satu artikel penulis pycaps; tidak dievaluasi mendalam. | Proyek ini sudah punya 10 gaya caption ASS per kata. Animasi berbasis CSS butuh perender browser. |
| WhisperX (timestamp kata via alignment) | tidak diperiksa | Hanya hasil pencarian dan sebutannya di README ClipsAI. | `faster-whisper` opsional sudah memberi timestamp kata. Tidak dievaluasi lebih jauh. |
| MediaPipe sebagai detektor wajah | Apache-2.0 | Dipakai AutoFlip dan OpenShorts. | Dependensi besar. YuNet cukup untuk kebutuhan ini dan lebih ringan. |

## Belum dikerjakan (ide lanjutan)

1. Pembicara aktif (Light-ASD atau gerak bibir) untuk klip dengan banyak orang atau untuk memilih panel yang diberi caption.
2. Dua orang terlihat bersamaan tetapi pindah-pindah posisi cepat: jalur kiri/kanan saat ini ditentukan oleh posisi (kiri → panel atas), belum oleh identitas orang.
3. Pemilihan klip yang sadar adegan (batas adegan sebagai petunjuk titik potong).

## Batas verifikasi

- Yang terverifikasi nyata di sandbox: YuNet asli + ffmpeg pada video **sintetis** (foto astronot ditempel pada latar, 1280×720), render layout duo dan fallback, serta UI di Chromium.
- **Belum** diuji pada footage wawancara/podcast sungguhan, dengan YouTube asli, atau dengan GPU/Windows.
- Pada video sintetis dengan dua orang berdampingan di sumber 16:9, tetangga masih terlihat sebagian di tepi panel (akibat crop 16:9 → 9:8). Itu sifat layout, bukan galat.

## Sumber

- [OpenCV: Haar cascade vs YuNet](https://opencv.org/opencv-face-detection-cascade-classifier-vs-yunet/)
- [opencv_zoo: face_detection_yunet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet)
- [MediaPipe AutoFlip](https://github.com/google-ai-edge/mediapipe/blob/master/docs/solutions/autoflip.md)
- [PySceneDetect](https://www.scenedetect.com)
- [OpenShorts (artikel)](https://themenonlab.blog/blog/openshorts-open-source-ai-clip-generator)
- [ClipsAI](https://github.com/ClipsAI/clipsai)
- [Light-ASD](https://github.com/Junhua-Liao/Light-ASD)
- [AI-Youtube-Shorts-Generator](https://github.com/SamurAIGPT/AI-Youtube-Shorts-Generator)
- [Daftar alternatif OpusClip sumber terbuka](https://openapps.pro/alternatives/opusclip)
