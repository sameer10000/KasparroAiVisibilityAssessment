"""Builds the draft report: three diagnostics and three prioritised remedies.

Diagnostics are produced by deterministic detectors over validated per-response results, so every
number is recomputable and every excerpt is a verbatim quote from a named response. Each diagnostic
separates the recorded pattern, a possible explanation and the evidence that would test it.
The analyst can then edit the wording through PATCH /reports/{id}; edits are re-verified.
"""
import re
import sqlite3
from collections import Counter

from .extract import KASPR_RE, prose_view, quote_in, sentence_with

ENGINES = ("aimode", "chatgpt", "gemini")
JOB_BOARD_HINTS = ("bebee.com", "getmereferred.com", "iimjobs.com", "/jobs/", "job-listing", "naukri", "indeed")


def _frac(rows, pred):
    ok = [r for r in rows if r["status"] == "ok"]
    return sum(1 for r in ok if pred(r)), len(ok)


def _evidence_item(conn, r, excerpt, note=""):
    return {"response_id": r["response_id"], "prompt_id": r["prompt_id"], "engine": r["engine"],
            "excerpt": excerpt, **({"note": note} if note else {})}


def _answer(conn, response_id) -> str:
    row = conn.execute("SELECT answer_markdown, answer_text FROM responses WHERE response_id = ?",
                       (response_id,)).fetchone()
    return prose_view(row["answer_markdown"], row["answer_text"]) if row else ""


def _per_engine(rows, pred) -> dict:
    out = {}
    for e in ENGINES:
        k, n = _frac([r for r in rows if r["engine"] == e], pred)
        out[e] = f"{k}/{n}"
    return out


def unbranded_invisibility(conn, rows) -> dict | None:
    ub = [r for r in rows if r["brand_scope"] == "unbranded"]
    k, n = _frac(ub, lambda r: r["labels"]["mentioned"])
    if n == 0:
        return None
    # Exact counts use the deterministic dictionary; the model's free-form competitor list varies in
    # spelling ("Peec" / "Peec AI") and is kept per response rather than counted here.
    comp_counts = Counter(c for r in ub if r["status"] == "ok"
                          for c in r["trace"]["deterministic_facts"]["dictionary_competitors"])
    top = [name for name, _ in comp_counts.most_common(6)]
    evidence = []
    for engine in ENGINES:
        for r in ub:
            if r["status"] != "ok" or r["engine"] != engine or r["labels"]["mentioned"]:
                continue
            names = [c for c in top if c in r["trace"]["deterministic_facts"]["dictionary_competitors"]]
            if names:
                ex = sentence_with(_answer(conn, r["response_id"]), re.compile(re.escape(names[0]), re.I))
                if ex:
                    evidence.append(_evidence_item(conn, r, ex, f"names {names[0]}; Kasparro absent"))
                    break
    domains = conn.execute(
        "SELECT domain, COUNT(DISTINCT response_id) AS n FROM citations WHERE response_id IN ({}) "
        "GROUP BY domain ORDER BY n DESC, domain LIMIT 6".format(",".join("?" * len(ub))),
        [r["response_id"] for r in ub]).fetchall()
    return {
        "id": "D1",
        "title": "Kasparro is absent from unbranded discovery answers",
        "kind": "loss",
        "pattern": (f"Kasparro is named in {k}/{n} successfully analysed answers to the 25 unbranded questions "
                    f"(per engine: {_per_engine(ub, lambda r: r['labels']['mentioned'])}). These are the questions a "
                    f"buyer asks before knowing any vendor. Other tools are named instead; most frequent: "
                    f"{', '.join(f'{c} ({comp_counts[c]} answers)' for c in top)}."),
        "possible_explanation": ("Engines assemble these answers from category listicles, reviews and vendor blogs. "
                                 "If no retrieved source discusses Kasparro, it cannot be included. Most-cited domains in "
                                 f"unbranded answers: {', '.join(f'{d[0]} ({d[1]} answers)' for d in domains)}."),
        "test_evidence": ("Open the top cited pages for the unbranded prompts and record whether any mentions Kasparro; "
                          "if none do, the explanation is supported. Re-run the 25 unbranded prompts on a later date "
                          "to separate a persistent gap from single-run noise."),
        "metrics": {"mentioned": f"{k}/{n}", "by_engine": _per_engine(ub, lambda r: r["labels"]["mentioned"]),
                    "top_competitors_unbranded": dict(comp_counts.most_common(10))},
        "evidence": evidence,
    }


