"""
Minimal, pure-pathlib file/folder browser widget for Streamlit.

A browser tab can't hand a native OS file-picker dialog a path on the
*server's* filesystem — file_uploader only ever gets you an upload, not a
server-side path. This walks the filesystem from the Streamlit process
instead (plain os/pathlib, no OS-specific separators or drive assumptions
hardcoded), so it behaves the same whether the app is hosted on macOS or
Windows.
"""
from __future__ import annotations

import os
import re
import string
from pathlib import Path

import streamlit as st


def _safe_key(text: str) -> str:
    """Sanitizes an arbitrary path into a Streamlit widget-key-safe suffix.
    Used to namespace the directory-listing widgets by the directory they're
    currently showing — reusing one fixed key across different directories
    lets Streamlit carry over a stale selected value from the previous
    listing (the old option string just isn't in the new options anymore,
    but the widget silently keeps displaying it)."""
    return re.sub(r"[^A-Za-z0-9]+", "_", text)[-60:]


def _list_dir(path: Path, file_extensions: tuple[str, ...] | None) -> tuple[list[Path], list[Path]]:
    try:
        entries = sorted(path.iterdir(), key=lambda p: p.name.lower())
    except (PermissionError, FileNotFoundError, NotADirectoryError, OSError):
        return [], []
    dirs = [p for p in entries if p.is_dir() and not p.name.startswith(".")]
    files: list[Path] = []
    if file_extensions is not None:
        files = [p for p in entries if p.is_file() and p.suffix.lower() in file_extensions]
    return dirs, files


def _windows_drives() -> list[str]:
    if os.name != "nt":
        return []
    return [f"{d}:\\" for d in string.ascii_uppercase if Path(f"{d}:\\").exists()]


def _set_session_value(state_key: str, value: str) -> None:
    """The only safe way to change an *already-instantiated* widget's value:
    mutate session_state from an on_click callback, which Streamlit runs
    before the script re-executes and the widget is recreated. Setting
    st.session_state[key] directly inside a plain `if st.button(...):`
    branch — after the same-keyed widget already rendered earlier in this
    run — raises 'cannot be modified after widget ... is instantiated'."""
    st.session_state[state_key] = value


def path_picker(key_prefix: str, current_value: str, mode: str,
                 file_extensions: tuple[str, ...] | None = None,
                 label: str = "Path") -> str:
    """Text input (type a path directly) + a "Browse" expander (navigate and
    pick one). mode is "file" or "dir". Returns the current path string.

    Must NOT be placed inside an st.form — the Browse buttons need to rerun
    immediately to navigate, and plain st.button is not allowed inside
    st.form."""
    text_key = f"{key_prefix}_text"
    if text_key not in st.session_state:
        st.session_state[text_key] = current_value

    value = st.text_input(
        label, key=text_key,
        help="Type an absolute or relative path, or pick one with Browse below.",
    )

    browse_key = f"{key_prefix}_cwd"
    with st.expander("📂 Browse"):
        if browse_key not in st.session_state:
            seed = Path(value).expanduser()
            seed_dir = seed if seed.is_dir() else seed.parent
            st.session_state[browse_key] = str(seed_dir) if seed_dir.exists() else str(Path.home())

        cwd = Path(st.session_state[browse_key])
        if not cwd.exists():
            cwd = Path.home()
            st.session_state[browse_key] = str(cwd)

        st.code(str(cwd), language=None)

        nav1, nav2, nav3 = st.columns([1, 1, 3])
        if nav1.button("⬆ Up", key=f"{key_prefix}_up", disabled=cwd.parent == cwd):
            st.session_state[browse_key] = str(cwd.parent)
            st.rerun()
        if nav2.button("🏠 Home", key=f"{key_prefix}_home"):
            st.session_state[browse_key] = str(Path.home())
            st.rerun()

        drives = _windows_drives()
        if drives:
            current_drive = f"{cwd.drive}\\" if cwd.drive else drives[0]
            drive_index = drives.index(current_drive) if current_drive in drives else 0
            drive = nav3.selectbox("Drive", drives, index=drive_index,
                                    key=f"{key_prefix}_drive", label_visibility="collapsed")
            if drive != current_drive and st.button(f"Go to {drive}", key=f"{key_prefix}_go_drive"):
                st.session_state[browse_key] = drive
                st.rerun()

        dirs, files = _list_dir(cwd, file_extensions if mode == "file" else None)
        options = [f"\U0001F4C1 {d.name}" for d in dirs]
        if mode == "file":
            options += [f"\U0001F4C4 {f.name}" for f in files]

        if not options:
            empty_msg = "(no subfolders here)" if mode == "dir" else "(no subfolders or matching files here)"
            st.caption(empty_msg)
        else:
            dir_tag = _safe_key(str(cwd))
            picked = st.selectbox("Contents", options, key=f"{key_prefix}_entry_{dir_tag}")
            is_dir_pick = picked.startswith("\U0001F4C1 ")
            target = cwd / picked[2:].strip()

            oc1, oc2 = st.columns(2)
            if is_dir_pick:
                if oc1.button("Open folder", key=f"{key_prefix}_open_{dir_tag}"):
                    st.session_state[browse_key] = str(target)
                    st.rerun()
                if mode == "dir":
                    oc2.button(
                        "Select this folder", key=f"{key_prefix}_select_sub_{dir_tag}",
                        on_click=_set_session_value, args=(text_key, str(target)),
                    )
            else:
                oc1.button(
                    "Select this file", key=f"{key_prefix}_select_file_{dir_tag}",
                    on_click=_set_session_value, args=(text_key, str(target)),
                )

        if mode == "dir":
            st.button(
                f"Use current folder ({cwd})", key=f"{key_prefix}_use_cwd",
                on_click=_set_session_value, args=(text_key, str(cwd)),
            )

    return value
