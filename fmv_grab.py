#!/usr/bin/env python3
"""
fmv_grab.py - mirror the T.REX ARMS Field Manual Vault PDFs (and optionally EPUBs).

Two phases, both resumable, state kept in SQLite (<out>/manifest.db):

  discover  Playwright (real Chrome) walks the vault listing pages and every
            manual detail page, records each manual's PDF/EPUB URL, and follows
            edition / reference / cited-by slugs until no new slugs appear.
            Each file is downloaded by a background worker as soon as its
            detail page is parsed (--workers 0 to only build the manifest).
  download  Sweep: fetch anything still missing or failed. Plain HTTP against
            files.trex-arms.com. Range-resume of .part files, size check
            against Content-Length, MD5 check against ETag.

Files are saved mirroring the URL path (pdf/army/tc/tc_3-21-76_2025-09.pdf), so
the same URL can never be stored twice and two URLs can never collide.

Usage:
  ./setup.sh && source .venv/bin/activate
  python fmv_grab.py discover --out ./fmv
  python fmv_grab.py download --out ./fmv [--epub] [--workers 4]
  python fmv_grab.py status   --out ./fmv
  python fmv_grab.py all      --out ./fmv
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

BASE = "https://www.trex-arms.com"
VAULT = f"{BASE}/field-manual-vault"
FILES_HOST = "files.trex-arms.com"

# Listing seeds. The unfiltered landing page shows "Recommended", not the full
# list, so walk each collection and each branch. Overlap is fine (dedup by slug).
SEEDS = [
    "coll=current", "coll=recent-archive", "coll=lessons-learned", "coll=historic",
    "branch=army", "branch=usmc", "branch=navy", "branch=joint", "branch=af",
]

RESERVED = {"faq", "citations"}
CF_CHALLENGE_TIMEOUT_MS = 90_000
RE_HREF_SLUG = re.compile(r'href="/field-manual-vault/([a-z0-9][a-z0-9-]*)"')
RE_DATA_SLUG = re.compile(r'slug:"([a-z0-9][a-z0-9-]*)"')
RE_PDF = re.compile(r'https://files\.trex-arms\.com/pdf/[^"\'\s<>]+?\.pdf')
RE_EPUB = re.compile(r'https://files\.trex-arms\.com/epub/[^"\'\s<>]+?\.epub')
RE_TITLE = re.compile(r'<meta property="og:title" content="([^"]*)"')

log = logging.getLogger("fmv")


# --------------------------------------------------------------------------- db
SCHEMA = """
CREATE TABLE IF NOT EXISTS seeds (
    seed TEXT NOT NULL, page INTEGER NOT NULL, n INTEGER NOT NULL,
    PRIMARY KEY (seed, page));
CREATE TABLE IF NOT EXISTS manuals (
    slug TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'new',      -- new | done | failed
    title TEXT, pdf_url TEXT, epub_url TEXT,
    attempts INTEGER NOT NULL DEFAULT 0, error TEXT);
CREATE TABLE IF NOT EXISTS files (
    url TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                     -- pdf | epub
    path TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'new',      -- new | done | failed
    size INTEGER, etag TEXT, attempts INTEGER NOT NULL DEFAULT 0, error TEXT);