def entity_confusion(conn, rows) -> dict | None:
    br = [r for r in rows if r["brand_scope"] == "branded"]
    k, n = _frac(br, lambda r: r["labels"]["entity_confusion"])
    kaspr_k, _ = _frac(br, lambda r: r["trace"]["deterministic_facts"]["kaspr_named"])
    if n == 0 or (k == 0 and kaspr_k == 0):
        return None
    evidence = []
    # Evidence: answers the model labelled as confused (with its verified quote), then branded answers that
    # discuss Kaspr and never name Kasparro (a deterministic, unambiguous case). An answer that merely
    # mentions Kaspr to disambiguate is not evidence of confusion.
    swapped = [r for r in br if r["status"] == "ok" and r["trace"]["deterministic_facts"]["kaspr_named"]
               and not r["labels"]["mentioned"]]
    for r in br:
        if r["status"] == "ok" and r["labels"]["entity_confusion"] and (r["evidence"] or {}).get("confusion"):
            evidence.append(_evidence_item(conn, r, r["evidence"]["confusion"], "model-labelled confusion"))
    for r in swapped:
        if r["response_id"] not in {e["response_id"] for e in evidence}:
            evidence.append(_evidence_item(conn, r, sentence_with(_answer(conn, r["response_id"]), KASPR_RE),
                                           "answer is about Kaspr; Kasparro never named"))
    evidence = evidence[:6]
    return {
        "id": "D2",
        "title": "Branded answers confuse Kasparro with Kaspr or cannot identify it",
        "kind": "loss",
        "pattern": (f"In {k}/{n} branded answers the model labelled the answer as treating Kasparro as another product "
                    f"or as unidentifiable (per engine: {_per_engine(br, lambda r: r['labels']['entity_confusion'])}). "
                    f"Deterministic cross-check: {len(swapped)}/{n} branded answers discuss Kaspr and never name "
                    f"Kasparro; {kaspr_k}/{n} contain the word 'Kaspr' at all (some only to disambiguate)."),
        "possible_explanation": ("'Kaspr' (a LinkedIn prospecting tool with extensive help pages) is a near-identical, "
                                 "better-documented name, so retrieval may resolve 'kasparro' to it, especially for "
                                 "commercial questions such as pricing, cancellation and self-serve."),
        "test_evidence": ("Check whether confused answers cite help.kaspr.io or kaspr.io pages and whether they cluster in "
                          "pricing/onboarding prompts; re-ask the same prompts with 'Kasparro (kasparro.com)' to see "
                          "if disambiguation alone removes the confusion."),
        "metrics": {"entity_confusion": f"{k}/{n}", "kaspr_only_no_kasparro": f"{len(swapped)}/{n}",
                    "kaspr_word": f"{kaspr_k}/{n}",
                    "by_engine": _per_engine(br, lambda r: r["labels"]["entity_confusion"])},
        "evidence": evidence,
    }


