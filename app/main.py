from fastapi import (
    FastAPI, Request, WebSocket,
    UploadFile, File, Form
)
from fastapi.responses import FileResponse, JSONResponse
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
import sys
import tempfile
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
            "SELECT path FROM project_files WHERE project_id = $1",
            project_id
        )
        db_paths = {row["path"] for row in db_rows}

        db_folder_rows = await conn.fetch(
            "SELECT path FROM project_folders WHERE project_id = $1",
            project_id
        )
        db_folder_paths = {row["path"] for row in db_folder_rows}

        files_to_delete = db_paths - set(disk_files.keys())
        folders_to_delete = db_folder_paths - empty_disk_dirs

        async with conn.transaction():

            for path in files_to_delete:

                await conn.execute(
                    "DELETE FROM project_files "
                    "WHERE project_id = $1 AND path = $2",
                    project_id, path
                )

            for path, (content, is_binary) in disk_files.items():

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


@asynccontextmanager
async def lifespan(app: FastAPI):

    await db_init_pool()
    await db_restore_projects_to_disk()

    yield

    if db_pool is not None:
        await db_pool.close()


app = FastAPI(title="Python IDE", lifespan=lifespan)

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
        script = jedi.Script(code=content, path=filename)
        completions = script.complete(line=line, column=column)

    except Exception:
        # Jedi is generally tolerant of broken/incomplete code
        # (that's the whole point, mid-typing), but it's still
        # third-party static analysis running on arbitrary
        # user text - never let it 500 the request.
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
        # large/unusual files - run it off the event loop and
        # give it a hard ceiling so one slow completion request
        # can't stall the terminal/other requests behind it.
        results = await asyncio.wait_for(
            asyncio.to_thread(
                _jedi_completions,
                content,
                filename,
                line + 1,
                col
            ),
            timeout=3.0
        )

    except asyncio.TimeoutError:
        results = []

    return {
        "completions": results,
        "available": True
    }


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
