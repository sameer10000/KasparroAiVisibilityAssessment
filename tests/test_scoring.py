"""Deterministic score arithmetic, independent of any model."""
from app import scoring

BASE = {"mentioned": True, "recommended": False, "negative": False, "entity_confusion": False,
        "recommendation_position": None}


def test_weights_sum_to_100_at_best_case():
    best = dict(BASE, recommended=True, recommendation_position=1)
    assert scoring.score(scoring.components(best, owned_cited=True)) == 100.0


def test_component_examples():
    assert scoring.score(scoring.components(dict(BASE, mentioned=False), False)) == 0.0
    assert scoring.score(scoring.components(BASE, False)) == 40.0                       # 25 + 15
    assert scoring.score(scoring.components(dict(BASE, entity_confusion=True), False)) == 25.0
    assert scoring.score(scoring.components(dict(BASE, negative=True), True)) == 20.0  # 25+15+10-30
    third = dict(BASE, recommended=True, recommendation_position=3)
    assert scoring.score(scoring.components(third, False)) == 83.33                    # 25+15+40+10/3


def test_score_is_clamped_at_zero():
    comp = scoring.components(dict(BASE, entity_confusion=True, negative=True), False)  # 25 - 30
    assert scoring.score(comp) == 0.0


def test_position_ignored_without_recommendation():
    assert scoring.components(dict(BASE, recommendation_position=2), False)["position"] == 0.0


def test_failed_rows_excluded_from_denominators():
    ok = {"status": "ok", "labels": BASE, "components": scoring.components(BASE, False), "score": 40.0}
    failed = {"status": "failed", "labels": None, "components": None, "score": None}
    s = scoring.summarise([ok, failed])
    assert (s["eligible"], s["ok"], s["failed"]) == (2, 1, 1)
    assert s["mentioned"] == {"count": 1, "of": 1, "rate": 1.0, "ci95": scoring.wilson(1, 1)}


def test_wilson_bounds():
    assert scoring.wilson(0, 75) == [0.0, 0.049]
    assert scoring.wilson(0, 0) is None