"""


def open_db(out: Path) -> sqlite3.Connection:
    out.mkdir(parents=True, exist_ok=True)
    # One connection per thread; WAL + busy timeout handles the concurrency.
    db = sqlite3.connect(out / "manifest.db", isolation_level=None, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


def add_slugs(db: sqlite3.Connection, slugs) -> int:
    before = db.total_changes
    db.executemany("INSERT OR IGNORE INTO manuals(slug) VALUES (?)",
                   [(s,) for s in slugs if s not in RESERVED])
    return db.total_changes - before


def add_file(db: sqlite3.Connection, url: str, kind: str) -> None:
    rel = urlparse(url).path.lstrip("/")  # pdf/army/tc/x.pdf
    db.execute("INSERT OR IGNORE INTO files(url, kind, path) VALUES (?,?,?)", (url, kind, rel))


# --------------------------------------------------------------------- discover
def discover(out: Path, delay: float, headed: bool, max_attempts: int,
             workers: int, epub: bool, verify: bool) -> None:
    from playwright.sync_api import (sync_playwright, TimeoutError as PWTimeout,
                                     Error as PWError)

    db = open_db(out)
    kinds = ("pdf", "epub") if epub else ("pdf",)
    pool = ThreadPoolExecutor(max_workers=workers) if workers else None
    pending: list = []
    submitted: set[str] = set()

    def queue_download(url: str) -> None:
        if pool is None or url in submitted:
            return
        row = db.execute("SELECT path, state FROM files WHERE url=?", (url,)).fetchone()
        if not row or row[1] == "done":
            return
        submitted.add(url)
        pending.append(pool.submit(download_job, out, url, row[0], verify))

    def reap_downloads(block: bool = False) -> None:
        for f in (as_completed(pending) if block else [f for f in pending if f.done()]):
            res, rel, info = f.result()
            log.info("file   %-4s %s %s", res, rel,
                     f"{info/1e6:.1f} MB" if isinstance(info, int) else info)
        pending[:] = [] if block else [f for f in pending if not f.done()]

    # Pick up files recorded by an earlier run that never finished downloading.
    for (url,) in db.execute(
            f"SELECT url FROM files WHERE kind IN ({','.join('?' * len(kinds))}) "
            "AND (state='new' OR (state='failed' AND attempts < ?))", (*kinds, max_attempts)):
        queue_download(url)
    if pending:
        log.info("resuming %d downloads from previous run", len(pending))

    with sync_playwright() as pw:
        # Site is behind a Cloudflare managed challenge. The bundled headless
        # shell is rejected outright, so use the installed Google Chrome with a
        # persistent profile: once the challenge passes, cf_clearance is kept.
        browser = pw.chromium.launch_persistent_context(
            user_data_dir=str(out / "browser-profile"),
            channel="chrome",
            headless=not headed,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        page = browser.pages[0] if browser.pages else browser.new_page()
        # Skip images/fonts/media: we only need the server-rendered HTML.
        page.route("**/*", lambda r: r.abort()
                   if r.request.resource_type in ("image", "font", "media") else r.continue_())

        def wait_for_challenge() -> bool:
            try:
                page.wait_for_function(
                    "!document.title.startsWith('Just a moment') && !document.querySelector('#challenge-running')",
                    timeout=CF_CHALLENGE_TIMEOUT_MS)
                return True
            except PWTimeout:
                return False

        def fetch_html(url: str) -> str | None:
            for attempt in range(3):
                try:
                    resp = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                    status = resp.status if resp else 0
                    if status == 200:
                        return page.content()
                    if resp and resp.headers.get("cf-mitigated") == "challenge":
                        log.info("cloudflare challenge on %s, waiting%s", url,
                                 "" if headed else " (rerun with --headed if this never clears)")
                        if wait_for_challenge():
                            continue  # cookie is set now; re-request for a clean 200
                        log.warning("challenge did not clear on %s", url)
                    else:
                        log.warning("HTTP %s on %s", status, url)
                    if status in (403, 429, 503):
                        time.sleep(30 * (attempt + 1))  # back off, do not hammer
                        continue
                    return None
                except PWTimeout:
                    log.warning("timeout on %s", url)
                    time.sleep(5 * (attempt + 1))
                except PWError as e:
                    if "closed" in str(e):
                        sys.exit("browser window was closed; progress is saved, rerun to resume")
                    raise
            return None

        # 1) Listing pages. 30 per page; an empty page ends the seed.
        for seed in SEEDS:
            done_pages = {p for (p,) in db.execute("SELECT page FROM seeds WHERE seed=?", (seed,))}
            last_empty = db.execute("SELECT 1 FROM seeds WHERE seed=? AND n=0", (seed,)).fetchone()
            if last_empty:
                log.info("seed %s already complete", seed)
                continue
            pg = 1
            while True:
                if pg in done_pages:
                    pg += 1
                    continue
                html = fetch_html(f"{VAULT}?{seed}&page={pg}")
                if html is None:
                    log.error("giving up on %s page %d for now (rerun to resume)", seed, pg)
                    break
                slugs = set(RE_HREF_SLUG.findall(html)) - RESERVED
                new = add_slugs(db, slugs)
                db.execute("INSERT OR REPLACE INTO seeds VALUES (?,?,?)", (seed, pg, len(slugs)))
                log.info("list %-22s p%-3d %2d slugs (%d new)", seed, pg, len(slugs), new)
                if not slugs:
                    break
                pg += 1
                time.sleep(delay)

        # 2) Detail pages, BFS until closure. Detail pages embed edition,
        #    reference and cited-by slugs as slug:"..." in the SvelteKit payload.
        while True:
            row = db.execute(
                "SELECT slug FROM manuals WHERE state='new' "
                "OR (state='failed' AND attempts < ?) ORDER BY attempts, slug LIMIT 1",
                (max_attempts,)).fetchone()
            if not row:
                break
            slug = row[0]
            db.execute("UPDATE manuals SET attempts=attempts+1 WHERE slug=?", (slug,))
            html = fetch_html(f"{VAULT}/{slug}")
            if html is None:
                db.execute("UPDATE manuals SET state='failed', error='fetch' WHERE slug=?", (slug,))
                continue
            pdfs = sorted(set(RE_PDF.findall(html)))
            epubs = sorted(set(RE_EPUB.findall(html)))
            # The page's own download button is the first PDF link in the DOM;
            # other PDFs on the page would be unexpected, so log them.
            own_pdf = RE_PDF.search(html)
            own_epub = RE_EPUB.search(html)
            if len(pdfs) > 1:
                log.warning("%s has %d PDF links; recording all", slug, len(pdfs))
            for u in pdfs:
                add_file(db, u, "pdf")
            for u in epubs:
                add_file(db, u, "epub")
            for u in (pdfs + epubs if "epub" in kinds else pdfs):
                queue_download(u)
            t = RE_TITLE.search(html)
            db.execute("UPDATE manuals SET state=?, title=?, pdf_url=?, epub_url=?, error=? WHERE slug=?",
                       ("done" if own_pdf else "failed",
                        t.group(1) if t else None,
                        own_pdf.group(0) if own_pdf else None,
                        own_epub.group(0) if own_epub else None,
                        None if own_pdf else "no pdf link", slug))
            new = add_slugs(db, set(RE_DATA_SLUG.findall(html)) | set(RE_HREF_SLUG.findall(html)))
            remaining = db.execute("SELECT COUNT(*) FROM manuals WHERE state='new'").fetchone()[0]
            log.info("detail %-40s pdf=%s +%d new slugs, %d queued, %d downloading",
                     slug, "y" if own_pdf else "N", new, remaining, len(pending))
            reap_downloads()
            time.sleep(delay)

        browser.close()
    if pool is not None:
        if pending:
            log.info("discovery done, waiting for %d downloads to finish", len(pending))
        reap_downloads(block=True)
        pool.shutdown()
    status(out)


# --------------------------------------------------------------------- download
def md5_of(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_one(session, out: Path, url: str, rel: str, verify: bool) -> tuple[str, int, str]:
    dest = out / rel
    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)

    head = session.head(url, timeout=60, allow_redirects=True)
    head.raise_for_status()
    total = int(head.headers.get("Content-Length", 0))
    etag = head.headers.get("ETag", "").strip('"')
    md5_ok = verify and re.fullmatch(r"[0-9a-f]{32}", etag) is not None  # multipart etags have "-N"

    if dest.exists() and dest.stat().st_size == total:
        if not md5_ok or md5_of(dest) == etag:
            return "skip", total, etag
        log.warning("md5 mismatch on existing %s, re-downloading", rel)
        dest.unlink()

    have = part.stat().st_size if part.exists() else 0
    if have > total:
        part.unlink()
        have = 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    if have and etag:
        headers["If-Range"] = f'"{etag}"'  # file changed upstream -> server sends full 200

    with session.get(url, headers=headers, stream=True, timeout=(30, 120)) as r:
        r.raise_for_status()
        mode = "ab" if (have and r.status_code == 206) else "wb"
        with part.open(mode) as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)

    got = part.stat().st_size
    if got != total:
        raise IOError(f"size {got} != {total} (kept .part for resume)")
    if md5_ok and md5_of(part) != etag:
        part.unlink()
        raise IOError("md5 mismatch, discarded .part")
    part.replace(dest)  # atomic: a final-named file is always complete
    return "ok", total, etag


_local = threading.local()


def thread_session():
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    if not hasattr(_local, "s"):
        s = requests.Session()
        s.headers["User-Agent"] = "fmv-grab/1.0 (personal archive; resumable, rate limited)"
        retry = Retry(total=5, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=("HEAD", "GET"), respect_retry_after_header=True)
        s.mount("https://", HTTPAdapter(max_retries=retry))
        _local.s = s
    return _local.s


def thread_db(out: Path) -> sqlite3.Connection:
    if not hasattr(_local, "db"):
        _local.db = open_db(out)
    return _local.db


def download_job(out: Path, url: str, rel: str, verify: bool) -> tuple[str, str, int | str]:
    db = thread_db(out)
    try:
        res, size, etag = download_one(thread_session(), out, url, rel, verify)
        db.execute("UPDATE files SET state='done', size=?, etag=?, error=NULL, "
                   "attempts=attempts+1 WHERE url=?", (size, etag, url))
        return res, rel, size
    except Exception as e:  # noqa: BLE001 - record and move on
        db.execute("UPDATE files SET state='failed', error=?, attempts=attempts+1 WHERE url=?",
                   (str(e)[:500], url))
        return "fail", rel, str(e)


def download(out: Path, workers: int, epub: bool, verify: bool, max_attempts: int) -> None:
    db = open_db(out)
    kinds = ("pdf", "epub") if epub else ("pdf",)
    # Re-queue anything marked done whose file has since gone missing.
    for url, rel in db.execute("SELECT url, path FROM files WHERE state='done'").fetchall():
        if not (out / rel).exists():
            db.execute("UPDATE files SET state='new' WHERE url=?", (url,))
    rows = db.execute(
        f"SELECT url, path FROM files WHERE kind IN ({','.join('?' * len(kinds))}) "
        "AND (state='new' OR (state='failed' AND attempts < ?)) ORDER BY url",
        (*kinds, max_attempts)).fetchall()
    log.info("%d files to fetch", len(rows))

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(download_job, out, u, p, verify) for u, p in rows]
        for f in as_completed(futs):
            res, rel, info = f.result()
            done += 1
            log.info("[%d/%d] %-4s %s %s", done, len(rows), res, rel,
                     f"{info/1e6:.1f} MB" if isinstance(info, int) else info)
    status(out)


# ----------------------------------------------------------------------- status
def status(out: Path) -> None:
    db = open_db(out)
    q = lambda sql: db.execute(sql).fetchall()  # noqa: E731
    print("manuals:", dict(q("SELECT state, COUNT(*) FROM manuals GROUP BY state")))
    for kind, st, n, b in q("SELECT kind, state, COUNT(*), COALESCE(SUM(size),0) "
                            "FROM files GROUP BY kind, state ORDER BY kind, state"):
        print(f"files {kind:4} {st:6} {n:6}  {b/1e9:8.2f} GB")
    for slug, err in q("SELECT slug, error FROM manuals WHERE state='failed' LIMIT 20"):
        print("  manual failed:", slug, err)
    for url, err in q("SELECT url, error FROM files WHERE state='failed' LIMIT 20"):
        print("  file failed:", url, err)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["discover", "download", "status", "all"])
    ap.add_argument("--out", type=Path, default=Path("./fmv"))
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between page loads (discover)")
    ap.add_argument("--workers", type=int, default=4,
                    help="parallel downloads; 0 makes discover build the manifest only")
    ap.add_argument("--epub", action="store_true", help="also download EPUBs")
    ap.add_argument("--no-verify", action="store_true", help="skip MD5 vs ETag check")
    ap.add_argument("--headed", action="store_true", help="show the browser (debug)")
    ap.add_argument("--max-attempts", type=int, default=5)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(a.out / "fmv.log")])
    if a.cmd in ("discover", "all"):
        discover(a.out, a.delay, a.headed, a.max_attempts, a.workers, a.epub, not a.no_verify)
    if a.cmd in ("download", "all"):
        download(a.out, a.workers, a.epub, not a.no_verify, a.max_attempts)
    if a.cmd == "status":
        status(a.out)


if __name__ == "__main__":
    main()
