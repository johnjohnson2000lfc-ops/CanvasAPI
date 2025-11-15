"""
Canvas Modules Downloader

This script automates the download of files from the "Modules" section of a Canvas course.
It uses Playwright to launch a browser with an existing user profile, which should
allow it to use your active Canvas login session.

SETUP:
1.  Install required Python packages:
    pip install playwright requests

2.  Install Playwright's browser dependencies:
    playwright install

3.  Set your Canvas course URL:
    -   Find the `COURSE_URL` variable below and replace the placeholder URL
        with the one for your course.

4.  Set your browser's user data directory and executable path:
    -   Find the `USER_DATA_DIR` and `EXECUTABLE_PATH` variables and set them
        to the correct paths for your browser.
"""

import os
import re
import time
import platform
from pathlib import Path
from urllib.parse import urlparse, urlunparse, urljoin
import requests
from playwright.sync_api import sync_playwright, Page, BrowserContext

# --- CONFIGURATION ---
# 1. Replace this with the URL of your Canvas course homepage.
COURSE_URL = "https://liverpool.instructure.com/courses/83671"

# 2. Set the path to your browser's user data directory.
USER_DATA_DIR = r"C:\Users\johnj\AppData\Local\Perplexity\Comet\User Data"

# 3. (Optional) Set the path to your browser's executable file.
#    This is only needed if the script has trouble launching your browser.
EXECUTABLE_PATH = r"C:\Users\johnj\AppData\Local\Perplexity\Comet\Application\comet.exe"

# --- SCRIPT SETTINGS ---
DOWNLOAD_DIR = Path("canvas_downloads")
ALLOWED_EXT = {".pdf", ".txt", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".zip"}
BASE_HOST = urlparse(COURSE_URL).hostname

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
    if "/download" in url:
        return url
    m = re.search(r"/files/(\d+)", url)
    if m:
        parsed = urlparse(url)
        new_path = f"/files/{m.group(1)}/download"
        new_query = "download_frd=1"
        return urlunparse(("https", BASE_HOST, new_path, "", new_query, ""))
    if any(url.lower().split("?")[0].endswith(ext) for ext in ALLOWED_EXT):
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

def build_requests_session_from_context(context: BrowserContext) -> requests.Session:
    """Creates a requests session with cookies from the Playwright context."""
    sess = requests.Session()
    for c in context.cookies():
        domain = c.get("domain") or ""
        if BASE_HOST in domain or domain.endswith(".instructure.com"):
            sess.cookies.set(c["name"], c["value"], domain=domain, path=c.get("path", "/"))
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-GB,en;q=0.9",
    })
    return sess

def collect_module_file_links(page: Page) -> list[str]:
    """Navigates to the Modules page and collects all file links."""
    print("Navigating to Modules page...")
    try:
        page.get_by_role("link", name=re.compile(r"Modules", re.I)).click(timeout=10000)
    except Exception:
        print("  Could not find 'Modules' link, assuming we are on the right page or it's named differently.")

    page.wait_for_load_state("domcontentloaded", timeout=20000)

    print("Expanding all module sections...")
    try:
        expand_buttons = page.locator('button[aria-label*="Expand"], button[title*="Expand"]').all()
        for btn in expand_buttons:
            try:
                btn.click(timeout=1000)
                time.sleep(0.2)
            except Exception:
                pass
    except Exception:
        print("  No expand buttons found or they were not clickable.")

    print("Gathering all links from the page...")
    anchors = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")

    urls = []
    for href in anchors:
        if not href or (BASE_HOST not in href and href.startswith("http")):
            continue
        dl = to_download_url(href)
        if dl:
            urls.append(dl)

    seen = set()
    return [u for u in urls if not (u in seen or seen.add(u))]

def download_all(urls: list[str], sess: requests.Session, out_dir: Path, referer: str):
    """Downloads all files from the given URLs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ok, fail = 0, 0
    total = len(urls)
    for i, u in enumerate(urls, 1):
        try:
            if u.startswith("/"):
                u = urljoin(f"https://{BASE_HOST}", u)

            headers = {"Referer": referer}
            with sess.get(u, stream=True, allow_redirects=True, timeout=60, headers=headers) as r:
                r.raise_for_status()
                fname = derive_filename(r, u)
                dest = out_dir / fname

                if dest.exists() and dest.stat().st_size == r.headers.get('Content-Length'):
                    print(f"  [{i}/{total}] SKIPPED (exists): {fname}")
                    ok += 1
                    continue

                tmp_path = dest.with_suffix(dest.suffix + ".part")
                with open(tmp_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        f.write(chunk)
                tmp_path.rename(dest)
                print(f"  [{i}/{total}] SAVED: {fname}")
                ok += 1
        except requests.RequestException as e:
            print(f"  [{i}/{total}] FAILED (network): {u} ({e})")
            fail += 1
        except Exception as e:
            print(f"  [{i}/{total}] FAILED (other): {u} ({str(e)[:120]})")
            fail += 1
        time.sleep(0.2)
    print(f"\nDone. Downloaded {ok}, failed {fail}. Files are in: {out_dir.resolve()}")

def main():
    """Main function to run the downloader."""
    if "instructure.com" not in COURSE_URL:
        print("Error: Please set your COURSE_URL at the top of the script.")
        return

    user_data_dir = USER_DATA_DIR
    if not user_data_dir:
        print("Error: Please set the USER_DATA_DIR path in the script.")
        return

    with sync_playwright() as p:
        try:
            browser_ctx = p.chromium.launch_persistent_context(
                user_data_dir,
                headless=False,
                executable_path=EXECUTABLE_PATH if EXECUTABLE_PATH else None
            )
        except Exception as e:
            print(f"Error launching browser: {e}")
            print("\nTroubleshooting:")
            print(f"1. Make sure your browser is installed and the path '{user_data_dir}' is correct.")
            print(f"2. If you've set it, ensure the EXECUTABLE_PATH is also correct.")
            print("3. Close all other running instances of the browser.")
            return

        try:
            page = browser_ctx.pages[0] if browser_ctx.pages else browser_ctx.new_page()

            print(f"Attempting to navigate to: {COURSE_URL}")
            page.goto(COURSE_URL, wait_until="domcontentloaded", timeout=30000)

            if "login" in page.url.lower() or "courses" not in page.url:
                print("\nACTION REQUIRED:")
                print("The script opened a browser window. Please complete the login process manually.")
                print("After you have successfully navigated to your course homepage, press Enter here.")
                input("Press Enter to continue...")

            if "courses" not in page.url:
                 print("Error: Could not navigate to the course page. Please check the URL and your login.")
                 return

            urls = collect_module_file_links(page)
            urls = [u for u in urls if any(u.lower().split("?", 1)[0].endswith(ext) or "/download" in u for ext in ALLOWED_EXT)]

            if not urls:
                print("\nNo downloadable files found on the Modules page.")
                return

            print(f"Found {len(urls)} candidate files to download.")
            sess = build_requests_session_from_context(browser_ctx)

            course_name = re.search(r'courses/(\d+)', COURSE_URL)
            course_id = course_name.group(1) if course_name else "unknown_course"
            out_dir = DOWNLOAD_DIR / f"course_{course_id}"

            download_all(urls, sess, out_dir, referer=page.url)

        except Exception as e:
            print(f"\nAn unexpected error occurred: {e}")

        finally:
            print("Closing browser.")
            browser_ctx.close()

if __name__ == "__main__":
    main()
