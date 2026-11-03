"""SQLite storage. The schema is created idempotently on startup (or via `python -m app.cli init-db`)."""
import json
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    dataset_id      TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    summary_json    TEXT NOT NULL
);

-- Raw inputs are stored verbatim (raw_json) next to the few columns we query on.
CREATE TABLE IF NOT EXISTS prompts (
    prompt_id       TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    text            TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    raw_json        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS responses (
    response_id     TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    prompt_id       TEXT NOT NULL REFERENCES prompts(prompt_id),
    engine          TEXT NOT NULL,
    model_version   TEXT,
    answer_text     TEXT NOT NULL,
    answer_markdown TEXT,
    content_hash    TEXT NOT NULL,
    raw_json        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS citations (
    citation_id     TEXT PRIMARY KEY,
    response_id     TEXT NOT NULL REFERENCES responses(response_id),
    ord             INTEGER NOT NULL,
    citation_type   TEXT NOT NULL,
    url             TEXT NOT NULL,
    domain          TEXT,
    content_hash    TEXT NOT NULL,
    raw_json        TEXT NOT NULL
);

-- Which records an import contained (a record can belong to several imports, stored once).
CREATE TABLE IF NOT EXISTS dataset_responses (
    dataset_id      TEXT NOT NULL REFERENCES datasets(dataset_id),
    response_id     TEXT NOT NULL REFERENCES responses(response_id),
    PRIMARY KEY (dataset_id, response_id)
);

CREATE TABLE IF NOT EXISTS analysis_runs (
    analysis_id     TEXT PRIMARY KEY,
    dataset_id      TEXT NOT NULL REFERENCES datasets(dataset_id),
    status          TEXT NOT NULL,          -- queued | running | completed | failed
    execution_mode  TEXT NOT NULL,          -- replay | stub | ollama
    config_json     TEXT NOT NULL,          -- prompt/model/scoring/category versions
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    finished_at     TEXT,
    counts_json     TEXT,
    aggregates_json TEXT,
    error           TEXT
);

CREATE TABLE IF NOT EXISTS response_results (
    analysis_id     TEXT NOT NULL REFERENCES analysis_runs(analysis_id),
    response_id     TEXT NOT NULL REFERENCES responses(response_id),
    status          TEXT NOT NULL,          -- ok | failed | skipped
    reason          TEXT,
    labels_json     TEXT,
    components_json TEXT,
    score           REAL,
    evidence_json   TEXT,
    trace_json      TEXT NOT NULL,
    PRIMARY KEY (analysis_id, response_id)
);

CREATE TABLE IF NOT EXISTS reports (
    report_id       TEXT PRIMARY KEY,
    analysis_id     TEXT NOT NULL REFERENCES analysis_runs(analysis_id),
    created_at      TEXT NOT NULL
);

-- Once a version leaves 'draft' its content never changes; edits create a new version.
CREATE TABLE IF NOT EXISTS report_versions (
    report_id       TEXT NOT NULL REFERENCES reports(report_id),
    version         INTEGER NOT NULL,
    status          TEXT NOT NULL,          -- draft | submitted | approved | rejected
    content_json    TEXT NOT NULL,
    content_hash    TEXT,                   -- set at submit time
    based_on        INTEGER,
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    submitted_by    TEXT,
    submitted_at    TEXT,
    PRIMARY KEY (report_id, version)
);

CREATE TABLE IF NOT EXISTS review_decisions (
    decision_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id       TEXT NOT NULL,
    version         INTEGER NOT NULL,
    content_hash    TEXT NOT NULL,
    decision        TEXT NOT NULL,          -- approved | rejected
    reviewer        TEXT NOT NULL,
    comment         TEXT NOT NULL,
    decided_at      TEXT NOT NULL,
    FOREIGN KEY (report_id, version) REFERENCES report_versions(report_id, version)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)
