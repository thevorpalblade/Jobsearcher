"""FastAPI app: factory, lifespan, per-request store connections, routes, Jinja filters.

Routes are plain `def`s, so FastAPI runs them in its threadpool. Each request gets
its own SQLite connection (see `get_store`), read-only unless it writes.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode, urlsplit
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
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from jobsearcher import chat as chat_module
from jobsearcher import cvs, settings
from jobsearcher.auth import (
    MIN_PASSWORD,
    SESSION_MAX,
    Auth,
    AuthError,
    User,
    check_new_password,
    totp_uri,
)
from jobsearcher.companies.config import Company, load_companies
from jobsearcher.config import (
    KEY_ENV,
    Config,
    Provider,
    config_file_path,
    load_config,
    load_profile_settings,
    save_secret,
)
from jobsearcher.contacts import is_generic_email
from jobsearcher.contacts import service as contacts_service
from jobsearcher.contacts.links import search_links
from jobsearcher.drafting import service as draft_service
from jobsearcher.drafting.core import Draft
from jobsearcher.drafting.manager import DraftManager, ProfileDrafts
from jobsearcher.models import Application, ApplicationState, JobStatus
from jobsearcher.ranking.config import RankingConfig
from jobsearcher.ranking.ranker import Ranking, job_details
from jobsearcher.store import Store
from jobsearcher.web import forms, views

WEB_DIR = Path(__file__).parent


@dataclass
class Shared:
    """What every profile's pages share: config.yaml, templates, the chat (admin only)
    and the draft queue. Each profile gets its own WebState, made on first use."""

    base: Config  # config.yaml: read at startup and after it's saved on /settings
    config_path: Path
    templates: Jinja2Templates
    tz: ZoneInfo
    chat: chat_module.ChatManager
    draft_manager: DraftManager
    contact_manager: DraftManager  # contact lookups, one at a time, apart from drafts
    states: dict[str, WebState] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def state_for(self, slug: str | None) -> WebState:
        """A profile's state (an unknown or missing profile: the first one)."""
        slugs = self.base.profile_slugs()
        slug = slug if slug in slugs else slugs[0]
        with self.lock:
            state = self.states.get(slug)
            if state is None:
                state = self.states[slug] = WebState(self, self.base.for_profile(slug))
            return state

    def reload(self) -> None:
        """Pick up a saved config.yaml or profile.yaml (paths to ranking.yaml and the CVs
        may change) for every profile."""
        self.base = load_config(self.config_path)
        self.chat.config = self.base.chat
        self.chat.user_name = self.base.web.user_name
        slugs = self.base.profile_slugs()
        with self.lock:
            for slug, state in list(self.states.items()):
                if slug in slugs:
                    state.use_config(self.base.for_profile(slug))
                else:
                    del self.states[slug]


class WebState:
    """One profile's view of the app: its config and its cached ranking.yaml and CVs."""

    def __init__(self, shared: Shared, config: Config):
        self.shared = shared
        self.memo = views.PrefilterMemo()
        self.drafts = ProfileDrafts(shared.draft_manager, lambda: self.config)
        self.contacts = ProfileDrafts(shared.contact_manager, lambda: self.config)
        self.use_config(config)

    def use_config(self, config: Config) -> None:
        self.config = config
        self.ranking: views.MtimeCache[RankingConfig] = views.ranking_config_cache(
            config.ranking_config
        )
        self.cv: views.MtimeCache[str | None] = views.cv_cache(config.cv_path)

    @property
    def config_path(self) -> Path:
        return self.shared.config_path

    @property
    def templates(self) -> Jinja2Templates:
        return self.shared.templates

    @property
    def tz(self) -> ZoneInfo:
        return self.shared.tz

    @property
    def chat(self) -> chat_module.ChatManager:
        return self.shared.chat

    def reload_config(self) -> None:
        self.shared.reload()

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


def current_user(request: Request) -> User | None:
    """The logged-in user (set by the login middleware; None on public pages)."""
    return getattr(request.state, "user", None)


def _state(request: Request) -> WebState:
    """The state of the profile the logged-in user sees."""
    user = current_user(request)
    shared: Shared = request.app.state.web
    if user is not None and not user.is_admin and user.profile not in shared.base.profile_slugs():
        raise HTTPException(403, "Your account has no candidate profile yet; ask the admin.")
    return shared.state_for(user.profile if user else None)


def is_public(request: Request) -> bool:
    """Did the request come in on the internet-facing listener (through Caddy)? Decided
    by the local port it arrived on, which a client can't forge, unlike a header."""
    port = request.app.state.web.base.web.public_port
    server = request.scope.get("server")
    return port is not None and server is not None and server[1] == port


def require_lan(request: Request) -> None:
    """The chat runs Claude Code with full permissions: never from the internet."""
    if is_public(request):
        raise HTTPException(404)


def require_admin(request: Request) -> None:
    user = current_user(request)
    if user is None or not user.is_admin:
        raise HTTPException(404)  # don't reveal admin pages to others


State = Annotated[WebState, Depends(_state)]


