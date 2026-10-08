"""SQLite persistence. Jobs are stored as JSON documents plus a few indexed columns."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jobsearcher.config import DEFAULT_PROFILE
from jobsearcher.models import Application, ApplicationState, Contact, Job, JobStatus

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    dedupe_key  TEXT NOT NULL,
    status      TEXT NOT NULL,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    deadline    TEXT,
    data        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_dedupe ON jobs(dedupe_key);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);

-- Maps every (source, source_id) we have seen to the canonical job it was merged into.
CREATE TABLE IF NOT EXISTS job_sources (
    source      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    job_id      TEXT NOT NULL REFERENCES jobs(id),
    PRIMARY KEY (source, source_id)
);

CREATE TABLE IF NOT EXISTS runs (
    source      TEXT PRIMARY KEY,
    last_run    TEXT NOT NULL
);

-- Jobs are shared; rankings, drafts, applications and signals belong to one profile
-- (one candidate, docs/m10-multi-user.md).
CREATE TABLE IF NOT EXISTS rankings (
    profile        TEXT NOT NULL,
    job_id         TEXT NOT NULL REFERENCES jobs(id),
    input_hash     TEXT NOT NULL,
    data           TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (profile, job_id, input_hash)
);
CREATE TABLE IF NOT EXISTS drafts (
    profile        TEXT NOT NULL,
    job_id         TEXT NOT NULL,   -- a job id, or "company:<slug>"
    input_hash     TEXT NOT NULL,
    data           TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (profile, job_id, input_hash)
);
CREATE TABLE IF NOT EXISTS llm_usage (
    ts             TEXT NOT NULL,
    model          TEXT NOT NULL,
    purpose        TEXT NOT NULL,
    input_tokens   INTEGER NOT NULL,
    output_tokens  INTEGER NOT NULL,
    cost_usd       REAL NOT NULL,
    cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens  INTEGER NOT NULL DEFAULT 0,
    profile        TEXT
);
CREATE INDEX IF NOT EXISTS llm_usage_ts ON llm_usage(ts);

-- Application tracking from the web UI. A job without a row is "new".
CREATE TABLE IF NOT EXISTS applications (
    profile     TEXT NOT NULL,
    job_id      TEXT NOT NULL REFERENCES jobs(id),
    state       TEXT NOT NULL,  -- shortlisted|applied|interview|rejected|ignored (or new + notes)
    notes       TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (profile, job_id)
);

-- Where each target company (companies.yaml, by slug) posts its jobs, as detected.
CREATE TABLE IF NOT EXISTS company_ats (
    company      TEXT PRIMARY KEY,
    ats_type     TEXT,            -- NULL when no supported ATS was found
    ats_ref      TEXT,
    careers_url  TEXT,
    checked_at   TEXT NOT NULL,
    error        TEXT
);

-- Conversations with Claude Code from the web UI.
CREATE TABLE IF NOT EXISTS chat_sessions (
    id                 TEXT PRIMARY KEY,
    title              TEXT NOT NULL,
    claude_session_id  TEXT NOT NULL,   -- Claude Code's own session, resumed each turn
    owner              TEXT,            -- the account's username (NULL: from before logins)
    started            INTEGER NOT NULL DEFAULT 0,  -- 1 once Claude Code has seen it
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES chat_sessions(id),
    role        TEXT NOT NULL,          -- user | assistant | tool | error
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_messages_session ON chat_messages(session_id, id);

-- News about target companies, and what each item signals for a spontaneous application.
CREATE TABLE IF NOT EXISTS news_items (
    id            TEXT PRIMARY KEY,   -- hash of the URL
    company       TEXT NOT NULL,      -- company slug
    title         TEXT NOT NULL,
    url           TEXT NOT NULL,
    domain        TEXT,
    published_at  TEXT,
    fetched_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS news_items_company ON news_items(company);
CREATE TABLE IF NOT EXISTS signals (
    profile         TEXT NOT NULL,
    item_id         TEXT NOT NULL REFERENCES news_items(id),
    kind            TEXT NOT NULL,
    relevance       INTEGER NOT NULL,
    summary         TEXT NOT NULL,
    model           TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (profile, item_id)
);

-- Web UI accounts and sessions (jobsearcher/auth.py). Tokens are stored as SHA-256.
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    role            TEXT NOT NULL,          -- admin | user
    profile         TEXT,                   -- the candidate profile they see
    password_hash   TEXT,                   -- NULL until the invite link is used
    invite_hash     TEXT,
    invite_expires  TEXT,
    totp_secret     TEXT,                   -- two-factor codes (base32), once confirmed
    totp_pending    TEXT,                   -- a new secret waiting for its first code
    totp_last       TEXT,                   -- the last time step used (no replays)
    disabled        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    created_at  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    ip          TEXT NOT NULL DEFAULT '',
    user_agent  TEXT NOT NULL DEFAULT '',
    acting_profile  TEXT                -- an admin viewing another profile
);
CREATE TABLE IF NOT EXISTS login_failures (
    key  TEXT NOT NULL,                     -- "user:<name>" or "ip:<address>"
    ts   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS login_failures_key ON login_failures(key, ts);
CREATE TABLE IF NOT EXISTS audit_log (
    ts        TEXT NOT NULL,
    event     TEXT NOT NULL,
    username  TEXT NOT NULL DEFAULT '',
    detail    TEXT NOT NULL DEFAULT '',
    ip        TEXT NOT NULL DEFAULT ''
);
"""


