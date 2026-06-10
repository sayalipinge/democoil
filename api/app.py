"""
JSW Coil OCR — Web Application

Run:   python api/app.py
Open:  http://localhost:8000

Features:
    1. Camera capture + file upload
    2. Client-side image compression (saves bandwidth on weak WiFi)
    3. Gemini Vision OCR (10-digit coil ID)
    4. Key rotation (up to 3 free API keys)
    5. Quota exhaustion → clear manual entry prompt
    6. Black strap detection → manual entry
    7. Duplicate ID warning
    8. Sticker print (2 or 3 copies)
    9. Scan history (last 2 days, auto-refreshes every 30s)
   10. MES/SAP webhook (set MES_WEBHOOK_URL in .env to enable)

Environment variables (.env file):
    GEMINI_API_KEY_1   — required
    GEMINI_API_KEY_2   — optional backup key
    GEMINI_API_KEY_3   — optional backup key
    MES_WEBHOOK_URL    — optional, POST coil_id to MES/SAP on confirm
    DRIVE_UPLOAD_URL   — optional, Google Apps Script URL that saves each coil
                         photo to Google Drive (for manager review). Unset = off.
    SAP_USER/SAP_PASS  — optional, SAP login for the SAP push (see SAP_* block).
    SAP_TOKEN          — optional, SAP bearer/OAuth token (alternative to user/pass).
    PORT               — optional, default 8000
"""
import sys
import json
import os
import base64
import cv2
import numpy as np
import httpx
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, File, UploadFile, HTTPException, Form, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from pipeline.gemini_pipeline import predict, register_coil
from modules.inventory import InventoryManager
from modules.worker_accounts import WorkerAccountManager

ROOT = Path(__file__).resolve().parent.parent

# DATA_DIR: set via env var on cloud (e.g. /data for Fly.io volume)
# Defaults to project's data/ folder for local use
DATA_DIR   = Path(os.environ.get("DATA_DIR", str(ROOT / "data")))
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
worker_accounts = WorkerAccountManager()


def _require_worker(token: str) -> dict:
    worker = worker_accounts.authenticate(token)
    if not worker:
        raise HTTPException(401, "Login required")
    return worker


def _pattern_for_yard(yard: str) -> str:
    return "HSM2" if yard == "HSM Yard" else "CSP"


def _coil_matches_yard(coil_id: str, yard: str) -> bool:
    year = datetime.now().strftime("%y")
    if yard == "HSM Yard":
        return coil_id.startswith(f"02{year}")
    return coil_id.startswith(year) and coil_id[3] == "0"


def _require_manager(pin: str):
    if pin != os.environ.get("MANAGER_PIN", "1234").strip():
        raise HTTPException(401, "Manager PIN required")


def _scan_image_url(image_path: str) -> str:
    if not image_path:
        return ""
    name = Path(image_path).name
    return f"/scan_image/{name}" if name else ""

# ── Image save settings ──────────────────────────────────────────────────────
# Save compressed image (not full 4MB raw). Same compression as Gemini gets.
SAVE_MAX_DIM  = 768   # max pixel dimension when saving to disk
SAVE_QUALITY  = 70    # JPEG quality for saved images

# ── MES/SAP webhook (optional) ───────────────────────────────────────────────
MES_WEBHOOK_URL = os.environ.get("MES_WEBHOOK_URL", "").strip()

# ── Google Drive photo upload ────────────────────────────────────────────────
# Each confirmed coil photo is sent to a Google Apps Script web-app, which saves
# it into a Drive folder ("JSW Coil Photos") on YOUR Google account. The manager
# opens that folder to check whether workers photograph coils properly.
#
# The /exec URL is baked in below (Render env vars wouldn't stick, so we hardcode).
#   CHANGE the script : replace the URL in _DRIVE_URL_DEFAULT below.
#   DISABLE uploads   : set _DRIVE_URL_DEFAULT = "" (empty string).
#   OVERRIDE via env  : if DRIVE_UPLOAD_URL is set in Render, it wins over the default.
# See drive_apps_script.gs for the script itself + setup steps.
_DRIVE_URL_DEFAULT = "https://script.google.com/macros/s/AKfycbxjPKvOg9AVMEvpDjZee-NXlYKdgfg_aG5f5izujiQee1L2QDpNQ7tNPCnpMKtDyVL2/exec"
DRIVE_UPLOAD_URL = os.environ.get("DRIVE_UPLOAD_URL", "").strip() or _DRIVE_URL_DEFAULT

# ── SAP integration (push each confirmed coil ID into SAP) ─────────────────────
# OFF until you fill SAP_ENDPOINT below. The whole app works fine without it.
#
# WHEN SAP/IT GIVES YOU THE DETAILS, edit ONLY the values in this block —
# nothing else in the file needs to change. Map their answers like this:
#   "the URL to send to"              -> SAP_ENDPOINT
#   "how it logs in"                  -> SAP_AUTH_MODE  (+ user/pass or token)
#   "which field the coil ID goes in" -> SAP_FIELD_COILID
#   "is it an OData/Gateway service?" -> SAP_ODATA_CSRF
#
# --- 1. endpoint ----------------------------------------------------------------
SAP_ENDPOINT = ""          # e.g. "https://sapgw.jsw.in/sap/opu/odata/sap/ZCOIL_SRV/CoilSet"
                           # leave "" to keep SAP push DISABLED.
# --- 2. login / auth ------------------------------------------------------------
SAP_AUTH_MODE = "basic"    # "none" | "basic" | "bearer"
#   SECURITY: do NOT paste a real SAP password here while the GitHub repo is
#   public. Set SAP_USER / SAP_PASS / SAP_TOKEN as Render env vars (code reads
#   env first). If env vars won't stick, make the repo PRIVATE before hardcoding.
SAP_USER  = os.environ.get("SAP_USER",  "").strip()   # used when mode = "basic"
SAP_PASS  = os.environ.get("SAP_PASS",  "").strip()   # used when mode = "basic"
SAP_TOKEN = os.environ.get("SAP_TOKEN", "").strip()   # used when mode = "bearer"
# --- 3. what to send ------------------------------------------------------------
SAP_FIELD_COILID    = "CoilId"     # SAP's field name for the 10-digit ID.
                                   # ask IT — could be "Charg"(batch) / "Matnr"(material) / a Z-field.
SAP_FIELD_TIMESTAMP = "Timestamp"  # SAP's field for the scan time ("" = don't send time).
SAP_EXTRA_FIELDS    = {}           # constant fields SAP needs, e.g. {"Werks": "1234", "Line": "HSM2"}.
# --- 4. OData quirk -------------------------------------------------------------
SAP_ODATA_CSRF = True      # True  -> SAP Gateway / OData (fetches an X-CSRF-Token first; required for writes).
                           # False -> plain REST endpoint or PI-PO / CPI middleware.

# --- 5. READ-BACK (fills the printed label) ------------------------------------
# To print a FULL JSW label (customer, grade, destination, weight...) the app must
# look those fields up from SAP by coil ID. Ask IT for a READ (GET) endpoint.
#   SAP_READ_ENDPOINT : GET URL with a {coil_id} placeholder, e.g.
#     "https://sapgw.jsw.in/sap/opu/odata/sap/ZCOIL_SRV/CoilSet('{coil_id}')?$format=json"
#     leave "" -> label prints the coil ID + blank fields (still usable).
#   SAP_READ_FIELD_MAP: map each label field (left) to SAP's JSON key (right).
#     Uncomment + set the ones your SAP returns; the rest stay blank.
SAP_READ_ENDPOINT  = ""
SAP_READ_FIELD_MAP = {
    # label_field      : "SAP_JSON_KEY",   (uncomment + set the ones SAP returns)
    # "product"        : "ProductType",      # title, e.g. "HOT Rolled Coil"
    # "cert_std"       : "CertifiedToStd",   # "Certified to Std", e.g. MS1768_2004
    # "cert_no"        : "CertificationNo",  # "Certification No",  e.g. PC 012776
    # "grade"          : "Grade",
    # "size"           : "Size",
    # "heat_no"        : "HeatNo",
    # "net_weight"     : "NetWeight",
    # "quality"        : "Quality",
    # "customer"       : "CustomerName",
    # "so_no"          : "SoNumber",
    # "destination"    : "Destination",
    # "batch_no"       : "BatchNumber",
    # "delivery_cond"  : "DeliveryCondition",
    # "insp_date"      : "InspDate",
    # "shipping_mark"  : "ShippingMark",
    # "inspected_by"   : "InspectedBy",
}

