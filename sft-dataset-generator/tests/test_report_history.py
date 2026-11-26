"""Report history: each run's report saved as data, the picker in report.html, and compare.html."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from balpipe_common.config import Settings
from balpipe_common.db import Database

from sftgen import report_history as hist
from sftgen.cli import main
from sftgen.report import write_report


def _question(db: Database, i: int, status: str = "pending") -> None:
    page = db.insert("pages", {"source": "learn", "url": f"https://ballerina.io/learn/p{i}", "status": "chunked"})
    chunk = db.insert("chunks", {"page_id": page, "source": "learn", "source_url": f"https://ballerina.io/learn/p{i}",
                                 "ordinal": 0, "heading_path": "H", "text": "t", "text_hash": f"t{i}",
                                 "token_estimate": 5, "has_code": 0, "status": "questioned"})
    db.insert("questions", {"chunk_id": chunk, "source": "learn", "source_url": f"https://ballerina.io/learn/p{i}",
                            "question": f"Question number {i}?", "question_hash": f"q{i}", "category": "api",
                            "difficulty": "beginner", "status": status})


def test_every_report_is_saved_with_changes(settings: Settings, db: Database) -> None:
    _question(db, 1)
    _, summary = write_report(settings, db, archive=True, command="generate-questions", since_run=0)
    assert "Saved" in summary and "Changes" not in summary          # first report: nothing to compare with
    before = int(db.scalar("SELECT COALESCE(MAX(id), 0) FROM runs"))
    with db.run("generate-questions", settings.limits()) as run:
        _question(db, 2)
        _question(db, 3, status="answered")
        run.count("questions_saved", 2)
    path, summary = write_report(settings, db, archive=True, command="generate-questions", since_run=before)
    assert "Questions +2" in summary and "Questions answered +1" in summary

    entries = hist.load_entries(settings)
    assert [e["command"] for e in entries] == ["generate-questions", "generate-questions"]
    assert entries[0]["metrics"]["Questions"] == 1 and entries[1]["metrics"]["Questions"] == 3
    assert [r["command"] for r in entries[1]["runs"]] == ["generate-questions"] and not entries[0]["runs"]
    hdir = hist.history_dir(settings)
    assert entries[0]["file"] != entries[1]["file"] and all((hdir / e["file"]).is_file() for e in entries)
    assert "_generate-questions" in entries[1]["file"] and entries[1]["file"].endswith(".js")
    assert not list(hdir.glob("*.html")) and not (path.parent / "index.html").exists()   # data only, no pages

    latest = path.read_text(encoding="utf-8")
    assert "This run: sftgen generate-questions" in latest and "Changes since the previous report" in latest
    saved = hist.read_data(hdir / entries[1]["file"])                         # the saved data is this report
    assert saved["command"] == "generate-questions" and saved["body"] in latest and "<html" not in saved["body"]
    # the picker: the page itself first (latest), then the older report
    picker = latest.split('<select id="pick">')[1].split("</select>")[0]
    assert picker.index("Latest · ") < picker.index(f'value="{entries[0]["file"]}"')
    assert f'value="{entries[1]["file"]}"' not in picker

    compare = hist.compare_page(settings).read_text(encoding="utf-8")
    assert "All reports (2)" in compare and f'report.html#r={entries[0]["file"]}' in compare
    data = compare.split('id="entries">')[1].split("</script>")[0]
    assert [e["file"] for e in json.loads(data)] == [e["file"] for e in entries]


def test_old_html_reports_become_data(settings: Settings, db: Database) -> None:
    hdir = hist.history_dir(settings)
    hdir.mkdir(parents=True)
    (hdir / "2026-11-26_10-34-50_report.html").write_text(
        "<html><body><main><h1>Old</h1><a href=\"../index.html\">x</a></main></body></html>")
    (hdir / "index.json").write_text(json.dumps([{"file": "2026-11-26_10-34-50_report.html", "at": "2026-11-26T10:34:50",
                                                  "command": "report", "exit_code": 0, "runs": [], "metrics": {}}]))
    (settings.report_path.parent / "index.html").write_text("<title>Report history</title>")
    write_report(settings, db, archive=True)
    entries = hist.load_entries(settings)
    assert [e["file"] for e in entries][0] == "2026-11-26_10-34-50_report.js" and len(entries) == 2
    old = hist.read_data(hdir / entries[0]["file"])
    assert old["body"] == '<h1>Old</h1><a href="compare.html">x</a>'
    assert not list(hdir.glob("*.html")) and not (settings.report_path.parent / "index.html").exists()


def test_keep_history_prunes_oldest(settings: Settings, db: Database) -> None:
    s = settings.model_copy(update={"report": settings.report.model_copy(update={"keep_history": 2})})
    for i in range(3):
        _question(db, i)
        write_report(s, db, archive=True, command=f"cmd{i}")
    entries = hist.load_entries(s)
    assert [e["command"] for e in entries] == ["cmd1", "cmd2"]
    assert sorted(p.name for p in hist.history_dir(s).glob("*.js")) == sorted(e["file"] for e in entries)


def test_plain_write_report_does_not_archive(settings: Settings, db: Database) -> None:
    write_report(settings, db)
    assert not hist.history_dir(settings).exists() and "This run" not in settings.report_path.read_text()


def test_cli_saves_a_report_per_command(settings: Settings, monkeypatch) -> None:  # noqa: ANN001
    (settings.repo_root / "config").mkdir()
    shutil.copy(Path(__file__).parents[2] / "config" / "pipeline.yaml", settings.repo_root / "config")
    monkeypatch.setenv("BALPIPE_ROOT", str(settings.repo_root))
    monkeypatch.setenv("LOCAL_DATA_DIR", str(settings.repo_root / "data"))
    for k in ("STORAGE_BUCKET",):
        monkeypatch.delenv(k, raising=False)
    assert main(["report"]) == 0
    assert main(["export"]) == 0
    entries = json.loads((settings.repo_root / "data" / "reports" / "history" / "index.json").read_text())
    assert [e["command"] for e in entries] == ["report", "export"]
    assert [r["command"] for r in entries[1]["runs"]] == ["export"]
