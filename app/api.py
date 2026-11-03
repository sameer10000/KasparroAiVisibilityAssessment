"""HTTP layer. Interactive API docs are generated at /docs (OpenAPI JSON at /openapi.json)."""
import json
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, Depends, FastAPI, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pathlib import Path
from pydantic import BaseModel, ConfigDict

from . import analysis, reports
from .config import Identity, Settings, load_settings
from .db import connect, init_db
from .ingest import DatasetIn, ImportError_, import_dataset
from .llm import build_gateway
from .reports import ApiError


class DemoLoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str


class AnalysisIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: str


def create_app(settings: Settings | None = None, gateway_factory=None) -> FastAPI:
    settings = settings or load_settings()
    gateway_factory = gateway_factory or (lambda: build_gateway(settings))

    @asynccontextmanager
    async def lifespan(app):
        conn = connect(settings.db_path)
        init_db(conn)
        conn.close()
        yield

    app = FastAPI(title="Kasparro AI visibility analysis", version="1.0.0", lifespan=lifespan)

    def db():
        conn = connect(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    bearer = HTTPBearer(auto_error=False, description="Analyst or reviewer token from .env")

    def identity(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Identity:
        token = creds.credentials.strip() if creds else ""
        who = settings.tokens.get(token) if token else None
        if who is None:
            raise ApiError(401, "unauthenticated", "Send 'Authorization: Bearer <token>' with a configured token")
        return who

    @app.exception_handler(ApiError)
    async def api_error(_: Request, e: ApiError):
        return JSONResponse(status_code=e.status, content={"error": {"code": e.code, "message": e.message, "details": e.details}})

    @app.exception_handler(ImportError_)
    async def import_error(_: Request, e: ImportError_):
        return JSONResponse(status_code=e.status, content={"error": {"code": e.code, "message": e.message, "details": e.details}})

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, e: RequestValidationError):
        details = [{"loc": list(x["loc"]), "msg": x["msg"]} for x in e.errors()[:20]]
        return JSONResponse(status_code=422, content={"error": {"code": "invalid_request", "message": "Request failed validation", "details": details}})

    @app.get("/", include_in_schema=False)
    def ui():
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    @app.get("/demo/login", include_in_schema=False)
    def demo_status():
        return {"enabled": settings.demo_login}

    @app.post("/demo/login", include_in_schema=False)
    def demo_login(body: DemoLoginIn):
        """DEMO_LOGIN=true only. Hands the configured token for a role to the local web page, which then
        uses it like any other client. Every endpoint still derives the role from the token."""
        if not settings.demo_login:
            raise ApiError(404, "demo_login_disabled", "Demo login is off. Set DEMO_LOGIN=true for local use only.")
        token = next((t for t, who in settings.tokens.items() if who.role == body.role), None)
        if token is None:
            raise ApiError(404, "role_not_configured", f"No token configured for role {body.role}")
        return {"token": token, "role": body.role}

    @app.get("/whoami")
    def whoami(who: Identity = Depends(identity)):
        return {"name": who.name, "role": who.role}

    @app.get("/reports")
    def list_reports(conn=Depends(db), who: Identity = Depends(identity)):
        rows = conn.execute(
            "SELECT r.report_id, r.analysis_id, r.created_at, a.execution_mode, v.version, v.status"
            " FROM reports r JOIN analysis_runs a USING (analysis_id)"
            " JOIN report_versions v ON v.report_id = r.report_id"
            " AND v.version = (SELECT MAX(version) FROM report_versions WHERE report_id = r.report_id)"
            " ORDER BY r.created_at DESC").fetchall()
        return {"reports": [dict(r) for r in rows]}

    @app.get("/health")
    def health(conn=Depends(db)):
        conn.execute("SELECT 1").fetchone()
        return {"status": "ok", "db": "ok", "model_mode": settings.model_mode}

    @app.post("/datasets", status_code=201)
    def post_dataset(body: DatasetIn, conn=Depends(db), who: Identity = Depends(identity)):
        reports.require_role(who, "analyst")
        summary, created = import_dataset(conn, body, who.name)
        if not created:
            return JSONResponse(status_code=200, content={**summary, "reused_existing_dataset": True})
        return summary

    @app.get("/datasets/{dataset_id}")
    def get_dataset(dataset_id: str, conn=Depends(db), who: Identity = Depends(identity)):
        row = conn.execute("SELECT * FROM datasets WHERE dataset_id = ?", (dataset_id,)).fetchone()
        if not row:
            raise ApiError(404, "dataset_not_found", f"No dataset {dataset_id}")
        return json.loads(row["summary_json"])

    @app.post("/analyses", status_code=202)
    def post_analysis(body: AnalysisIn, tasks: BackgroundTasks, conn=Depends(db), who: Identity = Depends(identity)):
        reports.require_role(who, "analyst")
        if not conn.execute("SELECT 1 FROM datasets WHERE dataset_id = ?", (body.dataset_id,)).fetchone():
            raise ApiError(404, "dataset_not_found", f"No dataset {body.dataset_id}")
        gateway = gateway_factory()
        analysis_id = analysis.create_run(conn, body.dataset_id, gateway, who.name)

        def work():
            c = connect(settings.db_path)
            try:
                analysis.execute(c, analysis_id, gateway)
            except Exception:
                pass  # recorded on the run row as status=failed
            finally:
                c.close()

        tasks.add_task(work)
        return {"analysis_id": analysis_id, "status": "queued", "execution_mode": gateway.mode,
                "poll": f"/analyses/{analysis_id}"}

    @app.get("/analyses/{analysis_id}")
    def get_analysis(analysis_id: str, conn=Depends(db), who: Identity = Depends(identity)):
        run = conn.execute("SELECT * FROM analysis_runs WHERE analysis_id = ?", (analysis_id,)).fetchone()
        if not run:
            raise ApiError(404, "analysis_not_found", f"No analysis {analysis_id}")
        rep = conn.execute("SELECT report_id FROM reports WHERE analysis_id = ?", (analysis_id,)).fetchone()
        return {
            "analysis_id": analysis_id, "dataset_id": run["dataset_id"], "status": run["status"],
            "execution_mode": run["execution_mode"], "config": json.loads(run["config_json"]),
            "created_at": run["created_at"], "finished_at": run["finished_at"], "error": run["error"],
            "counts": json.loads(run["counts_json"]) if run["counts_json"] else None,
            "scores": json.loads(run["aggregates_json"]) if run["aggregates_json"] else None,
            "report_id": rep["report_id"] if rep else None,
        }

    @app.get("/analyses/{analysis_id}/results")
    def get_results(analysis_id: str, status: str | None = Query(default=None, pattern="^(ok|failed|skipped)$"),
                    include_trace: bool = False, conn=Depends(db), who: Identity = Depends(identity)):
        rows = analysis.result_rows(conn, analysis_id)
        if status:
            rows = [r for r in rows if r["status"] == status]
        if not include_trace:
            for r in rows:
                r.pop("trace")
        return {"analysis_id": analysis_id, "count": len(rows), "results": rows}

    @app.get("/reports/{report_id}")
    def get_report(report_id: str, version: int | None = None, conn=Depends(db), who: Identity = Depends(identity)):
        return reports.get_report(conn, report_id, version)

    @app.patch("/reports/{report_id}")
    def patch_report(report_id: str, body: reports.ReportPatch, conn=Depends(db), who: Identity = Depends(identity)):
        return reports.patch_report(conn, report_id, body, who)

    @app.post("/reports/{report_id}/submit")
    def submit(report_id: str, body: reports.SubmitIn, conn=Depends(db), who: Identity = Depends(identity)):
        return reports.submit_report(conn, report_id, body, who)

    @app.post("/reports/{report_id}/decision")
    def decision(report_id: str, body: reports.DecisionIn, conn=Depends(db), who: Identity = Depends(identity)):
        return reports.decide(conn, report_id, body, who)

    @app.get("/reports/{report_id}/export")
    def export(report_id: str, version: int | None = None, conn=Depends(db), who: Identity = Depends(identity)):
        return reports.export_report(conn, report_id, version)

    return app


app = create_app()
