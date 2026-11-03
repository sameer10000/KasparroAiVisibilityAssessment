"""Report versions and human review. All permission and state checks live here, server-side.

States per version: draft -> submitted -> approved | rejected.
  * Only a draft is mutable. Editing a submitted, approved or rejected version creates a new draft.
  * Submitting freezes the content and records its SHA-256.
  * A reviewer decides on one exact (version, hash). Approval never carries over to a later version.
  * Export of the latest version needs that version approved. An older approved version can be
    exported only when requested explicitly by number, and is labelled as not the latest.
"""
import hashlib
import json
import sqlite3

from pydantic import BaseModel, ConfigDict, Field

from .config import Identity
from .db import dumps, now
from .diagnostics import verify_evidence


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details=None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    response_id: str
    prompt_id: str | None = None
    engine: str | None = None
    excerpt: str = Field(min_length=8, max_length=1000)
    note: str | None = None


class Diagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: str = Field(min_length=3, max_length=300)
    kind: str = "loss"
    pattern: str = Field(min_length=10, max_length=4000)
    possible_explanation: str = Field(max_length=4000)
    test_evidence: str = Field(max_length=4000)
    metrics: dict = Field(default_factory=dict)
    evidence: list[EvidenceItem] = Field(min_length=1, max_length=20)


class Remedy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    priority: int = Field(ge=1, le=10)
    diagnostic_id: str
    problem: str
    supporting_evidence: str
    action: str
    expected_benefit: str
    effort: str
    uncertainty: str
    validation: str
    priority_rationale: str | None = None


class ReportPatch(BaseModel):
    """Editable fields only. Unknown fields (e.g. 'status', 'role', 'approved_by') are rejected."""
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=300)
    notes: str | None = Field(default=None, max_length=10_000)
    diagnostics: list[Diagnostic] | None = Field(default=None, max_length=10)
    remedies: list[Remedy] | None = Field(default=None, max_length=10)


class SubmitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int
    decision: str = Field(pattern="^(approve|reject)$")
    comment: str = Field(min_length=3, max_length=4000)


def require_role(identity: Identity, role: str) -> None:
    if identity.role != role:
        raise ApiError(403, "wrong_role", f"This action requires the {role} role; you are {identity.role}.")


def content_hash(content: dict) -> str:
    return hashlib.sha256(dumps(content).encode()).hexdigest()


def _report(conn, report_id):
    rep = conn.execute("SELECT * FROM reports WHERE report_id = ?", (report_id,)).fetchone()
    if not rep:
        raise ApiError(404, "report_not_found", f"No report {report_id}")
    return rep


