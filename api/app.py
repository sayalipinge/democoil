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
from fastapi.responses import HTMLResponse, JSONResponse, Response

from pipeline.gemini_pipeline import predict, register_coil
from modules.inventory import InventoryManager

ROOT = Path(__file__).resolve().parent.parent

# DATA_DIR: set via env var on cloud (e.g. /data for Fly.io volume)
# Defaults to project's data/ folder for local use
DATA_DIR   = Path(os.environ.get("DATA_DIR", str(ROOT / "data")))
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

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
    # label_field      : "SAP_JSON_KEY",
    # "product"        : "ProductType",
    # "grade"          : "Grade",
    # "size"           : "Size",
    # "heat_no"        : "HeatNo",
    # "net_weight"     : "NetWeight",
    # "quality"        : "Quality",
    # "certification"  : "CertifiedToStd",
    # "customer"       : "CustomerName",
    # "so_no"          : "SoNumber",
    # "destination"    : "Destination",
    # "batch_no"       : "BatchNumber",
    # "delivery_cond"  : "DeliveryCondition",
    # "insp_date"      : "InspDate",
    # "shipping_mark"  : "ShippingMark",
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


async def _notify_mes(coil_id: str, timestamp: str, image_path: str):
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
                "source":     "jsw_coil_ocr",
            })
    except Exception as e:
        print(f"[mes_webhook] Failed to notify MES: {e}")


async def _send_to_drive(coil_id: str, image_path: str, worker_verified: bool):
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
    filename = f"{coil_id}_{stamp}_{tag}.jpg"

    payload = {
        "filename":  filename,
        "image_b64": base64.b64encode(img_bytes).decode(),
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
        "start_url":        "/",
        "display":          "standalone",
        "background_color": "#0f172a",
        "theme_color":      "#3b82f6",
        "orientation":      "portrait-primary",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    })


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


# ── Label data (coil ID + SAP fields, if read is configured) ─────────────────
@app.get("/label_data")
async def label_data(coil_id: str):
    """Fields for the printed label. coil_id always present; rest come from SAP read."""
    ok     = len(coil_id) == 10 and coil_id.isdigit()
    fields = await _fetch_sap_fields(coil_id) if ok else {}
    return {"coil_id": coil_id, "fields": fields, "sap_read": bool(SAP_READ_ENDPOINT)}


# ── Predict endpoint ─────────────────────────────────────────────────────────
@app.post("/predict")
async def predict_endpoint(file: UploadFile = File(...)):
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "File must be an image")

    contents = await file.read()
    nparr    = np.frombuffer(contents, np.uint8)
    image_bgr = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

    if image_bgr is None:
        raise HTTPException(400, "Could not decode image")

    # Save compressed image (not full resolution)
    ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_path = UPLOAD_DIR / f"scan_{ts}.jpg"
    _save_compressed(image_bgr, save_path)

    # Run prediction
    result = predict(image_bgr, check_inventory=True, image_path=str(save_path))
    result["image_saved"] = str(save_path)
    return result


# ── Confirm endpoint ─────────────────────────────────────────────────────────
@app.post("/confirm")
async def confirm_endpoint(
    background_tasks: BackgroundTasks,
    coil_id:         str  = Form(...),
    image_path:      str  = Form(""),
    worker_verified: bool = Form(False),
):
    if len(coil_id) != 10 or not coil_id.isdigit():
        raise HTTPException(400, "Coil ID must be exactly 10 digits")

    result    = register_coil(coil_id, image_path, worker_verified)
    timestamp = datetime.now().isoformat()

    # Notify MES/SAP (fire and forget — won't delay response)
    await _notify_mes(coil_id, timestamp, image_path)

    # Save the photo to Google Drive for manager review.
    # Background task = runs AFTER the response is sent, so confirm stays instant.
    background_tasks.add_task(_send_to_drive, coil_id, image_path, worker_verified)

    # Push the coil ID into SAP (also background — see the SAP_* config block).
    # No-op until SAP_ENDPOINT is filled, so this is safe to ship now.
    background_tasks.add_task(_notify_sap, coil_id, timestamp)

    return result


# ── History endpoint ─────────────────────────────────────────────────────────
@app.get("/history")
def history_endpoint():
    inv     = InventoryManager()
    all_c   = inv.get_all()
    cutoff  = datetime.now() - timedelta(days=2)
    recent  = []

    for coil_id, data in all_c.items():
        for scan in data["scans"]:
            try:
                scan_time = datetime.fromisoformat(scan["timestamp"])
                if scan_time >= cutoff:
                    recent.append({
                        "coil_id":         coil_id,
                        "timestamp":       scan["timestamp"],
                        "image_path":      scan.get("image_path", ""),
                        "worker_verified": scan.get("worker_verified", False),
                    })
            except (ValueError, KeyError):
                pass

    recent.sort(key=lambda x: x["timestamp"], reverse=True)
    return {"scans": recent, "total": len(recent)}


# ── Inventory endpoint ────────────────────────────────────────────────────────
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
<title>JSW Coil OCR</title>

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

<div class="header">
  <h1>JSW Coil OCR</h1>
  <div class="mode" id="modeLabel">Checking...</div>
</div>

