"""Analysis run: per-response extraction + model labelling + scoring, then aggregation and a draft report.

Each response is processed and committed independently, so one bad record cannot erase the others.
Outcomes per response:
  ok      : labels validated, score computed
  failed  : attempted, but the model or its output failed validation (no score, never a zero)
  skipped : not attempted (e.g. call budget exhausted), with the reason recorded
"""
import json
import sqlite3
import uuid

from . import categories, scoring
from .db import dumps, now
from .diagnostics import build_report_content
from .extract import QUOTE_MATCH_VERSION, OutputInvalid, deterministic_facts, injection_suspected, parse_model_output, prose_view, reconcile
from .llm import PROMPT_VERSION, ModelError, ModelGateway

RAW_OUTPUT_TRACE_CHARS = 4000


def run_config(gateway: ModelGateway) -> dict:
    return {
        "execution_mode": gateway.mode,
        "model": gateway.model,
        "prompt_version": PROMPT_VERSION,
        "score_version": scoring.SCORE_VERSION,
        "category_version": categories.CATEGORY_VERSION,
        "quote_match_version": QUOTE_MATCH_VERSION,
        "max_retries": gateway.max_retries,
        "max_calls": gateway.max_calls,
    }


def create_run(conn: sqlite3.Connection, dataset_id: str, gateway: ModelGateway, actor: str) -> str:
    analysis_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO analysis_runs (analysis_id, dataset_id, status, execution_mode, config_json, created_by, created_at)"
        " VALUES (?, ?, 'queued', ?, ?, ?, ?)",
        (analysis_id, dataset_id, gateway.mode, dumps(run_config(gateway)), actor, now()),
    )
    return analysis_id


def analyse_response(row: sqlite3.Row, prompt_text: str, cits: list[dict], gateway: ModelGateway) -> dict:
    cat = categories.categorise(prompt_text)
    facts = deterministic_facts(row["answer_markdown"], row["answer_text"], cits)
    trace = {
        "response_id": row["response_id"], "prompt_id": row["prompt_id"], "engine": row["engine"],
        "model_version_reported": row["model_version"], "categories": cat,
        "deterministic_facts": facts, **run_config(gateway),
    }
    result = {"status": None, "reason": None, "labels": None, "components": None, "score": None,
              "evidence": None, "trace": trace}
    suspicious = injection_suspected(row["answer_text"] + "\n" + (row["answer_markdown"] or ""))
    if suspicious:
        # Labels from a model that has read instructions aimed at it cannot be trusted; keep the record
        # visible as a failure for human review instead of scoring it.
        trace["injection_match"] = suspicious
        return {**result, "status": "failed", "reason": "suspected_prompt_injection"}
    try:
        raw, call = gateway.classify(row["response_id"], prompt_text, prose_view(row["answer_markdown"], row["answer_text"]))
        trace["call"] = call.to_dict()
        trace["call"]["raw_output"] = (raw or "")[:RAW_OUTPUT_TRACE_CHARS]
        labels = parse_model_output(raw)
    except ModelError as e:
        trace["call"] = getattr(e, "trace", None).to_dict() if getattr(e, "trace", None) else {"error": e.code}
        status = "skipped" if e.code == "call_budget_exhausted" else "failed"
        return {**result, "status": status, "reason": e.code}
    except OutputInvalid as e:
        return {**result, "status": "failed", "reason": e.code}

    final, evidence, notes = reconcile(facts, labels, prose_view(row["answer_markdown"], row["answer_text"]))
    trace["guard_notes"] = notes
    comp = scoring.components(final, facts["citations"]["owned_cited"])
    return {**result, "status": "ok", "labels": final, "components": comp,
            "score": scoring.score(comp), "evidence": evidence}


