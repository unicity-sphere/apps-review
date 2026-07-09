import asyncio
import csv
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright

CUTOFF_DATE = datetime(2026, 8, 1, tzinfo=timezone.utc)
MODERATION_URL = "https://quest-admin.unicity.network/moderation"
CSV_PATH = Path("data/moderation_review_full.csv")
HTML_PATH = Path("index.html")
COOKIE_DOMAIN = "quest-admin.unicity.network"
FIELDNAMES = [
    "project_name", "organization", "category", "suggested_track",
    "app_url", "status", "tier_suggestion", "good", "missing",
]


def parse_cookie_header(raw):
    cookies = []
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        cookies.append({
            "name": name.strip(),
            "value": value.strip(),
            "domain": COOKIE_DOMAIN,
            "path": "/",
        })
    return cookies


async def scrape_queue(cookie_header):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context()
        await context.add_cookies(parse_cookie_header(cookie_header))
        page = await context.new_page()
        await page.goto(MODERATION_URL, wait_until="networkidle")

        if "/login" in page.url:
            print("Session cookie invalid or expired -- skipping this run.")
            await browser.close()
            return None

        rows = []
        while True:
            await page.wait_for_selector("main table tbody tr")
            trs = await page.query_selector_all("main table tbody tr")
            for tr in trs:
                cells = await tr.query_selector_all("td")
                if len(cells) < 4:
                    continue
                project_name = (await cells[0].inner_text()).strip()
                organization = (await cells[1].inner_text()).strip()
                category = (await cells[2].inner_text()).strip().upper()
                link = await cells[3].query_selector("a")
                app_url = ""
                if link:
                    href = await link.get_attribute("href")
                    if href:
                        app_url = re.sub(r"^https?://", "", href).rstrip("/")
                if project_name:
                    rows.append({
                        "project_name": project_name,
                        "organization": organization,
                        "category": category,
                        "app_url": app_url,
                    })

            next_btn = await page.query_selector("button:text('Next')")
            if not next_btn:
                break
            is_disabled = await next_btn.is_disabled()
            if is_disabled:
                break
            await next_btn.click()
            await page.wait_for_timeout(800)

        await browser.close()
        return rows


def load_known(csv_path):
    known = {}
    if not csv_path.exists():
        return known
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            known[row["project_name"]] = row
    return known


def write_csv(csv_path, rows_by_name):
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows_by_name.values():
            writer.writerow({k: row.get(k, "") for k in FIELDNAMES})


def update_html_data(html_path, rows_by_name):
    text = html_path.read_text(encoding="utf-8")
    m = re.search(r"const DATA = (\[.*?\]);", text, re.DOTALL)
    if not m:
        raise RuntimeError("Could not find DATA array in index.html")
    data_json = json.dumps(list(rows_by_name.values()), ensure_ascii=False)
    new_text = text[:m.start(1)] + data_json + text[m.end(1):]
    html_path.write_text(new_text, encoding="utf-8")


def main():
    now = datetime.now(timezone.utc)
    if now >= CUTOFF_DATE:
        print(f"Past cutoff date ({CUTOFF_DATE.date()}) -- no-op.")
        return

    cookie_header = os.environ.get("QUEST_ADMIN_COOKIE", "").strip()
    if not cookie_header:
        print("No QUEST_ADMIN_COOKIE secret set -- skipping this run.")
        return

    scraped = asyncio.run(scrape_queue(cookie_header))
    if scraped is None:
        return  # login wall / expired cookie, already logged

    known = load_known(CSV_PATH)
    today = now.strftime("%Y-%m-%d")
    changed = False

    for item in scraped:
        name = item["project_name"]
        if name not in known:
            known[name] = {
                "project_name": name,
                "organization": item["organization"],
                "category": item["category"],
                "suggested_track": "N/A",
                "app_url": item["app_url"],
                "status": "New - Unreviewed",
                "tier_suggestion": "Needs review",
                "good": "N/A.",
                "missing": (
                    f"Newly submitted since the last automated check ({today}); "
                    "not yet live-reviewed for SDK use, security, or craft. "
                    "Needs manual pass."
                ),
            }
            changed = True
        else:
            existing = known[name]
            if (existing.get("app_url") != item["app_url"]
                    or (existing.get("category") or "").upper() != item["category"].upper()):
                old_url = existing.get("app_url")
                existing["organization"] = item["organization"]
                existing["category"] = item["category"]
                existing["app_url"] = item["app_url"]
                existing["status"] = "Needs recheck"
                existing["tier_suggestion"] = "Needs recheck"
                existing["good"] = (existing.get("good") or "") + (
                    f" (Note: submission details changed on {today} -- "
                    "previous review may be stale, re-verify.)"
                )
                existing["missing"] = (
                    f"Submission was updated since the last live review "
                    f"(previous app_url: {old_url}). Re-verify live functionality, "
                    "SDK use, and security before scoring."
                )
                changed = True

    if not changed:
        print("No new or altered submissions -- nothing to do.")
        return

    write_csv(CSV_PATH, known)
    update_html_data(HTML_PATH, known)
    print(f"Updated data -- {len(known)} total rows tracked.")


if __name__ == "__main__":
    main()