def credibility_gap(conn, rows) -> dict | None:
    br = [r for r in rows if r["brand_scope"] == "branded"]
    neg_k, n = _frac(br, lambda r: r["labels"]["negative"])
    rec_k, _ = _frac(br, lambda r: r["labels"]["recommended"])
    if n == 0:
        return None
    candidates = [r for r in br if r["status"] == "ok" and r["labels"]["negative"] and (r["evidence"] or {}).get("negative")]
    # quotes that name Kasparro first: they show the judgement is about Kasparro, not the category
    candidates.sort(key=lambda r: "kasparro" not in r["evidence"]["negative"].lower())
    evidence = [_evidence_item(conn, r, r["evidence"]["negative"]) for r in candidates][:5]
    urls = [u for r in br if r["status"] == "ok"
            for u in r["trace"]["deterministic_facts"]["citations"]["third_party_kasparro_urls"]
            + r["trace"]["deterministic_facts"]["citations"]["owned_urls"]]
    job = sum(1 for u in urls if any(h in u for h in JOB_BOARD_HINTS))
    owned = sum(1 for u in urls if "kasparro.com" in u)
    owned_k, _ = _frac(br, lambda r: r["components"]["owned_citation"] > 0)
    return {
        "id": "D3",
        "title": "When asked about Kasparro directly, engines rarely recommend it and often doubt it",
        "kind": "loss",
        "pattern": (f"Of {n} successfully analysed branded answers, {rec_k}/{n} present Kasparro as a suitable option and "
                    f"{neg_k}/{n} characterise it unfavourably (per engine negative: {_per_engine(br, lambda r: r['labels']['negative'])}). "
                    f"Only {owned_k}/{n} branded answers cite kasparro.com. Of {len(urls)} unique Kasparro-related "
                    f"URLs cited in branded answers, {job} are job listings and {owned} are kasparro.com pages."),
        "possible_explanation": ("Engines find little independent material about Kasparro (reviews, case studies, "
                                 "pricing, comparisons), so they fall back on job posts and hedge with 'no independent "
                                 "evidence'. This is a hypothesis; the run does not show what the engines retrieved but "
                                 "did not cite."),
        "test_evidence": ("Inspect the cited Kasparro-related pages and record which claims each supports; list the "
                          "specific facts answers say are missing (price, onboarding time, results) and check whether "
                          "kasparro.com states them in crawlable text."),
        "metrics": {"recommended": f"{rec_k}/{n}", "negative": f"{neg_k}/{n}", "owned_citation": f"{owned_k}/{n}",
                    "kasparro_related_urls": len(urls), "job_listing_urls": job, "kasparro_com_urls": owned},
        "evidence": evidence,
    }


REMEDIES = {
    "D1": {
        "problem": "Kasparro does not enter the consideration set for vendor-neutral discovery questions.",
        "action": ("Investigate first: audit the pages engines cite for the unbranded prompts and identify which "
                   "independent lists, reviews and comparisons Kasparro could legitimately be covered in; then pursue "
                   "inclusion in those sources with accurate, verifiable information."),
        "expected_benefit": "Creates the sources engines would need before Kasparro can appear in discovery answers.",
        "effort": "Medium to high (outreach and content over weeks; audit itself about 1 day).",
        "uncertainty": "High. Inclusion is not guaranteed and engines differ in retrieval; one snapshot cannot show cause.",
        "validation": ("Re-run the same 25 unbranded prompts on all three engines (same location/language settings) "
                       "and track unbranded mention rate against the 0-baseline, plus the count of cited pages that "
                       "mention Kasparro."),
    },
    "D2": {
        "problem": "Engines resolve 'Kasparro' to Kaspr or fail to identify it, so even branded answers can be about the wrong company.",
        "action": ("Make the entity unambiguous: consistent name ('Kasparro', domain kasparro.com) across the site, "
                   "Organization structured data with sameAs links to official profiles, and an About page stating "
                   "what Kasparro is in plain text. Align naming on LinkedIn and job posts ('Kasparro AI' vs 'Kasparro')."),
        "expected_benefit": "Fewer answers about the wrong product on commercial questions (pricing, cancellation, signup).",
        "effort": "Low (days).",
        "uncertainty": "Medium. Name similarity may persist in retrieval; Google guidance says structured data must match visible content and guarantees nothing.",
        "validation": "Re-run the 25 branded prompts; compare entity-confusion count and the deterministic 'Kaspr' count with this run.",
    },
    "D3": {
        "problem": "Direct questions get hedged or unfavourable answers backed mostly by job listings.",
        "action": ("Publish verifiable facts buyers ask about (pricing, diagnostic scope, onboarding time, cancellation "
                   "terms, methodology) as crawlable text on kasparro.com, and only cite outcomes that have evidence. "
                   "Check robots/crawler access for OAI-SearchBot and Googlebot on those pages."),
        "expected_benefit": "Engines can cite first-party facts instead of job posts or 'no information found'.",
        "effort": "Low to medium.",
        "uncertainty": "Medium. First-party pages establish what Kasparro says, not independent proof; doubts about results may remain.",
        "validation": ("Re-run branded prompts; track negative rate, owned-citation rate and the share of Kasparro-related "
                       "citations that are kasparro.com pages rather than job listings."),
    },
}

