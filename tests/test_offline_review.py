import pytest
from rich.console import Console
from test_migration import job as job
from typer.testing import CliRunner

from spotify_local_migrator import cli
from spotify_local_migrator.errors import SpotifyAPIError
from spotify_local_migrator.matching.models import PreparedTrack
from spotify_local_migrator.workflow import match_job


def test_partial_offline_review_does_not_contact_spotify(job, settings, monkeypatch):
    store, _, report, _ = job
    report.matching_complete = False
    decision = report.decisions[0]
    decision.candidate = None
    decision.status = "unmatched"
    decision.needs_review = True
    store.save("matches.json", report)
    monkeypatch.setattr(cli, "load_settings", lambda env_file: settings)

    def forbidden(*args):
        raise AssertionError("Offline review must not open services.")

    monkeypatch.setattr(cli, "services", forbidden)
    # One candidate -> leave option 3.
    result = CliRunner().invoke(
        cli.app, ["review", "--offline", "--job", str(store.directory)], input="3\n"
    )
    assert result.exit_code == 0, result.output
    saved = store.report()
    assert not saved.matching_complete
    assert saved.decisions[0].status == "rejected"
    assert saved.decisions[0].review_completed


def test_offline_manual_option_stays_in_review(job, settings, monkeypatch):
    store, _, report, _ = job
    decision = report.decisions[0]
    decision.candidate = None
    decision.status = "unmatched"
    decision.needs_review = True
    store.save("matches.json", report)
    monkeypatch.setattr(cli, "load_settings", lambda env_file: settings)
    result = CliRunner().invoke(
        cli.app, ["review", "--offline", "--job", str(store.directory)], input="2\n3\n"
    )
    assert result.exit_code == 0, result.output
    assert "requires a live connection" in result.output
    assert store.report().decisions[0].status == "rejected"


def test_resume_refreshes_cleanup_but_preserves_human_decisions(job, settings, monkeypatch):
    store, _, report, _ = job
    decision = report.decisions[0]
    decision.review_completed = False
    decision.prepared = PreparedTrack(
        title="Uploader - Westside Gunn - Elizabeth",
        core_title="dirty title",
        artists=["Westside Gunn"],
        primary_artists=["Westside Gunn"],
        artist_source="title",
    )
    decision.searched_queries = ["old query"]
    report.matching_complete = False
    store.save("matches.json", report)
    seen = []

    class Search:
        account_id = "current-account"
        requests = 0

        def __init__(self, *args, **kwargs):
            pass

        def for_track(self, prepared):
            seen.append(prepared.title)
            raise SpotifyAPIError("test interruption")

    class Client:
        def current_user(self):
            return {"account_id": "current-account"}

    monkeypatch.setattr("spotify_local_migrator.workflow.CatalogueSearch", Search)
    with pytest.raises(SpotifyAPIError):
        match_job(settings, Client(), Console(quiet=True), store=store)
    assert seen == ["Elizabeth"]
    saved = store.report()
    assert len(saved.decisions) == 2
    assert all(decision.review_completed for decision in saved.decisions)