def get_store(state: State) -> Iterator[Store]:
    """A read-only connection for one request. check_same_thread=False because FastAPI
    may run this dependency's setup, the endpoint and the teardown on different
    threadpool threads; the request still uses the connection serially."""
    store = Store(
        state.config.db_path,
        readonly=True,
        check_same_thread=False,
        profile=state.config.profile,
    )
    try:
        yield store
    finally:
        store.close()


def get_writable_store(state: State) -> Iterator[Store]:
    """A writable connection for one POST; the schema already exists (see lifespan)."""
    store = Store(
        state.config.db_path,
        check_same_thread=False,
        init_schema=False,
        profile=state.config.profile,
    )
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
    templates.env.filters["markdown"] = views.render_markdown
    templates.env.filters["localdate"] = local
    templates.env.filters["localtime"] = lambda v: local(v, "%Y-%m-%d %H:%M")
    templates.env.filters["usd"] = lambda v: f"${v:,.2f}"
    templates.env.filters["safe_url"] = views.safe_url
    templates.env.filters["provenance"] = views.provenance_label
    templates.env.tests["generic_email"] = is_generic_email
    templates.env.globals["contact_key"] = contacts_service.contact_key
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
    user = current_user(request)
    if user is not None and user.is_admin:  # the nav's profile switcher
        context.setdefault("profiles", request.app.state.web.base.profile_slugs())
    context = {"config": state.config, "user": user, **context}
    return state.templates.TemplateResponse(request, name, context, status_code=status_code)


def is_htmx(request: Request) -> bool:
    """A partial-page request from HTMX (a history restore wants the full page)."""
    return "HX-Request" in request.headers and "HX-History-Restore-Request" not in request.headers


router = APIRouter()


@router.get("/healthz", response_class=PlainTextResponse)
def healthz(store: ReadStore) -> str:
    store.count_jobs()  # fails if the database can't be read
    return "ok"


def _draft_chips(state: WebState, store: Store) -> dict[str, str]:
    """Per job or company key: "ready", "review" (needs review) or "working"."""
    chips: dict[str, str] = {}
    for key, row in store.latest_drafts().items():
        chips[key] = "review" if Draft.model_validate_json(row["data"]).needs_review else "ready"
    for key, status in state.drafts.items():
        if status.active:
            chips[key] = "working"
    return chips


def _spontaneous_suggestions(state: WebState, store: Store, limit: int = 4) -> list[Any]:
    """Companies with a fresh, relevant news signal she could write to unprompted."""
    from jobsearcher.signals.run import digest

    path = state.config.companies_config
    if not path.is_file():
        return []
    return digest(store, load_companies(path), state.config.companies.news_days, 50)[:limit]


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request, state: State, store: ReadStore, chat: str | None = None
) -> HTMLResponse:
    ctx = state.row_context()
    rows = views.load_rows(store, ctx, "all")
    user = current_user(request)
    # The chat runs Claude Code with full permissions on this machine: admin only, and
    # only on the home network.
    chat_allowed = may_chat(request)
    chats = []
    if chat_allowed and user is not None:  # each person sees only their own chats
        chats = store.list_chats(owner=user.username, with_unowned=user.is_admin)
    current = None
    if chat != "new" and chats:
        mine = {c["id"]: c for c in chats}
        current = mine.get(chat) if chat else chats[0]
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
        drafts=_draft_chips(state, store),
        companies=_spontaneous_suggestions(state, store),
        stats=views.dashboard_stats(rows, store.last_runs()),
        ranking=ctx.config,
        chats=chats,
        current=current,
        messages=messages,
        running_after=after,
        chat_problem=state.chat.unavailable(),
        suggestions=chat_module.SUGGESTIONS,
        chat_allowed=chat_allowed,
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


@router.get("/status", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
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
        **_draft_context(state, store, job_id, f"/jobs/{job_id}"),
        **_contacts_context(
            state, store, job_id, f"/jobs/{job_id}", record.job, _job_role(store, job_id)
        ),
    )


def require_htmx(request: Request) -> None:
    """Writes must come from HTMX, one of two CSRF guards (with _same_origin_writes): a custom
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
    if new_state == ApplicationState.SHORTLISTED:
        _draft_on_shortlist(state, store, job_id)
    return _tracking_response(request, state, store, job_id, app)


def _draft_on_shortlist(state: WebState, store: Store, job_id: str) -> None:
    """Shortlisting shows interest: start a draft unless one exists or is on its way, or
    today's automatic drafts have used up the cap."""
    settings_ = state.ranking.get().drafting
    if not settings_.auto_on_shortlist or store.list_drafts(job_id):
        return
    status = state.drafts.status(job_id)
    if status is not None and status.active:
        return
    midnight = datetime.now(state.tz).replace(hour=0, minute=0, second=0, microsecond=0)
    if store.auto_drafts_since(midnight) + state.drafts.pending() >= settings_.max_drafts_per_day:
        return
    state.drafts.submit(
        job_id,
        lambda config, st: draft_service.draft_job(config, st, job_id, trigger="shortlist"),
    )


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
    user = current_user(request)
    files = [
        (f, p)
        for f, p in settings.available(state.config, state.config_path)
        if f.key not in ADMIN_FILES or (user and user.is_admin)
    ]
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
    _cv_file(state, name)  # validates the name
    if not text.strip():
        return _result(request, state, False, "The CV is empty; nothing was saved.")
    cvs.save_cv(state.config.cv_path, name, text)
    details = ["Ranking reads every CV: every open job is re-ranked on the next run."]
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


