from fastapi import (
    FastAPI, Request, WebSocket,
    UploadFile, File, Form
)
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from contextlib import asynccontextmanager
from pathlib import Path
import ast
import asyncio
import base64
import importlib.util
import json
import os
import re
import signal
import struct
import subprocess
import socket
import sys
import tempfile
import threading
import time
from collections import deque
import uuid
import shutil
import zipfile

# Used by the /lint endpoint (inline diagnostics) to flag things
# beyond plain syntax errors - unused imports, undefined names,
# unused variables, etc. Optional import so the app still starts
# (with linting simply falling back to syntax-only checks) if it
# hasn't been installed yet.
try:
    from pyflakes.checker import Checker as PyflakesChecker
except ImportError:  # pragma: no cover
    PyflakesChecker = None

# Used by the /complete endpoint (autocomplete / IntelliSense)
# for real completions - object attributes, imported names,
# function signatures. Optional import, same reasoning as
# pyflakes above: the frontend already has a local fallback
# (document words + keywords/builtins) if this isn't installed.
try:
    import jedi
except ImportError:  # pragma: no cover
    jedi = None

# Jedi's environment introspection (used for compiled/dynamic
# parts of stdlib modules - the "AccessPath" machinery) talks to
# a single shared subprocess for the whole process. It isn't
# designed to be called from multiple threads at once - two
# completion requests overlapping (very possible here, since
# each one runs in its own thread via asyncio.to_thread) can
# corrupt that shared pipe and produce bizarre, unrelated-
# looking exceptions (seen in practice: AttributeError on a
# str, AssertionError on an internal AccessPath object). This
# lock forces every Jedi call in this process to run one at a
# time, which is the actual fix rather than a workaround.
_jedi_lock = threading.Lock()

# Postgres-backed persistence (see the "PERSISTENCE" section below).
# Optional import so the app still starts locally even if this
# hasn't been installed / no database is configured.
try:
    import asyncpg
except ImportError:  # pragma: no cover
    asyncpg = None

# fcntl/termios/pty are POSIX-only. The container this runs in
# (python:3.12-slim on Linux) always has them, but importing this
# way means the app still starts (with the terminal feature simply
# disabled) if it's ever run somewhere without them.
try:
    import fcntl
    import termios
    import pty
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None
    termios = None
    pty = None

# Used by the live Camera feature to decode/encode JPEG frames
# and run each project's camera.py. Optional import so the rest
# of the app still starts even if this hasn't been installed.
try:
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover
    cv2 = None
    np = None

# Used to reverse-proxy requests to a project's own running
# server (see the PROJECT SERVERS section below). Optional
# import so the rest of the app still starts (with that one
# feature disabled) if it hasn't been installed yet.
try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None


# =========================================================
# PERSISTENCE (Postgres)
# =========================================================
#
# PROJECTS_DIR below is local disk - on Render's free plan
# (and generally, any container without an attached volume)
# that disk is wiped on every restart/redeploy. To survive
# that, Postgres is the source of truth for project files;
# PROJECTS_DIR is just a working *cache* the rest of the app
# (the run button, the terminal, the camera feature) reads
# and writes like normal local files.
#
# - On startup, every project/file is restored from the DB
#   onto disk.
# - Every write through the file API (save, delete, rename,
#   new project) is mirrored to the DB immediately.
# - The interactive terminal can also change files in ways
#   the API never sees (pip install, rm, mv, a text editor
#   run inside the shell). For that, db_full_resync() walks
#   a project's folder and reconciles the DB to match it -
#   run periodically while a terminal is open and once more
#   when it closes.
#
# If DATABASE_URL isn't set (e.g. running locally), all of
# this quietly no-ops and the app behaves as it did before -
# projects just won't survive a restart.

DATABASE_URL = os.getenv("DATABASE_URL")

db_pool = None  # asyncpg.Pool, set during startup

_SYNC_EXCLUDED_DIR_NAMES = {
    "__pycache__", ".git", ".venv", "venv",
    "node_modules", ".mypy_cache", ".pytest_cache"
}
_SYNC_MAX_FILE_BYTES = 2_000_000

# asyncpg's DSN parser only recognizes a fixed set of query-string
# options (sslmode, sslcert, etc.) - anything else it doesn't
# recognize gets forwarded to Postgres as a server setting instead
# of a connection option. Neon (and some other hosts) append
# channel_binding=require to their connection strings, which trips
# this: it isn't a valid server setting, so the connection fails
# with "unrecognized configuration parameter". Stripping it here
# is safe - it doesn't weaken the connection, since sslmode=require
# already guarantees the traffic is encrypted.
_ASYNCPG_UNSUPPORTED_DSN_PARAMS = {"channel_binding"}


def _sanitize_dsn_for_asyncpg(dsn: str) -> str:

    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

    parts = urlsplit(dsn)

    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key not in _ASYNCPG_UNSUPPORTED_DSN_PARAMS
    ]

    return urlunsplit((
        parts.scheme,
        parts.netloc,
        parts.path,
        urlencode(query_pairs),
        parts.fragment
    ))


