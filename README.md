# Kasparro AI visibility analysis service

This backend turns the supplied snapshot (50 prompts, 150 archived answers from Google AI Mode, ChatGPT and Gemini, and 2,439 citations) into:

- labels for each response
- a visibility score anyone can recompute
- three diagnostics backed by evidence
- three prioritised remedies

A human reviewer controls which report version can be exported.

Stack: Python 3.14, FastAPI, SQLite (stdlib `sqlite3`), pydantic, httpx. The local model is `qwen2.5:3b` via Ollama. No paid services are used.

## Layout

| Path | What it does |
|---|---|
| `app/ingest.py` | Validates input records, persists raw JSON, handles duplicate IDs |
| `app/categories.py` | Deterministic prompt categories (`cat-v1`): branded vs unbranded × intent |
| `app/llm.py` | Model adapter (ollama / replay / stub), prompt `prompt-v1`, retry/time/size/call bounds |
| `app/extract.py` | Deterministic mention/citation facts; validation of model output; verbatim-quote guard |
| `app/scoring.py` | Score `score-v1`, aggregation with denominators and Wilson intervals |
| `app/analysis.py` | Per-response processing, persisted one record at a time |
| `app/diagnostics.py` | Diagnostic detectors, remedies, evidence verification |
| `app/reports.py` | Report versions, review decisions, export rules |
| `app/api.py` | HTTP API (OpenAPI docs at `/docs`) |
| `app/db.py` | SQLite schema (created on startup or with `init-db`) |
| `eval/` | Labelled cases, synthetic edge cases, grader, results |
| `fixtures/controlled_fixture.json` | Synthetic records for the engineering tests |
| `replay/` | Raw outputs recorded from one real local-model run |
| `scripts/demo_flow.py` | Walks the API through import → analyse → review → export |

## Setup (about 5 minutes, plus an optional model download)

```bash
cd kasparro-visibility
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then set PACK_DIR and change the two tokens
python -m app.cli init-db   # creates kasparro.db (the API also does this on startup)
```

`PACK_DIR` must point at the extracted assignment folder. Inputs are only read and never modified, and the pack is not copied into this repository.

### Execution modes (`MODEL_MODE` in `.env`)

| Mode | What runs | Use |
|---|---|---|
| `replay` (default) | Raw outputs recorded from one real `qwen2.5:3b` run (`replay/qwen2.5-3b_prompt-v1.jsonl`) | Reproduces the submitted results without a model. Proves mechanics, not live-model quality. |
| `ollama` | Real local model over HTTP | Live results |
| `stub` | Deterministic fake | Engineering tests only. Not a quality result. |

Every analysis run, trace, report and export records its `execution_mode`.

To switch to the real model:

```bash
brew install ollama          # or see ollama.com; then:
ollama serve &               # if not already running
ollama pull qwen2.5:3b       # ~1.9 GB download
MODEL_MODE=ollama python -m app.cli run                    # ~10-15 s per answer on an 8 GB M1 (~35 min)
MODEL_MODE=ollama python -m app.cli record-replay --out replay/new_run.jsonl   # re-record replay outputs
```

## Commands

```bash
python -m pytest -q                 # engineering tests (stub model, synthetic fixture), ~2 s
python -m app.cli run               # import pack -> analyse -> output/analysis_output.json
python -m eval.run_eval             # evaluation -> eval/results/report_<mode>.md and results_<mode>.json
uvicorn app.api:app --port 8000     # API; docs at http://localhost:8000/docs
python scripts/demo_flow.py         # full review flow against the running API
```

## Web page

`uvicorn app.api:app --port 8000`, then open http://localhost:8000/. The page shows reports, findings with their evidence, remedies and version history, and has buttons for save, submit, approve/reject and export.
- **Normal mode:** paste the analyst and reviewer tokens once, then switch roles with one click.
- **Demo mode:** with `DEMO_LOGIN=true` (local only; `false` in `.env.example`), the page offers an Analyst/Reviewer dropdown instead. The server still authorises every request by token. Demo login only hands the page the configured token for the chosen role, so never enable it where others can reach the server.

## API and example requests

Authentication is `Authorization: Bearer <token>`. The role comes only from which configured token is presented. Fields such as `role` or `status` in a request body are rejected with 422.