# config.yaml is the whole system's (sources, models, schedule): the admin's to edit.
ADMIN_FILES = {"config"}


def _config_file(
    state: WebState, key: str, user: User | None = None
) -> tuple[settings.ConfigFile, Path]:
    file = settings.FILES.get(key)
    path = file.path(state.config, state.config_path) if file else None
    if file is None or path is None or (key in ADMIN_FILES and not (user and user.is_admin)):
        raise HTTPException(404)
    return file, path


@router.get("/settings/files/{key}", response_class=HTMLResponse)
def config_file_page(request: Request, key: str, state: State, store: ReadStore) -> HTMLResponse:
    file, path = _config_file(state, key, current_user(request))
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
    file, path = _config_file(state, key, current_user(request))
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
    if key in ("config", "profile"):
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


# --- application drafts --------------------------------------------------------------

DRAFT_FILES = {"cv.md", "cv.docx", "cv.pdf", "letter.md", "letter.docx", "letter.pdf"}


def _draft_context(
    state: WebState, store: Store, key: str, base: str, version: str | None = None
) -> dict[str, Any]:
    """What the draft panel shows for a job or company: the chosen version, the
    others, whether one is being written, and the CVs a draft can start from."""
    drafts = [Draft.model_validate_json(r["data"]) for r in store.list_drafts(key)]
    current = next((d for d in drafts if d.input_hash == version), None) or (
        drafts[0] if drafts else None
    )
    return {
        "draft_key": key,
        "draft_base": base,
        "draft": current,
        "draft_versions": drafts,
        "draft_status": state.drafts.status(key),
        "draft_cvs": cvs.list_cvs(state.config.cv_path),
        "draft_files_ok": True,
    }


def _draft_panel(request: Request, state: WebState, store: Store, key: str, base: str,
                 version: str | None = None) -> HTMLResponse:  # fmt: skip
    return render(request, state, "_draft.html", **_draft_context(state, store, key, base, version))


def _download_name(draft: Draft, label: str, name: str, ext: str) -> str:
    candidate = draft.cv.splitlines()[0].lstrip("# ").split(",")[0] if draft.cv else "Application"
    parts = [candidate, label, "CV" if name.startswith("cv") else "Cover letter"]
    return re.sub(r"[^A-Za-z0-9åäöÅÄÖ._-]+", "-", "-".join(parts)).strip("-") + "." + ext


def _draft_file(state: WebState, store: Store, key: str, label: str, version: str, name: str):  # type: ignore[no-untyped-def]
    row = store.get_draft(key, version)
    if row is None or name not in DRAFT_FILES:
        raise HTTPException(404)
    draft = Draft.model_validate_json(row["data"])
    path = draft_service.drafts_dir(state.config) / draft_service._safe_key(key) / version / name
    if name not in draft.files or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, filename=_download_name(draft, label, name, path.suffix[1:]))


# --- contact people (docs/m5-contacts.md) ---------------------------------------------


def _contacts_context(
    state: WebState, store: Store, key: str, base: str, job: Any = None, role: str | None = None
) -> dict[str, Any]:
    found = contacts_service.load_lookup(store, key)
    company = job.company if job is not None else None
    if company is None and key.startswith(contacts_service.COMPANY_PREFIX):
        slug = key.removeprefix(contacts_service.COMPANY_PREFIX)
        company = _company(state, slug).name
    return {
        "contacts_base": base,
        "contacts_status": state.contacts.status(key),
        "found": found,
        "chosen": found.chosen if found else None,
        "ad_contacts": list(job.contacts) if job is not None else [],
        "links": search_links(company, role, found.domain if found else None),
    }


def _job_role(store: Store, job_id: str) -> str | None:
    latest = store.latest_ranking(job_id)
    if latest is None:
        return None
    try:
        return Ranking.model_validate_json(latest[0]).assessment.matched_role
    except ValueError:
        return None


def _contacts_panel(request: Request, state: WebState, store: Store, key: str) -> HTMLResponse:
    if key.startswith(contacts_service.COMPANY_PREFIX):
        slug = key.removeprefix(contacts_service.COMPANY_PREFIX)
        context = _contacts_context(state, store, key, f"/companies/{slug}")
    else:
        job = store.get_job(key)
        if job is None:
            raise HTTPException(404)
        context = _contacts_context(state, store, key, f"/jobs/{key}", job, _job_role(store, key))
    return render(request, state, "_contacts.html", **context)


def _start_lookup(state: WebState, key: str) -> None:
    if key.startswith(contacts_service.COMPANY_PREFIX):
        company = _company(state, key.removeprefix(contacts_service.COMPANY_PREFIX))
        state.contacts.submit(
            key, lambda config, st: contacts_service.for_company(config, st, company, force=True)
        )
    else:
        state.contacts.submit(
            key, lambda config, st: contacts_service.for_job(config, st, key, force=True)
        )