async def db_init_pool():

    global db_pool

    if not DATABASE_URL:
        print(
            "DATABASE_URL is not set - projects will NOT "
            "persist across restarts."
        )
        return

    if asyncpg is None:
        print(
            "asyncpg is not installed - projects will NOT "
            "persist across restarts."
        )
        return

    # A slow-to-wake or unreachable database must never take the
    # whole app down with it. If this doesn't succeed quickly,
    # the app still starts and serves projects from local disk -
    # they just won't be persisted until the DB comes back (the
    # next successful write, or the next restart's restore,
    # picks back up normally).
    try:
        db_pool = await asyncio.wait_for(
            asyncpg.create_pool(
                _sanitize_dsn_for_asyncpg(DATABASE_URL),
                min_size=1, max_size=5
            ),
            timeout=10
        )

        async with db_pool.acquire() as conn:

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                """
            )

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS project_files (
                    project_id TEXT NOT NULL REFERENCES
                        projects(id) ON DELETE CASCADE,
                    path TEXT NOT NULL,
                    content TEXT NOT NULL,
                    is_binary BOOLEAN NOT NULL DEFAULT false,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (project_id, path)
                );
                """
            )

            # Explicit folder tracking. Folders that contain
            # files are already implied by project_files.path,
            # but an *empty* folder has no file to imply it, so
            # it needs its own row or it would vanish on
            # restart / resync.
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS project_folders (
                    project_id TEXT NOT NULL REFERENCES
                        projects(id) ON DELETE CASCADE,
                    path TEXT NOT NULL,
                    PRIMARY KEY (project_id, path)
                );
                """
            )

            # Small generic key/value store - currently used for
            # the GitHub Personal Access Token (see the GIT /
            # GITHUB INTEGRATION section below). Not project-
            # scoped: one GitHub account is connected per
            # deployment, same as the rest of this single-user
            # app.
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

            # Upgrading an existing deployment that predates
            # binary file support - add the column if it isn't
            # there yet.
            await conn.execute(
                """
                ALTER TABLE project_files
                ADD COLUMN IF NOT EXISTS is_binary
                    BOOLEAN NOT NULL DEFAULT false;
                """
            )

            # Snapshots of a file's *previous* content, taken
            # right before it gets overwritten (by a save, a
            # terminal-driven change picked up by db_full_resync,
            # or a restore itself) - see db_save_file() and
            # _snapshot_file_version() below. This is what lets a
            # file be rolled back after an accidental overwrite
            # or deletion.
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS file_versions (
                    id SERIAL PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES
                        projects(id) ON DELETE CASCADE,
                    path TEXT NOT NULL,
                    content TEXT,
                    is_binary BOOLEAN NOT NULL DEFAULT false,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                """
            )
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_file_versions_lookup
                ON file_versions (project_id, path, created_at DESC);
                """
            )

            # Which project servers (see PROJECT SERVERS below)
            # should be running. This is desired *state*, not a
            # live process table - a PID from a previous container
            # is meaningless after a restart, so only project_id/
            # entry_file/status are persisted. On startup, every
            # row with status='running' gets relaunched.
            # ON DELETE CASCADE means deleting a project also
            # drops its server record automatically - same as
            # project_files/project_folders above.
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS project_servers (
                    project_id TEXT PRIMARY KEY REFERENCES
                        projects(id) ON DELETE CASCADE,
                    entry_file TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                """
            )

            # Packages installed by the user at runtime (e.g.
            # `pip install requests` typed into the in-browser
            # terminal). These live in the container's system
            # site-packages, which - like PROJECTS_DIR - is wiped
            # on every restart/redeploy on a host with no disk
            # persistence. Postgres is the source of truth here
            # too; see the PIP PACKAGE PERSISTENCE section below.
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pip_packages (
                    name TEXT PRIMARY KEY,
                    version TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                """
            )

        print("Connected to the database.")

    except Exception as error:

        print(
            "Could not connect to the database within 10s - "
            f"continuing without persistence for now: {error}"
        )

        if db_pool is not None:
            try:
                await db_pool.close()
            except Exception:
                pass

        db_pool = None


async def db_restore_projects_to_disk():

    if db_pool is None:
        return

    # Same principle as db_init_pool: this runs once at startup,
    # before the app can serve any requests, so it must not be
    # able to hang or crash the whole app - a slow query or a
    # connection blip here would otherwise take Render's health
    # check down with it, and the deploy would never go live.
    try:
        async with asyncio.timeout(20):

            async with db_pool.acquire() as conn:

                projects = await conn.fetch(
                    "SELECT id FROM projects"
                )

                for row in projects:

                    project_id = row["id"]

                    try:
                        folder = project_path(project_id)
                    except ValueError:
                        continue

                    folder.mkdir(parents=True, exist_ok=True)

                    # Folders first (so empty ones exist even
                    # if no file ever gets written into them),
                    # then files.
                    folder_rows = await conn.fetch(
                        "SELECT path FROM project_folders "
                        "WHERE project_id = $1",
                        project_id
                    )

                    for folder_row in folder_rows:

                        try:
                            relative = safe_relative_path(
                                folder_row["path"]
                            )
                        except ValueError:
                            continue

                        (folder / relative).mkdir(
                            parents=True, exist_ok=True
                        )

                    files = await conn.fetch(
                        "SELECT path, content, is_binary "
                        "FROM project_files WHERE project_id = $1",
                        project_id
                    )

                    for file_row in files:

                        try:
                            relative = safe_relative_path(
                                file_row["path"]
                            )
                        except ValueError:
                            continue

                        target = folder / relative
                        target.parent.mkdir(
                            parents=True, exist_ok=True
                        )

                        if file_row["is_binary"]:
                            target.write_bytes(
                                base64.b64decode(
                                    file_row["content"]
                                )
                            )
                        else:
                            target.write_text(
                                file_row["content"],
                                encoding="utf-8"
                            )

            print(
                f"Restored {len(projects)} project(s) "
                "from the database."
            )

    except Exception as error:

        print(
            "Restoring projects from the database failed or "
            f"timed out - starting with an empty workspace "
            f"instead of blocking startup: {error}"
        )


async def db_save_project(project_id: str, name: str):

    if db_pool is None:
        return

    async with db_pool.acquire() as conn:

        await conn.execute(
            """
            INSERT INTO projects (id, name)
            VALUES ($1, $2)
            ON CONFLICT (id) DO NOTHING
            """,
            project_id, name
        )


# In-memory fallback for app_settings when there's no database -
# same "quietly degrade instead of failing" approach as projects:
# a connected GitHub account just won't survive a restart without
# a database, but the feature still works for the current session.
_memory_settings = {}


async def db_get_setting(key: str):

    if db_pool is None:
        return _memory_settings.get(key)

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM app_settings WHERE key = $1",
            key
        )
        return row["value"] if row else None


async def db_set_setting(key: str, value: str):

    if db_pool is None:
        _memory_settings[key] = value
        return

    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO app_settings (key, value)
            VALUES ($1, $2)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            key, value
        )


async def db_delete_setting(key: str):

    if db_pool is None:
        _memory_settings.pop(key, None)
        return

    async with db_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM app_settings WHERE key = $1",
            key
        )


_MAX_FILE_VERSIONS_PER_FILE = 25


async def _snapshot_file_version(
    conn, project_id: str, path: str, content, is_binary: bool
):
    """
    Records what a file looked like right before it's about to
    be overwritten or deleted. `conn` is an already-acquired
    connection (and may already be inside a transaction) so this
    never opens its own. Keeps only the most recent
    _MAX_FILE_VERSIONS_PER_FILE snapshots per (project, path) so
    history can't grow without bound.
    """

    await conn.execute(
        """
        INSERT INTO file_versions (project_id, path, content, is_binary)
        VALUES ($1, $2, $3, $4)
        """,
        project_id, path, content, is_binary
    )

    await conn.execute(
        """
        DELETE FROM file_versions
        WHERE id IN (
            SELECT id FROM file_versions
            WHERE project_id = $1 AND path = $2
            ORDER BY created_at DESC
            OFFSET $3
        )
        """,
        project_id, path, _MAX_FILE_VERSIONS_PER_FILE
    )


async def db_save_file(
    project_id: str,
    path: str,
    content: str,
    is_binary: bool = False
):
    """
    content is the file's text for text files, or base64-encoded
    bytes for binary files (is_binary=True).
    """

    if db_pool is None:
        return

    async with db_pool.acquire() as conn:

        old_row = await conn.fetchrow(
            "SELECT content, is_binary FROM project_files "
            "WHERE project_id = $1 AND path = $2",
            project_id, path
        )

        if old_row is not None and (
            old_row["content"] != content
            or old_row["is_binary"] != is_binary
        ):
            await _snapshot_file_version(
                conn, project_id, path,
                old_row["content"], old_row["is_binary"]
            )

        await conn.execute(
            """
            INSERT INTO project_files
                (project_id, path, content, is_binary, updated_at)
            VALUES ($1, $2, $3, $4, now())
            ON CONFLICT (project_id, path)
            DO UPDATE SET
                content = EXCLUDED.content,
                is_binary = EXCLUDED.is_binary,
                updated_at = now()
            """,
            project_id, path, content, is_binary
        )

        # Saving a file implies every ancestor folder exists -
        # drop any explicit (now-redundant) folder row for them,
        # matching what db_full_resync would settle on anyway.
        parents = list(Path(path).parents)[:-1]

        if parents:
            await conn.execute(
                "DELETE FROM project_folders "
                "WHERE project_id = $1 AND path = ANY($2::text[])",
                project_id,
                [p.as_posix() for p in parents]
            )


async def db_delete_file(project_id: str, path: str):

    if db_pool is None:
        return

    async with db_pool.acquire() as conn:

        await conn.execute(
            "DELETE FROM project_files "
            "WHERE project_id = $1 AND path = $2",
            project_id, path
        )


async def db_save_folder(project_id: str, path: str):

    if db_pool is None:
        return

    async with db_pool.acquire() as conn:

        await conn.execute(
            """
            INSERT INTO project_folders (project_id, path)
            VALUES ($1, $2)
            ON CONFLICT (project_id, path) DO NOTHING
            """,
            project_id, path
        )


async def db_delete_folder(project_id: str, path: str):
    """Deletes a folder and everything nested under it."""

    if db_pool is None:
        return

    prefix = path.rstrip("/") + "/"

    async with db_pool.acquire() as conn:

        async with conn.transaction():

            await conn.execute(
                "DELETE FROM project_files "
                "WHERE project_id = $1 "
                "AND (path = $2 OR path LIKE $3)",
                project_id, path, prefix + "%"
            )

            await conn.execute(
                "DELETE FROM project_folders "
                "WHERE project_id = $1 "
                "AND (path = $2 OR path LIKE $3)",
                project_id, path, prefix + "%"
            )


async def db_move_prefix(
    project_id: str, old_path: str, new_path: str
):
    """
    Renames/moves a folder: updates every file and folder row
    whose path is old_path, or starts with old_path + "/", to
    start with new_path instead.
    """

    if db_pool is None:
        return

    old_prefix = old_path.rstrip("/") + "/"

    async with db_pool.acquire() as conn:

        rows = await conn.fetch(
            "SELECT path FROM project_files "
            "WHERE project_id = $1 "
            "AND (path = $2 OR path LIKE $3)",
            project_id, old_path, old_prefix + "%"
        )

        folder_rows = await conn.fetch(
            "SELECT path FROM project_folders "
            "WHERE project_id = $1 "
            "AND (path = $2 OR path LIKE $3)",
            project_id, old_path, old_prefix + "%"
        )

        async with conn.transaction():

            for row in rows:

                old = row["path"]
                new = new_path + old[len(old_path):]

                await conn.execute(
                    "UPDATE project_files SET path = $3 "
                    "WHERE project_id = $1 AND path = $2",
                    project_id, old, new
                )

            for row in folder_rows:

                old = row["path"]
                new = new_path + old[len(old_path):]

                await conn.execute(
                    "UPDATE project_folders SET path = $3 "
                    "WHERE project_id = $1 AND path = $2",
                    project_id, old, new
                )


def _should_sync_path(relative_parts) -> bool:

    return not any(
        part in _SYNC_EXCLUDED_DIR_NAMES
        for part in relative_parts
    )


async def db_full_resync(project_id: str):
    """
    Walks a project's folder on disk and makes the database
    match it exactly. This is what catches changes made
    outside the file-editor API - e.g. through the
    interactive terminal (pip install, rm, mv, an editor
    run inside the shell).
    """

    if db_pool is None:
        return

    try:
        folder = project_path(project_id)
    except ValueError:
        return

    if not folder.is_dir():
        return

    disk_files = {}
    disk_dirs = set()

    for path in folder.rglob("*"):

        relative = path.relative_to(folder)

        if not _should_sync_path(relative.parts):
            continue

        if path.is_dir():
            disk_dirs.add(relative.as_posix())
            continue

        if not path.is_file():
            continue

        try:

            if path.stat().st_size > _SYNC_MAX_FILE_BYTES:
                continue

            try:
                content = path.read_text(encoding="utf-8")
                is_binary = False
            except UnicodeDecodeError:
                content = base64.b64encode(
                    path.read_bytes()
                ).decode("ascii")
                is_binary = True

        except OSError:
            continue

        disk_files[relative.as_posix()] = (content, is_binary)

    # Folders implied by a file's own path don't need an explicit
    # row - only genuinely empty ones do.
    implied_dirs = set()
    for file_path in disk_files:
        for parent in Path(file_path).parents:
            if parent != Path("."):
                implied_dirs.add(parent.as_posix())

    empty_disk_dirs = disk_dirs - implied_dirs

    async with db_pool.acquire() as conn:

        db_rows = await conn.fetch(
            "SELECT path, content, is_binary FROM project_files "
            "WHERE project_id = $1",
            project_id
        )
        db_paths = {row["path"] for row in db_rows}
        db_content_by_path = {
            row["path"]: (row["content"], row["is_binary"])
            for row in db_rows
        }

        db_folder_rows = await conn.fetch(
            "SELECT path FROM project_folders WHERE project_id = $1",
            project_id
        )
        db_folder_paths = {row["path"] for row in db_folder_rows}

        files_to_delete = db_paths - set(disk_files.keys())
        folders_to_delete = db_folder_paths - empty_disk_dirs

        async with conn.transaction():

            for path in files_to_delete:

                # A file that vanished on disk (e.g. `rm` in the
                # terminal) - snapshot its last known content
                # before dropping the row, so it's recoverable
                # from File History even though the row itself
                # is gone.
                old = db_content_by_path.get(path)
                if old is not None:
                    await _snapshot_file_version(
                        conn, project_id, path, old[0], old[1]
                    )

                await conn.execute(
                    "DELETE FROM project_files "
                    "WHERE project_id = $1 AND path = $2",
                    project_id, path
                )

            for path, (content, is_binary) in disk_files.items():

                old = db_content_by_path.get(path)
                if old is not None and (
                    old[0] != content or old[1] != is_binary
                ):
                    await _snapshot_file_version(
                        conn, project_id, path, old[0], old[1]
                    )

                await conn.execute(
                    """
                    INSERT INTO project_files
                        (project_id, path, content, is_binary,
                         updated_at)
                    VALUES ($1, $2, $3, $4, now())
                    ON CONFLICT (project_id, path)
                    DO UPDATE SET
                        content = EXCLUDED.content,
                        is_binary = EXCLUDED.is_binary,
                        updated_at = now()
                    """,
                    project_id, path, content, is_binary
                )

            for path in folders_to_delete:

                await conn.execute(
                    "DELETE FROM project_folders "
                    "WHERE project_id = $1 AND path = $2",
                    project_id, path
                )

            for path in empty_disk_dirs:

                await conn.execute(
                    """
                    INSERT INTO project_folders (project_id, path)
                    VALUES ($1, $2)
                    ON CONFLICT (project_id, path) DO NOTHING
                    """,
                    project_id, path
                )


# =========================================================
# PIP PACKAGE PERSISTENCE (Postgres)
# =========================================================
#
# Same problem as PROJECTS_DIR above, applied to installed
# packages instead of project files: the terminal is a real
# shell, so `pip install <package>` works exactly like it
# would locally - but it installs into the container's system
# site-packages, which lives on the same disk that gets wiped
# on every restart/redeploy. Without this, every package a
# user installs by hand disappears the next time the service
# restarts, even though requirements.txt-based packages come
# back fine (the Dockerfile reinstalls those on every build).
#
# The fix mirrors db_full_resync(): periodically diff
# `pip freeze` against requirements.txt to find packages the
# user installed that AREN'T already pinned in the image, and
# keep Postgres's pip_packages table in sync with that diff.
# On startup, after restoring project files, everything in
# that table gets pip-installed again before the app is
# considered ready.

_PKG_NAME_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")


def _normalize_pkg_name(name: str) -> str:
    # pip/PyPI treat "-", "_" and "." as interchangeable in
    # distribution names (e.g. opencv-contrib-python-headless
    # vs opencv_contrib_python_headless) - normalize so the
    # requirements.txt exclusion list actually matches.
    return re.sub(r"[-_.]+", "-", name).lower()


def _load_base_requirement_names() -> set:
    """
    Package names already pinned in requirements.txt - these
    get reinstalled by the Dockerfile on every build/redeploy
    anyway, so they're excluded from the "user installed this
    by hand" diff below.
    """

    names = set()

    try:
        req_path = Path(__file__).resolve().parent.parent / "requirements.txt"
        for line in req_path.read_text().splitlines():

            line = line.split("#", 1)[0].strip()

            if not line:
                continue

            match = _PKG_NAME_RE.match(line)

            if match:
                names.add(_normalize_pkg_name(match.group(1)))

    except OSError:
        pass

    # setuptools/wheel/pip itself, plus whatever the base image
    # ships with - never worth tracking as "user installed".
    names |= {"pip", "setuptools", "wheel"}

    return names


_BASE_REQUIREMENT_NAMES = _load_base_requirement_names()


async def _pip_freeze() -> dict:
    """Returns {normalized_name: (original_name, version)} for
    everything currently installed in this environment."""

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "pip", "list", "--format=json"],
            capture_output=True, text=True, timeout=30
        )

        if result.returncode != 0:
            return {}

        packages = json.loads(result.stdout)

        return {
            _normalize_pkg_name(pkg["name"]): (pkg["name"], pkg["version"])
            for pkg in packages
        }

    except Exception:
        return {}


async def db_sync_pip_packages():
    """
    Diffs the environment's currently installed packages against
    requirements.txt and writes the difference (packages the user
    installed themselves, via the terminal) to Postgres. Called
    periodically while a terminal is open and once more when it
    closes - same cadence as db_full_resync().
    """

    if db_pool is None:
        return

    installed = await _pip_freeze()

    if not installed:
        return

    extras = {
        norm: name_version
        for norm, name_version in installed.items()
        if norm not in _BASE_REQUIREMENT_NAMES
    }

    try:
        async with db_pool.acquire() as conn:
            async with conn.transaction():

                await conn.execute("DELETE FROM pip_packages")

                for name, version in extras.values():
                    await conn.execute(
                        """
                        INSERT INTO pip_packages (name, version, updated_at)
                        VALUES ($1, $2, now())
                        """,
                        name, version
                    )

    except Exception as error:
        print(f"pip package sync failed: {error}")


async def db_reinstall_pip_packages():
    """
    Reinstalls every package recorded in pip_packages. Runs once
    at startup, after project files are restored, in the
    background - a slow or large reinstall must never delay the
    app coming up and passing Render's health check.
    """

    if db_pool is None:
        return

    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT name, version FROM pip_packages ORDER BY name"
            )

    except Exception as error:
        print(f"Could not read saved pip packages: {error}")
        return

    if not rows:
        return

    specs = [f"{row['name']}=={row['version']}" for row in rows]

    print(
        f"Reinstalling {len(specs)} user-installed pip package(s) "
        f"from a previous session: {', '.join(specs)}"
    )

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "pip", "install", "--no-cache-dir", *specs],
            capture_output=True, text=True, timeout=600
        )

        if result.returncode != 0:
            print(
                "Some saved pip packages failed to reinstall:\n"
                f"{result.stderr[-2000:]}"
            )
        else:
            print("Saved pip packages reinstalled successfully.")

    except Exception as error:
        print(f"Reinstalling saved pip packages failed: {error}")


# =========================================================
# PROJECT SERVERS (long-running "run as a server", start/stop,
# survives an app restart, reachable at a dedicated URL)
# =========================================================
#
# This is different from the Run button (which runs a script
# once, captures its output, and kills it after a few seconds
# or when it finishes). A "project server" is a process meant
# to keep running indefinitely - a Flask/FastAPI app, a bot, a
# long poll loop - started explicitly and left running until
# it's stopped.
#
# Three things this needs to actually deliver "stays running
# even if the main app restarts":
#
# 1. Postgres remembers *which* projects should be running
#    (project_servers table above) - not the OS process itself,
#    since a PID from before a restart is meaningless in a new
#    container. On startup, db_resume_project_servers() replays
#    that desired state by relaunching each one.
# 2. A real child process (asyncio subprocess), tracked in
#    _running_servers, bound to an *internal* port on
#    127.0.0.1 chosen from _SERVER_PORT_RANGE. This is never
#    exposed to the internet directly - Render only forwards
#    one external port, the one this app itself listens on.
# 3. A dedicated URL per project - /run/{project_id}/... -
#    implemented as a reverse proxy (proxy_to_project_server
#    below) that forwards to whichever internal port that
#    project is currently bound to. The URL a user bookmarks
#    stays the same across restarts even though the internal
#    port behind it may change.
#
# IMPORTANT for whatever the user runs as a project server: it
# must read the PORT environment variable and bind to it (e.g.
# Flask: app.run(host="0.0.0.0", port=int(os.environ["PORT"])).
# A hardcoded port won't be reachable through /run/{project_id}/.
#
# Known limitations:
# - Plain HTTP request/response only - no WebSocket proxying yet.
# - Path-based, not a real separate origin: if the project's own
#   HTML/JS references absolute paths like "/static/app.css", the
#   browser requests that from this app's own root, not from
#   /run/{project_id}/static/app.css, and 404s. Frameworks that
#   only use paths relative to the page they're rendering (most
#   simple Flask/FastAPI apps) work fine as-is.

_SERVER_PORT_RANGE = range(20000, 20100)

# project_id -> {process, port, entry_file, log (deque),
#                started_at, watcher (asyncio.Task)}
_running_servers = {}

# Guards start/stop so two overlapping requests for the same
# project (e.g. a double-tapped Start button) can't both spawn
# a process or race on the same in-memory entry.
_server_locks = {}

_http_client = None  # httpx.AsyncClient, set during lifespan


class ProjectServerError(Exception):
    """Raised for any expected/user-facing project-server
    failure (bad entry file, no free port, etc). Endpoints
    catch this and turn it into a 400 with the message as-is -
    unexpected exceptions still propagate as 500s."""


def _server_lock(project_id: str) -> asyncio.Lock:
    lock = _server_locks.get(project_id)
    if lock is None:
        lock = asyncio.Lock()
        _server_locks[project_id] = lock
    return lock


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _allocate_server_port() -> int:
    taken = {
        entry["port"]
        for entry in _running_servers.values()
    }
    for port in _SERVER_PORT_RANGE:
        if port not in taken and _port_is_free(port):
            return port
    raise ProjectServerError(
        "No free internal port available right now - try "
        "stopping another running project server first."
    )


def _validate_entry_file(folder: Path, entry_file: str) -> Path:

    entry_file = (entry_file or "main.py").strip()

    try:
        relative = safe_relative_path(entry_file)
    except ValueError:
        raise ProjectServerError(
            f"'{entry_file}' isn't a valid file path."
        )

    full_path = (folder / relative).resolve()

    try:
        full_path.relative_to(folder.resolve())
    except ValueError:
        raise ProjectServerError(
            f"'{entry_file}' isn't a valid file path."
        )

    if not full_path.is_file():
        raise ProjectServerError(
            f"'{entry_file}' doesn't exist in this project."
        )

    return relative


async def db_save_project_server(
    project_id: str, entry_file: str, status: str
):
    if db_pool is None:
        return

    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO project_servers
                (project_id, entry_file, status, updated_at)
            VALUES ($1, $2, $3, now())
            ON CONFLICT (project_id) DO UPDATE SET
                entry_file = EXCLUDED.entry_file,
                status = EXCLUDED.status,
                updated_at = now()
            """,
            project_id, entry_file, status
        )


