"""Streamlit entry point for the Lab Notebook."""

from __future__ import annotations

import html
import json
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import streamlit as st

from lab_notebook.config import AppConfig, app_paths, validate_settings
from lab_notebook.notebook import (
    CHANNELS,
    SnapshotConflict,
    active_notes,
    add_note,
    change_admin_password,
    connect,
    discover,
    export_bads_csv,
    has_admin,
    initialize_admin,
    latest_snapshot,
    read_latest_fif_channels,
    require_admin,
    save_channel_snapshot,
    selection_with_missing_channels,
    settings_config,
    settings_row,
    snapshot_history,
    update_settings,
    verify_admin,
)

ADMIN_TIMEOUT_SECONDS = 30 * 60
# Adjust these two UI colors to change the announcement frame and missing-channel options.
ANNOUNCEMENT_BORDER_COLOR = "#F28C28"
MISSING_CHANNEL_COLOR = "#8B9098"


def _clear_admin_session() -> None:
    for key in ("admin_generation", "admin_last_activity"):
        st.session_state.pop(key, None)


def _flash(message: str, kind: str = "success") -> None:
    st.session_state["lab-notebook-flash"] = (kind, message)


def _show_flash() -> None:
    notice = st.session_state.pop("lab-notebook-flash", None)
    if notice:
        getattr(st, notice[0])(notice[1])


def _admin_generation(db: sqlite3.Connection) -> int | None:
    generation = st.session_state.get("admin_generation")
    if generation is None:
        return None
    now = time.time()
    if now - st.session_state.get("admin_last_activity", 0) >= ADMIN_TIMEOUT_SECONDS:
        _clear_admin_session()
        return None
    try:
        require_admin(db, generation)
    except PermissionError:
        _clear_admin_session()
        return None
    st.session_state.admin_last_activity = now
    return int(generation)


def _format_local_time(timestamp: str, timezone_name: str) -> str:
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    try:
        timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        timezone = ZoneInfo("UTC")
    return parsed.astimezone(timezone).strftime("%Y-%m-%d %H:%M %Z")


def _channel_option_css(missing_channels: set[str], selected_channels: list[str]) -> str:
    rules = []
    for channel in sorted(missing_channels, key=lambda item: int(item[1:])):
        option = int(channel[1:])
        selector = f'[data-baseweb="popover"] [role="listbox"] [role="option"]:nth-child({option})'
        rules.append(f"{selector}, {selector} * {{ color: {MISSING_CHANNEL_COLOR} !important; }}")
    selected_missing = [index for index, channel in enumerate(selected_channels, start=1)
                        if channel in missing_channels]
    for index in selected_missing:
        selector = f'[data-testid="stMultiSelect"] [data-baseweb="tag"]:nth-of-type({index})'
        rules.append(f"{selector}, {selector} * {{ color: {MISSING_CHANNEL_COLOR} !important; }}")
    return f"<style>{''.join(rules)}</style>"


def render_announcement_banner(db: sqlite3.Connection, timezone_name: str) -> None:
    notes = active_notes(db, ("lab", "lab"))
    if not notes:
        st.info("No Lab announcements have been posted.")
        return
    latest = notes[0]
    with st.container(border=True):
        st.markdown('<span id="lab-announcement-frame-anchor"></span>', unsafe_allow_html=True)
        st.caption(f"Lab announcement · {_format_local_time(latest['created_at'], timezone_name)}")
        st.markdown(latest["body"])
        if len(notes) > 1:
            with st.expander("Show previous"):
                for note in notes[1:]:
                    st.caption(_format_local_time(note["created_at"], timezone_name))
                    st.markdown(note["body"])
                    st.divider()


def render_setup(db: sqlite3.Connection) -> None:
    existing_settings = settings_row(db)
    st.header("Initialize Lab Notebook administrator")
    if existing_settings:
        st.info("Admin credentials were reset. Existing notebook data and settings are preserved.")
    else:
        st.info("Create the shared admin password and configure the paths before using the notebook.")
    with st.form("initial-admin-setup", clear_on_submit=True):
        recordings_dir = lab_dir = timezone_name = None
        if existing_settings is None:
            recordings_dir = st.text_input("Recordings directory", value="~/Recordings", key="setup-recordings-dir")
            lab_dir = st.text_input("Lab export directory", value="~/Lab", key="setup-lab-export-dir")
            timezone_name = st.text_input("Lab timezone (IANA)", value="Europe/Stockholm", key="setup-timezone")
            st.caption("Choose the actual Recordings folder, containing project/session folders.")
        password = st.text_input("Create admin password", type="password", key="setup-password")
        confirmation = st.text_input("Confirm admin password", type="password", key="setup-password-confirm")
        submitted = st.form_submit_button("Create administrator", key="setup-create-admin")
    if submitted:
        try:
            generation = initialize_admin(
                db, password, confirmation, recordings_dir, lab_dir,
                timezone_name or "Europe/Stockholm",
            )
            st.session_state.admin_generation = generation
            st.session_state.admin_last_activity = time.time()
            _flash("Administrator initialized")
            st.rerun()
        except (OSError, ValueError, sqlite3.Error) as error:
            st.error(str(error))
    st.stop()


