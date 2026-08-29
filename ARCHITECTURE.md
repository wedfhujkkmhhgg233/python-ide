# Personal Cloud Python IDE — Architecture & Design

This document covers items A–H requested before implementation. Phase 1 (auth +
secured filesystem + basic explorer) is implemented in this delivery. Later
phases (editor, execution, packages, hardening, mobile/bandwidth polish,
tests) are scaffolded conceptually below and will be built incrementally on
top of this foundation — they are not faked in Phase 1 code; they simply
don't exist yet.

## A. Architecture Diagram

```
                Web Browser (phone or desktop)
                        │
                        ▼
          Static HTML/CSS/JS (no framework, no build step)
                        │  fetch() + (later) WebSocket
                        ▼
               FastAPI Server (control plane)
   ┌─────────────────────────────────────────────────┐
   │ auth/        session cookies, password hashing   │
   │ api/         HTTP route handlers (thin)          │
   │ filesystem/  workspace abstraction                │
   │ security/    path + input validation (central)    │
   │ storage/     SQLite metadata                       │
   │ execution/   (Phase 3) process manager             │
   │ packages/    (Phase 4) venv/pip manager             │
   └─────────────────────────────────────────────────┘
                        │  controlled API only
                        ▼
        Isolated Executor (Phase 3+, NOT built yet)
        subprocess per project venv, resource-limited,
        non-root, network-off by default
```

Control plane (FastAPI) and execution plane (isolated Python subprocess) are
kept separate per item 38 — Phase 1 has no execution yet, so this boundary is
currently just the empty `execution/` slot for Phase 3.

## B. Project Directory Structure

```
pyide/
├── backend/
│   ├── main.py            FastAPI app + route registration
│   ├── config.py          env-based settings, no hardcoded secrets
│   ├── db.py               SQLite init + connection helper
│   ├── models.py           Pydantic request/response models
│   ├── auth/
│   │   └── security.py     password hashing, session tokens, login guard
│   ├── api/
│   │   ├── auth_routes.py
│   │   ├── project_routes.py
│   │   └── file_routes.py
│   ├── filesystem/
│   │   └── workspace.py    create/read/write/rename/delete, canonical-path checks
│   └── security/
│       └── path_security.py  the ONE place path traversal is prevented
├── frontend/
│   ├── index.html
│   ├── app.js
│   └── style.css
├── workspace/projects/      actual project files live here on disk
├── requirements.txt
├── .env.example
└── README.md
```

## C. Security Threat Model

| # | Asset | Threat | Attack | Impact | Mitigation (Phase implemented) |
|---|---|---|---|---|---|
|1| Host filesystem | Path traversal | `../../etc/passwd` in file path param | Read/write arbitrary files | `path_security.py` resolves canonical path, rejects anything outside workspace root (**Phase 1**) |
|2| Host filesystem | Symlink escape | Create symlink pointing outside workspace | Escape sandbox | Canonical resolution (`os.path.realpath`) performed *after* join, checked against root (**Phase 1**) |
|3| Server process | Arbitrary code execution | Malicious code in `exec()`-style endpoint | Full host compromise | No `exec`/`eval` anywhere in control plane; execution deferred entirely to isolated executor (**Phase 3**, not built yet — no execution endpoint exists in Phase 1) |
|4| Server process | Command injection | Shell metacharacters in filename/package name | RCE | All future subprocess calls use argument arrays, never `shell=True` (**Phase 3/4 design constraint**, enforced in Phase 1 by having no shell calls at all) |
|5| Host resources | Resource exhaustion / fork bombs | `while True: os.fork()` | DoS | CPU/mem/PID/disk limits on executor (**Phase 3/5**, N/A in Phase 1 — no execution yet) |
|6| Auth | Authentication bypass | Guessing/brute-forcing admin password | Full takeover | bcrypt hashing, rate-limited login attempts, signed session cookie (**Phase 1**) |
|7| Sessions | Session hijacking | Cookie theft via XSS/network sniff | Account takeover | `HttpOnly`, `SameSite=Lax` cookies; `Secure` flag in production; server-side session store with expiry (**Phase 1**) |
|8| Secrets | Secret leakage | Server env vars exposed via API/error | Credential theft | Errors sanitized before returning to browser; server env never serialized into any response (**Phase 1**) |
|9| WebSocket (future) | Cross-process hijacking | Client changes `process_id` in URL | View/control another process | Server-side ownership check against session before attaching (**Phase 3 design constraint**) |
|10| Packages (future) | Malicious dependency | `pip install` of typosquatted package | Supply-chain compromise | Name validation regex, install only inside project venv, non-root (**Phase 4**) |
|11| Uploads (future) | Malicious file upload | Oversized or crafted file | DoS / disk fill | Size caps, extension checks (**Phase 6 polish**) |
|12| SSRF (future) | User code calls internal services | `requests.get("http://169.254.169.254")` | Cloud metadata theft | Network OFF by default for executed code; explicit allow-list if enabled (**Phase 3/5**) |