app = FastAPI(title="JSW Coil OCR", version="3.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


def _save_compressed(image_bgr: np.ndarray, path: Path):
    """Save image compressed to disk (same size as what Gemini received)."""
    h, w = image_bgr.shape[:2]
    if max(h, w) > SAVE_MAX_DIM:
        scale = SAVE_MAX_DIM / max(h, w)
        image_bgr = cv2.resize(image_bgr, (int(w * scale), int(h * scale)),
                               interpolation=cv2.INTER_AREA)
    cv2.imwrite(str(path), image_bgr, [cv2.IMWRITE_JPEG_QUALITY, SAVE_QUALITY])


async def _notify_mes(coil_id: str, timestamp: str, image_path: str, worker: dict):
    """
    POST coil data to MES/SAP webhook after worker confirms.
    Set MES_WEBHOOK_URL in .env to enable. Does nothing if not set.
    Timeout 5s — won't block the confirm response.
    """
    if not MES_WEBHOOK_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(MES_WEBHOOK_URL, json={
                "coil_id":    coil_id,
                "timestamp":  timestamp,
                "image_path": image_path,
                "worker_id":  worker["worker_id"],
                "worker_name": worker["full_name"],
                "shift":      worker["shift"],
                "yard":       worker["yard"],
                "source":     "jsw_coil_ocr",
            })
    except Exception as e:
        print(f"[mes_webhook] Failed to notify MES: {e}")


async def _send_to_drive(
    coil_id: str,
    image_path: str,
    worker_verified: bool,
    worker: dict,
):
    """
    Upload one confirmed coil photo to Google Drive (via Apps Script web-app).

    Runs as a FastAPI background task, so it NEVER delays the worker's confirm.
    Silently does nothing if DRIVE_UPLOAD_URL is not set or the image is missing.
    The saved file name carries the info the manager needs:
        <coil_id>_<YYYY-MM-DD>_<HHMM>_<auto|manual>.jpg
    e.g. 0226031432_2026-05-29_1253_auto.jpg
    """
    if not DRIVE_UPLOAD_URL:
        return
    try:
        # image_path already points to a compressed ~80KB JPEG (see _save_compressed)
        img_bytes = Path(image_path).read_bytes()
    except Exception as e:
        print(f"[drive] cannot read image {image_path}: {e}")
        return

    tag      = "manual" if worker_verified else "auto"
    stamp    = datetime.now().strftime("%Y-%m-%d_%H%M")
    worker_id = worker["worker_id"].replace(" ", "_")
    yard = worker["yard"].replace(" Yard", "").upper()
    shift = worker["shift"].replace(" Shift", "").replace(" ", "_")
    filename = f"{coil_id}_{stamp}_{worker_id}_{yard}_{shift}_{tag}.jpg"

    payload = {
        "filename":  filename,
        "image_b64": base64.b64encode(img_bytes).decode(),
        "coil_id": coil_id,
        "worker_id": worker["worker_id"],
        "worker_name": worker["full_name"],
        "shift": worker["shift"],
        "yard": worker["yard"],
        "worker_verified": worker_verified,
    }
    try:
        # follow_redirects=True is REQUIRED: Apps Script replies with a 302 to
        # googleusercontent.com that holds the actual {"ok":true} response body.
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            r = await client.post(DRIVE_UPLOAD_URL, json=payload)
            if r.status_code != 200 or '"ok"' not in r.text:
                print(f"[drive] upload may have failed: {r.status_code} {r.text[:150]}")
    except Exception as e:
        print(f"[drive] upload failed: {e}")


async def _notify_sap(coil_id: str, timestamp: str):
    """
    Push one confirmed coil ID into SAP. Runs as a background task, so it NEVER
    delays the worker's confirm. Does nothing until SAP_ENDPOINT is set.

    Handles the two common SAP setups (toggle with SAP_ODATA_CSRF):
      • plain REST / middleware (PI-PO, CPI)  -> SAP_ODATA_CSRF = False
      • SAP Gateway / OData service           -> SAP_ODATA_CSRF = True
        (OData rejects writes without an X-CSRF-Token, so we fetch one first.)

    To change WHAT is sent or HOW it logs in, edit the SAP_* config block at the
    top of this file — not this function. Debug via Render logs: look for [sap].
    """
    if not SAP_ENDPOINT:
        return

    # Build the JSON body SAP expects, straight from your field-name config.
    payload = {SAP_FIELD_COILID: coil_id}
    if SAP_FIELD_TIMESTAMP:
        payload[SAP_FIELD_TIMESTAMP] = timestamp
    payload.update(SAP_EXTRA_FIELDS)

    # Auth, per SAP_AUTH_MODE.
    auth    = None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if SAP_AUTH_MODE == "basic":
        auth = httpx.BasicAuth(SAP_USER, SAP_PASS)
    elif SAP_AUTH_MODE == "bearer":
        headers["Authorization"] = f"Bearer {SAP_TOKEN}"

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, auth=auth) as client:
            # OData/Gateway needs a CSRF token + session cookie before any write.
            # One GET with "X-CSRF-Token: Fetch" returns the token; the same client
            # keeps the cookie, so the POST below is accepted.
            if SAP_ODATA_CSRF:
                tok  = await client.get(SAP_ENDPOINT, headers={**headers, "X-CSRF-Token": "Fetch"})
                csrf = tok.headers.get("x-csrf-token", "")
                if csrf:
                    headers["X-CSRF-Token"] = csrf
                else:
                    print("[sap] warning: no X-CSRF-Token returned; posting without it")
            r = await client.post(SAP_ENDPOINT, json=payload, headers=headers)
            if r.status_code in (200, 201, 202):
                print(f"[sap] pushed coil {coil_id} -> {r.status_code}")
            else:
                print(f"[sap] push failed: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"[sap] push error: {e}")


async def _fetch_sap_fields(coil_id: str) -> dict:
    """
    Read extra label fields (customer, grade, destination, ...) from SAP by coil ID.
    Returns {} until SAP_READ_ENDPOINT is configured — the label then prints the
    coil ID with blank fields, which is still usable.

    Edit SAP_READ_ENDPOINT + SAP_READ_FIELD_MAP in the SAP_* config block to enable.
    Debug via Render logs: look for [sap].
    """
    if not SAP_READ_ENDPOINT:
        return {}
    url     = SAP_READ_ENDPOINT.replace("{coil_id}", coil_id)
    auth    = None
    headers = {"Accept": "application/json"}
    if SAP_AUTH_MODE == "basic":
        auth = httpx.BasicAuth(SAP_USER, SAP_PASS)
    elif SAP_AUTH_MODE == "bearer":
        headers["Authorization"] = f"Bearer {SAP_TOKEN}"
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, auth=auth) as client:
            r = await client.get(url, headers=headers)
            if r.status_code != 200:
                print(f"[sap] read failed: {r.status_code} {r.text[:150]}")
                return {}
            data = r.json()
            # OData usually nests the record under d / d.results — dig in if present.
            if isinstance(data, dict):
                if isinstance(data.get("d"), dict):
                    data = data["d"]
                if isinstance(data.get("results"), list) and data["results"]:
                    data = data["results"][0]
            out = {}
            if isinstance(data, dict):
                for label_field, sap_key in SAP_READ_FIELD_MAP.items():
                    if sap_key in data and data[sap_key] not in (None, ""):
                        out[label_field] = data[sap_key]
            return out
    except Exception as e:
        print(f"[sap] read error: {e}")
        return {}


# ── PWA Manifest ─────────────────────────────────────────────────────────────
@app.get("/manifest.json")
def manifest():
    """PWA manifest — makes the app installable from Chrome on Android."""
    return JSONResponse({
        "name":             "JSW Coil OCR",
        "short_name":       "CoilOCR",
        "description":      "Scan steel coil IDs with phone camera",
        "id":               "/",
        "start_url":        "/",
        "scope":            "/",
        "display":          "standalone",
        "background_color": "#0f172a",
        "theme_color":      "#3b82f6",
        "orientation":      "portrait-primary",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    })


