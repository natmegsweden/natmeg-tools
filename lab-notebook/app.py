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
    snapshot_dict,
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
        st.toast(notice[1])


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


def _channel_option_css(missing_channels: set[str]) -> str:
    # React Aria retains the original option key through filtering/virtualization.
    option_keys = ", ".join(
        f'[data-key="{CHANNELS.index(channel)}"]'
        for channel in sorted(missing_channels, key=lambda item: int(item[1:]))
    )
    selector = f'[role="listbox"][aria-label="Bad channels"] [role="option"]:is({option_keys})'
    tag_keys = ", ".join(f'[title="{channel}"]' for channel in sorted(
        missing_channels, key=lambda item: int(item[1:])
    ))
    tag = (
        '.st-key-bad-channel-editor [data-testid="stMultiSelect"] '
        f':is([data-tag], [data-baseweb="tag"]):has(> :is({tag_keys}))'
    )
    return (
        f"<style>{selector} {{ color: color-mix(in srgb, currentColor 75%, {MISSING_CHANNEL_COLOR}) !important; }}"
        f"{selector} * {{ color: inherit !important; }}"
        f"{tag} {{ background-color: color-mix(in srgb, {MISSING_CHANNEL_COLOR} 30%, transparent) !important; "
        f"border: 1px solid color-mix(in srgb, {MISSING_CHANNEL_COLOR} 60%, transparent) !important; "
        "color: inherit !important; }"
        f"{tag} span, {tag} button {{ color: inherit !important; }}"
        f"{tag} svg {{ fill: currentColor !important; }}</style>"
    )


def _open_dialog(name: str) -> None:
    st.session_state["active-dialog"] = name
    st.session_state["bad-channels-dialog-open"] = name == "bad-channels"


def _dismiss_dialog() -> None:
    st.session_state.pop("active-dialog", None)
    st.session_state["bad-channels-dialog-open"] = False


def _clear_bad_channel_context() -> None:
    for key in list(st.session_state):
        if key.startswith("bad-"):
            st.session_state.pop(key, None)
    if st.session_state.get("active-dialog") == "bad-channels":
        _dismiss_dialog()


