from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from pathlib import Path
import os
import subprocess
import sys
import tempfile
import uuid
import shutil


app = FastAPI(title="Python IDE")

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
        str(STATIC_DIR / "index.html")
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


    (
        folder / "main.py"
    ).write_text(
        'print("Hello from your Python project!")\n',
        encoding="utf-8"
    )


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
        list_files(folder)
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


    return {
        "ok": True,
        "path": path
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


    return {
        "ok": True
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
