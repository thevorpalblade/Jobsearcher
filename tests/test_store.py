from datetime import UTC, datetime, timedelta

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
