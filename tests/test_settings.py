import io

import pytest
from test_web import RANKING_YAML, web  # noqa: F401  (the `web` fixture)

from jobsearcher import cvs, settings
from jobsearcher.config import Config
from jobsearcher.ranking import load_ranking_config

HX = {"HX-Request": "true"}


def _pdf(text: str) -> bytes:
    """A minimal one-page PDF showing `text` (offsets computed so readers accept it)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for i, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    )
    return out.getvalue()


def _docx() -> bytes:
    import docx

    document = docx.Document()
    document.add_heading("Anna Andersson", level=0)
    document.add_heading("Experience", level=1)
    document.add_paragraph("Led a reorganisation.", style="List Bullet")
    document.add_paragraph("HR Business Partner, 2019-2026")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


# --- conversion and CV files --------------------------------------------------------


def test_convert_uploads():
    assert "Hello CV" in cvs.convert_upload("cv.pdf", _pdf("Hello CV"))
    md = cvs.convert_upload("cv.docx", _docx())
    assert md.splitlines()[:3] == ["# Anna Andersson", "## Experience", "- Led a reorganisation."]
    assert cvs.convert_upload("cv.md", b"# A\r\n\r\n\r\n\r\nB  \r\n") == "# A\n\nB\n"
    with pytest.raises(cvs.CvError, match="Unsupported"):
        cvs.convert_upload("cv.exe", b"x")
    with pytest.raises(cvs.CvError, match="Couldn't read the PDF"):
        cvs.convert_upload("cv.pdf", b"not a pdf")
    with pytest.raises(cvs.CvError, match="No text"):
        cvs.convert_upload("cv.md", b"   ")
    with pytest.raises(cvs.CvError, match="larger"):
        cvs.convert_upload("cv.md", b"x" * (cvs.MAX_UPLOAD_BYTES + 1))


def test_cv_files_master_and_backup(tmp_path):
    master = tmp_path / "cvs" / "master.md"
    cvs.save_cv(master, "master", "# Old master\n")
    name = cvs.free_name(master, "Anna CV 2026.pdf")
    assert name == "anna-cv-2026"
    cvs.save_cv(master, name, "# New\n")
    assert cvs.free_name(master, "anna cv 2026.docx") == "anna-cv-2026-2"
    assert cvs.free_name(master, "master.md") == "master-copy"
    assert [(c.name, c.is_master) for c in cvs.list_cvs(master)] == [
        ("master", True),
        ("anna-cv-2026", False),
    ]
    backup = cvs.make_master(master, name, tmp_path / "backups")
    assert master.read_text() == "# New\n" and backup.read_text() == "# Old master\n"
    with pytest.raises(cvs.CvError, match="can't be deleted"):
        cvs.delete_cv(master, "master")
    cvs.delete_cv(master, name)
    assert [c.name for c in cvs.list_cvs(master)] == ["master"]
    for bad in ("../etc/passwd", "Master", "a/b", ""):
        with pytest.raises(cvs.CvError):
            cvs.cv_path(master, bad)


def test_ranking_reads_every_cv(tmp_path):
    master = tmp_path / "cvs" / "master.md"
    assert cvs.ranking_cv(master) is None  # no master yet
    cvs.save_cv(master, "master", "# Anna\nHR partner\n")
    assert cvs.ranking_cv(master) == "# Anna\nHR partner"
    stamp = cvs.cvs_stamp(master)
    cvs.save_cv(master, "older", "# Anna\nPayroll lead, 2015\n")
    cvs.save_cv(master, "copy", "# Anna\nHR partner\n")  # e.g. the CV made the master
    assert cvs.ranking_cv(master) == (
        "# Anna\nHR partner\n\n"
        "# Other CV: older (more facts about the same candidate)\n# Anna\nPayroll lead, 2015"
    )
    assert cvs.cvs_stamp(master) != stamp  # a new CV re-ranks


# --- config files ----------------------------------------------------------------


def test_parse_reports_yaml_and_schema_errors():
    ranking = settings.FILES["ranking"]
    assert settings.parse(ranking, RANKING_YAML)[1] == []
    _, errors = settings.parse(ranking, "weights: {fit: [1\n")
    assert errors[0].startswith("YAML syntax error at line")
    _, errors = settings.parse(ranking, "weights: {fit: lots}\n")
    assert errors == [
        "weights.fit: Input should be a valid number, unable to parse string as a number"
    ]
    _, errors = settings.parse(ranking, "- a list\n")
    assert "mapping" in errors[0]
    companies = settings.FILES["companies"]
    _, errors = settings.parse(companies, "companies: [{name: A}, {name: a}]\n")
    assert errors == ["Duplicate company: a"]
    assert settings.parse(settings.FILES["config"], "")[1] == []  # all defaults


def test_effects_explain_reranking():
    ranking = settings.FILES["ranking"]
    new, _ = settings.parse(ranking, RANKING_YAML.replace("HRBP", "HR-partner"))
    assert any("re-ranked" in n for n in settings.effects(ranking, RANKING_YAML, new))
    new, _ = settings.parse(ranking, RANKING_YAML.replace("fit: 0.6", "fit: 0.7"))
    assert settings.effects(ranking, RANKING_YAML, new) == [
        "Weights or adjustments changed: scores update immediately, free."
    ]


def test_save_backs_up_and_writes_in_place(tmp_path):
    path = tmp_path / "ranking.yaml"
    path.write_text("# keep me\nweights: {fit: 0.6}\n")
    inode = path.stat().st_ino
    backup = settings.save(path, "# keep me\nweights: {fit: 0.7}", tmp_path / "backups")
    assert path.read_text() == "# keep me\nweights: {fit: 0.7}\n"
    assert path.stat().st_ino == inode  # same file: works on Docker bind mounts
    assert backup.read_text() == "# keep me\nweights: {fit: 0.6}\n"


def test_examples_are_found():
    for file in settings.FILES.values():
        assert settings.example_text(file).startswith("#"), file.key


# --- web pages ---------------------------------------------------------------------


def test_settings_page_lists_cvs_and_files(web):  # noqa: F811
    page = web.client.get("/settings")
    assert page.status_code == 200
    assert "master.md" in page.text and "ranking.yaml" in page.text
    assert '<a href="/settings">Settings</a>' in web.client.get("/").text


def test_upload_review_and_make_master(web):  # noqa: F811
    response = web.client.post(
        "/settings/cvs", files={"file": ("Ref CV.docx", _docx())}, headers=HX
    )
    assert response.headers["HX-Redirect"] == "/settings/cvs/ref-cv?uploaded=1"
    page = web.client.get("/settings/cvs/ref-cv?uploaded=1")
    assert "Converted." in page.text and "# Anna Andersson" in page.text

    saved = web.client.post("/settings/cvs/ref-cv", data={"text": "# Edited\n"}, headers=HX)
    assert "Saved." in saved.text
    master = web.config.cv_path
    response = web.client.post("/settings/cvs/ref-cv/master", headers=HX)
    assert response.headers["HX-Redirect"] == "/settings?master=1"
    assert master.read_text() == "# Edited\n"
    assert list((web.config.data_dir / "backups" / "cvs").glob("master-*.md"))
    # The web app's CV cache sees the new master (the "stale" badges use it).
    assert "re-ranked" in web.client.get("/settings?master=1").text


def test_upload_errors_and_guards(web):  # noqa: F811
    bad = web.client.post("/settings/cvs", files={"file": ("cv.exe", b"x")}, headers=HX)
    assert "Unsupported file type" in bad.text
    assert web.client.post("/settings/cvs", files={"file": ("a.md", b"# A")}).status_code == 403
    assert web.client.get("/settings/cvs/..%2Fsecret").status_code == 404
    assert web.client.get("/settings/cvs/nope").status_code == 404
    deleted = web.client.post("/settings/cvs/master/delete", headers=HX)
    assert "be deleted" in deleted.text


def test_edit_config_files(web, tmp_path):  # noqa: F811
    page = web.client.get("/settings/files/ranking")
    assert page.status_code == 200 and "target_roles" in page.text and "Example" in page.text
    assert web.client.get("/settings/files/nope").status_code == 404

    invalid = web.client.post(
        "/settings/files/ranking", data={"text": "weights: {fit: lots}"}, headers=HX
    )
    assert "Not saved" in invalid.text and "weights.fit" in invalid.text
    assert web.config.ranking_config.read_text() == RANKING_YAML  # untouched

    changed = RANKING_YAML.replace("fit: 0.6, success: 0.4", "fit: 0.9, success: 0.1")
    check = web.client.post("/settings/files/ranking/check", data={"text": changed}, headers=HX)
    assert "Valid." in check.text and web.config.ranking_config.read_text() == RANKING_YAML
    saved = web.client.post("/settings/files/ranking", data={"text": changed}, headers=HX)
    assert "Saved ranking.yaml" in saved.text and "scores update immediately" in saved.text
    assert web.config.ranking_config.read_text() == changed
    assert web.client.post("/settings/files/ranking", data={"text": changed}).status_code == 403


def test_saving_config_yaml_reloads_the_app(tmp_path):
    from fastapi.testclient import TestClient

    from jobsearcher.web import create_app

    config_path = tmp_path / "config.yaml"
    config_path.write_text("data_dir: data\n")
    (tmp_path / "cvs").mkdir()
    (tmp_path / "cvs" / "master.md").write_text("# CV\n")
    config = Config(data_dir=tmp_path / "data", cv_path=tmp_path / "cvs" / "master.md")
    with TestClient(create_app(config, config_path)) as client:
        text = "data_dir: data\ncv_path: cvs/master.md\nllm:\n  monthly_budget_usd: 7\n"
        assert (
            "Saved config.yaml"
            in client.post("/settings/files/config", data={"text": text}, headers=HX).text
        )
        assert client.app.state.web.config.llm.monthly_budget_usd == 7
        assert "$7.00" in client.get("/settings").text  # budget widget uses the new config


# --- structured forms ------------------------------------------------------------------


def _form_fields(html: str) -> list[tuple[str, str]]:
    """The fields a browser would submit from a settings page (inputs, checked boxes,
    textareas, selected options), skipping the <template> for new blocks."""
    import re

    html = re.sub(r"<template.*?</template>", "", html, flags=re.S)
    fields: list[tuple[str, str]] = []
    for tag in re.finditer(
        r"<(input|textarea|select)\b([^>]*)>(.*?)(?=</textarea>|</select>|$)?", html, re.S
    ):
        kind, attrs = tag.group(1), tag.group(2)
        name = re.search(r'name="([^"]+)"', attrs)
        if not name:
            continue
        if kind == "input":
            if 'type="checkbox"' in attrs and "checked" not in attrs:
                continue
            value = re.search(r'value="([^"]*)"', attrs)
            fields.append((name.group(1), value.group(1) if value else ""))
    for match in re.finditer(r'<textarea name="([^"]+)"[^>]*>(.*?)</textarea>', html, re.S):
        fields.append((match.group(1), match.group(2)))
    for match in re.finditer(r'<select name="([^"]+)">(.*?)</select>', html, re.S):
        selected = re.search(r"<option selected>([^<]*)</option>", match.group(2))
        fields.append((match.group(1), selected.group(1) if selected else ""))
    import html as htmllib

    return [(k, htmllib.unescape(v)) for k, v in fields]


def _post(client, url, fields, htmx=True):
    """POST form fields exactly as a browser does: urlencoded, in page order, repeats kept."""
    from urllib.parse import urlencode

    headers = {"Content-Type": "application/x-www-form-urlencoded", **(HX if htmx else {})}
    return client.post(url, content=urlencode(fields), headers=headers)


def _set(fields, name, value):
    return [(k, value if k == name else v) for k, v in fields]


RANKING_WITH_COMMENTS = """\
# My roles
target_roles:
  - name: Project manager  # the main one
    aliases: [projektledare]
    exclude_occupations: [Bygg]
  - name: HR Business Partner
    aliases: [HRBP]
