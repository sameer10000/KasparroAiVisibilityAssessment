"""End-to-end demo against a running server:
import -> analyse -> edit -> submit -> wrong role -> reject -> edit -> submit -> approve -> export
-> edit after approval -> stale export refused -> explicit old-version export.

    uvicorn app.api:app --port 8000      # in one terminal
    python scripts/demo_flow.py          # in another (reads PACK_DIR and tokens from .env)
"""
import json
import os
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import load_settings  # noqa: E402
from app.ingest import load_pack_payload  # noqa: E402

BASE = os.environ.get("API_BASE", "http://localhost:8000")


def main():
    s = load_settings()
    by_role = {i.role: t for t, i in s.tokens.items()}
    A = {"Authorization": f"Bearer {by_role['analyst']}"}
    R = {"Authorization": f"Bearer {by_role['reviewer']}"}
    c = httpx.Client(base_url=BASE, timeout=120)

    def show(step, r):
        body = r.json()
        brief = body.get("error") or {k: body[k] for k in ("status", "current_version", "version", "dataset_id",
                                                           "analysis_id", "report_id", "is_latest") if k in body}
        print(f"{step:<48} -> HTTP {r.status_code} {json.dumps(brief)[:200]}")
        return body

    show("GET /health", c.get("/health"))
    ds = show("POST /datasets (analyst)", c.post("/datasets", json=load_pack_payload(s.pack_dir), headers=A))
    show("POST /datasets again (duplicate import)", c.post("/datasets", json=load_pack_payload(s.pack_dir), headers=A))
    run = show("POST /analyses", c.post("/analyses", json={"dataset_id": ds["dataset_id"]}, headers=A))
    while True:
        a = c.get(f"/analyses/{run['analysis_id']}", headers=A).json()
        if a["status"] in ("completed", "failed"):
            break
        time.sleep(2)
    print(f"{'analysis finished':<48} -> {a['status']} counts={a['counts']}")
    rid = a["report_id"]

    show("PATCH /reports (analyst adds note)", c.patch(f"/reports/{rid}", json={"notes": "Reviewed D1-D3 wording."}, headers=A))
    show("POST submit v1", c.post(f"/reports/{rid}/submit", json={"version": 1}, headers=A))
    show("POST decision as ANALYST (must be 403)", c.post(f"/reports/{rid}/decision", headers=A,
         json={"version": 1, "decision": "approve", "comment": "self-approval"}))
    show("POST decision reject v1 (reviewer)", c.post(f"/reports/{rid}/decision", headers=R,
         json={"version": 1, "decision": "reject", "comment": "D3 needs the job-listing caveat stated."}))
    show("GET export after rejection (must be 409)", c.get(f"/reports/{rid}/export", headers=A))
    show("PATCH after rejection -> new draft v2", c.patch(f"/reports/{rid}", headers=A,
         json={"notes": "D3: most Kasparro-related citations are job listings; treated as weak evidence."}))
    show("POST submit v2", c.post(f"/reports/{rid}/submit", json={"version": 2}, headers=A))
    show("POST decision approve v2", c.post(f"/reports/{rid}/decision", headers=R,
         json={"version": 2, "decision": "approve", "comment": "Evidence checked against cited responses."}))
    exp = show("GET export (approved v2)", c.get(f"/reports/{rid}/export", headers=A))
    Path("output").mkdir(exist_ok=True)
    Path("output/approved_report_export.json").write_text(json.dumps(exp, indent=2, ensure_ascii=False))
    show("PATCH after approval -> new draft v3", c.patch(f"/reports/{rid}", json={"title": "Edited after approval"}, headers=A))
    show("GET export latest (must be 409 stale)", c.get(f"/reports/{rid}/export", headers=A))
    show("GET export ?version=2 (explicit old approval)", c.get(f"/reports/{rid}/export?version=2", headers=A))
    print("approved export written to output/approved_report_export.json")


if __name__ == "__main__":
    main()
