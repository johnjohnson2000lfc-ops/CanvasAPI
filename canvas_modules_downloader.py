"""
Canvas Course Crawler and Downloader

This script automates the download of files from a Canvas course by crawling all accessible pages.
It uses Playwright to launch a browser with an existing user profile to use your active
Canvas login session.

SETUP:
1.  Install required Python packages:
    pip install playwright requests beautifulsoup4

2.  Install Playwright's browser dependencies:
    playwright install

3.  Set your Canvas course URL.
4.  Set your browser's user data directory and executable path.
"""

import os
import re
import time
import platform
from pathlib import Path
from urllib.parse import urlparse, urlunparse, urljoin
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Page, BrowserContext

# --- CONFIGURATION ---
COURSE_URL = "https://liverpool.instructure.com/courses/83671"
USER_DATA_DIR = r"C:\Users\johnj\AppData\Local\Perplexity\Comet\User Data"
EXECUTABLE_PATH = r"C:\Users\johnj\AppData\Local\Perplexity\Comet\Application\comet.exe"

# --- SCRIPT SETTINGS ---
DOWNLOAD_DIR = Path("canvas_downloads")
ALLOWED_EXT = {".pdf", ".txt", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".zip"}
BASE_HOST = urlparse(COURSE_URL).hostname
COURSE_BASE_URL = f"https://{BASE_HOST}/courses/{COURSE_URL.split('/courses/')[-1]}"

def sanitize_filename(name: str) -> str:
    """Removes invalid characters from a filename and truncates it if too long."""
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = name.strip().strip(". ")
    if len(name) > 220:
        root, ext = os.path.splitext(name)
        return root[:220 - len(ext)] + ext
    return name or "file"

def to_download_url(url: str) -> str | None:
    """Converts a Canvas file URL to a direct download link if possible."""
    if "/download" in url or any(url.lower().split("?")[0].endswith(ext) for ext in ALLOWED_EXT):
        m = re.search(r"/files/(\d+)", url)
        if m:
            return urlunparse(("https", BASE_HOST, f"/files/{m.group(1)}/download", "", "download_frd=1", ""))
        return url
    return None

def derive_filename(resp: requests.Response, fallback_url: str) -> str:
    """Derives a filename from Content-Disposition header or URL."""
    cd = resp.headers.get("Content-Disposition", "")
    if cd:
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
        if m:
            return sanitize_filename(os.path.basename(m.group(1)))
    path = urlparse(fallback_url).path
    name = os.path.basename(path)
    if not os.path.splitext(name)[1]:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "pdf" in ctype: name += ".pdf"
        elif "text/plain" in ctype: name += ".txt"
    return sanitize_filename(name or "file")

def build_requests_session(context: BrowserContext) -> requests.Session:
    """Creates a requests session with cookies from the Playwright context."""
    sess = requests.Session()
    for c in context.cookies():
        domain = c.get("domain") or ""
        if BASE_HOST in domain:
            sess.cookies.set(c["name"], c["value"], domain=domain, path=c.get("path", "/"))
    sess.headers.update({"User-Agent": "Mozilla/5.0"})
    return sess

def crawl_for_files(page: Page) -> list[str]:
    """Crawls the course, starting from the homepage, to find all downloadable files."""
    to_visit = [COURSE_BASE_URL]
    visited = set()
    download_links = set()

    while to_visit:
        current_url = to_visit.pop(0)
        if current_url in visited:
            continue

        print(f"  Crawling: {current_url}")
        visited.add(current_url)

        try:
            page.goto(current_url, wait_until="domcontentloaded", timeout=20000)
            soup = BeautifulSoup(page.content(), "html.parser")

            for a in soup.find_all("a", href=True):
                href = a["href"]
                full_url = urljoin(COURSE_BASE_URL, href)

                dl_url = to_download_url(full_url)
                if dl_url:
                    download_links.add(dl_url)
                elif full_url.startswith(COURSE_BASE_URL) and full_url not in visited:
                    to_visit.append(full_url)
        except Exception as e:
            print(f"    Could not crawl page: {e}")

    return list(download_links)

def download_all(urls: list[str], sess: requests.Session, out_dir: Path):
    """Downloads all files from the given URLs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ok, fail = 0, 0
    total = len(urls)
    for i, u in enumerate(urls, 1):
        try:
            with sess.get(u, stream=True, allow_redirects=True, timeout=60) as r:
                r.raise_for_status()
                fname = derive_filename(r, u)
                dest = out_dir / fname

                if dest.exists() and dest.stat().st_size == int(r.headers.get('Content-Length', 0)):
                    print(f"  [{i}/{total}] SKIPPED (exists): {fname}")
                    ok += 1
                    continue

                with open(dest.with_suffix(".part"), "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        f.write(chunk)
                dest.with_suffix(".part").rename(dest)
                print(f"  [{i}/{total}] SAVED: {fname}")
                ok += 1
        except Exception as e:
            print(f"  [{i}/{total}] FAILED: {u} ({e})")
            fail += 1
        time.sleep(0.2)
    print(f"\nDone. Downloaded {ok}, failed {fail}. Files in: {out_dir.resolve()}")

def main():
    """Main function to run the downloader."""
    with sync_playwright() as p:
        print("Launching browser...")
        try:
            browser_ctx = p.chromium.launch_persistent_context(
                USER_DATA_DIR,
                headless=False,
                executable_path=EXECUTABLE_PATH or None
            )
        except Exception as e:
            print(f"Error launching browser: {e}")
            return

        try:
            page = browser_ctx.new_page()
            page.goto(COURSE_URL, wait_until="domcontentloaded", timeout=30000)

            if "login" in page.url.lower():
                print("ACTION REQUIRED: Please log in in the browser, then press Enter.")
                input("Press Enter to continue...")

            if not page.url.startswith(COURSE_BASE_URL):
                 print("Error: Could not navigate to the course page.")
                 return

            print("Starting crawler to find files...")
            urls = crawl_for_files(page)

            if not urls:
                print("\nNo downloadable files found after crawling the course.")
                return

            print(f"Found {len(urls)} files to download.")
            sess = build_requests_session(browser_ctx)

            course_id = COURSE_BASE_URL.split('/')[-1]
            out_dir = DOWNLOAD_DIR / f"course_{course_id}"

            download_all(urls, sess, out_dir)

        except Exception as e:
            print(f"\nAn unexpected error occurred: {e}")

        finally:
            print("Closing browser.")
            browser_ctx.close()

if __name__ == "__main__":
    main()