@router.get("/jobs/{job_id}/contacts", response_class=HTMLResponse)
def job_contacts_panel(
    request: Request, job_id: str, state: State, store: ReadStore
) -> HTMLResponse:
    return _contacts_panel(request, state, store, job_id)


@router.get("/companies/{slug}/contacts", response_class=HTMLResponse)
def company_contacts_panel(
    request: Request, slug: str, state: State, store: ReadStore
) -> HTMLResponse:
    _company(state, slug)
    return _contacts_panel(request, state, store, contacts_service.COMPANY_PREFIX + slug)


def _contacts_key(state: WebState, store: Store, kind: str, ident: str) -> str:
    if kind == "companies":
        _company(state, ident)
        return contacts_service.COMPANY_PREFIX + ident
    if store.get_job(ident) is None:
        raise HTTPException(404)
    return ident


@router.post(
    "/{kind}/{ident}/contacts",
    response_class=HTMLResponse,
    dependencies=[Depends(require_htmx)],
)
def contacts_start(
    request: Request, kind: str, ident: str, state: State, store: ReadStore
) -> HTMLResponse:
    if kind not in ("jobs", "companies"):
        raise HTTPException(404)
    key = _contacts_key(state, store, kind, ident)
    _start_lookup(state, key)
    return _contacts_panel(request, state, store, key)