async def _watch_server_process(project_id: str, proc):
    """Notices if a project server exits on its own (crash, or
    the script just finishing) and cleans up state so it isn't
    shown as "running" forever."""

    await proc.wait()

    entry = _running_servers.get(project_id)

    # Only clean up if this watcher's process is still the
    # current one for this project - stop_project_server() may
    # have already popped it (intentional stop) or a new start
    # may have already replaced it.
    if entry is not None and entry.get("process") is proc:
        _running_servers.pop(project_id, None)
        await db_save_project_server(
            project_id, entry.get("entry_file", "main.py"), "stopped"
        )


async def _pump_server_log(project_id: str, proc):

    entry = _running_servers.get(project_id)
    if entry is None:
        return

    log = entry["log"]

    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            log.append(
                line.decode("utf-8", errors="replace").rstrip("\n")
            )
            entry["total_lines"] = entry.get("total_lines", 0) + 1
    except Exception:
        pass


async def start_project_server(
    project_id: str, entry_file: str = "main.py"
) -> dict:

    try:
        folder = project_path(project_id)
    except ValueError:
        raise ProjectServerError("Invalid project ID")

    if not folder.is_dir():
        raise ProjectServerError("Project not found")

    async with _server_lock(project_id):

        existing = _running_servers.get(project_id)
        if existing is not None and existing["process"].returncode is None:
            # Already running - starting again is a no-op, not
            # an error (matches how the Start button behaves if
            # tapped twice).
            return _server_status_dict(project_id)

        relative_entry = _validate_entry_file(folder, entry_file)
        port = _allocate_server_port()

        env = {
            **os.environ,
            "PORT": str(port),
            "HOST": "127.0.0.1",
            "PYTHONUNBUFFERED": "1",
        }

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(relative_entry),
                cwd=str(folder),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception as error:
            raise ProjectServerError(
                f"Couldn't start '{entry_file}': {error}"
            )

        _running_servers[project_id] = {
            "process": proc,
            "port": port,
            "entry_file": str(relative_entry),
            "log": deque(maxlen=400),
            "total_lines": 0,
            "started_at": time.time(),
        }

        asyncio.create_task(_pump_server_log(project_id, proc))
        asyncio.create_task(_watch_server_process(project_id, proc))

        await db_save_project_server(
            project_id, str(relative_entry), "running"
        )

        return _server_status_dict(project_id)


async def db_mark_project_server_stopped(project_id: str):
    """
    Like db_save_project_server(..., "stopped"), but for when
    there's no in-memory entry to read entry_file from (e.g.
    stopping something that's already stopped). Only touches
    `status` so it can never clobber a previously-saved
    entry_file with a wrong default.
    """
    if db_pool is None:
        return

    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE project_servers SET status = 'stopped', "
            "updated_at = now() WHERE project_id = $1",
            project_id
        )


async def stop_project_server(project_id: str) -> dict:

    async with _server_lock(project_id):

        entry = _running_servers.pop(project_id, None)

        if entry is not None:
            proc = entry["process"]
            if proc.returncode is None:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except (asyncio.TimeoutError, ProcessLookupError):
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                except Exception:
                    pass

            await db_save_project_server(
                project_id, entry["entry_file"], "stopped"
            )
        else:
            await db_mark_project_server_stopped(project_id)

        return {"status": "stopped", "project_id": project_id}


def _server_status_dict(project_id: str) -> dict:

    entry = _running_servers.get(project_id)

    if entry is None or entry["process"].returncode is not None:
        return {
            "status": "stopped",
            "project_id": project_id,
            "url": None,
            "log": list(entry["log"]) if entry else [],
        }

    return {
        "status": "running",
        "project_id": project_id,
        "entry_file": entry["entry_file"],
        "url": f"/run/{project_id}/",
        "started_at": entry["started_at"],
        "log": list(entry["log"]),
    }


async def db_resume_project_servers():
    """
    Runs once at startup, after project files are restored -
    relaunches every project server that was running before the
    last restart. Same "background task, never block startup"
    reasoning as db_reinstall_pip_packages().
    """

    if db_pool is None:
        return

    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT project_id, entry_file FROM project_servers "
                "WHERE status = 'running'"
            )
    except Exception as error:
        print(f"Could not read saved project servers: {error}")
        return

    for row in rows:
        try:
            await start_project_server(
                row["project_id"], row["entry_file"]
            )
            print(
                f"Resumed project server for '{row['project_id']}' "
                f"({row['entry_file']})."
            )
        except Exception as error:
            print(
                f"Could not resume project server for "
                f"'{row['project_id']}': {error}"
            )


_HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers",
    "transfer-encoding", "upgrade", "host", "content-length",
}


# proxy_to_project_server (the /run/{project_id}/... route) is
# registered further down, right after `app = FastAPI(...)` is
# created - a route decorator needs `app` to already exist.


async def _run_post_startup_background_tasks():
    """
    Reinstalling saved pip packages, then resuming project
    servers - in that order, in the background, so a project
    server that depends on one of those packages doesn't race
    its own dependency install. Both steps individually already
    tolerate failure (a bad package or a project that no longer
    starts just logs and moves on; see each function's own
    try/except).
    """
    await db_reinstall_pip_packages()
    await db_resume_project_servers()


@asynccontextmanager
async def lifespan(app: FastAPI):

    global _http_client

    await db_init_pool()
    await db_restore_projects_to_disk()

    if httpx is not None:
        _http_client = httpx.AsyncClient(timeout=30.0)

    # Fire-and-forget: both reinstalling packages and resuming
    # project servers can take a while, so neither can block the
    # app from coming up and passing Render's health check. They
    # run in the background instead; the terminal and Run button
    # work immediately either way.
    asyncio.create_task(_run_post_startup_background_tasks())

    # Modern git refuses to operate on a repo it doesn't think the
    # current user "owns" (a safety check against a class of
    # multi-user-machine attacks that doesn't apply here - this
    # container only ever runs this one app). Without this, every
    # git command below would fail immediately with "detected
    # dubious ownership in repository".
    try:
        subprocess.run(
            ["git", "config", "--global",
             "--add", "safe.directory", "*"],
            timeout=10, check=False,
            capture_output=True
        )
    except Exception:
        pass

    yield

    if db_pool is not None:
        await db_pool.close()

    if _http_client is not None:
        await _http_client.aclose()


app = FastAPI(title="Python IDE", lifespan=lifespan)


@app.api_route(
    "/run/{project_id}/{sub_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
)
async def proxy_to_project_server(
    project_id: str, sub_path: str, request: Request
):
    """
    Reverse proxy: forwards a request to whatever internal port
    the given project's server currently owns (if any). This is
    what gives each running project a stable, dedicated URL that
    doesn't change even though the internal port behind it can -
    see the PROJECT SERVERS section above for the full picture.
    """

    entry = _running_servers.get(project_id)

    if entry is None or entry["process"].returncode is not None:
        return JSONResponse(
            {
                "error":
                    f"No server is currently running for "
                    f"'{project_id}'. Start it from the Server "
                    f"tab first."
            },
            status_code=404
        )

    if httpx is None or _http_client is None:
        return JSONResponse(
            {
                "error":
                    "The proxy feature isn't available "
                    "(httpx isn't installed)."
            },
            status_code=501
        )

    port = entry["port"]
    target_url = f"http://127.0.0.1:{port}/{sub_path}"

    forward_headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _HOP_BY_HOP_HEADERS
    }

    body = await request.body()

    try:
        upstream = await _http_client.request(
            request.method,
            target_url,
            params=request.query_params,
            headers=forward_headers,
            content=body,
        )
    except httpx.RequestError as error:
        return JSONResponse(
            {
                "error":
                    f"Project server for '{project_id}' isn't "
                    f"responding yet (it may still be starting "
                    f"up): {error}"
            },
            status_code=502
        )

    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in _HOP_BY_HOP_HEADERS
    }

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
    )

# Resolve paths relative to this file instead of the process's current
# working directory. Previously these were relative strings like
# "app/static", which only worked if the server happened to be started
# from the exact project root. Running it any other way (e.g. `python
# app/main.py`, or a different WORKDIR) raised a startup error because
# the directory couldn't be found.
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app.mount(
    "/static",
    StaticFiles(directory=str(STATIC_DIR)),
    name="static"
)


@app.middleware("http")
async def no_cache_static_assets(request: Request, call_next):
    """
    Same problem as index.html above, for app.js/style.css:
    mobile browsers cache static assets aggressively, and
    StaticFiles doesn't send a Cache-Control header on its
    own. Without this, a phone can keep running yesterday's
    app.js forever - every deploy "does nothing" - even
    though index.html itself is always fetched fresh. This
    still lets the browser send conditional requests (via
    StaticFiles' own ETag/Last-Modified), it just forces a
    revalidation instead of trusting a stale local copy.
    """
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = (
            "no-cache, must-revalidate"
        )
    return response


