"""SQLite persistence. Jobs are stored as JSON documents plus a few indexed columns."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jobsearcher.models import Contact, Job, JobStatus

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

-- Filled by later milestones (M2 ranking, M4 drafting, tracking).
CREATE TABLE IF NOT EXISTS rankings (
    job_id         TEXT NOT NULL REFERENCES jobs(id),
    input_hash     TEXT NOT NULL,
    data           TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (job_id, input_hash)
);
CREATE TABLE IF NOT EXISTS drafts (
    job_id         TEXT NOT NULL REFERENCES jobs(id),
    input_hash     TEXT NOT NULL,
    data           TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (job_id, input_hash)
);
CREATE TABLE IF NOT EXISTS llm_usage (
    ts             TEXT NOT NULL,
    model          TEXT NOT NULL,
    purpose        TEXT NOT NULL,
    input_tokens   INTEGER NOT NULL,
    output_tokens  INTEGER NOT NULL,
    cost_usd       REAL NOT NULL,
    cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS llm_usage_ts ON llm_usage(ts);

-- Where each target company (companies.yaml, by slug) posts its jobs, as detected.
CREATE TABLE IF NOT EXISTS company_ats (
    company      TEXT PRIMARY KEY,
    ats_type     TEXT,            -- NULL when no supported ATS was found
    ats_ref      TEXT,
    careers_url  TEXT,
    checked_at   TEXT NOT NULL,
    error        TEXT
);

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
    item_id         TEXT PRIMARY KEY REFERENCES news_items(id),
    kind            TEXT NOT NULL,
    relevance       INTEGER NOT NULL,
    summary         TEXT NOT NULL,
    model           TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
"""


def _now() -> datetime:
    return datetime.now(UTC)


class Store:
    def __init__(self, path: str | Path):
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(llm_usage)")}
        for column in ("cache_read_tokens", "cache_write_tokens"):
            if column not in columns:
                self.conn.execute(
                    f"ALTER TABLE llm_usage ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
                )

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

    def count_jobs(self, status: JobStatus | None = None) -> int:
        if status is None:
            return self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        return self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = ?", (status,)
        ).fetchone()[0]

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
            "SELECT data FROM rankings WHERE job_id = ? AND input_hash = ?", (job_id, input_hash)
        ).fetchone()
        return row["data"] if row else None

    def save_ranking(self, job_id: str, input_hash: str, data: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO rankings (job_id, input_hash, data, created_at)"
                " VALUES (?, ?, ?, ?)",
                (job_id, input_hash, data, _now().isoformat()),
            )

    def latest_rankings(self, status: JobStatus | None = JobStatus.OPEN) -> dict[str, str]:
        """Most recent ranking per job, as {job_id: ranking JSON}."""
        rows = self.conn.execute(
            "SELECT r.job_id, r.data FROM rankings r JOIN jobs j ON j.id = r.job_id"
            " WHERE (? IS NULL OR j.status = ?) ORDER BY r.created_at",
            (status, status),
        )
        return {row["job_id"]: row["data"] for row in rows}

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
                " cache_read_tokens, cache_write_tokens, cost_usd)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    when.isoformat(),
                    model,
                    purpose,
                    input_tokens,
                    output_tokens,
                    cache_read_tokens,
                    cache_write_tokens,
                    cost_usd,
                ),
            )

    def llm_calls_since(self, since: datetime, model_prefix: str) -> tuple[int, int]:
        """(number of calls, total tokens) for models starting with `model_prefix`."""
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(input_tokens + output_tokens + cache_read_tokens"
            " + cache_write_tokens), 0) FROM llm_usage WHERE ts >= ? AND model LIKE ?",
            (since.isoformat(), model_prefix + "%"),
        ).fetchone()
        return int(row[0]), int(row[1])

    def llm_cost_since(self, since: datetime) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage WHERE ts >= ?", (since.isoformat(),)
        ).fetchone()
        return float(row[0])

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
            "SELECT n.* FROM news_items n LEFT JOIN signals s ON s.item_id = n.id"
            " WHERE s.item_id IS NULL OR s.prompt_version != ?"
            " ORDER BY n.company, n.published_at",
            (prompt_version,),
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
                "INSERT OR REPLACE INTO signals"
                " (item_id, kind, relevance, summary, model, prompt_version, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (item_id, kind, relevance, summary, model, prompt_version, _now().isoformat()),
            )

    def signals_since(self, since: datetime) -> list[sqlite3.Row]:
        """Classified news published since `since`, most relevant first."""
        return self.conn.execute(
            "SELECT n.company, n.title, n.url, n.domain, n.published_at,"
            " s.kind, s.relevance, s.summary"
            " FROM signals s JOIN news_items n ON n.id = s.item_id"
            " WHERE n.published_at >= ? ORDER BY s.relevance DESC, n.published_at DESC",
            (since.isoformat(),),
        ).fetchall()

    # --- run bookkeeping --------------------------------------------------

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
