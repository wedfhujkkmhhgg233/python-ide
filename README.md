# Personal Cloud Python IDE — Phase 1

Phase 1 of the plan in `ARCHITECTURE.md`: authentication, a secured
filesystem layer, project management, and a mobile-first file explorer +
plain-text editor with debounced autosave. **No code execution exists yet**
— that's Phase 3, and nothing in this codebase runs user-submitted code.

## What actually works right now

- Login/logout with bcrypt-hashed password, signed session cookie,
  rate-limited login attempts.
- Create/list/delete projects.
- Create/list/rename/delete files and folders, one directory level at a
  time (no full-tree download).
- Open a file, edit it in a plain `<textarea>`, debounced autosave (1.2s
  after you stop typing), manual save happens automatically, offline
  detection shows "Offline / Connection lost" instead of silently losing
  changes.
- All path input is funneled through `backend/security/path_security.py` —
  traversal, absolute paths, and symlink escapes are rejected before any
  filesystem call.

## What's intentionally NOT here yet

Per the phased plan — these are not stubbed with fake buttons, they simply
don't have UI/routes yet:

- Real code editor (Monaco/CodeMirror), syntax highlighting, tabs — Phase 2.
- Running Python, process manager, terminal, REPL, WebSockets — Phase 3.
- Virtual environments, pip install/uninstall — Phase 4.
- Resource limits / sandboxing (not needed yet since nothing executes) —
  Phase 5, required before Phase 3 ships.
- Upload/download, search/replace, low-bandwidth polish beyond the basics
  already applied — Phase 6.
- Automated test suite — Phase 7.

## Setup

```bash
cd pyide
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Generate a password hash and put it in .env as ADMIN_PASSWORD_HASH:
python -m backend.auth.security hash "choose-a-strong-password"
# Generate a session secret and put it in .env as SESSION_SECRET:
python -c "import secrets; print(secrets.token_hex(32))"

uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` (or your phone's browser pointed at your
machine's LAN IP) and log in with the admin username/password you set.

## Deployment notes (Render or similar)

- Set `ENV=production`, `ADMIN_PASSWORD_HASH`, and `SESSION_SECRET` as
  environment variables in your host's dashboard — never commit them.
- **Important**: `WORKSPACE_ROOT` (your project files) and `DB_PATH`
  (SQLite metadata) currently default to local disk. Most PaaS free/basic
  tiers use ephemeral filesystems — a redeploy or restart can wipe
  everything. Attach a persistent disk/volume and point `WORKSPACE_ROOT`
  and `DB_PATH` at it, or don't rely on this for anything you can't afford
  to lose until Phase 5/6 storage-backend work lands (see
  `StorageBackend` abstraction in `ARCHITECTURE.md` section B).
- Run behind HTTPS so the `Secure` session cookie flag (enabled
  automatically when `ENV=production`) actually protects the cookie in
  transit.

## Security notes for this phase

- No code from the browser is ever executed, evaluated, or passed to a
  shell. The only backend operations are file CRUD, mediated entirely
  through `path_security.safe_join`.
- Passwords are never stored in plaintext; only a bcrypt hash lives in
  configuration, and only a SHA-256 hash of the session token lives in the
  database (not the raw token).
- Unhandled exceptions never reach the browser with a stack trace or
  internal path — see `unhandled_exception_handler` in `backend/main.py`.