PROJECTS_DIR = Path(
    os.getenv(
        "PROJECTS_DIR",
        "/tmp/python-ide-projects"
    )
)

PROJECTS_DIR.mkdir(
    parents=True,
    exist_ok=True
)


# =========================================================
# SECURITY / PATH HELPERS
# =========================================================

def project_path(project_id: str) -> Path:

    if not project_id:
        raise ValueError(
            "Invalid project ID"
        )

    allowed = (
        "abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "0123456789-_"
    )

    if any(
        char not in allowed
        for char in project_id
    ):
        raise ValueError(
            "Invalid project ID"
        )

    return PROJECTS_DIR / project_id


def safe_relative_path(path: str) -> Path:

    relative = Path(path)

    if relative.is_absolute():
        raise ValueError(
            "Absolute paths are not allowed"
        )

    if ".." in relative.parts:
        raise ValueError(
            "Parent paths are not allowed"
        )

    if not relative.parts:
        raise ValueError(
            "Invalid path"
        )

    return relative


def build_file_tree(folder: Path):
    """
    Builds a nested VS Code-style tree: folders before files at
    each level, both alphabetical. Empty folders are included
    (rglob would otherwise silently drop them).
    """

    root = {}

    def get_node(parts):
        node = root
        for part in parts:
            node = node.setdefault(part, {"__children__": {}})
            node = node["__children__"]
        return node

    for path in sorted(folder.rglob("*")):

        relative = path.relative_to(folder)

        if not _should_sync_path(relative.parts):
            continue

        parent_node = get_node(relative.parts[:-1])

        if path.is_dir():
            parent_node.setdefault(
                relative.parts[-1], {"__children__": {}}
            )
        elif path.is_file():
            parent_node[relative.parts[-1]] = {
                "__file__": True
            }

    def to_list(node, prefix=""):

        folders = []
        files = []

        for name, value in node.items():

            path = f"{prefix}{name}"

            if value.get("__file__"):
                files.append({
                    "name": name,
                    "path": path,
                    "type": "file"
                })
            else:
                folders.append({
                    "name": name,
                    "path": path,
                    "type": "folder",
                    "children": to_list(
                        value["__children__"], path + "/"
                    )
                })

        folders.sort(key=lambda n: n["name"].lower())
        files.sort(key=lambda n: n["name"].lower())

        return folders + files

    return to_list(root)


def list_files(folder: Path):

    files = []

    for path in sorted(
        folder.rglob("*")
    ):

        if path.is_file():

            files.append(
                path.relative_to(
                    folder
                ).as_posix()
            )

    return files


# =========================================================
# HOME
# =========================================================

@app.get("/")
async def index():

    return FileResponse(
        str(STATIC_DIR / "index.html"),
        # This page changes often as the app is developed, and
        # mobile browsers cache HTML pretty aggressively by
        # default (no explicit header = the browser guesses how
        # long it's "fresh" for). Without this, a refresh can
        # silently serve an old cached copy even after a new
        # version has been deployed - forcing revalidation on
        # every load means you always get what's actually live.
        headers={
            "Cache-Control":
                "no-cache, must-revalidate"
        }
    )


# =========================================================
# PROJECTS
# =========================================================

@app.get("/api/projects")
async def get_projects():

    projects = []

    for folder in sorted(
        PROJECTS_DIR.iterdir()
    ):

        if folder.is_dir():

            projects.append({
                "id": folder.name,
                "name": folder.name
            })

    return {
        "projects": projects
    }


@app.post("/api/projects")
async def create_project(
    request: Request
):

    data = await request.json()

    name = str(
        data.get(
            "name",
            "project"
        )
    ).strip()

    if not name:
        name = "project"


    name = "".join(
        char
        if char.isalnum()
        or char in "-_"
        else "-"
        for char in name
    )


    project_id = (
        f"{name}-"
        f"{uuid.uuid4().hex[:8]}"
    )


    folder = project_path(
        project_id
    )

    folder.mkdir(
        parents=True,
        exist_ok=False
    )


    starter_content = (
        'print("Hello from your Python project!")\n'
    )

    (
        folder / "main.py"
    ).write_text(
        starter_content,
        encoding="utf-8"
    )

    await db_save_project(project_id, project_id)
    await db_save_file(project_id, "main.py", starter_content)


    return {
        "id": project_id,
        "name": project_id
    }


# =========================================================
# DELETE PROJECT
# =========================================================

@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str):

    try:
        folder = project_path(project_id)
    except ValueError:
        return JSONResponse(
            {"error": "Invalid project ID"}, status_code=400
        )

    if not folder.is_dir():
        return JSONResponse(
            {"error": "Project not found"}, status_code=404
        )

    # Stop its server first, if one is running - nothing should
    # keep writing to (or holding a port open for) a project
    # that's about to be deleted.
    await stop_project_server(project_id)
    _server_locks.pop(project_id, None)

    try:
        shutil.rmtree(folder)
    except OSError as error:
        return JSONResponse(
            {"error": f"Could not delete project files: {error}"},
            status_code=500
        )

    if db_pool is not None:
        try:
            async with db_pool.acquire() as conn:
                # ON DELETE CASCADE on project_files,
                # project_folders and project_servers takes
                # care of the rest.
                await conn.execute(
                    "DELETE FROM projects WHERE id = $1", project_id
                )
        except Exception as error:
            print(
                f"Could not delete project '{project_id}' from "
                f"the database: {error}"
            )

    return {"deleted": project_id}


# =========================================================
# PROJECT SERVER (start / stop / status) - see the PROJECT
# SERVERS section far above for the full design.
# =========================================================

@app.post("/api/projects/{project_id}/server/start")
async def api_start_project_server(project_id: str, request: Request):

    try:
        data = await request.json()
    except Exception:
        data = {}

    entry_file = str(data.get("entry_file") or "main.py").strip()

    try:
        return await start_project_server(project_id, entry_file)
    except ProjectServerError as error:
        return JSONResponse({"error": str(error)}, status_code=400)


@app.post("/api/projects/{project_id}/server/stop")
async def api_stop_project_server(project_id: str):
    return await stop_project_server(project_id)


@app.get("/api/projects/{project_id}/server")
async def api_get_project_server(project_id: str):
    return _server_status_dict(project_id)


@app.websocket("/ws/projects/{project_id}/server-log")
async def project_server_log_ws(websocket: WebSocket, project_id: str):
    """
    Live-tails a project server's output - like Render's log
    view. One-directional (server to client only): polls the
    in-memory log buffer every 750ms and pushes only what's new,
    using a running total-lines counter rather than buffer
    length so it stays correct even once the buffer's 400-line
    cap starts dropping old lines. Survives the project server
    being stopped and restarted without the client reconnecting -
    it just reports the status change and starts counting fresh.
    """

    await websocket.accept()

    last_process = None
    last_sent_total = 0
    last_reported_status = None

    try:
        while True:

            entry = _running_servers.get(project_id)

            if entry is None or entry["process"].returncode is not None:

                if last_reported_status != "stopped":
                    await websocket.send_text(json.dumps({
                        "type": "status", "status": "stopped"
                    }))
                    last_reported_status = "stopped"
                    last_process = None
                    last_sent_total = 0

            else:

                if entry["process"] is not last_process:
                    # A (re)start since we last looked - fresh
                    # log buffer, so count from zero.
                    last_process = entry["process"]
                    last_sent_total = 0
                    await websocket.send_text(json.dumps({
                        "type": "status", "status": "running"
                    }))
                    last_reported_status = "running"

                new_count = entry.get("total_lines", 0) - last_sent_total

                if new_count > 0:
                    current = list(entry["log"])
                    to_send = (
                        current[-new_count:]
                        if new_count <= len(current)
                        else current
                    )
                    for line in to_send:
                        await websocket.send_text(json.dumps({
                            "type": "log", "line": line
                        }))
                    last_sent_total = entry.get("total_lines", 0)

            await asyncio.sleep(0.75)

    except Exception:
        # Covers a normal client-initiated disconnect as well as
        # any transient send error - either way there's nothing
        # to clean up, this handler owns no resources beyond the
        # socket itself.
        pass


# =========================================================
# FILE LIST
# =========================================================

@app.get(
    "/api/projects/{project_id}/files"
)
async def get_files(
    project_id: str
):

    try:

        folder = project_path(
            project_id
        )

    except ValueError:

        return JSONResponse(
            {
                "error":
                "Invalid project ID"
            },
            status_code=400
        )


    if not folder.is_dir():

        return JSONResponse(
            {
                "error":
                "Project not found"
            },
            status_code=404
        )


    return {
        "files":
        list_files(folder),
        "tree":
        build_file_tree(folder)
    }


# =========================================================
# READ FILE
# =========================================================

@app.get(
    "/api/projects/{project_id}/file"
)
async def read_file(
    project_id: str,
    path: str
):

    try:

        folder = project_path(
            project_id
        )

        relative = safe_relative_path(
            path
        )

        target = folder / relative

    except ValueError:

        return JSONResponse(
            {
                "error":
                "Invalid path"
            },
            status_code=400
        )


    if not target.is_file():

        return JSONResponse(
            {
                "error":
                "File not found"
            },
            status_code=404
        )


    if target.stat().st_size > 1_000_000:

        return JSONResponse(
            {
                "error":
                "File is too large"
            },
            status_code=413
        )


    try:

        content = target.read_text(
            encoding="utf-8"
        )

    except UnicodeDecodeError:

        return JSONResponse(
            {
                "error":
                "Only UTF-8 text files are supported"
            },
            status_code=415
        )


    return {
        "path": path,
        "content": content
    }


# =========================================================
# WRITE FILE
# =========================================================

@app.post(
    "/api/projects/{project_id}/file"
)
async def write_file(
    project_id: str,
    request: Request
):

    data = await request.json()


    path = str(
        data.get(
            "path",
            ""
        )
    ).strip()


    content = data.get(
        "content",
        ""
    )


    if not path:

        return JSONResponse(
            {
                "error":
                "Path is required"
            },
            status_code=400
        )


    if not isinstance(
        content,
        str
    ):

        return JSONResponse(
            {
                "error":
                "Content must be a string"
            },
            status_code=400
        )


    if len(content) > 1_000_000:

        return JSONResponse(
            {
                "error":
                "File is too large"
            },
            status_code=413
        )


    try:

        folder = project_path(
            project_id
        )

        relative = safe_relative_path(
            path
        )

        target = folder / relative

    except ValueError:

        return JSONResponse(
            {
                "error":
                "Invalid path"
            },
            status_code=400
        )


    target.parent.mkdir(
        parents=True,
        exist_ok=True
    )


    target.write_text(
        content,
        encoding="utf-8"
    )

    await db_save_file(
        project_id, relative.as_posix(), content
    )


    return {
        "ok": True,
        "path": path
    }


# =========================================================
# FILE HISTORY (list / restore earlier saved versions)
# =========================================================

@app.get("/api/projects/{project_id}/files/versions")
async def list_file_versions(project_id: str, path: str):

    if db_pool is None:
        return {"versions": []}

    try:
        relative = safe_relative_path(path)
    except ValueError:
        return JSONResponse({"error": "Invalid path"}, status_code=400)

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, content, is_binary, created_at "
            "FROM file_versions "
            "WHERE project_id = $1 AND path = $2 "
            "ORDER BY created_at DESC",
            project_id, relative.as_posix()
        )

    versions = []
    for row in rows:
        content = row["content"] or ""
        if row["is_binary"]:
            preview = "(binary file)"
        else:
            preview = content[:120].replace("\n", " ")
        versions.append({
            "id": row["id"],
            "created_at": row["created_at"].isoformat(),
            "preview": preview,
            "size": len(content)
        })

    return {"versions": versions}


@app.post("/api/projects/{project_id}/files/restore")
async def restore_file_version(project_id: str, request: Request):

    try:
        data = await request.json()
    except Exception:
        return JSONResponse(
            {"error": "Invalid request body"}, status_code=400
        )

    path = str(data.get("path", "")).strip()
    version_id = data.get("version_id")

    if not path or version_id is None:
        return JSONResponse(
            {"error": "path and version_id are required"},
            status_code=400
        )

    try:
        folder = project_path(project_id)
        relative = safe_relative_path(path)
    except ValueError:
        return JSONResponse({"error": "Invalid path"}, status_code=400)

    if db_pool is None:
        return JSONResponse(
            {"error": "Database not available"}, status_code=500
        )

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT content, is_binary FROM file_versions "
            "WHERE id = $1 AND project_id = $2 AND path = $3",
            version_id, project_id, relative.as_posix()
        )

    if row is None:
        return JSONResponse(
            {"error": "That version no longer exists"},
            status_code=404
        )

    content = row["content"] or ""
    is_binary = row["is_binary"]

    target = folder / relative
    target.parent.mkdir(parents=True, exist_ok=True)

    if is_binary:
        target.write_bytes(base64.b64decode(content))
    else:
        target.write_text(content, encoding="utf-8")

    # db_save_file() snapshots whatever's about to be overwritten
    # (i.e. the file's current content) before writing the
    # restored content - so restoring is itself undoable through
    # the same History list, never a one-way trip.
    await db_save_file(
        project_id, relative.as_posix(), content, is_binary
    )

    return {
        "path": relative.as_posix(),
        "content": content,
        "is_binary": is_binary
    }


