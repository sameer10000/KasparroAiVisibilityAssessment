"""Import and validate the supplied pack records into our own contract.

Duplicate policy (per record, keyed by the supplied stable ID):
  * same ID, identical content  -> reused (stored once, linked to the new dataset)
  * same ID, different content  -> conflict, record rejected, everything else still imports
  * same ID twice in one payload -> second copy treated by the same rules
A payload identical to an earlier import returns that dataset instead of creating a new one.
"""
import hashlib
import json
import sqlite3
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .db import dumps, now

ENGINES = ("aimode", "chatgpt", "gemini")
MAX_ERRORS_REPORTED = 50


class PromptIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    prompt_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    text: str = Field(min_length=1)


class ResponseIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    response_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    prompt_id: str = Field(min_length=1)
    engine: Literal["aimode", "chatgpt", "gemini"]
    answer_text: str = Field(min_length=1)
    answer_markdown: str | None = None
    model_version: str | None = None  # unknown stays None, never guessed
    capture_status: Literal["captured"]


class CitationIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    citation_id: str = Field(min_length=1)
    response_id: str = Field(min_length=1)
    prompt_id: str = Field(min_length=1)
    engine: Literal["aimode", "chatgpt", "gemini"]
    order: int = Field(ge=1)
    citation_type: Literal["inline_hyperlink", "sources_panel"]
    url: str = Field(min_length=1)
    domain: str | None = None
    publication_date: str | None = None


class DatasetIn(BaseModel):
    """Request body for POST /datasets. Records are validated one by one below so that a
    single bad record is reported instead of failing the whole import."""
    model_config = ConfigDict(extra="forbid")
    run: dict
    prompts: list[dict] = Field(min_length=1, max_length=10_000)
    responses: list[dict] = Field(min_length=1, max_length=10_000)
    citations: list[dict] = Field(default_factory=list, max_length=100_000)


