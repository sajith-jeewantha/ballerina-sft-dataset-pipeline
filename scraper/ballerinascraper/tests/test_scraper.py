from __future__ import annotations

from pathlib import Path

import pytest

from balpipe_common.config import load_settings
from ballerinascraper.collectors.central import stable_package_view
from ballerinascraper.collectors.github import media_type_for, path_selected
from balpipe_common.jsonl_io import iter_raw_dicts
from ballerinascraper.raw_sink import RawSink


def test_glob_selection() -> None:
    inc = ["examples/*/*.bal", "swan-lake/**/*.md", "examples/index.json"]
    exc = ["examples/*/tests/**", "swan-lake/**/release-notes/**"]
    assert path_selected("examples/hello-world/hello_world.bal", inc, exc)
    assert path_selected("examples/index.json", inc, exc)
    assert path_selected("swan-lake/a/b/c.md", inc, exc)
    assert path_selected("swan-lake/c.md", inc, exc)
    assert not path_selected("examples/hello-world/tests/t.bal", inc, exc)
    assert not path_selected("examples/a/b/deep.bal", inc, exc)
    assert not path_selected("swan-lake/x/release-notes/r.md", inc, exc)
    assert media_type_for("x/y.metatags") == "text/x-metatags"


def test_central_view_drops_volatile_fields() -> None:
    pkg = {"organization": "ballerina", "name": "http", "version": "1", "balaURL": "https://x?Signature=abc",
           "pullCount": 5, "readme": "# r", "modules": [{"name": "http", "apiDocURL": "u", "packageURL": "p"}]}
    view = stable_package_view(pkg)
    assert "balaURL" not in view and "pullCount" not in view
    assert view["modules"] == [{"name": "http", "summary": None, "apiDocURL": "u"}]


@pytest.fixture()
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "pipeline.yaml").write_text("data_dir: data\n", encoding="utf-8")
    monkeypatch.setenv("BALPIPE_ROOT", str(tmp_path))
    return load_settings(root=tmp_path)


def _emit(sink: RawSink, body: str) -> object:
    return sink.emit(source_uri="https://x/a", media_type="text/plain", body=body,
                     requested_url="https://x/a", final_url="https://x/a", http_status=200)


def test_raw_sink_skips_unchanged_content_across_runs(settings) -> None:
    with RawSink(settings, "web", fetcher="test") as s1:
        assert _emit(s1, "hello") is not None
        assert _emit(s1, "hello") is None  # same run, same content
    with RawSink(settings, "web", fetcher="test") as s2:
        assert _emit(s2, "hello") is None  # previous run already has it
        rec = _emit(s2, "hello v2")
        assert rec is not None and rec.raw_id.startswith("raw_")
    recs = list(iter_raw_dicts(settings.data_path("raw", "web")))
    assert [r["body"] for r in recs] == ["hello", "hello v2"]
    manifests = sorted(settings.data_path("raw", "web").glob("*/_manifest.json"))
    assert len(manifests) == 2


def test_canonical_url_strips_tracking() -> None:
    from ballerinascraper.spiders.web import canonical_url

    assert canonical_url("https://b.io/p?utm_source=x&id=2#top") == "https://b.io/p?id=2"


def test_identity_matches_collector_identity_uris() -> None:
    from ballerinascraper.raw_sink import identity_of

    central = {"source_uri": "https://central.ballerina.io/ballerina/http/2.1.0",
               "central": {"org": "ballerina", "package": "http"}, "git": None}
    git = {"source_uri": "https://github.com/o/r/blob/abc123/p.md",
           "git": {"repo": "o/r", "ref": "master", "path": "p.md"}, "central": None}
    # these must equal the identity_uri values the Central / GitHub collectors pass to emit()
    assert identity_of(central) == "https://central.ballerina.io/ballerina/http"
    assert identity_of(git) == "https://github.com/o/r/blob/master/p.md"
