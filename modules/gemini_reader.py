"""
Gemini Vision API reader for coil ID detection.

CONFIGURATION (set in environment or .env file):
    GEMINI_API_KEY_1  — primary free key   (required)
    GEMINI_API_KEY_2  — backup free key    (optional, get from 2nd Google account)
    GEMINI_API_KEY_3  — backup free key    (optional, get from 3rd Google account)

    If one key hits daily quota (1500 req/day), auto-rotates to next key.
    Three keys = 4500 req/day free.

    To get a key: https://aistudio.google.com/apikey (free, no credit card)

IMAGE COMPRESSION:
    All images resized to max 768px before sending.
    768px = 1 tile = 258 tokens  (vs 5000+ tokens for raw phone photo)
    Saves ~95% of token usage. Also faster on weak WiFi.

    Tweak these if needed:
        MAX_IMAGE_DIM = 768   (pixels — don't go below 512)
        JPEG_QUALITY  = 70    (0-100 — don't go below 60)
"""
from __future__ import annotations

import base64
import os
import re
import cv2
import numpy as np
from pathlib import Path
from typing import Optional, Union

# ── CONFIGURATION — edit these if needed ────────────────────────────────────
MAX_IMAGE_DIM = 768   # max pixel dimension before sending to Gemini
JPEG_QUALITY  = 70    # JPEG compression quality (60-85 is fine)
GEMINI_MODEL  = "gemini-2.5-flash"  # model name — update if Google changes it
# ────────────────────────────────────────────────────────────────────────────


# ── Load API keys from environment ──────────────────────────────────────────
def _get_api_keys() -> list[str]:
    """
    Returns list of available Gemini API keys.
    Checks GEMINI_API_KEY_1, _2, _3 and also GEMINI_API_KEY / GOOGLE_API_KEY
    for backward compatibility.
    Add more keys by setting GEMINI_API_KEY_2, GEMINI_API_KEY_3 in your .env
    """
    keys = []
    # New style: numbered keys for rotation
    for i in range(1, 4):
        k = os.environ.get(f"GEMINI_API_KEY_{i}", "").strip()
        if k:
            keys.append(k)
    # Backward compat: single key
    for env_name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        k = os.environ.get(env_name, "").strip()
        if k and k not in keys:
            keys.append(k)
    return keys


# ── Image compression ────────────────────────────────────────────────────────
def _compress_image(image_input: Union[str, Path, bytes]) -> tuple[bytes, str]:
    """
    Compress image to max MAX_IMAGE_DIM px at JPEG_QUALITY.
    Returns (compressed_bytes, mime_type).

    Why: raw phone photo = 4MB = 5000+ tokens.
         compressed 768px = 80KB = 258 tokens (1 tile).
         19x cheaper, 10x faster upload on weak WiFi.
    """
    # Load image
    if isinstance(image_input, (str, Path)):
        img = cv2.imread(str(image_input))
    elif isinstance(image_input, bytes):
        arr = np.frombuffer(image_input, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    else:
        raise TypeError(f"Expected path or bytes, got {type(image_input)}")

    if img is None:
        raise ValueError("Could not decode image")

    # Resize if larger than MAX_IMAGE_DIM
    h, w = img.shape[:2]
    if max(h, w) > MAX_IMAGE_DIM:
        scale = MAX_IMAGE_DIM / max(h, w)
        new_w = int(w * scale)
        new_h = int(h * scale)
        img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)

    # Encode as JPEG
    encode_params = [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
    ok, buf = cv2.imencode(".jpg", img, encode_params)
    if not ok:
        raise RuntimeError("Failed to encode image as JPEG")

    return buf.tobytes(), "image/jpeg"


def _to_b64(image_bytes: bytes) -> str:
    return base64.b64encode(image_bytes).decode()


# ── Gemini API call with key rotation ────────────────────────────────────────
class QuotaExhaustedException(Exception):
    """Raised when ALL Gemini API keys have hit their daily quota."""
    pass


def _call_gemini(image_bytes: bytes, mime_type: str, prompt: str) -> str:
    """
    Send compressed image + prompt to Gemini.

    Key rotation:
        - RPM limit (per-minute burst) → wait 8s, retry same key
        - Daily quota exhausted        → silently rotate to next key
        - All keys exhausted           → raise QuotaExhaustedException

    Worker sees "Enter manually" instead of a crash.
    """
    import time
    from google import genai

    keys = _get_api_keys()
    if not keys:
        raise RuntimeError(
            "No GEMINI_API_KEY set.\n"
            "Get a free key at https://aistudio.google.com/apikey\n"
            "Then set GEMINI_API_KEY_1=your_key_here in your .env file"
        )

    img_b64 = _to_b64(image_bytes)

    for key_index, api_key in enumerate(keys):
        rpm_retries = 0
        while rpm_retries < 3:
            try:
                client = genai.Client(api_key=api_key)
                response = client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=[
                        {
                            "inline_data": {
                                "mime_type": mime_type,
                                "data": img_b64,
                            }
                        },
                        prompt,
                    ],
                )
                return response.text.strip()

            except Exception as e:
                err = str(e).upper()

                # Daily quota exhausted → try next key (don't retry same key)
                if "RESOURCE_EXHAUSTED" in err or "QUOTA" in err:
                    print(f"[gemini_reader] Key {key_index + 1} daily quota exhausted. Trying next key...")
                    break  # break inner while, go to next key in for loop

                # Per-minute rate limit → wait and retry same key
                elif "429" in err or "RATE_LIMIT" in err:
                    rpm_retries += 1
                    if rpm_retries < 3:
                        wait = 8 * rpm_retries
                        print(f"[gemini_reader] RPM limit hit. Waiting {wait}s...")
                        time.sleep(wait)
                        continue
                    else:
                        break  # give up on this key after 3 RPM retries

                else:
                    raise  # real error (bad key, network down, etc) — propagate

    # All keys exhausted
    raise QuotaExhaustedException(
        f"All {len(keys)} Gemini API key(s) have hit their daily quota. "
        "Worker must enter ID manually. Quota resets at midnight."
    )


