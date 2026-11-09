"""Company HQ: pick a company, start its business day, watch the agents' steps live.

Same protections as the Shopper web app: restrictive CSP (no inline script), nosniff, no
referrer, no-store, same-origin JSON-only POSTs with an 8 KiB body cap, per-browser session
cookies isolating job reads, bounded jobs/events. No owner credentials or owner routes.
One business day runs at a time; the durable journal makes a repeated start of the same
company day replay statuses instead of spending model calls or creating new requests.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ...common import AgentError, Redactor, identifier
from ..runner import run_company
from ..scenarios import COMPANIES, FREELANCERS

STATIC = Path(__file__).parent / "static"
COOKIE = "company_hq_session"


class DayInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: str = Field(min_length=1, max_length=32)
    run_id: str = Field(min_length=1, max_length=128)


def cast() -> dict:
    """Public cast list: trusted configuration only (no payee mailboxes, no keys)."""
    return {"companies": [{
        "company_id": c.company_id, "name": c.name, "tagline": c.tagline,
        "agents": [{"key": a.key, "name": a.name, "mission": a.mission,
                    "monthly_budget": a.monthly_budget,
                    "per_transaction_limit": a.per_transaction_limit,
                    "errands": [{"errand_id": e.errand_id, "goal": e.goal, "budget": e.budget,
                                 "kind": e.kind, "payees": [FREELANCERS[i.payee_id].name
                                                            for i in e.invoices]}
                                for e in a.errands]} for a in c.agents]}
        for c in COMPANIES.values()]}


def create_app(*, runner=run_company, journal_path: str | None = None,
               env: dict[str, str] | None = None) -> FastAPI:
    jobs: dict[str, dict] = {}
    lock = threading.Lock()
    slots = threading.BoundedSemaphore(1)  # free model quota: one business day at a time
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="company-hq")
    path = journal_path or os.environ.get("COMPANY_JOURNAL", "/tmp/company-hq/errands.sqlite3")
    environment = dict(os.environ if env is None else env)
    redact = Redactor(environment, extra=[environment.get("COMPANY_AGENT_KEYS", "")])
    planner = environment.get("COMPANY_PLANNER", "llm")

    @asynccontextmanager
    async def lifespan(app):
        yield
        executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="AgentPal Company HQ", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        return JSONResponse({"error": "invalid_day_input"}, status_code=422)

    @app.middleware("http")
    async def protect(request: Request, call_next):
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"error": "origin_refused"}, status_code=403)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"error": "json_required"}, status_code=415)
            size, chunks = 0, []
            async for chunk in request.stream():
                size += len(chunk)
                if size > 8192:
                    return JSONResponse({"error": "input_too_large"}, status_code=413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers.update({
            "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
                                       "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
                                       "form-action 'self'",
            "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store"})
        return response

    @app.get("/")
    def index(request: Request):
        response = FileResponse(STATIC / "index.html")
        if not request.cookies.get(COOKIE):
            response.set_cookie(COOKIE, secrets.token_urlsafe(32), httponly=True,
                                secure=request.url.scheme == "https", samesite="strict")
        return response

    @app.get("/health")
    def health():
        return {"status": "ok", "payment_environment": "sandbox"}

    @app.get("/static/hq.js")
    def javascript():
        return FileResponse(STATIC / "hq.js", media_type="text/javascript")

    @app.get("/static/hq.css")
    def stylesheet():
        return FileResponse(STATIC / "hq.css", media_type="text/css")

    @app.get("/companies")
    def companies():
        return cast()

    def session(request):
        value = request.cookies.get(COOKIE, "")
        if not 32 <= len(value) <= 128:
            raise HTTPException(403, "session_required")
        return value

    def work(job):
        def emit(event):
            event = redact.clean(event)
            with lock:
                job["events"].append(event)
                job["event_count"] += 1
                job["events"] = job["events"][-400:]
        try:
            result = runner(job["company"], env=environment, journal_path=path,
                            planner=planner, emit=emit)
            with lock:
                job["results"] = redact.clean(result["results"])
        except AgentError as exc:
            emit({"message": "Business day stopped safely.", "error": str(exc)})
        except Exception:
            emit({"message": "Business day stopped safely.", "error": "company_hq_unavailable"})
        finally:
            with lock:
                job["done"] = True
                job["finished_at"] = time.monotonic()
            slots.release()

    @app.post("/days", status_code=202)
    def start_day(body: DayInput, request: Request):
        owner_session = session(request)
        if body.company not in COMPANIES:
            raise HTTPException(422, "invalid_day_input")
        try:
            identifier(body.run_id)
        except AgentError:
            raise HTTPException(422, "invalid_day_input") from None
        key = hashlib.sha256((owner_session + ":" + body.run_id).encode()).hexdigest()
        with lock:
            old = jobs.get(key)
            if old:
                if old["company"] != body.company:
                    raise HTTPException(409, "run_id_conflict")
                return {"job_id": key}
            for old_key, job in list(jobs.items()):
                if job["done"] and time.monotonic() - job["finished_at"] > 3600:
                    del jobs[old_key]
            if len(jobs) >= 32 or not slots.acquire(blocking=False):
                raise HTTPException(429, "company_hq_busy")
            job = {"session": owner_session, "company": body.company, "events": [],
                   "event_count": 0, "done": False, "results": []}
            jobs[key] = job
        executor.submit(work, job)
        return {"job_id": key}

    @app.get("/days/{job_id}")
    def poll(job_id: str, request: Request):
        owner_session = session(request)
        with lock:
            job = jobs.get(job_id)
            if not job or not hmac.compare_digest(job["session"], owner_session):
                raise HTTPException(404, "day_not_found")
            return {"events": list(job["events"]), "done": job["done"],
                    "offset": job["event_count"] - len(job["events"]),
                    "results": list(job["results"])}

    return app


app = create_app()
