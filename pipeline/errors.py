"""Typed failures. Each carries an exit code the CLI surfaces verbatim."""


class PipelineError(Exception):
    """Base for every failure the pipeline raises deliberately."""

    exit_code = 1

    def __init__(self, message: str, *, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


class ConfigError(PipelineError):
    """The job file is malformed, contradictory, or points at missing inputs."""

    exit_code = 2


class MissingDependency(PipelineError):
    """A required binary, Python package, or credential is unavailable."""

    exit_code = 3


class StepFailed(PipelineError):
    """A step ran and could not produce its declared outputs."""

    exit_code = 4


class BudgetExceeded(PipelineError):
    """A paid call would push this run past its configured cap."""

    exit_code = 5


class QualityGateFailed(PipelineError):
    """Output was produced but failed a check (identity, lint, duration drift)."""

    exit_code = 6


class NeedsScript(PipelineError):
    """The VO script is not written yet; Claude must author it and re-run.

    This is a handoff, not a crash: the step has written script_request.json
    with everything needed to draft the lines.
    """

    exit_code = 20