# =========================================================
# LINT (inline diagnostics)
# =========================================================
#
# Two layers, cheapest/most-reliable first:
#   1. ast.parse() - always available, catches real syntax
#      errors (the file literally won't run).
#   2. pyflakes' Checker - catches "runs fine but is probably
#      wrong": unused imports, undefined names, unused local
#      variables, duplicate arguments, etc. Skipped entirely
#      (rather than erroring the request) if pyflakes isn't
#      installed, or if anything about it misbehaves - a lint
#      pass should never be able to break the editor.
#
# Diagnostics are returned as 0-based line/col so the frontend
# can hand them straight to CodeMirror without translating.

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _token_end_col(line_text: str, col: int) -> int:
    """
    Best-effort width for the squiggly underline: the
    identifier/word starting at `col`, or just one character
    if there isn't a clean word there (e.g. a bare ':').
    """

    match = _IDENTIFIER_RE.match(line_text, col)

    if match:
        return match.end()

    return min(col + 1, len(line_text)) if line_text else col + 1


def _lint_python_source(source: str, filename: str = "<file>"):
    diagnostics = []

    try:
        tree = ast.parse(source, filename=filename)

    except SyntaxError as exc:
        line = max((exc.lineno or 1) - 1, 0)
        col = max((exc.offset or 1) - 1, 0)

        diagnostics.append({
            "line": line,
            "col": col,
            "endCol": col + 1,
            "severity": "error",
            "message": exc.msg or "Invalid syntax",
            "source": "python"
        })

        return diagnostics

    except (ValueError, RecursionError):
        # Malformed source ast.parse can't even attempt (e.g.
        # a stray null byte) - fail quiet, no diagnostics.
        return diagnostics

    if PyflakesChecker is None:
        return diagnostics

    try:
        checker = PyflakesChecker(tree, filename=filename)
        messages = list(checker.messages)

    except Exception:
        # pyflakes choked on something CPython's own parser
        # accepted (has happened, is rare, is never worth a
        # 500 - the user just gets syntax-only diagnostics).
        return diagnostics

    source_lines = source.splitlines()

    for message in messages:
        try:
            line = max(message.lineno - 1, 0)
            col = max(getattr(message, "col", 0), 0)
            text = message.message % message.message_args

        except Exception:
            continue

        line_text = (
            source_lines[line]
            if line < len(source_lines)
            else ""
        )

        diagnostics.append({
            "line": line,
            "col": col,
            "endCol": _token_end_col(line_text, col),
            "severity": "warning",
            "message": text,
            "source": "pyflakes"
        })

    diagnostics.sort(key=lambda d: (d["line"], d["col"]))

    return diagnostics


@app.post(
    "/api/projects/{project_id}/lint"
)
async def lint_file(
    project_id: str,
    request: Request
):
    """
    Lints Python source and returns structured diagnostics.
    Takes the *unsaved* editor content directly (not a path
    read from disk) so it can run continuously as the user
    types, not just after Save.
    """

    data = await request.json()

    path = str(data.get("path", "")).strip()
    content = data.get("content", "")

    if not isinstance(content, str):
        return JSONResponse(
            {
                "error":
                "Content must be a string"
            },
            status_code=400
        )

    if not path.lower().endswith(".py"):
        return {
            "diagnostics": [],
            "linted": False
        }

    diagnostics = _lint_python_source(
        content,
        filename=path.rsplit("/", 1)[-1] or "<file>"
    )

    return {
        "diagnostics": diagnostics,
        "linted": True,
        "pyflakesAvailable": PyflakesChecker is not None
    }


# =========================================================
# AUTOCOMPLETE (IntelliSense)
# =========================================================
#
# Backed by Jedi, which does real static analysis - it knows
# what's on an object after ".", what a function's parameters
# are, and can see names from other files in the project (not
# just the one currently open). Like /lint, this works against
# whatever's in the editor right now, not the saved file.
#
# Jedi is skipped (not treated as an error) if it isn't
# installed - the frontend already has a local fallback built
# from document words + keywords/builtins, so autocomplete
# still works, just without the "understands your code" part.

def _jedi_completions(
    content: str,
    filename: str,
    line: int,
    column: int
):
    if jedi is None:
        return []

    try:
        # See _jedi_lock's comment above - this must be held for
        # the whole Script()+complete() call, not just complete(),
        # since Script() is what can trigger the compiled-
        # subprocess environment setup in the first place.
        with _jedi_lock:
            script = jedi.Script(code=content, path=filename)
            completions = script.complete(line=line, column=column)

    except Exception as exc:
        # Jedi is generally tolerant of broken/incomplete code
        # (that's the whole point, mid-typing), but it's still
        # third-party static analysis running on arbitrary user
        # text, and it does have real internal bugs on certain
        # stdlib stubs (seen in practice: a version-mismatch-
        # sensitive "cannot unpack non-iterable bool object").
        # Never let that 500 the request - log it server-side
        # (visible in Render's logs) and just fall back to no
        # smart completions for that one request; the frontend's
        # local candidates still carry the popup.
        print(
            "[/complete] jedi raised " +
            type(exc).__name__ + ": " + str(exc)[:200]
        )
        return []

    results = []

    for completion in completions[:40]:
        try:
            detail = ""

            try:
                sig = completion.get_signatures()

                if sig:
                    detail = sig[0].to_string()

            except Exception:
                detail = ""

            if not detail:
                detail = (completion.description or "")[:80]

            results.append({
                "text": completion.name,
                "type": completion.type,
                "detail": detail[:80]
            })

        except Exception:
            continue

    return results


@app.post(
    "/api/projects/{project_id}/complete"
)
async def complete_file(
    project_id: str,
    request: Request
):
    data = await request.json()

    path = str(data.get("path", "")).strip()
    content = data.get("content", "")

    try:
        line = int(data.get("line", 0))
        col = int(data.get("col", 0))

    except (TypeError, ValueError):
        return JSONResponse(
            {
                "error":
                "line and col must be integers"
            },
            status_code=400
        )

    if not isinstance(content, str):
        return JSONResponse(
            {
                "error":
                "Content must be a string"
            },
            status_code=400
        )

    if not path.lower().endswith(".py") or jedi is None:
        return {
            "completions": [],
            "available": jedi is not None
        }

    filename = path.rsplit("/", 1)[-1] or "<file>"

    try:
        # Jedi's own analysis can occasionally be slow on
        # large/unusual files, especially its first real pass on
        # a free-tier host - run it off the event loop and give
        # it a reasonably generous ceiling so one slow completion
        # request can't stall the terminal/other requests behind
        # it forever, but also doesn't get cut off before Jedi
        # genuinely finishes.
        results = await asyncio.wait_for(
            asyncio.to_thread(
                _jedi_completions,
                content,
                filename,
                line + 1,
                col
            ),
            timeout=6.0
        )

    except asyncio.TimeoutError:
        results = []

    return {
        "completions": results,
        "available": True
    }


# =========================================================
# GIT / GITHUB INTEGRATION
# =========================================================
#
# Two halves:
#   - Per-project git operations (status/stage/commit/diff/
#     push/pull) that shell out to the real `git` binary in
#     the project's own folder - same idea as the interactive
#     terminal, just structured instead of freeform.
#   - GitHub account connection (a Personal Access Token,
#     stored via the app_settings table above) and the two
#     things it unlocks: pushing a project to a new/existing
#     GitHub repo, and importing a GitHub repo as a new local
#     project.
#
# Important limitation, inherited from how this app persists
# data (see the PERSISTENCE section far above): .git folders
# are deliberately excluded from the Postgres sync that lets
# project files survive a restart on a host with no disk
# persistence (like Render's free tier). That means local
# commit history / staged state can be lost on a restart even
# though the files themselves survive - pushing to GitHub is
# the actual durable backup, not the local repo.

GITHUB_API_BASE = "https://api.github.com"


def _scrub_token(text: str, token: str) -> str:
    """
    A GitHub token is embedded in the remote URL for push/pull
    (see git_push below) - git itself can echo that URL back in
    an error message (e.g. an auth failure), so any text that
    might contain it gets scrubbed before it's ever sent to the
    frontend or logged.
    """

    if not text or not token:
        return text

    return text.replace(token, "***")


async def _run_git(folder: Path, args, timeout: int = 30):
    """
    Runs `git <args>` in `folder`. Returns
    (returncode, stdout, stderr) - never raises for a failed git
    command (a merge conflict, no upstream, etc. are all normal,
    expected outcomes the caller decides how to present), only
    for git genuinely not being runnable at all.
    """

    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"

    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-c", "safe.directory=*", *args,
            cwd=str(folder),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )

        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return 1, "", "git command timed out"

    except FileNotFoundError:
        return 1, "", "git is not installed on the server"

    return (
        proc.returncode,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace")
    )


async def _ensure_git_identity(folder: Path):
    """
    A commit fails outright with no configured user.name/email.
    Since this is a single-user app with no concept of "who" is
    committing beyond "whoever is using this IDE", a generic
    local (repo-scoped, not global) identity is set the first
    time it's missing rather than making the user configure
    this themselves before their first commit works.
    """

    code, out, _ = await _run_git(folder, ["config", "user.email"])
    if code != 0 or not out.strip():
        await _run_git(
            folder, ["config", "user.email", "ide@local"]
        )

    code, out, _ = await _run_git(folder, ["config", "user.name"])
    if code != 0 or not out.strip():
        await _run_git(
            folder, ["config", "user.name", "Python IDE"]
        )


def _github_api_sync(
    token: str, method: str, path: str, body=None
):

    import urllib.error
    import urllib.request

    url = GITHUB_API_BASE + path
    data = (
        json.dumps(body).encode("utf-8")
        if body is not None else None
    )

    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", "Bearer " + token)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("User-Agent", "python-ide-app")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")

    if data is not None:
        request.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            raw = resp.read()
            status = resp.status

    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code

    except Exception as exc:
        return 0, {"message": str(exc)}

    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        parsed = {"message": raw.decode("utf-8", "replace")[:300]}

    return status, parsed


async def _github_api(token: str, method: str, path: str, body=None):
    return await asyncio.to_thread(
        _github_api_sync, token, method, path, body
    )


def _authed_clone_url(token: str, https_url: str) -> str:
    """
    Turns https://github.com/owner/repo.git into
    https://<token>@github.com/owner/repo.git, so git can push/
    pull/clone without an interactive credential prompt (which
    GIT_TERMINAL_PROMPT=0 would otherwise just fail on outright).
    """

    if https_url.startswith("https://"):
        return "https://" + token + "@" + https_url[len("https://"):]

    return https_url


# ---------------------------------------------------------
# GitHub account connection
# ---------------------------------------------------------

@app.get("/api/github/status")
async def github_status():

    token = await db_get_setting("github_token")

    if not token:
        return {"connected": False}

    return {
        "connected": True,
        "username": await db_get_setting("github_username")
    }


@app.post("/api/github/connect")
async def github_connect(request: Request):

    data = await request.json()
    token = str(data.get("token", "")).strip()

    if not token:
        return JSONResponse(
            {"error": "Token cannot be empty"},
            status_code=400
        )

    status, user = await _github_api(token, "GET", "/user")

    if status != 200 or not isinstance(user, dict) or "login" not in user:
        message = (
            user.get("message")
            if isinstance(user, dict) else None
        ) or "Could not verify that token with GitHub."
        return JSONResponse(
            {"error": message},
            status_code=400
        )

    await db_set_setting("github_token", token)
    await db_set_setting("github_username", user["login"])

    return {
        "connected": True,
        "username": user["login"]
    }


@app.post("/api/github/disconnect")
async def github_disconnect():

    await db_delete_setting("github_token")
    await db_delete_setting("github_username")

    return {"connected": False}


@app.get("/api/github/repos")
async def github_repos():

    token = await db_get_setting("github_token")

    if not token:
        return JSONResponse(
            {"error": "GitHub isn't connected yet."},
            status_code=400
        )

    status, data = await _github_api(
        token, "GET",
        "/user/repos?per_page=60&sort=updated&affiliation=owner,collaborator"
    )

    if status != 200 or not isinstance(data, list):
        message = (
            data.get("message")
            if isinstance(data, dict) else None
        ) or "Could not load repositories from GitHub."
        return JSONResponse(
            {"error": message},
            status_code=502
        )

    return {
        "repos": [
            {
                "full_name": repo.get("full_name"),
                "private": repo.get("private", False),
                "description": repo.get("description") or "",
                "updated_at": repo.get("updated_at"),
                "default_branch": repo.get("default_branch", "main")
            }
            for repo in data
        ]
    }