def render_admin_panel(db: sqlite3.Connection, config: AppConfig,
                       generation: int | None) -> None:
    st.subheader("Administrator")
    if generation is None:
        with st.form("admin-login", clear_on_submit=True):
            password = st.text_input("Admin password", type="password", key="login-password")
            submitted = st.form_submit_button("Log in", key="admin-login-submit")
        if submitted:
            matched = verify_admin(db, password)
            if matched is None:
                st.error("Unable to log in. Check the password or wait before trying again.")
            else:
                st.session_state.admin_generation = matched
                st.session_state.admin_last_activity = time.time()
                _flash("Logged in as administrator")
                st.rerun()
        return

    try:
        require_admin(db, generation)
        csv_data = export_bads_csv(db, generation)
    except PermissionError:
        _clear_admin_session()
        st.warning("Admin session expired. Log in again.")
        return

    announcements_tab, qc_tab, settings_tab, password_tab = st.tabs(
        ["Announcements", "Quality control", "Settings", "Password"]
    )
    with announcements_tab:
        with st.form("publish-announcement", clear_on_submit=True):
            announcement = st.text_area("New Lab announcement", key="new-announcement")
            if st.form_submit_button("Publish announcement", key="publish-announcement-submit"):
                try:
                    add_note(db, config, ("lab", "lab"), announcement, admin_generation=generation)
                    _flash("Announcement published")
                    st.rerun()
                except (PermissionError, ValueError, RuntimeError, OSError, sqlite3.Error) as error:
                    st.error(str(error))

    with qc_tab:
        st.caption("Exports all session bad-channel snapshots, including resolved and archived sessions.")
        st.download_button(
            "Export bads CSV", csv_data,
            file_name=f"bad-channels-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv",
            mime="text/csv", key="export-bads-csv",
        )

    with settings_tab:
        with st.form("update-path-settings"):
            new_recordings_dir = st.text_input("Recordings directory", value=str(config.recordings_dir))
            new_lab_dir = st.text_input("Lab export directory", value=str(config.lab_export_dir))
            new_timezone = st.text_input("Lab timezone (IANA)", value=config.timezone)
            changed = (new_recordings_dir != str(config.recordings_dir)
                       or new_lab_dir != str(config.lab_export_dir)
                       or new_timezone != config.timezone)
            confirmed = True
            if new_recordings_dir != str(config.recordings_dir):
                confirmed = st.checkbox(
                    "I understand that matching project/session names will reuse their existing notebook records; "
                    "old export and recording files will not be moved or deleted.", key="confirm-recordings-path-change"
                )
            if st.form_submit_button("Save settings"):
                if not confirmed:
                    st.error("Confirm the Recordings path change before saving.")
                else:
                    try:
                        update_settings(db, generation, new_recordings_dir, new_lab_dir, new_timezone)
                        _flash("Settings saved")
                        for key in ("selected-project", "selected-session"):
                            st.session_state.pop(key, None)
                        st.rerun()
                    except (PermissionError, ValueError, OSError, sqlite3.Error) as error:
                        st.error(str(error))

    with password_tab:
        with st.form("change-admin-password", clear_on_submit=True):
            old_password = st.text_input("Current password", type="password")
            new_password = st.text_input("New password", type="password")
            new_confirmation = st.text_input("Confirm new password", type="password")
            if st.form_submit_button("Change password"):
                try:
                    new_generation = change_admin_password(
                        db, generation, old_password, new_password, new_confirmation
                    )
                    st.session_state.admin_generation = new_generation
                    st.session_state.admin_last_activity = time.time()
                    _flash("Password changed")
                    st.rerun()
                except (PermissionError, ValueError, sqlite3.Error) as error:
                    st.error(str(error))

    if st.button("Log out", key="admin-logout"):
        _clear_admin_session()
        st.rerun()


def _session_widget_key(project_id: str, session_id: str, field: str) -> str:
    return f"bad-{field}-{project_id}-{session_id}"