def _version(conn, report_id, version=None):
    if version is None:
        row = conn.execute("SELECT * FROM report_versions WHERE report_id = ? ORDER BY version DESC LIMIT 1",
                           (report_id,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM report_versions WHERE report_id = ? AND version = ?",
                           (report_id, version)).fetchone()
    if not row:
        raise ApiError(404, "version_not_found", f"No version {version} for report {report_id}")
    return row


def _version_view(conn, v) -> dict:
    decisions = [dict(d) for d in conn.execute(
        "SELECT decision, reviewer, comment, decided_at, content_hash FROM review_decisions"
        " WHERE report_id = ? AND version = ? ORDER BY decision_id", (v["report_id"], v["version"]))]
    return {"version": v["version"], "status": v["status"], "based_on": v["based_on"],
            "content_hash": v["content_hash"], "created_by": v["created_by"], "created_at": v["created_at"],
            "submitted_by": v["submitted_by"], "submitted_at": v["submitted_at"], "decisions": decisions}


def get_report(conn: sqlite3.Connection, report_id: str, version: int | None = None) -> dict:
    rep = _report(conn, report_id)
    current = _version(conn, report_id)
    shown = _version(conn, report_id, version) if version else current
    versions = [_version_view(conn, v) for v in conn.execute(
        "SELECT * FROM report_versions WHERE report_id = ? ORDER BY version", (report_id,))]
    return {"report_id": report_id, "analysis_id": rep["analysis_id"], "current_version": current["version"],
            "shown_version": shown["version"], "status": shown["status"],
            "content": json.loads(shown["content_json"]), "versions": versions}


def _validate_content(conn, analysis_id: str, content: dict) -> None:
    problems = verify_evidence(conn, analysis_id, content.get("diagnostics", []))
    diag_ids = {d["id"] for d in content.get("diagnostics", [])}
    for r in content.get("remedies", []):
        if r["diagnostic_id"] not in diag_ids:
            problems.append({"remedy": r["id"], "error": "remedy_without_diagnostic", "diagnostic_id": r["diagnostic_id"]})
    if problems:
        raise ApiError(422, "unsupported_evidence", "Report content failed evidence checks", problems)


def patch_report(conn: sqlite3.Connection, report_id: str, patch: ReportPatch, who: Identity) -> dict:
    require_role(who, "analyst")
    rep = _report(conn, report_id)
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = _version(conn, report_id)
        content = json.loads(cur["content_json"])
        changes = patch.model_dump(exclude_none=True)
        if not changes:
            raise ApiError(422, "empty_patch", "No editable fields supplied")
        content.update(changes)
        _validate_content(conn, rep["analysis_id"], content)
        ts = now()
        if cur["status"] == "draft":
            conn.execute("UPDATE report_versions SET content_json = ?, updated_at = ? WHERE report_id = ? AND version = ?",
                         (dumps(content), ts, report_id, cur["version"]))
            version, created = cur["version"], False
        else:
            version, created = cur["version"] + 1, True
            conn.execute(
                "INSERT INTO report_versions (report_id, version, status, content_json, based_on, created_by,"
                " created_at, updated_at) VALUES (?, ?, 'draft', ?, ?, ?, ?, ?)",
                (report_id, version, dumps(content), cur["version"], who.name, ts, ts))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    out = get_report(conn, report_id)
    out["new_version_created"] = created
    return out


def submit_report(conn: sqlite3.Connection, report_id: str, body: SubmitIn, who: Identity) -> dict:
    require_role(who, "analyst")
    rep = _report(conn, report_id)
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = _version(conn, report_id)
        if body.version != cur["version"]:
            raise ApiError(409, "not_current_version", f"Version {body.version} is not the current version {cur['version']}")
        if cur["status"] != "draft":
            raise ApiError(409, "not_a_draft", f"Version {cur['version']} is {cur['status']}; only drafts can be submitted")
        content = json.loads(cur["content_json"])
        if not content.get("diagnostics") or not content.get("remedies"):
            raise ApiError(422, "incomplete_report", "A report needs diagnostics and remedies before review")
        _validate_content(conn, rep["analysis_id"], content)
        conn.execute("UPDATE report_versions SET status = 'submitted', content_hash = ?, submitted_by = ?, submitted_at = ?"
                     " WHERE report_id = ? AND version = ?",
                     (content_hash(content), who.name, now(), report_id, cur["version"]))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_report(conn, report_id)


def decide(conn: sqlite3.Connection, report_id: str, body: DecisionIn, who: Identity) -> dict:
    require_role(who, "reviewer")
    _report(conn, report_id)
    conn.execute("BEGIN IMMEDIATE")
    try:
        v = _version(conn, report_id, body.version)
        if v["status"] != "submitted":
            raise ApiError(409, "not_submitted", f"Version {v['version']} is {v['status']}; only submitted versions can be decided")
        if content_hash(json.loads(v["content_json"])) != v["content_hash"]:
            raise ApiError(409, "integrity_error", "Submitted content no longer matches its recorded hash")
        status = "approved" if body.decision == "approve" else "rejected"
        conn.execute("UPDATE report_versions SET status = ? WHERE report_id = ? AND version = ?",
                     (status, report_id, v["version"]))
        conn.execute("INSERT INTO review_decisions (report_id, version, content_hash, decision, reviewer, comment, decided_at)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (report_id, v["version"], v["content_hash"], status, who.name, body.comment, now()))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_report(conn, report_id, body.version)


def export_report(conn: sqlite3.Connection, report_id: str, version: int | None) -> dict:
    _report(conn, report_id)
    latest = _version(conn, report_id)
    v = _version(conn, report_id, version) if version else latest
    if v["status"] != "approved":
        if v["status"] == "rejected":
            raise ApiError(409, "rejected", f"Version {v['version']} was rejected and cannot be exported")
        older = conn.execute("SELECT MAX(version) AS v FROM report_versions WHERE report_id = ? AND status = 'approved'",
                             (report_id,)).fetchone()["v"]
        if version is None and older:
            raise ApiError(409, "stale_approval",
                           f"Latest version {v['version']} is {v['status']} and has not been approved. Approval of "
                           f"version {older} does not cover it. Export version {older} explicitly with ?version={older}.",
                           {"latest_version": v["version"], "approved_version": older})
        raise ApiError(409, "not_approved", f"Version {v['version']} is {v['status']}; only approved versions can be exported")
    content = json.loads(v["content_json"])
    if content_hash(content) != v["content_hash"]:
        raise ApiError(409, "integrity_error", "Approved content no longer matches its recorded hash")
    approval = conn.execute("SELECT reviewer, comment, decided_at, content_hash FROM review_decisions WHERE report_id = ?"
                            " AND version = ? AND decision = 'approved' ORDER BY decision_id DESC LIMIT 1",
                            (report_id, v["version"])).fetchone()
    return {
        "report_id": report_id, "version": v["version"], "is_latest": v["version"] == latest["version"],
        "latest_version": latest["version"], "content_hash": v["content_hash"],
        "execution_mode": content.get("execution_mode"), "approval": dict(approval), "exported_at": now(),
        "content": content,
    }