@app.post("/api/github/import")
async def github_import(request: Request):

    data = await request.json()
    full_name = str(data.get("full_name", "")).strip()
    manual_url = str(data.get("url", "")).strip()
    requested_name = str(data.get("name", "")).strip()

    if not full_name and not manual_url:
        return JSONResponse(
            {"error": "Provide a repo to import."},
            status_code=400
        )

    token = await db_get_setting("github_token")

    if full_name:
        clone_url = f"https://github.com/{full_name}.git"
        default_name = full_name.split("/")[-1]
    else:
        clone_url = manual_url
        default_name = (
            manual_url.rstrip("/").rsplit("/", 1)[-1]
            .removesuffix(".git")
        ) or "imported-project"

    source_url = (
        _authed_clone_url(token, clone_url)
        if token else clone_url
    )

    name = requested_name or default_name
    name = "".join(
        char if char.isalnum() or char in "-_" else "-"
        for char in name
    ) or "imported-project"

    project_id = f"{name}-{uuid.uuid4().hex[:8]}"
    folder = project_path(project_id)

    code, _, stderr = await _run_git(
        PROJECTS_DIR,
        ["clone", "--depth", "1", source_url, str(folder)],
        timeout=120
    )

    if code != 0:
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
        return JSONResponse(
            {
                "error":
                _scrub_token(
                    stderr.strip() or "git clone failed.",
                    token or ""
                )
            },
            status_code=502
        )

    await db_save_project(project_id, name)
    await db_full_resync(project_id)

    return {
        "id": project_id,
        "name": name
    }


# ---------------------------------------------------------
# Per-project git operations
# ---------------------------------------------------------

def _parse_git_status(raw: str):

    lines = raw.split("\n")
    branch = None
    ahead = 0
    behind = 0
    has_upstream = False
    staged = []
    unstaged = []
    untracked = []

    for line in lines:

        if not line:
            continue

        if line.startswith("## "):
            header = line[3:]

            if "..." in header:
                has_upstream = True
                branch = header.split("...")[0]

                if "[" in header:
                    bracket = header[header.index("["):]

                    ahead_match = re.search(r"ahead (\d+)", bracket)
                    behind_match = re.search(r"behind (\d+)", bracket)

                    if ahead_match:
                        ahead = int(ahead_match.group(1))
                    if behind_match:
                        behind = int(behind_match.group(1))

            elif "(no branch)" in header:
                branch = "(detached)"
            else:
                branch = header.split(" ")[0]

            continue

        if len(line) < 3:
            continue

        x, y, rest = line[0], line[1], line[3:]
        path = rest.split(" -> ")[-1]

        if x == "?" and y == "?":
            untracked.append({"path": path, "status": "U"})
            continue

        if x != " ":
            staged.append({"path": path, "status": x})

        if y != " ":
            unstaged.append({"path": path, "status": y})

    return {
        "is_repo": True,
        "branch": branch,
        "has_upstream": has_upstream,
        "ahead": ahead,
        "behind": behind,
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked
    }


@app.get("/api/projects/{project_id}/git/status")
async def git_status(project_id: str):

    try:
        folder = project_path(project_id)
    except ValueError:
        return JSONResponse(
            {"error": "Invalid project"}, status_code=400
        )

    if not (folder / ".git").is_dir():
        return {"is_repo": False}

    code, out, stderr = await _run_git(
        folder, ["status", "--porcelain=v1", "--branch"]
    )

    if code != 0:
        return JSONResponse(
            {"error": stderr.strip() or "git status failed."},
            status_code=502
        )

    return _parse_git_status(out)


@app.post("/api/projects/{project_id}/git/init")
async def git_init(project_id: str):

    try:
        folder = project_path(project_id)
    except ValueError:
        return JSONResponse(
            {"error": "Invalid project"}, status_code=400
        )

    if (folder / ".git").is_dir():
        return {"initialized": True, "already_existed": True}

    code, _, stderr = await _run_git(folder, ["init", "-b", "main"])

    if code != 0:
        return JSONResponse(
            {"error": stderr.strip() or "git init failed."},
            status_code=502
        )

    await _ensure_git_identity(folder)

    return {"initialized": True, "already_existed": False}


@app.post("/api/projects/{project_id}/git/stage")
async def git_stage(project_id: str, request: Request):

    try:
        folder = project_path(project_id)
    except ValueError:
        return JSONResponse(
            {"error": "Invalid project"}, status_code=400
        )

    data = await request.json()
    paths = data.get("paths") or []
    stage_all = bool(data.get("all"))

    args = ["add", "-A"] if stage_all else ["add", "--"] + list(paths)

    if not stage_all and not paths:
        return {"staged": True}

    code, _, stderr = await _run_git(folder, args)

    if code != 0:
        return JSONResponse(
            {"error": stderr.strip() or "git add failed."},
            status_code=502
        )

    return {"staged": True}


@app.post("/api/projects/{project_id}/git/unstage")
async def git_unstage(project_id: str, request: Request):

    try:
        folder = project_path(project_id)
    except ValueError:
        return JSONResponse(
            {"error": "Invalid project"}, status_code=400
        )

    data = await request.json()
    paths = data.get("paths") or []
    unstage_all = bool(data.get("all"))

    args = (
        ["reset"] if unstage_all
        else ["restore", "--staged", "--"] + list(paths)
    )

    if not unstage_all and not paths:
        return {"unstaged": True}

    code, _, stderr = await _run_git(folder, args)

    if code != 0:
        return JSONResponse(
            {"error": stderr.strip() or "git restore failed."},
            status_code=502
        )

    return {"unstaged": True}


@app.get("/api/projects/{project_id}/git/diff")
async def git_diff(project_id: str, path: str, staged: int = 0):

    try:
        folder = project_path(project_id)
        relative = safe_relative_path(path)
    except ValueError:
        return JSONResponse(
            {"error": "Invalid project or path"}, status_code=400
        )

    if staged:
        args = ["diff", "--cached", "--", relative.as_posix()]
    else:
        # An untracked file has nothing to diff against in the
        # index - `git diff --no-index` against /dev/null gives
        # the same "every line added" view a brand-new file
        # should show, instead of coming back empty.
        status_code, status_out, _ = await _run_git(
            folder, ["status", "--porcelain=v1", "--",
                     relative.as_posix()]
        )
        is_untracked = (
            status_code == 0 and
            status_out.startswith("??")
        )

        if is_untracked:
            args = [
                "diff", "--no-index", "--",
                "/dev/null", relative.as_posix()
            ]
        else:
            args = ["diff", "--", relative.as_posix()]

    code, out, stderr = await _run_git(folder, args)

    # git diff --no-index exits 1 when it finds differences (the
    # normal case here, not a failure) - only treat >1 as an
    # actual error.
    if code > 1:
        return JSONResponse(
            {"error": stderr.strip() or "git diff failed."},
            status_code=502
        )

    return {"diff": out[:40_000]}


@app.post("/api/projects/{project_id}/git/commit")
async def git_commit(project_id: str, request: Request):

    try:
        folder = project_path(project_id)
    except ValueError:
        return JSONResponse(
            {"error": "Invalid project"}, status_code=400
        )

    data = await request.json()
    message = str(data.get("message", "")).strip()

    if not message:
        return JSONResponse(
            {"error": "Commit message cannot be empty."},
            status_code=400
        )

    await _ensure_git_identity(folder)

    code, out, stderr = await _run_git(
        folder, ["commit", "-m", message]
    )

    if code != 0:
        return JSONResponse(
            {
                "error":
                stderr.strip() or out.strip() or
                "Nothing to commit."
            },
            status_code=400
        )

    return {"committed": True}


@app.get("/api/projects/{project_id}/git/log")
async def git_log(project_id: str, limit: int = 20):

    try:
        folder = project_path(project_id)
    except ValueError:
        return JSONResponse(
            {"error": "Invalid project"}, status_code=400
        )

    if not (folder / ".git").is_dir():
        return {"commits": []}

    code, out, _ = await _run_git(
        folder,
        [
            "log", f"-n{max(1, min(limit, 100))}",
            "--pretty=format:%h\x1f%an\x1f%ar\x1f%s"
        ]
    )

    if code != 0:
        return {"commits": []}

    commits = []

    for line in out.split("\n"):
        if not line:
            continue
        parts = line.split("\x1f")
        if len(parts) == 4:
            commits.append({
                "hash": parts[0],
                "author": parts[1],
                "when": parts[2],
                "message": parts[3]
            })

    return {"commits": commits}


@app.post("/api/projects/{project_id}/git/push")
async def git_push(project_id: str):

    try:
        folder = project_path(project_id)
    except ValueError:
        return JSONResponse(
            {"error": "Invalid project"}, status_code=400
        )

    if not (folder / ".git").is_dir():
        return JSONResponse(
            {"error": "This project isn't a git repository yet."},
            status_code=400
        )

    token = await db_get_setting("github_token")
    created_repo = False

    code, remote_url, _ = await _run_git(
        folder, ["remote", "get-url", "origin"]
    )
    has_remote = code == 0 and remote_url.strip()

    if not has_remote:

        if not token:
            return JSONResponse(
                {
                    "error":
                    "No remote is set up for this project, and "
                    "no GitHub account is connected to create "
                    "one automatically."
                },
                status_code=400
            )

        status, repo = await _github_api(
            token, "POST", "/user/repos",
            {"name": project_id, "private": True}
        )

        if status not in (200, 201):
            message = (
                repo.get("message")
                if isinstance(repo, dict) else None
            ) or "Could not create a GitHub repository."
            return JSONResponse({"error": message}, status_code=502)

        created_repo = True
        html_url = repo.get("html_url", "")
        clone_url = repo.get("clone_url", html_url + ".git")

        code, _, stderr = await _run_git(
            folder,
            ["remote", "add", "origin",
             _authed_clone_url(token, clone_url)]
        )

        if code != 0:
            return JSONResponse(
                {"error": stderr.strip() or "Could not set the remote."},
                status_code=502
            )

    _, branch_out, _ = await _run_git(
        folder, ["branch", "--show-current"]
    )
    branch = branch_out.strip() or "main"

    code, out, stderr = await _run_git(
        folder, ["push", "-u", "origin", branch], timeout=60
    )

    if code != 0:
        return JSONResponse(
            {
                "error":
                _scrub_token(
                    stderr.strip() or out.strip() or "git push failed.",
                    token or ""
                )
            },
            status_code=502
        )

    _, remote_url_out, _ = await _run_git(
        folder, ["remote", "get-url", "origin"]
    )

    return {
        "pushed": True,
        "created_repo": created_repo,
        "repo_url":
            _scrub_token(remote_url_out.strip(), token or "")
            .removesuffix(".git")
    }


@app.post("/api/projects/{project_id}/git/pull")
async def git_pull(project_id: str):

    try:
        folder = project_path(project_id)
    except ValueError:
        return JSONResponse(
            {"error": "Invalid project"}, status_code=400
        )

    if not (folder / ".git").is_dir():
        return JSONResponse(
            {"error": "This project isn't a git repository yet."},
            status_code=400
        )

    token = await db_get_setting("github_token")

    code, out, stderr = await _run_git(
        folder, ["pull", "--no-rebase"], timeout=60
    )

    # Pulling changes files on disk directly, bypassing the file-
    # save API entirely - without this, those changes would only
    # live on local disk and vanish on the next restart.
    await db_full_resync(project_id)

    if code != 0:
        return JSONResponse(
            {
                "error":
                _scrub_token(
                    stderr.strip() or out.strip() or "git pull failed.",
                    token or ""
                )
            },
            status_code=502
        )

    return {"pulled": True, "summary": out.strip()[:500]}


# =========================================================
# DELETE FILE
# =========================================================

@app.delete(
    "/api/projects/{project_id}/file"
)
async def delete_file(
    project_id: str,
    path: str
):

    try:

        folder = project_path(
            project_id
        )

        relative = safe_relative_path(
            path
        )

        target = folder / relative

    except ValueError:

        return JSONResponse(
            {
                "error":
                "Invalid path"
            },
            status_code=400
        )


    if not target.is_file():

        return JSONResponse(
            {
                "error":
                "File not found"
            },
            status_code=404
        )


    target.unlink()

    await db_delete_file(
        project_id, relative.as_posix()
    )


    return {
        "ok": True
    }


# =========================================================
# FOLDERS
# =========================================================

