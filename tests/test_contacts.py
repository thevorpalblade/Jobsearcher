"""Contact people from employers' websites (docs/m5-contacts.md)."""

from conftest import make_assessment, make_job
from test_companies import FakeClient
from test_drafting import FakeLLM
from test_web import HX, web  # noqa: F401  (the `web` fixture)

from jobsearcher.contacts import service
from jobsearcher.contacts.extract import (
    Person,
    SiteFindings,
    _Named,
    _NamedPeople,
    find_pattern,
    guess_email,
    jsonld_people,
    site_findings,
)
from jobsearcher.contacts.links import search_links
from jobsearcher.contacts.pick import _Pick, _Picks, pick_people
from jobsearcher.contacts.site import (
    candidate_domains,
    company_key,
    contact_pages,
    find_site,
    is_company_host,
    registered_domain,
)
from jobsearcher.models import ApplicationState, Contact
from jobsearcher.ranking.config import RankingConfig, TargetRole
from jobsearcher.store import Store

HOME = """<html><title>Acme AB</title><body>Acme AB makes widgets.
<a href="/om-oss">Om oss</a> <a href="/kontakt">Kontakt</a> <a href="/produkter">Produkter</a>
<a href="https://elsewhere.example/team">Team</a> <a href="mailto:rekrytering@acme.se">Jobb</a>
</body></html>"""
CONTACT = """<html><body><h1>Kontakt</h1>
<p>Anna Svensson, HR-chef, anna.svensson@acme.se, 08-123 456 78</p>
<p>Bertil Ök, Driftchef</p>
<script type="application/ld+json">{"@type": "Person", "name": "Cecilia Lund",
 "jobTitle": "Head of Talent Acquisition"}</script></body></html>"""


def acme_client():
    return FakeClient(
        {
            "https://acme.se/": HOME,
            "https://acme.se/kontakt": CONTACT,
            "https://acme.se/om-oss": "<p>Om Acme. Vår VD är David Ek.</p>",
        }
    )


def test_search_links():
    links = search_links("Acme Sverige AB", "Project manager", "acme.se")
    labels = [link.label for link in links]
    assert labels == [
        "Recruiters on LinkedIn",
        "Recruiters via Google",
        "Heads of Project manager on LinkedIn",
        "Contact pages on acme.se",
    ]
    assert "%22Acme%22" in links[0].url and links[0].url.startswith("https://www.linkedin.com/")
    assert search_links(None) == []


def test_domains_and_candidates():
    assert registered_domain("jobs.acme.se") == "acme.se"
    assert registered_domain("www.shop.acme.co.uk") == "acme.co.uk"
    assert not is_company_host("www.linkedin.com") and not is_company_host("x.teamtailor.com")
    assert is_company_host("jobs.acme.se")
    job = make_job(
        1,
        "PM",
        company="Acme AB",
        company_url="https://www.acme.com",
        apply_email="jobs@gmail.com",
        apply_url="https://jobs.acme-careers.se/apply",
        url="https://www.linkedin.com/jobs/1",
        contacts=[Contact(email="x@acme.se", provenance="ad_text")],
    )
    assert candidate_domains(job, {company_key("Acme"): "https://acme.se"}) == [
        ("acme.se", "companies.yaml"),
        ("acme.com", "job source"),
        ("acme-careers.se", "application link"),
    ]  # acme.se from the contact email is already in; gmail and LinkedIn aren't sites


def test_find_site_verifies_and_guesses_only_when_needed():
    job = make_job(1, "PM", company="Acme AB", url="https://www.linkedin.com/jobs/1")
    guesses = []

    def guess(company):
        guesses.append(company)
        return "acme.se"

    found = find_site(job, {}, acme_client(), guess)
    assert found is not None and found[:2] == ("acme.se", "model guess") and guesses == ["Acme AB"]
    job.company_url = "https://acme.se"
    assert find_site(job, {}, acme_client(), guess)[1] == "job source" and len(guesses) == 1
    other = make_job(2, "PM", company="Beta AB", company_url="https://acme.se")
    assert find_site(other, {}, acme_client(), None) is None  # acme.se doesn't name Beta


def test_contact_pages_stay_on_the_site():
    client = acme_client()
    pages = contact_pages("https://acme.se/", HOME, client)
    assert pages[0][0] == "https://acme.se/"  # then the about and contact pages, not products
    assert {url for url, _ in pages[1:]} == {"https://acme.se/om-oss", "https://acme.se/kontakt"}
    assert "https://elsewhere.example/team" not in client.requests


