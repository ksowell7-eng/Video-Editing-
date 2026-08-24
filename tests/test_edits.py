"""The edit loop: timecodes, op validation, and non-destructive replay."""

import json
import shutil
import subprocess

import pytest

from pipeline.edits.apply import _file_fingerprint, load_edit_list, next_version
from pipeline.edits.ops import (
    OPS, _aspect, _atempo_chain, _escape_drawtext, describe, validate,
)
from pipeline.edits.review import _layout, sheet_times
from pipeline.edits.timecode import format_tc, parse, resolve_span
from pipeline.errors import ConfigError

has_ffmpeg = shutil.which("ffmpeg") is not None


class TestTimecode:
    @pytest.mark.parametrize("value,expected", [
        (12, 12.0), ("12", 12.0), ("12.5", 12.5),
        ("1:12", 72.0), ("0:01:12.5", 72.5), ("1:00:00", 3600.0),
        ("500ms", 0.5), ("2min", 120.0), ("3s", 3.0),
        ("start", 0.0), (None, None), ("", None), ("end", None),
    ])
    def test_it_parses_how_people_write_time(self, value, expected):
        assert parse(value) == expected

    @pytest.mark.parametrize("value", ["banana", "1:99", ":", "1:2:3:4"])
    def test_nonsense_is_refused_with_a_hint(self, value):
        with pytest.raises(ConfigError):
            parse(value)

    def test_negative_time_is_refused(self):
        with pytest.raises(ConfigError, match="negative"):
            parse(-5)

    def test_it_formats_back_into_something_readable(self):
        assert format_tc(74.5) == "1:14"
        assert format_tc(74.5, millis=True) == "1:14.500"
        assert format_tc(3725.25).startswith("1:02:05")

    def test_a_span_is_clamped_to_the_clip(self):
        assert resolve_span({"from": 2, "to": 999}, 10.0) == (2.0, 10.0)

    def test_a_reversed_span_is_put_back_in_order(self):
        assert resolve_span({"from": 8, "to": 2}, 10.0) == (2.0, 8.0)

    def test_an_open_ended_span_runs_to_the_end(self):
        assert resolve_span({"from": 3}, 10.0) == (3.0, 10.0)


class TestValidation:
    def test_a_good_op_validates(self):
        assert validate({"op": "cut", "from": "0:01", "to": "0:02"}, 0) == "cut"

    def test_an_unknown_op_lists_the_real_ones(self):
        with pytest.raises(ConfigError, match="unknown op") as caught:
            validate({"op": "deblur"}, 0)
        # The hint is where the recovery path lives, so it has to carry it.
        assert "Available:" in caught.value.hint
        assert "cut" in caught.value.hint

    def test_a_missing_required_field_is_named(self):
        with pytest.raises(ConfigError, match="missing: factor"):
            validate({"op": "speed"}, 0)

    def test_a_bad_timecode_inside_an_op_is_caught_early(self):
        with pytest.raises(ConfigError, match=r"ops\[2\].from"):
            validate({"op": "cut", "from": "half past"}, 2)

    def test_every_registered_op_has_a_summary(self):
        for name, spec in OPS.items():
            assert spec.summary, name
            assert callable(spec.fn), name


class TestDescribe:
    def test_it_reads_like_the_change_that_was_asked_for(self):
        line = describe({"op": "cut", "from": "0:00", "to": "0:03.5",
                         "note": "dead air at the top"})
        assert "cut" in line and "dead air at the top" in line

    def test_fractional_times_keep_their_precision(self):
        assert "3.500" in describe({"op": "cut", "from": 0, "to": 3.5})

    def test_whole_second_times_stay_tidy(self):
        assert "0:03" in describe({"op": "cut", "from": 0, "to": 3})


class TestHelpers:
    def test_known_aspects_resolve_to_vertical_sizes(self):
        assert _aspect("9:16") == (1080, 1920)
        assert _aspect("1920x1080") == (1920, 1080)

    def test_an_unknown_aspect_is_refused(self):
        with pytest.raises(ConfigError):
            _aspect("cinemascope")

    def test_extreme_speeds_are_chained_within_atempo_limits(self):
        # atempo only accepts 0.5-100 per instance.
        chain = _atempo_chain(0.1)
        assert chain.count("atempo") > 1
        product = 1.0
        for part in chain.split(","):
            product *= float(part.split("=")[1])
        assert product == pytest.approx(0.1)

    def test_a_normal_speed_needs_only_one_stage(self):
        assert _atempo_chain(1.5).count("atempo") == 1

    def test_drawtext_escapes_what_would_break_the_filtergraph(self):
        escaped = _escape_drawtext("0:00.250 100% \\ 'quoted'")
        assert "\\:" in escaped
        assert "\\%" in escaped
        assert escaped.startswith("'") and escaped.endswith("'")


