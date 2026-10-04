import io

import pytest
from test_web import RANKING_YAML, web  # noqa: F401  (the `web` fixture)

from jobsearcher import cvs, settings
from jobsearcher.config import Config

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
