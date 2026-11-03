"""Evaluation runner: one command reruns everything and writes eval/results/report.md + results.json.

    python -m eval.run_eval            # uses MODEL_MODE from env (.env), replay by default

What it measures
  1. Per-label precision / recall / accuracy on hand-labelled cases, for the model pipeline and for a
     rule-based keyword baseline, split into tuning and final cases.
  2. Competitor recall on cases with a must-include list, and forbidden names (engines, Kasparro).
  3. Score arithmetic: the pipeline score must equal an independent re-implementation of score-v1.
  4. Diagnostics: each claim's numbers are recomputed from per-response results and each excerpt is
     checked verbatim against its response. A deliberately corrupted diagnostic must be caught.
Limits: labels are one person's judgement; there is one model run per case; the grader checks
evidence and arithmetic, not whether the prose of a diagnostic is wise.
"""
import json
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import analysis, scoring  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.db import connect, init_db  # noqa: E402
from app.diagnostics import verify_evidence  # noqa: E402
from app.extract import KNOWN_COMPETITORS, prose_view  # noqa: E402
from app.ingest import DatasetIn, import_dataset, load_pack_payload  # noqa: E402
from app.llm import PROMPT_VERSION, build_gateway  # noqa: E402

CASES = ROOT / "eval" / "cases" / "v1"
OUT = ROOT / "eval" / "results"
LABELS = ("mentioned", "recommended", "negative", "entity_confusion")
FORBIDDEN_COMPETITORS = {"kasparro", "kaspr", "chatgpt", "gemini", "claude", "perplexity", "copilot"}


# ---------------------------------------------------------------- baseline
# Terms come from generic wording plus the tuning split only (C01, C03); final cases were not used.
NEG_RE = re.compile(r"not widely|couldn't find|could not find|limited public|lean toward|very new|"
                    r"little independent|not well known|scam|unproven|avoid", re.I)
REC_RE = re.compile(r"(choose|recommend|go with|worth it|good fit|best for)[^.\n]{0,80}kasparro|"
                    r"kasparro[^.\n]{0,80}(is a good|is worth|is the best|recommended)", re.I)


def baseline(answer_text: str) -> dict:
    """Keyword rules a reviewer could write in ten minutes. Works on the raw text, URLs included."""
    mentioned = "kasparro" in answer_text.lower()
    return {
        "mentioned": mentioned,
        "recommended": bool(mentioned and REC_RE.search(answer_text)),
        "negative": bool(mentioned and NEG_RE.search(answer_text)),
        "entity_confusion": bool(re.search(r"\bkaspr\b", answer_text, re.I)),
        "competitors": [c for c in KNOWN_COMPETITORS if re.search(r"\b" + re.escape(c) + r"\b", answer_text, re.I)],
    }


# ---------------------------------------------------------------- helpers
def load_cases(pack_dir: str) -> list[dict]:
    payload = load_pack_payload(pack_dir)
    responses = {r["response_id"]: r for r in payload["responses"]}
    prompts = {p["prompt_id"]: p["text"] for p in payload["prompts"]}
    cits: dict[str, list] = {}
    for c in payload["citations"]:
        cits.setdefault(c["response_id"], []).append(c)
    cases = []
    for line in (CASES / "labels.jsonl").read_text().splitlines():
        c = json.loads(line)
        r = responses[c["response_id"]]
        c.update(question=prompts[r["prompt_id"]], prompt_id=r["prompt_id"], answer_markdown=r["answer_markdown"],
                 answer_text=r["answer_text"], citations=cits.get(r["response_id"], []), model_version=r["model_version"])
        cases.append(c)
    for line in (CASES / "edge_cases.jsonl").read_text().splitlines():
        c = json.loads(line)
        c.update(response_id=c["case_id"], prompt_id=f"synthetic:{c['case_id']}", answer_text=c["answer_markdown"],
                 model_version=None)
        cases.append(c)
    return cases