PROFILE_TABLES = ("rankings", "drafts", "applications", "signals")


def _table_ddl(table: str) -> str:
    start = SCHEMA.index(f"CREATE TABLE IF NOT EXISTS {table} (")
    return SCHEMA[start : SCHEMA.index(");", start) + 1]


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class JobRecord:
    """A job with the store's bookkeeping dates (not part of the Job document)."""

    job: Job
    first_seen: datetime
    last_seen: datetime

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> JobRecord:
        return cls(
            job=Job.model_validate_json(row["data"]),
            first_seen=datetime.fromisoformat(row["first_seen"]),
            last_seen=datetime.fromisoformat(row["last_seen"]),
        )


@dataclass
class UsageSummary:
    model: str
    purpose: str
    calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float


class Store:
    def __init__(
        self,
        path: str | Path,
        *,
        readonly: bool = False,
        check_same_thread: bool = True,
        init_schema: bool = True,
        profile: str = DEFAULT_PROFILE,
    ):
        """Open the database.

        Rankings, drafts, applications, signals and LLM usage are read and written for
        `profile` only (one candidate); jobs and everything else are shared.

        `readonly` opens the file with `mode=ro` and skips schema setup, so a reader
        can never write. `check_same_thread=False` is for callers that hand one
        connection between threads but use it serially (the web app). `init_schema`
        is skipped by short-lived writers once the schema is known to exist.
        """
        self.profile = profile
        path = Path(path)
        in_memory = str(path) == ":memory:"
        if readonly:
            self.conn = sqlite3.connect(
                f"file:{path}?mode=ro", uri=True, check_same_thread=check_same_thread
            )
        else:
            if not in_memory:
                path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(path, check_same_thread=check_same_thread)
        self.conn.row_factory = sqlite3.Row
        if readonly:
            return
        if not in_memory:
            self._enable_wal()
        if init_schema:
            self.conn.executescript(SCHEMA)
            self._migrate()

    def _enable_wal(self) -> None:
        # WAL lets the web UI read while the daemon writes. The mode is stored in the
        # file, but switching needs exclusive access; if another connection is busy,
        # leave it to the next opener.
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.OperationalError as exc:
            log.debug("Could not switch to WAL yet: %s", exc)

    def _columns(self, table: str) -> set[str]:
        return {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}

    def _migrate(self) -> None:
        columns = self._columns("llm_usage")
        for column in ("cache_read_tokens", "cache_write_tokens"):
            if column not in columns:
                self.conn.execute(
                    f"ALTER TABLE llm_usage ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
                )
        for table, column in (
            ("chat_sessions", "owner"),
            ("sessions", "acting_profile"),
            ("users", "totp_pending"),
            ("users", "totp_last"),
        ):
            if column not in self._columns(table):
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
        if "profile" not in columns:
            with self.conn:
                self.conn.execute("ALTER TABLE llm_usage ADD COLUMN profile TEXT")
                self.conn.execute("UPDATE llm_usage SET profile = ?", (DEFAULT_PROFILE,))
        # Before profiles, these tables had no profile column: their rows belong to the
        # one profile there was. The key changes, so each table is rebuilt.
        for table in PROFILE_TABLES:
            old = self._columns(table)
            if "profile" in old:
                continue
            cols = ", ".join(sorted(old))
            self.conn.executescript(
                f"BEGIN; ALTER TABLE {table} RENAME TO {table}_old; {_table_ddl(table)};"
                f" INSERT INTO {table} (profile, {cols})"
                f" SELECT '{DEFAULT_PROFILE}', {cols} FROM {table}_old;"
                f" DROP TABLE {table}_old; COMMIT;"
            )
            log.info("Added profiles to the %s table", table)

    def rename_profile(self, old: str, new: str) -> None:
        """Move every row of profile `old` to `new` (jobsearcher migrate-profiles)."""
        with self.conn:
            for table in (*PROFILE_TABLES, "llm_usage"):
                self.conn.execute(f"UPDATE {table} SET profile = ? WHERE profile = ?", (new, old))

    def close(self) -> None:
        self.conn.close()

    # --- jobs -------------------------------------------------------------

    def upsert_job(self, job: Job, now: datetime | None = None) -> tuple[str, bool]:
        """Insert or merge a job. Returns (canonical job id, created?).

        A job is matched first by any of its (source, source_id) pairs, then by dedupe key,
        so the same ad listed on several sources collapses into one record.
        """
        now = now or _now()
        existing = self._find_existing(job)
        with self.conn:
            if existing is None:
                self.conn.execute(
                    "INSERT INTO jobs"
                    " (id, dedupe_key, status, first_seen, last_seen, deadline, data)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        job.id,
                        job.dedupe_key,
                        job.status,
                        now.isoformat(),
                        now.isoformat(),
                        job.deadline.isoformat() if job.deadline else None,
                        job.model_dump_json(),
                    ),
                )
                canonical_id, created = job.id, True
                merged = job
            else:
                merged = merge_jobs(existing, job)
                self.conn.execute(
                    "UPDATE jobs SET status = ?, last_seen = ?, deadline = ?, data = ?"
                    " WHERE id = ?",
                    (
                        JobStatus.OPEN,
                        now.isoformat(),
                        merged.deadline.isoformat() if merged.deadline else None,
                        merged.model_dump_json(),
                        existing.id,
                    ),
                )
                canonical_id, created = existing.id, False
            self.conn.executemany(
                "INSERT OR REPLACE INTO job_sources (source, source_id, job_id) VALUES (?, ?, ?)",
                [(s.source, s.source_id, canonical_id) for s in merged.sources],
            )
        return canonical_id, created

    def _find_existing(self, job: Job) -> Job | None:
        for ref in job.sources:
            row = self.conn.execute(
                "SELECT j.data FROM job_sources s JOIN jobs j ON j.id = s.job_id"
                " WHERE s.source = ? AND s.source_id = ?",
                (ref.source, ref.source_id),
            ).fetchone()
            if row:
                return Job.model_validate_json(row["data"])
        row = self.conn.execute(
            "SELECT data FROM jobs WHERE dedupe_key = ? ORDER BY first_seen LIMIT 1",
            (job.dedupe_key,),
        ).fetchone()
        return Job.model_validate_json(row["data"]) if row else None

    def get_job(self, job_id: str) -> Job | None:
        row = self.conn.execute("SELECT data FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job.model_validate_json(row["data"]) if row else None

    def iter_jobs(self, status: JobStatus | None = JobStatus.OPEN) -> Iterator[Job]:
        if status is None:
            rows = self.conn.execute("SELECT data FROM jobs ORDER BY first_seen DESC")
        else:
            rows = self.conn.execute(
                "SELECT data FROM jobs WHERE status = ? ORDER BY first_seen DESC", (status,)
            )
        for row in rows:
            yield Job.model_validate_json(row["data"])

    def job_records(
        self, status: JobStatus = JobStatus.OPEN, limit: int | None = None
    ) -> list[JobRecord]:
        """Open jobs newest first; expired jobs most recently seen first (use a limit:
        they accumulate)."""
        order = "first_seen" if status == JobStatus.OPEN else "last_seen"
        rows = self.conn.execute(
            "SELECT data, first_seen, last_seen FROM jobs WHERE status = ?"
            f" ORDER BY {order} DESC LIMIT ?",
            (status, -1 if limit is None else limit),
        )
        return [JobRecord.from_row(row) for row in rows]

    def job_record(self, job_id: str) -> JobRecord | None:
        row = self.conn.execute(
            "SELECT data, first_seen, last_seen FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return JobRecord.from_row(row) if row else None

    def count_jobs(self, status: JobStatus | None = None) -> int:
        if status is None:
            return self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        return self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = ?", (status,)
        ).fetchone()[0]

    def expire_by_age(
        self, sources: set[str], max_age_days: int, now: datetime | None = None
    ) -> int:
        """Expire open jobs listed on any of `sources` once their ad is older than
        `max_age_days` (for sources that only return recent ads, where not seeing a
        job again says nothing about whether it's filled). Contacts are deleted."""
        now = now or _now()
        cutoff = now - timedelta(days=max_age_days)
        expired = 0
        rows = self.conn.execute("SELECT id, data FROM jobs WHERE status = ?", (JobStatus.OPEN,))
        with self.conn:
            for row in rows.fetchall():
                job = Job.model_validate_json(row["data"])
                if not any(s.source in sources for s in job.sources):
                    continue
                published = job.published_at
                if published is not None and published.tzinfo is None:
                    published = published.replace(tzinfo=UTC)
                if published is None or published >= cutoff:
                    continue
                job.status = JobStatus.EXPIRED
                job.contacts = []
                self.conn.execute(
                    "UPDATE jobs SET status = ?, data = ? WHERE id = ?",
                    (JobStatus.EXPIRED, job.model_dump_json(), row["id"]),
                )
                expired += 1
        return expired

    def expire_jobs(
        self,
        expire_after_days: int,
        now: datetime | None = None,
        skip_sources: set[str] | frozenset[str] = frozenset(),
    ) -> int:
        """Mark jobs expired when unseen for a while or past their deadline.

        Jobs listed on any of `skip_sources` (sources that failed this run, so their
        jobs weren't re-seen) are left alone unless past their deadline.
        Contact details of expired jobs are deleted (GDPR data minimisation).
        """
        now = now or _now()
        cutoff = (now - timedelta(days=expire_after_days)).isoformat()
        rows = self.conn.execute(
            "SELECT id, data, COALESCE(deadline < ?, 0) AS past_deadline FROM jobs"
            " WHERE status = ? AND (last_seen < ? OR deadline < ?)",
            (now.isoformat(), JobStatus.OPEN, cutoff, now.isoformat()),
        ).fetchall()
        expired = 0
        with self.conn:
            for row in rows:
                job = Job.model_validate_json(row["data"])
                skipped = any(s.source in skip_sources for s in job.sources)
                if skipped and not row["past_deadline"]:
                    continue
                expired += 1
                job.status = JobStatus.EXPIRED
                job.contacts = []
                self.conn.execute(
                    "UPDATE jobs SET status = ?, data = ? WHERE id = ?",
                    (JobStatus.EXPIRED, job.model_dump_json(), row["id"]),
                )
        return expired

    def save_job(self, job: Job) -> None:
        """Overwrite a stored job's data (e.g. after adding contacts) without touching
        its seen/expiry bookkeeping."""
        with self.conn:
            self.conn.execute(
                "UPDATE jobs SET data = ? WHERE id = ?", (job.model_dump_json(), job.id)
            )

    # --- rankings ---------------------------------------------------------

    def get_ranking(self, job_id: str, input_hash: str) -> str | None:
        row = self.conn.execute(
            "SELECT data FROM rankings WHERE profile = ? AND job_id = ? AND input_hash = ?",
            (self.profile, job_id, input_hash),
        ).fetchone()
        return row["data"] if row else None

    def save_ranking(self, job_id: str, input_hash: str, data: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO rankings (profile, job_id, input_hash, data, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (self.profile, job_id, input_hash, data, _now().isoformat()),
            )

    def latest_rankings(self, status: JobStatus | None = JobStatus.OPEN) -> dict[str, str]:
        """Most recent ranking per job, as {job_id: ranking JSON}."""
        rows = self.conn.execute(
            "SELECT r.job_id, r.data FROM rankings r JOIN jobs j ON j.id = r.job_id"
            " WHERE r.profile = ? AND (? IS NULL OR j.status = ?) ORDER BY r.created_at",
            (self.profile, status, status),
        )
        return {row["job_id"]: row["data"] for row in rows}

    def latest_ranking(self, job_id: str) -> tuple[str, datetime] | None:
        """(ranking JSON, created_at) of a job's most recent ranking."""
        row = self.conn.execute(
            "SELECT data, created_at FROM rankings WHERE profile = ? AND job_id = ?"
            " ORDER BY created_at DESC LIMIT 1",
            (self.profile, job_id),
        ).fetchone()
        return (row["data"], datetime.fromisoformat(row["created_at"])) if row else None

    # --- application tracking --------------------------------------------

    def applications(self) -> dict[str, Application]:
        rows = self.conn.execute(
            "SELECT job_id, state, notes, updated_at FROM applications WHERE profile = ?",
            (self.profile,),
        )
        return {row["job_id"]: Application(**dict(row)) for row in rows}

    def get_application(self, job_id: str) -> Application | None:
        row = self.conn.execute(
            "SELECT job_id, state, notes, updated_at FROM applications"
            " WHERE profile = ? AND job_id = ?",
            (self.profile, job_id),
        ).fetchone()
        return Application(**dict(row)) if row else None

    def set_application(
        self, job_id: str, state: ApplicationState, notes: str, now: datetime | None = None
    ) -> Application | None:
        """Save a job's tracking state and notes. Back to "new" without notes removes
        the row, so the job counts as untracked again."""
        with self.conn:
            if state == ApplicationState.NEW and not notes.strip():
                self.conn.execute(
                    "DELETE FROM applications WHERE profile = ? AND job_id = ?",
                    (self.profile, job_id),
                )
                return None
            app = Application(job_id=job_id, state=state, notes=notes, updated_at=now or _now())
            self.conn.execute(
                "INSERT OR REPLACE INTO applications"
                " (profile, job_id, state, notes, updated_at) VALUES (?, ?, ?, ?, ?)",
                (self.profile, job_id, app.state, app.notes, app.updated_at.isoformat()),
            )
        return app

    def tracked_job_records(self) -> list[JobRecord]:
        """Every job with a tracking row, open or expired, most recently seen first."""
        rows = self.conn.execute(
            "SELECT data, first_seen, last_seen FROM jobs"
            " WHERE id IN (SELECT job_id FROM applications WHERE profile = ?)"
            " ORDER BY last_seen DESC",
            (self.profile,),
        )
        return [JobRecord.from_row(row) for row in rows]

    # --- LLM usage -------------------------------------------------------

    def record_llm_usage(
        self,
        when: datetime,
        model: str,
        purpose: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
        cost_usd: float,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO llm_usage (ts, model, purpose, input_tokens, output_tokens,"
                " cache_read_tokens, cache_write_tokens, cost_usd, profile)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    when.isoformat(),
                    model,
                    purpose,
                    input_tokens,
                    output_tokens,
                    cache_read_tokens,
                    cache_write_tokens,
                    cost_usd,
                    self.profile,
                ),
            )

    def llm_calls_since(self, since: datetime, model_prefix: str) -> tuple[int, int]:
        """(number of calls, total tokens) for models starting with `model_prefix`."""
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(input_tokens + output_tokens + cache_read_tokens"
            " + cache_write_tokens), 0) FROM llm_usage"
            " WHERE profile = ? AND ts >= ? AND model LIKE ?",
            (self.profile, since.isoformat(), model_prefix + "%"),
        ).fetchone()
        return int(row[0]), int(row[1])

    def llm_cost_since(self, since: datetime) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage WHERE profile = ? AND ts >= ?",
            (self.profile, since.isoformat()),
        ).fetchone()
        return float(row[0])

    def llm_usage_summary(self, since: datetime) -> list[UsageSummary]:
        """Calls, tokens and cost since `since`, per model and purpose, costliest first."""
        rows = self.conn.execute(
            "SELECT model, purpose, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens,"
            " SUM(output_tokens) AS output_tokens, SUM(cache_read_tokens) AS cache_read_tokens,"
            " SUM(cache_write_tokens) AS cache_write_tokens, SUM(cost_usd) AS cost_usd"
            " FROM llm_usage WHERE profile = ? AND ts >= ? GROUP BY model, purpose"
            " ORDER BY cost_usd DESC, calls DESC",
            (self.profile, since.isoformat()),
        )
        return [UsageSummary(**dict(row)) for row in rows]

    # --- target companies ------------------------------------------------

    def company_ats(self) -> dict[str, sqlite3.Row]:
        """Detection results for every company, keyed by company slug."""
        rows = self.conn.execute("SELECT * FROM company_ats")
        return {row["company"]: row for row in rows}

    def save_company_ats(
        self,
        company: str,
        ats_type: str | None,
        ats_ref: str | None,
        careers_url: str | None,
        error: str | None = None,
        when: datetime | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO company_ats"
                " (company, ats_type, ats_ref, careers_url, checked_at, error)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (company, ats_type, ats_ref, careers_url, (when or _now()).isoformat(), error),
            )

    # --- chat ---------------------------------------------------------------

    def create_chat(
        self,
        chat_id: str,
        claude_session_id: str,
        title: str = "New chat",
        owner: str | None = None,
    ) -> None:
        now = _now().isoformat()
        with self.conn:
            self.conn.execute(
                "INSERT INTO chat_sessions"
                " (id, title, claude_session_id, created_at, updated_at, owner)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (chat_id, title, claude_session_id, now, now, owner),
            )

    def get_chat(self, chat_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM chat_sessions WHERE id = ?", (chat_id,)).fetchone()

    def list_chats(
        self, limit: int = 30, owner: str | None = None, with_unowned: bool = True
    ) -> list[sqlite3.Row]:
        """Chats, newest first: all of them, or with `owner`, that user's (plus, with
        `with_unowned`, the chats from before logins, which only admins see)."""
        if owner is None:
            sql, args = "SELECT * FROM chat_sessions", ()
        else:
            sql = "SELECT * FROM chat_sessions WHERE owner = ? COLLATE NOCASE"
            sql += " OR owner IS NULL" if with_unowned else ""
            args = (owner,)
        return self.conn.execute(
            sql + " ORDER BY updated_at DESC LIMIT ?", (*args, limit)
        ).fetchall()

    def mark_chat_started(self, chat_id: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE chat_sessions SET started = 1 WHERE id = ?", (chat_id,))

    def set_chat_title(self, chat_id: str, title: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE chat_sessions SET title = ? WHERE id = ?", (title, chat_id))

    def add_chat_message(self, chat_id: str, role: str, text: str) -> int:
        now = _now().isoformat()
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO chat_messages (session_id, role, text, created_at)"
                " VALUES (?, ?, ?, ?)",
                (chat_id, role, text, now),
            )
            self.conn.execute(
                "UPDATE chat_sessions SET updated_at = ? WHERE id = ?", (now, chat_id)
            )
        return int(cursor.lastrowid or 0)

    def chat_messages(self, chat_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM chat_messages WHERE session_id = ? ORDER BY id", (chat_id,)
        ).fetchall()

    def delete_chat(self, chat_id: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (chat_id,))
            self.conn.execute("DELETE FROM chat_sessions WHERE id = ?", (chat_id,))

    # --- application drafts -----------------------------------------------

    def save_draft(self, key: str, input_hash: str, data: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO drafts (profile, job_id, input_hash, data, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (self.profile, key, input_hash, data, _now().isoformat()),
            )

    def get_draft(self, key: str, input_hash: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM drafts WHERE profile = ? AND job_id = ? AND input_hash = ?",
            (self.profile, key, input_hash),
        ).fetchone()

    def list_drafts(self, key: str) -> list[sqlite3.Row]:
        """All versions of a job's (or company's) draft, newest first."""
        return self.conn.execute(
            "SELECT * FROM drafts WHERE profile = ? AND job_id = ? ORDER BY created_at DESC",
            (self.profile, key),
        ).fetchall()

    def latest_drafts(self) -> dict[str, sqlite3.Row]:
        """The newest draft per job/company key."""
        rows = self.conn.execute(
            "SELECT * FROM drafts WHERE profile = ? ORDER BY created_at", (self.profile,)
        )
        return {row["job_id"]: row for row in rows}

    def auto_drafts_since(self, since: datetime) -> int:
        """Drafts started by shortlisting (not by a click) since `since`."""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM drafts WHERE profile = ? AND created_at >= ?"
            " AND json_extract(data, '$.trigger') = 'shortlist'",
            (self.profile, since.isoformat()),
        ).fetchone()
        return int(row[0])

    # --- news and signals -------------------------------------------------

    def save_news_items(self, items: list[dict[str, str | None]]) -> int:
        """Insert news items not seen before; returns how many were new."""
        before = self.conn.total_changes
        with self.conn:
            self.conn.executemany(
                "INSERT OR IGNORE INTO news_items"
                " (id, company, title, url, domain, published_at, fetched_at)"
                " VALUES (:id, :company, :title, :url, :domain, :published_at, :fetched_at)",
                items,
            )
        return self.conn.total_changes - before

    def unclassified_news(self, prompt_version: str) -> list[sqlite3.Row]:
        """News items with no signal for the current prompt version, oldest first."""
        return self.conn.execute(
            "SELECT n.* FROM news_items n"
            " LEFT JOIN signals s ON s.item_id = n.id AND s.profile = ?"
            " WHERE s.item_id IS NULL OR s.prompt_version != ?"
            " ORDER BY n.company, n.published_at",
            (self.profile, prompt_version),
        ).fetchall()

    def save_signal(
        self,
        item_id: str,
        kind: str,
        relevance: int,
        summary: str,
        model: str,
        prompt_version: str,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO signals (profile, item_id, kind, relevance, summary,"
                " model, prompt_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.profile,
                    item_id,
                    kind,
                    relevance,
                    summary,
                    model,
                    prompt_version,
                    _now().isoformat(),
                ),
            )

    def signals_since(self, since: datetime) -> list[sqlite3.Row]:
        """Classified news published since `since`, most relevant first."""
        return self.conn.execute(
            "SELECT n.company, n.title, n.url, n.domain, n.published_at,"
            " s.kind, s.relevance, s.summary"
            " FROM signals s JOIN news_items n ON n.id = s.item_id"
            " WHERE s.profile = ? AND n.published_at >= ?"
            " ORDER BY s.relevance DESC, n.published_at DESC",
            (self.profile, since.isoformat()),
        ).fetchall()

    # --- run bookkeeping --------------------------------------------------

    def last_runs(self) -> dict[str, datetime]:
        rows = self.conn.execute("SELECT source, last_run FROM runs ORDER BY source")
        return {row["source"]: datetime.fromisoformat(row["last_run"]) for row in rows}

    def last_run(self, source: str) -> datetime | None:
        row = self.conn.execute("SELECT last_run FROM runs WHERE source = ?", (source,)).fetchone()
        return datetime.fromisoformat(row["last_run"]) if row else None

    def set_last_run(self, source: str, when: datetime) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO runs (source, last_run) VALUES (?, ?)",
                (source, when.isoformat()),
            )


def merge_jobs(old: Job, new: Job) -> Job:
    """Combine two records of the same job. Fields already set on `old` win,
    except the description, where the longer (more complete) text is kept."""
    merged = old.model_copy(deep=True)
    for field in Job.model_fields:
        if field in {"id", "contacts", "sources", "status", "description"}:
            continue
        if getattr(merged, field) in (None, "") and getattr(new, field) not in (None, ""):
            setattr(merged, field, getattr(new, field))
    if len(new.description) > len(merged.description):
        merged.description = new.description
    merged.status = JobStatus.OPEN

    seen_refs = {(s.source, s.source_id) for s in merged.sources}
    merged.sources += [s for s in new.sources if (s.source, s.source_id) not in seen_refs]
    merged.contacts = merge_contacts(merged.contacts, new.contacts)
    return merged


def merge_contacts(a: list[Contact], b: list[Contact]) -> list[Contact]:
    out: list[Contact] = []
    seen: set[tuple[str, str, str]] = set()
    for contact in [*a, *b]:
        key = contact.key()
        if key in seen or key == ("", "", ""):
            continue
        seen.add(key)
        out.append(contact)
    return out