def execute(conn: sqlite3.Connection, analysis_id: str, gateway: ModelGateway) -> None:
    run = conn.execute("SELECT * FROM analysis_runs WHERE analysis_id = ?", (analysis_id,)).fetchone()
    conn.execute("UPDATE analysis_runs SET status = 'running' WHERE analysis_id = ?", (analysis_id,))
    try:
        responses = conn.execute(
            "SELECT r.*, p.text AS prompt_text FROM responses r JOIN dataset_responses d USING (response_id)"
            " JOIN prompts p ON p.prompt_id = r.prompt_id WHERE d.dataset_id = ? ORDER BY r.prompt_id, r.engine",
            (run["dataset_id"],),
        ).fetchall()
        for row in responses:
            cits = [dict(c) for c in conn.execute(
                "SELECT url, domain, citation_type FROM citations WHERE response_id = ?", (row["response_id"],))]
            try:
                res = analyse_response(row, row["prompt_text"], cits, gateway)
            except Exception as e:  # one bad record must not stop the run
                res = {"status": "failed", "reason": f"internal_error: {type(e).__name__}", "labels": None,
                       "components": None, "score": None, "evidence": None,
                       "trace": {"response_id": row["response_id"], "error": str(e)[:300]}}
            conn.execute(
                "INSERT OR REPLACE INTO response_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (analysis_id, row["response_id"], res["status"], res["reason"],
                 dumps(res["labels"]) if res["labels"] else None,
                 dumps(res["components"]) if res["components"] else None,
                 res["score"], dumps(res["evidence"]) if res["evidence"] else None, dumps(res["trace"])),
            )

        rows = result_rows(conn, analysis_id)
        aggregates = scoring.aggregate(rows)
        counts = {k: aggregates["overall"][k] for k in ("eligible", "ok", "failed", "skipped")}
        counts["failure_reasons"] = {}
        for r in rows:
            if r["status"] != "ok":
                counts["failure_reasons"][r["reason"]] = counts["failure_reasons"].get(r["reason"], 0) + 1
        counts["model_calls"] = gateway.calls_made
        conn.execute(
            "UPDATE analysis_runs SET status = 'completed', finished_at = ?, counts_json = ?, aggregates_json = ?"
            " WHERE analysis_id = ?", (now(), dumps(counts), dumps(aggregates), analysis_id))

        content = build_report_content(conn, analysis_id, rows, aggregates, json.loads(run["config_json"]))
        report_id = str(uuid.uuid4())
        ts = now()
        conn.execute("INSERT INTO reports VALUES (?, ?, ?)", (report_id, analysis_id, ts))
        conn.execute(
            "INSERT INTO report_versions (report_id, version, status, content_json, created_by, created_at, updated_at)"
            " VALUES (?, 1, 'draft', ?, ?, ?, ?)", (report_id, dumps(content), "system:analysis", ts, ts))
    except Exception as e:
        conn.execute("UPDATE analysis_runs SET status = 'failed', finished_at = ?, error = ? WHERE analysis_id = ?",
                     (now(), f"{type(e).__name__}: {e}"[:500], analysis_id))
        raise


def result_rows(conn: sqlite3.Connection, analysis_id: str) -> list[dict]:
    out = []
    for r in conn.execute(
        "SELECT rr.*, r.engine, r.prompt_id, p.text AS prompt_text FROM response_results rr"
        " JOIN responses r USING (response_id) JOIN prompts p ON p.prompt_id = r.prompt_id"
        " WHERE rr.analysis_id = ? ORDER BY r.prompt_id, r.engine", (analysis_id,)):
        cat = categories.categorise(r["prompt_text"])
        out.append({
            "response_id": r["response_id"], "prompt_id": r["prompt_id"], "engine": r["engine"],
            "brand_scope": cat["brand_scope"], "intent": cat["intent"],
            "status": r["status"], "reason": r["reason"],
            "labels": json.loads(r["labels_json"]) if r["labels_json"] else None,
            "components": json.loads(r["components_json"]) if r["components_json"] else None,
            "score": r["score"],
            "evidence": json.loads(r["evidence_json"]) if r["evidence_json"] else None,
            "trace": json.loads(r["trace_json"]),
        })
    return out