def check_label_file(cases) -> list[str]:
    """The labels themselves must quote their responses verbatim."""
    from app.extract import quote_in
    problems = []
    for c in cases:
        for key, quote in (c.get("evidence") or {}).items():
            if not quote_in(quote, prose_view(c["answer_markdown"], c["answer_text"])):
                problems.append(f"{c['case_id']}: expected {key} evidence not found verbatim")
    return problems


def run_pipeline(case: dict, gateway) -> dict:
    row = {"response_id": case["response_id"], "prompt_id": case["prompt_id"], "engine": case["engine"],
           "model_version": case["model_version"], "answer_markdown": case["answer_markdown"],
           "answer_text": case["answer_text"]}
    return analysis.analyse_response(row, case["question"], case["citations"], gateway)


def prf(pairs: list[tuple[bool, bool]]) -> dict:
    tp = sum(e and p for e, p in pairs)
    fp = sum((not e) and p for e, p in pairs)
    fn = sum(e and (not p) for e, p in pairs)
    tn = sum((not e) and (not p) for e, p in pairs)
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "n": len(pairs),
            "precision": round(tp / (tp + fp), 3) if tp + fp else None,
            "recall": round(tp / (tp + fn), 3) if tp + fn else None,
            "accuracy": round((tp + tn) / len(pairs), 3) if pairs else None}


def reference_score(labels: dict, owned_cited: bool) -> float:
    """Independent re-implementation of score-v1 for checking the production code."""
    if not labels["mentioned"]:
        total = 0.0
    else:
        total = 25 + (0 if labels["entity_confusion"] else 15)
        if labels["recommended"]:
            total += 40 + (10 / labels["recommendation_position"] if labels.get("recommendation_position") else 0)
    total += 10 if owned_cited else 0
    total -= 30 if labels["negative"] else 0
    return round(min(100.0, max(0.0, total)), 2)


# ---------------------------------------------------------------- diagnostics
def recompute_claims(rows: list[dict], diag: dict) -> list[str]:
    """Recompute the headline fraction of each diagnostic from per-response results."""
    def frac(scope, pred):
        ok = [r for r in rows if r["brand_scope"] == scope and r["status"] == "ok"]
        return f"{sum(1 for r in ok if pred(r))}/{len(ok)}"

    checks = {
        "D1": [("mentioned", frac("unbranded", lambda r: r["labels"]["mentioned"]))],
        "D2": [("entity_confusion", frac("branded", lambda r: r["labels"]["entity_confusion"]))],
        "D3": [("negative", frac("branded", lambda r: r["labels"]["negative"])),
               ("recommended", frac("branded", lambda r: r["labels"]["recommended"]))],
    }
    problems = []
    for key, expected in checks.get(diag["id"], []):
        claimed = diag.get("metrics", {}).get(key)
        if claimed != expected:
            problems.append(f"{diag['id']}.{key}: claims {claimed}, results give {expected}")
        if expected not in diag.get("pattern", ""):
            problems.append(f"{diag['id']}: pattern text does not state {key} = {expected}")
    return problems


def grade_diagnostics(conn, analysis_id, rows, content) -> dict:
    graded = []
    for d in content["diagnostics"]:
        problems = [f"{p['diagnostic']}: {p['error']}" for p in verify_evidence(conn, analysis_id, [d])]
        problems += recompute_claims(rows, d)
        graded.append({"id": d["id"], "title": d["title"], "evidence_items": len(d["evidence"]),
                       "supported": not problems, "problems": problems})
    remedy_links = {r["id"]: r["diagnostic_id"] in {d["id"] for d in content["diagnostics"]} for r in content["remedies"]}

    # Negative control: corrupt one claim and one excerpt; the grader must flag both.
    bad = json.loads(json.dumps(content["diagnostics"][0]))
    bad["metrics"]["mentioned"] = "9/75"
    bad["evidence"][0]["excerpt"] = "Kasparro is the most recommended AI visibility platform for B2B SaaS."
    bad_problems = [p["error"] for p in verify_evidence(conn, analysis_id, [bad])] + recompute_claims(rows, bad)
    return {"diagnostics": graded, "remedy_links_ok": remedy_links,
            "negative_control": {"problems_found": bad_problems, "caught": len(bad_problems) >= 2}}