PRIORITY_RATIONALE = {
    "D1": "Largest audience: unbranded questions are where new buyers form shortlists, and the gap is total.",
    "D2": "Cheapest to fix and it corrupts branded answers on commercial questions.",
    "D3": "Affects buyers already considering Kasparro; depends partly on evidence Kasparro may not have yet.",
}


def build_report_content(conn: sqlite3.Connection, analysis_id: str, rows: list[dict], aggregates: dict, config: dict) -> dict:
    diagnostics, omitted = [], []
    for d in (unbranded_invisibility(conn, rows), entity_confusion(conn, rows), credibility_gap(conn, rows)):
        if d and d["evidence"]:
            diagnostics.append(d)
        elif d:
            omitted.append({"id": d["id"], "title": d["title"], "reason": "no verbatim supporting evidence in results"})
    remedies = []
    for i, d in enumerate(diagnostics, start=1):
        remedies.append({"id": f"R{i}", "priority": i, "diagnostic_id": d["id"],
                         "priority_rationale": PRIORITY_RATIONALE[d["id"]],
                         "supporting_evidence": f"See {d['id']}: {d['metrics']}", **REMEDIES[d["id"]]})
    return {
        "title": "Kasparro AI visibility snapshot (captured 2026-07-31, India, English)",
        "analysis_id": analysis_id,
        "execution_mode": config["execution_mode"],
        "config": config,
        "scores": {k: aggregates[k] for k in ("score_version", "weights", "overall", "by_scope", "by_scope_engine")},
        "diagnostics": diagnostics,
        "remedies": remedies,
        "omitted_diagnostics": omitted,
        "notes": "",
        "limitations": [
            "One captured answer per prompt and engine: a snapshot, not a stable ranking.",
            "Citation support is unverified: linked pages were not opened.",
            "Labels come from a small local model plus verbatim-quote guards; see evaluation results.",
        ],
    }


def verify_evidence(conn: sqlite3.Connection, analysis_id: str, diagnostics: list[dict]) -> list[dict]:
    """Every evidence item must name a response in this analysis and quote it verbatim."""
    problems = []
    valid_ids = {r[0] for r in conn.execute(
        "SELECT response_id FROM response_results WHERE analysis_id = ?", (analysis_id,))}
    for d in diagnostics:
        for i, ev in enumerate(d.get("evidence", [])):
            rid = ev.get("response_id")
            if rid not in valid_ids:
                problems.append({"diagnostic": d.get("id"), "evidence_index": i, "error": "unknown_response_id"})
            elif not quote_in(ev.get("excerpt", ""), _answer(conn, rid)):
                problems.append({"diagnostic": d.get("id"), "evidence_index": i, "error": "excerpt_not_in_response",
                                 "response_id": rid})
        if not d.get("evidence"):
            problems.append({"diagnostic": d.get("id"), "error": "no_evidence"})
    return problems