Phase 1 specifically closes off #1, #2, #6, #7, #8 because those apply to the
filesystem + auth surface that exists today. #3–#5, #9–#12 are architectural
commitments enforced by *not having built the execution/package/upload
endpoints yet* — nothing in Phase 1 lets a browser request cause code to run
on the server.

## D. Data-Flow Design (Phase 1)

```
Browser → POST /api/auth/login {username, password}
        ← Set-Cookie: session=<signed token>, HttpOnly

Browser → GET /api/projects              (Cookie: session=...)
        ← [{id, name, created_at, modified_at}, ...]

Browser → POST /api/projects {name}
        ← {id, name, ...}                (creates workspace/projects/<id>/ dir)

Browser → GET /api/projects/{id}/files?path=src
        ← [{name, type: file|dir, size}, ...]   (one directory level only — lazy)

Browser → GET /api/projects/{id}/file?path=main.py
        ← {path, content}                (only the opened file, not the tree)

Browser → PUT /api/projects/{id}/file {path, content}
        ← {saved: true, modified_at}
```

Every request carrying a `path` is passed through `path_security.safe_join()`
before any filesystem call — this is the only path in the codebase that is
allowed to build a filesystem path from user input.

## E. API Design (Phase 1 subset — full list stands as in item 22)

```
POST   /api/auth/login
POST   /api/auth/logout
GET    /api/auth/me

GET    /api/projects
POST   /api/projects
DELETE /api/projects/{project_id}

GET    /api/projects/{project_id}/files?path=<dir>
POST   /api/projects/{project_id}/files        {path, type: file|dir}
GET    /api/projects/{project_id}/file?path=<file>
PUT    /api/projects/{project_id}/file          {path, content}
DELETE /api/projects/{project_id}/file?path=<file>
POST   /api/projects/{project_id}/file/rename   {old_path, new_path}
```

Execution, packages, and WebSocket terminal routes are designed (item 22) but
intentionally not implemented until Phases 3–4.

## F. Execution Isolation Strategy (design for Phase 3, not yet built)

- Each `run` request spawns `["<project>/.venv/bin/python", entry_file]` as a
  subprocess (argument array, never a shell string).
- Wrapped with OS-level limits: `resource.setrlimit` for CPU/mem/file-size in
  the child's `preexec_fn`, plus a wall-clock timeout that force-kills the
  process group.
- Runs as a dedicated non-root OS user with a restricted `PATH` and no
  access to server secrets (child env is built from an explicit allow-list,
  never inherited wholesale).
- Longer-term: move the child into a container (gVisor/Firecracker or a
  locked-down Docker container with `--pids-limit`, `--memory`, `--cpus`,
  `--network=none` by default) for real kernel-level isolation rather than
  relying on rlimits alone.

## G. Low-Bandwidth Strategy

- No framework bundle: hand-written HTML/CSS/vanilla JS for Phase 1 (a few KB
  total, gzip-friendly). Monaco/CodeMirror is deferred to Phase 2 and will be
  lazy-loaded only when a file is opened.
- Directory listings are one level deep per request, not a full recursive
  tree.
- File content is fetched only when a tab is opened, never all at once.
- Autosave (Phase 2) will debounce keystrokes and diff before sending.
- Terminal output (Phase 3) will be capped client-side and delta-streamed
  over WebSocket rather than polled.

## H. Development Phases

Matches item 35 exactly:
1. **Project structure, auth, filesystem, basic explorer — implemented now.**
2. Editor, tabs, save/autosave.
3. Execution, process manager, terminal, WebSocket.
4. Virtual environments, package manager.
5. Security hardening / resource limits / isolation.
6. Mobile + bandwidth optimization.
7. Testing + deployment.