def render_announcement_banner(db: sqlite3.Connection, timezone_name: str) -> None:
    notes = active_notes(db, ("lab", "lab"))
    with st.container(border=True, key="lab-announcement"):
        with st.container(horizontal=True, vertical_alignment="center"):
            label = "Lab announcement"
            if notes:
                label += f" · {_format_local_time(notes[0]['created_at'], timezone_name)}"
            with st.container(width="stretch"):
                st.caption(label)
            if st.button("Admin", help="Administrator controls", key="toggle-admin-panel"):
                _open_dialog("admin")
        if not notes:
            st.caption("No Lab announcements have been posted.")
        else:
            st.markdown(notes[0]["body"])
            if len(notes) > 1:
                with st.expander("Show previous"):
                    for index, note in enumerate(notes[1:]):
                        if index:
                            st.divider()
                        st.caption(_format_local_time(note["created_at"], timezone_name))
                        st.markdown(note["body"])


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
    if generation is None:
        with st.form("admin-login", clear_on_submit=True, border=False):
            password = st.text_input("Admin password", type="password", key="login-password")
            submitted = st.form_submit_button("Log in", key="admin-login-submit", type="primary")
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
        with st.form("publish-announcement", clear_on_submit=True, border=False):
            announcement = st.text_area("New Lab announcement", key="new-announcement")
            if st.form_submit_button("Publish announcement", key="publish-announcement-submit", type="primary"):
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
        with st.form("update-path-settings", border=False):
            new_recordings_dir = st.text_input("Recordings directory", value=str(config.recordings_dir))
            new_lab_dir = st.text_input("Lab export directory", value=str(config.lab_export_dir))
            new_timezone = st.text_input("Lab timezone (IANA)", value=config.timezone)
            confirmed = True
            if new_recordings_dir != str(config.recordings_dir):
                confirmed = st.checkbox(
                    "I understand that matching project/session names will reuse their existing notebook records; "
                    "old export and recording files will not be moved or deleted.", key="confirm-recordings-path-change"
                )
            if st.form_submit_button("Save settings", type="primary"):
                if not confirmed:
                    st.error("Confirm the Recordings path change before saving.")
                else:
                    try:
                        update_settings(db, generation, new_recordings_dir, new_lab_dir, new_timezone)
                        _flash("Settings saved")
                        for key in ("selected-project", "selected-session"):
                            st.session_state.pop(key, None)
                        _clear_bad_channel_context()
                        st.rerun()
                    except (PermissionError, ValueError, OSError, sqlite3.Error) as error:
                        st.error(str(error))

    with password_tab:
        with st.form("change-admin-password", clear_on_submit=True, border=False):
            old_password = st.text_input("Current password", type="password")
            new_password = st.text_input("New password", type="password")
            new_confirmation = st.text_input("Confirm new password", type="password")
            if st.form_submit_button("Change password", type="primary"):
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
    draft_key = _session_widget_key(project_id, session_id, "draft")
    reason_key = _session_widget_key(project_id, session_id, "reason")
    source_key = _session_widget_key(project_id, session_id, "source")
    source_channels_key = _session_widget_key(project_id, session_id, "source-channels")
    available_channels_key = _session_widget_key(project_id, session_id, "available-channels")
    availability_source_key = _session_widget_key(project_id, session_id, "availability-source")
    expected_key = _session_widget_key(project_id, session_id, "expected")
    conflict_key = _session_widget_key(project_id, session_id, "conflict")
    current = latest_snapshot(db, project_id, session_id)
    current_id = int(current["id"]) if current else None
    st.subheader("Import and source")
    if st.session_state.get(conflict_key):
        st.warning("Bad-channel history changed in another session. Your draft is retained.")
    if st.button("Reload current snapshot", key=_session_widget_key(project_id, session_id, "reload")):
        for key in (draft_key, reason_key, source_key, source_channels_key,
                     available_channels_key, availability_source_key, expected_key, conflict_key):
            st.session_state.pop(key, None)
    if draft_key not in st.session_state:
        json_channels = json.loads(current["channels_json"]) if current else []
        st.session_state[draft_key] = json_channels
        st.session_state[reason_key] = current["reason"] if current else ""
        st.session_state[source_key] = current["source_file"] if current else None
        st.session_state[source_channels_key] = json_channels if current and current["source_file"] else None
        st.session_state[available_channels_key] = (
            json.loads(current["available_channels_json"])
            if current and current["available_channels_json"] is not None else None
        )
        st.session_state[availability_source_key] = current["availability_source_file"] if current else None
        st.session_state[expected_key] = current_id

    if st.button("Get bad from last record", key=_session_widget_key(project_id, session_id, "import")):
        try:
            source, channels, available_channels = read_latest_fif_channels(config, project_id, session_id)
            imported_selection = selection_with_missing_channels(channels, available_channels)
            missing_count = len(set(CHANNELS) - set(available_channels))
            st.session_state[draft_key] = imported_selection
            st.session_state[source_key] = str(source)
            st.session_state[source_channels_key] = imported_selection
            st.session_state[available_channels_key] = available_channels
            st.session_state[availability_source_key] = str(source)
            st.toast(
                f"Loaded {len(channels)} FIF bad channels and automatically selected {missing_count} "
                f"missing channels from {source}; review and save to record this snapshot."
            )
        except (FileNotFoundError, RuntimeError, ValueError) as error:
            st.error(str(error))

    available_channels = st.session_state.get(available_channels_key)
    missing_channels = set(CHANNELS) - set(available_channels) if available_channels is not None else set()
    if st.session_state.get(availability_source_key):
        st.caption(f"Availability source: {st.session_state[availability_source_key]}")
    if missing_channels:
        missing_label = ", ".join(sorted(missing_channels, key=lambda item: int(item[1:])))
        st.caption(f"Missing from recording: {missing_label}. Select only those to exclude.")
        st.markdown(_channel_option_css(missing_channels), unsafe_allow_html=True)
    if current and not current["classification_known"]:
        st.caption(
            "This development snapshot has unknown classification. Saving without a new FIF import "
            "treats the selected channels as manually marked bad."
        )

    st.divider()
    st.subheader("Editable draft")
    with st.container(key="bad-channel-editor"), st.form(
        _session_widget_key(project_id, session_id, "save-form"), border=False,
    ):
        selected_channels = st.multiselect(
            "Bad channels", options=CHANNELS, key=draft_key,
            placeholder="Select channels s1–s136",
            format_func=lambda channel: channel,
        )
        reason = st.text_input("Reason (optional)", key=reason_key)
        imported_channels = st.session_state.get(source_channels_key)
        source_file = st.session_state.get(source_key)
        if imported_channels is not None and sorted(selected_channels, key=lambda item: int(item[1:])) != imported_channels:
            source_file = None
            st.session_state[source_key] = None
        saved = st.form_submit_button(
            "Save bad channels", key=_session_widget_key(project_id, session_id, "submit"),
            disabled=bool(st.session_state.get(conflict_key)), type="primary",
        )
    if saved:
        try:
            snapshot_id = save_channel_snapshot(
                db, config, project_id, session_id, selected_channels, reason,
                source_file, st.session_state.get(expected_key),
                available_channels=available_channels,
                availability_source_file=st.session_state.get(availability_source_key),
            )
            st.session_state[expected_key] = snapshot_id
            st.session_state[conflict_key] = False
            st.session_state[source_channels_key] = list(selected_channels) if source_file else None
            st.toast("Bad-channel check saved")
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
    st.divider()
    st.subheader("Current snapshot")
    if history:
        render_snapshot(history[0], config.timezone)
        if len(history) > 1:
            with st.expander("Show previous bad channels"):
                with st.container(height=230, border=False):
                    for index, snapshot in enumerate(history[1:]):
                        if index:
                            st.divider()
                        render_snapshot(snapshot, config.timezone)
    else:
        st.info("No bad-channel checks have been saved for this session.")


