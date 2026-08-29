from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import os
import subprocess
import tempfile


app = FastAPI(title="Python IDE")

app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/")
async def index():
    return FileResponse("app/static/index.html")


@app.post("/run")
async def run_code(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(
            {"error": "Invalid JSON"},
            status_code=400
        )

    code = data.get("code", "")

    if not isinstance(code, str):
        return JSONResponse(
            {"error": "Code must be a string"},
            status_code=400
        )

    if not code.strip():
        return JSONResponse(
            {"error": "No code provided"},
            status_code=400
        )

    # Keep the prototype small.
    if len(code) > 20_000:
        return JSONResponse(
            {"error": "Code is too large"},
            status_code=413
        )

    with tempfile.TemporaryDirectory() as temp_dir:
        script = os.path.join(temp_dir, "main.py")

        with open(script, "w", encoding="utf-8") as f:
            f.write(code)

        try:
            process = subprocess.run(
                ["python", script],
                cwd=temp_dir,
                capture_output=True,
                text=True,
                timeout=5
            )

            return {
                "stdout": process.stdout,
                "stderr": process.stderr,
                "returncode": process.returncode,
            }

        except subprocess.TimeoutExpired:
            return {
                "stdout": "",
                "stderr": "Execution timed out.",
                "returncode": -1,
            }

        except Exception as e:
            return {
                "stdout": "",
                "stderr": str(e),
                "returncode": -1,
            }
