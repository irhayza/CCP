"""Uji UI di Chromium sungguhan.  Jalankan:  python -m tests.e2e.run_ui [folder_screenshot]
Butuh: pip install playwright && playwright install chromium"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from tests.e2e.adapter import serve

SHOTS = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/shots")
SHOTS.mkdir(parents=True, exist_ok=True)


def main() -> int:
    srv, backend = serve()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    errors: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True, locale="id-ID")
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" and "Failed to load resource" not in m.text else None)

        page.goto(base)
        expect(page.locator(".chip").first).to_be_visible(timeout=10_000)
        page.screenshot(path=str(SHOTS / "01-awal.png"))

        # Pengaturan: isi API key
        page.click("text=Pengaturan")
        page.fill("#apikey", "AIza" + "x" * 30)
        page.click("#dlg-settings button.primary")

        page.fill("#url", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        page.click("#go")
        expect(page.locator(".clip")).to_have_count(4, timeout=30_000)
        expect(page.locator(".mark")).to_have_count(4)
        page.wait_for_timeout(900)                                   # biarkan animasi "developing" selesai
        page.screenshot(path=str(SHOTS / "02-hasil.png"), full_page=True)
        assert "Gemini" in page.inner_text("#notes")
        chan = page.locator("#video-meta a.chan")                       # kanal sumber video: tautan aman ke YouTube
        expect(chan).to_have_text("Chan")
        assert chan.get_attribute("href") == "https://www.youtube.com/@chan"
        assert chan.get_attribute("target") == "_blank" and "noopener" in chan.get_attribute("rel")
        first_score = int(page.locator(".clip .score").first.inner_text().split("\n")[0])
        assert first_score >= 90, first_score
        assert page.locator(".clip-title").first.input_value().startswith("Kesalahan terbesar")

        # Studio terbuka otomatis untuk klip aktif; tunggu frame dimuat
        expect(page.locator("#phone")).to_be_visible(timeout=10_000)
        expect(page.locator("#phone img").first).to_be_visible(timeout=30_000)
        expect(page.locator("#phone .pv-msg")).to_have_count(0, timeout=60_000)      # frame asli sudah dimuat
        assert page.locator("#phone img").first.get_attribute("src").startswith("blob:")
        page.screenshot(path=str(SHOTS / "03-studio.png"))

        # Semua layout menghasilkan lapisan preview yang benar
        counts = {}
        for mode in ("fill_center", "letterbox", "split", "pip", "duo", "fill_face"):
            page.click(f'[data-k="layout.mode"][data-v="{mode}"]')
            counts[mode] = page.locator("#phone .pv").count()
            if mode in ("split", "pip", "letterbox", "duo"):
                page.screenshot(path=str(SHOTS / f"04-layout-{mode}.png"), clip=page.locator(".stage").bounding_box())
        assert counts == {"fill_center": 1, "letterbox": 2, "split": 2, "pip": 2, "duo": 2, "fill_face": 1}, counts

        # Caption & judul
        page.click('[data-k="captions.preset"][data-v="beast_punch"]')
        assert "57, 255, 20" in page.locator(".pv-cap span").get_attribute("style") or "39ff14" in page.locator(".pv-cap span").get_attribute("style").lower()
        page.click("summary:has-text('Judul di atas video')")      # grup ini tertutup secara default
        page.check('[data-k="title.enabled"]')
        expect(page.locator(".pv-title")).to_be_visible()
        page.uncheck('[data-k="captions.enabled"]')
        expect(page.locator(".pv-cap")).to_have_count(0)
        page.check('[data-k="captions.enabled"]')
        page.click('[data-k="layout.mode"][data-v="fill_center"]')
        page.click("summary:has-text('Hasil akhir')")
        page.click('[data-k="resolution"][data-v="720"]')
        page.click('[data-k="quality"][data-v="fast"]')
        page.screenshot(path=str(SHOTS / "05-studio-styled.png"))

        # Edit waktu klip: tombol + menambah 0,5 dan durasi ikut berubah
        row = page.locator(".clip").nth(1)
        end_in = row.locator('[data-f="end"]')
        end0, dur0 = float(end_in.input_value()), row.locator("[data-dur]").inner_text()
        row.locator('[data-step="end:0.5"]').click()
        row.locator('[data-step="end:0.5"]').click()
        assert abs(float(end_in.input_value()) - (round(end0 * 2) / 2 + 1.0)) < 1e-6, (end0, end_in.input_value())
        assert row.locator("[data-dur]").inner_text() != dur0
        assert "." not in end_in.input_value() or len(end_in.input_value().split(".")[1]) <= 1

        # Pilih 2 klip lalu render batch
        page.locator(".clip .pick").nth(0).check()
        page.locator(".clip .pick").nth(1).check()
        for i in (2, 3):
            page.locator(".clip .pick").nth(i).uncheck()
        expect(page.locator("#render")).to_have_text("Render 2 klip")
        page.click("#render")
        expect(page.locator("#queue")).to_be_visible(timeout=10_000)
        expect(page.locator("#queue-summary")).to_have_text("2 dari 2 selesai", timeout=180_000)
        expect(page.locator(".job.done")).to_have_count(2)
        assert "{n}" not in page.inner_text("body"), "placeholder terjemahan belum terisi"      # mis. toast 'Render selesai'
        page.screenshot(path=str(SHOTS / "06-antrean.png"))
        page.locator("#queue-list").scroll_into_view_if_needed()
        page.screenshot(path=str(SHOTS / "06b-antrean-sumber.png"), clip=page.locator("#queue").bounding_box())
        expect(page.locator(".job.done .job-source")).to_have_count(2)       # tiap klip jadi mencantumkan kanal sumbernya
        expect(page.locator(".job.done .job-source").first).to_contain_text("Sumber:")
        jl = page.locator(".job.done .job-source a.chan").first
        assert jl.get_attribute("href") == "https://www.youtube.com/@chan" and "noopener" in jl.get_attribute("rel")
        zip_href = page.get_attribute("#zip", "href")
        r = ctx.request.get(base + zip_href)
        assert r.status == 200 and r.headers["content-type"] == "application/zip" and len(r.body()) > 10_000
        assert ctx.request.get(base + page.get_attribute("#csv", "href")).status == 200
        with page.expect_download() as dl:
            page.locator(".job.done a.btn").first.click()
        assert dl.value.suggested_filename.endswith(".mp4")
        page.locator('.job.done [data-act="copy"]').first.click()

        # Bahasa & dialog
        page.click("#lang-en")
        expect(page.locator("#go")).to_have_text("Analyze video")
        expect(page.locator("#queue-summary")).to_have_text("2 of 2 done")
        expect(page.locator(".job.done .job-source").first).to_contain_text("Source:")
        page.click("text=Cookies")
        page.fill("#ck-text", "bukan cookies")
        page.click("#ck-save")
        expect(page.locator("#ck-error")).to_be_visible()
        page.screenshot(path=str(SHOTS / "07-cookies-en.png"))
        page.keyboard.press("Escape")
        page.click("#lang-id")

        # Mobile
        m = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, locale="id-ID")
        mp = m.new_page()
        mp.on("pageerror", lambda e: errors.append(f"mobile pageerror: {e}"))
        mp.goto(base)
        mp.fill("#url", "https://youtu.be/dQw4w9WgXcQ")
        mp.click("#go")
        mp.wait_for_selector(".clip", timeout=30_000)              # tanpa API key -> jalur heatmap saja (8 klip)
        assert mp.locator(".clip").count() == 8
        assert "heatmap" in mp.inner_text("#notes").lower()
        mp.wait_for_timeout(900)
        boxes = mp.evaluate("[...document.querySelectorAll('.mark b')].map(b => { const r = b.getBoundingClientRect(); return [r.left, r.top, r.right, r.bottom]; })")
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                assert a[2] <= b[0] + 1 or b[2] <= a[0] + 1 or a[3] <= b[1] + 1 or b[3] <= a[1] + 1, f"label strip bertumpuk: {a} {b}"
        overflow = mp.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        mp.screenshot(path=str(SHOTS / "08-mobile.png"), full_page=True)
        assert overflow <= 1, f"overflow horizontal {overflow}px di mobile"
        browser.close()
    backend.close()
    if errors:
        print("ERROR KONSOL:", *errors, sep="\n  ")
        return 1
    print("UI E2E OK. Screenshot:", SHOTS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
