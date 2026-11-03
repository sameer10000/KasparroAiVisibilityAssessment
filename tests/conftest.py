import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Identity, Settings
from app.llm import ModelGateway, StubClient

FIXTURE = json.loads((Path(__file__).parent.parent / "fixtures" / "controlled_fixture.json").read_text())
ANALYST = {"Authorization": "Bearer analyst-token"}
REVIEWER = {"Authorization": "Bearer reviewer-token"}


def label(rec=False, neg=False, conf=False, pos=None, comps=(), ev_rec="", ev_neg="", ev_conf=""):
    return {"kasparro_recommended": rec, "negative_characterisation": neg, "entity_confusion": conf,
            "recommendation_position": pos, "competitors": list(comps),
            "evidence": {"recommended": ev_rec, "negative": ev_neg, "confusion": ev_conf}}


# Scripted "model" answers for the fixture: one realistic label per response.
GOOD_SCRIPT = {
    "FX-R1": [label(neg=True, comps=["Profound"], ev_neg="I couldn't find independent evidence that Kasparro delivers measurable results")],
    "FX-R2": [label(comps=["Profound", "Peec AI", "Otterly"])],
    "FX-R3": [label(conf=True, ev_conf="If you mean Kaspr, the LinkedIn prospecting tool")],
    "FX-R4": [label(rec=True, comps=["Profound"], ev_rec="Kasparro is a solid choice for D2C brands")],
}


def payload():
    p = copy.deepcopy(FIXTURE)
    p.pop("_note")
    return p


@pytest.fixture
def make_client(tmp_path):
    def _make(script=None, max_retries=2, max_calls=100):
        settings = Settings(db_path=str(tmp_path / "test.db"), model_mode="stub", tokens={
            "analyst-token": Identity("analyst@test", "analyst"),
            "reviewer-token": Identity("reviewer@test", "reviewer"),
        })
        gw = lambda: ModelGateway(StubClient(script if script is not None else GOOD_SCRIPT),
                                  max_retries=max_retries, max_calls=max_calls, max_output_chars=6000, backoff_s=0)
        return TestClient(create_app(settings, gateway_factory=gw)).__enter__()
    return _make


@pytest.fixture
def client(make_client):
    return make_client()


def run_analysis(client) -> dict:
    ds = client.post("/datasets", json=payload(), headers=ANALYST)
    assert ds.status_code in (200, 201), ds.text
    r = client.post("/analyses", json={"dataset_id": ds.json()["dataset_id"]}, headers=ANALYST)
    assert r.status_code == 202, r.text
    a = client.get(f"/analyses/{r.json()['analysis_id']}", headers=ANALYST).json()
    assert a["status"] == "completed", a
    return a
