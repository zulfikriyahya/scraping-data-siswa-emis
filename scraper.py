import os
import re
import time
import json
from pathlib import Path
from collections import Counter
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
from tqdm import tqdm
import pandas as pd

load_dotenv()
EMAIL = os.getenv("EMIS_EMAIL")
PASSWORD = os.getenv("EMIS_PASSWORD")

AUTH_FILE = "auth.json"
BASE_URL = "https://emis.kemenag.go.id/kesiswaan"
TOTAL_PAGES = 31

TABLE_SECTION_TITLES = {
    "AKTIVITAS BELAJAR": "aktivitas_belajar",
    "BEASISWA & BANTUAN": "beasiswa",
    "PRESTASI SISWA": "prestasi",
}
PERSON_SECTION_KEYS = {
    "Ayah kandung": "ayah",
    "Ibu kandung": "ibu",
    "Wali": "wali",
}


def ensure_login(playwright):
    """Login manual sekali (captcha), simpan session ke auth.json. Selalu headed."""
    if Path(AUTH_FILE).exists():
        return
    browser = playwright.firefox.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto("https://emis.kemenag.go.id/")
    page.get_by_role("button", name="Login").click()
    page.locator('input[type="email"]').fill(EMAIL)
    page.locator('input[type="password"]').fill(PASSWORD)
    print("\n>>> Selesaikan captcha 'Saya bukan robot' secara manual di window Firefox.")
    print(">>> Setelah berhasil login dan halaman utama EMIS muncul, kembali ke terminal ini dan tekan ENTER.\n")
    input("Tekan ENTER setelah login manual selesai...")
    context.storage_state(path=AUTH_FILE)
    browser.close()


def parse_person_section(section_tag):
    """Ambil pasangan label:value dari satu section (siswa/ayah/ibu/wali)."""
    data = {}
    rows = section_tag.find_all(
        "div",
        class_=lambda c: c and all(
            k in c.split() for k in ["r-18u37iz", "r-1wtj0ep", "r-oyd9sg", "r-13qz1uu"]
        ),
        recursive=False,
    )
    for row in rows:
        if row.select_one('[data-testid="text-name"]'):
            continue  # baris judul, skip
        label_el = row.select_one('div[class*="r-1enofrn"]')
        value_el = row.select_one('[data-testid="text-address-value"]')
        if label_el and value_el:
            label = label_el.get_text(strip=True)
            value = value_el.get_text(strip=True)
            data[label] = value
    return data


def parse_table_section(section_tag):
    """Ambil isi tabel (aktivitas belajar / beasiswa / prestasi)."""
    if section_tag.select_one('[data-testid="no-data"]'):
        return []

    header_row = section_tag.select_one('div[style*="padding: 10px"] > div')
    if not header_row:
        return []
    headers = [c.get_text(strip=True) for c in header_row.find_all("div", recursive=False)]

    rows_container = section_tag.select_one(".r-tvv088")
    if not rows_container:
        return []

    records = []
    for row in rows_container.find_all("div", recursive=False):
        cells = [c.get_text(strip=True) for c in row.find_all("div", recursive=False)]
        if len(cells) == len(headers):
            records.append(dict(zip(headers, cells)))
    return records


