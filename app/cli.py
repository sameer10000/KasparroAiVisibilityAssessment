"""Command-line entry points (in-process, no server needed).

  python -m app.cli init-db
  python -m app.cli run            # import pack -> analyse -> write output/analysis_output.json
  python -m app.cli record-replay  # MODEL_MODE=ollama: label all responses and save raw outputs for replay
"""
import argparse
import json
import sys
import time
from pathlib import Path

from . import analysis
from .config import load_settings
from .db import connect, dumps, init_db
from .ingest import DatasetIn, import_dataset, load_pack_payload
from .llm import PROMPT_VERSION, ModelError, build_gateway
from .extract import prose_view


def cmd_init_db(settings):
    conn = connect(settings.db_path)
    init_db(conn)
    print(f"initialised {settings.db_path}")


def cmd_run(settings, out_path: str):
    if not settings.pack_dir:
        sys.exit("Set PACK_DIR to the extracted assignment pack (see .env.example)")
    conn = connect(settings.db_path)
    init_db(conn)
    summary, created = import_dataset(conn, DatasetIn.model_validate(load_pack_payload(settings.pack_dir)), "cli:analyst")
    print(f"dataset {summary['dataset_id']} ({'new' if created else 'reused'}): {summary['counts']}")
    gateway = build_gateway(settings)
    analysis_id = analysis.create_run(conn, summary["dataset_id"], gateway, "cli:analyst")
    started = time.monotonic()
    analysis.execute(conn, analysis_id, gateway)
    run = conn.execute("SELECT * FROM analysis_runs WHERE analysis_id = ?", (analysis_id,)).fetchone()
    report = conn.execute("SELECT report_id FROM reports WHERE analysis_id = ?", (analysis_id,)).fetchone()
    content = json.loads(conn.execute("SELECT content_json FROM report_versions WHERE report_id = ? AND version = 1",
                                      (report["report_id"],)).fetchone()["content_json"])
    output = {
        "execution_mode": run["execution_mode"],
        "label": ("REPLAY of recorded local-model outputs: mechanics only, not fresh model evidence"
                  if run["execution_mode"] == "replay" else f"{run['execution_mode']} run"),
        "analysis_id": analysis_id, "report_id": report["report_id"],
        "config": json.loads(run["config_json"]), "counts": json.loads(run["counts_json"]),
        "wall_time_s": round(time.monotonic() - started, 1),
        "scores": json.loads(run["aggregates_json"]),
        "diagnostics": content["diagnostics"], "remedies": content["remedies"],
        "results": [{k: r[k] for k in ("response_id", "prompt_id", "engine", "brand_scope", "intent", "status",
                                       "reason", "labels", "components", "score", "evidence")}
                    for r in analysis.result_rows(conn, analysis_id)],
    }
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(output, indent=2, ensure_ascii=False))
    print(f"analysis {analysis_id} [{run['execution_mode']}] counts={output['counts']}")
    print(f"report {report['report_id']} (draft v1); output written to {out_path}")


def cmd_record_replay(settings, out_path: str, limit: int | None):
    """Runs the real local model once over every response and stores the raw outputs."""
    if settings.model_mode != "ollama":
        sys.exit("record-replay needs MODEL_MODE=ollama")
    payload = load_pack_payload(settings.pack_dir)
    prompts = {p["prompt_id"]: p["text"] for p in payload["prompts"]}
    gateway = build_gateway(settings)
    done = set()
    out = Path(out_path)
    if out.exists():
        done = {json.loads(l)["response_id"] for l in out.read_text().splitlines() if l.strip()}
    responses = payload["responses"]
    edge = Path(__file__).resolve().parent.parent / "eval" / "cases" / "v1" / "edge_cases.jsonl"
    for line in edge.read_text().splitlines():  # synthetic eval cases are recorded too, keyed by case_id
        c = json.loads(line)
        prompts[c["case_id"]] = c["question"]
        responses.append({"response_id": c["case_id"], "prompt_id": c["case_id"], "engine": "synthetic",
                          "answer_markdown": c["answer_markdown"], "answer_text": c["answer_markdown"]})
    todo = [r for r in responses if r["response_id"] not in done][:limit]
    print(f"{len(done)} already recorded, {len(todo)} to go with {settings.ollama_model}")
    with out.open("a", encoding="utf-8") as f:
        for i, r in enumerate(todo, 1):
            rec = {"response_id": r["response_id"], "prompt_version": PROMPT_VERSION, "model": settings.ollama_model}
            try:
                raw, trace = gateway.classify(r["response_id"], prompts[r["prompt_id"]],
                                              prose_view(r.get("answer_markdown"), r["answer_text"]))
                rec.update(raw_output=raw, latency_ms=trace.latency_ms, attempts=len(trace.attempts))
            except ModelError as e:
                rec.update(raw_output=None, error=e.code)
            f.write(dumps(rec) + "\n")
            f.flush()
            print(f"[{i}/{len(todo)}] {r['engine']:8s} {r['prompt_id'][-3:]} {rec.get('latency_ms', '-')}ms {rec.get('error', '')}")


def main():
    ap = argparse.ArgumentParser(prog="python -m app.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    p = sub.add_parser("run")
    p.add_argument("--out", default="output/analysis_output.json")
    p = sub.add_parser("record-replay")
    p.add_argument("--out", default=None)
    p.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    settings = load_settings()
    if args.cmd == "init-db":
        cmd_init_db(settings)
    elif args.cmd == "run":
        cmd_run(settings, args.out)
    else:
        cmd_record_replay(settings, args.out or settings.replay_path, args.limit)


if __name__ == "__main__":
    main()
