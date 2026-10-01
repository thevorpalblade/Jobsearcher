import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from jobsearcher.models import JobStatus
from jobsearcher.sources import jobtech_links, platsbanken
from jobsearcher.store import Store


def _jobs(load_fixture):
    pb = platsbanken.parse_hit(load_fixture("platsbanken_search.json")["hits"][0])
    links = jobtech_links.parse_hit(load_fixture("jobtech_links_search.json")["hits"][0])
    return pb, links


def test_cross_source_dedupe_merges(load_fixture):
    store = Store(":memory:")
    pb, links = _jobs(load_fixture)
    assert pb.dedupe_key == links.dedupe_key  # "Exempel AB" vs "Exempel AB (publ)"

    pb_id, created = store.upsert_job(pb)
    assert created
    links_id, created = store.upsert_job(links)
    assert not created and links_id == pb_id

    merged = store.get_job(pb_id)
    assert {s.source for s in merged.sources} == {"platsbanken", "jobtech_links"}
    assert merged.description == pb.description  # longer text kept
    assert merged.contacts  # platsbanken contacts survive
    assert store.count_jobs() == 1


def test_upsert_same_source_is_idempotent(load_fixture):
    store = Store(":memory:")
    pb, _ = _jobs(load_fixture)
    store.upsert_job(pb)
    _, created = store.upsert_job(pb)
    assert not created
    assert len(store.get_job(pb.id).contacts) == len(pb.contacts)


def test_expire_unseen_jobs_and_drop_contacts(load_fixture):
    store = Store(":memory:")
    pb, _ = _jobs(load_fixture)
    t0 = datetime(2026, 9, 1, tzinfo=UTC)
    store.upsert_job(pb, now=t0)
    assert store.expire_jobs(3, now=t0 + timedelta(days=1)) == 0
    assert store.expire_jobs(3, now=t0 + timedelta(days=5)) == 1
    job = store.get_job(pb.id)
    assert job.status == JobStatus.EXPIRED
    assert job.contacts == []
    # Seen again -> reopened.
    store.upsert_job(pb, now=t0 + timedelta(days=6))
    assert store.get_job(pb.id).status == JobStatus.OPEN


def test_file_store_uses_wal(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    assert store.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_wal_switch_is_skipped_while_another_connection_writes(tmp_path):
    path = tmp_path / "db.sqlite"
    Store(path).conn.execute("PRAGMA journal_mode=DELETE")
    busy = sqlite3.connect(path)
    busy.execute("BEGIN EXCLUSIVE")
    try:
        conn = sqlite3.connect(path, timeout=0)
        store = Store.__new__(Store)
        store.conn = conn
        store._enable_wal()  # must not raise
    finally:
        busy.rollback()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_readonly_store_rejects_writes(tmp_path, load_fixture):
    path = tmp_path / "db.sqlite"
    pb, _ = _jobs(load_fixture)
    Store(path).upsert_job(pb)
    ro = Store(path, readonly=True)
    assert ro.get_job(pb.id) is not None
    with pytest.raises(sqlite3.OperationalError):
        ro.set_last_run("platsbanken", datetime.now(UTC))


def test_readonly_store_reads_during_a_write(tmp_path, load_fixture):
    path = tmp_path / "db.sqlite"
    pb, links = _jobs(load_fixture)
    Store(path).upsert_job(pb)
    writer = Store(path)
    writer.conn.execute("BEGIN IMMEDIATE")
    writer.conn.execute("DELETE FROM jobs")
    try:
        ro = Store(path, readonly=True)
        assert ro.count_jobs() == 1  # sees the last committed state, no lock wait
    finally:
        writer.conn.rollback()
