# WP Maintenance WebUI — FastAPI Migration Plan

**Note: This is for WebUI refactoring only. Other refactoring plans in other REFACTOR files.**

## Overview

- Current state — file inventory and pain points
- Target architecture — full directory structure with annotations
- 6 migration phases — sequenced by risk, each designed to ship as its own PR
- Key mapping tables — webui.py patterns → FastAPI equivalents
- SSE bridging pattern — the actual code for the highest-risk phase
- Risk register — 5 specific risks with mitigations
- Validation checklist — how to confirm each phase before cutover


The `webui-maintenance-console` branch contains a complete, working web operator console for `wp_update.py` built on Python's raw `http.server.BaseHTTPRequestHandler`. The goal of this migration is to replace that hand-rolled HTTP layer with **FastAPI** while leaving all business logic — especially `wp_update.py` and `db.py` — untouched.

The architecture target is **Layered (N-Tier) Architecture** with a **Worker pattern** for subprocess delegation:

- **Presentation Layer** — routers, Jinja2 templates, static assets
- **Business/Service Layer** — auth, session, run manager, settings
- **Data/Persistence Layer** — `db.py` (raw sqlite3, unchanged), `clients/*.json` files
- **External Worker** — `wp_update.py` called as a subprocess (never imported)

---

## Current State Assessment

### What exists on `webui-maintenance-console`

| File | Size | Role |
|------|------|------|
| `webui.py` | ~90KB | Entire web layer — routing, HTML, auth, SSE, process management |
| `db.py` | ~29KB | Raw sqlite3 persistence layer |
| `wp_update.py` | ~101KB | SSH/WP-CLI automation, called as subprocess |

### Core pain points in `webui.py`

- Manual path-matching router (`if path == "/api/clients":`) replacing a real framework
- 90KB Python file with HTML, CSS, and JavaScript baked into f-string constants (`LOGIN_HTML`, `app_html()`)
- CSRF and session validation hand-written and re-checked per route handler
- `threading.Thread` + `queue.Queue` SSE streaming without async support
- No Pydantic validation — all payload parsing done manually with `str(payload.get(...) or "")` guards

### What does NOT need to change

- `db.py` — well-structured, no ORM migration needed
- `wp_update.py` — untouched; it remains an external subprocess worker
- `clients/*.json` — file layout unchanged
- All business logic inside `webui.py` functions like `start_run`, `cancel_run`, `list_client_files`, `build_wp_args`, `validate_client_doc` — these port to FastAPI verbatim

---

## Target Architecture

### Directory structure

```
automated-wordpress-maintenance/
├── app/
│   ├── main.py                  # FastAPI app init, lifespan (init_db), mounts routers
│   ├── routers/
│   │   ├── auth.py              # POST /api/login, GET /logout
│   │   ├── clients.py           # GET /api/clients, GET /api/clients/{name}/history, POST /api/clients/import, POST /api/clients/manual
│   │   ├── runs.py              # POST /api/runs, GET /api/runs/{id}/stream (SSE), POST /api/runs/{id}/cancel, GET /api/runs/{id}/summary
│   │   ├── ssh_keys.py          # GET /api/ssh-keys, POST /api/ssh-keys
│   │   ├── stats.py             # GET /api/stats/plugins
│   │   └── logs.py              # GET /api/logs, GET /api/logs/{filename}
│   ├── templates/
│   │   ├── index.html.j2        # extracted from app_html() f-string
│   │   └── login.html.j2        # extracted from LOGIN_HTML constant
│   ├── static/
│   │   └── app.js               # extracted inline JS (~500 lines from app_html)
│   ├── core/
│   │   ├── auth.py              # sign_session, verify_session, session_csrf → FastAPI Depends()
│   │   ├── run_manager.py       # RunRecord, RUNS dict, start_run, cancel_run (logic unchanged)
│   │   └── settings.py          # Settings dataclass → Pydantic BaseSettings
│   └── db.py                    # symlink or copy of existing db.py (zero changes)
├── wp_update.py                 # untouched — called as subprocess only
├── clients/
├── logs/
└── db/
```

### Dependency additions (minimal)

