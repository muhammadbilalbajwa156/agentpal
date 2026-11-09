"""Small session-isolated chat UI. No owner credentials or owner endpoints."""

from __future__ import annotations

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

from ..core import Shopper, ShopperError, identifier, money
from ..journal import Journal, run_errand

STATIC = Path(__file__).parent / "static"


class ErrandInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: str = Field(min_length=1, max_length=500)
    budget: str = Field(min_length=1, max_length=12)
    errand_id: str = Field(min_length=1, max_length=128)
    merchant_paypal_email: str | None = Field(default=None, max_length=127)


def create_app(*, shopper_factory=Shopper, journal_path: str | None = None) -> FastAPI:
    jobs: dict[str, dict] = {}
    lock = threading.Lock()
    slots = threading.BoundedSemaphore(2)
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="shopper")
    path = journal_path or os.environ.get("SHOPPER_JOURNAL", "/tmp/shopper/errands.sqlite3")

    @asynccontextmanager
    async def lifespan(app):
        yield
        executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(title="AgentPal Shopper", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        return JSONResponse({"error": "invalid_errand_input"}, status_code=422)

    @app.middleware("http")
    async def protect(request: Request, call_next):
        # Refuse cross-origin writes; JSON POST also forces a browser preflight.
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"error": "origin_refused"}, status_code=403)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"error": "json_required"}, status_code=415)
            size = 0
            chunks = []
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
        if not request.cookies.get("shopper_session"):
            response.set_cookie("shopper_session", secrets.token_urlsafe(32), httponly=True,
                                secure=request.url.scheme == "https", samesite="strict")
        return response

    @app.get("/health")
    def health():
        return {"status": "ok", "payment_environment": "sandbox"}

    @app.get("/static/app.js")
    def javascript():
        return FileResponse(STATIC / "app.js", media_type="text/javascript")

    @app.get("/static/style.css")
    def stylesheet():
        return FileResponse(STATIC / "style.css", media_type="text/css")

    def session(request):
        value = request.cookies.get("shopper_session", "")
        if not 32 <= len(value) <= 128:
            raise HTTPException(403, "session_required")
        return value

    def work(job):
        shopper = journal = None

        def emit(event):
            with lock:
                job["events"].append(event)
                job["event_count"] += 1
                job["events"] = job["events"][-300:]
                for key in ("request_id", "status"):
                    if event.get(key):
                        job[key] = event[key]
        try:
            shopper = shopper_factory(emit=emit)
            journal = Journal(path)
            result = run_errand(shopper, journal, **job["input"], wait_seconds=21600)
            emit({"message": "Errand run ended.", **result})
        except ShopperError as exc:
            emit({"message": "Errand stopped safely.", "error": str(exc)})
        except Exception:
            emit({"message": "Errand stopped safely.", "error": "shopper_unavailable"})
        finally:
            if shopper:
                shopper.close()
            if journal:
                journal.close()
            with lock:
                job["done"] = True
                job["finished_at"] = time.monotonic()
            slots.release()

    @app.post("/errands", status_code=202)
    def start_errand(body: ErrandInput, request: Request):
        owner_session = session(request)
        try:
            identifier(body.errand_id)
            money(body.budget)
        except ShopperError:
            raise HTTPException(422, "invalid_errand_input") from None
        values = body.model_dump()
        # Session-bound ID prevents one browser from resuming another browser's errand.
        import hashlib
        key = hashlib.sha256((owner_session + ":" + body.errand_id).encode()).hexdigest()
        values["errand_id"] = key
        with lock:
            old = jobs.get(key)
            if old:
                if old["input"] != values:
                    raise HTTPException(409, "errand_id_conflict")
                return {"job_id": key}
            for old_key, job in list(jobs.items()):
                if job["done"] and time.monotonic() - job["finished_at"] > 3600:
                    del jobs[old_key]
            if len(jobs) >= 64 or not slots.acquire(blocking=False):
                raise HTTPException(429, "shopper_busy")
            job = {"session": owner_session, "input": values, "events": [], "done": False,
                   "event_count": 0}
            jobs[key] = job
        executor.submit(work, job)
        return {"job_id": key}

    @app.get("/errands/{job_id}")
    def poll(job_id: str, request: Request):
        owner_session = session(request)
        with lock:
            job = jobs.get(job_id)
            if not job or not hmac.compare_digest(job["session"], owner_session):
                raise HTTPException(404, "errand_not_found")
            return {"events": list(job["events"]), "done": job["done"],
                    "offset": job["event_count"] - len(job["events"]),
                    "request_id": job.get("request_id"), "status": job.get("status")}

    return app


app = create_app()
