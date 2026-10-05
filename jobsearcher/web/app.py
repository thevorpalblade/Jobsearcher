"""FastAPI app: factory, lifespan, per-request store connections, routes, Jinja filters.

Routes are plain `def`s, so FastAPI runs them in its threadpool. Each request gets
its own SQLite connection (see `get_store`), read-only unless it writes.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import yaml
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from jobsearcher import chat as chat_module
from jobsearcher import cvs, settings
from jobsearcher.config import Config, config_file_path, load_config
from jobsearcher.contacts import is_generic_email
from jobsearcher.models import Application, ApplicationState, JobStatus
from jobsearcher.ranking.config import RankingConfig
from jobsearcher.ranking.ranker import job_details
from jobsearcher.store import Store
from jobsearcher.web import forms, views

WEB_DIR = Path(__file__).parent


@dataclass
class WebState:
    config: Config  # config.yaml: read at startup and after it's saved on /settings
    config_path: Path
    templates: Jinja2Templates
    tz: ZoneInfo
    chat: chat_module.ChatManager
    ranking: views.MtimeCache[RankingConfig]  # ranking.yaml, reloaded when edited
    cv: views.MtimeCache[str | None]
    memo: views.PrefilterMemo

    def reload_config(self) -> None:
        """Pick up a saved config.yaml (paths to ranking.yaml and the CV may change)."""
        self.config = load_config(self.config_path)
        self.chat.config = self.config.chat
        self.chat.user_name = self.config.web.user_name
        self.ranking = views.ranking_config_cache(self.config.ranking_config)
        self.cv = views.cv_cache(self.config.cv_path)

    @property
    def backup_dir(self) -> Path:
        return self.config.data_dir / "backups"

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

    def static_url(name: str) -> str:
        """A static file's URL with a version that changes with the file, so browsers
        fetch the new CSS/JS after an update instead of using a stale cached copy."""
        try:
            version = (WEB_DIR / "static" / name).stat().st_mtime_ns
        except OSError:
            version = 0
        return f"/static/{name}?v={version}"

    templates.env.globals["static"] = static_url
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
def dashboard(
    request: Request, state: State, store: ReadStore, chat: str | None = None
) -> HTMLResponse:
    ctx = state.row_context()
    rows = views.load_rows(store, ctx, "all")
    chats = store.list_chats()
    current = None
    if chat != "new":
        current = store.get_chat(chat) if chat else (chats[0] if chats else None)
    run = state.chat.run_for(current["id"]) if current else None
    # Read the event count first: anything emitted after it is streamed, anything
    # before it is already in the stored messages (each is saved before it's emitted).
    after = len(run.events) if run and not run.done else None
    messages = (
        [
            {"id": m["id"], "role": m["role"], "text": m["text"]}
            for m in store.chat_messages(current["id"])
        ]
        if current
        else []
    )
    return render(
        request,
        state,
        "dashboard.html",
        store,
        top=views.top_rows(rows, 5),
        stats=views.dashboard_stats(rows, store.last_runs()),
        ranking=ctx.config,
        chats=chats,
        current=current,
        messages=messages,
        running_after=after,
        chat_problem=state.chat.unavailable(),
        suggestions=chat_module.SUGGESTIONS,
    )


@router.get("/jobs", response_class=HTMLResponse)
def job_list(
    request: Request,
    state: State,
    store: ReadStore,
    filters: Annotated[views.ListFilters, Query()],
) -> HTMLResponse:
    ctx = state.row_context()
    rows = views.load_rows(store, ctx, filters.view)
    options = views.filter_options(rows, ctx.config)
    rows = views.sort_rows(views.apply_filters(rows, filters), filters.sort, filters.dir)
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
        sort_links=views.sort_links(filters),
        sort_labels=views.SORT_LABELS,
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
    state: State,
    store: WriteStore,
    # An emptied textarea arrives as a missing field; that clears the notes.
    notes: Annotated[str, Form()] = "",
) -> HTMLResponse:
    if store.get_job(job_id) is None:
        raise HTTPException(404)
    current = store.get_application(job_id)
    app = store.set_application(job_id, current.state if current else ApplicationState.NEW, notes)
    return _tracking_response(request, state, store, job_id, app)


# --- settings: CVs and config files ------------------------------------------------


def htmx_redirect(url: str) -> Response:
    return Response(status_code=204, headers={"HX-Redirect": url})


def _result(
    request: Request, state: WebState, ok: bool, message: str, details: list[str] | None = None
) -> HTMLResponse:
    return render(request, state, "_result.html", ok=ok, message=message, details=details or [])


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, state: State, store: ReadStore) -> HTMLResponse:
    master = state.config.cv_path
    files = [(f, f.path(state.config, state.config_path)) for f in settings.FILES.values()]
    return render(
        request,
        state,
        "settings.html",
        store,
        cvs=cvs.list_cvs(master),
        master=master,
        files=files,
        upload_types=", ".join(cvs.UPLOAD_TYPES),
        max_mb=cvs.MAX_UPLOAD_BYTES // 1024 // 1024,
    )


@router.post("/settings/cvs", dependencies=[Depends(require_htmx)])
def upload_cv(request: Request, state: State, file: Annotated[UploadFile, File()]) -> Response:
    data = file.file.read(cvs.MAX_UPLOAD_BYTES + 1)
    try:
        text = cvs.convert_upload(file.filename or "cv", data)
    except cvs.CvError as exc:
        return _result(request, state, False, str(exc))
    master = state.config.cv_path
    name = cvs.free_name(master, file.filename or "cv")
    cvs.save_cv(master, name, text)
    return htmx_redirect(f"/settings/cvs/{name}?uploaded=1")


def _cv_file(state: WebState, name: str) -> Path:
    try:
        path = cvs.cv_path(state.config.cv_path, name)
    except cvs.CvError:
        raise HTTPException(404) from None
    if not path.is_file():
        raise HTTPException(404)
    return path


@router.get("/settings/cvs/{name}", response_class=HTMLResponse)
def cv_page(
    request: Request, name: str, state: State, store: ReadStore, uploaded: bool = False
) -> HTMLResponse:
    path = _cv_file(state, name)
    return render(
        request,
        state,
        "cv.html",
        store,
        cv_name=name,
        text=path.read_text(),
        is_master=path.resolve() == state.config.cv_path.resolve(),
        uploaded=uploaded,
    )


@router.post("/settings/cvs/{name}", dependencies=[Depends(require_htmx)])
def save_cv(
    request: Request, name: str, state: State, text: Annotated[str, Form()] = ""
) -> HTMLResponse:
    path = _cv_file(state, name)
    if not text.strip():
        return _result(request, state, False, "The CV is empty; nothing was saved.")
    cvs.save_cv(state.config.cv_path, name, text)
    details = []
    if path.resolve() == state.config.cv_path.resolve():
        details.append("This is the master CV: every open job is re-ranked on the next run.")
    return _result(request, state, True, "Saved.", details)


@router.post("/settings/cvs/{name}/master", dependencies=[Depends(require_htmx)])
def make_master(name: str, state: State) -> Response:
    _cv_file(state, name)
    cvs.make_master(state.config.cv_path, name, state.backup_dir / "cvs")
    return htmx_redirect("/settings?master=1")


@router.post("/settings/cvs/{name}/delete", dependencies=[Depends(require_htmx)])
def delete_cv(request: Request, name: str, state: State) -> Response:
    _cv_file(state, name)
    try:
        cvs.delete_cv(state.config.cv_path, name)
    except cvs.CvError as exc:
        return _result(request, state, False, str(exc))
    return htmx_redirect("/settings")


def _config_file(key: str) -> settings.ConfigFile:
    file = settings.FILES.get(key)
    if file is None:
        raise HTTPException(404)
    return file


@router.get("/settings/files/{key}", response_class=HTMLResponse)
def config_file_page(request: Request, key: str, state: State, store: ReadStore) -> HTMLResponse:
    file = _config_file(key)
    path = file.path(state.config, state.config_path)
    exists = path.is_file()
    return render(
        request,
        state,
        "config_file.html",
        store,
        file=file,
        path=path,
        exists=exists,
        text=path.read_text() if exists else settings.example_text(file),
        example=settings.example_text(file),
    )


def _check(request: Request, state: WebState, key: str, text: str, write: bool) -> HTMLResponse:
    file = _config_file(key)
    path = file.path(state.config, state.config_path)
    model, errors = settings.parse(file, text)
    if model is None:
        return _result(
            request, state, False, "Not saved: fix these first." if write else "Invalid:", errors
        )
    old = path.read_text() if path.is_file() else ""
    notes = settings.effects(file, old, model)
    if not write:
        return _result(
            request, state, True, "Valid. Saving it would:" if notes else "Valid.", notes
        )
    backup = settings.save(path, text, state.backup_dir)
    if key == "config":
        state.reload_config()
    if backup is not None:
        notes.append(f"The previous version is in {backup}.")
    return _result(request, state, True, f"Saved {path.name}.", notes)


@router.post("/settings/files/{key}/check", dependencies=[Depends(require_htmx)])
def check_config_file(
    request: Request, key: str, state: State, text: Annotated[str, Form()] = ""
) -> HTMLResponse:
    return _check(request, state, key, text, write=False)


@router.post("/settings/files/{key}", dependencies=[Depends(require_htmx)])
def save_config_file(
    request: Request, key: str, state: State, text: Annotated[str, Form()] = ""
) -> HTMLResponse:
    return _check(request, state, key, text, write=True)


# --- chat with Claude Code ----------------------------------------------------------


def require_chat_host(request: Request) -> None:
    """Refuse chat requests addressed to a public-looking name (DNS rebinding)."""
    allowed = _state(request).config.web.allowed_hosts
    if not chat_module.host_allowed(request.headers.get("host", ""), allowed):
        raise HTTPException(
            403,
            f"The chat doesn't accept requests for host {request.headers.get('host')!r}. "
            "Open the page by IP address, or add the name to web.allowed_hosts in config.yaml.",
        )


ChatGuards = [Depends(require_htmx), Depends(require_chat_host)]


@router.post("/chat/send", dependencies=ChatGuards)
def chat_send(
    state: State,
    message: Annotated[str, Form()] = "",
    chat_id: Annotated[str, Form()] = "",
) -> JSONResponse:
    try:
        new_id, _ = state.chat.start(chat_id or None, message)
    except chat_module.ChatUnavailable as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    except chat_module.ChatBusy as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    return JSONResponse({"chat_id": new_id})


@router.get("/chat/{chat_id}/stream", dependencies=[Depends(require_chat_host)])
def chat_stream(chat_id: str, state: State, after: int = 0) -> StreamingResponse:
    """Server-Sent Events for the chat's current run, from event number `after`."""
    run = state.chat.run_for(chat_id)

    def events() -> Iterator[str]:
        if run is None:
            yield f"data: {json.dumps({'type': 'done', 'error': None})}\n\n"
            return
        index, idle = max(after, 0), 0.0
        while True:
            fresh, done = run.since(index)
            for event in fresh:
                index += 1
                yield f"id: {index}\ndata: {json.dumps(event)}\n\n"
            if done and not fresh:
                return
            if not fresh:
                time.sleep(0.25)
                idle += 0.25
                if idle >= 15:
                    yield ": keepalive\n\n"  # keeps proxies from closing a quiet stream
                    idle = 0.0

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/chat/{chat_id}/cancel", dependencies=ChatGuards)
def chat_cancel(chat_id: str, state: State) -> JSONResponse:
    return JSONResponse({"stopped": state.chat.cancel(chat_id)})


