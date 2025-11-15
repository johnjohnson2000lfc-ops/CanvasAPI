# canvas_modules_downloader.py
# pip install playwright requests
# And ensure: playwright install

import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse, urljoin
import requests
from playwright.sync_api import sync_playwright

COURSE_URL = "https://liverpool.instructure.com/courses/83671"
DOWNLOAD_DIR = Path("canvas_downloads")
ALLOWED_EXT = {".pdf", ".txt"}
BASE_HOST = "liverpool.instructure.com"

def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = name.strip().strip(". ")
    if len(name) > 220:
        root, ext = os.path.splitext(name)
        name = root[:220 - len(ext)] + ext
    return name or "file"

def to_download_url(url: str) -> str | None:
    # If already a direct download link, keep it
    if "/download" in url:
        return url
    # Convert /files/<id> to /files/<id>/download?download_frd=1
    m = re.search(r"/files/(\d+)", url)
    if m:
        parsed = urlparse(url)
        scheme, netloc = parsed.scheme, parsed.netloc
        if not netloc:
            netloc = BASE_HOST
        new_path = f"/files/{m.group(1)}/download"
        new_query = "download_frd=1"
        return urlunparse((scheme or "https", netloc, new_path, "", new_query, ""))
    # If it has a direct extension we care about, keep as-is
    if any(url.lower().split("?")[0].endswith(ext) for ext in ALLOWED_EXT):
        return url
    return None

def derive_filename(resp: requests.Response, fallback_url: str) -> str:
    cd = resp.headers.get("Content-Disposition", "")
    # Try to extract filename from Content-Disposition
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
    if m:
        return sanitize_filename(os.path.basename(m.group(1)))
    # Fallback to URL path
    path = urlparse(fallback_url).path
    name = os.path.basename(path)
    if not os.path.splitext(name)[1]:
        # Add extension based on content-type if missing
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "pdf" in ctype:
            name += ".pdf"
        elif "text/plain" in ctype:
            name += ".txt"
    return sanitize_filename(name or "file")

def build_requests_session_from_context(context) -> requests.Session:
    sess = requests.Session()
    # Use Canvas cookies so requests are authenticated
    for c in context.cookies():
        # Only set cookies for Canvas domains
        domain = c.get("domain") or ""
        if BASE_HOST in domain or domain.endswith(".instructure.com"):
            sess.cookies.set(
                c["name"], c["value"], domain=domain, path=c.get("path", "/")
            )
    # Basic headers to look like a browser
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/129 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-GB,en;q=0.9",
    })
    return sess

def collect_module_file_links(page) -> list[str]:
    # Go to Modules via the left nav (by accessible name)
    try:
        page.get_by_role("link", name=re.compile(r"Modules", re.I)).click(timeout=8000)
    except:
        # If already there or different nav structure, just continue
        pass
    page.wait_for_load_state("domcontentloaded", timeout=15000)
    time.sleep(1.5)

    # Expand all collapsed modules (if expand buttons exist)
    try:
        expand_buttons = page.locator('button[aria-label*="Expand"], button[title*="Expand"]').all()
        for btn in expand_buttons:
            try:
                btn.click(timeout=1000)
                time.sleep(0.2)
            except:
                pass
    except:
        pass

    # Gather all anchors and filter
    anchors = page.eval_on_selector_all(
        "a[href]", "els => els.map(e => ({href: e.href, text: e.textContent||''}))"
    )

    urls = []
    for a in anchors:
        href = a["href"]
        if not href:
            continue
        # Only Canvas domain or absolute
        if BASE_HOST not in href and href.startswith("http"):
            # Skip external links
            continue
        dl = to_download_url(href)
        if not dl:
            continue
        # Filter by extension when possible
        path_no_q = dl.split("?", 1)[0].lower()
        if not any(path_no_q.endswith(ext) for ext in ALLOWED_EXT):
            # If it's the normalized /download endpoint, keep it; otherwise skip
            if "/download" not in dl:
                continue
        urls.append(dl)

    # Deduplicate while preserving order
    seen = set()
    ordered = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    return ordered

def download_all(urls: list[str], sess: requests.Session, out_dir: Path, referer: str):
    out_dir.mkdir(parents=True, exist_ok=True)
    ok, fail = 0, 0
    for i, u in enumerate(urls, 1):
        try:
            # Ensure absolute URL
            if u.startswith("/"):
                u = urljoin(f"https://{BASE_HOST}", u)

            # Some endpoints expect a referer
            headers = {"Referer": referer}
            with sess.get(u, stream=True, allow_redirects=True, timeout=45, headers=headers) as r:
                r.raise_for_status()
                fname = derive_filename(r, u)
                dest = out_dir / fname

                # Skip if already downloaded
                if dest.exists() and dest.stat().st_size > 0:
                    print(f"  [{i}/{len(urls)}] Exists: {fname}")
                    ok += 1
                    continue

                tmp = dest.with_suffix(dest.suffix + ".part")
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        if chunk:
                            f.write(chunk)
                tmp.rename(dest)
                print(f"  [{i}/{len(urls)}] Saved: {fname}")
                ok += 1
        except Exception as e:
            print(f"  [{i}/{len(urls)}] Failed: {u}  ({str(e)[:120]})")
            fail += 1
    print(f"\nDone. Downloaded {ok}, failed {fail}. Files in: {out_dir.resolve()}")

def main():
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    user_data_dir = os.path.expanduser(r"~\AppData\Local\Google\Chrome\User Data")

    with sync_playwright() as p:
        # CRITICAL: use ONLY the Basic config that worked for you
        browser_ctx = p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir
        )
        try:
            page = browser_ctx.pages[0] if browser_ctx.pages else browser_ctx.new_page()

            # Try to navigate programmatically; if blocked, allow manual nav
            try:
                page.goto(COURSE_URL, wait_until="domcontentloaded", timeout=20000)
            except:
                print("Navigation blocked. Please manually open the course in the opened window, then press Enter.")
                input("Press Enter once the course page is loaded...")

            # If still not on the course page, give one more chance to manual
            if "courses/" not in page.url:
                print("Please manually navigate to the course page in the browser, then press Enter.")
                input("Press Enter when ready...")

            print("Collecting file links from Modules...")
            urls = collect_module_file_links(page)
            # Keep only allowed types confidently
            urls = [u for u in urls if any(u.lower().split("?",1)[0].endswith(ext) or "/download" in u for ext in ALLOWED_EXT)]

            print(f"Found {len(urls)} candidate files.")
            if len(urls) == 0:
                print("No files found on Modules. If your course uses Files instead, tell me and I’ll add a Files crawler.")
                return

            sess = build_requests_session_from_context(browser_ctx)
            out_dir = DOWNLOAD_DIR / ("course_" + re.sub(r'[^A-Za-z0-9_-]+', "_", urlparse(COURSE_URL).path))
            download_all(urls, sess, out_dir, referer=COURSE_URL)
        finally:
            # Leave the browser open if you like; but typically close it
            browser_ctx.close()

if __name__ == "__main__":
    main()# CanvasAPI
