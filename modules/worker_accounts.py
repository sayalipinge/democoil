"""Worker self-registration and persistent login sessions."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional


DATA_DIR = Path(os.environ.get(
    "DATA_DIR",
    str(Path(__file__).resolve().parent.parent / "data"),
))
ACCOUNTS_PATH = DATA_DIR / "workers.json"

VALID_SHIFTS = {"General Shift", "A Shift", "B Shift", "C Shift"}
VALID_YARDS = {"HSM Yard", "CSP Yard"}
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{3,24}$")
_lock = threading.Lock()


def _pin_hash(pin: str, salt_hex: str) -> str:
    salt = bytes.fromhex(salt_hex)
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, 150_000).hex()


def _validate_pin(pin: str):
    if not (len(pin) == 4 and pin.isdigit()):
        raise ValueError("PIN must be exactly 4 digits")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class WorkerAccountManager:
    def __init__(self, path: Optional[str] = None):
        self.path = Path(path) if path else ACCOUNTS_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict:
        if not self.path.exists():
            return {"workers": {}}
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data.get("workers"), dict) else {"workers": {}}
        except (OSError, json.JSONDecodeError):
            return {"workers": {}}

    def _save(self, data: dict) -> None:
        temp = self.path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        temp.replace(self.path)

    @staticmethod
    def _public(worker: dict) -> dict:
        return {
            "worker_id": worker["worker_id"],
            "full_name": worker["full_name"],
            "shift": worker["shift"],
            "yard": worker["yard"],
            "created_at": worker["created_at"],
        }

    @staticmethod
    def _add_session(worker: dict, token: str) -> None:
        token_hash = _token_hash(token)
        sessions = worker.get("session_hashes")
        if not isinstance(sessions, list):
            sessions = []
            old_hash = worker.get("session_hash")
            if old_hash:
                sessions.append(old_hash)
        sessions = [s for s in sessions if isinstance(s, str) and s != token_hash]
        sessions.append(token_hash)
        worker["session_hashes"] = sessions[-8:]
        worker["session_hash"] = token_hash

    @staticmethod
    def _has_session(worker: dict, token_hash: str) -> bool:
        sessions = worker.get("session_hashes")
        if isinstance(sessions, list) and any(
            hmac.compare_digest(str(session), token_hash) for session in sessions
        ):
            return True
        return hmac.compare_digest(worker.get("session_hash", ""), token_hash)

    @staticmethod
    def _validate_profile(
        worker_id: str,
        full_name: str,
        pin: str,
        shift: str,
        yard: str,
    ) -> tuple[str, str]:
        clean_id = worker_id.strip().upper()
        clean_name = " ".join(full_name.strip().split())
        if not _ID_RE.fullmatch(clean_id):
            raise ValueError("Worker ID must be 3-24 letters, numbers, dot, dash or underscore")
        if len(clean_name) < 2 or len(clean_name) > 60:
            raise ValueError("Enter a valid full name")
        _validate_pin(pin)
        if shift not in VALID_SHIFTS:
            raise ValueError("Select a valid shift")
        if yard not in VALID_YARDS:
            raise ValueError("Select a valid work location")
        return clean_id, clean_name

    def register(
        self,
        worker_id: str,
        full_name: str,
        pin: str,
        shift: str,
        yard: str,
    ) -> dict:
        clean_id, clean_name = self._validate_profile(
            worker_id, full_name, pin, shift, yard
        )
        with _lock:
            data = self._load()
            if clean_id in data["workers"]:
                raise ValueError("That Worker ID already exists")
            salt = secrets.token_hex(16)
            token = secrets.token_urlsafe(32)
            worker = {
                "worker_id": clean_id,
                "full_name": clean_name,
                "pin_salt": salt,
                "pin_hash": _pin_hash(pin, salt),
                "session_hash": "",
                "session_hashes": [],
                "shift": shift,
                "yard": yard,
                "created_at": datetime.now().isoformat(),
            }
            self._add_session(worker, token)
            data["workers"][clean_id] = worker
            self._save(data)
        return {"token": token, "worker": self._public(worker)}

    def login(self, worker_id: str, pin: str) -> dict:
        clean_id = worker_id.strip().upper()
        with _lock:
            data = self._load()
            worker = data["workers"].get(clean_id)
            if not worker:
                raise ValueError("Worker ID or PIN is incorrect")
            candidate = _pin_hash(pin, worker["pin_salt"])
            if not hmac.compare_digest(candidate, worker["pin_hash"]):
                raise ValueError("Worker ID or PIN is incorrect")
            token = secrets.token_urlsafe(32)
            self._add_session(worker, token)
            self._save(data)
        return {"token": token, "worker": self._public(worker)}

    def authenticate(self, token: str) -> Optional[dict]:
        if not token:
            return None
        token_hash = _token_hash(token)
        data = self._load()
        for worker in data["workers"].values():
            if self._has_session(worker, token_hash):
                return self._public(worker)
        return None

    def update_context(self, token: str, shift: str, yard: str) -> dict:
        if shift not in VALID_SHIFTS or yard not in VALID_YARDS:
            raise ValueError("Select a valid shift and work location")
        token_hash = _token_hash(token)
        with _lock:
            data = self._load()
            for worker in data["workers"].values():
                if self._has_session(worker, token_hash):
                    worker["shift"] = shift
                    worker["yard"] = yard
                    self._save(data)
                    return self._public(worker)
        raise ValueError("Session expired. Log in again.")

    def update_profile(self, token: str, full_name: str) -> dict:
        clean_name = " ".join(full_name.strip().split())
        if len(clean_name) < 2 or len(clean_name) > 60:
            raise ValueError("Enter a valid full name")
        token_hash = _token_hash(token)
        with _lock:
            data = self._load()
            for worker in data["workers"].values():
                if self._has_session(worker, token_hash):
                    worker["full_name"] = clean_name
                    self._save(data)
                    return self._public(worker)
        raise ValueError("Session expired. Log in again.")

    def change_pin(self, token: str, old_pin: str, new_pin: str) -> dict:
        _validate_pin(old_pin)
        _validate_pin(new_pin)
        token_hash = _token_hash(token)
        with _lock:
            data = self._load()
            for worker in data["workers"].values():
                if self._has_session(worker, token_hash):
                    old_hash = _pin_hash(old_pin, worker["pin_salt"])
                    if not hmac.compare_digest(old_hash, worker["pin_hash"]):
                        raise ValueError("Old PIN is incorrect")
                    salt = secrets.token_hex(16)
                    worker["pin_salt"] = salt
                    worker["pin_hash"] = _pin_hash(new_pin, salt)
                    self._save(data)
                    return self._public(worker)
        raise ValueError("Session expired. Log in again.")

    def reset_pin(self, worker_id: str, new_pin: str) -> dict:
        clean_id = worker_id.strip().upper()
        _validate_pin(new_pin)
        with _lock:
            data = self._load()
            worker = data["workers"].get(clean_id)
            if not worker:
                raise ValueError("Worker ID not found")
            salt = secrets.token_hex(16)
            worker["pin_salt"] = salt
            worker["pin_hash"] = _pin_hash(new_pin, salt)
            self._save(data)
            return self._public(worker)

    def list_workers(self) -> list[dict]:
        data = self._load()
        return [self._public(worker) for worker in data["workers"].values()]
