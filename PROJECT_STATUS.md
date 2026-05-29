# JSW Coil OCR — PROJECT STATUS (living log)

_Last updated: 2026-05-29_

This file = the running record of what is built, what is live, and what is
pending. Read this first to know where things stand. (CLAUDE.md describes the
older CRNN/Eff-DB research pipeline — that is NOT what production runs now.)

---

## 1. What this app is (production reality)
- 10-digit coil ID reader for JSW workers. Phone photo → reads the ID → worker confirms.
- **Pure Gemini 2.5 Flash online pipeline.** CRNN/Eff-DB offline models are DROPPED in prod.
  - Code path: `api/app.py` → `pipeline/gemini_pipeline.py` → `modules/gemini_reader.py`.
- **LIVE at:** https://jsw-coil-ocr.onrender.com  (Render.com free tier, Docker, HTTPS)
  - Free tier sleeps after 15 min idle → first scan wakes it (~50s cold start).

## 2. Deploy workflow (how to ship a change)
1. Edit source in `C:\Users\sayal\Downloads\democoil\jsw_coil_ocr\`
2. `Copy-Item` the changed file to `C:\Users\sayal\Downloads\coil_deploy\` (same path)
3. From `coil_deploy`:  `git add .` → `git commit -m "..."` → `git push origin deploy:main`
4. Render auto-deploys from `main`. Check https://jsw-coil-ocr.onrender.com/health

**/health flags:** `mode: gemini_online` (≥1 key) · `drive_upload` · `sap_push`

## 3. API keys / quota
- `GEMINI_API_KEY_1` is set in Render and working. Keys 2 & 3 optional (3 separate
  Google projects = 3× the free 1500/day quota). Auto-rotates when one hits quota.

## 4. Photo storage (manager review) — ✅ LIVE
- Every confirmed scan uploads the photo to Google Drive folder **"JSW Coil Photos"**.
- How: app POSTs to a Google Apps Script web-app (`drive_apps_script.gs`).
- URL is baked into `api/app.py` → `_DRIVE_URL_DEFAULT` (Render env vars wouldn't persist).
  - Change/disable: edit that constant (`""` = off). Comment above it explains.
- Filename: `<coilID>_<YYYY-MM-DD>_<HHMM>_<manual|auto>.jpg`.
- Verified end-to-end 2026-05-29 (real scan landed in the folder).

## 5. SAP integration — 🔶 CODE READY, OFF (waiting on IT)
- Manager confirmed **SAP** (not MES). Technical details NOT received yet.
- Pre-built a configurable push: `_notify_sap()` in `api/app.py`, fired on `/confirm`.
- **To turn on:** fill the `SAP_*` config block at top of `api/app.py`:
  - `SAP_ENDPOINT`, `SAP_AUTH_MODE` (none/basic/bearer), `SAP_FIELD_COILID`,
    `SAP_EXTRA_FIELDS`, `SAP_ODATA_CSRF`.
  - Credentials via env: `SAP_USER`/`SAP_PASS`/`SAP_TOKEN` (do NOT hardcode in public repo).
- Handles both plain REST/middleware and SAP Gateway/OData (CSRF token). Logic unit-tested.
- **Ask IT for:** REST/OData endpoint, login method, which SAP field the coil ID maps to,
  a QAS/test system, and **a READ endpoint** (needed for the full label — see §7).

## 6. Worker-flow safety features — audited 2026-05-29
| # | Feature | Status |
|---|---------|--------|
| 1 | Strap covering digits → force manual entry | ✅ (prompt now covers black/grey/silver strap) |
| 2 | Duplicate ID → ask "scan anyway?" (yes/no) | ✅ (explicit confirm dialog before re-scan) |
| 3 | Blurry/noisy image → "Retake", block confirm | ✅ (Gemini `IMAGE_UNCLEAR`) |
| 4 | Manual entry ALWAYS available, even on good read | ✅ (manual box now shows on success as override) |

## 7. Printed label — ✅ TEMPLATE + BARCODE LIVE (fields fill when SAP read is wired)
- Full JSW-style label now prints (2 or 3 copies): header (JSW / Made in India /
  Dolvi address), product title, field grid, Code128 barcode + number, mill footer.
  Buttons after confirm: "Print 2 Labels" / "Print 3 Labels".
- Barcode: `GET /barcode/{value}` → Code128 SVG (python-barcode). Offline / same-origin.
- Fields (customer, grade, destination...) come from SAP via `GET /label_data`
  → `_fetch_sap_fields()`. OFF until `SAP_READ_ENDPOINT` + `SAP_READ_FIELD_MAP`
  are set (in the SAP_* block). Until then the label prints the coil ID + barcode + blanks.
- Verified 2026-05-29: endpoints return SVG + JSON; label rendered + screenshotted, matches the photo.

## 8. Pending / next
- [x] Worker-flow safety fixes (§6) — deployed
- [x] Label template + barcode + SAP-read stub — deployed (fields blank till SAP read)
- [ ] Get SAP details from IT (WRITE endpoint for §5 push + READ endpoint for §7 label)
      → fill the SAP_* block in api/app.py, redeploy
- [ ] (optional) Add `GEMINI_API_KEY_2` / `_3` for more free quota

---
_Update this file whenever something ships or status changes._