@router.post(
    "/{kind}/{ident}/contacts/choose",
    response_class=HTMLResponse,
    dependencies=[Depends(require_htmx)],
)
def contacts_choose(
    request: Request,
    kind: str,
    ident: str,
    state: State,
    store: WriteStore,
    key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    if kind not in ("jobs", "companies"):
        raise HTTPException(404)
    job_key = _contacts_key(state, store, kind, ident)
    store.choose_contact(job_key, key[:500] or None)
    return _contacts_panel(request, state, store, job_key)


@router.post(
    "/{kind}/{ident}/contacts/site",
    response_class=HTMLResponse,
    dependencies=[Depends(require_htmx)],
)
def contacts_site(
    request: Request,
    kind: str,
    ident: str,
    state: State,
    store: WriteStore,
    domain: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """The user's correction of the company's website; then look again with it."""
    from urllib.parse import urlsplit

    from jobsearcher.contacts.site import is_company_host, registered_domain

    if kind not in ("jobs", "companies"):
        raise HTTPException(404)
    job_key = _contacts_key(state, store, kind, ident)
    if kind == "companies":
        company_name = _company(state, ident).name
    else:
        company_name = store.get_job(ident).company or ""  # type: ignore[union-attr]
    raw = domain.strip()
    host = urlsplit(raw if "//" in raw else f"//{raw}").hostname or ""
    if not company_name or not host or not is_company_host(host):
        return _result(request, state, False, f"“{raw}” doesn't look like a company website.")
    contacts_service.set_site(store, company_name, registered_domain(host))
    _start_lookup(state, job_key)
    return _contacts_panel(request, state, store, job_key)


@router.get("/jobs/{job_id}/draft", response_class=HTMLResponse)
def job_draft_panel(
    request: Request, job_id: str, state: State, store: ReadStore, v: str | None = None
) -> HTMLResponse:
    return _draft_panel(request, state, store, job_id, f"/jobs/{job_id}", v)


@router.post(
    "/jobs/{job_id}/draft", response_class=HTMLResponse, dependencies=[Depends(require_htmx)]
)
def job_draft_start(
    request: Request,
    job_id: str,
    state: State,
    store: ReadStore,
    instructions: Annotated[str, Form()] = "",
    base_cv: Annotated[str, Form()] = "",
) -> HTMLResponse:
    if store.get_job(job_id) is None:
        raise HTTPException(404)
    state.drafts.submit(
        job_id,
        lambda config, st: draft_service.draft_job(
            config, st, job_id, instructions.strip(), base_cv or None, force=True
        ),
    )
    return _draft_panel(request, state, store, job_id, f"/jobs/{job_id}")


@router.get("/jobs/{job_id}/draft/{version}/{name}")
def job_draft_file(job_id: str, version: str, name: str, state: State, store: ReadStore):  # type: ignore[no-untyped-def]
    job = store.get_job(job_id)
    if job is None:
        raise HTTPException(404)
    return _draft_file(state, store, job_id, job.company or "Job", version, name)


def _company(state: WebState, slug: str) -> Company:
    for company in load_companies(state.config.companies_config):
        if company.slug == slug:
            return company
    raise HTTPException(404)


@router.get("/companies/{slug}", response_class=HTMLResponse)
def company_page(request: Request, slug: str, state: State, store: ReadStore) -> HTMLResponse:
    company = _company(state, slug)
    since = datetime.now(UTC) - timedelta(days=draft_service.SIGNAL_DAYS)
    signals = [r for r in store.signals_since(since) if r["company"] == slug]
    open_jobs = [
        j for j in store.iter_jobs() if (j.company or "").casefold() == company.name.casefold()
    ]
    key = draft_service.COMPANY_PREFIX + slug
    return render(
        request,
        state,
        "company.html",
        store,
        company=company,
        signals=[s for s in signals if s["kind"] != "not_about_company"],
        open_jobs=open_jobs,
        **_draft_context(state, store, key, f"/companies/{slug}"),
        **_contacts_context(
            state, store, contacts_service.COMPANY_PREFIX + slug, f"/companies/{slug}"
        ),
    )


@router.get("/companies/{slug}/draft", response_class=HTMLResponse)
def company_draft_panel(
    request: Request, slug: str, state: State, store: ReadStore, v: str | None = None
) -> HTMLResponse:
    _company(state, slug)
    return _draft_panel(
        request, state, store, draft_service.COMPANY_PREFIX + slug, f"/companies/{slug}", v
    )


@router.post(
    "/companies/{slug}/draft", response_class=HTMLResponse, dependencies=[Depends(require_htmx)]
)
def company_draft_start(
    request: Request,
    slug: str,
    state: State,
    store: ReadStore,
    instructions: Annotated[str, Form()] = "",
    base_cv: Annotated[str, Form()] = "",
) -> HTMLResponse:
    company = _company(state, slug)
    key = draft_service.COMPANY_PREFIX + slug
    state.drafts.submit(
        key,
        lambda config, st: draft_service.draft_company(
            config, st, company, instructions.strip(), base_cv or None, force=True
        ),
    )
    return _draft_panel(request, state, store, key, f"/companies/{slug}")


@router.get("/companies/{slug}/draft/{version}/{name}")
def company_draft_file(slug: str, version: str, name: str, state: State, store: ReadStore):  # type: ignore[no-untyped-def]
    company = _company(state, slug)
    return _draft_file(
        state, store, draft_service.COMPANY_PREFIX + slug, company.name, version, name
    )


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


def may_chat(request: Request) -> bool:
    """Admins, and the accounts in chat.users, on the home network only."""
    user = current_user(request)
    if user is None or is_public(request):
        return False
    allowed = {name.casefold() for name in request.app.state.web.base.chat.users}
    return user.is_admin or user.username.casefold() in allowed


def require_chat_user(request: Request) -> None:
    if not may_chat(request):
        raise HTTPException(404)


def _chat_user(request: Request, state: WebState) -> chat_module.ChatUser:
    user = current_user(request)
    assert user is not None
    return chat_module.ChatUser(
        username=user.username,
        name=state.config.web.user_name if not user.is_admin else user.username.title(),
        profile=user.profile,
        is_admin=user.is_admin,
    )


def _own_chat(request: Request, state: WebState, chat_id: str) -> None:
    """404 unless the chat is the logged-in user's (or, for admins, from before logins)."""
    store = Store(state.config.db_path, readonly=True, check_same_thread=False)
    try:
        chat = store.get_chat(chat_id)
    finally:
        store.close()
    if chat is None or not _chat_user(request, state).may_open(chat):
        raise HTTPException(404)


ChatGuards = [
    Depends(require_lan),
    Depends(require_chat_user),
    Depends(require_htmx),
    Depends(require_chat_host),
]


@router.post("/chat/send", dependencies=ChatGuards)
def chat_send(
    request: Request,
    state: State,
    message: Annotated[str, Form()] = "",
    chat_id: Annotated[str, Form()] = "",
) -> JSONResponse:
    try:
        new_id, _ = state.chat.start(chat_id or None, message, _chat_user(request, state))
    except chat_module.ChatUnavailable as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    except chat_module.ChatBusy as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    return JSONResponse({"chat_id": new_id})


@router.get(
    "/chat/{chat_id}/stream",
    dependencies=[Depends(require_lan), Depends(require_chat_user), Depends(require_chat_host)],
)
def chat_stream(request: Request, chat_id: str, state: State, after: int = 0) -> StreamingResponse:
    """Server-Sent Events for the chat's current run, from event number `after`."""
    _own_chat(request, state, chat_id)
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
def chat_cancel(request: Request, chat_id: str, state: State) -> JSONResponse:
    _own_chat(request, state, chat_id)
    return JSONResponse({"stopped": state.chat.cancel(chat_id)})


@router.post("/chat/{chat_id}/delete", dependencies=ChatGuards)
def chat_delete(request: Request, chat_id: str, state: State, store: WriteStore) -> Response:
    _own_chat(request, state, chat_id)
    run = state.chat.run_for(chat_id)
    if run is not None and not run.done:
        raise HTTPException(409, "Stop the running reply first.")
    store.delete_chat(chat_id)
    return htmx_redirect("/?chat=new")


# --- structured settings forms (ranking.yaml, companies.yaml) -----------------------


def _file_data(state: WebState, key: str) -> tuple[settings.ConfigFile, Path, str]:
    file, path = _config_file(state, key)
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


# --- models and API keys (each profile's own; docs/m10-multi-user.md section 3a) ------


def _allowed_providers(config: Config) -> list[str]:
    return [p.value for p in Provider if config.provider_allowed(p)]


def _key_rows(config: Config) -> list[dict[str, Any]]:
    """Each provider's key: set or not, and its last 4 characters (never the key)."""
    rows = []
    for provider, env in KEY_ENV.items():
        key = config.api_keys.get(env)
        rows.append({"provider": provider.value, "env": env, "hint": key[-4:] if key else None})
    return rows


@router.get("/settings/models", response_class=HTMLResponse)
def models_page(request: Request, state: State, store: ReadStore) -> HTMLResponse:
    path = state.config.profile_file
    own = load_profile_settings(path).llm if path is not None else None
    return render(
        request,
        state,
        "models.html",
        store,
        own=own,
        effective=state.config.llm,
        base=state.shared.base.llm,
        providers=_allowed_providers(state.config),
        keys=_key_rows(state.config) if state.config.own_keys else None,
    )


@router.post("/settings/models", response_class=HTMLResponse)
async def save_models(request: Request) -> HTMLResponse:
    require_htmx(request)
    state = _state(request)
    path = state.config.profile_file
    if path is None:
        raise HTTPException(404)  # no profiles: models are in config.yaml
    form = await request.form()
    old_text = path.read_text() if path.is_file() else ""
    try:
        data = forms.models_data(form, set(_allowed_providers(state.config)))
    except forms.FormErrors as exc:
        return _result(request, state, False, "Not saved: fix these first.", exc.errors)
    new_text = settings.merge_yaml(old_text, data)
    file = settings.FILES["profile"]
    model, errors = settings.parse(file, new_text)
    if model is None:
        return _result(request, state, False, "Not saved: fix these first.", errors)
    if new_text == old_text:
        return _result(request, state, True, "No changes.")
    notes = settings.effects(file, old_text, model)
    backup = settings.save(path, new_text, state.backup_dir)
    state.reload_config()
    if backup is not None:
        notes.append(f"The previous version is in {backup}.")
    return _result(request, state, True, "Saved.", notes)


@router.post("/settings/keys", dependencies=[Depends(require_htmx)])
def save_key(
    request: Request,
    state: State,
    env: Annotated[str, Form()] = "",
    key: Annotated[str, Form()] = "",
    remove: Annotated[str, Form()] = "",
) -> Response:
    path = state.config.secrets_file
    if path is None:
        raise HTTPException(404)  # this profile uses the server's keys
    try:
        save_secret(path, env, None if remove else key)
    except ValueError as exc:
        return _result(request, state, False, str(exc))
    user = current_user(request)
    with _auth(request) as auth:
        action = "removed" if remove else "set"
        auth.audit(
            "api_key_" + action, user.username if user else "", f"{state.config.profile} {env}"
        )
    state.reload_config()
    return htmx_redirect("/settings/models")


@router.post(
    "/settings/models/test", response_class=HTMLResponse, dependencies=[Depends(require_htmx)]
)
def test_models(request: Request, state: State, store: WriteStore) -> HTMLResponse:
    """One tiny request to each model, like `jobsearcher llm-check` (a fraction of a cent)."""
    from pydantic import BaseModel

    from jobsearcher.llm import BudgetTracker, LLMError, make_llm

    class Pong(BaseModel):
        reply: str

    tracker = BudgetTracker(store, state.config.llm)
    lines, ok = [], True
    for role in forms.MODEL_ROLES:
        spec = getattr(state.config.llm, role)
        try:
            result = make_llm(state.config, role, tracker).complete(  # type: ignore[arg-type]
                system="You are a connectivity check.",
                prompt='Reply with {"reply": "pong"}.',
                schema=Pong,
            )
        except LLMError as exc:
            ok = False
            lines.append(f"{role} ({spec.provider} / {spec.model}): failed: {exc}")
            continue
        usage = result.usage
        cost = f"${tracker.cost(usage):.5f}" if usage.billed else "subscription"
        lines.append(f"{role} ({spec.provider} / {spec.model}): works ({cost})")
    return _result(request, state, ok, "Both models work." if ok else "A model failed:", lines)


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


# --- logging in (jobsearcher/auth.py) -------------------------------------------------

SESSION_COOKIE = "jobsearcher_session"
PUBLIC_PATHS = {"/login", "/healthz"}
PUBLIC_PREFIXES = ("/static/", "/invite/")


@contextmanager
def _auth(request: Request) -> Iterator[Auth]:
    """Account tables on a connection of their own (request stores may be read-only)."""
    store = Store(request.app.state.web.base.db_path, check_same_thread=False, init_schema=False)
    try:
        yield Auth(store.conn)
    finally:
        store.close()


def _client_ip(request: Request) -> str:
    """The visitor's address. Through Caddy, the last X-Forwarded-For entry is the one
    Caddy added (the address it saw); earlier entries come from the client and are
    ignored. On the home network the header isn't trusted at all."""
    if is_public(request):
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.rsplit(",", 1)[-1].strip()
    return request.client.host if request.client else ""


def _session_user(request: Request, token: str) -> User | None:
    with _auth(request) as auth:
        return auth.session_user(token)


async def _login_required(request: Request, call_next: Any) -> Response:
    """Every page needs a logged-in user, except the login and invite pages, static
    files and the health check. Deny by default: a new route is private."""
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
        return await call_next(request)
    token = request.cookies.get(SESSION_COOKIE, "")
    user = await run_in_threadpool(_session_user, request, token) if token else None
    if user is None:
        here = path + (f"?{request.url.query}" if request.url.query else "")
        target = "/login?" + urlencode({"next": here})
        if "HX-Request" in request.headers:
            return Response(status_code=401, headers={"HX-Redirect": target})
        if request.method in ("GET", "HEAD"):
            return RedirectResponse(target, status_code=303)
        return PlainTextResponse("Log in first.", status_code=401)
    if user.is_admin and not user.has_totp and is_public(request):
        return PlainTextResponse(
            "Admin accounts need two-factor codes to work from the internet. Set them up "
            "on the Account page from the home network.",
            status_code=403,
        )
    request.state.user = user
    return await call_next(request)


SECURITY_HEADERS = {
    # No inline scripts anywhere (HTMX is vendored); inline styles are allowed for a
    # few style attributes and HTMX's indicator styles.
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
}


async def _security_headers(request: Request, call_next: Any) -> Response:
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


async def _same_origin_writes(request: Request, call_next: Any) -> Response:
    """Refuse writes another site's page sends. With a session cookie, the HX-Request
    check alone isn't enough. Browsers label every request with Sec-Fetch-Site, which
    pages can't change: only same-origin (or "none", typed by the user) may write.
    Without it (old browsers), Origin must match the Host. A form post's Origin can be
    "null" under a no-referrer policy, so Origin alone would refuse real logins."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        site = request.headers.get("sec-fetch-site")
        origin = request.headers.get("origin")
        if site is not None:
            refused = site not in ("same-origin", "none")
        else:
            refused = origin not in (None, "null") and (
                urlsplit(origin).netloc != request.headers.get("host")
            )
        if refused:
            return PlainTextResponse("Cross-site request refused.", status_code=403)
    return await call_next(request)


def _safe_next(target: str | None) -> str:
    """Only redirect within this site after logging in."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"


def _start_session(request: Request, response: Response, auth: Auth, user: User) -> None:
    token = auth.create_session(user, _client_ip(request), request.headers.get("user-agent", ""))
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(SESSION_MAX.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https" or is_public(request),
        path="/",
    )


def _public_state(request: Request) -> WebState:
    return request.app.state.web.state_for(None)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/") -> HTMLResponse:
    return render(request, _public_state(request), "login.html", next=_safe_next(next))


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    code: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "/",
) -> Response:
    with _auth(request) as auth:
        try:
            user = auth.authenticate(username, password, _client_ip(request), code)
            if user.is_admin and not user.has_totp and is_public(request):
                raise AuthError(
                    "Admin accounts need two-factor codes to log in from the internet. "
                    "Set them up on the Account page from the home network."
                )
        except AuthError as exc:
            return render(
                request,
                _public_state(request),
                "login.html",
                status_code=401,
                error=str(exc),
                username=username,
                next=_safe_next(next),
            )
        response = RedirectResponse(_safe_next(next), status_code=303)
        _start_session(request, response, auth, user)
    return response


@router.post("/logout")
def logout(request: Request) -> Response:
    with _auth(request) as auth:
        auth.end_session(request.cookies.get(SESSION_COOKIE, ""))
        user = current_user(request)
        auth.audit("logout", user.username if user else "", ip=_client_ip(request))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/invite/{token}", response_class=HTMLResponse)
def invite_page(request: Request, token: str) -> HTMLResponse:
    with _auth(request) as auth:
        invitee = auth.user_by_invite(token)
    return render(
        request,
        _public_state(request),
        "invite.html",
        status_code=200 if invitee else 404,
        invitee=invitee,
        token=token,
        min_length=MIN_PASSWORD,
    )


@router.post("/invite/{token}", response_class=HTMLResponse)
def invite_set_password(
    request: Request,
    token: str,
    password: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
) -> Response:
    with _auth(request) as auth:
        invitee = auth.user_by_invite(token)
        try:
            if invitee is None:
                raise AuthError("This link has expired or has already been used.")
            check_new_password(password, confirm)
            auth.set_password(invitee, password, _client_ip(request))
        except AuthError as exc:
            return render(
                request,
                _public_state(request),
                "invite.html",
                status_code=400,
                invitee=invitee,
                token=token,
                error=str(exc),
                min_length=MIN_PASSWORD,
            )
        response = RedirectResponse("/", status_code=303)
        _start_session(request, response, auth, invitee)
    return response


def _account(
    request: Request, state: WebState, status_code: int = 200, **context: Any
) -> HTMLResponse:
    return render(
        request, state, "account.html", status_code=status_code, min_length=MIN_PASSWORD, **context
    )


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request, state: State, changed: bool = False) -> HTMLResponse:
    return _account(request, state, changed=changed)