def render_bad_channel_session(db: sqlite3.Connection, config: AppConfig,
                              project: dict | None, session: dict | None) -> None:
    if project is None or session is None:
        st.info("Select a project and recording session above to manage bad channels.")
        return
    project_id, session_id = project["id"], session["id"]
    scope_key = f"{project_id}/{session_id}"
    draft_key = _session_widget_key(project_id, session_id, "draft")
    reason_key = _session_widget_key(project_id, session_id, "reason")
    source_key = _session_widget_key(project_id, session_id, "source")
    source_channels_key = _session_widget_key(project_id, session_id, "source-channels")
    available_channels_key = _session_widget_key(project_id, session_id, "available-channels")
    expected_key = _session_widget_key(project_id, session_id, "expected")
    conflict_key = _session_widget_key(project_id, session_id, "conflict")
    current = latest_snapshot(db, project_id, session_id)
    current_id = int(current["id"]) if current else None
    if draft_key not in st.session_state:
        json_channels = json.loads(current["channels_json"]) if current else []
        st.session_state[draft_key] = json_channels
        st.session_state[reason_key] = current["reason"] if current else ""
        st.session_state[source_key] = current["source_file"] if current else None
        st.session_state[source_channels_key] = json_channels if current and current["source_file"] else None
        st.session_state[available_channels_key] = None
        st.session_state[expected_key] = current_id

    if st.session_state.get(conflict_key):
        st.warning("Bad-channel history changed in another session. Your draft is retained.")
        if st.button("Reload current snapshot", key=_session_widget_key(project_id, session_id, "reload")):
            for key in (draft_key, reason_key, source_key, source_channels_key,
                        available_channels_key, expected_key, conflict_key):
                st.session_state.pop(key, None)
            st.rerun()

    if st.button("Get bad from last record", key=_session_widget_key(project_id, session_id, "import")):
        try:
            source, channels, available_channels = read_latest_fif_channels(config, project_id, session_id)
            imported_selection = selection_with_missing_channels(channels, available_channels)
            missing_count = len(set(CHANNELS) - set(available_channels))
            st.session_state[draft_key] = imported_selection
            st.session_state[source_key] = str(source)
            st.session_state[source_channels_key] = imported_selection
            st.session_state[available_channels_key] = available_channels
            _flash(
                f"Loaded {len(channels)} FIF bad channels and automatically selected {missing_count} "
                f"missing channels from {source}; review and save to record this snapshot."
            )
            st.rerun()
        except (FileNotFoundError, RuntimeError, ValueError) as error:
            st.error(str(error))

    history = snapshot_history(db, project_id, session_id)
    current = history[0] if history else None
    if current:
        st.caption(f"Current snapshot · {_format_local_time(current['created_at'], config.timezone)}")

    available_channels = st.session_state.get(available_channels_key)
    missing_channels = set(CHANNELS) - set(available_channels) if available_channels is not None else set()
    if missing_channels:
        missing_label = ", ".join(sorted(missing_channels, key=lambda item: int(item[1:])))
        st.markdown(
            f'<div style="color:{MISSING_CHANNEL_COLOR}">Missing channels automatically marked bad: '
            f'{html.escape(missing_label)}</div>',
            unsafe_allow_html=True,
        )
        draft_channels = st.session_state.get(draft_key, [])
        st.markdown(_channel_option_css(missing_channels, draft_channels), unsafe_allow_html=True)

    with st.form(_session_widget_key(project_id, session_id, "save-form")):
        selected_channels = st.multiselect(
            "Bad channels", options=CHANNELS, key=draft_key,
            placeholder="Select channels s1–s136",
        )
        reason = st.text_input("Reason (optional)", key=reason_key)
        imported_channels = st.session_state.get(source_channels_key)
        source_file = st.session_state.get(source_key)
        if imported_channels is not None and sorted(selected_channels, key=lambda item: int(item[1:])) != imported_channels:
            source_file = None
            st.session_state[source_key] = None
        saved = st.form_submit_button(
            "Save bad channels", key=_session_widget_key(project_id, session_id, "submit"),
            disabled=bool(st.session_state.get(conflict_key)),
        )
    if saved:
        try:
            snapshot_id = save_channel_snapshot(
                db, config, project_id, session_id, selected_channels, reason,
                source_file, st.session_state.get(expected_key),
            )
            st.session_state[expected_key] = snapshot_id
            st.session_state[conflict_key] = False
            st.session_state[source_channels_key] = list(selected_channels) if source_file else None
            _flash("Bad-channel check saved")
            st.rerun()
        except SnapshotConflict as error:
            st.session_state[conflict_key] = True
            st.error(str(error))
        except RuntimeError as error:
            committed = latest_snapshot(db, project_id, session_id)
            st.session_state[expected_key] = int(committed["id"]) if committed else None
            st.error(str(error))
        except (ValueError, OSError, sqlite3.Error) as error:
            st.error(str(error))

    history = snapshot_history(db, project_id, session_id)
    if history:
        st.markdown("**Current bad channels**")
        channels = json.loads(history[0]["channels_json"])
        st.write(f"Bad channels: {', '.join(channels)}" if channels else "Bad channels: none")
        if len(history) > 1:
            with st.expander("Show previous bad channels"):
                with st.container(height=230, border=True):
                    for snapshot in history[1:]:
                        previous_channels = json.loads(snapshot["channels_json"])
                        label = ", ".join(previous_channels) if previous_channels else "none"
                        st.markdown(
                            f"{_format_local_time(snapshot['created_at'], config.timezone)} "
                            f"| Bad channels: {label}"
                        )
    else:
        st.info("No bad-channel checks have been saved for this session.")


