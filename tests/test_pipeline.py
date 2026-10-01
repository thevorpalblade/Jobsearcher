from jobsearcher.config import Config, SearchConfig
from jobsearcher.contacts import extract_contacts_from_text
from jobsearcher.models import JobStatus
from jobsearcher.pipeline import matches_filters, run_search
from jobsearcher.sources import jobtech_links, platsbanken
from jobsearcher.store import Store


class FakeSource:
    def __init__(self, name, jobs, fail=False):
        self.name, self.jobs, self.fail = name, jobs, fail

    def search(self, keyword, published_after=None):
        if self.fail:
            raise RuntimeError("boom")
        yield from self.jobs


def _config(**search):
    return Config(search=SearchConfig(keywords=["python", "utvecklare"], **search))


def test_run_search_filters_dedupes_and_merges(load_fixture):
    pb_jobs = [platsbanken.parse_hit(h) for h in load_fixture("platsbanken_search.json")["hits"]]
    links_jobs = [
        jobtech_links.parse_hit(h) for h in load_fixture("jobtech_links_search.json")["hits"]
    ]
    store = Store(":memory:")
    report = run_search(
        _config(locations=["Stockholm"]),
        store,
        [FakeSource("platsbanken", pb_jobs), FakeSource("jobtech_links", links_jobs)],
    )
    # 2 platsbanken ads + 1 links ad; the same ads are returned for both keywords.
    assert report.fetched == 3
    assert report.filtered_out == 1  # Göteborg
    assert (report.new, report.updated) == (1, 1)
    assert store.count_jobs(JobStatus.OPEN) == 1


def test_failed_source_blocks_expiry(load_fixture):
    store = Store(":memory:")
    report = run_search(_config(), store, [FakeSource("platsbanken", [], fail=True)])
    assert report.failed_sources == ["platsbanken"]
    assert report.expired == 0


def test_exclude_keywords(load_fixture):
    job = platsbanken.parse_hit(load_fixture("platsbanken_search.json")["hits"][0])
    assert matches_filters(job, SearchConfig())
    assert not matches_filters(job, SearchConfig(exclude_keywords=["DISTANS"]))
    assert not matches_filters(job, SearchConfig(locations=["Malmö"], include_remote=False))
    assert matches_filters(job, SearchConfig(locations=["Malmö"], include_remote=True))


def test_extract_contacts_from_text():
    contacts = extract_contacts_from_text(
        "Kontakta jobb@firma.se eller Per på per.persson@firma.se, tel 08-123 456 78."
    )
    emails = {c.email: c.role for c in contacts if c.email}
    assert emails == {"jobb@firma.se": "generic mailbox", "per.persson@firma.se": None}
    assert any(c.phone for c in contacts)


def test_search_keywords_include_target_roles(tmp_path):
    from pathlib import Path

    from jobsearcher.pipeline import search_keywords

    example = Path(__file__).parent.parent / "ranking.example.yaml"
    config = Config(
        search=SearchConfig(keywords=["extra", "projektledare"]), ranking_config=example
    )
    keywords = search_keywords(config)
    assert keywords[0] == "extra"
    assert "HR Business Partner" in keywords and "förändringsledare" in keywords
    assert keywords.count("projektledare") == 1
    config.search.use_target_roles = False
    assert search_keywords(config) == ["extra", "projektledare"]