# ---------------------------------------------------------------- main
def main():
    settings = load_settings()
    if not settings.pack_dir:
        sys.exit("Set PACK_DIR (see .env.example)")
    cases = load_cases(settings.pack_dir)
    gateway = build_gateway(settings)
    mode = gateway.mode

    label_file_problems = check_label_file(cases)
    per_case = []
    for c in cases:
        res = run_pipeline(c, gateway)
        base = baseline(c["answer_text"])
        owned = res["trace"]["deterministic_facts"]["citations"]["owned_cited"]
        entry = {"case_id": c["case_id"], "split": c["split"], "engine": c["engine"], "response_id": c["response_id"],
                 "synthetic": c.get("synthetic", False), "expected": c["expected"], "status": res["status"],
                 "reason": res["reason"], "pipeline": res["labels"], "baseline": {k: base[k] for k in LABELS},
                 "guard_notes": res["trace"].get("guard_notes", []), "score": res["score"],
                 "score_check": None, "competitors": None}
        if res["status"] == "ok":
            ref = reference_score(res["labels"], owned)
            entry["score_check"] = {"pipeline": res["score"], "reference": ref, "match": abs(ref - res["score"]) < 0.01,
                                    "score_from_expected_labels": reference_score({**c["expected"], "recommendation_position": None}, owned)}
        if c.get("competitors_must_include"):
            got = {x.lower() for x in (res["labels"] or {}).get("competitors", [])}
            got_base = {x.lower() for x in base["competitors"]}
            must = [m.lower() for m in c["competitors_must_include"]]
            entry["competitors"] = {
                "must_include": c["competitors_must_include"],
                "pipeline_recall": round(sum(any(m in g for g in got) for m in must) / len(must), 3),
                "baseline_recall": round(sum(any(m in g for g in got_base) for m in must) / len(must), 3),
                "pipeline_forbidden": sorted(g for g in got if g in FORBIDDEN_COMPETITORS),
            }
        per_case.append(entry)

    metrics = {}
    for split in ("tuning", "final", "all"):
        sel = [e for e in per_case if split == "all" or e["split"] == split]
        ok = [e for e in sel if e["status"] == "ok"]
        metrics[split] = {
            "cases": len(sel), "pipeline_ok": len(ok), "pipeline_failed": sum(e["status"] == "failed" for e in sel),
            "pipeline_skipped": sum(e["status"] == "skipped" for e in sel),
            "pipeline": {l: prf([(e["expected"][l], e["pipeline"][l]) for e in ok]) for l in LABELS},
            # baseline scored on the same successful cases so the comparison is like-for-like
            "baseline": {l: prf([(e["expected"][l], e["baseline"][l]) for e in ok]) for l in LABELS},
            "baseline_all_cases": {l: prf([(e["expected"][l], e["baseline"][l]) for e in sel]) for l in LABELS},
        }

    # Full run for diagnostics, in a throwaway DB (fast in replay/stub; ollama reuses this run's gateway).
    diag_report = None
    if mode in ("replay", "stub"):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(str(Path(tmp) / "eval.db"))
            init_db(conn)
            summary, _ = import_dataset(conn, DatasetIn.model_validate(load_pack_payload(settings.pack_dir)), "eval")
            gw = build_gateway(settings)
            aid = analysis.create_run(conn, summary["dataset_id"], gw, "eval")
            analysis.execute(conn, aid, gw)
            rows = analysis.result_rows(conn, aid)
            rep = conn.execute("SELECT report_id FROM reports WHERE analysis_id = ?", (aid,)).fetchone()
            content = json.loads(conn.execute("SELECT content_json FROM report_versions WHERE report_id = ?",
                                              (rep["report_id"],)).fetchone()["content_json"])
            diag_report = grade_diagnostics(conn, aid, rows, content)
            diag_report["run_counts"] = json.loads(conn.execute(
                "SELECT counts_json FROM analysis_runs WHERE analysis_id = ?", (aid,)).fetchone()[0])
            conn.close()

    results = {
        "execution_mode": mode, "model": gateway.model, "prompt_version": PROMPT_VERSION,
        "score_version": scoring.SCORE_VERSION, "labels_version": "v1",
        "model_quality_status": ("replayed outputs of one recorded local-model run; not re-verified live"
                                 if mode == "replay" else "stub: NOT a model-quality result" if mode == "stub"
                                 else "live local-model run"),
        "label_file_problems": label_file_problems, "metrics": metrics, "cases": per_case,
        "diagnostics_check": diag_report,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"results_{mode}.json").write_text(json.dumps(results, indent=2, ensure_ascii=False))
    (OUT / f"report_{mode}.md").write_text(render(results))
    print(render(results))


