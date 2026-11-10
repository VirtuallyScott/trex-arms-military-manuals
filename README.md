# T.REX ARMS Field Manual Vault mirror: handoff

Goal: download every PDF (optionally EPUB) from https://www.trex-arms.com/field-manual-vault. It must be resumable, must never download a file twice and must survive interruption at any point.

A working draft is in `fmv_grab.py`. I tested the download phase end to end (fresh download, resume from a partial `.part`, MD5 check, rerun skip, re-fetch of a deleted file). I could **not** test the discovery phase: www.trex-arms.com returns 403 to non-browser clients from my sandbox. Run discovery on your machine first and fix anything below that turns out to be wrong.

## Setup

Requires Python 3.9+ and Google Chrome (discovery drives real Chrome via Playwright `channel="chrome"`).

`setup.sh` creates `.venv`, installs `requirements.txt` (uses `uv` if installed, else `venv` + `pip`), and checks for Chrome, installing it via `playwright install chrome` if missing. Safe to rerun.

```bash
# Option A: run it, then activate yourself
./setup.sh
source .venv/bin/activate

# Option B: source it to set up and activate in one step (bash or zsh)
source ./setup.sh
```

Options:

| Flag | Effect |
|---|---|
| `-p, --python PATH` | Build the venv from a specific interpreter (default `python3`; env `PYTHON`) |
| `-r, --recreate` | Delete `.venv` and rebuild from scratch |
| `--no-browser` | Skip the Chrome check/install |
| `--no-uv` | Force `python -m venv` + `pip` even if `uv` exists |
| `-h, --help` | Show help |

Set `VENV_DIR` to put the venv somewhere other than `./.venv`.

Common commands once the venv is active:

```bash
python fmv_grab.py discover --out ./fmv --headed   # build manifest.db (watch the first pages)
python fmv_grab.py download --out ./fmv            # fetch PDFs; Ctrl-C and rerun to resume
python fmv_grab.py download --out ./fmv --epub     # also fetch EPUBs
python fmv_grab.py status   --out ./fmv            # progress / failures
python fmv_grab.py all      --out ./fmv            # discover + download
deactivate                                         # leave the venv
```

Without activating: `.venv/bin/python fmv_grab.py status --out ./fmv`.

Dependencies changed? Edit `requirements.txt` and rerun `./setup.sh`. Broken venv: `./setup.sh --recreate`.

## What recon found (2026-11-09)

### Site
- SvelteKit app behind Cloudflare. `www` returned 403 to plain curl. That is why discovery uses Playwright with a real Chromium. Do not spoof user agents or try to get around Cloudflare. If headless gets challenged, run `--headed` and slow down.
- `robots.txt` disallows `/api/`, `/api2/` and `/*__data.json`. The listing and detail HTML pages are allowed. The script only uses HTML pages. Keep it that way.
- Front page claims 2,192 manuals, 4,215 publications and 361,297 pages. The difference is editions: the listing collapses older editions of the same designation under one card.

### Listing pages
- The unfiltered `/field-manual-vault` shows a "Recommended" landing, not the full list. You need a filter to get a paged list.
- Paging: `?<filter>&page=N`, 30 results per page. A page past the end has zero results (verified: historic p41 has 1, p999 has 0).
- Totals per filter (editions collapsed): `coll=current` 726, `coll=recent-archive` 1637, `coll=lessons-learned` 70, `coll=historic` 1201, `branch=army` 2898, `branch=usmc` 332, `branch=navy` 152, `branch=joint` 132, `branch=af` 49. Neither set sums to 4,215, so the listings alone are not enough.
- Each result card links to `/field-manual-vault/<slug>`, e.g. `tc-3-21-76-2025-09`. `faq` and `citations` are not manuals.

### Detail pages
- `/field-manual-vault/<slug>`. The download buttons are plain links in the server rendered HTML:
  - `https://files.trex-arms.com/pdf/army/tc/tc_3-21-76_2025-09.pdf`
  - `https://files.trex-arms.com/epub/army/tc/tc_3-21-76_2025-09.epub`