@router.post("/chat/{chat_id}/delete", dependencies=ChatGuards)
def chat_delete(chat_id: str, state: State, store: WriteStore) -> Response:
    run = state.chat.run_for(chat_id)
    if run is not None and not run.done:
        raise HTTPException(409, "Stop the running reply first.")
    store.delete_chat(chat_id)
    return htmx_redirect("/?chat=new")


# --- structured settings forms (ranking.yaml, companies.yaml) -----------------------


def _file_data(state: WebState, key: str) -> tuple[settings.ConfigFile, Path, str]:
    file = settings.FILES[key]
    path = file.path(state.config, state.config_path)
    return file, path, path.read_text() if path.is_file() else ""


@router.get("/settings/ranking", response_class=HTMLResponse)
def ranking_form(request: Request, state: State, store: ReadStore) -> HTMLResponse:
    file, path, text = _file_data(state, "ranking")
    model, errors = settings.parse(file, text)
    return render(
        request,
        state,
        "ranking_form.html",
        store,
        rc=model,
        errors=errors,
        path=path,
        exists=path.is_file(),
    )


@router.get("/settings/companies", response_class=HTMLResponse)
def companies_form(request: Request, state: State, store: ReadStore) -> HTMLResponse:
    from collections import Counter

    from jobsearcher.sources.ats import FETCHERS
    from jobsearcher.sources.ats.common import feed_source

    file, path, text = _file_data(state, "companies")
    model, errors = settings.parse(file, text)
    detected = store.company_ats()
    open_jobs = Counter(
        s.source for job in store.iter_jobs() for s in job.sources if ":" in s.source
    )
    rows = []
    for company in model.companies if model else []:  # type: ignore[attr-defined]
        row = detected.get(company.slug)
        ats = company.ats.type if company.ats else (row["ats_type"] if row else None)
        ref = company.ats.ref if company.ats else (row["ats_ref"] if row else None)
        rows.append(
            {
                "company": company,
                "ats": ats,
                "ref": ref,
                "supported": ats in FETCHERS,
                "error": row["error"] if row and not ats else None,
                "checked": row["checked_at"][:10] if row else None,
                "jobs": open_jobs.get(feed_source(ats, ref), 0) if ats and ref else 0,
            }
        )
    return render(
        request,
        state,
        "companies_form.html",
        store,
        rows=rows,
        errors=errors,
        path=path,
        exists=path.is_file(),
        ats_types=list(FETCHERS),
    )