def render_snapshot(row: sqlite3.Row, timezone_name: str) -> None:
    snapshot = snapshot_dict(row)
    st.caption(_format_local_time(snapshot["created_at"], timezone_name))
    if snapshot["classification_known"]:
        st.write(f"Bad channels: {', '.join(snapshot['bad_channels']) or 'none'}")
        st.write(f"Selected missing channels: {', '.join(snapshot['missing_channels']) or 'none'}")
    else:
        st.write("Unknown classification")
        st.write(f"Excluded channels: {', '.join(snapshot['excluded_channels']) or 'none'}")
    if snapshot["reason"]:
        st.caption(f"Reason: {snapshot['reason']}")


@st.dialog("Administrator", width="large", on_dismiss=_dismiss_dialog)
def render_admin_dialog() -> None:
    dialog_db = connect(app_paths().database_path)
    try:
        config = settings_config(dialog_db)
        if config is None:
            st.error("Notebook settings are unavailable. Run local admin password setup.")
            return
        render_admin_panel(dialog_db, config, _admin_generation(dialog_db))
    finally:
        dialog_db.close()


@st.dialog("Session Bad Channels", width="large", on_dismiss=_dismiss_dialog)
def render_bad_channel_dialog(config: AppConfig, project: dict, session: dict) -> None:
    dialog_db = connect(app_paths().database_path)
    try:
        st.caption(f"{project['id']} / {session['id']}")
        render_bad_channel_session(dialog_db, config, project, session)
    finally:
        dialog_db.close()


def render_note_history(db: sqlite3.Connection, scope: tuple[str, str] | None,
                        timezone_name: str) -> None:
    st.divider()
    st.subheader("Notes")
    notes = active_notes(db, scope) if scope else []
    if not notes:
        st.caption("No notes have been saved yet.")
    for index, note in enumerate(notes):
        if index:
            st.divider()
        with st.container(horizontal=True, vertical_alignment="top"):
            with st.container(width="stretch"):
                st.markdown(note["body"])
            with st.container(width="content"):
                st.markdown(
                    f'<div style="text-align:right; white-space:nowrap; color:var(--text-color); opacity:0.65">'
                    f'{html.escape(_format_local_time(note["created_at"], timezone_name))}</div>',
                    unsafe_allow_html=True,
                )


