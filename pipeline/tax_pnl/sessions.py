"""Ephemeral session storage for uploaded Tax P&L files.

Each session lives in <UPLOAD_ROOT>/<session_id>/ and contains:
  - meta.json          session metadata (label, created_at, TTL)
  - column_mapping.json  (optional) GenericAdapter column mapping
  - <uploaded files>   the actual xlsx / csv files
  - parsed.json        cached parse result (so we don't re-parse on every page load)

Sessions expire 24 hours after creation. A daily sweeper removes expired
sessions. The sweeper is wired into pipeline.scheduler.

Storage root is OUTSIDE data/tax_pnl/ so uploaded files can never
pollute your own Tax P&L pipeline.
"""
from __future__ import annotations

import json
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from pipeline.runtime_paths import data_root

# Stored OUTSIDE data/tax_pnl/ so uploaded files never pollute your own pipeline.
# NOTE: this used to be a hardcoded absolute path
# (/Users/hkc21/portfolio-tracker/data/tax_pnl_uploads), which only ever
# worked on one specific machine and would break on any other host,
# CI runner, or serverless deployment (Vercel). data_root() resolves to
# <project>/data locally, or /tmp/portfolio-tracker-data on Vercel
# (see pipeline/runtime_paths.py).
UPLOAD_ROOT = data_root() / "tax_pnl_uploads"
MAX_FILES_PER_SESSION = 10
MAX_FILE_BYTES = 20 * 1024 * 1024  # 20 MB
SESSION_TTL_HOURS = 24

# Allowed file types
ALLOWED_EXTENSIONS = {".xlsx", ".xlsm", ".csv"}
ALLOWED_MIME = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel.sheet.macroEnabled.12",
    "text/csv",
    "application/csv",
    "text/plain",  # some browsers send this for csv
    "application/octet-stream",  # some browsers don't sniff
}

# Excel magic bytes: PK\x03\x04
_XLSX_MAGIC = b"PK\x03\x04"


@dataclass
class SessionMeta:
    session_id: str
    label: str
    created_at: float
    expires_at: float
    source_files: list[str] = field(default_factory=list)
    detected_brokers: list[str] = field(default_factory=list)
    column_mapping: dict = field(default_factory=dict)

    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, s: str) -> "SessionMeta":
        d = json.loads(s)
        return cls(**d)


def _session_dir(session_id: str) -> Path:
    # Defensive: reject anything that isn't a UUID
    try:
        uuid.UUID(session_id)
    except ValueError:
        raise ValueError(f"invalid session id: {session_id!r}")
    return UPLOAD_ROOT / session_id


def create_session(label: Optional[str] = None) -> SessionMeta:
    """Create a new session and return its metadata. The session dir is created."""
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    sid = uuid.uuid4().hex
    now = time.time()
    meta = SessionMeta(
        session_id=sid,
        label=label or f"Uploaded {datetime.fromtimestamp(now).strftime('%Y-%m-%d %H:%M')}",
        created_at=now,
        expires_at=now + SESSION_TTL_HOURS * 3600,
    )
    sd = _session_dir(sid)
    sd.mkdir(parents=True, exist_ok=False)
    (sd / "meta.json").write_text(meta.to_json())
    return meta


def get_session(session_id: str) -> Optional[SessionMeta]:
    """Return session metadata, or None if it doesn't exist or is expired."""
    try:
        sd = _session_dir(session_id)
    except ValueError:
        return None
    meta_path = sd / "meta.json"
    if not meta_path.exists():
        return None
    try:
        meta = SessionMeta.from_json(meta_path.read_text())
    except Exception:
        return None
    if meta.is_expired():
        return None
    return meta


def save_uploaded_file(session_id: str, src_path: Path, original_name: str) -> Path:
    """Move a temp-uploaded file into the session dir, with a sanitised name."""
    sd = _session_dir(session_id)
    safe = Path(original_name).name  # strip any path components
    safe = "".join(c for c in safe if c.isalnum() or c in "._- ") or "upload.bin"
    dest = sd / safe
    # If the name collides, suffix with a counter
    counter = 1
    while dest.exists():
        stem, _, ext = safe.rpartition(".")
        if not stem:
            dest = sd / f"{safe}.{counter}"
        else:
            dest = sd / f"{stem}_{counter}.{ext}"
        counter += 1
    shutil.move(str(src_path), str(dest))
    return dest


def list_session_files(session_id: str) -> list[Path]:
    sd = _session_dir(session_id)
    if not sd.exists():
        return []
    return [p for p in sd.iterdir()
            if p.is_file() and p.suffix.lower() in ALLOWED_EXTENSIONS]


def set_column_mapping(session_id: str, mapping: dict) -> None:
    meta = get_session(session_id)
    if meta is None:
        return
    meta.column_mapping = mapping
    (_session_dir(session_id) / "meta.json").write_text(meta.to_json())


def cache_parsed(session_id: str, data: dict) -> None:
    """Cache the parsed NormalizedTaxPnl (serialised) so re-renders don't
    have to re-parse the xlsx."""
    sd = _session_dir(session_id)
    (sd / "parsed.json").write_text(json.dumps(data, indent=2, default=str))


def get_cached_parsed(session_id: str, max_age_sec: int = 600) -> Optional[dict]:
    sd = _session_dir(session_id)
    p = sd / "parsed.json"
    if not p.exists():
        return None
    age = time.time() - p.stat().st_mtime
    if age > max_age_sec:
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def update_meta(session_id: str, **fields) -> None:
    meta = get_session(session_id)
    if meta is None:
        return
    for k, v in fields.items():
        if hasattr(meta, k):
            setattr(meta, k, v)
    (_session_dir(session_id) / "meta.json").write_text(meta.to_json())


def delete_session(session_id: str) -> bool:
    """Delete a session. Returns True if the dir existed, False if not.
    Raises ValueError if the session_id isn't a valid UUID (defensive)."""
    sd = _session_dir(session_id)  # raises ValueError on bad UUID
    if sd.exists():
        shutil.rmtree(sd, ignore_errors=True)
        return True
    return False


def sweep_expired_sessions() -> tuple[int, int]:
    """Remove all expired sessions. Returns (deleted_count, total_count)."""
    if not UPLOAD_ROOT.exists():
        return (0, 0)
    deleted = 0
    total = 0
    for child in UPLOAD_ROOT.iterdir():
        if not child.is_dir():
            continue
        total += 1
        meta_path = child / "meta.json"
        if not meta_path.exists():
            # No metadata = orphan, delete it
            shutil.rmtree(child, ignore_errors=True)
            deleted += 1
            continue
        try:
            meta = SessionMeta.from_json(meta_path.read_text())
        except Exception:
            shutil.rmtree(child, ignore_errors=True)
            deleted += 1
            continue
        if meta.is_expired():
            shutil.rmtree(child, ignore_errors=True)
            deleted += 1
    return (deleted, total)


def validate_upload(filename: str, content: bytes) -> Optional[str]:
    """Return an error string if the upload is invalid, else None.

    Checks: extension, size, xlsx magic bytes (if xlsx).
    """
    if not filename:
        return "missing filename"
    if "/" in filename or "\\" in filename or filename.startswith("."):
        return "invalid filename"
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return f"unsupported file type: {ext!r} (allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))})"
    if len(content) > MAX_FILE_BYTES:
        return f"file too large: {len(content):,} bytes (max {MAX_FILE_BYTES:,})"
    if len(content) == 0:
        return "file is empty"
    if ext in (".xlsx", ".xlsm") and not content.startswith(_XLSX_MAGIC):
        return "file is not a valid xlsx (bad magic bytes)"
    return None