```
fastapi
uvicorn[standard]
jinja2
python-multipart      # for form uploads (SSH key upload)
pydantic-settings     # for Settings.from_env()
```

No new database dependencies. No ORM. No task queue.

---

## Migration Phases

### Phase 1 — Framework scaffold (Low risk)

Install FastAPI and create `app/main.py` with a `lifespan` context manager that calls `init_db()` and runs `db.sweep_orphan_running` / `db.reconcile_pending_ingests` on startup — exactly what `webui.py` does today.

Port each `do_GET` / `do_POST` branch in `WebUIHandler` to a corresponding FastAPI router file. At this stage, the logic inside each route function is copy-pasted from `webui.py` verbatim. The goal is structural: routes resolve, responses return the same JSON shape, the app starts.

Run both `webui.py` and the new FastAPI app in parallel to diff responses during validation.

### Phase 2 — Template extraction (Low risk)

Extract the `LOGIN_HTML` constant and the `app_html()` f-string into `app/templates/login.html.j2` and `app/templates/index.html.j2`. Replace the f-string interpolations (`{SOME_VAR}`) with Jinja2 template variables (`{{ some_var }}`).

Extract the inline JavaScript from `app_html()` into `app/static/app.js`. Mount the static directory in `main.py`:

```python
app.mount("/static", StaticFiles(directory="app/static"), name="static")
```

After this phase, `app_html()` disappears entirely and `webui.py`'s largest pain point is resolved.

### Phase 3 — Auth as dependency (Medium risk)

Move `sign_session`, `verify_session`, `session_csrf`, and the login rate-limit logic from procedural functions into FastAPI `Depends()` callables.

```python
async def require_auth(request: Request) -> str:
    token = request.cookies.get(SESSION_COOKIE)
    username = verify_session(token or "", settings.secret)
    if not username:
        raise HTTPException(status_code=401)
    return username

async def require_csrf(request: Request, username: str = Depends(require_auth)) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    csrf_from_session = session_csrf(token or "", settings.secret)
    csrf_from_header = request.headers.get("X-CSRF-Token")
    if not csrf_from_session or not hmac.compare_digest(csrf_from_session, csrf_from_header or ""):
        raise HTTPException(status_code=403)
```

All POST routes declare `_: None = Depends(require_csrf)`. Auth and CSRF stop being per-route string-checking and become composable dependencies.

### Phase 4 — SSE migration (Medium risk)

This is the most technically sensitive phase. The current `_stream_run` method in `webui.py` uses a `threading.Lock` and `queue.Queue` to feed output lines to HTTP clients. This threading model must be handled carefully in FastAPI's async context.

**Recommended approach — bridge threads to async:**

```python
@router.get("/api/runs/{run_id}/stream")
async def stream_run(run_id: str, username: str = Depends(require_auth)):
    record = get_run(run_id)
    if record is None:
        raise HTTPException(status_code=404)

    async def event_generator():
        q: asyncio.Queue[str | None] = asyncio.Queue()
        loop = asyncio.get_event_loop()

        def on_line(line: str | None) -> None:
            loop.call_soon_threadsafe(q.put_nowait, line)

        # Register async-safe listener on the RunRecord
        record.add_async_listener(on_line)
        try:
            # Replay buffered lines first
            for line in record.get_buffered_lines():
                yield f"data: {line}\n\n"
            # Then stream live
            while True:
                line = await q.get()
                if line is None:
                    yield "event: done\ndata: {}\n\n"
                    break
                yield f"data: {line}\n\n"
        finally:
            record.remove_async_listener(on_line)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
```

The key: `loop.call_soon_threadsafe` bridges the worker thread's output to the async event loop without blocking it. The `RunRecord` needs a small addition — an `add_async_listener` / `remove_async_listener` pair alongside the existing `queue.Queue` listener mechanism.

**Do not** replace `subprocess.Popen` + `threading.Thread` with `asyncio.create_subprocess_exec` in this phase. That's a separate, optional optimization. The threading model works; only the SSE delivery path needs to be async-safe.

### Phase 5 — Pydantic models (Low risk, optional)

Replace the manual `str(payload.get(...) or "")` payload extraction in `build_wp_args`, `validate_client_doc`, and `start_run` with Pydantic request models:

```python
class RunPayload(BaseModel):
    target: Literal["local", "remote"] = "local"
    execute: bool = False
    includeWooCommerce: bool = False
    clientFile: str = ""
    clientFiles: list[str] = []
    sshKey: str = ""
    remoteHost: str = ""
    remoteUser: str = ""
    remotePort: int = 22
    remoteRepoPath: str = ""
    remoteIdentityFile: str = ""
    remotePython: str = "python3"
    provider: str = "Cloudways"
```

FastAPI validates the request body automatically and returns a `422 Unprocessable Entity` with field-level detail on bad input — replacing all the `raise ValueError(...)` guards scattered through the route handlers.

### Phase 6 — Deferred (separate PRs)

These should not be done during the framework migration:

- **`wp_update.py` extraction into `app/services/`** — it works as a subprocess; extract only if the architecture needs to call its logic programmatically
- **`db.py` → SQLModel migration** — the raw sqlite3 layer is tested and functional; an ORM rewrite is optional and belongs in a dedicated PR after the web layer is proven

---

## Key Migration Mappings

### Route translation

| `webui.py` pattern | FastAPI equivalent |
|---|---|
| `if path == "/api/clients":` | `@router.get("/api/clients")` |
| `if path.startswith("/api/runs/") and path.endswith("/stream"):` | `@router.get("/api/runs/{run_id}/stream")` |
| `if not self._authenticated(): return 401` | `Depends(require_auth)` on router |
| `if not self._csrf_ok(): return 403` | `Depends(require_csrf)` on POST routes |
| `payload = self._read_json()` | `payload: RunPayload = Body(...)` |
| `json_response(self, 200, {...})` | `return JSONResponse({...})` or just `return {...}` |
| `self.send_error(404)` | `raise HTTPException(status_code=404)` |

### Settings migration

`Settings.from_env()` becomes a Pydantic `BaseSettings` class — same env var names, same defaults, but with automatic env parsing and type coercion:

```python
class Settings(BaseSettings):
    host: str = "127.0.0.1"
    port: int = 8787
    username: str = "admin"
    password: str = ""
    secret: str = Field(default_factory=lambda: secrets.token_urlsafe(32))
    # ... etc

    model_config = SettingsConfigDict(env_prefix="WEBUI_")
```

### Server startup

Replace:
```python
server = ThreadingHTTPServer((host, port), handler_class)
server.serve_forever()
```

With:
```python
uvicorn.run("app.main:app", host=host, port=port, workers=1)
```

---

## Risk Register

| Risk | Likelihood | Mitigation |
|---|---|---|
| SSE blocking the event loop | High if done naively | Use `loop.call_soon_threadsafe` bridge pattern (Phase 4) |
| Session cookie behavior changes | Medium | Keep exact same cookie name, path, max-age, SameSite values |
| CSRF token mismatch after migration | Medium | Validate the `X-CSRF-Token` header flow end-to-end before deploying |
| Template variable escaping breaks JS | Low | Jinja2 auto-escapes HTML — use `{% raw %}` blocks around JS literals or `{{ var | tojson }}` |
| `db.py` import path changes | Low | Adjust relative imports; all function signatures stay identical |

---

## Validation Approach

After each phase, verify:

1. All existing API endpoints return the same response shape as `webui.py`
2. Login flow sets the session cookie correctly
3. A dry-run triggers, streams live output over SSE, and terminates cleanly
4. An execute-run writes the DB row, ingests the summary JSON, and appears in `/api/runs`
5. Cancel sends SIGTERM and the run transitions to `cancelled` status
6. The UI correctly displays the run history and client list

Run `webui.py` on port 8787 and the FastAPI app on port 8788 in parallel during phases 1–3 to diff JSON responses with `curl` for confidence before cutover.

---

## Summary

The migration is a **web layer port only**. The subprocess worker, persistence layer, client file format, auth token scheme, and all business logic remain identical. FastAPI eliminates ~400 lines of boilerplate (manual routing, cookie parsing, JSON encoding, CSRF checks) and converts the remaining logic into idiomatic, maintainable Python. The six phases are sequenced so each can be reviewed independently as a pull request, with the highest-risk change (SSE async bridging) isolated in Phase 4.