@app.post(
    "/api/projects/{project_id}/folder"
)
async def create_folder(
    project_id: str,
    request: Request
):

    data = await request.json()

    path = str(
        data.get("path", "")
    ).strip()

    if not path:

        return JSONResponse(
            {"error": "Path is required"},
            status_code=400
        )

    try:

        folder = project_path(project_id)
        relative = safe_relative_path(path)
        target = folder / relative

    except ValueError:

        return JSONResponse(
            {"error": "Invalid path"},
            status_code=400
        )

    if target.exists():

        return JSONResponse(
            {
                "error":
                "A file or folder already exists at that path"
            },
            status_code=409
        )

    target.mkdir(parents=True)

    await db_save_folder(project_id, relative.as_posix())

    return {
        "ok": True,
        "path": relative.as_posix()
    }


@app.delete(
    "/api/projects/{project_id}/folder"
)
async def delete_folder(
    project_id: str,
    path: str
):

    try:

        folder = project_path(project_id)
        relative = safe_relative_path(path)
        target = folder / relative

    except ValueError:

        return JSONResponse(
            {"error": "Invalid path"},
            status_code=400
        )

    if not target.is_dir():

        return JSONResponse(
            {"error": "Folder not found"},
            status_code=404
        )

    shutil.rmtree(target)

    await db_delete_folder(project_id, relative.as_posix())

    return {
        "ok": True
    }


# =========================================================
# UPLOAD FILES (binary-safe)
# =========================================================

@app.post(
    "/api/projects/{project_id}/upload"
)
async def upload_files(
    project_id: str,
    target_dir: str = Form(""),
    files: list[UploadFile] = File(...)
):

    try:

        folder = project_path(project_id)

        target_folder = folder

        if target_dir.strip():
            target_folder = (
                folder / safe_relative_path(target_dir.strip())
            )

    except ValueError:

        return JSONResponse(
            {"error": "Invalid path"},
            status_code=400
        )

    if not folder.is_dir():

        return JSONResponse(
            {"error": "Project not found"},
            status_code=404
        )

    saved = []
    skipped = []

    for upload in files:

        raw_name = upload.filename or ""

        # Browsers can send a webkitRelativePath-style name for
        # folder uploads (e.g. "assets/logo.png") - keep that
        # nested structure if so, otherwise it's just a filename.
        try:
            relative = safe_relative_path(raw_name)
        except ValueError:
            skipped.append(raw_name)
            continue

        content_bytes = await upload.read()

        if len(content_bytes) > 5_000_000:
            skipped.append(raw_name)
            continue

        if raw_name.lower().endswith(".zip"):

            extracted = extract_zip_bytes(
                content_bytes, target_folder, folder
            )

            for extracted_file in extracted["files"]:
                await db_save_file(
                    project_id,
                    extracted_file["path"],
                    extracted_file["content"],
                    extracted_file["is_binary"]
                )

            for extracted_folder in extracted["folders"]:
                await db_save_folder(
                    project_id, extracted_folder
                )

            saved.extend(
                p["path"] for p in extracted["files"]
            )

            continue

        target = target_folder / relative
        target.parent.mkdir(parents=True, exist_ok=True)

        target.write_bytes(content_bytes)

        db_relative = target.relative_to(folder).as_posix()

        try:
            text_content = content_bytes.decode("utf-8")
            await db_save_file(
                project_id, db_relative, text_content, False
            )
        except UnicodeDecodeError:
            await db_save_file(
                project_id,
                db_relative,
                base64.b64encode(content_bytes).decode("ascii"),
                True
            )

        saved.append(db_relative)

    return {
        "ok": True,
        "saved": saved,
        "skipped": skipped
    }


def extract_zip_bytes(
    zip_bytes: bytes, target_folder: Path, project_root: Path
):
    """
    Extracts a zip's contents into target_folder, guarding against
    zip-slip (entries whose name escapes the target folder via
    ".." or an absolute path). Returns the files/folders written,
    with paths relative to project_root, for the caller to mirror
    into the database.
    """

    import io

    written_files = []
    written_folders = []

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:

        for entry in archive.infolist():

            try:
                relative = safe_relative_path(entry.filename)
            except ValueError:
                continue

            destination = target_folder / relative

            # Belt-and-suspenders on top of safe_relative_path:
            # confirm the resolved path really is inside the
            # project root before writing anything.
            try:
                destination.resolve().relative_to(
                    project_root.resolve()
                )
            except ValueError:
                continue

            if entry.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                written_folders.append(
                    destination.relative_to(
                        project_root
                    ).as_posix()
                )
                continue

            destination.parent.mkdir(parents=True, exist_ok=True)

            data = archive.read(entry)

            if len(data) > 5_000_000:
                continue

            destination.write_bytes(data)

            db_relative = destination.relative_to(
                project_root
            ).as_posix()

            try:
                text_content = data.decode("utf-8")
                written_files.append({
                    "path": db_relative,
                    "content": text_content,
                    "is_binary": False
                })
            except UnicodeDecodeError:
                written_files.append({
                    "path": db_relative,
                    "content":
                        base64.b64encode(data).decode("ascii"),
                    "is_binary": True
                })

    return {
        "files": written_files,
        "folders": written_folders
    }


# =========================================================
# RENAME / MOVE FILE
# =========================================================

@app.post(
    "/api/projects/{project_id}/file/rename"
)
async def rename_file(
    project_id: str,
    request: Request
):

    try:

        folder = project_path(
            project_id
        )

    except ValueError:

        return JSONResponse(
            {
                "error":
                "Invalid project ID"
            },
            status_code=400
        )


    if not folder.is_dir():

        return JSONResponse(
            {
                "error":
                "Project not found"
            },
            status_code=404
        )


    data = await request.json()

    old_path = str(
        data.get(
            "old_path",
            ""
        )
    ).strip()

    new_path = str(
        data.get(
            "new_path",
            ""
        )
    ).strip()


    if not old_path or not new_path:

        return JSONResponse(
            {
                "error":
                "old_path and new_path are required"
            },
            status_code=400
        )


    try:

        old_relative = safe_relative_path(
            old_path
        )

        new_relative = safe_relative_path(
            new_path
        )

        source = folder / old_relative

        destination = folder / new_relative

    except ValueError:

        return JSONResponse(
            {
                "error":
                "Invalid path"
            },
            status_code=400
        )


    if not source.exists():

        return JSONResponse(
            {
                "error":
                "File or folder not found"
            },
            status_code=404
        )


    if destination.exists():

        return JSONResponse(
            {
                "error":
                "Something already exists at that path"
            },
            status_code=409
        )


    destination.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    is_folder = source.is_dir()

    source.rename(
        destination
    )

    if is_folder:

        await db_move_prefix(
            project_id,
            old_relative.as_posix(),
            new_relative.as_posix()
        )

    else:

        try:
            moved_content = destination.read_text(
                encoding="utf-8"
            )
            is_binary = False
        except UnicodeDecodeError:
            moved_content = base64.b64encode(
                destination.read_bytes()
            ).decode("ascii")
            is_binary = True

        await db_save_file(
            project_id,
            new_relative.as_posix(),
            moved_content,
            is_binary
        )

        await db_delete_file(
            project_id, old_relative.as_posix()
        )


    return {
        "ok": True,
        "path": new_relative.as_posix()
    }


# =========================================================
# RUN ENTIRE PROJECT
# =========================================================

@app.post(
    "/api/projects/{project_id}/run"
)
async def run_project(
    project_id: str,
    request: Request
):

    try:

        project = project_path(
            project_id
        )

    except ValueError:

        return JSONResponse(
            {
                "error":
                "Invalid project ID"
            },
            status_code=400
        )


    if not project.is_dir():

        return JSONResponse(
            {
                "error":
                "Project not found"
            },
            status_code=404
        )


    data = await request.json()

    entry_file = str(
        data.get(
            "entry",
            "main.py"
        )
    ).strip()


    try:

        entry = safe_relative_path(
            entry_file
        )

    except ValueError:

        return JSONResponse(
            {
                "error":
                "Invalid entry file"
            },
            status_code=400
        )


    source_entry = project / entry


    if not source_entry.is_file():

        return JSONResponse(
            {
                "error":
                f"Entry file '{entry_file}' not found"
            },
            status_code=404
        )


    if source_entry.suffix != ".py":

        return JSONResponse(
            {
                "error":
                "Entry file must be a Python file"
            },
            status_code=400
        )


    # -----------------------------------------------------
    # Create an isolated temporary copy of the project.
    #
    # This means:
    #
    # project/
    #   main.py
    #   utils.py
    #   config.py
    #
    # becomes:
    #
    # temp/
    #   main.py
    #   utils.py
    #   config.py
    #
    # Python can therefore import the other files normally.
    # -----------------------------------------------------

    with tempfile.TemporaryDirectory() as temp_dir:

        execution_dir = Path(
            temp_dir
        )


        try:

            shutil.copytree(
                project,
                execution_dir,
                dirs_exist_ok=True
            )

        except Exception as error:

            return JSONResponse(
                {
                    "error":
                    "Could not prepare project: "
                    + str(error)
                },
                status_code=500
            )


        execution_file = (
            execution_dir /
            entry
        )


        try:

            process = subprocess.run(

                [
                    # Use the exact interpreter running this server
                    # (sys.executable) instead of the bare "python"
                    # command. On many systems - most Linux distros,
                    # macOS, and some cloud environments - only
                    # "python3" is on PATH, not "python". That mismatch
                    # made every run fail with a "No such file or
                    # directory: 'python'" error even though the code
                    # itself was fine.
                    sys.executable,
                    "-u",
                    str(execution_file)
                ],

                cwd=str(
                    execution_dir
                ),

                capture_output=True,

                text=True,

                timeout=5

            )


            return {

                "stdout":
                    process.stdout,

                "stderr":
                    process.stderr,

                "returncode":
                    process.returncode

            }


        except subprocess.TimeoutExpired:

            return {

                "stdout": "",

                "stderr":
                    "Execution timed out after 5 seconds.",

                "returncode": -1

            }


        except Exception as error:

            return {

                "stdout": "",

                "stderr":
                    str(error),

                "returncode": -1

            }


# =========================================================
# INTERACTIVE TERMINAL (real shell over WebSocket + PTY)
# =========================================================
#
# This is a genuine, interactive terminal attached to a real
# shell process running inside the project's folder - not a
# canned "run and capture output" call. That's what makes
# `pip install <package>`, running a script with `python
# file.py` (including ones that call input()), `ls`, `git`,
# long-running programs, etc. all work exactly as they would
# in a local terminal or in VS Code's integrated terminal.

TERMINAL_SHELL = (
    shutil.which("bash")
    or shutil.which("sh")
    or "/bin/sh"
)
TERMINAL_IS_BASH = os.path.basename(TERMINAL_SHELL) == "bash"

# A minimal, self-contained bash rc file for the in-browser
# terminal. The base image's own ~/.bashrc (root's, in this
# container) sets its own PS1 unconditionally, which is what
# was clobbering the short colored prompt below and leaving
# the terminal showing the full "root@<long-container-id>:
# /full/path#" line - noisy and easy to lose the actual
# output in. Pointing bash at this file instead (via
# --rcfile) sidesteps that entirely, and also turns color on
# for ls/grep the way a normal dev machine's shell would.
_TERM_RC_PATH = os.path.join(
    tempfile.gettempdir(), "python_ide_termrc.sh"
)
_TERM_RC_CONTENT = r"""
if command -v dircolors >/dev/null 2>&1; then
    eval "$(dircolors -b 2>/dev/null)"
fi
alias ls='ls --color=auto'
alias grep='grep --color=auto'
alias fgrep='fgrep --color=auto'
alias egrep='egrep --color=auto'
if diff --color=auto /dev/null /dev/null >/dev/null 2>&1; then
    alias diff='diff --color=auto'
fi
export CLICOLOR=1
export FORCE_COLOR=1
# Short + colorful: cyan-green folder name, blue prompt
# symbol ('#' for root, '$' otherwise, via bash's \$).
export PS1='\[\e[38;5;114m\]\W\[\e[0m\] \[\e[38;5;81m\]\$\[\e[0m\] '
"""


def _ensure_term_rc() -> str:
    """
    Write (once) the terminal's rc file to a fixed path in
    /tmp so bash can be pointed at it with --rcfile. Cheap
    to re-write on every call, so no need to guard against
    concurrent servers/reloads disagreeing about its content.
    """

    try:
        with open(_TERM_RC_PATH, "w") as rc_file:
            rc_file.write(_TERM_RC_CONTENT)

    except Exception:
        pass

    return _TERM_RC_PATH


async def _reap_child(pid: int) -> None:
    """
    Wait for a terminal's shell process to exit and reap it,
    escalating to SIGKILL if it lingers. Runs as a detached
    background task so closing a websocket never has to
    block on this.
    """

    for _ in range(15):

        try:
            reaped_pid, _status = os.waitpid(
                pid,
                os.WNOHANG
            )

        except ChildProcessError:
            return

        if reaped_pid == pid:
            return

        await asyncio.sleep(0.2)

    try:
        os.kill(pid, signal.SIGKILL)

    except ProcessLookupError:
        pass

    try:
        os.waitpid(pid, 0)

    except ChildProcessError:
        pass