def parse_detail_html(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one('div[style*="padding-bottom: 20px"]') or soup

    result = {"siswa": {}, "ayah": {}, "ibu": {}, "wali": {},
              "aktivitas_belajar": [], "beasiswa": [], "prestasi": []}

    headers = root.select('[data-testid="text-name"]')
    for i, header in enumerate(headers):
        title_el = header.select_one('[data-testid="text-name-value"]')
        title = title_el.get_text(strip=True) if title_el else ""
        section_container = header.parent.parent  # naik 2 level dari header ke container section

        if title in TABLE_SECTION_TITLES:
            key = TABLE_SECTION_TITLES[title]
            result[key] = parse_table_section(section_container)
        elif title in PERSON_SECTION_KEYS:
            key = PERSON_SECTION_KEYS[title]
            result[key] = parse_person_section(section_container)
        else:
            # section pertama tanpa judul section eksplisit = data siswa; title = nama siswa
            result["siswa"] = parse_person_section(section_container)
            result["siswa"]["NAMA"] = title

    return result


def is_record_valid(record) -> bool:
    """Anggap valid kalau minimal salah satu dari NIK/NAMA/NISN terisi."""
    siswa = record.get("siswa", {})
    return bool(siswa.get("NIK") or siswa.get("NAMA") or siswa.get("NISN"))


def scrape_page(page, page_num, all_records, pbar=None):
    page.goto(f"{BASE_URL}?page={page_num}", wait_until="networkidle")

    try:
        page.wait_for_selector("text=Aksi", timeout=15000)
    except Exception as e:
        if pbar:
            pbar.write(f"Gagal nemu teks 'Aksi' di page {page_num}: {e}")
        return

    rows = page.locator('div.r-14lw9ot[style*="border-radius: 0px"]').filter(has_text="Aksi")
    row_count = rows.count()

    for i in range(row_count):
        if pbar:
            pbar.set_description(f"Page {page_num} | Baris {i+1}/{row_count}")

        try:
            row = rows.nth(i)
            row.scroll_into_view_if_needed(timeout=45000)
            row.get_by_text("Aksi", exact=True).click()
            page.get_by_role("menuitem", name="Lihat Detail").click()

            page.wait_for_selector('[data-testid="text-name-value"]', state="visible", timeout=15000)
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1000)

            page.wait_for_function(
                """() => {
                    const headers = document.querySelectorAll('[data-testid="text-name-value"]');
                    return Array.from(headers).some(h => h.textContent.includes('PRESTASI SISWA'));
                }""",
                timeout=10000,
            )

            heading = page.locator('[data-testid="text-name"]').first
            detail_container = heading.locator(
                'xpath=ancestor::div[@style and contains(@style, "padding-bottom: 20px")]'
            ).first
            detail_container.wait_for(state="visible", timeout=10000)
            html = detail_container.evaluate("el => el.outerHTML")

            record = parse_detail_html(html)

            if not is_record_valid(record):
                if pbar:
                    pbar.write(f"  ⚠️  Data siswa kosong di page {page_num} baris {i+1}, retry...")
                page.wait_for_timeout(1500)
                html = detail_container.evaluate("el => el.outerHTML")
                record = parse_detail_html(html)

            record["_page"] = page_num
            record["_row"] = i + 1
            record["_failed"] = not is_record_valid(record)
            all_records.append(record)

            nama = record["siswa"].get("NAMA", "-")
            if pbar:
                pbar.write(f"  ✅ Page {page_num} baris {i+1}: {nama}")

            page.go_back()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(500)

        except Exception as e:
            if pbar:
                pbar.write(f"  ❌ Gagal di page {page_num} baris {i+1}: {e}")
            try:
                page.screenshot(path=f"debug_page{page_num}_row{i+1}.png", full_page=True)
            except Exception:
                pass
            all_records.append({
                "siswa": {}, "ayah": {}, "ibu": {}, "wali": {},
                "aktivitas_belajar": [], "beasiswa": [], "prestasi": [],
                "_page": page_num, "_row": i + 1, "_failed": True,
            })
            try:
                page.goto(f"{BASE_URL}?page={page_num}", wait_until="networkidle")
            except Exception:
                pass
            continue
        finally:
            if pbar:
                pbar.update(1)


def check_duplicate_nisn(records):
    nisn_list = [r["siswa"].get("NISN", "") for r in records if r["siswa"].get("NISN")]
    counter = Counter(nisn_list)
    duplicates = {nisn: count for nisn, count in counter.items() if count > 1}
    if duplicates:
        print(f"\n⚠️  Ditemukan {len(duplicates)} NISN duplikat:")
        for nisn, count in duplicates.items():
            print(f"  - {nisn}: muncul {count}x")
    else:
        print("\n✅ Tidak ada NISN duplikat.")
    return duplicates


