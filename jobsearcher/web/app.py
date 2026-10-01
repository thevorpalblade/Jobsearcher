"""FastAPI app: factory, lifespan, per-request store connections, routes, Jinja filters.

Routes are plain `def`s, so FastAPI runs them in its threadpool. Each request gets
its own SQLite connection (see `get_store`), read-only unless it writes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from jobsearcher.config import Config, load_config
from jobsearcher.contacts import is_generic_email
from jobsearcher.models import Application, ApplicationState, JobStatus
from jobsearcher.ranking.config import RankingConfig
from jobsearcher.ranking.ranker import job_details
from jobsearcher.store import Store
from jobsearcher.web import views

WEB_DIR = Path(__file__).parent


@dataclass
class WebState:
    config: Config  # config.yaml, read once at startup
    templates: Jinja2Templates
    tz: ZoneInfo
    ranking: views.MtimeCache[RankingConfig]  # ranking.yaml, reloaded when edited
    cv: views.MtimeCache[str | None]
    memo: views.PrefilterMemo

    def row_context(self) -> views.RowContext:
        return views.RowContext(
            config=self.ranking.get(),
            memo=self.memo,
            cv=self.cv.get(),
            model=self.config.llm.ranking.model,
            now=datetime.now(self.tz),
        )


def _state(request: Request) -> WebState:
    return request.app.state.web


State = Annotated[WebState, Depends(_state)]


def get_store(state: State) -> Iterator[Store]:
    """A read-only connection for one request. check_same_thread=False because FastAPI
    may run this dependency's setup, the endpoint and the teardown on different
    threadpool threads; the request still uses the connection serially."""
    store = Store(state.config.db_path, readonly=True, check_same_thread=False)
    try:
        yield store
    finally:
        store.close()


def get_writable_store(state: State) -> Iterator[Store]:
    """A writable connection for one POST; the schema already exists (see lifespan)."""
    store = Store(state.config.db_path, check_same_thread=False, init_schema=False)
    try:
        yield store
    finally:
        store.close()


ReadStore = Annotated[Store, Depends(get_store)]
WriteStore = Annotated[Store, Depends(get_writable_store)]


def _make_templates(tz: ZoneInfo) -> Jinja2Templates:
    # Starlette turns autoescaping on: ad text and other scraped fields are untrusted.
    templates = Jinja2Templates(directory=WEB_DIR / "templates")

    def local(value: datetime | None, fmt: str = "%Y-%m-%d") -> str:
        if value is None:
            return "–"
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(tz).strftime(fmt)

    templates.env.filters["localdate"] = local
    templates.env.filters["localtime"] = lambda v: local(v, "%Y-%m-%d %H:%M")
    templates.env.filters["usd"] = lambda v: f"${v:,.2f}"
    templates.env.filters["safe_url"] = views.safe_url
    templates.env.filters["provenance"] = views.provenance_label
    templates.env.tests["generic_email"] = is_generic_email
    return templates


def render(
    request: Request,
    state: WebState,
    name: str,
    store: Store | None = None,
    status_code: int = 200,
    **context: Any,
) -> HTMLResponse:
    """Render a template. Pass the store for full pages: their header shows the budget."""
    if store is not None:
        context["budget"] = views.budget_info(store, state.config.llm)
    return state.templates.TemplateResponse(
        request, name, {"config": state.config, **context}, status_code=status_code
    )


def is_htmx(request: Request) -> bool:
    """A partial-page request from HTMX (a history restore wants the full page)."""
    return "HX-Request" in request.headers and "HX-History-Restore-Request" not in request.headers


router = APIRouter()


@router.get("/healthz", response_class=PlainTextResponse)
def healthz(store: ReadStore) -> str:
    store.count_jobs()  # fails if the database can't be read
    return "ok"


@router.get("/", response_class=HTMLResponse)
def job_list(
    request: Request,
    state: State,
    store: ReadStore,
    filters: Annotated[views.ListFilters, Query()],
) -> HTMLResponse:
    ctx = state.row_context()
    rows = views.load_rows(store, ctx, filters.view)
    options = views.filter_options(rows, ctx.config)
    rows = views.sort_rows(views.apply_filters(rows, filters), filters.sort)
    fragment = is_htmx(request)
    response = render(
        request,
        state,
        "_rows.html" if fragment else "list.html",
        None if fragment else store,
        rows=rows,
        filters=filters,
        options=options,
        ranking=ctx.config,
        new_days=filters.new or views.NEW_DAYS,
    )
    # The same URL returns a fragment or a full page; keep caches from mixing them up.
    response.headers["Vary"] = "HX-Request"
    return response


@router.get("/prefilter", response_class=HTMLResponse)
def prefilter_page(request: Request, state: State, store: ReadStore) -> HTMLResponse:
    ctx = state.row_context()
    rows = views.load_rows(store, ctx, "all")
    stages = {stage: sum(r.stage == stage for r in rows) for stage in views.STAGES}
    return render(
        request,
        state,
        "prefilter.html",
        store,
        summary=views.prefilter_summary(rows, ctx.config),
        total=len(rows),
        stages=stages,
        ranking=ctx.config,
    )


@router.get("/partials/budget", response_class=HTMLResponse)
def budget_partial(request: Request, state: State, store: ReadStore) -> HTMLResponse:
    """The header's budget widget, polled by HTMX every minute."""
    return render(request, state, "_budget.html", store)