@app.get("/sw.js")
def service_worker():
    """Tiny service worker so Android Chrome treats the site as installable."""
    js = r"""
const CACHE_NAME = 'jsw-coil-scanner-v2';
const APP_SHELL = ['/', '/manifest.json', '/icon-192.png', '/icon-512.png'];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(APP_SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.map(key => key === CACHE_NAME ? null : caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);

  if (request.method !== 'GET' || url.origin !== self.location.origin) {
    return;
  }

  if (url.pathname === '/' || url.pathname === '/manifest.json' || url.pathname.startsWith('/icon-')) {
    event.respondWith(
      fetch(request)
        .then(response => {
          const copy = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(request, copy));
          return response;
        })
        .catch(() => caches.match(request))
    );
  }
});
"""
    return Response(
        content=js,
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/icon-{size}.png")
def app_icon(size: int):
    """
    Auto-generated app icon. Blue background + white coil symbol.
    No external files needed — built with OpenCV.
    """
    if size not in (192, 512):
        size = 192

    # Blue background
    img = np.zeros((size, size, 3), dtype=np.uint8)
    img[:] = (59, 130, 246)   # BGR = Tailwind blue-500

    # White circle outline (coil symbol)
    cx, cy = size // 2, size // 2
    r_out  = int(size * 0.38)
    r_in   = int(size * 0.18)
    thick  = max(3, int(size * 0.06))
    cv2.circle(img, (cx, cy), r_out, (255, 255, 255), thick)
    cv2.circle(img, (cx, cy), r_in,  (255, 255, 255), thick)

    # "JSW" text
    font       = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = size / 320.0
    text       = "JSW"
    (tw, th), _ = cv2.getTextSize(text, font, font_scale, 2)
    cv2.putText(img, text,
                (cx - tw // 2, cy + th // 2),
                font, font_scale, (255, 255, 255), 2, cv2.LINE_AA)

    _, buf = cv2.imencode(".png", img)
    return Response(content=buf.tobytes(), media_type="image/png")


# ── Health check ─────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    has_key = bool(
        any(os.environ.get(f"GEMINI_API_KEY_{i}", "") for i in range(1, 4))
        or os.environ.get("GEMINI_API_KEY", "")
        or os.environ.get("GOOGLE_API_KEY", "")
    )
    return {
        "status":  "ok",
        "version": "3.0.0",
        "mode":    "gemini_online" if has_key else "no_api_key",
        "mes_webhook":  bool(MES_WEBHOOK_URL),
        "drive_upload": bool(DRIVE_UPLOAD_URL),
        "sap_push":     bool(SAP_ENDPOINT),
        "sap_read":     bool(SAP_READ_ENDPOINT),
    }


# ── Barcode (Code128 SVG) for the printed label ──────────────────────────────
@app.get("/barcode/{value}")
def barcode_svg(value: str):
    """Code128 barcode of `value` as crisp SVG (used on the printed coil label)."""
    import io
    import barcode
    from barcode.writer import SVGWriter
    safe = "".join(ch for ch in value if ch.isalnum())[:24] or "0"
    buf  = io.BytesIO()
    barcode.get("code128", safe, writer=SVGWriter()).write(
        buf, options={"write_text": False, "module_height": 12.0,
                      "module_width": 0.3, "quiet_zone": 1.0})
    return Response(content=buf.getvalue(), media_type="image/svg+xml")


# ── QR code (SVG) for the printed label ──────────────────────────────────────
@app.get("/qrcode/{value}")
def qrcode_svg(value: str):
    """QR code of `value` as SVG (top-right of the printed coil label)."""
    import io
    import segno
    safe = "".join(ch for ch in value if ch.isalnum())[:64] or "0"
    buf  = io.BytesIO()
    segno.make(safe, error="m").save(buf, kind="svg", scale=2, border=0)
    return Response(content=buf.getvalue(), media_type="image/svg+xml")


# ── Label data (coil ID + SAP fields, if read is configured) ─────────────────
@app.get("/label_data")
async def label_data(coil_id: str):
    """Fields for the printed label. coil_id always present; rest come from SAP read."""
    ok     = len(coil_id) == 10 and coil_id.isdigit()
    fields = await _fetch_sap_fields(coil_id) if ok else {}
    return {"coil_id": coil_id, "fields": fields, "sap_read": bool(SAP_READ_ENDPOINT)}


# ── Predict endpoint ─────────────────────────────────────────────────────────
@app.post("/worker/register")
def worker_register(
    worker_id: str = Form(...),
    full_name: str = Form(...),
    pin: str = Form(...),
    shift: str = Form(...),
    yard: str = Form(...),
):
    try:
        return worker_accounts.register(worker_id, full_name, pin, shift, yard)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/worker/login")
def worker_login(worker_id: str = Form(...), pin: str = Form(...)):
    try:
        return worker_accounts.login(worker_id, pin)
    except ValueError as e:
        raise HTTPException(401, str(e))


@app.post("/worker/session")
def worker_session(session_token: str = Form(...)):
    return {"worker": _require_worker(session_token)}


@app.post("/worker/context")
def worker_context(
    session_token: str = Form(...),
    shift: str = Form(...),
    yard: str = Form(...),
):
    _require_worker(session_token)
    try:
        return {"worker": worker_accounts.update_context(session_token, shift, yard)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/predict")
async def predict_endpoint(
    file: UploadFile = File(...),
    session_token: str = Form(...),
):
    worker = _require_worker(session_token)
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "File must be an image")

    contents = await file.read()
    nparr    = np.frombuffer(contents, np.uint8)
    image_bgr = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

    if image_bgr is None:
        raise HTTPException(400, "Could not decode image")

    # Save compressed image (not full resolution)
    ts        = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    save_path = UPLOAD_DIR / f"scan_{ts}_{worker['worker_id']}.jpg"
    _save_compressed(image_bgr, save_path)

    # Run prediction
    result = predict(
        image_bgr,
        pattern_hint=_pattern_for_yard(worker["yard"]),
        check_inventory=True,
        image_path=str(save_path),
    )
    result["image_saved"] = str(save_path)
    result["yard"] = worker["yard"]
    return result


# ── Confirm endpoint ─────────────────────────────────────────────────────────
@app.post("/confirm")
async def confirm_endpoint(
    background_tasks: BackgroundTasks,
    coil_id:         str  = Form(...),
    image_path:      str  = Form(""),
    worker_verified: bool = Form(False),
    session_token:   str  = Form(...),
):
    worker = _require_worker(session_token)
    if len(coil_id) != 10 or not coil_id.isdigit():
        raise HTTPException(400, "Coil ID must be exactly 10 digits")
    if not _coil_matches_yard(coil_id, worker["yard"]):
        year = datetime.now().strftime("%y")
        expected = (
            f"02{year} + 6 digits"
            if worker["yard"] == "HSM Yard"
            else f"{year} + shell digit + 0 + 6 digits"
        )
        raise HTTPException(
            400, f"Coil ID does not match {worker['yard']}: expected {expected}"
        )

    result    = register_coil(coil_id, image_path, worker_verified, worker)
    timestamp = datetime.now().isoformat()

    # Notify MES/SAP (fire and forget — won't delay response)
    await _notify_mes(coil_id, timestamp, image_path, worker)

    # Save the photo to Google Drive for manager review.
    # Background task = runs AFTER the response is sent, so confirm stays instant.
    background_tasks.add_task(
        _send_to_drive, coil_id, image_path, worker_verified, worker
    )

    # Push the coil ID into SAP (also background — see the SAP_* config block).
    # No-op until SAP_ENDPOINT is filled, so this is safe to ship now.
    background_tasks.add_task(_notify_sap, coil_id, timestamp)

    return {**result, "worker": worker}


# ── History endpoint ─────────────────────────────────────────────────────────
@app.get("/history")
def history_endpoint(session_token: str):
    worker = _require_worker(session_token)
    inv     = InventoryManager()
    all_c   = inv.get_all()
    cutoff  = datetime.now() - timedelta(days=2)
    recent  = []

    for coil_id, data in all_c.items():
        for scan in data["scans"]:
            try:
                scan_time = datetime.fromisoformat(scan["timestamp"])
                if scan_time >= cutoff:
                    if scan.get("worker_id") != worker["worker_id"]:
                        continue
                    recent.append({
                        "coil_id":         coil_id,
                        "timestamp":       scan["timestamp"],
                        "image_path":      scan.get("image_path", ""),
                        "image_url":       _scan_image_url(scan.get("image_path", "")),
                        "worker_verified": scan.get("worker_verified", False),
                        "worker_id":       scan.get("worker_id", ""),
                        "worker_name":     scan.get("worker_name", ""),
                        "shift":           scan.get("shift", ""),
                        "yard":            scan.get("yard", ""),
                    })
            except (ValueError, KeyError):
                pass

    recent.sort(key=lambda x: x["timestamp"], reverse=True)
    return {"scans": recent, "total": len(recent)}


# ── Inventory endpoint ────────────────────────────────────────────────────────
@app.get("/scan_image/{filename}")
def scan_image(filename: str):
    safe_name = Path(filename).name
    path = UPLOAD_DIR / safe_name
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Photo not found on server")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/manager_scans")
def manager_scans(pin: str):
    _require_manager(pin)
    inv = InventoryManager()
    scans = []
    for coil_id, data in inv.get_all().items():
        for scan in data.get("scans", []):
            image_url = _scan_image_url(scan.get("image_path", ""))
            image_exists = bool(image_url and (UPLOAD_DIR / Path(image_url).name).exists())
            scans.append({
                "coil_id": coil_id,
                "timestamp": scan.get("timestamp", ""),
                "image_url": image_url if image_exists else "",
                "image_exists": image_exists,
                "worker_verified": scan.get("worker_verified", False),
                "worker_id": scan.get("worker_id", ""),
                "worker_name": scan.get("worker_name", ""),
                "shift": scan.get("shift", ""),
                "yard": scan.get("yard", ""),
            })
    scans.sort(key=lambda x: x["timestamp"], reverse=True)
    return {"scans": scans[:300], "total": len(scans)}


