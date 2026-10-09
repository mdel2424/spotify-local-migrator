import os
import select
import signal
import subprocess
import sys
import time

import pytest
from test_migration import A, C, raw_track
from test_migration import job as job
from typer.testing import CliRunner

from spotify_local_migrator import cli
from spotify_local_migrator.matching.search import candidate_from_api


def prepare_review(job, settings, monkeypatch):
    store, _, report, _ = job
    decision = report.decisions[0]
    decision.candidate = None
    decision.status = "unmatched"
    decision.needs_review = True
    monkeypatch.setattr(cli, "load_settings", lambda env_file: settings)

    def no_network(*args):
        pytest.fail("Saved-candidate shortcuts must not contact Spotify")

    monkeypatch.setattr(cli, "services", no_network)
    return store, report, decision


def test_enter_approves_highest_ranked_match_and_saves_it(job, settings, monkeypatch):
    store, report, decision = prepare_review(job, settings, monkeypatch)
    # The best result is deliberately not first in the saved candidate list.
    decision.candidates.insert(0, candidate_from_api(raw_track(C, "Unrelated song")))
    store.save("matches.json", report)
    result = CliRunner().invoke(
        cli.app, ["review", "--offline", "--job", str(store.directory)], input="\n"
    )
    assert result.exit_code == 0, result.output
    saved = store.report().decisions[0]
    assert saved.status == "approved"
    assert saved.candidate.spotify_id == A
    assert saved.review_completed and not saved.needs_review
    assert "Enter: accept top match; Esc: leave unchanged" in result.output


def test_escape_leaves_track_unmatched_and_saves_it(job, settings, monkeypatch):
    store, report, _ = prepare_review(job, settings, monkeypatch)
    store.save("matches.json", report)
    result = CliRunner().invoke(
        cli.app, ["review", "--offline", "--job", str(store.directory)], input="\x1b\n"
    )
    assert result.exit_code == 0, result.output
    saved = store.report().decisions[0]
    assert saved.status == "rejected" and saved.candidate is None
    assert saved.review_completed and not saved.needs_review


def test_enter_with_no_candidates_leaves_track_unmatched(job, settings, monkeypatch):
    store, report, decision = prepare_review(job, settings, monkeypatch)
    decision.candidates.clear()
    store.save("matches.json", report)
    result = CliRunner().invoke(
        cli.app, ["review", "--offline", "--job", str(store.directory)], input="\n"
    )
    assert result.exit_code == 0, result.output
    assert "Enter/Esc: leave unchanged" in result.output
    saved = store.report().decisions[0]
    assert saved.status == "rejected" and saved.candidate is None


def test_enter_does_not_approve_unavailable_top_match(job, settings, monkeypatch):
    store, report, decision = prepare_review(job, settings, monkeypatch)
    decision.candidates[0].is_playable = False
    store.save("matches.json", report)
    result = CliRunner().invoke(
        cli.app, ["review", "--offline", "--job", str(store.directory)], input="\n\x1b\n"
    )
    assert result.exit_code == 0, result.output
    assert "availability is unconfirmed" in result.output
    saved = store.report().decisions[0]
    assert saved.status == "rejected" and saved.candidate is None


def test_viewing_other_matches_does_not_accept_top_and_numbered_choice_is_saved(
    job, settings, monkeypatch
):
    store, report, decision = prepare_review(job, settings, monkeypatch)
    decision.candidates.append(candidate_from_api(raw_track(C, "Alternate song")))
    store.save("matches.json", report)
    # Two matches: show other matches is 6; then choose the second result.
    result = CliRunner().invoke(
        cli.app, ["review", "--offline", "--job", str(store.directory)], input="6\n2\n"
    )
    assert result.exit_code == 0, result.output
    before, after = result.output.split("Other matches", 1)
    assert "Alternate song" not in before
    assert "Alternate song" in after
    saved = store.report().decisions[0]
    assert saved.candidate.spotify_id == C
    assert saved.status == "approved"
    assert saved.review_completed and not saved.needs_review


@pytest.mark.skipif(os.name != "posix", reason="PTY shortcuts require a POSIX terminal")
@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        (b"\n", 1),
        (b"\x1b", -1),  # No newline: Escape must work immediately.
        (b"12\n", 12),
        (b"12\x7f\n", 1),
        (b"12\x1b", -1),
        (b"\x1b[A\n", 1),  # Up arrow must not act as Escape.
        (b"\x1bOP\n", 1),  # F1 must not act as Escape.
        (b"invalid\n2\n", 2),
        (b"\x04", "EOFError"),
        (None, "KeyboardInterrupt"),
    ],
)
def test_real_terminal_shortcuts_and_settings_restoration(keys, expected):
    import termios

    master, slave = os.openpty()
    previous = termios.tcgetattr(slave)
    code = """
from rich.console import Console
from spotify_local_migrator.ui.prompts import ReviewChoicePrompt
try:
    result = ReviewChoicePrompt.ask('Choice', default=1, console=Console(force_terminal=False))
except (EOFError, KeyboardInterrupt) as error:
    result = type(error).__name__
print(f'RESULT:{result}', flush=True)
"""
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", code], stdin=slave, stdout=slave, stderr=slave
    )
    output = b""
    deadline = time.monotonic() + 5

    def read_until(marker):
        nonlocal output
        while marker not in output and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                output += os.read(master, 4096)
        assert marker in output, output.decode(errors="replace")

    try:
        read_until(b"Choice")
        assert not termios.tcgetattr(slave)[3] & termios.ICANON
        if keys is None:
            process.send_signal(signal.SIGINT)
        else:
            os.write(master, keys)
        read_until(f"RESULT:{expected}".encode())
        assert process.wait(timeout=2) == 0
        assert termios.tcgetattr(slave) == previous
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        os.close(master)
        os.close(slave)
