"""Resumability: a re-run must not repeat work that would re-bill."""

import json

import pytest

from pipeline.state import RunState
from pipeline.steps.render import _render_warnings


@pytest.fixture
def state(tmp_path):
    return RunState(tmp_path, "job")


def test_a_completed_step_is_fresh_for_the_same_fingerprint(state, tmp_path):
    output = tmp_path / "out.mp4"
    output.write_bytes(b"x")
    state.complete("voice", fingerprint="abc", outputs=[output])
    assert state.is_fresh("voice", "abc")


def test_a_changed_fingerprint_invalidates_the_step(state):
    state.complete("voice", fingerprint="abc")
    assert not state.is_fresh("voice", "different")


def test_a_deleted_output_invalidates_the_step(state, tmp_path):
    output = tmp_path / "out.mp4"
    output.write_bytes(b"x")
    state.complete("voice", fingerprint="abc", outputs=[output])
    output.unlink()
    assert not state.is_fresh("voice", "abc")


def test_a_failed_step_is_never_fresh(state):
    state.fail("voice", "provider timed out")
    assert not state.is_fresh("voice", "abc")


def test_state_survives_a_restart(state, tmp_path):
    state.complete("article", fingerprint="f", data={"title": "T"})
    reopened = RunState(tmp_path, "job")
    assert reopened.is_fresh("article", "f")
    assert reopened.data("article")["title"] == "T"


def test_a_corrupt_state_file_starts_clean_instead_of_crashing(tmp_path):
    (tmp_path / "state.json").write_text("{ truncated")
    assert RunState(tmp_path, "job").steps == {}


def test_data_from_an_unrun_step_is_empty(state):
    assert state.data("never-ran") == {}


def test_the_state_file_stays_valid_json(state):
    state.complete("article", fingerprint="f", data={"n": 1})
    blob = json.loads((state.file).read_text())
    assert blob["steps"]["article"]["status"] == "ok"


class TestRenderWarnings:
    """The render can succeed and still be wrong; this is how that is caught."""

    def test_it_finds_codes_in_the_json_trace(self):
        line = '{"warningCodes":["sub_timeline_script_failure"],"framesCompleted":300}'
        assert "sub_timeline_script_failure" in _render_warnings(line)

    def test_it_finds_codes_in_the_human_readable_log(self):
        line = "  [sub_timeline_script_failure] A sub-composition timeline script failed to load"
        assert "sub_timeline_script_failure" in _render_warnings(line)

    def test_a_clean_render_reports_nothing(self):
        assert _render_warnings('{"warningCodes":[],"framesCompleted":300}') == set()

    def test_empty_output_is_safe(self):
        assert _render_warnings("") == set()
