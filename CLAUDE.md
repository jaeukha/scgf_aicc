# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A demo AICC (AI contact-center) assistant for 서울신용보증재단 guarantee consultations. Today the whole app is a single Gradio file, `app.py` (~3,250 lines). `PRD.md` describes the planned target: a FastAPI backend, a React UI, PostgreSQL storage, API trace monitoring and a CRM. **Read PRD §1.4 and §12 before building toward it.** The PRD asks for migration, not a rewrite: pull the pure logic (model loading, inference, routing, segmentation, output validation) into `providers/`/`services/` modules, do not port the Gradio callbacks or `gr.State`, and build the Mock end-to-end flow first. There is no git repo, no test suite and no requirements file. Dependencies are pinned in `app.py` (`MODEL_PACKAGES`, `UI_PACKAGES`, `TORCH_PACKAGES`, `COACH_PACKAGES`) and installed with `--setup`.

## New AICC backend (`aicc/`, PRD M1–M2, mock providers)

```powershell
python -m alembic upgrade head                 # schema; app never auto-creates tables
python -m uvicorn aicc.main:app --port 8000    # UI + API at http://127.0.0.1:8000
python -m pytest -q                            # tests/test_api.py, runs migrations on a temp SQLite
python -m pytest -q -k stale                   # single test
docker compose up --build                      # Postgres profile
```

- `aicc/providers.py` imports `app.py` as `legacy` to reuse `run_routing`, `validate_routing_config`, `parse_guidance` and `COACH_SYSTEM`. Keep `app.py` free of top-level heavy imports, or this backend breaks.
- `MockDecider.decide` matches `app.Runtime.decide`'s signature, so a real Decider adapter can drop in.
- Wrap every provider or DB step in `telemetry.span()`; that is what feeds monitoring.
- Schema changes need a new Alembic revision. Generate it with `DATABASE_URL=sqlite:///data/gen.db alembic revision --autogenerate`, then fix any bare `Text()` to `sa.Text()`.
- `PROVIDER_MODE=local` switches ASR and Decider to HTTP calls against `model_server.py`. That server runs in the GPU `.venv` (`app.py --setup`) and wraps app.py's `Runtime`/`ASRRuntime`/`GuidanceRuntime` behind `/internal/*`. `COACH_MODE=internal` uses its Coach.
- On Windows, always use `127.0.0.1` in service URLs. `localhost` adds about 2 s per request because it tries IPv6 first.
- Twenty sync (`aicc/twenty_sync.py`) pushes one way, AICC → Twenty, through the public REST API, with idempotent upserts via `crm_sync_links`.
- Twenty runs from `twenty/` as compose project `aicc-twenty`. Its key is in `twenty/.env`, which is gitignored.
- An unrelated pre-existing `twenty_db-data` volume belongs to the user. Never touch it.
- The backend runs in system Python with `requirements.txt`. The model server runs in `.venv`. Keep them separate, since gradio pins different fastapi/starlette versions.
- The "지금 할 일" panel uses only curated text from `config/routing.json` (`ask` / `action`). Coach (Qwen3.5-2B) output is shown only as a collapsed AI memo, because QA showed it mixes up voices and invents promises. Don't promote it back without an evaluation.
- Routing thresholds: topics use `topic_min_probability` (0.80) and nodes use `min_probability` (0.70). This was tuned against a 42-conversation real-model QA set; re-run that set after config changes.
- RAG: `aicc/knowledge.py` handles parsing, chunking, the embedder HTTP service (`embedder/`, EmbeddingGemma 2 on CPU in Docker) and Qdrant (Docker, `qdrant/qdrant:v1.19.2`). Selected by `KNOWLEDGE_MODE=qdrant`. Search results are stored as `analysis.knowledge` events regardless of Coach. Tests use `EMBED_BASE_URL=mock` with `QDRANT_URL=:memory:`.
- Backend Python env is `.venv-app` (requirements.txt, incl. qdrant-client 1.19.1). Run tests with `.venv-app\Scripts\python.exe -m pytest -q`.
- README.md has the RTX 5080 measurements and the list of what's left. `docs/` has the Twenty PoC results and the DGX Spark checklist.

