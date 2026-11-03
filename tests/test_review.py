"""Human review controls export: roles, rejection, edit-after-approval, evidence checks."""
from tests.conftest import ANALYST, REVIEWER, run_analysis


def setup_report(client):
    a = run_analysis(client)
    rid = a["report_id"]
    rep = client.get(f"/reports/{rid}", headers=ANALYST).json()
    assert rep["status"] == "draft" and rep["current_version"] == 1
    return rid, rep


def submit(client, rid, version):
    r = client.post(f"/reports/{rid}/submit", json={"version": version}, headers=ANALYST)
    assert r.status_code == 200, r.text
    return r.json()


def decide(client, rid, version, decision, headers=REVIEWER, comment="Checked evidence against responses."):
    return client.post(f"/reports/{rid}/decision", json={"version": version, "decision": decision, "comment": comment},
                       headers=headers)


def test_report_has_three_diagnostics_with_evidence(client):
    _, rep = setup_report(client)
    diags = rep["content"]["diagnostics"]
    assert [d["id"] for d in diags] == ["D1", "D2", "D3"]
    assert all(d["evidence"] for d in diags)
    assert len(rep["content"]["remedies"]) == 3


def test_wrong_role_cannot_decide_or_edit(client):
    rid, _ = setup_report(client)
    submit(client, rid, 1)
    r = decide(client, rid, 1, "approve", headers=ANALYST)
    assert r.status_code == 403 and r.json()["error"]["code"] == "wrong_role"
    assert client.patch(f"/reports/{rid}", json={"notes": "x"}, headers=REVIEWER).status_code == 403
    assert decide(client, rid, 1, "approve", headers={"Authorization": "Bearer forged"}).status_code == 401


def test_caller_supplied_role_is_not_authority(client):
    rid, _ = setup_report(client)
    submit(client, rid, 1)
    r = client.post(f"/reports/{rid}/decision", headers=ANALYST,
                    json={"version": 1, "decision": "approve", "comment": "ok ok", "role": "reviewer"})
    assert r.status_code == 422  # unknown field rejected before anything else
    r = client.patch(f"/reports/{rid}", json={"status": "approved"}, headers=ANALYST)
    assert r.status_code == 422


def test_unapproved_cannot_be_exported(client):
    rid, _ = setup_report(client)
    assert client.get(f"/reports/{rid}/export", headers=ANALYST).json()["error"]["code"] == "not_approved"
    submit(client, rid, 1)
    r = client.get(f"/reports/{rid}/export", headers=ANALYST)
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_approved"


def test_rejection_blocks_export(client):
    rid, _ = setup_report(client)
    submit(client, rid, 1)
    assert decide(client, rid, 1, "reject", comment="D3 overstates the evidence").status_code == 200
    r = client.get(f"/reports/{rid}/export", headers=ANALYST)
    assert r.status_code == 409 and r.json()["error"]["code"] == "rejected"
    rep = client.get(f"/reports/{rid}", headers=ANALYST).json()
    d = rep["versions"][0]["decisions"][0]
    assert (d["decision"], d["reviewer"], d["comment"]) == ("rejected", "reviewer@test", "D3 overstates the evidence")
    assert decide(client, rid, 1, "approve").status_code == 409  # cannot flip a decided version


def test_approve_then_export(client):
    rid, _ = setup_report(client)
    submit(client, rid, 1)
    assert decide(client, rid, 1, "approve").status_code == 200
    r = client.get(f"/reports/{rid}/export", headers=REVIEWER)
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == 1 and body["is_latest"] is True and body["execution_mode"] == "stub"
    assert body["approval"]["reviewer"] == "reviewer@test"


def test_edit_after_approval_requires_new_review(client):
    rid, _ = setup_report(client)
    submit(client, rid, 1)
    decide(client, rid, 1, "approve")
    r = client.patch(f"/reports/{rid}", json={"notes": "Added context after approval"}, headers=ANALYST)
    assert r.status_code == 200 and r.json()["new_version_created"] is True
    assert r.json()["current_version"] == 2 and r.json()["status"] == "draft"
    # the old approval does not cover the new draft
    e = client.get(f"/reports/{rid}/export", headers=ANALYST)
    assert e.status_code == 409 and e.json()["error"]["code"] == "stale_approval"
    # the old approved version stays exportable only when named explicitly, and says it is not the latest
    old = client.get(f"/reports/{rid}/export?version=1", headers=ANALYST).json()
    assert old["version"] == 1 and old["is_latest"] is False and old["content"]["notes"] == ""
    # new review cycle for v2
    submit(client, rid, 2)
    decide(client, rid, 2, "approve")
    new = client.get(f"/reports/{rid}/export", headers=ANALYST).json()
    assert new["version"] == 2 and new["content"]["notes"] == "Added context after approval"


def test_submitted_version_is_frozen(client):
    rid, _ = setup_report(client)
    submitted = submit(client, rid, 1)
    frozen_hash = submitted["versions"][0]["content_hash"]
    r = client.patch(f"/reports/{rid}", json={"notes": "late edit"}, headers=ANALYST).json()
    assert r["current_version"] == 2
    v1 = client.get(f"/reports/{rid}?version=1", headers=ANALYST).json()
    assert v1["content"]["notes"] == "" and v1["versions"][0]["content_hash"] == frozen_hash
    # reviewer can still decide v1 exactly as submitted, but it is then stale for default export
    decide(client, rid, 1, "approve")
    assert client.get(f"/reports/{rid}/export", headers=ANALYST).json()["error"]["code"] == "stale_approval"


def test_unsupported_evidence_edit_is_rejected(client):
    rid, rep = setup_report(client)
    diags = rep["content"]["diagnostics"]
    diags[0]["evidence"][0]["excerpt"] = "Kasparro is the top recommended tool on every engine."
    r = client.patch(f"/reports/{rid}", json={"diagnostics": diags}, headers=ANALYST)
    assert r.status_code == 422
    assert r.json()["error"]["details"][0]["error"] == "excerpt_not_in_response"
    diags[0]["evidence"][0]["response_id"] = "not-a-response"
    r = client.patch(f"/reports/{rid}", json={"diagnostics": diags}, headers=ANALYST)
    assert r.json()["error"]["details"][0]["error"] == "unknown_response_id"


def test_cannot_submit_stale_version_number(client):
    rid, _ = setup_report(client)
    r = client.post(f"/reports/{rid}/submit", json={"version": 7}, headers=ANALYST)
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_current_version"


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_demo_login_is_off_by_default(client):
    assert client.get("/demo/login").json() == {"enabled": False}
    r = client.post("/demo/login", json={"role": "reviewer"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "demo_login_disabled"


def test_demo_login_still_enforces_roles_through_tokens(tmp_path):
    from fastapi.testclient import TestClient
    from app.api import create_app
    from app.config import Identity, Settings
    settings = Settings(db_path=str(tmp_path / "d.db"), model_mode="stub", demo_login=True, tokens={
        "a-tok": Identity("analyst@test", "analyst"), "r-tok": Identity("reviewer@test", "reviewer")})
    with TestClient(create_app(settings)) as c:
        tok = c.post("/demo/login", json={"role": "analyst"}).json()["token"]
        assert c.get("/whoami", headers={"Authorization": f"Bearer {tok}"}).json()["role"] == "analyst"
        assert c.post("/demo/login", json={"role": "admin"}).status_code == 404