# ── OCR Prompt ───────────────────────────────────────────────────────────────
# Carefully crafted. Edit with caution.
# STRAP_BLOCKED and IMAGE_UNCLEAR are handled in read_coil_id() below.
_OCR_PROMPT = """You are reading a steel coil identification number from a factory photo.

TASK: Find and read the spray-painted 10-digit coil ID number on the steel coil surface.

RULES:
1. The ID is exactly 10 digits (0-9). On the coil the 10 digits MAY be
   immediately preceded or followed by a letter or short mark (a grade/suffix,
   e.g. "A", "B", "X"). Read ONLY the 10 digits and IGNORE any such adjacent
   letter. A leading/trailing letter is NOT a reason to say IMAGE_UNCLEAR.
2. The digits are spray-painted in white/light color on the dark steel surface.
3. The number may be curved (painted on round coil surface).
4. IGNORE any stickers, labels, tags, or printed text. Read ONLY the spray-painted number.
5. If a black, grey, or silver plastic strap/band is covering ANY digit (even partly), respond with: STRAP_BLOCKED
6. Only if the digits are GENUINELY unreadable (too blurry, dark, or angled),
   respond with: IMAGE_UNCLEAR. Do NOT use IMAGE_UNCLEAR just because of an
   extra letter or mark next to the number.
7. If you can read all 10 digits, respond with ONLY the 10 digits, nothing else
   (no letters, no spaces, no suffix).

Respond with one of:
- The 10 digits (e.g., 0226048954)
- STRAP_BLOCKED
- IMAGE_UNCLEAR"""


# ── Main OCR function ─────────────────────────────────────────────────────────
def read_coil_id(image_input: Union[str, Path, bytes],
                 pattern_hint: Optional[str] = None) -> dict:
    """
    Read coil ID from image using Gemini Vision API.

    Returns dict:
        coil_id:         str | None   — 10-digit ID or None
        confidence:      float        — 0.95 (pattern match) / 0.80 (no pattern) / 0.0 (fail)
        pattern:         str | None   — "HSM2", "CSP", or None
        status:          str          — "success" / "strap_blocked" / "image_unclear" /
                                        "partial_detection" / "quota_exhausted" / "api_error"
        strap_detected:  bool
        raw_response:    str          — raw Gemini output for debugging
        requires_worker: bool
    """
    # Step 1: Compress image
    try:
        image_bytes, mime_type = _compress_image(image_input)
    except Exception as e:
        return {
            "coil_id": None, "confidence": 0.0, "pattern": None,
            "status": f"compress_error: {str(e)[:80]}",
            "strap_detected": False, "raw_response": "",
            "requires_worker": True,
        }

    # Step 2: Call Gemini
    try:
        raw = _call_gemini(image_bytes, mime_type, _OCR_PROMPT)
    except QuotaExhaustedException as e:
        return {
            "coil_id": None, "confidence": 0.0, "pattern": None,
            "status": "quota_exhausted",
            "strap_detected": False, "raw_response": str(e),
            "requires_worker": True,
        }
    except Exception as e:
        return {
            "coil_id": None, "confidence": 0.0, "pattern": None,
            "status": f"api_error: {str(e)[:100]}",
            "strap_detected": False, "raw_response": str(e),
            "requires_worker": True,
        }

    # Step 3: Parse response
    raw_upper = raw.upper()

    if "STRAP_BLOCKED" in raw_upper:
        return {
            "coil_id": None, "confidence": 0.0, "pattern": None,
            "status": "strap_blocked",
            "strap_detected": True, "raw_response": raw,
            "requires_worker": True,
        }

    if "IMAGE_UNCLEAR" in raw_upper:
        return {
            "coil_id": None, "confidence": 0.0, "pattern": None,
            "status": "image_unclear",
            "strap_detected": False, "raw_response": raw,
            "requires_worker": True,
        }

    # Extract digits only
    digits = re.sub(r"[^0-9]", "", raw)

    if len(digits) < 10:
        return {
            "coil_id": digits if digits else None,
            "confidence": 0.3, "pattern": None,
            "status": "partial_detection",
            "strap_detected": False, "raw_response": raw,
            "requires_worker": True,
        }

    coil_id = digits[:10]

    # Step 4: Validate pattern
    from modules.validator import validate_format
    pattern = validate_format(coil_id)

    # Confidence:
    #   pattern match + hint match = 0.98
    #   pattern match only         = 0.95
    #   no pattern (still 10 digits) = 0.80  ← show to worker, don't block
    if pattern and pattern_hint and pattern == pattern_hint:
        conf = 0.98
    elif pattern:
        conf = 0.95
    else:
        conf = 0.80  # valid 10 digits but unknown pattern — worker verifies

    return {
        "coil_id": coil_id,
        "confidence": conf,
        "pattern": pattern,
        "status": "success",
        "strap_detected": False,
        "raw_response": raw,
        "requires_worker": (conf < 0.90 or pattern is None),
    }
