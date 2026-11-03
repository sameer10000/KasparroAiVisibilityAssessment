"""Invalid input and duplicate-response handling."""
import copy

from tests.conftest import ANALYST, REVIEWER, payload


def test_valid_import_counts(client):
    r = client.post("/datasets", json=payload(), headers=ANALYST)
    assert r.status_code == 201
    c = r.json()["counts"]
    assert c["responses"] == {"received": 4, "inserted": 4, "reused": 0, "conflict": 0, "invalid": 0}
    assert c["citations"]["inserted"] == 2


def test_malformed_request_is_422(client):
    r = client.post("/datasets", json={"run": {"run_id": "x"}, "prompts": []}, headers=ANALYST)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"


def test_bad_record_is_reported_and_others_still_import(client):
    p = payload()
    p["responses"][0]["engine"] = "bing"          # not an allowed engine
    del p["responses"][1]["answer_text"]          # required field missing
    p["responses"][2]["prompt_id"] = "fixture:999"  # unknown prompt
    r = client.post("/datasets", json=p, headers=ANALYST)
    assert r.status_code == 201
    body = r.json()
    assert body["counts"]["responses"]["invalid"] == 3
    assert body["response_count"] == 1
    assert {e["error"] for e in body["errors"]} == {"invalid"}


def test_all_records_invalid_is_rejected(client):
    p = payload()
    for resp in p["responses"]:
        resp["answer_text"] = ""
    r = client.post("/datasets", json=p, headers=ANALYST)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "no_valid_responses"


def test_identical_reimport_reuses_dataset(client):
    first = client.post("/datasets", json=payload(), headers=ANALYST).json()
    again = client.post("/datasets", json=payload(), headers=ANALYST)
    assert again.status_code == 200
    assert again.json()["dataset_id"] == first["dataset_id"]
    assert again.json()["reused_existing_dataset"] is True


def test_same_response_in_new_payload_is_reused_not_duplicated(client):
    client.post("/datasets", json=payload(), headers=ANALYST)
    p = payload()
    p["responses"].append(copy.deepcopy(p["responses"][0]))  # duplicate inside the payload too
    p["citations"] = []                                     # makes the payload different overall
    r = client.post("/datasets", json=p, headers=ANALYST).json()
    assert r["counts"]["responses"]["inserted"] == 0
    assert r["counts"]["responses"]["reused"] == 5
    assert r["response_count"] == 4


def test_changed_content_under_same_id_is_a_conflict(client):
    client.post("/datasets", json=payload(), headers=ANALYST)
    p = payload()
    p["responses"][0]["answer_text"] = "tampered text"
    p["citations"] = []
    r = client.post("/datasets", json=p, headers=ANALYST).json()
    assert r["counts"]["responses"]["conflict"] == 1
    assert r["response_count"] == 3
    stored = client.get(f"/datasets/{r['dataset_id']}", headers=ANALYST).json()
    assert any(e["error"] == "conflict" and e["id"] == "FX-R1" for e in stored["errors"])


def test_import_requires_analyst(client):
    assert client.post("/datasets", json=payload()).status_code == 401
    assert client.post("/datasets", json=payload(), headers=REVIEWER).status_code == 403