def export_to_excel(records, filename="data_siswa_emis.xlsx"):
    check_duplicate_nisn(records)

    valid_records = [r for r in records if not r.get("_failed") and is_record_valid(r)]
    skipped = len(records) - len(valid_records)
    if skipped:
        print(f"ℹ️  {skipped} record gagal/kosong tidak diikutkan ke Excel.")

    siswa_rows, ayah_rows, ibu_rows, wali_rows = [], [], [], []
    aktivitas_rows, beasiswa_rows, prestasi_rows = [], [], []

    for r in valid_records:
        nisn = r["siswa"].get("NISN", "")
        nama = r["siswa"].get("NAMA", "")

        siswa_row = {"nama": nama, **r["siswa"]}
        siswa_rows.append(siswa_row)

        if r["ayah"]:
            ayah_rows.append({"nisn": nisn, "nama_siswa": nama, **r["ayah"]})
        if r["ibu"]:
            ibu_rows.append({"nisn": nisn, "nama_siswa": nama, **r["ibu"]})
        if r["wali"]:
            wali_rows.append({"nisn": nisn, "nama_siswa": nama, **r["wali"]})

        for row in r["aktivitas_belajar"]:
            aktivitas_rows.append({"nisn": nisn, "nama_siswa": nama, **row})
        for row in r["beasiswa"]:
            beasiswa_rows.append({"nisn": nisn, "nama_siswa": nama, **row})
        for row in r["prestasi"]:
            prestasi_rows.append({"nisn": nisn, "nama_siswa": nama, **row})

    with pd.ExcelWriter(filename, engine="openpyxl") as writer:
        pd.DataFrame(siswa_rows).to_excel(writer, sheet_name="Siswa", index=False)
        pd.DataFrame(ayah_rows).to_excel(writer, sheet_name="Ayah", index=False)
        pd.DataFrame(ibu_rows).to_excel(writer, sheet_name="Ibu", index=False)
        pd.DataFrame(wali_rows).to_excel(writer, sheet_name="Wali", index=False)
        pd.DataFrame(aktivitas_rows).to_excel(writer, sheet_name="Aktivitas Belajar", index=False)
        pd.DataFrame(beasiswa_rows).to_excel(writer, sheet_name="Beasiswa", index=False)
        pd.DataFrame(prestasi_rows).to_excel(writer, sheet_name="Prestasi", index=False)

    print(f"Selesai. Data tersimpan di {filename}")


def load_existing_records():
    if Path("progress_backup.json").exists():
        with open("progress_backup.json", "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def main():
    import sys
    retry_mode = len(sys.argv) > 1 and sys.argv[1] == "retry"

    with sync_playwright() as playwright:
        ensure_login(playwright)

        browser = playwright.firefox.launch(headless=True)
        context = browser.new_context(storage_state=AUTH_FILE)
        page = context.new_page()

        all_records = load_existing_records() if retry_mode else []

        if retry_mode:
            pages_needing_retry = set()
            counted = {}
            for r in all_records:
                if r.get("_failed") or not is_record_valid(r):
                    pages_needing_retry.add(r["_page"])
                counted[r["_page"]] = counted.get(r["_page"], 0) + 1
            for p, c in counted.items():
                if c < 30 and p != TOTAL_PAGES:
                    pages_needing_retry.add(p)

            print(f"Page yang akan di-retry: {sorted(pages_needing_retry)}")
            all_records = [r for r in all_records if r["_page"] not in pages_needing_retry]

            total_estimate = len(pages_needing_retry) * 30
            with tqdm(total=total_estimate, unit="siswa") as pbar:
                for page_num in sorted(pages_needing_retry):
                    try:
                        scrape_page(page, page_num, all_records, pbar)
                    except Exception as e:
                        pbar.write(f"Gagal di page {page_num}: {e}")
                        continue
                    with open("progress_backup.json", "w", encoding="utf-8") as f:
                        json.dump(all_records, f, ensure_ascii=False, indent=2)
        else:
            total_estimate = TOTAL_PAGES * 30
            with tqdm(total=total_estimate, unit="siswa") as pbar:
                for page_num in range(1, TOTAL_PAGES + 1):
                    try:
                        scrape_page(page, page_num, all_records, pbar)
                    except Exception as e:
                        pbar.write(f"Gagal di page {page_num}: {e}")
                        continue
                    with open("progress_backup.json", "w", encoding="utf-8") as f:
                        json.dump(all_records, f, ensure_ascii=False, indent=2)

        browser.close()
        export_to_excel(all_records)


if __name__ == "__main__":
    main()