def test_people_are_kept_only_if_on_the_page():
    pages = [("https://acme.se/kontakt", CONTACT)]
    answer = _NamedPeople(
        people=[
            _Named(name="Anna Svensson", role="HR-chef", email="anna.svensson@acme.se",
                   phone="08-123 456 78", page=1),
            _Named(name="Bertil Ök", role="Driftchef", email="bertil.ok@acme.se", phone=None,
                   page=1),  # the email isn't on the page: invented
            _Named(name="Erik Påhittad", role="CEO", email=None, phone=None, page=1),
            _Named(name="Anna Svensson", role=None, email=None, phone=None, page=9),
        ]
    )  # fmt: skip
    findings = site_findings(FakeLLM("glm", answer), "Acme AB", "acme.se", pages)
    assert {p.name: p.source for p in findings.people} == {
        "Cecilia Lund": "jsonld",
        "Anna Svensson": "model",
    }
    assert findings.pattern == "{first}.{last}" and findings.pattern_url == pages[0][0]
    assert jsonld_people(CONTACT, "u")[0].role == "Head of Talent Acquisition"


def test_guessed_addresses_need_a_real_example():
    people = [Person("Åsa Öberg", None, "asa.oberg@acme.se", None, "u", "model")]
    assert find_pattern(people, "acme.se") == ("{first}.{last}", "u")
    assert guess_email("Cecilia Lund", "{first}.{last}", "acme.se") == "cecilia.lund@acme.se"
    assert (
        find_pattern([Person("A B", None, "info@acme.se", None, "u", "model")], "acme.se") is None
    )
    assert guess_email("Madonna", "{first}.{last}", "acme.se") is None


def test_picking_people_for_a_job():
    findings = SiteFindings(
        people=[
            Person("Anna Svensson", "HR-chef", "anna.svensson@acme.se", None, "u1", "model"),
            Person("Cecilia Lund", "Head of TA", None, None, "u2", "jsonld"),
        ],
        mailboxes=[],
        pattern="{first}.{last}",
        pattern_url="u1",
    )
    llm = FakeLLM("glm", _Picks(picks=[_Pick(number=2, reason="Recruits for this."),
                                        _Pick(number=7, reason="?"), _Pick(number=2, reason="x")]))  # fmt: skip
    [contact] = pick_people(llm, findings, "acme.se", "Job: PM at Acme")
    assert (contact.name, contact.note, contact.provenance) == (
        "Cecilia Lund",
        "Recruits for this.",
        "company_site:u2",
    )
    assert contact.email is None and contact.guessed_email == "cecilia.lund@acme.se"


def test_lookup_caches_the_site_and_its_people(tmp_path):
    from jobsearcher.config import Config

    store = Store(tmp_path / "db", profile="anna")
    job = make_job(1, "PM", company="Acme AB", company_url="https://acme.se")
    store.upsert_job(job)
    kontakt = 1 + [u for u, _ in contact_pages("https://acme.se/", HOME, acme_client())].index(
        "https://acme.se/kontakt"
    )
    answer = _NamedPeople(people=[_Named(name="Anna Svensson", role="HR-chef",
                                         email=None, phone=None, page=kontakt)])  # fmt: skip
    picks = _Picks(picks=[_Pick(number=2, reason="Head of HR.")])  # [Cecilia (JSON-LD), Anna]
    client = acme_client()
    clients = service.Clients(client, FakeLLM("glm", answer, picks, picks))
    found = service.lookup(Config(), store, job.id, job, "Job: PM", clients)
    assert [c.name for c in found.contacts] == ["Anna Svensson"] and found.domain == "acme.se"
    assert found.mailboxes == ["rekrytering@acme.se"]
    fetched = len(client.requests)
    again = service.lookup(Config(), store, job.id, job, "Job: PM", clients)
    assert len(client.requests) == fetched and again.contacts == found.contacts  # cached
    assert service.load_lookup(store, job.id).contacts == found.contacts

    service.set_site(store, "Acme AB", None)  # the user: "it has no site"
    none = service.lookup(Config(), store, job.id, job, "Job: PM", clients)
    assert none.contacts == [] and "Couldn't find the company's website" in none.error