<div class="container">

  <!-- Camera -->
  <div class="camera-section">
    <video id="video" autoplay playsinline></video>
    <canvas id="canvas"></canvas>
    <button class="btn btn-capture" id="captureBtn" onclick="capture()">Capture Photo</button>
    <button class="btn btn-upload" onclick="document.getElementById('fileInput').click()">Upload Image</button>
    <input type="file" id="fileInput" accept="image/*" capture="environment" onchange="uploadFile(event)">
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

    // Abort controller for timeout
    const ctrl    = new AbortController();
    const timeout = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT);

    try {
        const r = await fetch('/predict', { method: 'POST', body: form, signal: ctrl.signal });
        clearTimeout(timeout);
        const result = await r.json();
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
    if (val.length === 10) {
        // worker typed a full override → use it
        document.getElementById('confirmBtn').disabled  = false;
        document.getElementById('resultId').textContent = val;
        document.getElementById('resultId').className   = 'result-id warning';
    } else if (val.length === 0 && detected) {
        // override cleared → fall back to confirming the AI-detected ID
        document.getElementById('confirmBtn').disabled  = false;
        document.getElementById('resultId').textContent = detected;
    } else {
        // partial typing → not a valid 10-digit ID yet
        document.getElementById('confirmBtn').disabled = true;
    }
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

        const r = await fetch('/confirm', { method: 'POST', body: form });
        const d = await r.json();

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

    const v = k => (fields[k] !== undefined && fields[k] !== null && fields[k] !== '') ? fields[k] : '—';
    const origin = window.location.origin;

    const oneLabel = () => `
      <div class="lbl">
        <div class="lbl-head">
          <div class="made">MADE<br>IN<br>INDIA</div>
          <div class="brand"><i>JSW</i> <span>Steel</span></div>
          <div class="addr">Dolvi Works, Taluka - Pen, Dist - Raigad,<br>Maharashtra - 402107, India</div>
        </div>
        <div class="lbl-title">${v('product')==='—' ? 'HOT ROLLED COIL' : v('product')}</div>
        <table class="lbl-tbl">
          <tr><td class="k">Coil/Pack Number</td><td class="big" colspan="3">${coilId}</td></tr>
          <tr><td class="k">Grade</td><td>${v('grade')}</td><td class="k">Heat No</td><td>${v('heat_no')}</td></tr>
          <tr><td class="k">Size (mm)</td><td>${v('size')}</td><td class="k">Net Weight (MT)</td><td>${v('net_weight')}</td></tr>
          <tr><td class="k">Quality</td><td>${v('quality')}</td><td class="k">Certified to Std</td><td>${v('certification')}</td></tr>
          <tr><td class="k">Customer Name</td><td>${v('customer')}</td><td class="k">SO No.</td><td>${v('so_no')}</td></tr>
          <tr><td class="k">Destination</td><td>${v('destination')}</td><td class="k">Batch Number</td><td>${v('batch_no')}</td></tr>
          <tr><td class="k">Delivery Condition</td><td>${v('delivery_cond')}</td><td class="k">Insp. Date</td><td>${v('insp_date')}</td></tr>
          <tr><td class="k">Shipping Mark</td><td colspan="3">${v('shipping_mark')}</td></tr>
        </table>
        <div class="lbl-bc"><img src="${origin}/barcode/${coilId}" alt="${coilId}"><div class="bc-num">${coilId}</div></div>
        <div class="lbl-foot">Weighed on the Mill scale certified by the Weights and Measures Department.</div>
      </div>`;

    let labels = '';
    for (let i = 0; i < count; i++) labels += oneLabel();

    const css = `<style>
        @media print { .noprint{display:none} @page{ margin:8mm } }
        body{ font-family:Arial,sans-serif; padding:10px; }
        .lbl{ border:2px solid #000; padding:10px 14px; margin:0 0 14px; width:480px; page-break-inside:avoid; }
        .lbl-head{ display:flex; align-items:center; gap:10px; border-bottom:2px solid #000; padding-bottom:6px; }
        .made{ font-size:8px; font-weight:bold; text-align:center; border:1px solid #000; padding:2px 4px; line-height:1.1; }
        .brand{ font-size:26px; font-weight:bold; } .brand span{ font-size:13px; font-weight:normal; }
        .addr{ font-size:9px; color:#222; margin-left:auto; text-align:right; }
        .lbl-title{ text-align:center; font-weight:bold; font-size:13px; padding:5px 0; border-bottom:1px solid #000; }
        .lbl-tbl{ width:100%; border-collapse:collapse; margin-top:4px; }
        .lbl-tbl td{ border:1px solid #999; padding:3px 6px; font-size:11px; vertical-align:top; }
        .lbl-tbl .k{ color:#444; font-size:9px; width:92px; }
        .lbl-tbl .big{ font-size:20px; font-weight:bold; letter-spacing:2px; font-family:monospace; }
        .lbl-bc{ text-align:center; margin-top:8px; }
        .lbl-bc img{ height:46px; width:90%; }
        .bc-num{ font-family:monospace; font-size:13px; letter-spacing:3px; }
        .lbl-foot{ font-size:8px; color:#333; border-top:1px solid #000; margin-top:6px; padding-top:4px; }
      </style>`;

    const win = window.open('', '_blank');
    win.document.write('<html><head><title>JSW Coil Label</title>' + css + '</head><body>'
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
    document.getElementById('fileInput').value            = '';
    currentResult = null;
}

// ── History ───────────────────────────────────────────────────────────────────
async function loadHistory() {
    try {
        const r    = await fetch('/history');
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
            div.innerHTML = '<span class="history-id">' + s.coil_id + '</span>'
                          + '<span class="history-time">' + time + '</span>';
            list.appendChild(div);
        });
    } catch(e) {}
}

// ── Init ──────────────────────────────────────────────────────────────────────
startCamera();
checkMode();
loadHistory();
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
