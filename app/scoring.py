"""Transparent visibility score (score-v1) and aggregation. Pure functions, no model calls.

Per-response score, 0-100, computed only for successfully analysed responses:
    inclusion        25 x [Kasparro mentioned]
    clarity          15 x [mentioned and not confused with another product]
    recommendation   40 x [presented as a suitable option]
    position         10 x (1 / position) when recommended in an explicit list, else 0
    owned_citation   10 x [a kasparro.com URL is among the response's citations]
    negative        -30 x [unfavourable characterisation]
    score = clamp(sum, 0, 100)
Failed or skipped responses get no score (None), are excluded from rate denominators and are
reported as separate counts. Branded and unbranded prompts are always aggregated separately.
"""
import math

SCORE_VERSION = "score-v1"
WEIGHTS = {"inclusion": 25, "clarity": 15, "recommendation": 40, "position": 10, "owned_citation": 10, "negative": -30}


def components(labels: dict, owned_cited: bool) -> dict:
    pos = labels.get("recommendation_position")
    return {
        "inclusion": 1.0 if labels["mentioned"] else 0.0,
        "clarity": 1.0 if labels["mentioned"] and not labels["entity_confusion"] else 0.0,
        "recommendation": 1.0 if labels["recommended"] else 0.0,
        "position": round(1.0 / pos, 4) if labels["recommended"] and pos else 0.0,
        "owned_citation": 1.0 if owned_cited else 0.0,
        "negative": 1.0 if labels["negative"] else 0.0,
    }


def score(comp: dict) -> float:
    total = sum(WEIGHTS[k] * v for k, v in comp.items())
    return round(min(100.0, max(0.0, total)), 2)


def wilson(k: int, n: int, z: float = 1.96) -> list[float] | None:
    """95% Wilson interval for a proportion. With one answer per prompt and engine this captures
    sampling over prompts only, not run-to-run model variability."""
    if n == 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)]


RATE_LABELS = ("mentioned", "recommended", "negative", "entity_confusion")


def summarise(rows: list[dict]) -> dict:
    """rows: dicts with status, labels, components, score."""
    ok = [r for r in rows if r["status"] == "ok"]
    out = {
        "eligible": len(rows),
        "ok": len(ok),
        "failed": sum(r["status"] == "failed" for r in rows),
        "skipped": sum(r["status"] == "skipped" for r in rows),
        "coverage": f"{len(ok)}/{len(rows)}",
    }
    n = len(ok)
    for label in RATE_LABELS:
        k = sum(1 for r in ok if r["labels"][label])
        out[label] = {"count": k, "of": n, "rate": round(k / n, 3) if n else None, "ci95": wilson(k, n)}
    k = sum(1 for r in ok if r["components"]["owned_citation"])
    out["owned_citation"] = {"count": k, "of": n, "rate": round(k / n, 3) if n else None, "ci95": wilson(k, n)}
    out["mean_score"] = round(sum(r["score"] for r in ok) / n, 2) if n else None
    return out


def aggregate(rows: list[dict]) -> dict:
    """rows additionally carry engine, prompt_id, brand_scope and intent."""
    def group(keyfn):
        groups: dict[str, list] = {}
        for r in rows:
            groups.setdefault(keyfn(r), []).append(r)
        return {k: summarise(v) for k, v in sorted(groups.items())}

    by_prompt: dict[str, dict] = {}
    for r in rows:
        by_prompt.setdefault(r["prompt_id"], {})[r["engine"]] = r
    paired = {"prompts_with_all_engines_ok": 0, "mentioned_by_n_engines": {"0": 0, "1": 0, "2": 0, "3": 0},
              "recommended_by_n_engines": {"0": 0, "1": 0, "2": 0, "3": 0}}
    for engines in by_prompt.values():
        if len(engines) == 3 and all(e["status"] == "ok" for e in engines.values()):
            paired["prompts_with_all_engines_ok"] += 1
            paired["mentioned_by_n_engines"][str(sum(e["labels"]["mentioned"] for e in engines.values()))] += 1
            paired["recommended_by_n_engines"][str(sum(e["labels"]["recommended"] for e in engines.values()))] += 1

    return {
        "score_version": SCORE_VERSION,
        "weights": WEIGHTS,
        "overall": summarise(rows),
        "by_scope": group(lambda r: r["brand_scope"]),
        "by_scope_engine": group(lambda r: f"{r['brand_scope']}|{r['engine']}"),
        "by_scope_intent": group(lambda r: f"{r['brand_scope']}|{r['intent']}"),
        "same_prompt_comparison": paired,
    }