| Method and path | Role | Purpose |
|---|---|---|
| `GET /health` | none | Liveness, DB check, model mode |
| `POST /datasets` | analyst | Import and validate a pack (`{run, prompts, responses, citations}`) |
| `GET /datasets/{id}` | any | Import summary with per-record errors |
| `POST /analyses` | analyst | Start an analysis (`{dataset_id}`); returns 202 |
| `GET /analyses/{id}` | any | Status, counts, failure reasons, scores, `report_id` |
| `GET /analyses/{id}/results?status=failed&include_trace=true` | any | Per-response results and traces |
| `GET /reports/{id}[?version=N]` | any | Report content and version history with decisions |
| `PATCH /reports/{id}` | analyst | Edit `title`, `notes`, `diagnostics`, `remedies` |
| `POST /reports/{id}/submit` | analyst | Freeze the current draft (`{version}`) |
| `POST /reports/{id}/decision` | reviewer | `{version, decision: approve|reject, comment}` |
| `GET /reports/{id}/export[?version=N]` | any | Approved content only |

```bash
A="Authorization: Bearer $ANALYST_TOKEN"; R="Authorization: Bearer $REVIEWER_TOKEN"
python -c "from app.ingest import load_pack_payload; import json,os; print(json.dumps(load_pack_payload(os.environ['PACK_DIR'])))" > /tmp/pack.json

curl -s -X POST localhost:8000/datasets -H "$A" -H 'content-type: application/json' --data @/tmp/pack.json
curl -s -X POST localhost:8000/analyses -H "$A" -H 'content-type: application/json' -d '{"dataset_id":"<dataset_id>"}'
curl -s localhost:8000/analyses/<analysis_id> -H "$A"                       # -> report_id once completed
curl -s -X PATCH localhost:8000/reports/<report_id> -H "$A" -H 'content-type: application/json' -d '{"notes":"checked"}'
curl -s -X POST localhost:8000/reports/<report_id>/submit -H "$A" -H 'content-type: application/json' -d '{"version":1}'
curl -s -X POST localhost:8000/reports/<report_id>/decision -H "$R" -H 'content-type: application/json' \
     -d '{"version":1,"decision":"reject","comment":"D3 overstates the evidence"}'
curl -s localhost:8000/reports/<report_id>/export -H "$A"                   # 409 rejected
# edit -> creates v2 draft; submit v2; approve v2; export -> 200
curl -s -X POST localhost:8000/reports/<report_id>/decision -H "$R" -H 'content-type: application/json' \
     -d '{"version":2,"decision":"approve","comment":"Evidence verified"}'
curl -s localhost:8000/reports/<report_id>/export -H "$A"                   # 200, version 2
```

## Rules the code enforces

- **Duplicates.** Records are keyed by their supplied IDs and stored with a content hash.
  - Same ID with identical content is reused (stored once and linked to the new import).
  - Same ID with different content is a `conflict`: that record is rejected and the rest still imports.
  - Re-posting an identical payload returns the original dataset (HTTP 200).
- **Mentions and citations.**
  - Mentions are counted in the prose (`answer_markdown`, with URLs removed). A kasparro.com URL alone is a citation, not a mention.
  - `answer_text` and `answer_markdown` are one response and are never counted twice.
  - Citation occurrences are kept as supplied. "Cited" flags deduplicate per response on a normalised URL (no fragment, no `utm_*`, no trailing slash).
- **Model output** must parse as JSON and match the schema. Otherwise the response is `failed` (`malformed_json`, `empty_output`, `schema_mismatch`, `timeout`, `output_too_large`) and gets no score. A model label survives only with a verbatim quote from the answer. Recommendation and negative labels need Kasparro to be mentioned. Competitor names must appear in the answer.
- **Bounds.** Timeout per call (`MODEL_TIMEOUT_S`), retries for transient errors only (`MODEL_MAX_RETRIES`), a total call budget (`MODEL_MAX_CALLS`, after which responses are `skipped`), and output size (`MODEL_MAX_OUTPUT_CHARS`). Input answers are clipped at 12,000 characters.
- **Untrusted text.** The answer is wrapped as data in the prompt. The model has no tools, and its output can only set labels, which are then checked. Identity, permissions and export are decided only by server code.
- **Review.**
  - Only drafts are mutable.
  - Submitting records a SHA-256 of the content.
  - A decision applies to one exact version and hash.
  - Editing a submitted, approved or rejected version creates a new draft.
  - Export refuses draft, submitted, rejected and stale versions. An older approved version can be exported only by naming it (`?version=N`), and the export says `is_latest: false`.

## Not implemented (future work)

- **Approval expiry.** Proposed: an approval is valid for N days or until the analysis config changes, checked at export.
- **Review deadline.** A submitted version past its deadline should go to an `expired` state and never be auto-approved.
- **Optimistic locking on PATCH.** An `If-Match` version header would stop silent overwrites. Today `BEGIN IMMEDIATE` serialises writes, but two analysts can still overwrite each other's draft edits.
- **Restart/resume of an interrupted analysis.** Results are committed per response, so resuming would mean skipping already-processed IDs.
- **Opening cited pages** to verify claim support. All citation support is reported as unverified.