st.set_page_config(page_title="Lab Notebook", layout="wide")
st.markdown(
    f"""<style>
    div[data-testid="stVerticalBlockBorderWrapper"]:has(#lab-announcement-frame-anchor) {{
        border: 2px solid {ANNOUNCEMENT_BORDER_COLOR} !important;
        border-radius: 0.6rem !important;
    }}
    </style>""",
    unsafe_allow_html=True,
)
st.title("Lab Notebook")
try:
    paths = app_paths()
    db = connect(paths.database_path)
except (OSError, ValueError, sqlite3.Error) as error:
    st.error(f"Could not open the notebook database: {error}")
    st.stop()

try:
    if not has_admin(db):
        render_setup(db)

    config = settings_config(db)
    if config is None:
        st.error("Notebook settings are unavailable. Run local admin password setup.")
        st.stop()

    generation = _admin_generation(db)
    _, title_right = st.columns([8, 1])
    with title_right:
        if st.button("⚙ Admin", help="Administrator controls", key="toggle-admin-panel"):
            st.session_state.admin_panel_open = not st.session_state.get("admin_panel_open", False)
            st.rerun()

    settings_error = None
    try:
        validate_settings(config.recordings_dir, config.lab_export_dir, config.timezone)
    except (ValueError, OSError) as error:
        settings_error = str(error)
    if settings_error:
        st.warning(f"Configured paths need attention: {settings_error}")

    if st.session_state.get("admin_panel_open", False):
        render_admin_panel(db, config, generation)

    _show_flash()
    render_announcement_banner(db, config.timezone)
    if settings_error:
        projects = []
    else:
        projects = discover(config)["projects"]

    selector_project, selector_session = st.columns(2)
    project_id = selector_project.selectbox(
        "Project", [item["id"] for item in projects], index=0 if projects else None,
        placeholder="Select a project", key="selected-project",
    )
    project = next((item for item in projects if item["id"] == project_id), None)
    sessions = project["sessions"] if project else []
    session_id = selector_session.selectbox(
        "Recording session", [item["id"] for item in sessions], index=0 if sessions else None,
        placeholder="Select a session", key="selected-session",
    )
    session = next((item for item in sessions if item["id"] == session_id), None)

    notes_tab, bad_tab = st.tabs(["Notes", "Bad Channels"], key="main-tabs", on_change="rerun")
    with notes_tab:
        left, right = st.columns(2)
        with left:
            st.subheader(f"Project: {project_id}" if project else "Project")
            if project:
                for note in active_notes(db, ("project", project["id"])):
                    st.markdown(f"**{_format_local_time(note['created_at'], config.timezone)}**\n\n{note['body']}")
                with st.form("add-project-note", clear_on_submit=True):
                    body = st.text_area("Append a project note")
                    if st.form_submit_button("Add project note"):
                        try:
                            add_note(db, config, ("project", project["id"]), body)
                            st.success("Project note saved")
                        except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
                            st.error(str(error))
            else:
                st.info("Select a project above.")
        with right:
            st.subheader(f"Recording: {session_id}" if session else "Recording")
            if session and project:
                for note in active_notes(db, ("session", f"{project['id']}/{session['id']}")):
                    st.markdown(f"**{_format_local_time(note['created_at'], config.timezone)}**\n\n{note['body']}")
                with st.form("add-session-note", clear_on_submit=True):
                    body = st.text_area("Append a recording note")
                    if st.form_submit_button("Add recording note"):
                        try:
                            add_note(db, config, ("session", f"{project['id']}/{session['id']}"), body)
                            st.success("Recording note saved")
                        except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
                            st.error(str(error))
            else:
                st.info("Select a project and recording session above.")

    with bad_tab:
        st.subheader(f"Bad Channels · {project_id} / {session_id}" if project and session else "Bad Channels")
        if session and project:
            render_bad_channel_session(db, config, project, session)
        else:
            st.info("Select a project and recording session above to manage bad channels.")
finally:
    db.close()
