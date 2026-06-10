"""
Coil ID Inventory Manager — detects duplicate IDs.

Stores detected coil IDs with timestamps. When a new detection comes in,
checks if this ID was already scanned. If so, returns a duplicate warning.

Storage: Simple JSON file (no database needed).
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

# DATA_DIR env var lets cloud deployments point to a persistent volume
_DEFAULT_DB = Path(os.environ.get("DATA_DIR",
    str(Path(__file__).resolve().parent.parent / "data"))) / "inventory.json"


_write_lock = threading.Lock()   # prevents 2 workers corrupting JSON simultaneously


class InventoryManager:
    """Track scanned coil IDs to detect duplicates."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = Path(db_path) if db_path else _DEFAULT_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._data = self._load()

    def _load(self) -> dict:
        if self.db_path.exists():
            try:
                with open(self.db_path, "r") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                return {"coils": {}}
        return {"coils": {}}

    def _save(self):
        with _write_lock:
            # reload latest from disk first (another worker may have written)
            if self.db_path.exists():
                try:
                    with open(self.db_path, "r") as f:
                        latest = json.load(f)
                    # merge: keep all existing entries + add new ones from self
                    for cid, val in self._data["coils"].items():
                        latest["coils"][cid] = val
                    self._data = latest
                except (json.JSONDecodeError, OSError):
                    pass
            with open(self.db_path, "w") as f:
                json.dump(self._data, f, indent=2, default=str)

    def check_duplicate(self, coil_id: str) -> dict:
        """
        Check if coil_id already exists in inventory.

        Returns:
            is_duplicate: bool
            previous_scan: dict | None  (timestamp, image_path of first scan)
            count: int  (how many times this ID has been scanned)
        """
        if not coil_id or len(coil_id) != 10:
            return {"is_duplicate": False, "previous_scan": None, "count": 0}

        entry = self._data["coils"].get(coil_id)
        if entry:
            return {
                "is_duplicate": True,
                "previous_scan": entry["scans"][0],  # first scan
                "count": len(entry["scans"]),
            }
        return {"is_duplicate": False, "previous_scan": None, "count": 0}

    def register(self, coil_id: str, image_path: str = "",
                 worker_verified: bool = False,
                 worker: Optional[dict] = None) -> dict:
        """
        Register a new coil scan. Returns duplicate info.
        """
        if not coil_id or len(coil_id) != 10:
            return {"registered": False, "reason": "Invalid coil ID"}

        scan_record = {
            "timestamp": datetime.now().isoformat(),
            "image_path": str(image_path),
            "worker_verified": worker_verified,
            "worker_id": (worker or {}).get("worker_id", ""),
            "worker_name": (worker or {}).get("full_name", ""),
            "shift": (worker or {}).get("shift", ""),
            "yard": (worker or {}).get("yard", ""),
        }

        dup_info = self.check_duplicate(coil_id)

        if coil_id not in self._data["coils"]:
            self._data["coils"][coil_id] = {"scans": []}

        self._data["coils"][coil_id]["scans"].append(scan_record)
        self._save()

        return {
            "registered": True,
            "is_duplicate": dup_info["is_duplicate"],
            "total_scans": len(self._data["coils"][coil_id]["scans"]),
        }

    def get_all(self) -> dict:
        """Return all registered coils."""
        return self._data["coils"]

    def get_count(self) -> int:
        """Total unique coils in inventory."""
        return len(self._data["coils"])

    def clear(self):
        """Clear all inventory data."""
        self._data = {"coils": {}}
        self._save()
