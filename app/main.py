from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from pathlib import Path
import os
import subprocess
import tempfile
import uuid


app = FastAPI(title="Python IDE")

app.mount("/static", StaticFiles(directory="app/static"), name="static")


# IMPORTANT:
# This is only prototype storage.
# Render's normal filesystem is ephemeral.
PROJECTS_DIR = Path(
    os.getenv("PROJECTS_DIR", "/tmp/python-ide-projects")
)

PROJECTS_DIR.mkdir(parents=True, exist_ok=True)


def project_path(project_id: str) -> Path:
    if not project_id:
        raise ValueError("Invalid project ID")

    allowed = (
        "abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "0123456789-_"
    )

    if any(char not in allowed for char in project_id):
        raise ValueError("Invalid project ID")

    return PROJECTS_DIR / project_id


def safe_relative_path(path: str) -> Path:
    path = Path(path)

    if path.is_absolute():
        raise ValueError("Absolute paths are not allowed")

    if ".." in path.parts:
        raise ValueError("Parent paths are not allowed")

    if not path.parts:
        raise ValueError("Invalid path")

    return path


def list_files(folder: Path):
    files = []

    for path in sorted(folder.rglob("*")):
        if path.is_file():
            files.append(
                path.relative_to(folder).as_posix()
            )

    return files


@app.get("/")
async def index():
    return FileResponse("app/static/index.html")


# -------------------------
# PROJECTS
# -------------------------

@app.get("/api/projects")
async def get_projects():

    projects = []

    for folder in sorted(PROJECTS_DIR.iterdir()):

        if folder.is_dir():

            projects.append({
                "id": folder.name,
                "name": folder.name
            })

    return {
        "projects": projects
    }


@app.post("/api/projects")
async def create_project(request: Request):

    data = await request.json()

    name = str(
        data.get("name", "project")
    ).strip()

    if not name:
        name = "project"

    # Keep project name filesystem-safe.
    name = "".join(
        char if char.isalnum() or char in "-_"
        else "-"
        for char in name
    )

    project_id = (
        f"{name}-{uuid.uuid4().hex[:8]}"
    )

    folder = project_path(project_id)

    folder.mkdir(
        parents=True,
        exist_ok=False
    )

    main_file = folder / "main.py"

    main_file.write_text(
        'print("Hello from your Python project!")\n',
        encoding="utf-8"
    )

    return {
        "id": project_id,
        "name": project_id
    }


# -------------------------
# FILES
# -------------------------

@app.get("/api/projects/{project_id}/files")
async def get_files(project_id: str):

    try:
        folder = project_path(project_id)

    except ValueError:

        return JSONResponse(
            {
                "error": "Invalid project ID"
            },
            status_code=400
        )

    if not folder.is_dir():

        return JSONResponse(
            {
                "error": "Project not found"
            },
            status_code=404
        )

    return {
        "files": list_files(folder)
    }


@app.get("/api/projects/{project_id}/file")
async def read_file(
    project_id: str,
    path: str
):

    try:

        folder = project_path(project_id)
        relative = safe_relative_path(path)

        target = folder / relative

    except ValueError:

        return JSONResponse(
            {
                "error": "Invalid path"
            },
            status_code=400
        )

    if not target.is_file():

        return JSONResponse(
            {
                "error": "File not found"
            },
            status_code=404
        )

    if target.stat().st_size > 1_000_000:

        return JSONResponse(
            {
                "error": "File is too large"
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


@app.post("/api/projects/{project_id}/file")
async def write_file(
    project_id: str,
    request: Request
):

    data = await request.json()

    path = str(
        data.get("path", "")
    ).strip()

    content = data.get(
        "content",
        ""
    )

    if not path:

        return JSONResponse(
            {
                "error": "Path is required"
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

    if len(content) > 1_000_000:

        return JSONResponse(
            {
                "error":
                "File is too large"
            },
            status_code=413
        )

    try:

        folder = project_path(project_id)

        relative = safe_relative_path(path)

        target = folder / relative

    except ValueError:

        return JSONResponse(
            {
                "error": "Invalid path"
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


@app.delete("/api/projects/{project_id}/file")
async def delete_file(
    project_id: str,
    path: str
):

    try:

        folder = project_path(project_id)

        relative = safe_relative_path(path)

        target = folder / relative

    except ValueError:

        return JSONResponse(
            {
                "error": "Invalid path"
            },
            status_code=400
        )

    if not target.is_file():

        return JSONResponse(
            {
                "error": "File not found"
            },
            status_code=404
        )

    target.unlink()

    return {
        "ok": True
    }


# -------------------------
# PYTHON EXECUTION
# -------------------------

@app.post("/run")
async def run_code(request: Request):

    data = await request.json()

    code = data.get(
        "code",
        ""
    )

    if not isinstance(code, str):

        return JSONResponse(
            {
                "error":
                "Code must be a string"
            },
            status_code=400
        )

    if not code.strip():

        return JSONResponse(
            {
                "error":
                "No code provided"
            },
            status_code=400
        )

    if len(code) > 20_000:

        return JSONResponse(
            {
                "error":
                "Code is too large"
            },
            status_code=413
        )

    with tempfile.TemporaryDirectory() as temp_dir:

        script = os.path.join(
            temp_dir,
            "main.py"
        )

        with open(
            script,
            "w",
            encoding="utf-8"
        ) as file:

            file.write(code)

        try:

            process = subprocess.run(
                [
                    "python",
                    script
                ],
                cwd=temp_dir,
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