class TestVersioning:
    def test_each_round_lands_beside_the_last(self, tmp_path):
        base = tmp_path / "clip.mp4"
        first = next_version(base)
        assert first.name == "clip.v1.mp4"
        first.write_bytes(b"x")
        assert next_version(base).name == "clip.v2.mp4"

    def test_versioning_a_versioned_name_does_not_stack(self, tmp_path):
        (tmp_path / "clip.v1.mp4").write_bytes(b"x")
        assert next_version(tmp_path / "clip.v1.mp4").name == "clip.v2.mp4"

    def test_a_changed_source_changes_its_fingerprint(self, tmp_path):
        path = tmp_path / "a.mp4"
        path.write_bytes(b"one")
        before = _file_fingerprint(path)
        path.write_bytes(b"a longer set of bytes")
        assert _file_fingerprint(path) != before


class TestEditList:
    def test_it_validates_every_op_on_load(self, tmp_path):
        path = tmp_path / "e.json"
        path.write_text(json.dumps({"source": "a.mp4", "ops": [{"op": "nope"}]}))
        with pytest.raises(ConfigError, match="unknown op"):
            load_edit_list(path)

    def test_an_empty_op_list_is_allowed(self, tmp_path):
        path = tmp_path / "e.json"
        path.write_text(json.dumps({"source": "a.mp4"}))
        assert load_edit_list(path)["ops"] == []

    def test_malformed_json_names_the_file(self, tmp_path):
        path = tmp_path / "e.json"
        path.write_text("{ oops")
        with pytest.raises(ConfigError, match="not valid JSON"):
            load_edit_list(path)


class TestContactSheet:
    def test_samples_stay_inside_the_clip(self):
        times = sheet_times(24.0, 6)
        assert len(times) == 6
        assert times[0] > 0 and times[-1] < 24.0

    def test_a_single_sample_takes_the_middle(self):
        assert sheet_times(10.0, 1) == [5.0]

    def test_an_empty_clip_yields_nothing(self):
        assert sheet_times(0, 5) == []

    def test_the_grid_layout_is_row_major(self):
        assert _layout(4, 2) == "0_0|w0_0|0_h0|w0_h0"


@pytest.mark.skipif(not has_ffmpeg, reason="ffmpeg not installed")
class TestApplyIntegration:
    """A real render, because the ffmpeg arguments are the risky part."""

    @pytest.fixture
    def source(self, tmp_path):
        path = tmp_path / "src.mp4"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=15:duration=6",
            "-f", "lavfi", "-i", "sine=frequency=300:duration=6",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(path),
        ], check=True, capture_output=True)
        return path

    def _apply(self, source, ops, tmp_path, **kwargs):
        from pipeline.config import DEFAULTS
        from pipeline.edits.apply import apply_edits

        return apply_edits(
            source, ops, tmp_path / "out.mp4",
            workdir=tmp_path / "work", fps=15, job_root=tmp_path,
            reframe_cfg=DEFAULTS["reframe"], **kwargs,
        )

    def test_a_cut_shortens_the_clip_by_the_span(self, source, tmp_path):
        result = self._apply(source, [{"op": "cut", "from": 1, "to": 3}], tmp_path)
        assert result.duration_s == pytest.approx(4.0, abs=0.4)

    def test_the_source_is_never_modified(self, source, tmp_path):
        before = source.read_bytes()
        self._apply(source, [{"op": "cut", "from": 0, "to": 2}], tmp_path)
        assert source.read_bytes() == before

    def test_an_empty_op_list_still_produces_a_deliverable(self, source, tmp_path):
        result = self._apply(source, [], tmp_path)
        assert result.output.exists()
        assert result.duration_s == pytest.approx(6.0, abs=0.3)

    def test_replaying_an_unchanged_list_re_renders_nothing(self, source, tmp_path):
        ops = [{"op": "cut", "from": 1, "to": 2}]
        first = self._apply(source, ops, tmp_path)
        second = self._apply(source, ops, tmp_path)
        assert first.rendered == 1
        assert second.rendered == 0 and second.reused == 1

    def test_changing_the_last_op_reuses_the_prefix(self, source, tmp_path):
        base = [{"op": "cut", "from": 1, "to": 2}, {"op": "volume", "factor": 0.5}]
        self._apply(source, base, tmp_path)
        changed = [base[0], {"op": "volume", "factor": 0.8}]
        result = self._apply(source, changed, tmp_path)
        assert result.reused == 1 and result.rendered == 1

    def test_a_failing_op_names_which_one_it_was(self, source, tmp_path):
        with pytest.raises(ConfigError, match="op 1"):
            self._apply(source, [{"op": "trim", "from": 5.99, "to": 5.995}], tmp_path)