@app.websocket(
    "/ws/projects/{project_id}/terminal"
)
async def project_terminal(
    websocket: WebSocket,
    project_id: str
):

    await websocket.accept()

    try:
        project = project_path(project_id)

    except ValueError:
        await websocket.close(code=4000)
        return

    if not project.is_dir():
        await websocket.close(code=4004)
        return

    if pty is None:

        await websocket.send_text(
            json.dumps({
                "type": "error",
                "message":
                    "Interactive terminals are not "
                    "supported on this server."
            })
        )

        await websocket.close(code=1011)
        return

    try:
        pid, fd = pty.fork()

    except OSError as error:

        await websocket.send_text(
            json.dumps({
                "type": "error",
                "message":
                    "Could not start a terminal: "
                    + str(error)
            })
        )

        await websocket.close(code=1011)
        return

    if pid == 0:

        # ---------------------------------------------
        # CHILD PROCESS: this becomes the interactive
        # shell. pty.fork() already wired fds 0/1/2 to
        # the pty slave, so from here on this process
        # *is* the terminal.
        # ---------------------------------------------

        try:
            os.chdir(str(project))

        except Exception:
            pass

        # Close anything else inherited from the server
        # process (listening sockets, other clients'
        # connections, log files) so the shell doesn't
        # hang on to them.
        try:
            os.closerange(3, 1024)

        except Exception:
            pass

        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["PYTHONUNBUFFERED"] = "1"
        env.setdefault("LANG", "C.UTF-8")
        env.setdefault("LC_ALL", "C.UTF-8")

        try:
            if TERMINAL_IS_BASH:
                os.execvpe(
                    TERMINAL_SHELL,
                    [
                        TERMINAL_SHELL,
                        "--rcfile",
                        _ensure_term_rc()
                    ],
                    env
                )
            else:
                env["PS1"] = (
                    "\\[\\e[36m\\]\\W\\[\\e[0m\\] $ "
                )
                os.execvpe(
                    TERMINAL_SHELL,
                    [TERMINAL_SHELL],
                    env
                )

        except Exception:
            os._exit(1)

    # -----------------------------------------------------
    # PARENT PROCESS continues here, proxying bytes between
    # the websocket and the pty's master file descriptor.
    # -----------------------------------------------------

    os.set_blocking(fd, False)

    loop = asyncio.get_running_loop()

    # An optional "run this once the shell is ready" command,
    # e.g. `?cmd=python3+-u+%27main.py%27`. Typing it from the
    # *server* side, timed off the shell's own first output,
    # avoids a real race: writing into a pty before the shell
    # has finished starting up can intermittently swallow or
    # garble the first thing typed into it.
    initial_command = websocket.query_params.get("cmd")
    pending_output = b""

    if initial_command:

        deadline = loop.time() + 3.0

        while loop.time() < deadline:

            await asyncio.sleep(0.02)

            try:
                chunk = os.read(fd, 65536)

            except (BlockingIOError, OSError):
                chunk = b""

            if chunk:
                pending_output += chunk
                break

        try:
            os.write(
                fd,
                (initial_command + "\r").encode(
                    "utf-8",
                    errors="ignore"
                )
            )

        except OSError:
            pass

    if pending_output:

        try:
            await websocket.send_bytes(pending_output)

        except Exception:
            pass

    output_queue = asyncio.Queue()

    def _on_readable():

        try:
            data = os.read(fd, 65536)

        except OSError:
            data = b""

        if not data:

            try:
                loop.remove_reader(fd)

            except Exception:
                pass

        output_queue.put_nowait(data)

    loop.add_reader(fd, _on_readable)

    async def pump_output():

        while True:

            data = await output_queue.get()

            if not data:

                try:
                    await websocket.send_text(
                        json.dumps({"type": "exit"})
                    )

                except Exception:
                    pass

                break

            try:
                await websocket.send_bytes(data)

            except Exception:
                break

    async def pump_input():

        while True:

            message = await websocket.receive()

            if message["type"] == "websocket.disconnect":
                break

            text = message.get("text")

            if text is not None:

                try:
                    payload = json.loads(text)

                except ValueError:
                    continue

                kind = payload.get("type")

                if kind == "input":

                    data = payload.get("data", "")

                    try:
                        os.write(
                            fd,
                            data.encode(
                                "utf-8",
                                errors="ignore"
                            )
                        )

                    except OSError:
                        break

                elif kind == "resize":

                    try:
                        cols = int(payload.get("cols", 80))
                        rows = int(payload.get("rows", 24))

                        winsize = struct.pack(
                            "HHHH", rows, cols, 0, 0
                        )

                        fcntl.ioctl(
                            fd,
                            termios.TIOCSWINSZ,
                            winsize
                        )

                    except Exception:
                        pass

                continue

            raw = message.get("bytes")

            if raw:

                try:
                    os.write(fd, raw)

                except OSError:
                    break

    output_task = asyncio.create_task(pump_output())
    input_task = asyncio.create_task(pump_input())

    # The shell can change files in ways the file-editor API
    # never sees (pip install, rm, mv, an editor run inside
    # the terminal itself). Periodically reconcile the DB to
    # whatever is actually on disk while the session is open,
    # in case the server restarts before the user closes the
    # tab - and once more, for certain, when it closes.
    async def periodic_resync():

        while True:
            await asyncio.sleep(20)
            await db_full_resync(project_id)
            await db_sync_pip_packages()

    resync_task = asyncio.create_task(periodic_resync())

    try:
        await asyncio.wait(
            {output_task, input_task},
            return_when=asyncio.FIRST_COMPLETED
        )

    finally:

        for task in (output_task, input_task, resync_task):
            task.cancel()

        try:
            loop.remove_reader(fd)

        except Exception:
            pass

        try:
            os.kill(pid, signal.SIGHUP)

        except ProcessLookupError:
            pass

        try:
            os.close(fd)

        except OSError:
            pass

        asyncio.create_task(
            _reap_child(pid)
        )

        await db_full_resync(project_id)
        await db_sync_pip_packages()

        try:
            await websocket.close()

        except Exception:
            pass


# =========================================================
# LIVE CAMERA (phone camera -> OpenCV -> back to browser)
# =========================================================
#
# The server has no camera of its own - it's a remote container,
# so `cv2.VideoCapture(0)` would have nothing to open here. What
# this does instead: the browser captures the *user's* phone
# camera, ships each frame to this endpoint as a JPEG over a
# websocket, the project's own camera.py runs on it with OpenCV,
# and the result streams back to be shown live. Editing and
# saving camera.py takes effect on the very next frame - no
# restart needed.

CAMERA_STARTER_CONTENT = '''"""
Powers the live Camera tab. For every frame the server gets
from your phone's camera, it calls process_frame() below and
streams back whatever you return. Edit this, hit save, and
the next frame uses your new code - no restart needed.

`frame` is a BGR NumPy image (OpenCV's usual color order).
Return a NumPy image - color or grayscale - to display it.
"""
import cv2


def process_frame(frame):
    # Try one of these, or write your own OpenCV code.

    # Grayscale (on by default so you can see it's working):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return gray

    # Edge detection:
    # edges = cv2.Canny(frame, 100, 200)
    # return edges

    # Face detection boxes:
    # cascade = cv2.CascadeClassifier(
    #     cv2.data.haarcascades
    #     + "haarcascade_frontalface_default.xml"
    # )
    # gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    # for (x, y, w, h) in cascade.detectMultiScale(gray, 1.3, 5):
    #     cv2.rectangle(
    #         frame, (x, y), (x + w, y + h), (0, 255, 0), 2
    #     )
    # return frame
'''

# project_id -> (mtime, process_frame_callable_or_None, error_or_None)
_camera_module_cache: dict = {}


def _load_camera_processor(project_id: str, camera_file: Path):
    """
    (Re)loads a project's camera.py only when its mtime has
    changed since the last frame, so editing it takes effect
    live without paying import cost on every single frame.
    Returns (process_fn, error_message) - exactly one is None.
    """

    if not camera_file.is_file():
        try:
            camera_file.write_text(
                CAMERA_STARTER_CONTENT,
                encoding="utf-8"
            )

        except Exception as error:
            return None, f"Could not create camera.py: {error}"

    try:
        mtime = camera_file.stat().st_mtime

    except OSError as error:
        return None, f"Could not read camera.py: {error}"

    cached = _camera_module_cache.get(project_id)

    if cached and cached[0] == mtime:
        return cached[1], cached[2]

    module_name = f"_camera_module_{project_id.replace('-', '_')}"

    try:
        spec = importlib.util.spec_from_file_location(
            module_name,
            str(camera_file)
        )

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        process_fn = getattr(module, "process_frame", None)

        if not callable(process_fn):
            result = (
                None,
                "camera.py must define a "
                "process_frame(frame) function."
            )

        else:
            result = (process_fn, None)

    except Exception as error:
        result = (None, f"{type(error).__name__}: {error}")

    _camera_module_cache[project_id] = (
        mtime, result[0], result[1]
    )

    return result


def _draw_camera_error(frame, message: str):
    """
    Overlays an error message on the passthrough frame so a
    bug in camera.py shows up right on the live feed itself,
    the same way a traceback shows up in the terminal - instead
    of the stream just silently freezing or dropping.
    """

    if cv2 is None:
        return frame

    banner_height = 60
    overlay = frame.copy()

    cv2.rectangle(
        overlay,
        (0, 0),
        (overlay.shape[1], banner_height),
        (0, 0, 0),
        -1
    )

    frame = cv2.addWeighted(overlay, 0.75, frame, 0.25, 0)

    text = message.strip().replace("\n", " ")

    if len(text) > 70:
        text = text[:67] + "..."

    cv2.putText(
        frame,
        "camera.py error:",
        (10, 20),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.5,
        (0, 0, 255),
        1,
        cv2.LINE_AA
    )

    cv2.putText(
        frame,
        text,
        (10, 44),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.45,
        (0, 0, 255),
        1,
        cv2.LINE_AA
    )

    return frame


def _process_and_encode_camera_frame(
    data: bytes, process_fn, load_error
):
    """
    Decode -> run process_frame -> re-encode, all in a single
    call so it's one dispatch to a worker thread per frame
    instead of three separate hops between the event loop and
    the thread pool. That per-frame overhead was the main
    thing capping throughput well below what the actual
    OpenCV work and network transfer needed.
    """

    array = np.frombuffer(data, dtype=np.uint8)
    frame = cv2.imdecode(array, cv2.IMREAD_COLOR)

    if frame is None:
        return None

    if load_error:
        output = _draw_camera_error(frame, load_error)

    else:

        try:
            result = process_fn(frame)

        except Exception as error:
            output = _draw_camera_error(
                frame, f"{type(error).__name__}: {error}"
            )

        else:

            if result is None or not hasattr(
                result, "shape"
            ):
                output = _draw_camera_error(
                    frame,
                    "process_frame must return a NumPy "
                    "image (it returned "
                    f"{type(result).__name__})"
                )

            else:
                output = result

                if (
                    hasattr(output, "ndim")
                    and output.ndim == 2
                ):
                    output = cv2.cvtColor(
                        output, cv2.COLOR_GRAY2BGR
                    )

    ok, encoded = cv2.imencode(
        ".jpg",
        output,
        [cv2.IMWRITE_JPEG_QUALITY, 50]
    )

    if not ok:
        return None

    return encoded.tobytes()


@app.websocket(
    "/ws/projects/{project_id}/camera"
)
async def project_camera(
    websocket: WebSocket,
    project_id: str
):

    await websocket.accept()

    try:
        project = project_path(project_id)

    except ValueError:
        await websocket.close(code=4000)
        return

    if not project.is_dir():
        await websocket.close(code=4004)
        return

    if cv2 is None or np is None:

        await websocket.send_text(
            json.dumps({
                "type": "error",
                "message":
                    "opencv-python-headless is not "
                    "installed on this server."
            })
        )

        await websocket.close(code=1011)
        return

    camera_file = project / "camera.py"
    loop = asyncio.get_event_loop()

    try:
        while True:

            try:
                data = await websocket.receive_bytes()

            except Exception:
                break

            if not data:
                continue

            process_fn, load_error = _load_camera_processor(
                project_id, camera_file
            )

            try:
                encoded_bytes = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        _process_and_encode_camera_frame,
                        data, process_fn, load_error
                    ),
                    timeout=2.0
                )

            except asyncio.TimeoutError:
                # A single pathologically slow frame just
                # gets skipped (the feed briefly holds on the
                # last good frame) rather than blocking the
                # connection - persistent timeouts mean
                # process_frame itself is too slow for video.
                continue

            if not encoded_bytes:
                continue

            try:
                await websocket.send_bytes(encoded_bytes)

            except Exception:
                break

    finally:

        try:
            await websocket.close()

        except Exception:
            pass
