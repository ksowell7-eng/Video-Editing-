"""The ordered step registry.

`sections` lists the config a step's fingerprint depends on, and `depends_on`
chains upstream fingerprints, so editing one parameter re-runs exactly the
steps that parameter can affect and nothing else.
"""

from __future__ import annotations

from .base import Context, SimpleStep, StepResult, execute
from . import article, avatar, broll, compose, identity, reframe, render, script, transcribe, voice

STEPS: list[SimpleStep] = [
    SimpleStep(
        name="article",
        description="scrape the source article and measure phrase positions",
        sections=("input", "article"),
        fn=article.run,
    ),
    SimpleStep(
        name="broll",
        description="search, download and cut b-roll from YouTube",
        sections=("broll", "output"),
        fn=broll.run,
        depends_on=("article",),
    ),
    SimpleStep(
        name="reframe",
        description="auto-reframe 16:9 footage to vertical with a tracked crop",
        sections=("reframe", "output", "input"),
        fn=reframe.run,
        depends_on=("broll",),
    ),
    SimpleStep(
        name="script",
        description="draft and validate the narrator and coach lines",
        sections=("script", "phases", "output"),
        fn=script.run,
        depends_on=("article",),
    ),
    SimpleStep(
        name="voice",
        description="synthesise the voiceover with ElevenLabs",
        sections=("voice", "phases"),
        fn=voice.run,
        depends_on=("script",),
    ),
    SimpleStep(
        name="avatar",
        description="generate the coach avatar through KIE.ai seedance-2",
        sections=("avatar", "phases", "input", "output"),
        fn=avatar.run,
        depends_on=("voice",),
    ),
    SimpleStep(
        name="identity",
        description="check generated and sourced footage against a local vision model",
        sections=("identity", "input"),
        fn=identity.run,
        depends_on=("avatar", "reframe"),
    ),
    SimpleStep(
        name="transcribe",
        description="word-level timings via hyperframes transcribe",
        sections=("captions", "render"),
        fn=transcribe.run,
        depends_on=("voice",),
    ),
    SimpleStep(
        name="compose",
        description="assemble the HyperFrames composition",
        sections=("captions", "markers", "phases", "output", "input"),
        fn=compose.run,
        depends_on=("reframe", "avatar", "transcribe", "identity"),
    ),
    SimpleStep(
        name="render",
        description="lint, render and verify the final video",
        sections=("render", "output"),
        fn=render.run_step,
        depends_on=("compose",),
    ),
]

STEP_NAMES = [s.name for s in STEPS]

__all__ = ["STEPS", "STEP_NAMES", "Context", "StepResult", "SimpleStep", "execute"]
