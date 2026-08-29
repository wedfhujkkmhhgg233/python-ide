from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from pathlib import Path
import asyncio
import json
import os
import signal
import struct
import subprocess
import sys
import tempfile
import uuid
import shutil

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


    if not source.is_file():

        return JSONResponse(
            {
                "error":
                "File not found"
            },
            status_code=404
        )


    if destination.exists():

        return JSONResponse(
            {
                "error":
                "A file already exists at that path"
            },
            status_code=409
        )


    destination.parent.mkdir(
        parents=True,
        exist_ok=True
    )


    source.rename(
        destination
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
        env["PS1"] = "\\[\\e[36m\\]\\W\\[\\e[0m\\] $ "

        try:
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

    try:
        await asyncio.wait(
            {output_task, input_task},
            return_when=asyncio.FIRST_COMPLETED
        )

    finally:

        for task in (output_task, input_task):
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

        try:
            await websocket.close()

        except Exception:
            pass