@router.post("/account/2fa/start", response_class=HTMLResponse)
def totp_start(request: Request, state: State) -> HTMLResponse:
    """A new secret, shown as a QR code; it's switched on once a code from it is entered."""
    import segno

    user = current_user(request)
    assert user is not None
    with _auth(request) as auth:
        secret = auth.start_totp(user)
    qr = segno.make(totp_uri(secret, user.username), error="m")
    return _account(request, state, totp_secret=secret, totp_qr=qr.svg_inline(scale=5, border=2))


@router.post("/account/2fa/confirm", response_class=HTMLResponse)
def totp_confirm(request: Request, state: State, code: Annotated[str, Form()] = "") -> Response:
    user = current_user(request)
    assert user is not None
    with _auth(request) as auth:
        try:
            auth.confirm_totp(user, code, _client_ip(request))
        except AuthError as exc:
            return _account(request, state, 400, totp_error=str(exc))
    return RedirectResponse("/account?totp=on", status_code=303)


@router.post("/account/2fa/disable", response_class=HTMLResponse)
def totp_disable(
    request: Request,
    state: State,
    password: Annotated[str, Form()] = "",
    code: Annotated[str, Form()] = "",
) -> Response:
    user = current_user(request)
    assert user is not None
    with _auth(request) as auth:
        try:
            auth.disable_totp(user, password, code, _client_ip(request))
        except AuthError as exc:
            return _account(request, state, 400, totp_error=str(exc))
    return RedirectResponse("/account?totp=off", status_code=303)