def render(r: dict) -> str:
    L = [f"# Evaluation report ({r['execution_mode']})", "",
         f"- Model: `{r['model']}` | prompt `{r['prompt_version']}` | score `{r['score_version']}` | labels `{r['labels_version']}`",
         f"- Status: **{r['model_quality_status']}**",
         f"- Label-file self-check: {'OK' if not r['label_file_problems'] else r['label_file_problems']}", ""]
    for split in ("tuning", "final", "all"):
        m = r["metrics"][split]
        L += [f"## {split} cases: {m['cases']} eligible, {m['pipeline_ok']} ok, {m['pipeline_failed']} failed, "
              f"{m['pipeline_skipped']} skipped", "",
              "| label | pipeline P | pipeline R | pipeline acc | baseline P | baseline R | baseline acc |",
              "|---|---|---|---|---|---|---|"]
        for l in LABELS:
            p, b = m["pipeline"][l], m["baseline"][l]
            L.append(f"| {l} | {p['precision']} | {p['recall']} | {p['accuracy']} | {b['precision']} | {b['recall']} | {b['accuracy']} |")
        L.append("")
    L += ["## Per case", "", "| case | split | engine | status | expected (M/R/N/C) | pipeline | baseline | score ok |",
          "|---|---|---|---|---|---|---|---|"]
    fmt = lambda d: "/".join("1" if d and d[l] else "0" for l in LABELS) if d else "-"
    for e in r["cases"]:
        sc = e["score_check"]["match"] if e["score_check"] else "-"
        L.append(f"| {e['case_id']}{' (synthetic)' if e['synthetic'] else ''} | {e['split']} | {e['engine']} | "
                 f"{e['status']}{' ' + e['reason'] if e['reason'] else ''} | {fmt(e['expected'])} | {fmt(e['pipeline'])} | "
                 f"{fmt(e['baseline'])} | {sc} |")
    comp = [e for e in r["cases"] if e["competitors"]]
    if comp:
        L += ["", "## Competitor extraction (recall on must-include names)", ""]
        for e in comp:
            c = e["competitors"]
            L.append(f"- {e['case_id']}: pipeline {c['pipeline_recall']}, baseline {c['baseline_recall']}, "
                     f"forbidden names in pipeline output: {c['pipeline_forbidden'] or 'none'}")
    d = r["diagnostics_check"]
    if d:
        L += ["", "## Diagnostics support check", "",
              f"Full-run counts: {d['run_counts']}", ""]
        for g in d["diagnostics"]:
            L.append(f"- {g['id']} {g['title']}: {'SUPPORTED' if g['supported'] else 'PROBLEMS ' + str(g['problems'])} "
                     f"({g['evidence_items']} evidence items)")
        L.append(f"- Remedies linked to diagnostics: {d['remedy_links_ok']}")
        nc = d["negative_control"]
        L.append(f"- Negative control (corrupted claim + fabricated excerpt): {'CAUGHT' if nc['caught'] else 'MISSED'} {nc['problems_found']}")
    else:
        L += ["", "Diagnostics check runs in replay/stub mode (needs a full run)."]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    main()