## Legacy Gradio app commands (Windows / PowerShell, Python 3.12 x64, CUDA GPU with BF16 required)

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe app.py --setup            # install pinned deps into current env
.\.venv\Scripts\python.exe app.py --download-only    # fetch Decider + ASR into models/
.\.venv\Scripts\python.exe app.py --autoload         # run UI at http://127.0.0.1:7860
.\.venv\Scripts\python.exe app.py --offline --autoload
.\.venv\Scripts\python.exe app.py --offline --verify-model   # SHA-256 check of local models
# coach LLM (default --coach off):
.\.venv\Scripts\python.exe app.py --setup-coach; .\.venv\Scripts\python.exe app.py --download-coach
.\.venv\Scripts\python.exe app.py --autoload --coach local          # or --coach api --coach-url ... (COACH_API_KEY env)
```

Low VRAM: `--asr-device cpu` or `--max-length 4096`. Remote access: `--host 0.0.0.0`. A remote browser mic needs HTTPS (`--ssl-certfile/--ssl-keyfile`). Basic auth comes from the `APP_USER`/`APP_PASSWORD` env vars. The full usage notes are in the module docstring at the top of `app.py`.

## Architecture of app.py

- **Three models in one process**, with IDs and revisions pinned as constants at the top:
  - **Decider**: `perplexity-ai/pplx-decider-flash-v1.2`, run by `Runtime`. It imports `pplx_decider` from `models/decider-flash-v1.2/source/src`, which ships with the model, not from pip.
  - **ASR**: `Qwen/Qwen3-ASR-1.7B-hf`, run by `ASRRuntime` with native Transformers.
  - **Coach**: `Qwen/Qwen3.5-2B`, run by `GuidanceRuntime`. It loads locally in NF4 or calls an OpenAI-compatible `/chat/completions` API.
  - Model folders resolve as siblings of `--model-dir`: `models/qwen3-asr-1.7b` and `models/qwen3.5-2b-coach`. `models/qwen3-asr-0.6b` is a leftover and is not used by default.
- **Decider is a general structured judge, not an emotion model.** Emotion (`EMOTION_QUESTIONS`), consultation routing (`ROUTING_CONFIG`, `run_routing`) and the decision-demo tab (`decision_scenarios`, `decision_step`) all send Choice/Boolean/Score questions to the same `Runtime`. The routing thresholds `min_probability=0.70` and `min_margin=0.15` are demo values, not official criteria. When a judgment is uncertain, the flow stops at the next confirmation question.
- **Voice pipeline** (`VoiceService`):
  - Audio arrives from the browser mic, Windows WASAPI loopback (`SystemAudioCapture`, Windows-only, PyAudioWPatch) or file replay (`AudioFileReplay`). Every source is fed in about 0.5 s chunks.
  - `Segmenter` splits speech by RMS energy.
  - A single `voice-inference` worker thread runs ASR and then Decider. For partials it keeps only the latest snapshot; finals stay in order. The last segment is flushed on stop.
  - This is chunked re-transcription, not true streaming. Never describe it as streaming, and never invent speaker diarization or confidence values.
- **Coach staleness**: `GuidanceService` runs in its own worker and tags requests with `guidance_generation`. Old or cancelled results are dropped (`GuidanceCancelled`). Coach output is the JSON `staff_note` / `customer_question` / `action_items[]` (≤2), validated by `parse_guidance`. A coach failure must never discard the transcript or the Decider results.
- **State lives only in memory.** `VoiceSession` objects sit in `VoiceService.sessions` (max 16, sid is 32-char hex), with up to 500 final transcripts each. Nothing is persisted except an optional JSON export, and raw audio is never written to disk.
- **UI**: `build_app` assembles the tabs: the voice tab (`add_voice_tab`), the decision demo (`add_decision_demo_tab`) and the legacy PDF/image Decider playground. The PRD treats the PDF/image playground as out of AICC scope. Launch uses `queue(default_concurrency_limit=1)`, so GPU work is serialized.

## Constraints from PRD

- Use virtual or demo data only. Never log 사업자등록번호, phone numbers, names, transcript text or API keys.
- AI output is for staff reference only and never decides guarantee approval or eligibility.
- Separate a real HTTP call from an in-process inference step when tracing. Don't label a local ASR/Decider call as an HTTP API.
- Don't fabricate confidence, speaker labels or streaming capabilities the model doesn't provide.
- Don't report a test as passed unless it was actually run. Make no destructive changes to existing files without approval.