@router.post("/account/password", response_class=HTMLResponse)
def change_password(
    request: Request,
    state: State,
    current: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
) -> Response:
    user = current_user(request)
    assert user is not None  # the middleware let the request through
    with _auth(request) as auth:
        try:
            check_new_password(password, confirm)
            auth.change_password(user, current, password, _client_ip(request))
        except AuthError as exc:
            return _account(request, state, 400, error=str(exc))
        # Changing the password ended every session, this one too: start a new one.
        response = RedirectResponse("/account?changed=1", status_code=303)
        _start_session(request, response, auth, user)
    return response


# --- admin: profile switcher and accounts ----------------------------------------------


@router.post("/admin/act-as", dependencies=[Depends(require_admin), Depends(require_htmx)])
def act_as(request: Request, profile: Annotated[str, Form()] = "") -> Response:
    """Show another candidate's profile in this admin session (to help them)."""
    if profile not in request.app.state.web.base.profile_slugs():
        raise HTTPException(400, f"No profile {profile!r}")
    with _auth(request) as auth:
        auth.act_as(request.cookies.get(SESSION_COOKIE, ""), profile)
        user = current_user(request)
        auth.audit("act_as", user.username if user else "", profile, _client_ip(request))
    return htmx_redirect("/")


def _users_page(request: Request, state: WebState, **context: Any) -> HTMLResponse:
    with _auth(request) as auth:
        users = auth.users()
    return render(
        request,
        state,
        "users.html",
        users=users,
        profiles=state.shared.base.profile_slugs(),
        **context,
    )


