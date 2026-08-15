# AGENTS.md

AutoCut — a Windows-only (Win10/11, Python 3.10+) workbench that automates 剪映 (JianYing/CapCut) video production and headless rendering. Core pipeline: cross-device ingest → VLM/ASR perception → YAML template or draft editing → isolated-desktop render via Frida → QC.

## Layout & ownership

- `render_server.py` — main service (Flask, port 9002). Serves the SPA from `static/`, manages the render task pool, and **fuses VectCutAPI editing routes** at import time via `sys.path.insert(0, 'VectCutAPI')` + `from capcut_server import app`. Editing endpoints (`create_draft`, `add_video`, `add_text`, …) live in `VectCutAPI/capcut_server.py`, NOT here.
- `VectCutAPI/` — a vendored fork of CapCutAPI (`capcut-api`, own `pyproject.toml`/`requirements.txt`). `pyJianYingDraft/` is its draft-manipulation library. Treat as a separate package; its `.flake8` config is scoped to it only.
- `render_driver.py` — JianYing UI automation (Frida injection + Win32 `CreateDesktop` isolated desktops). Requires a real Windows JianYing install + calibration.
- `autocut_cli.py` + `cli/` — CLI that talks to `render_server` over HTTP. Needs the server running.
- `static/` — committed build output of `frontend-react/`. Do not edit directly; edit `frontend-react/src` and rebuild.
- `config.py` — **single source of truth** for all paths/ports/timeouts, driven by `.env`. Other modules must `from config import ...`.

## Commands

```bash
pip install -r requirements.txt          # main deps
pip install -r requirements-dev.txt      # + pytest, ruff
cp .env.example .env                     # then fill QWEN_API_KEY etc.
python render_driver.py calibrate        # one-time; writes gitignored calib.json
python render_server.py                  # main server (auto-uses waitress if installed)
python gui.py                            # Gradio debug console (:7860)

python -m pytest tests/ -v               # unit tests — run from repo root
ruff check .                             # Python lint (no config file; defaults)

cd frontend-react && npm install && npm run build   # outputs to ../static/
cd frontend-react && npm run lint        # = tsc --noEmit (NOT eslint)
```

## Gotchas

- **Tests import root modules top-level** (`config`, `template_engine`, `perceive`, `memory_store`, `task_store`, `localsend_recv`, `render_server`, `cli.client`). Run pytest from the repo root; there is no package `__init__.py` making it a package.
- **Tests are pure-logic only** — no Windows/JianYing/Frida/network needed. Rendering/driver paths are not covered by pytest.
- **`pyproject.toml` (root) packages only the CLI** (`autocut_cli`, `config`, `cli.*`). Render/driver/Windows-side code is intentionally excluded from `pip install -e .`.
- **CLI → server protocol**: `autocut` defaults to `http://127.0.0.1:9002`; override with `--api` or `AUTOCUT_API`. `--json` keeps stdout pure JSON (progress/logs go to stderr); exit code 0/1.
- **Machine-local runtime files are gitignored**: `calib.json`, `tasks.db`, `render_uploads/`, `gui_uploads/`, `analysis_cache/`. Don't expect them present; create `.env`/`calib.json` before running.
- **Security utilities live in `config.py`** (`is_within`, `safe_folder_name`, `safe_zip_extract`, `is_allowed_path`) — reuse them for any new file-upload/serve/draft path; server listens on `0.0.0.0` for LAN LocalSend.
- **ASR backend** is `remote` (third-party endpoint) by default; `ASR_BACKEND=local` uses faster-whisper (optional dep, not in requirements).
- Everything is Windows PowerShell; `serve.bat`/`stop.bat` are silent start/stop helpers.