@router.get("/status", response_class=HTMLResponse)
def status_page(request: Request, state: State, store: ReadStore) -> HTMLResponse:
    ctx = state.row_context()
    rows = views.load_rows(store, ctx, "all")
    counts = {stage: sum(r.stage == stage for r in rows) for stage in views.STAGES}
    counts["stale"] = sum(r.stale for r in rows)
    counts["unparseable"] = sum(r.unparseable for r in rows)
    month_start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return render(
        request,
        state,
        "status.html",
        store,
        open_jobs=len(rows),
        expired_jobs=store.count_jobs(JobStatus.EXPIRED),
        counts=counts,
        last_runs=store.last_runs(),
        usage=store.llm_usage_summary(month_start),
        month_start=month_start,
    )


# Declared before /jobs/{job_id}, which would otherwise match "<id>.json" too.
@router.get("/jobs/{job_id}.json")
def job_json(job_id: str, state: State, store: ReadStore) -> JSONResponse:
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(404)
    return JSONResponse(job_details(store, job, state.ranking.get()))


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_page(request: Request, job_id: str, state: State, store: ReadStore) -> HTMLResponse:
    record = store.job_record(job_id)
    if record is None:
        raise HTTPException(404)
    latest = store.latest_ranking(job_id)
    ctx = state.row_context()
    row = views.make_row(record, latest[0] if latest else None, ctx, store.get_application(job_id))
    return render(
        request,
        state,
        "job.html",
        store,
        row=row,
        job=record.job,
        ranked_at=latest[1] if latest else None,
        ranking=ctx.config,
        states=list(ApplicationState),
    )


def require_htmx(request: Request) -> None:
    """Writes must come from HTMX. Without a login, this is the CSRF guard: a custom
    header makes a cross-site request need a CORS preflight, which this app never
    allows, so plain cross-site form posts are refused."""
    if "HX-Request" not in request.headers:
        raise HTTPException(403, "Write requests must come from the web UI")


def _tracking_response(
    request: Request, state: WebState, store: Store, job_id: str, app: Application | None
) -> HTMLResponse:
    job = store.get_job(job_id)
    return render(
        request,
        state,
        "_tracking.html",
        job=job,
        application=app,
        states=list(ApplicationState),
    )


@router.post(
    "/jobs/{job_id}/state", response_class=HTMLResponse, dependencies=[Depends(require_htmx)]
)
def set_state(
    request: Request,
    job_id: str,
    new_state: Annotated[ApplicationState, Form(alias="state")],
    state: State,
    store: WriteStore,
) -> HTMLResponse:
    if store.get_job(job_id) is None:
        raise HTTPException(404)
    current = store.get_application(job_id)
    app = store.set_application(job_id, new_state, current.notes if current else "")
    return _tracking_response(request, state, store, job_id, app)


@router.post(
    "/jobs/{job_id}/notes", response_class=HTMLResponse, dependencies=[Depends(require_htmx)]
)
def set_notes(
    request: Request,
    job_id: str,
    notes: Annotated[str, Form()],
    state: State,
    store: WriteStore,
) -> HTMLResponse:
    if store.get_job(job_id) is None:
        raise HTTPException(404)
    current = store.get_application(job_id)
    app = store.set_application(job_id, current.state if current else ApplicationState.NEW, notes)
    return _tracking_response(request, state, store, job_id, app)


def create_app(config: Config) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # One normal open at startup creates the DB and schema on a fresh install,
        # runs migrations and switches to WAL; requests then open their own connections.
        Store(config.db_path).close()
        yield

    app = FastAPI(
        title="Jobsearcher", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    tz = ZoneInfo(config.schedule.timezone)
    app.state.web = WebState(
        config=config,
        templates=_make_templates(tz),
        tz=tz,
        ranking=views.ranking_config_cache(config.ranking_config),
        cv=views.cv_cache(config.cv_path),
        memo=views.PrefilterMemo(),
    )
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    @app.exception_handler(StarletteHTTPException)
    def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code == 404:
            return render(request, _state(request), "404.html", status_code=404)
        return PlainTextResponse(f"{exc.status_code}: {exc.detail}", status_code=exc.status_code)

    app.include_router(router)
    return app


def create_app_from_env() -> FastAPI:
    """Factory for `uvicorn --reload`, which needs an import string."""
    return create_app(load_config())