def _invite_url(request: Request, token: str) -> str:
    base = str(request.base_url).rstrip("/")
    if is_public(request):  # Caddy speaks plain HTTP to the app; visitors use HTTPS
        base = f"https://{request.headers.get('host', '')}"
    return f"{base}/invite/{token}"


@router.get("/admin/users", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def users_page(request: Request, state: State) -> HTMLResponse:
    return _users_page(request, state)


@router.post(
    "/admin/users",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin), Depends(require_htmx)],
)
def add_user(
    request: Request,
    state: State,
    username: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "user",
    profile: Annotated[str, Form()] = "",
) -> HTMLResponse:
    if profile not in state.shared.base.profile_slugs():
        return _users_page(request, state, error="Pick the candidate profile they'll see.")
    with _auth(request) as auth:
        try:
            _, token = auth.create_user(username, role, profile)
        except AuthError as exc:
            return _users_page(request, state, error=str(exc))
    return _users_page(request, state, link=_invite_url(request, token), link_for=username.strip())


@router.post(
    "/admin/users/{name}/{action}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin), Depends(require_htmx)],
)
def user_action(request: Request, name: str, action: str, state: State) -> HTMLResponse:
    with _auth(request) as auth:
        target = auth.user_by_name(name)
        if target is None or action not in ("invite", "disable", "enable"):
            raise HTTPException(404)
        me = current_user(request)
        if action == "disable" and me is not None and target.id == me.id:
            return _users_page(request, state, error="You can't disable your own account.")
        if action == "invite":
            link = _invite_url(request, auth.new_invite(target))
            return _users_page(request, state, link=link, link_for=target.username)
        auth.set_disabled(target, action == "disable")
    return _users_page(request, state)


def create_app(config: Config, config_path: Path | None = None) -> FastAPI:
    """The web UI for `config` (config.yaml as loaded, not one profile's): each logged-in
    user sees their own profile."""

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
    shared = app.state.web = Shared(
        base=config,
        config_path=config_path or config_file_path(),
        chat=chat_module.ChatManager(config.chat, config.db_path, config.web.user_name),
        draft_manager=DraftManager(lambda: shared.state_for(None).config, config.db_path),
        contact_manager=DraftManager(lambda: shared.state_for(None).config, config.db_path),
        templates=_make_templates(tz),
        tz=tz,
    )
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    app.middleware("http")(_login_required)
    app.middleware("http")(_same_origin_writes)
    app.middleware("http")(_security_headers)  # runs first: added last

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