async def _submit_form(request: Request, key: str, write: bool) -> HTMLResponse:
    """Validate (and with `write`, save) a structured settings form."""
    require_htmx(request)
    state = _state(request)
    # ~12 fields per company: Starlette's default of 1,000 fields covers only ~80.
    form = await request.form(max_fields=20_000)
    file, path, old_text = _file_data(state, key)
    try:
        old = yaml.safe_load(old_text) if old_text.strip() else {}
    except yaml.YAMLError:
        return _result(
            request, state, False, f"{path.name} has a YAML error; fix it in the YAML editor first."
        )
    old = old if isinstance(old, dict) else {}
    try:
        data = forms.ranking_data(form, old) if key == "ranking" else forms.companies_data(form)
    except forms.FormErrors as exc:
        return _result(request, state, False, "Not saved: fix these first.", exc.errors)
    new_text = settings.merge_yaml(old_text, data)
    model, errors = settings.parse(file, new_text)
    if model is None:
        return _result(request, state, False, "Not saved: fix these first.", errors)
    notes = settings.effects(file, old_text, model)
    if new_text == old_text:
        return _result(request, state, True, "No changes.")
    if not write:
        return _result(request, state, True, "Valid. Saving would:" if notes else "Valid.", notes)
    backup = settings.save(path, new_text, state.backup_dir)
    if backup is not None:
        notes.append(f"The previous version is in {backup}.")
    return _result(request, state, True, f"Saved {path.name}.", notes)


@router.post("/settings/ranking", response_class=HTMLResponse)
async def save_ranking_form(request: Request) -> HTMLResponse:
    return await _submit_form(request, "ranking", write=True)


@router.post("/settings/ranking/check", response_class=HTMLResponse)
async def check_ranking_form(request: Request) -> HTMLResponse:
    return await _submit_form(request, "ranking", write=False)


@router.post("/settings/companies", response_class=HTMLResponse)
async def save_companies_form(request: Request) -> HTMLResponse:
    return await _submit_form(request, "companies", write=True)


@router.post("/settings/companies/check", response_class=HTMLResponse)
async def check_companies_form(request: Request) -> HTMLResponse:
    return await _submit_form(request, "companies", write=False)


def create_app(config: Config, config_path: Path | None = None) -> FastAPI:
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
        config_path=config_path or config_file_path(),
        chat=chat_module.ChatManager(config.chat, config.db_path, config.web.user_name),
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
    return create_app(load_config(), config_file_path())