st.set_page_config(page_title="Lab Notebook", layout="wide")
st.markdown(
    f"""<style>
    .block-container {{ padding-top: 4rem !important; padding-bottom: 0.8rem !important; }}
    [data-testid="stHeader"] {{ height: 1.5rem; }}
    h1 {{ font-size: 1.65rem !important; margin: 0 !important; padding: 0 !important; }}
    h2, h3 {{ margin-top: 0.35rem !important; padding-top: 0 !important; }}
    .st-key-lab-announcement[data-testid="stVerticalBlock"] {{
        border: 2px solid {ANNOUNCEMENT_BORDER_COLOR} !important;
        border-radius: 0.6rem !important;
    }}
    .st-key-project-panel[data-testid="stVerticalBlock"],
    .st-key-recording-panel[data-testid="stVerticalBlock"] {{
        border-radius: 0.6rem;
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

    settings_error = None
    try:
        validate_settings(config.recordings_dir, config.lab_export_dir, config.timezone)
    except (ValueError, OSError) as error:
        settings_error = str(error)
    if settings_error:
        st.warning(f"Configured paths need attention: {settings_error}")

    _show_flash()
    render_announcement_banner(db, config.timezone)
    if settings_error:
        projects = []
    else:
        projects = discover(config)["projects"]

    left, right = st.columns([3, 7])
    with left, st.container(border=True, key="project-panel"):
        st.subheader("Project")
        project_id = st.selectbox(
            "Project", [item["id"] for item in projects], index=0 if projects else None,
            placeholder="Select a project", key="selected-project",
        )
        project = next((item for item in projects if item["id"] == project_id), None)
        sessions = project["sessions"] if project else []
        if project:
            with st.form("add-project-note", clear_on_submit=True, border=False):
                body = st.text_area("Append a project note", height=100)
                if st.form_submit_button("Add project note", type="primary"):
                    try:
                        add_note(db, config, ("project", project["id"]), body)
                        st.toast("Project note saved")
                    except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
                        st.error(str(error))
        else:
            st.caption("Select a project to add notes.")
        render_note_history(db, ("project", project_id) if project else None, config.timezone)

    with right, st.container(border=True, key="recording-panel"):
        st.subheader("Recording")
        session_id = st.selectbox(
            "Recording session", [item["id"] for item in sessions], index=0 if sessions else None,
            placeholder="Select a session", key="selected-session",
        )
        session = next((item for item in sessions if item["id"] == session_id), None)
        context = (str(config.recordings_dir), project_id, session_id)
        previous_context = st.session_state.get("bad-channel-context")
        if previous_context is not None and previous_context != context:
            if previous_context[0] != context[0]:
                _clear_bad_channel_context()
            elif st.session_state.get("active-dialog") == "bad-channels":
                _dismiss_dialog()
        st.session_state["bad-channel-context"] = context
        # Keep unsaved dialog drafts through full-script reruns and dismissal.
        for key in list(st.session_state):
            if key.startswith(("bad-draft-", "bad-reason-")):
                st.session_state[key] = st.session_state[key]
        if session and project:
            version_key = f"recording-note-version-{project['id']}-{session['id']}"
            version = st.session_state.get(version_key, 0)
            note_key = f"recording-note-{project['id']}-{session['id']}-{version}"
            body = st.text_area("Append a recording note", key=note_key, height=100)
            with st.container(horizontal=True):
                add_clicked = st.button("Add recording note", key="add-recording-note", type="primary")
                bad_clicked = st.button("Bad channels", key="open-bad-channels")
            if add_clicked:
                try:
                    add_note(db, config, ("session", f"{project['id']}/{session['id']}"), body)
                    st.session_state[version_key] = version + 1
                    _flash("Recording note saved")
                    st.rerun()
                except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
                    st.error(str(error))
            if bad_clicked:
                _open_dialog("bad-channels")
        else:
            st.caption("Select a project and recording session to add notes.")
        render_note_history(
            db, ("session", f"{project_id}/{session_id}") if session and project else None, config.timezone,
        )

    active_dialog = st.session_state.get("active-dialog")
    if active_dialog == "admin":
        render_admin_dialog()
    elif active_dialog == "bad-channels" and project and session:
        render_bad_channel_dialog(config, project, session)
finally:
    db.close()
