"""Application data paths and persisted settings validation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def default_app_data_dir() -> Path:
    override = os.environ.get("LAB_NOTEBOOK_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    elif os.uname().sysname == "Darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / "lab-notebook"


@dataclass(frozen=True)
class AppPaths:
    app_data_dir: Path

    @property
    def database_path(self) -> Path:
        return self.app_data_dir / "lab-notebook.sqlite3"


@dataclass(frozen=True)
class AppConfig:
    recordings_dir: Path
    lab_export_dir: Path
    timezone: str


def app_paths(app_data_dir: Path | None = None) -> AppPaths:
    requested = (app_data_dir or default_app_data_dir()).expanduser()
    requested.mkdir(mode=0o700, parents=True, exist_ok=True)
    if requested.is_symlink():
        raise ValueError(f"App data directory cannot be a symlink: {requested}")
    directory = requested.resolve()
    if os.name != "nt":
        directory.chmod(0o700)
    return AppPaths(directory)


def validate_settings(recordings_dir: str | Path, lab_export_dir: str | Path,
                      timezone: str) -> AppConfig:
    recordings = Path(recordings_dir).expanduser()
    exports = Path(lab_export_dir).expanduser()
    if not recordings.is_absolute() or not exports.is_absolute():
        raise ValueError("Recordings and Lab export paths must be absolute")
    recordings = recordings.resolve()
    exports = exports.resolve()
    if not recordings.is_dir() or not os.access(recordings, os.R_OK | os.X_OK):
        raise ValueError(f"Recordings directory is missing or unreadable: {recordings}")
    if recordings == exports or recordings in exports.parents or exports in recordings.parents:
        raise ValueError("Lab export directory must be separate from the Recordings directory")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(f"Unknown IANA timezone: {timezone}") from error
    exports.mkdir(parents=True, exist_ok=True)
    if not exports.is_dir() or not os.access(exports, os.W_OK | os.X_OK):
        raise ValueError(f"Lab export directory is not writable: {exports}")
    return AppConfig(recordings, exports, timezone)
