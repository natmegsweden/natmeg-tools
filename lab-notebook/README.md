# Lab Notebook

A local, single-admin Streamlit app for lab announcements, project/session notes, and session-scoped bad-channel snapshots. SQLite is the source of truth; JSON/CSV files are generated exports. Recording files are read-only.

## Install and launch

Requires Python 3.11 or later.

```sh
uv sync --project lab-notebook --extra test
uv run --project lab-notebook streamlit run lab-notebook/app.py --server.address 127.0.0.1
```

The app binds to localhost only. Do not expose it to a network; the shared password protects app actions, not OS/filesystem access.

On first launch, create the shared admin password and enter the actual `Recordings` directory, Lab export directory, and IANA timezone. Passwords have no minimum length; the confirmation field must match, and an empty password is accepted when confirmed. The default timezone is `Europe/Stockholm`. The database and settings live in the platform app-data directory (private permissions on Linux). On Linux this is `$XDG_DATA_HOME/lab-notebook` or `~/.local/share/lab-notebook`; set `LAB_NOTEBOOK_DATA_DIR` before launch to override it.

The recordings path should point directly to the folder containing project directories:

```text
/data/Recordings/
  ProjectA/
    2026-01-15/
      recording_raw.fif
```

The Lab export directory must be separate from the `Recordings` tree. Project and session identities are discovered from folders when the app loads. If recording paths change, matching project/session names reuse existing notebook records; old files are not moved or deleted. Existing `config.toml` files are no longer read or imported. An existing SQLite database retains its notes/channel history, but requires fresh admin/path setup if it has no credentials/settings.

## Features

- Lab announcements are append-only. Everyone sees the latest announcement with a local timestamp; previous announcements are expandable. Only the admin can publish announcements or change settings.
- Project and Recording notes remain available without login.
- Bad Channels are session-only complete-set snapshots using `s1` through `s136`. Every explicit save records a timestamped check, including unchanged and empty selections. The latest snapshot is current; older snapshots are scrollable under “Show previous bad channels.”
- “Get bad from last record” extracts an `s` plus digits channel ID from the newest session `.fif` names (for example, `VendorChannelS72suffix` becomes `s72`). Unmatched or ambiguous bad-channel names are rejected. Channels absent from that FIF's channel list are automatically selected as bad, greyed in the multiselect, and listed in muted gray beneath it. Review and explicitly save the resulting snapshot.
- Admin **Export bads CSV** downloads all historical session snapshots, including archived sessions. It excludes notes and announcements.
- Automatic JSON exports are written atomically to `lab.json` and `bad_channel_records.json` in the Lab export directory, `project.json` in each project directory, and `recording.json` in each session directory. Symlinked project/session folders are ignored; pre-existing JSON files without the app ownership marker are not overwritten. Export failures are reported while committed database changes are retained.

### UI colors

In `lab-notebook/app.py`, adjust `ANNOUNCEMENT_BORDER_COLOR` for the shared Lab announcement frame and `MISSING_CHANNEL_COLOR` for channels absent from the scanned FIF. Both accept CSS color values such as hex codes.

## Admin Recovery

The admin session expires after 30 minutes of inactivity. To reset the password locally while preserving settings and notebook data:

```sh
uv run --project lab-notebook python -m lab_notebook.admin reset-password --confirm-reset
```

The next local browser visit will prompt for a new password. Anyone with OS/database access can perform this reset.

## Tests

```sh
uv run --project lab-notebook --extra test pytest lab-notebook/tests
```