class ImportError_(Exception):
    def __init__(self, status: int, code: str, message: str, details=None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


def content_hash(record: dict) -> str:
    return hashlib.sha256(dumps(record).encode()).hexdigest()


def _counter():
    return {"received": 0, "inserted": 0, "reused": 0, "conflict": 0, "invalid": 0}


def import_dataset(conn: sqlite3.Connection, payload: DatasetIn, actor: str) -> tuple[dict, bool]:
    """Returns (dataset summary, created). created=False means an identical import already existed."""
    run_id = payload.run.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ImportError_(422, "invalid_run", "run.run_id is required")

    payload_hash = content_hash(payload.model_dump())
    existing = conn.execute(
        "SELECT dataset_id, summary_json FROM datasets WHERE json_extract(summary_json, '$.payload_hash') = ?",
        (payload_hash,),
    ).fetchone()
    if existing:
        return json.loads(existing["summary_json"]), False

    errors: list[dict] = []
    counts = {"prompts": _counter(), "responses": _counter(), "citations": _counter()}
    dataset_id = str(uuid.uuid4())
    linked_responses: set[str] = set()

    def err(kind, idx, rec_id, code, detail):
        counts[kind][code] += 1
        if len(errors) < MAX_ERRORS_REPORTED:
            errors.append({"type": kind, "index": idx, "id": rec_id, "error": code, "detail": detail})

    def upsert(kind, table, key_col, key, raw, row: dict) -> str:
        """Insert or reuse a record. Returns inserted | reused | conflict."""
        h = content_hash(raw)
        found = conn.execute(f"SELECT content_hash FROM {table} WHERE {key_col} = ?", (key,)).fetchone()
        if found:
            if found["content_hash"] == h:
                counts[kind]["reused"] += 1
                return "reused"
            return "conflict"
        cols = list(row) + ["content_hash", "raw_json"]
        conn.execute(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [*row.values(), h, dumps(raw)],
        )
        counts[kind]["inserted"] += 1
        return "inserted"

    conn.execute("BEGIN")
    try:
        conn.execute("INSERT INTO datasets VALUES (?, ?, ?, ?, '{}')", (dataset_id, run_id, actor, now()))
        for i, raw in enumerate(payload.prompts):
            counts["prompts"]["received"] += 1
            try:
                p = PromptIn.model_validate(raw)
            except ValidationError as e:
                err("prompts", i, raw.get("prompt_id") if isinstance(raw, dict) else None, "invalid", _short(e))
                continue
            if p.run_id != run_id:
                err("prompts", i, p.prompt_id, "invalid", f"run_id {p.run_id} does not match run {run_id}")
                continue
            outcome = upsert("prompts", "prompts", "prompt_id", p.prompt_id, raw,
                             {"prompt_id": p.prompt_id, "run_id": p.run_id, "text": p.text})
            if outcome == "conflict":
                err("prompts", i, p.prompt_id, "conflict", "same prompt_id already stored with different content")

        for i, raw in enumerate(payload.responses):
            counts["responses"]["received"] += 1
            try:
                r = ResponseIn.model_validate(raw)
            except ValidationError as e:
                err("responses", i, raw.get("response_id") if isinstance(raw, dict) else None, "invalid", _short(e))
                continue
            if r.run_id != run_id:
                err("responses", i, r.response_id, "invalid", f"run_id {r.run_id} does not match run {run_id}")
                continue
            if not conn.execute("SELECT 1 FROM prompts WHERE prompt_id = ?", (r.prompt_id,)).fetchone():
                err("responses", i, r.response_id, "invalid", f"unknown prompt_id {r.prompt_id}")
                continue
            outcome = upsert("responses", "responses", "response_id", r.response_id, raw, {
                "response_id": r.response_id, "run_id": r.run_id, "prompt_id": r.prompt_id,
                "engine": r.engine, "model_version": r.model_version,
                "answer_text": r.answer_text, "answer_markdown": r.answer_markdown,
            })
            if outcome == "conflict":
                err("responses", i, r.response_id, "conflict", "same response_id already stored with different content")
                continue
            if r.response_id in linked_responses:
                continue  # repeated inside this payload: already linked once
            linked_responses.add(r.response_id)
            conn.execute("INSERT INTO dataset_responses VALUES (?, ?)", (dataset_id, r.response_id))

        for i, raw in enumerate(payload.citations):
            counts["citations"]["received"] += 1
            try:
                c = CitationIn.model_validate(raw)
            except ValidationError as e:
                err("citations", i, raw.get("citation_id") if isinstance(raw, dict) else None, "invalid", _short(e))
                continue
            if not conn.execute("SELECT 1 FROM responses WHERE response_id = ?", (c.response_id,)).fetchone():
                err("citations", i, c.citation_id, "invalid", f"unknown response_id {c.response_id}")
                continue
            outcome = upsert("citations", "citations", "citation_id", c.citation_id, raw, {
                "citation_id": c.citation_id, "response_id": c.response_id, "ord": c.order,
                "citation_type": c.citation_type, "url": c.url, "domain": c.domain,
            })
            if outcome == "conflict":
                err("citations", i, c.citation_id, "conflict", "same citation_id already stored with different content")

        if not linked_responses:
            raise ImportError_(422, "no_valid_responses", "No valid responses in payload", {"counts": counts, "errors": errors})

        summary = {
            "dataset_id": dataset_id, "run_id": run_id, "payload_hash": payload_hash,
            "response_count": len(linked_responses), "counts": counts,
            "errors": errors, "errors_truncated": sum(
                c["invalid"] + c["conflict"] for c in counts.values()) > len(errors),
        }
        conn.execute("UPDATE datasets SET summary_json = ? WHERE dataset_id = ?", (dumps(summary), dataset_id))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return summary, True


def _short(e: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:5])


def load_pack_payload(pack_dir: str) -> dict:
    """Read the supplied pack files (read-only) into a POST /datasets body."""
    from pathlib import Path
    base = Path(pack_dir)

    def jsonl(name):
        with open(base / "data" / name, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    return {
        "run": json.loads((base / "data" / "run.json").read_text(encoding="utf-8")),
        "prompts": jsonl("prompts.jsonl"),
        "responses": jsonl("responses.jsonl"),
        "citations": jsonl("citations.jsonl"),
    }
