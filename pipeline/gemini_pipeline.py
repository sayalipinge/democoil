"""
Gemini Vision Pipeline — Primary coil ID detection.

Flow:
    Image → Compress (768px) → Gemini API → Validate → Inventory Check → Result

No offline fallback. If Gemini unavailable → worker enters manually.
That is safer than CRNN which was 20% accurate with fake-high confidence.

Returns dict with these keys:
    coil_id           : str | None  — 10-digit ID
    confidence        : float       — 0.0 to 1.0
    pattern           : str | None  — "HSM2", "CSP", or None
    status            : str         — see STATUS VALUES below
    requires_worker   : bool        — True = show manual entry to worker
    strap_detected    : bool
    duplicate_warning : bool
    quality_issues    : list
    method            : str

STATUS VALUES:
    success           — Gemini read 10 digits cleanly
    strap_blocked     — Black strap covers digits, worker must type
    image_unclear     — Image too blurry/dark, worker should retake
    partial_detection — Gemini found fewer than 10 digits
    quota_exhausted   — All API keys hit daily limit, worker must type
    api_error         — Network/API failure, worker must type
    no_api_key        — GEMINI_API_KEY_1 not set in environment
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.validator import validate_format
from modules.inventory import InventoryManager

# ── Inventory singleton (one instance for whole app lifetime) ────────────────
_inventory = None

def _get_inventory():
    global _inventory
    if _inventory is None:
        _inventory = InventoryManager()
    return _inventory


# ── Check if Gemini API key is configured ───────────────────────────────────
def _has_gemini() -> bool:
    """True if at least one API key is set."""
    for i in range(1, 4):
        if os.environ.get(f"GEMINI_API_KEY_{i}", "").strip():
            return True
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        if os.environ.get(name, "").strip():
            return True
    return False


# ── Main predict function ────────────────────────────────────────────────────
def predict(image_input,
            pattern_hint: Optional[str] = None,
            check_inventory: bool = True,
            image_path: str = "") -> dict:
    """
    Predict coil ID from image using Gemini Vision API.

    Args:
        image_input    : file path (str/Path) OR numpy BGR array OR raw bytes
        pattern_hint   : "HSM2" or "CSP" if known from context
        check_inventory: check for duplicate IDs in inventory
        image_path     : original file path for inventory logging
    """
    import cv2
    import numpy as np

    # ── No API key configured → tell worker to enter manually ───────────────
    if not _has_gemini():
        return {
            "coil_id": None,
            "confidence": 0.0,
            "pattern": None,
            "status": "no_api_key",
            "requires_worker": True,
            "strap_detected": False,
            "duplicate_warning": False,
            "quality_issues": ["GEMINI_API_KEY_1 not set. See .env.example"],
            "method": "none",
        }

    # ── Convert numpy array to bytes for Gemini ──────────────────────────────
    # (compression happens inside gemini_reader._compress_image)
    if isinstance(image_input, np.ndarray):
        ok, buf = cv2.imencode(".jpg", image_input)
        if not ok:
            return {
                "coil_id": None, "confidence": 0.0, "pattern": None,
                "status": "encode_error", "requires_worker": True,
                "strap_detected": False, "duplicate_warning": False,
                "quality_issues": [], "method": "error",
            }
        img_for_gemini = buf.tobytes()
    elif isinstance(image_input, (str, Path)):
        img_for_gemini = str(image_input)
        image_path = image_path or str(image_input)
    elif isinstance(image_input, bytes):
        img_for_gemini = image_input
    else:
        return {
            "coil_id": None, "confidence": 0.0, "pattern": None,
            "status": "invalid_input", "requires_worker": True,
            "strap_detected": False, "duplicate_warning": False,
            "quality_issues": ["Invalid image input type"], "method": "error",
        }

    # ── Call Gemini ──────────────────────────────────────────────────────────
    from modules.gemini_reader import read_coil_id

    ocr = read_coil_id(img_for_gemini, pattern_hint)

    result = {
        "coil_id":          ocr.get("coil_id"),
        "confidence":       ocr.get("confidence", 0.0),
        "pattern":          ocr.get("pattern"),
        "status":           ocr.get("status", "unknown"),
        "requires_worker":  ocr.get("requires_worker", True),
        "strap_detected":   ocr.get("strap_detected", False),
        "duplicate_warning": False,
        "quality_issues":   [],
        "raw_response":     ocr.get("raw_response", ""),
        "method":           "gemini",
    }

    # A selected yard is authoritative. Do not present a wrong-yard read as valid.
    if (pattern_hint and result["coil_id"]
            and result.get("status") == "success"
            and result.get("pattern") != pattern_hint):
        result["status"] = "yard_mismatch"
        result["requires_worker"] = True
        result["quality_issues"].append(
            f"Read does not match selected {pattern_hint} yard format"
        )

    # ── Strap detected → worker must type ───────────────────────────────────
    if result["strap_detected"]:
        result["status"] = "strap_blocked"
        result["requires_worker"] = True
        return result

    # ── Quota exhausted or API error → worker must type ─────────────────────
    if result["status"] in ("quota_exhausted", "api_error", "no_api_key"):
        result["requires_worker"] = True
        return result

    # ── Duplicate check ──────────────────────────────────────────────────────
    if check_inventory and result["coil_id"] and len(str(result["coil_id"])) == 10:
        inv = _get_inventory()
        dup = inv.check_duplicate(result["coil_id"])
        if dup["is_duplicate"]:
            result["duplicate_warning"] = True
            result["duplicate_count"] = dup["count"]
            result["duplicate_first_scan"] = dup["previous_scan"]

    return result


# ── Register confirmed coil ──────────────────────────────────────────────────
def register_coil(coil_id: str,
                  image_path: str = "",
                  worker_verified: bool = False,
                  worker: Optional[dict] = None) -> dict:
    """
    Register a confirmed coil ID into inventory.
    Call this after worker clicks Confirm.
    """
    inv = _get_inventory()
    return inv.register(coil_id, image_path, worker_verified, worker)