@app.get("/manager", response_class=HTMLResponse)
def manager_page():
    return r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JSW Scan Review</title>
<style>
body{margin:0;background:#0f172a;color:#e2e8f0;font-family:Arial,sans-serif}
.top{position:sticky;top:0;background:#1e293b;border-bottom:2px solid #3b82f6;padding:14px 16px;z-index:1}
h1{font-size:20px;margin:0 0 10px;color:#60a5fa}.bar{display:flex;gap:8px}
input{flex:1;background:#111827;color:#fff;border:1px solid #475569;border-radius:6px;padding:12px;font-size:16px}
button{background:#2563eb;color:#fff;border:0;border-radius:6px;padding:12px 16px;font-weight:700}
.wrap{padding:14px;max-width:900px;margin:auto}.msg{color:#94a3b8;margin:16px 2px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}
.card{background:#1e293b;border:1px solid #334155;border-radius:8px;overflow:hidden}
.card img{width:100%;height:210px;object-fit:cover;background:#020617;display:block}
.meta{padding:10px}.coil{font:700 22px monospace;color:#4ade80;letter-spacing:1px}
.line{font-size:13px;color:#cbd5e1;margin-top:4px}.missing{height:210px;display:flex;align-items:center;justify-content:center;color:#fca5a5;background:#111827}
</style>
</head>
<body>
<div class="top">
  <h1>JSW Scan Review</h1>
  <div class="bar"><input id="pin" type="password" inputmode="numeric" placeholder="Manager PIN"><button onclick="loadScans()">Open</button></div>
</div>
<div class="wrap"><div class="msg" id="msg">Enter manager PIN to view confirmed scan photos.</div><div class="grid" id="grid"></div></div>
<script>
const saved = localStorage.getItem('jsw_manager_pin') || '';
document.getElementById('pin').value = saved;
async function loadScans(){
  const pin = document.getElementById('pin').value.trim();
  localStorage.setItem('jsw_manager_pin', pin);
  const msg = document.getElementById('msg');
  const grid = document.getElementById('grid');
  msg.textContent = 'Loading scans...';
  grid.innerHTML = '';
  try {
    const r = await fetch('/manager_scans?pin=' + encodeURIComponent(pin));
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'Could not load scans');
    msg.textContent = d.total ? `${d.total} scans found` : 'No confirmed scans found yet';
    d.scans.forEach(s => {
      const card = document.createElement('div');
      card.className = 'card';
      const photo = s.image_url ? `<a href="${s.image_url}" target="_blank"><img src="${s.image_url}"></a>` : '<div class="missing">Photo not on server</div>';
      card.innerHTML = photo + `<div class="meta"><div class="coil">${s.coil_id}</div>
        <div class="line">${s.worker_name || '-'} (${s.worker_id || '-'})</div>
        <div class="line">${s.shift || '-'} | ${s.yard || '-'}</div>
        <div class="line">${new Date(s.timestamp).toLocaleString()}</div>
        <div class="line">${s.worker_verified ? 'Manual correction' : 'AI confirmed'}</div></div>`;
      grid.appendChild(card);
    });
  } catch(e) {
    msg.textContent = e.message;
  }
}
if (saved) loadScans();
</script>
</body>
</html>"""


@app.get("/inventory")
def inventory_endpoint():
    inv = InventoryManager()
    return {"total_unique_coils": inv.get_count(), "coils": inv.get_all()}


# ── Web UI ────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def index():
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>JSW COIL-ID SCANNER</title>

<!-- PWA: makes app installable from Chrome on Android -->
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#3b82f6">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="CoilOCR">
<link rel="apple-touch-icon" href="/icon-192.png">
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #0f172a; color: #e2e8f0; min-height: 100vh; }
.header { background: #1e293b; padding: 12px 16px; text-align: center; border-bottom: 2px solid #3b82f6; }
.header h1 { font-size: 20px; color: #3b82f6; }
.header .mode { font-size: 12px; color: #94a3b8; margin-top: 4px; }
.container { max-width: 480px; margin: 0 auto; padding: 16px; }
.auth-screen { position: fixed; inset: 0; z-index: 100; background: #0f172a; overflow-y: auto; padding: 28px 16px; }
.auth-box { max-width: 420px; margin: 0 auto; }
.auth-title { color: #3b82f6; font-size: 24px; text-align: center; margin-bottom: 6px; }
.auth-subtitle { color: #94a3b8; text-align: center; margin-bottom: 22px; font-size: 14px; }
.auth-tabs { display: grid; grid-template-columns: 1fr 1fr; border-bottom: 1px solid #334155; margin-bottom: 18px; }
.auth-tab { border: 0; background: transparent; color: #94a3b8; padding: 12px; font-size: 15px; font-weight: 700; }
.auth-tab.active { color: #60a5fa; border-bottom: 3px solid #3b82f6; }
.auth-form { display: grid; gap: 12px; }
.auth-form label { color: #cbd5e1; font-size: 13px; }
.auth-form input, .auth-form select, .context-select { width: 100%; border: 1px solid #475569; background: #111827; color: #f8fafc; border-radius: 6px; padding: 13px; font-size: 16px; }
.auth-error { min-height: 20px; color: #fca5a5; font-size: 13px; }
.worker-bar { display: none; margin-bottom: 14px; border-bottom: 1px solid #334155; padding: 0 0 12px; }
.worker-top { display: flex; justify-content: space-between; align-items: center; gap: 10px; }
.worker-name { font-weight: 700; }
.worker-meta { color: #94a3b8; font-size: 12px; margin-top: 3px; }
.worker-actions { display: flex; gap: 8px; }
.btn-small { padding: 8px 10px; font-size: 13px; border-radius: 6px; border: 1px solid #475569; background: transparent; color: #cbd5e1; }
.context-panel { display: none; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 12px; }
.context-panel .btn-small { grid-column: 1 / -1; background: #2563eb; color: white; border: 0; }

/* Camera */
.camera-section { text-align: center; margin-bottom: 16px; }
#video { width: 100%; max-height: 300px; border-radius: 12px; border: 2px solid #334155; object-fit: cover; }
#canvas { display: none; }
.btn { display: inline-block; padding: 14px 28px; border: none; border-radius: 10px; font-size: 16px; font-weight: 600; cursor: pointer; transition: all 0.2s; }
.btn-capture { background: #3b82f6; color: white; width: 100%; margin-top: 12px; }
.btn-capture:hover { background: #2563eb; }
.btn-capture:disabled { background: #475569; cursor: not-allowed; }
.btn-upload { background: #475569; color: #e2e8f0; width: 100%; margin-top: 8px; font-size: 14px; }
#fileInput { display: none; }

/* Result card */
.result-card { background: #1e293b; border-radius: 12px; padding: 20px; margin-top: 16px; display: none; }
.result-status { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
.status-icon { width: 48px; height: 48px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 24px; flex-shrink: 0; }
.status-ok   { background: #065f46; }
.status-warn { background: #92400e; }
.status-error{ background: #991b1b; }
.result-id { font-size: 32px; font-weight: 700; letter-spacing: 4px; text-align: center; padding: 16px; background: #0f172a; border-radius: 8px; margin: 12px 0; font-family: 'Courier New', monospace; }
.result-id.success { color: #4ade80; border: 2px solid #4ade80; }
.result-id.warning { color: #fbbf24; border: 2px solid #fbbf24; }
.result-id.error   { color: #f87171; border: 2px solid #f87171; }

/* Warning boxes */
.warning-box  { background: #422006; border: 1px solid #f59e0b; border-radius: 8px; padding: 12px; margin: 8px 0; font-size: 14px; color: #fef3c7; }
.strap-warning{ background: #450a0a; border-color: #ef4444; color: #fecaca; }
.quota-warning{ background: #1e3a5f; border-color: #60a5fa; color: #bfdbfe; }

/* Manual entry */
.manual-entry { margin: 12px 0; }
.manual-entry label { font-size: 13px; color: #94a3b8; display: block; margin-bottom: 6px; }
.manual-entry input { width: 100%; padding: 14px; font-size: 24px; letter-spacing: 4px; text-align: center; background: #0f172a; border: 2px solid #475569; border-radius: 8px; color: #e2e8f0; font-family: monospace; }
.manual-entry input:focus { border-color: #3b82f6; outline: none; }

/* Action buttons */
.action-row { display: flex; gap: 8px; margin-top: 12px; }
.btn-confirm { background: #16a34a; color: white; flex: 1; }
.btn-confirm:hover { background: #15803d; }
.btn-retake  { background: #dc2626; color: white; flex: 1; }
.btn-retake:hover  { background: #b91c1c; }

/* Print */
.print-section { margin-top: 12px; display: none; }
.print-row { display: flex; gap: 8px; }
.btn-print { background: #7c3aed; color: white; flex: 1; padding: 12px; }
.btn-print:hover { background: #6d28d9; }

/* Loading */
.loading { display: none; text-align: center; padding: 20px; }
.spinner { width: 40px; height: 40px; border: 4px solid #334155; border-top-color: #3b82f6; border-radius: 50%; animation: spin 0.8s linear infinite; margin: 0 auto 12px; }
@keyframes spin { to { transform: rotate(360deg); } }
.loading-text { font-size: 14px; color: #94a3b8; }

/* History */
.history-section { margin-top: 24px; }
.history-section h3 { font-size: 16px; color: #94a3b8; margin-bottom: 8px; }
.history-item { background: #1e293b; padding: 10px 14px; border-radius: 8px; margin-bottom: 6px; display: flex; justify-content: space-between; align-items: center; }
.history-id   { font-family: monospace; font-size: 16px; color: #4ade80; }
.history-time { font-size: 12px; color: #64748b; }
</style>
</head>
<body>

<div class="auth-screen" id="authScreen">
  <div class="auth-box">
    <div class="auth-title">JSW COIL-ID SCANNER</div>
    <div class="auth-subtitle">Worker access</div>
    <div class="auth-tabs">
      <button class="auth-tab active" id="loginTab" onclick="showAuthMode('login')">Sign In</button>
      <button class="auth-tab" id="registerTab" onclick="showAuthMode('register')">Create Account</button>
    </div>
    <form class="auth-form" id="loginForm" onsubmit="loginWorker(event)">
      <label>Worker ID<input id="loginWorkerId" autocomplete="username" required></label>
      <label>4-digit PIN<input id="loginPin" type="password" inputmode="numeric" maxlength="4" autocomplete="current-password" required></label>
      <button class="btn btn-capture" type="submit">Sign In</button>
    </form>
    <form class="auth-form" id="registerForm" onsubmit="registerWorker(event)" style="display:none;">
      <label>Full Name<input id="registerName" maxlength="60" required></label>
      <label>Create Worker ID<input id="registerWorkerId" maxlength="24" placeholder="Example: W001" required></label>
      <label>Create 4-digit PIN<input id="registerPin" type="password" inputmode="numeric" maxlength="4" required></label>
      <label>Shift<select id="registerShift" required>
        <option>General Shift</option><option>A Shift</option><option>B Shift</option><option>C Shift</option>
      </select></label>
      <label>Work Location<select id="registerYard" required>
        <option>HSM Yard</option><option>CSP Yard</option>
      </select></label>
      <button class="btn btn-capture" type="submit">Create Account</button>
    </form>
    <div class="auth-error" id="authError"></div>
  </div>
</div>

<div class="header">
  <h1>JSW COIL-ID SCANNER</h1>
  <div class="mode" id="modeLabel">Checking...</div>
</div>

<div class="container">

  <div class="worker-bar" id="workerBar">
    <div class="worker-top">
      <div>
        <div class="worker-name" id="workerName"></div>
        <div class="worker-meta" id="workerMeta"></div>
      </div>
      <div class="worker-actions">
        <button class="btn-small" onclick="toggleContext()">Change</button>
        <button class="btn-small" onclick="logoutWorker()">Logout</button>
      </div>
    </div>
    <div class="context-panel" id="contextPanel">
      <select class="context-select" id="contextShift">
        <option>General Shift</option><option>A Shift</option><option>B Shift</option><option>C Shift</option>
      </select>
      <select class="context-select" id="contextYard">
        <option>HSM Yard</option><option>CSP Yard</option>
      </select>
      <button class="btn-small" onclick="saveContext()">Save Shift and Location</button>
    </div>
  </div>

  <!-- Camera -->
  <div class="camera-section">
    <video id="video" autoplay playsinline></video>
    <canvas id="canvas"></canvas>
    <button class="btn btn-capture" id="captureBtn" onclick="capture()">Capture Photo</button>
  </div>

  <!-- Loading -->
  <div class="loading" id="loading">
    <div class="spinner"></div>
    <div class="loading-text" id="loadingText">Reading coil ID...</div>
  </div>

  <!-- Result -->
  <div class="result-card" id="resultCard">
    <div class="result-status" id="statusRow">
      <div class="status-icon" id="statusIcon"></div>
      <div>
        <div id="statusText" style="font-weight:600;"></div>
        <div id="statusDetail" style="font-size:13px; color:#94a3b8; margin-top:2px;"></div>
      </div>
    </div>

    <div class="result-id" id="resultId"></div>
    <div id="warnings"></div>

    <!-- Manual entry -->
    <div class="manual-entry" id="manualEntry" style="display:none;">
      <label id="manualLabel">Enter coil ID manually (10 digits):</label>
      <input type="tel" id="manualInput" maxlength="10" placeholder="0000000000" oninput="validateManual()">
    </div>

    <!-- Actions -->
    <div class="action-row">
      <button class="btn btn-confirm" id="confirmBtn" onclick="confirmId()">Confirm</button>
      <button class="btn btn-retake" onclick="retake()">Retake</button>
    </div>

    <!-- Print (shows after confirm) -->
    <div class="print-section" id="printSection">
      <div style="font-size:14px; color:#94a3b8; margin-bottom:8px; text-align:center;">Print labels:</div>
      <div class="print-row">
        <button class="btn btn-print" onclick="printLabel(2)">Print 2 Labels</button>
        <button class="btn btn-print" onclick="printLabel(3)">Print 3 Labels</button>
      </div>
    </div>
  </div>

  <!-- History -->
  <div class="history-section">
    <h3>Recent Scans</h3>
    <div id="historyList"></div>
  </div>

</div>

<script>
// ── CONFIG ───────────────────────────────────────────────────────────────────
// Edit these if needed
const MAX_IMAGE_DIM   = 768;    // pixels — must match server setting
const JPEG_QUALITY    = 0.70;   // 0.0 to 1.0
const FETCH_TIMEOUT   = 30000;  // ms — spinner hangs if exceeded → shows error
const MAX_RETRIES     = 2;      // how many times to retry on network failure
const HISTORY_REFRESH = 30000;  // ms — how often history auto-refreshes
// ─────────────────────────────────────────────────────────────────────────────

let video         = document.getElementById('video');
let canvas        = document.getElementById('canvas');
let currentResult = null;
let currentImagePath = '';
let sessionToken = localStorage.getItem('jsw_worker_session') || '';
let currentWorker = null;
let cameraStarted = false;

function showAuthMode(mode) {
    const login = mode === 'login';
    document.getElementById('loginForm').style.display = login ? 'grid' : 'none';
    document.getElementById('registerForm').style.display = login ? 'none' : 'grid';
    document.getElementById('loginTab').classList.toggle('active', login);
    document.getElementById('registerTab').classList.toggle('active', !login);
    document.getElementById('authError').textContent = '';
}

async function postForm(url, values) {
    const form = new FormData();
    Object.entries(values).forEach(([key, value]) => form.append(key, value));
    const response = await fetch(url, {method:'POST', body:form});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Request failed');
    return data;
}

function applyWorker(worker, token) {
    currentWorker = worker;
    if (token) {
        sessionToken = token;
        localStorage.setItem('jsw_worker_session', token);
    }
    document.getElementById('authScreen').style.display = 'none';
    document.getElementById('workerBar').style.display = 'block';
    document.getElementById('workerName').textContent =
        worker.full_name + ' (' + worker.worker_id + ')';
    document.getElementById('workerMeta').textContent =
        worker.shift + ' | ' + worker.yard;
    document.getElementById('contextShift').value = worker.shift;
    document.getElementById('contextYard').value = worker.yard;
    if (!cameraStarted) {
        cameraStarted = true;
        startCamera();
    }
    loadHistory();
}

async function restoreSession() {
    if (!sessionToken) return;
    try {
        const data = await postForm('/worker/session', {session_token:sessionToken});
        applyWorker(data.worker);
    } catch(e) {
        localStorage.removeItem('jsw_worker_session');
        sessionToken = '';
    }
}

async function loginWorker(event) {
    event.preventDefault();
    try {
        const data = await postForm('/worker/login', {
            worker_id: document.getElementById('loginWorkerId').value,
            pin: document.getElementById('loginPin').value
        });
        applyWorker(data.worker, data.token);
    } catch(e) {
        document.getElementById('authError').textContent = e.message;
    }
}

async function registerWorker(event) {
    event.preventDefault();
    try {
        const data = await postForm('/worker/register', {
            full_name: document.getElementById('registerName').value,
            worker_id: document.getElementById('registerWorkerId').value,
            pin: document.getElementById('registerPin').value,
            shift: document.getElementById('registerShift').value,
            yard: document.getElementById('registerYard').value
        });
        applyWorker(data.worker, data.token);
    } catch(e) {
        document.getElementById('authError').textContent = e.message;
    }
}

function logoutWorker() {
    sessionToken = '';
    currentWorker = null;
    localStorage.removeItem('jsw_worker_session');
    document.getElementById('authScreen').style.display = 'block';
    document.getElementById('workerBar').style.display = 'none';
    document.getElementById('resultCard').style.display = 'none';
    showAuthMode('login');
}

function toggleContext() {
    const panel = document.getElementById('contextPanel');
    panel.style.display = panel.style.display === 'grid' ? 'none' : 'grid';
}

async function saveContext() {
    try {
        const data = await postForm('/worker/context', {
            session_token: sessionToken,
            shift: document.getElementById('contextShift').value,
            yard: document.getElementById('contextYard').value
        });
        applyWorker(data.worker);
        document.getElementById('contextPanel').style.display = 'none';
        retake();
    } catch(e) {
        alert(e.message);
    }
}

// ── Camera init ──────────────────────────────────────────────────────────────
async function startCamera() {
    try {
        const stream = await navigator.mediaDevices.getUserMedia({
            video: { facingMode: 'environment', width: {ideal:1920}, height: {ideal:1080} }
        });
        video.srcObject = stream;
    } catch(e) {
        console.log('Camera not available, use file upload');
    }
}

// ── Mode label ───────────────────────────────────────────────────────────────
async function checkMode() {
    try {
        const r = await fetch('/health');
        const d = await r.json();
        const labels = {
            'gemini_online': 'Online — Gemini AI active',
            'no_api_key':    'No API key set — manual entry only',
        };
        document.getElementById('modeLabel').textContent =
            labels[d.mode] || d.mode;
    } catch(e) {
        document.getElementById('modeLabel').textContent = 'Server not reachable';
    }
}

// ── Image compression (runs in browser before upload) ────────────────────────
// Saves bandwidth on weak WiFi. Phone photo (4MB) → ~80KB.
function compressImage(blob) {
    return new Promise((resolve, reject) => {
        const img = new Image();
        const url = URL.createObjectURL(blob);
        img.onload = () => {
            URL.revokeObjectURL(url);
            const scale = Math.min(1, MAX_IMAGE_DIM / Math.max(img.width, img.height));
            const w = Math.round(img.width  * scale);
            const h = Math.round(img.height * scale);
            const cv = document.createElement('canvas');
            cv.width  = w;
            cv.height = h;
            cv.getContext('2d').drawImage(img, 0, 0, w, h);
            cv.toBlob(blob => blob ? resolve(blob) : reject(new Error('compress failed')),
                      'image/jpeg', JPEG_QUALITY);
        };
        img.onerror = () => { URL.revokeObjectURL(url); resolve(blob); }; // fallback: send original
        img.src = url;
    });
}

// ── Capture from camera ───────────────────────────────────────────────────────
function capture() {
    if (!currentWorker) {
        document.getElementById('authScreen').style.display = 'block';
        return;
    }
    canvas.width  = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext('2d').drawImage(video, 0, 0);
    // toBlob with 0.9 here — compressImage() will further reduce to 0.70 at 768px
    canvas.toBlob(blob => sendImage(blob), 'image/jpeg', 0.9);
}

// ── Upload from file ──────────────────────────────────────────────────────────
function uploadFile(e) {
    const file = e.target.files[0];
    if (file) sendImage(file);
}

// ── Send image to server (with compression + retry + timeout) ────────────────
async function sendImage(blob, attempt) {
    attempt = attempt || 0;

    // Compress in browser first
    let compressed;
    try {
        compressed = await compressImage(blob);
    } catch(e) {
        compressed = blob; // fallback: send original
    }

    // Show loading
    setLoadingText(attempt > 0 ? `Retry ${attempt}/${MAX_RETRIES}...` : 'Reading coil ID...');
    document.getElementById('loading').style.display    = 'block';
    document.getElementById('resultCard').style.display = 'none';
    document.getElementById('printSection').style.display = 'none';

    const form = new FormData();
    form.append('file', compressed, 'capture.jpg');
    form.append('session_token', sessionToken);

    // Abort controller for timeout
    const ctrl    = new AbortController();
    const timeout = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT);

    try {
        const r = await fetch('/predict', { method: 'POST', body: form, signal: ctrl.signal });
        clearTimeout(timeout);
        const result = await r.json();
        if (!r.ok) {
            if (r.status === 401) logoutWorker();
            throw new Error(result.detail || 'Scan failed');
        }
        currentResult    = result;
        currentImagePath = result.image_saved || '';
        showResult(result);
    } catch(e) {
        clearTimeout(timeout);
        const isTimeout = e.name === 'AbortError';
        const msg       = isTimeout ? 'Request timed out (30s)' : e.message;

        if (attempt < MAX_RETRIES) {
            console.log(`[retry] attempt ${attempt + 1} — ${msg}`);
            document.getElementById('loading').style.display = 'none';
            sendImage(blob, attempt + 1);  // retry with original blob
            return;
        }
        // All retries failed — show manual entry
        document.getElementById('loading').style.display = 'none';
        showNetworkError(msg);
    } finally {
        document.getElementById('loading').style.display = 'none';
    }
}

function setLoadingText(text) {
    const el = document.getElementById('loadingText');
    if (el) el.textContent = text;
}

// ── Network error state (no connection) ──────────────────────────────────────
function showNetworkError(msg) {
    showManualEntryRequired(
        '&#x1F4F5; No Connection',
        'Could not reach server. Enter coil ID manually.',
        `Details: ${msg}`
    );
}

// ── Helper: show manual entry required state ─────────────────────────────────
function showManualEntryRequired(title, detail, warningHtml) {
    const card = document.getElementById('resultCard');
    card.style.display = 'block';
    document.getElementById('statusIcon').className    = 'status-icon status-warn';
    document.getElementById('statusIcon').innerHTML    = '&#x26A0;';
    document.getElementById('statusText').textContent  = title;
    document.getElementById('statusDetail').textContent = detail;
    document.getElementById('resultId').textContent    = '_ _ _ _ _ _ _ _ _ _';
    document.getElementById('resultId').className      = 'result-id warning';
    document.getElementById('warnings').innerHTML      = warningHtml || '';
    document.getElementById('manualEntry').style.display = 'block';
    document.getElementById('confirmBtn').disabled     = true;
}

// ── Show result ───────────────────────────────────────────────────────────────
function showResult(r) {
    const card       = document.getElementById('resultCard');
    const icon       = document.getElementById('statusIcon');
    const idBox      = document.getElementById('resultId');
    const warnings   = document.getElementById('warnings');
    const manual     = document.getElementById('manualEntry');
    const confirmBtn = document.getElementById('confirmBtn');

    card.style.display   = 'block';
    warnings.innerHTML   = '';
    manual.style.display = 'none';
    confirmBtn.disabled  = false;
    // reset the manual box each time a new result comes in
    document.getElementById('manualLabel').textContent = 'Enter coil ID manually (10 digits):';
    document.getElementById('manualInput').value = '';

    const status = r.status || '';

    // ── Success ──────────────────────────────────────────────────────────────
    if (status === 'success' && r.coil_id) {
        icon.className = 'status-icon status-ok';
        icon.innerHTML = '&#x2714;';
        document.getElementById('statusText').textContent   = 'Coil ID Detected';
        document.getElementById('statusDetail').textContent =
            r.pattern ? ('Pattern: ' + r.pattern) : 'Pattern not recognised — please verify';
        idBox.textContent = r.coil_id;
        idBox.className   = 'result-id ' + (r.pattern ? 'success' : 'warning');

        // Feature #4: manual override is ALWAYS available, even on a good read.
        // Worker can confirm the detected ID as-is, OR type a correction here.
        manual.style.display = 'block';
        document.getElementById('manualLabel').textContent = 'AI read it above. Wrong? Type the correct 10 digits:';

        if (r.duplicate_warning) {
            warnings.innerHTML += '<div class="warning-box dup-warning">&#x26A0; DUPLICATE: This coil ID already scanned '
                + (r.duplicate_count || 1) + ' time(s)!</div>';
        }
        if (r.requires_worker) {
            document.getElementById('statusDetail').textContent = 'Verify digits before confirming';
        }

    // ── Strap blocked ─────────────────────────────────────────────────────────
    } else if (status === 'strap_blocked') {
        icon.className = 'status-icon status-warn';
        icon.innerHTML = '&#x26A0;';
        document.getElementById('statusText').textContent   = 'Strap Detected';
        document.getElementById('statusDetail').textContent = 'Enter coil ID manually';
        idBox.textContent = '_ _ _ _ _ _ _ _ _ _';
        idBox.className   = 'result-id warning';
        warnings.innerHTML = '<div class="warning-box strap-warning">&#x1F6AB; A strap is blocking the digits. Type the ID manually.</div>';
        manual.style.display = 'block';
        confirmBtn.disabled  = true;

    // ── Image unclear → retake ────────────────────────────────────────────────
    } else if (status === 'yard_mismatch') {
        icon.className = 'status-icon status-warn';
        icon.innerHTML = '&#x26A0;';
        document.getElementById('statusText').textContent = 'Wrong Yard Format';
        document.getElementById('statusDetail').textContent =
            'The AI read does not match ' + (currentWorker ? currentWorker.yard : 'the selected yard');
        idBox.textContent = r.coil_id || 'CHECK ID';
        idBox.className = 'result-id warning';
        warnings.innerHTML = '<div class="warning-box">Check the selected work location or type the correct coil ID.</div>';
        manual.style.display = 'block';
        confirmBtn.disabled = true;

    } else if (status === 'image_unclear' || status === 'image_rejected') {
        icon.className = 'status-icon status-error';
        icon.innerHTML = '&#x2716;';
        document.getElementById('statusText').textContent   = 'Image Not Clear';
        document.getElementById('statusDetail').textContent = 'Retake photo (better lighting / angle)';
        idBox.textContent = 'RETAKE';
        idBox.className   = 'result-id error';
        confirmBtn.disabled = true;

    // ── Quota exhausted ───────────────────────────────────────────────────────
    } else if (status === 'quota_exhausted') {
        icon.className = 'status-icon status-warn';
        icon.innerHTML = '&#x1F4CA;';
        document.getElementById('statusText').textContent   = 'Daily AI Limit Reached';
        document.getElementById('statusDetail').textContent = 'Resets at midnight. Enter ID manually now.';
        idBox.textContent = '_ _ _ _ _ _ _ _ _ _';
        idBox.className   = 'result-id warning';
        warnings.innerHTML = '<div class="warning-box quota-warning">&#x1F4CA; Gemini API daily quota exhausted. Enter the coil ID manually below.</div>';
        manual.style.display = 'block';
        confirmBtn.disabled  = true;

    // ── No API key ────────────────────────────────────────────────────────────
    } else if (status === 'no_api_key') {
        icon.className = 'status-icon status-error';
        icon.innerHTML = '&#x1F511;';
        document.getElementById('statusText').textContent   = 'API Key Not Set';
        document.getElementById('statusDetail').textContent = 'Set GEMINI_API_KEY_1 in .env file';
        idBox.textContent = 'NO KEY';
        idBox.className   = 'result-id error';
        manual.style.display = 'block';
        confirmBtn.disabled  = true;

    // ── Generic failure ───────────────────────────────────────────────────────
    } else {
        icon.className = 'status-icon status-error';
        icon.innerHTML = '&#x2716;';
        document.getElementById('statusText').textContent   = 'Detection Failed';
        document.getElementById('statusDetail').textContent = 'Enter ID manually';
        idBox.textContent = 'NO ID';
        idBox.className   = 'result-id error';
        manual.style.display = 'block';
        confirmBtn.disabled  = true;
    }
}

// ── Validate manual input ─────────────────────────────────────────────────────
function validateManual() {
    const input = document.getElementById('manualInput');
    const val   = input.value.replace(/[^0-9]/g, '');
    input.value = val;
    const detected = (currentResult && currentResult.coil_id && currentResult.coil_id.length === 10)
                     ? currentResult.coil_id : null;
    const yardValid = val.length === 10 && matchesCurrentYard(val);
    if (yardValid) {
        // worker typed a full override → use it
        document.getElementById('confirmBtn').disabled  = false;
        document.getElementById('resultId').textContent = val;
        document.getElementById('resultId').className   = 'result-id warning';
    } else if (val.length === 0 && detected && matchesCurrentYard(detected)) {
        // override cleared → fall back to confirming the AI-detected ID
        document.getElementById('confirmBtn').disabled  = false;
        document.getElementById('resultId').textContent = detected;
    } else {
        // partial typing → not a valid 10-digit ID yet
        document.getElementById('confirmBtn').disabled = true;
    }
}

function matchesCurrentYard(coilId) {
    if (!currentWorker || !/^\d{10}$/.test(coilId)) return false;
    const year = String(new Date().getFullYear()).slice(-2);
    if (currentWorker.yard === 'HSM Yard') return coilId.startsWith('02' + year);
    return coilId.startsWith(year) && coilId[3] === '0';
}

// ── Confirm ID ───────────────────────────────────────────────────────────────
async function confirmId() {
    const manualVal = document.getElementById('manualInput').value;
    const coilId    = (manualVal && manualVal.length === 10)
                      ? manualVal
                      : (currentResult && currentResult.coil_id);

    if (!coilId || coilId.length !== 10) {
        alert('No valid 10-digit coil ID to confirm');
        return;
    }
    if (!matchesCurrentYard(coilId)) {
        alert('This coil ID does not match ' + currentWorker.yard + ' for the current year.');
        return;
    }

    // Feature #2: if this ID is already in inventory, make the worker choose.
    if (currentResult && currentResult.duplicate_warning && coilId === currentResult.coil_id) {
        const n = currentResult.duplicate_count || 1;
        if (!confirm('This coil ID was already scanned ' + n + ' time(s).\nScan it again anyway?')) {
            return;  // worker chose NOT to re-scan the duplicate
        }
    }

    try {
        const form = new FormData();
        form.append('coil_id',         coilId);
        form.append('image_path',      currentImagePath);
        form.append('worker_verified', manualVal ? 'true' : 'false');
        form.append('session_token',   sessionToken);

        const r = await fetch('/confirm', { method: 'POST', body: form });
        const d = await r.json();
        if (!r.ok) {
            if (r.status === 401) logoutWorker();
            throw new Error(d.detail || 'Confirmation failed');
        }

        if (d.is_duplicate) {
            alert('Warning: This coil ID was already scanned ' + d.total_scans + ' time(s)!');
        }

        document.getElementById('printSection').style.display = 'block';
        document.getElementById('confirmBtn').disabled        = true;
        document.getElementById('confirmBtn').textContent     = 'Confirmed ✓';
        loadHistory();
    } catch(e) {
        alert('Error confirming: ' + e.message);
    }
}

// ── Print stickers ────────────────────────────────────────────────────────────
async function printLabel(count) {
    const coilId = document.getElementById('resultId').textContent.replace(/\s/g,'');
    if (!coilId || coilId.length !== 10 || !/^\d+$/.test(coilId)) {
        alert('Confirm a valid 10-digit coil ID first.');
        return;
    }

    // Pull label fields (customer, grade, ...) — empty until SAP read is wired.
    let fields = {};
    try {
        const resp = await fetch('/label_data?coil_id=' + encodeURIComponent(coilId));
        const data = await resp.json();
        fields = data.fields || {};
    } catch(e) { /* offline / SAP off -> print ID + blank fields */ }

    const v = k => (fields[k] !== undefined && fields[k] !== null && fields[k] !== '') ? fields[k] : '-';
    const origin = window.location.origin;
    const batch  = (v('batch_no') !== '-') ? v('batch_no') : coilId;          // right barcode value
    const prod   = (v('product')  !== '-') ? v('product')  : 'HOT Rolled Coil';

    const oneLabel = () => `
      <div class="lbl">
        <!-- header band: MADE IN INDIA | MS/SIRIM | JSW + address | QR -->
        <div class="hd">
          <div class="hd-made">MADE<br>IN<br>INDIA</div>
          <div class="hd-ms"><div class="ms-d">MS</div><div class="ms-s">SIRIM</div><div class="ms-pc">${v('cert_no')!=='-'?v('cert_no'):'PC 012776'}</div></div>
          <div class="hd-jsw"><div class="jsw-lg"><i>JSW</i> Steel</div><div class="jsw-ad">Dolvi Works, Taluka - Pen, Dist - Raigad<br>Maharashtra - 402107, India</div></div>
          <div class="hd-qr"><img src="${origin}/qrcode/${coilId}" alt="qr"></div>
        </div>
        <div class="ttl">${prod}</div>
        <!-- coil number + certified to std -->
        <div class="bk b2">
          <div class="cl"><div class="lb">Coil/Pack Number</div><div class="coil">${coilId}</div></div>
          <div class="cl"><div class="lb">Certified to Std</div><div class="vl">${v('cert_std')}</div></div>
        </div>
        <!-- specs row 1 -->
        <div class="bk b3">
          <div class="cl"><div class="lb">Size (mm)</div><div class="vl">${v('size')}</div></div>
          <div class="cl"><div class="lb">Heat No</div><div class="vl">${v('heat_no')}</div></div>
          <div class="cl"><div class="lb">Grade</div><div class="vl">${v('grade')}</div></div>
        </div>
        <!-- specs row 2 -->
        <div class="bk b3 dv">
          <div class="cl"><div class="lb">Net Weight (MT)</div><div class="vl">${v('net_weight')}</div></div>
          <div class="cl"><div class="lb">Quality</div><div class="vl">${v('quality')}</div></div>
          <div class="cl"><div class="lb">Certification No</div><div class="vl">${v('cert_no')}</div></div>
        </div>
        <!-- customer row -->
        <div class="bk b4">
          <div class="cl"><div class="lb">Customer Name</div><div class="vl">${v('customer')}</div></div>
          <div class="cl"><div class="lb">Delivery Condition</div><div class="vl">${v('delivery_cond')}</div></div>
          <div class="cl"><div class="lb">SO No.</div><div class="vl">${v('so_no')}</div></div>
          <div class="cl"><div class="lb">Insp. Date</div><div class="vl">${v('insp_date')}</div></div>
        </div>
        <!-- shipping row -->
        <div class="bk b4">
          <div class="cl"><div class="lb">Shipping Mark</div><div class="vl">${v('shipping_mark')}</div></div>
          <div class="cl"><div class="lb">Destination</div><div class="vl">${v('destination')}</div></div>
          <div class="cl"><div class="lb">Batch Number</div><div class="vl">${batch}</div></div>
          <div class="cl"><div class="lb">Inspected By</div><div class="vl">${v('inspected_by')}</div></div>
        </div>
        <!-- two barcodes -->
        <div class="bcs">
          <div class="bc"><img src="${origin}/barcode/${coilId}" alt="${coilId}"><div class="bcn">${coilId}</div></div>
          <div class="bc"><img src="${origin}/barcode/${batch}" alt="${batch}"><div class="bcn">${batch}</div></div>
        </div>
        <!-- weigh bridge footer -->
        <div class="wb">
          <div class="wb-h">WEIGH BRIDGE # IN</div>
          <div class="wb-t">Weighed on the Mill scale certified by the Weights and Measures Department which is accepted for the Self Removal Procedure by Central Excise and/or Custom Dept and all the statutory purposes</div>
        </div>
      </div>`;

    let labels = '';
    for (let i = 0; i < count; i++) labels += oneLabel();

    const css = `<style>
        @media print { .noprint{display:none} @page{ margin:6mm } }
        body{ font-family:Arial,Helvetica,sans-serif; padding:8px; color:#000; }
        .lbl{ border:2px solid #000; width:660px; margin:0 0 16px; page-break-inside:avoid; }
        .hd{ display:flex; align-items:center; border-bottom:2px solid #000; }
        .hd-made{ font-size:11px; font-weight:bold; text-align:center; border:2px solid #000; margin:6px; padding:4px 8px; line-height:1.15; }
        .hd-ms{ text-align:center; padding:4px 6px; line-height:1.05; }
        .ms-d{ display:inline-block; border:2px solid #000; font-weight:bold; font-size:15px; padding:1px 8px; }
        .ms-s{ font-size:8px; } .ms-pc{ font-size:9px; }
        .hd-jsw{ flex:1; text-align:center; }
        .jsw-lg{ font-size:26px; font-weight:bold; } .jsw-lg i{ font-style:italic; }
        .jsw-ad{ font-size:11px; }
        .hd-qr{ padding:5px; } .hd-qr img{ width:62px; height:62px; }
        .ttl{ text-align:center; font-weight:bold; font-size:16px; padding:3px 0; border-bottom:2px solid #000; }
        .bk{ display:flex; padding:6px 8px; }
        .bk.dv{ border-bottom:2px solid #000; }
        .cl{ flex:1; padding-right:8px; }
        .lb{ font-weight:bold; font-size:12px; }
        .vl{ font-size:12px; }
        .coil{ font-size:24px; font-weight:bold; font-family:monospace; letter-spacing:2px; }
        .bcs{ display:flex; gap:30px; padding:4px 10px 0; }
        .bc img{ height:50px; width:240px; }
        .bcn{ font-family:monospace; font-size:12px; letter-spacing:2px; }
        .wb{ border:2px solid #000; margin:8px; }
        .wb-h{ text-align:center; font-weight:bold; font-size:12px; border-bottom:1px solid #000; padding:2px; }
        .wb-t{ font-size:10px; padding:4px 8px; }
      </style>`;

    const win = window.open('', '_blank');
    win.document.write('<html><head><meta charset="utf-8"><title>JSW Coil Label</title>' + css + '</head><body>'
        + labels
        + '<button class="noprint" onclick="window.print()" style="padding:10px 30px;font-size:16px;cursor:pointer;margin:10px 0;">Print</button>'
        + '</body></html>');
    win.document.close();
}

// ── Retake ────────────────────────────────────────────────────────────────────
function retake() {
    document.getElementById('resultCard').style.display   = 'none';
    document.getElementById('printSection').style.display = 'none';
    document.getElementById('manualInput').value          = '';
    document.getElementById('confirmBtn').textContent     = 'Confirm';
    document.getElementById('confirmBtn').disabled        = false;
    currentResult = null;
}

// ── History ───────────────────────────────────────────────────────────────────
async function loadHistory() {
    try {
        if (!sessionToken) return;
        const r    = await fetch('/history?session_token=' + encodeURIComponent(sessionToken));
        if (r.status === 401) {
            logoutWorker();
            return;
        }
        const d    = await r.json();
        const list = document.getElementById('historyList');
        list.innerHTML = '';
        if (!d.scans.length) {
            list.innerHTML = '<div style="color:#475569;font-size:13px;">No scans in last 2 days</div>';
            return;
        }
        d.scans.slice(0, 20).forEach(s => {
            const div  = document.createElement('div');
            div.className = 'history-item';
            const time = new Date(s.timestamp).toLocaleString();
            div.innerHTML = '<span><span class="history-id">' + s.coil_id + '</span>'
                          + '<span class="history-time" style="display:block;">'
                          + s.shift + ' | ' + s.yard + '</span></span>'
                          + '<span class="history-time">' + time + '</span>';
            list.appendChild(div);
        });
    } catch(e) {}
}

// ── Init ──────────────────────────────────────────────────────────────────────
// PWA install support
function registerServiceWorker() {
    if (!('serviceWorker' in navigator) || window.location.protocol !== 'https:') {
        return;
    }
    navigator.serviceWorker.register('/sw.js').catch(e => {
        console.log('Service worker registration failed:', e);
    });
}

registerServiceWorker();
checkMode();
restoreSession();
setInterval(loadHistory, HISTORY_REFRESH);  // auto-refresh every 30s
</script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    print("=" * 50)
    print(f"  JSW Coil OCR v3.0")
    print(f"  Open: http://localhost:{port}")
    print("=" * 50)
    uvicorn.run(app, host="0.0.0.0", port=port)
