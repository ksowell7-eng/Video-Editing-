"""The job file is the whole parameter surface; bad ones must fail loudly."""

import json

import pytest

from pipeline.config import Job, parse_overrides
from pipeline.errors import ConfigError


def write_job(tmp_path, **overrides):
    clip = tmp_path / "highlight.mp4"
    clip.write_bytes(b"\x00")
    job = {
        "id": "test-job",
        "input": {"highlight_clip": "highlight.mp4", "article_url": "https://example.com/a"},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(job.get(key), dict):
            job[key].update(value)
        else:
            job[key] = value
    path = tmp_path / "test.job.json"
    path.write_text(json.dumps(job))
    return path


def test_a_minimal_job_loads_with_defaults(tmp_path):
    job = Job.load(write_job(tmp_path))
    assert job.size == (1080, 1920)
    assert [p.id for p in job.phases] == ["hook", "context", "analysis", "payoff"]


def test_a_typo_in_a_parameter_name_is_rejected(tmp_path):
    path = write_job(tmp_path, outputs={"fps": 30})
    with pytest.raises(ConfigError, match="Unknown parameter 'outputs'"):
        Job.load(path)


def test_a_nested_typo_names_its_path(tmp_path):
    path = write_job(tmp_path, output={"framerate": 30})
    with pytest.raises(ConfigError, match="output.framerate"):
        Job.load(path)


def test_a_missing_highlight_clip_is_caught_before_anything_runs(tmp_path):
    path = write_job(tmp_path, input={"highlight_clip": "nope.mp4"})
    with pytest.raises(ConfigError, match="highlight clip not found"):
        Job.load(path)


def test_an_article_source_is_required(tmp_path):
    clip = tmp_path / "highlight.mp4"
    clip.write_bytes(b"\x00")
    path = tmp_path / "j.json"
    path.write_text(json.dumps({"input": {"highlight_clip": "highlight.mp4"}}))
    with pytest.raises(ConfigError, match="article_url"):
        Job.load(path)


def test_phase_durations_must_agree_with_the_target(tmp_path):
    path = write_job(tmp_path, phases=[
        {"id": "only", "voice": "narrator", "target_s": 5, "bed": "highlight"},
    ])
    with pytest.raises(ConfigError, match="phase durations sum"):
        Job.load(path)


def test_a_landscape_output_is_refused(tmp_path):
    path = write_job(tmp_path, output={"width": 1920, "height": 1080})
    with pytest.raises(ConfigError, match="landscape"):
        Job.load(path)


def test_an_unsupported_fps_is_refused(tmp_path):
    path = write_job(tmp_path, output={"fps": 25})
    with pytest.raises(ConfigError, match="fps must be 24, 30 or 60"):
        Job.load(path)


def test_a_bed_with_no_source_enabled_is_caught(tmp_path):
    path = write_job(tmp_path, broll={"enabled": False})
    with pytest.raises(ConfigError, match="bed 'broll' but broll.enabled is false"):
        Job.load(path)


def test_the_avatar_identity_check_switches_off_without_a_reference(tmp_path):
    # Nothing to compare generated frames against, so the check is dropped
    # rather than failing the whole job.
    job = Job.load(write_job(tmp_path, identity={"targets": ["avatar", "broll"]}))
    assert job["identity"]["targets"] == ["broll"]


def test_duplicate_phase_ids_are_refused(tmp_path):
    path = write_job(tmp_path, output={"target_duration_s": 10}, phases=[
        {"id": "same", "voice": "narrator", "target_s": 5, "bed": "highlight"},
        {"id": "same", "voice": "narrator", "target_s": 5, "bed": "highlight"},
    ])
    with pytest.raises(ConfigError, match="duplicate phase id"):
        Job.load(path)


def test_an_unknown_bed_is_refused(tmp_path):
    path = write_job(tmp_path, output={"target_duration_s": 10}, phases=[
        {"id": "a", "voice": "narrator", "target_s": 10, "bed": "hologram"},
    ])
    with pytest.raises(ConfigError, match="not one of"):
        Job.load(path)


def test_paths_resolve_against_the_job_file(tmp_path):
    job = Job.load(write_job(tmp_path))
    assert job.resolve("highlight.mp4") == (tmp_path / "highlight.mp4").resolve()
    assert job.resolve("/abs/path.mp4").as_posix() == "/abs/path.mp4"


def test_fingerprints_track_the_sections_they_cover(tmp_path):
    path = write_job(tmp_path)
    first = Job.load(path).fingerprint("output")
    assert Job.load(path).fingerprint("output") == first
    assert Job.load(path, {"output": {"fps": 60}}).fingerprint("output") != first


def test_a_secret_in_the_job_file_is_rejected_as_unknown(tmp_path):
    # Keys live in the environment; a job file is committed to a repo.
    path = write_job(tmp_path, voice={"api_key": "sk-secret"})
    with pytest.raises(ConfigError, match="Unknown parameter 'voice.api_key'"):
        Job.load(path)


class TestOverrides:
    def test_dotted_keys_become_nested(self):
        assert parse_overrides(["output.fps=60"]) == {"output": {"fps": 60}}

    def test_values_are_parsed_as_json_when_possible(self):
        parsed = parse_overrides(["broll.enabled=false", "broll.keep_clips=3", "id=demo"])
        assert parsed["broll"] == {"enabled": False, "keep_clips": 3}
        assert parsed["id"] == "demo"

    def test_a_malformed_override_is_refused(self):
        with pytest.raises(ConfigError, match="key=value"):
            parse_overrides(["nonsense"])