def test_letter_contact_prefers_the_choice_then_the_ad():
    ad = Contact(name="Ad Person", provenance="platsbanken:application_contacts")
    site = Contact(name="Site Person", provenance="company_site:u")
    job = make_job(1, "PM", contacts=[ad])
    found = service.Lookup(contacts=[site])
    assert service.letter_contact(job, found) == ad
    found.chosen = service.contact_key(site)
    assert service.letter_contact(job, found) == site
    assert service.letter_contact(make_job(2, "PM"), service.Lookup(contacts=[site])) == site
    assert service.letter_contact(make_job(3, "PM"), None) is None


def test_due_for_lookup(tmp_path):
    store = Store(tmp_path / "db", profile="anna")
    rc = RankingConfig(target_roles=[TargetRole(name="PM")])
    jobs = {
        "high": make_job(1, "PM high", company="A"),
        "low": make_job(2, "PM low", company="B"),
        "short": make_job(3, "PM short", company="C"),
        "named": make_job(
            4, "PM named", company="D", contacts=[Contact(name="Eva", provenance="ad_text")]
        ),  # fmt: skip
    }
    from jobsearcher.ranking.ranker import Ranking

    scores = {"high": 90, "low": 20, "short": 20, "named": 95}
    for name, job in jobs.items():
        store.upsert_job(job)
        assessment = make_assessment(scores[name], scores[name])
        ranking = Ranking(job_id=job.id, input_hash="h", model="m", assessment=assessment)
        store.save_ranking(job.id, "h", ranking.model_dump_json())
    store.set_application(jobs["short"].id, ApplicationState.SHORTLISTED, "")
    due = {j.title for j in service.due_for_lookup(store, rc)}
    assert due == {"PM high", "PM short"}
    store.save_job_contacts(jobs["high"].id, service.Lookup().to_json())
    assert {j.title for j in service.due_for_lookup(store, rc)} == {"PM short"}


def test_contacts_panel_on_the_job_page(web, monkeypatch):  # noqa: F811
    job = web.add(make_job(1, "Projektledare", company="Acme AB"), make_assessment())
    page = web.client.get(f"/jobs/{job.id}").text
    assert "Recruiters on LinkedIn" in page and "Find contacts" in page

    def fake_for_job(config, store, job_id, force=False, clients=None):
        found = service.Lookup(
            contacts=[Contact(name="Anna Svensson", role="HR-chef", provenance="company_site:u",
                              url="https://acme.se/kontakt", note="Head of HR.",
                              guessed_email="anna.svensson@acme.se")],
            domain="acme.se", site_source="job source",
        )  # fmt: skip
        store.save_job_contacts(job_id, found.to_json())
        return found

    monkeypatch.setattr(service, "for_job", fake_for_job)
    web.client.post(f"/jobs/{job.id}/contacts", headers=HX)
    for _ in range(200):
        status = web.state.contacts.status(job.id)
        if status and not status.active:
            break
        import time

        time.sleep(0.02)
    panel = web.client.get(f"/jobs/{job.id}/contacts").text
    assert "Anna Svensson" in panel and "guess: anna.svensson@acme.se" in panel
    assert "use for the letter" in panel and "Contact pages on acme.se" in panel
    key = service.contact_key(Contact(name="Anna Svensson", role="HR-chef", provenance="x"))
    chosen = web.client.post(f"/jobs/{job.id}/contacts/choose", data={"key": key}, headers=HX)
    assert "letter is addressed to them" in chosen.text
    bad = web.client.post(f"/jobs/{job.id}/contacts/site", data={"domain": "linkedin.com"},
                          headers=HX)  # fmt: skip
    assert "look like a company website" in bad.text
    assert web.client.post("/other/x/contacts", headers=HX).status_code == 404


def test_drafts_are_addressed_to_the_chosen_contact(tmp_path):
    from jobsearcher.config import Config
    from jobsearcher.drafting import service as drafting

    store = Store(tmp_path / "db", profile="anna")
    job = make_job(1, "PM", company="Acme AB")
    store.upsert_job(job)
    site = Contact(name="Cecilia Lund", role="Head of TA", provenance="company_site:u")
    store.save_job_contacts(job.id, service.Lookup(contacts=[site]).to_json())
    contact = drafting._letter_contact(Config(), store, job.id, job)
    assert contact == site  # no one named in the ad: the website's pick
    request = drafting.job_request(job, None, "", contact)
    assert request.addressed_to == "Cecilia Lund" and "Dear Cecilia Lund" in request.prompt
    # No lookup yet and none possible (no network in tests): the letter has no name.
    other = make_job(2, "PM", company="Beta AB")
    store.upsert_job(other)
    assert drafting._letter_contact(Config(), store, other.id, other) is None
