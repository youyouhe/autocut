# AGENTS.md

AutoCut — a Windows (Win10/11, Python 3.10+) workbench that automates 剪映 (JianYing/CapCut) video production and headless rendering. Pipeline: cross-device ingest → VLM/ASR perception → YAML template or draft editing → rendering (ffmpeg direct, or isolated-desktop JianYing via Frida) → QC.

## Topology (two services, not one)

- `render_server.py` — **web backend** (Flask, port **9010**). Serves the SPA from `static/`, does all business logic (perception, drafting, templates, chat/agent, multi-tenant auth), and **fuses VectCutAPI editing routes** at import time via `sys.path.insert(0, 'VectCutAPI')` + `from capcut_server import app`. Editing endpoints (`create_draft`, `add_video`, `add_text`, …) live in `VectCutAPI/capcut_server.py`, NOT here. **Refuses to start if fusion fails** (editing routes are the product core) unless `ALLOW_RENDER_ONLY=1` forces degraded start.
- **Tiered rendering** (`render_by_draft_id` in render_server.py): `RENDER_ENGINE_PREFER` (default `ffmpeg`) → `ffmpeg_renderer.analyze_draft_for_ffmpeg()` decides. Drafts whose elements are all ffmpeg-feasible render **locally in the web backend** (engine `ffmpeg-local` — no JianYing, no render node); transitions/animations may be approximated (hard cuts) and flagged in the task note; effects/stickers/etc. → fallback: zip the draft and POST it to a render node. `RENDER_ENGINE_PREFER=jianying` sends everything to nodes. `FFMPEG_RENDER_BIN` overrides the render binary (default prefers distro `/usr/bin/ffmpeg`; dev builds mis-rotate video).
- `render_service.py` — **render worker** (Flask, port **9020**), normally deployed on the Win10 render node separate from the web backend (target: web on Linux, render on Win10). Receives self-contained draft zips, extracts via `config.safe_zip_extract`, drives JianYing via `render_driver`, returns mp4; runs TTL housekeeping + `upgrade_watchdog` (disable: `UPGRADE_WATCHDOG=0`). **Hard dependency rule: it must only `import config` + `task_store` + `render_driver` (subprocess). Never `import capcut_server` or `perceive`** — it is a fresh Flask app, not the fused one. When `WEB_BASE_URL` is set it pushes heartbeats/task events back to the backend (`/internal/render-heartbeat|render-event`).
- `VectCutAPI/` — vendored CapCutAPI fork (own `pyproject.toml`/`requirements.txt`; the repo's only `.flake8` lives here). `pyJianYingDraft/` is the draft library; its own `mcp_server.py` is distinct from the root one. **Drafts created via the editing API live inside this dir** (`VectCutAPI/<draft_id>/`, tenant-prefixed `u<uid8>_…`; `dfd_*` dirs are gitignored).
- `render_driver.py` — JianYing UI automation (Frida + Win32 `CreateDesktop` `JYRender_0`). Needs a real JianYing install + `calib.json`.
- `mcp_video_server.py` — root MCP server (wired in `.mcp.json`); wraps `cli.client.ApiClient` against `config.API_BASE` (the web backend).
- `autocut_cli.py` + `cli/` — CLI over HTTP. Needs the web backend running.
- `static/` — committed build output of `frontend-react/`. Edit `frontend-react/src`, rebuild (vite `outDir: '../static'`). **`frontend/` is a legacy Vue3 GUI — don't edit it.** Large downloadable files go in `downloads/`, not `static/` (vite build `emptyOutDir` wipes `static/`).
- `config.py` — **single source of truth** for all paths/ports/timeouts, driven by `.env`. Other modules `from config import ...`.
- `portable/build.py` — builds a self-contained Win10 render node (embeddable Python + render_service). Deploy helpers: `serve.bat`/`stop.bat`, `start_render_service.bat`, `start_web.sh`/`restart_web.sh`/`stop_web.sh`, `autocut-web.service`, `deploy-render.ps1`.

## Commands

```bash
pip install -r requirements.txt          # main deps (incl. torch/torchvision for scene detection)
pip install -r requirements-dev.txt      # + pytest, ruff
cp .env.example .env                     # fill QWEN_API_KEY, SECRET_KEY, RENDER_SERVICE_TOKEN etc.
python render_driver.py calibrate        # one-time (Windows node); writes gitignored calib.json

python render_server.py                  # web backend (auto-uses waitress if installed)
python render_service.py                 # render worker (Win10 node)
python gui.py                            # Gradio debug console (:7860)

python -m pytest tests/ -v               # unit tests — run from repo root; NEVER bare `pytest` (it would collect root test_5x.py, which fires 5 real renders at import time)
python -m pytest tests/test_core.py -v   # single file
ruff check .                             # Python lint (no root config; defaults)

cd frontend-react && npm install && npm run build   # outputs to ../static/
cd frontend-react && npm run lint        # = tsc --noEmit (NOT eslint)
```

## Gotchas

- **Ports are defined in `config.py`, not `.env.example`**: web backend defaults to **9010**, render worker to **9020**. `.env.example` still lists `RENDER_SERVER_PORT=9002` (stale) — trust `config.py`.
- **Tests are pure-logic only** (no Windows/JianYing/Frida/network): `tests/test_core.py` imports `config`; `tests/test_cli.py` imports `cli.client`/`autocut_cli` (function-level). No `__init__.py` — run pytest from the repo root. Root `test_5x.py` is a manual render-stability harness, not a unit test.
- **`pyproject.toml` packages only the CLI** (`autocut_cli`, `config`, `cli.client`, `cli.output`). Render/driver/Windows-side code is intentionally excluded from `pip install -e .`.
- **Multi-tenant auth**: Flask session + `SECRET_KEY` (set it in `.env` or every restart invalidates sessions). Internal agent tools self-call via `config.API_BASE` with an `X-Internal-Token` header matched against `config.INTERNAL_TOKEN` (random per-process unless set).
- **Draft tenant isolation**: draft_ids/folders are namespaced with a `u<uid[:8]>_` prefix (`VectCutAPI/create_draft.py` `_user_prefix`; `render_server.py` `_draft_tenant_prefix`). List/delete/cover/render/timeline/add-asset and all fused editing routes (via `_draft_ownership_gate`) are gated by `_draft_owned` — admin sees/edits all; unprefixed legacy/JianYing-native drafts are admin-only. New draft routes must keep this prefix discipline.
- **Per-user render nodes**: each user can configure their own render_service URL + `X-Render-Token`; render falls back to the public `RENDER_SERVICE_URL`/`RENDER_SERVICE_TOKEN` node on failure. The chosen node's URL/token is recorded on the task record. `X-Render-Token` unset = render_service auth disabled (local-dev only).
- **ffmpeg is resolved at startup** by `ffmpeg_util.resolve_ffmpeg()` (settings `FFMPEG_PATH` → candidate dirs `C:\ffmpeg\bin`, `Program Files\ffmpeg\bin`, `%LOCALAPPDATA%\ffmpeg\bin` → PATH) because `pythonw`/service doesn't inherit an interactive PATH. Scene detection, ffmpeg direct render, audio-strip, covers, and perception sampling all depend on it.
- **Scene/shot detection** (`shot_split.py`) uses torch/torchvision (GPU CNN). First run downloads ~45MB weights; CUDA wheels come from a mirror — see `requirements.txt` comments (GTX 1070 Ti = Pascal sm_61 → torch 2.7.x cu126).
- **CLI → server protocol**: `autocut` defaults to `http://127.0.0.1:9010`; resolution order is `--api` flag > `AUTOCUT_API` env > `config.API_BASE`. `--json` keeps stdout pure JSON (progress/logs → stderr); exit code 0/1.
- **Security utilities live in `config.py`** (`is_within`, `safe_folder_name`, `safe_zip_extract`, `is_allowed_path`) — reuse them for any new upload/serve/draft path; the server listens on `0.0.0.0` for LAN LocalSend.
- **ASR backend** is `remote` (third-party endpoint) by default; `ASR_BACKEND=local` uses faster-whisper (optional dep, not in requirements).
- **BrowserSkill publishing**: the agent tool `bsk_run` (`agent_tools.py`) drives a logged-in browser via the `bsk` CLI (`config.BSK_BIN`, default `~/.local/bin/bsk`) to publish to 视频号/抖音/小红书. Not installed by default.
- **Machine-local runtime data**: gitignored — `calib.json`, `*.db` (tasks/chats/assets/users/agent_sessions), `render_uploads/`, `gui_uploads/`, `analysis_cache/`, `downloads/`, media files. NOT gitignored despite being runtime data: `main_video/`, `user_render/` (per-user node configs), `asset_cache/` (content-addressed asset cache). Create `.env`/`calib.json` before running.

For deeper reference: `README.md` (features, CLI examples) and `SYSTEM_MANUAL.md` (846-line system/API manual, incl. known pitfalls).
