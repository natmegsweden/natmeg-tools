"""SQLite persistence, authorization, session snapshots, and JSON exports."""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import AppConfig, validate_settings

Scope = tuple[str, str]
CHANNELS = tuple(f"s{i}" for i in range(1, 137))
CHANNEL_RE = re.compile(r"s0*(\d+)", re.IGNORECASE)
SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1
LOGIN_FAILURE_LIMIT = 5
LOGIN_COOLDOWN_SECONDS = 300


class SnapshotConflict(RuntimeError):
    """Raised when another session saved a newer snapshot."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError(f"Database path cannot be a symlink: {path}")
    db = sqlite3.connect(path, timeout=10)
    if os.name != "nt":
        path.chmod(0o600)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        _initialize_database(db)
    except Exception:
        db.rollback()
        db.close()
        raise
    return db


def _initialize_database(db: sqlite3.Connection) -> None:
    """Serialize schema creation and additive migrations without changing history."""
    db.executescript("""
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY, scope_type TEXT NOT NULL, scope_id TEXT NOT NULL,
            body TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bad_channel_events (
            id INTEGER PRIMARY KEY, scope_type TEXT NOT NULL, scope_id TEXT NOT NULL,
            channel TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', source_file TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bad_channel_resolutions (
            id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL REFERENCES bad_channel_events(id),
            resolved_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS app_settings (
            id INTEGER PRIMARY KEY CHECK(id=1), recordings_dir TEXT NOT NULL,
            lab_export_dir TEXT NOT NULL, timezone TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS admin_credentials (
            id INTEGER PRIMARY KEY CHECK(id=1), salt BLOB NOT NULL, password_hash BLOB NOT NULL,
            scrypt_n INTEGER NOT NULL, scrypt_r INTEGER NOT NULL, scrypt_p INTEGER NOT NULL,
            generation INTEGER NOT NULL, failed_attempts INTEGER NOT NULL DEFAULT 0,
            locked_until REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS admin_security_state (
            id INTEGER PRIMARY KEY CHECK(id=1), generation INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bad_channel_snapshots (
            id INTEGER PRIMARY KEY, project_id TEXT NOT NULL, session_id TEXT NOT NULL,
            channels_json TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', source_file TEXT,
            created_at TEXT NOT NULL, available_channels_json TEXT,
            availability_source_file TEXT, classification_known INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS notes_scope ON notes(scope_type, scope_id, id);
        CREATE INDEX IF NOT EXISTS bad_snapshots_session ON bad_channel_snapshots(project_id, session_id, id);
        CREATE TRIGGER IF NOT EXISTS bad_snapshots_no_update BEFORE UPDATE ON bad_channel_snapshots
        BEGIN SELECT RAISE(ABORT, 'bad-channel snapshots are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS bad_snapshots_no_delete BEFORE DELETE ON bad_channel_snapshots
        BEGIN SELECT RAISE(ABORT, 'bad-channel snapshots are immutable'); END;
    """)
    columns = {row["name"] for row in db.execute("PRAGMA table_info(bad_channel_snapshots)")}
    for name, definition in (
        ("available_channels_json", "TEXT"),
        ("availability_source_file", "TEXT"),
        ("classification_known", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in columns:
            db.execute(f"ALTER TABLE bad_channel_snapshots ADD COLUMN {name} {definition}")
    db.execute("INSERT OR IGNORE INTO admin_security_state(id, generation) VALUES(1,0)")
    db.commit()


def settings_row(db: sqlite3.Connection) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM app_settings WHERE id=1").fetchone()


def settings_config(db: sqlite3.Connection) -> AppConfig | None:
    row = settings_row(db)
    if row is None:
        return None
    return AppConfig(Path(row["recordings_dir"]), Path(row["lab_export_dir"]), row["timezone"])


def has_admin(db: sqlite3.Connection) -> bool:
    return db.execute("SELECT 1 FROM admin_credentials WHERE id=1").fetchone() is not None


def _password_hash(password: str, salt: bytes, n: int = SCRYPT_N,
                   r: int = SCRYPT_R, p: int = SCRYPT_P) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)


def _validate_password_confirmation(password: str, confirmation: str) -> None:
    if password != confirmation:
        raise ValueError("Passwords do not match")


def initialize_admin(db: sqlite3.Connection, password: str,
                     confirmation: str, recordings_dir: str | None = None,
                     lab_export_dir: str | None = None,
                     timezone_name: str = "Europe/Stockholm") -> int:
    _validate_password_confirmation(password, confirmation)
    existing_settings = settings_row(db)
    config = None
    if existing_settings is None:
        if recordings_dir is None or lab_export_dir is None:
            raise ValueError("Recordings and Lab export paths are required for first-time setup")
        config = validate_settings(recordings_dir, lab_export_dir, timezone_name)
    salt = secrets.token_bytes(16)
    digest = _password_hash(password, salt)
    try:
        db.execute("BEGIN IMMEDIATE")
        if has_admin(db):
            raise ValueError("Admin setup has already been completed")
        current_settings = settings_row(db)
        if current_settings is None:
            if config is None:
                raise ValueError("First-time setup requires valid paths")
            db.execute("""INSERT INTO app_settings(id, recordings_dir, lab_export_dir, timezone, updated_at)
                VALUES(1,?,?,?,?)""", (str(config.recordings_dir), str(config.lab_export_dir),
                                          config.timezone, utc_now()))
        state = db.execute("SELECT generation FROM admin_security_state WHERE id=1").fetchone()
        generation = int(state["generation"]) + 1
        db.execute("UPDATE admin_security_state SET generation=? WHERE id=1", (generation,))
        db.execute("""INSERT INTO admin_credentials
            (id, salt, password_hash, scrypt_n, scrypt_r, scrypt_p, generation)
            VALUES(1,?,?,?,?,?,?)""", (salt, digest, SCRYPT_N, SCRYPT_R, SCRYPT_P, generation))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return generation


def verify_admin(db: sqlite3.Connection, password: str, now: float | None = None) -> int | None:
    timestamp = time.time() if now is None else now
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute("SELECT * FROM admin_credentials WHERE id=1").fetchone()
        if row is None or row["locked_until"] > timestamp:
            db.commit()
            return None
        digest = _password_hash(password, row["salt"], row["scrypt_n"], row["scrypt_r"], row["scrypt_p"])
        if hmac.compare_digest(digest, row["password_hash"]):
            db.execute("UPDATE admin_credentials SET failed_attempts=0, locked_until=0 WHERE id=1")
            db.commit()
            return int(row["generation"])
        failures = int(row["failed_attempts"]) + 1
        locked_until = timestamp + LOGIN_COOLDOWN_SECONDS if failures >= LOGIN_FAILURE_LIMIT else 0
        db.execute("UPDATE admin_credentials SET failed_attempts=?, locked_until=? WHERE id=1",
                   (0 if locked_until else failures, locked_until))
        db.commit()
        return None
    except Exception:
        db.rollback()
        raise


def require_admin(db: sqlite3.Connection, generation: int | None) -> None:
    row = db.execute("SELECT generation FROM admin_credentials WHERE id=1").fetchone()
    state = db.execute("SELECT generation FROM admin_security_state WHERE id=1").fetchone()
    if (generation is None or row is None or state is None
            or int(row["generation"]) != generation or int(state["generation"]) != generation):
        raise PermissionError("Admin authentication is required")


def change_admin_password(db: sqlite3.Connection, generation: int,
                          old_password: str, new_password: str, confirmation: str) -> int:
    db.execute("BEGIN IMMEDIATE")
    try:
        require_admin(db, generation)
        row = db.execute("SELECT * FROM admin_credentials WHERE id=1").fetchone()
        old_hash = _password_hash(old_password, row["salt"], row["scrypt_n"], row["scrypt_r"], row["scrypt_p"])
        if not hmac.compare_digest(old_hash, row["password_hash"]):
            raise ValueError("Current password is incorrect")
        _validate_password_confirmation(new_password, confirmation)
        salt = secrets.token_bytes(16)
        digest = _password_hash(new_password, salt)
        new_generation = int(row["generation"]) + 1
        db.execute("UPDATE admin_security_state SET generation=? WHERE id=1", (new_generation,))
        db.execute("""UPDATE admin_credentials SET salt=?, password_hash=?, scrypt_n=?, scrypt_r=?, scrypt_p=?,
            generation=?, failed_attempts=0, locked_until=0 WHERE id=1""",
            (salt, digest, SCRYPT_N, SCRYPT_R, SCRYPT_P, new_generation))
        db.commit()
        return new_generation
    except Exception:
        db.rollback()
        raise


def reset_admin_password(db: sqlite3.Connection, confirmed: bool) -> None:
    if not confirmed:
        raise ValueError("Pass --confirm-reset to clear admin credentials")
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("DELETE FROM admin_credentials WHERE id=1")
        db.execute("UPDATE admin_security_state SET generation=generation+1 WHERE id=1")
        db.commit()
    except Exception:
        db.rollback()
        raise


def update_settings(db: sqlite3.Connection, generation: int, recordings_dir: str,
                    lab_export_dir: str, timezone_name: str) -> AppConfig:
    require_admin(db, generation)
    config = validate_settings(recordings_dir, lab_export_dir, timezone_name)
    db.execute("BEGIN IMMEDIATE")
    try:
        require_admin(db, generation)
        cursor = db.execute("""UPDATE app_settings SET recordings_dir=?, lab_export_dir=?, timezone=?, updated_at=?
            WHERE id=1""", (str(config.recordings_dir), str(config.lab_export_dir), config.timezone, utc_now()))
        if cursor.rowcount == 0:
            raise ValueError("Settings have not been initialized")
        db.commit()
        return config
    except Exception:
        db.rollback()
        raise


def discover(config: AppConfig) -> dict[str, list[dict[str, Any]]]:
    projects: list[dict[str, Any]] = []
    root = config.recordings_dir.resolve()
    if root.is_dir() and not root.is_symlink():
        for project_dir in sorted(root.iterdir(), key=lambda p: p.name.casefold()):
            if project_dir.is_symlink() or not project_dir.is_dir():
                continue
            sessions = [
                {"id": session.name, "path": session}
                for session in sorted(project_dir.iterdir(), key=lambda p: p.name.casefold())
                if not session.is_symlink() and session.is_dir()
            ]
            projects.append({"id": project_dir.name, "path": project_dir, "sessions": sessions})
    return {"projects": projects}


def active_notes(db: sqlite3.Connection, scope: Scope) -> list[sqlite3.Row]:
    return db.execute("SELECT id, body, created_at FROM notes WHERE scope_type=? AND scope_id=? ORDER BY id DESC", scope).fetchall()


def add_note(db: sqlite3.Connection, config: AppConfig, scope: Scope, body: str,
             admin_generation: int | None = None) -> None:
    if scope[0] not in {"lab", "project", "session"}:
        raise ValueError("Invalid note scope")
    text = body.strip()
    if not text:
        raise ValueError("Note cannot be empty")
    db.execute("BEGIN IMMEDIATE")
    try:
        if scope[0] == "lab":
            require_admin(db, admin_generation)
        db.execute("INSERT INTO notes(scope_type, scope_id, body, created_at) VALUES(?,?,?,?)",
                   (*scope, text, utc_now()))
        db.commit()
    except Exception:
        db.rollback()
        raise
    try:
        _persist_export(db, config, scope)
    except OSError as error:
        raise RuntimeError(f"Note was saved, but JSON export failed: {error}") from error


def latest_snapshot(db: sqlite3.Connection, project_id: str, session_id: str) -> sqlite3.Row | None:
    return db.execute("""SELECT * FROM bad_channel_snapshots WHERE project_id=? AND session_id=?
        ORDER BY created_at DESC, id DESC LIMIT 1""", (project_id, session_id)).fetchone()


def snapshot_history(db: sqlite3.Connection, project_id: str, session_id: str) -> list[sqlite3.Row]:
    return db.execute("""SELECT * FROM bad_channel_snapshots WHERE project_id=? AND session_id=?
        ORDER BY created_at DESC, id DESC""", (project_id, session_id)).fetchall()


def _channel_id(name: str, strict: bool = True) -> str | None:
    matches = {int(match.group(1)) for match in CHANNEL_RE.finditer(str(name))}
    if not matches:
        if strict:
            raise ValueError(f"Unsupported channel name: {name}")
        return None
    if len(matches) != 1:
        if not strict:
            return None
        raise ValueError(f"Ambiguous channel name: {name}")
    number = matches.pop()
    if not 1 <= number <= len(CHANNELS):
        if not strict:
            return None
        raise ValueError(f"Unsupported channel name: {name}")
    return f"s{number}"


def _canonical_channels(channels: list[str] | tuple[str, ...]) -> list[str]:
    normalized: set[str] = set()
    for channel in channels:
        name = _channel_id(str(channel))
        normalized.add(name)
    return sorted(normalized, key=lambda channel: int(channel[1:]))


def _available_channels(names: list[str] | tuple[str, ...]) -> list[str]:
    found = set()
    for name in names:
        channel = _channel_id(str(name), strict=False)
        if channel is not None:
            found.add(channel)
    return sorted(found, key=lambda channel: int(channel[1:]))


def selection_with_missing_channels(channels: list[str] | tuple[str, ...],
                                    available_channels: list[str] | tuple[str, ...] | None) -> list[str]:
    selection = set(_canonical_channels(channels))
    if available_channels is not None:
        present = set(_canonical_channels(available_channels))
        selection.update(set(CHANNELS) - present)
    return sorted(selection, key=lambda channel: int(channel[1:]))


def save_channel_snapshot(db: sqlite3.Connection, config: AppConfig, project_id: str,
                          session_id: str, channels: list[str] | tuple[str, ...],
                          reason: str, source_file: str | None,
                          expected_latest_id: int | None, *,
                          available_channels: list[str] | tuple[str, ...] | None = None,
                          availability_source_file: str | None = None) -> int:
    """Append a classified selection; callers retain availability across manual edits.

    None availability means a manual selection (all bad); an empty list means
    known empty availability (all selected channels missing). Exact-selection
    provenance in source_file is independent of availability_source_file.
    """
    canonical = _canonical_channels(channels)
    available = _canonical_channels(available_channels) if available_channels is not None else None
    if available is None and availability_source_file is not None:
        raise ValueError("Availability source requires known available channels")
    try:
        db.execute("BEGIN IMMEDIATE")
        current = latest_snapshot(db, project_id, session_id)
        current_id = int(current["id"]) if current else None
        if current_id != expected_latest_id:
            raise SnapshotConflict("Bad-channel history changed in another session. Reload before saving.")
        cursor = db.execute("""INSERT INTO bad_channel_snapshots
            (project_id, session_id, channels_json, reason, source_file, created_at,
             available_channels_json, availability_source_file, classification_known)
            VALUES(?,?,?,?,?,?,?,?,1)""", (project_id, session_id, json.dumps(canonical),
                reason.strip(), source_file, utc_now(),
                json.dumps(available) if available is not None else None, availability_source_file))
        snapshot_id = int(cursor.lastrowid)
        db.commit()
    except Exception:
        db.rollback()
        raise
    try:
        _persist_export(db, config, ("session", f"{project_id}/{session_id}"))
    except OSError as error:
        raise RuntimeError(f"Snapshot was saved, but JSON export failed: {error}") from error
    return snapshot_id


def snapshot_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Serialize a saved snapshot for UI display and version-2 JSON/CSV exports.

    Legacy rows retain their exclusion selection but have unknown categories.
    Missing channels are selected exclusions absent from known availability,
    not every channel absent from the recording.
    """
    excluded = json.loads(row["channels_json"])
    known = bool(row["classification_known"])
    available = (json.loads(row["available_channels_json"])
                 if known and row["available_channels_json"] is not None else None)
    present = set(available) if available is not None else set(excluded)
    return {
        "snapshot_id": int(row["id"]),
        "project_id": row["project_id"],
        "session_id": row["session_id"],
        "excluded_channels": excluded,
        "bad_channels": [channel for channel in excluded if channel in present] if known else None,
        "missing_channels": [channel for channel in excluded if channel not in present] if known else None,
        "available_channels": available,
        "availability_source_file": row["availability_source_file"],
        "classification_known": known,
        "reason": row["reason"],
        "source_file": row["source_file"],
        "created_at": row["created_at"],
    }


def _all_snapshots(db: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = db.execute("""SELECT * FROM bad_channel_snapshots
        ORDER BY created_at, id""").fetchall()
    return [snapshot_dict(row) for row in rows]


def _json_snapshots_by_session(snapshots: list[dict[str, Any]]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Group aggregate JSON history by project and session identifiers."""
    projects: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for snapshot in snapshots:
        project_sessions = projects.setdefault(snapshot["project_id"], {})
        project_sessions.setdefault(snapshot["session_id"], []).append({
            key: value for key, value in snapshot.items()
            if key not in {"project_id", "session_id", "available_channels"}
        })
    return projects


def _json_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return a snapshot without internal channel-availability details."""
    return {key: value for key, value in snapshot.items() if key != "available_channels"}


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise FileExistsError(f"Refusing to replace symlink export: {path}")
    if path.exists():
        try:
            with path.open(encoding="utf-8") as existing_file:
                owner = json.load(existing_file).get("generated_by")
        except (OSError, ValueError, AttributeError):
            owner = None
        if owner != "lab-notebook":
            raise FileExistsError(f"Refusing to overwrite an existing file not generated by Lab Notebook: {path}")
    data = {"generated_by": "lab-notebook", **data}
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _recording_dir(config: AppConfig, path: Path) -> Path:
    root = config.recordings_dir.resolve()
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise OSError(f"Refusing to export outside the Recordings directory: {path}") from error
    return resolved


def _persist_export(db: sqlite3.Connection, config: AppConfig, scope: Scope) -> None:
    discovered = discover(config)
    if scope[0] == "lab":
        _atomic_json(config.lab_export_dir / "annotations_lab.json", {
            "exported_at": utc_now(), "notes": [dict(row) for row in active_notes(db, ("lab", "lab"))],
            "projects": [project["id"] for project in discovered["projects"]],
        })
        return
    if scope[0] == "project":
        project = next((item for item in discovered["projects"] if item["id"] == scope[1]), None)
        if project is None:
            raise FileNotFoundError(f"Project directory no longer exists: {scope[1]}")
        project_dir = _recording_dir(config, project["path"])
        _atomic_json(project_dir / "annotations_project.json", {
            "exported_at": utc_now(), "project_id": project["id"],
            "notes": [dict(row) for row in active_notes(db, ("project", project["id"]))],
        })
        return
    if scope[0] == "session" and "/" in scope[1]:
        project_id, session_id = scope[1].split("/", 1)
        project = next((item for item in discovered["projects"] if item["id"] == project_id), None)
        session = next((item for item in project["sessions"] if item["id"] == session_id), None) if project else None
        if session is None:
            raise FileNotFoundError(f"Recording session directory no longer exists: {scope[1]}")
        session_dir = _recording_dir(config, session["path"])
        history = [_json_snapshot(snapshot_dict(row))
                   for row in snapshot_history(db, project_id, session_id)]
        current = history[0] if history else None
        _atomic_json(session_dir / "annotations_recording.json", {
            "exported_at": utc_now(), "project_id": project_id, "session_id": session_id,
            "notes": [dict(row) for row in active_notes(db, ("session", scope[1]))],
            "bad_channel_snapshot_schema_version": 2,
            "current_bad_channels": current["bad_channels"] if current else [],
            "current_missing_channels": current["missing_channels"] if current else [],
            "current_excluded_channels": current["excluded_channels"] if current else [],
            "current_classification_known": current["classification_known"] if current else False,
            "bad_channel_snapshots": history,
        })
        _atomic_json(config.lab_export_dir / "bad_channel_records.json", {
            "exported_at": utc_now(), "schema_version": 2,
            "records": _json_snapshots_by_session(_all_snapshots(db)),
        })
        return
    raise ValueError(f"Unsupported export scope: {scope}")


def _format_csv_cell(value: str) -> str:
    text = str(value)
    stripped = text.lstrip(" \t\r\n")
    if stripped and stripped[0] in "=+-@":
        return "'" + text
    return text


def export_bads_csv(db: sqlite3.Connection, generation: int) -> str:
    db.execute("BEGIN")
    try:
        require_admin(db, generation)
        snapshots = _all_snapshots(db)
        db.commit()
    except Exception:
        db.rollback()
        raise
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(["snapshot_id", "project_id", "session_id", "created_at_utc",
                     "bad_channels", "missing_channels", "excluded_channels", "classification_known",
                     "available_channels", "reason", "source_file", "availability_source_file"])
    for item in snapshots:
        writer.writerow([
            item["snapshot_id"], _format_csv_cell(item["project_id"]),
            _format_csv_cell(item["session_id"]), item["created_at"],
            _format_csv_cell(", ".join(item["bad_channels"] or [])),
            _format_csv_cell(", ".join(item["missing_channels"] or [])),
            _format_csv_cell(", ".join(item["excluded_channels"])),
            str(item["classification_known"]).lower(),
            _format_csv_cell(", ".join(item["available_channels"] or [])),
            _format_csv_cell(item["reason"]),
            _format_csv_cell(item["source_file"] or ""),
            _format_csv_cell(item["availability_source_file"] or ""),
        ])
    return output.getvalue()


def read_latest_fif_channels(config: AppConfig, project_id: str, session_id: str,
                             reader: Callable[..., Any] | None = None) -> tuple[Path, list[str], list[str]]:
    folder = config.recordings_dir / project_id / session_id
    fif_files = list(folder.glob("*.fif")) if folder.is_dir() and not folder.is_symlink() else []
    if not fif_files:
        raise FileNotFoundError(f"No .fif file found in {folder}")
    latest = max(fif_files, key=lambda path: path.stat().st_mtime)
    if reader is None:
        import mne
        reader = mne.io.read_info
    try:
        info = reader(str(latest), verbose="ERROR")
        names = list(info["bads"])
        available_names = list(info["ch_names"])
    except Exception as error:
        raise RuntimeError(f"Could not read {latest}: {error}") from error
    channels = _canonical_channels(names)
    return latest, channels, _available_channels(available_names)