preferences:
  dealbreakers: []
weights: {fit: 0.6, success: 0.4}   # keep me
"""


def test_ranking_form_round_trip_keeps_comments(web):  # noqa: F811
    web.config.ranking_config.write_text(RANKING_WITH_COMMENTS)
    page = web.client.get("/settings/ranking")
    assert page.status_code == 200 and 'value="Project manager"' in page.text
    fields = _form_fields(page.text)

    # Submitting the page unchanged changes nothing.
    unchanged = _post(web.client, "/settings/ranking", fields)
    assert "No changes." in unchanged.text

    edited = _set(fields, "role-0-aliases", "projektledare\nprojektchef")
    edited = _set(edited, "weights-fit", "0.7")
    check = _post(web.client, "/settings/ranking/check", edited)
    assert "re-ranked" in check.text
    assert web.config.ranking_config.read_text() == RANKING_WITH_COMMENTS
    saved = _post(web.client, "/settings/ranking", edited)
    assert "Saved ranking.yaml" in saved.text
    text = web.config.ranking_config.read_text()
    assert "# My roles" in text and "# the main one" in text and "# keep me" in text
    assert "aliases: [projektledare, projektchef]" in text and "fit: 0.7" in text
    assert "dealbreakers: []" in text  # an existing empty list stays
    assert "prefilter" not in text  # defaults aren't written into the file


def test_ranking_form_add_remove_and_errors(web):  # noqa: F811
    web.config.ranking_config.write_text(RANKING_WITH_COMMENTS)
    fields = _form_fields(web.client.get("/settings/ranking").text)
    without_pm = [(k, v) for k, v in fields if not k.startswith("role-0-")]
    added = without_pm + [
        ("role-new1-name", "Change manager"),
        ("role-new1-aliases", "förändringsledare"),
    ]
    assert "Saved" in _post(web.client, "/settings/ranking", added).text
    rc = load_ranking_config(web.config.ranking_config)
    assert [r.name for r in rc.target_roles] == ["HR Business Partner", "Change manager"]

    bad = _set(added, "weights-fit", "lots") + [("role-x-aliases", "orphan")]
    result = _post(web.client, "/settings/ranking", bad).text
    assert "Fit weight" in result and "no name" in result
    dup = added + [("role-y-name", "change manager")]
    assert "same name" in _post(web.client, "/settings/ranking", dup).text
    assert _post(web.client, "/settings/ranking", added, htmx=False).status_code == 403


COMPANIES = """\
companies:
  # Big ones
  - {name: Acme, website: https://acme.se, tags: [largest]}
  - {name: Beta AB, website: https://beta.se, location: Lund}
"""


def test_companies_form(web):  # noqa: F811
    web.config.companies_config.write_text(COMPANIES)
    web.store.save_company_ats("acme", "teamtailor", "https://jobb.acme.se", None, None)
    page = web.client.get("/settings/companies")
    assert page.status_code == 200
    assert 'value="Beta AB"' in page.text and ">teamtailor<" in page.text
    fields = _form_fields(page.text)
    assert "No changes." in _post(web.client, "/settings/companies", fields).text

    # Turn Beta off (an unchecked box only sends the hidden "0"), override Acme's ATS,
    # and add a company.
    edited = [(k, v) for k, v in fields if not (k == "co-1-enabled" and v == "1")]
    edited = _set(edited, "co-0-ats_type", "lever")
    edited = _set(edited, "co-0-ats_ref", "acme")
    edited += [("co-new1-name", "Gamma"), ("co-new1-enabled", "1"), ("co-new1-tags", "a, b")]
    saved = _post(web.client, "/settings/companies", edited)
    assert "Saved companies.yaml" in saved.text and "1 new company" in saved.text
    text = web.config.companies_config.read_text()
    assert "# Big ones" in text
    assert "ats: {type: lever, ref: acme}" in text
    assert "{name: Beta AB, website: https://beta.se, location: Lund, enabled: false}" in text
    assert "{name: Gamma, tags: [a, b]" in text

    half = _set(fields, "co-0-ats_type", "lever")
    assert "needs both" in _post(web.client, "/settings/companies", half).text


def test_settings_overview_links_forms(web):  # noqa: F811
    page = web.client.get("/settings").text
    assert 'href="/settings/ranking"' in page and 'href="/settings/companies"' in page


def test_companies_form_handles_a_long_list(web):  # noqa: F811
    """107 companies make ~1,300 fields, over Starlette's default limit of 1,000."""
    rows = "".join(f"  - {{name: Company {n}, website: https://c{n}.se}}\n" for n in range(150))
    web.config.companies_config.write_text("companies:\n" + rows)
    fields = _form_fields(web.client.get("/settings/companies").text)
    assert len(fields) > 1000
    assert "No changes." in _post(web.client, "/settings/companies/check", fields).text