- The PDF path **cannot** be derived from the slug or id. Examples: `pdf/army/legacy/fm/fm_100-10_1949-01.pdf` (has a `legacy/` segment), `pdf/army/fm/fm_7-22_2020-10_C1.pdf` (has a change suffix). Always read it from the detail page.
- The page embeds a SvelteKit payload with `slug:"..."` entries for every edition plus references and cited-by. Example: `fm-100-10-1976-04` lists 1995, 1988, 1973, 1954, 1949, 1943 and 1942 editions; two of those were not in the historic listing's editions. Following these slugs (BFS until no new slugs) is how we reach all 4,215.

### File host (files.trex-arms.com)
- Cloudflare R2 or similar. No auth. Plain GET works.
- `Accept-Ranges: bytes` (206 confirmed), `Content-Length`, `Last-Modified`.
- `ETag` is the MD5 of the file (verified on 2 files). Multipart uploads would show `"<hex>-N"`; the script only checks MD5 when the ETag is a plain 32 hex digest.
- `Content-Disposition` carries a friendly name like `TC 3-21.76 Ranger Handbook (2025).pdf`. We save by URL path instead so names are unique and stable.

## Design in `fmv_grab.py`

```
python fmv_grab.py discover --out ./fmv     # Playwright, builds manifest.db
python fmv_grab.py download --out ./fmv     # requests, 4 workers
python fmv_grab.py download --out ./fmv --epub
python fmv_grab.py status   --out ./fmv
```

State lives in `<out>/manifest.db` (SQLite, WAL):

| table | key | purpose |
|---|---|---|
| `seeds` | (seed, page) | listing pages already walked; a row with n=0 marks a seed complete |
| `manuals` | slug | BFS queue; state new/done/failed, pdf_url, epub_url, title |
| `files` | url | download queue; path, size, etag, state, attempts, error |

Dedup is structural:
- slugs dedup in `manuals` (PK), so overlapping listings cost nothing
- URLs dedup in `files` (PK), so two slugs pointing at the same PDF download once
- the on-disk path mirrors the URL path, so a URL maps to exactly one file

Resume rules:
- discovery skips listing pages already in `seeds` and detail pages already `done`; failed ones retry up to `--max-attempts`
- downloads write to `<name>.part`, resume with `Range` plus `If-Range: "<etag>"` (if the file changed upstream the server sends a full 200 and we overwrite), check size, check MD5, then atomic rename. A file with its final name is always complete.
- existing final files are skipped when size matches `Content-Length` and MD5 matches ETag
- rows marked done whose file was deleted are re-queued

Politeness: 1 s between page loads in discovery, 4 download workers, urllib3 retry with backoff on 429/5xx honoring `Retry-After`, honest User-Agent on the file host.

## Tasks for the IDE

1. Run `source ./setup.sh` (see [Setup](#setup); needs Google Chrome; on the SAP machine check that Hyperspace or the corporate proxy does not interfere with downloads from files.trex-arms.com).
2. Run `discover` with `--headed` for the first few pages. Confirm:
   - listing pages return 30 slugs and end on an empty page
   - `page.content()` still contains the `slug:"..."` payload after hydration (it should; it lives in an inline script)
   - each detail page yields exactly one PDF link. The script warns if it sees more than one. If references render their own download links, narrow `RE_PDF` to the `a.download` element via `page.locator("a.download[href$='.pdf']")`.
3. Let discovery finish. Expect roughly 4,215 `manuals` rows in `done`. Investigate any `failed` with `status`.
4. Check disk before downloading. Sizes are not known until each file is HEADed, and 361k pages of scanned PDF is likely tens of GB. Add a `--sizes` mode that only HEADs every file and sums `Content-Length`.
5. Run `download`. Ctrl-C at any time; rerun the same command to resume.
6. Nice to have:
   - `--sizes` HEAD-only mode (above)
   - export `manifest.csv` (slug, title, designation, year, url, path) for a browsable index
   - optional symlink tree by friendly name from `Content-Disposition`
   - `--limit N` for smoke tests

## Known unknowns
- Whether headless Chromium passes Cloudflare from a residential IP without a challenge. If not, `--headed` and a larger `--delay`.
- Whether BFS over `slug:"..."` reaches all 4,215. If the count falls short, add more seeds: `type=` (FM, TM, ATP, MCRP, TC, JP, NAVEDTRA, MD), `fn=` and `ech=` values from the sidebar.
- Terms of use: the manuals are public domain, but T.REX did the scanning and hosting. Keep the rate low.
