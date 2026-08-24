"""Step 1 — scrape the source article with headless Chrome.

Produces four things the rest of the run depends on:

  article.json    title, byline, readable body text, derived keywords
  phrases.json    candidate marker phrases with their measured DOMRects
  article.png     full-page screenshot the markers are drawn over
  page geometry   so the composition can place that screenshot precisely

Text is captured from the live, laid-out page rather than from raw HTML,
because the rect measurement has to come from the same render — and because
half the article sites in existence assemble their body text in JavaScript.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..errors import MissingDependency, StepFailed
from ..scrape.phrases import keywords, select_spans
from ..shell import chrome
from .base import Context, StepResult
from .. import logs

_JS_PATH = Path(__file__).resolve().parent.parent / "scrape" / "article.js"
_SCREENSHOT_SCALE = 2
_MAX_SCREENSHOT_PX = 12000


def _playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
    except ImportError as exc:
        raise MissingDependency(
            "playwright is required to scrape the article",
            hint="pip install playwright && playwright install chromium",
        ) from exc
    return sync_playwright


def _launch(pw):
    """Launch Chromium, preferring an explicitly configured binary."""
    args = ["--no-sandbox", "--disable-dev-shm-usage", "--hide-scrollbars"]
    explicit = os.environ.get("CHROME_EXECUTABLE")
    if explicit:
        return pw.chromium.launch(executable_path=explicit, args=args)
    try:
        return pw.chromium.launch(args=args)
    except Exception as exc:  # noqa: BLE001 - fall back to a discovered browser
        logs.debug(f"playwright's bundled chromium unavailable ({exc}); falling back to a system browser")
        return pw.chromium.launch(executable_path=chrome(), args=args)


def scrape(url: str | None, html_file: Path | None, cfg: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    sync_playwright = _playwright()
    script = _JS_PATH.read_text(encoding="utf-8")
    viewport = {"width": int(cfg["viewport"]["width"]), "height": int(cfg["viewport"]["height"])}
    screenshot_path = out_dir / "article.png"

    with sync_playwright() as pw:
        browser = _launch(pw)
        try:
            context = browser.new_context(
                viewport=viewport,
                device_scale_factor=_SCREENSHOT_SCALE,
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            timeout_ms = int(cfg["timeout_s"]) * 1000

            if html_file is not None:
                page.goto(html_file.resolve().as_uri(), wait_until="load", timeout=timeout_ms)
            else:
                try:
                    page.goto(url, wait_until=cfg["wait_until"], timeout=timeout_ms)
                except Exception:  # noqa: BLE001 - networkidle never settles on many news sites
                    logs.warn(f"'{cfg['wait_until']}' never settled; continuing on domcontentloaded")
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

            # Nudge lazy-loaded images and body text into existence, then return
            # to the top so the screenshot starts where the layout expects.
            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(700)
            page.evaluate("() => window.scrollTo(0, 0)")
            page.wait_for_timeout(250)

            page.add_script_tag(content=script)
            extracted = page.evaluate(
                "([selectors, minChars]) => window.__vs.extract(selectors, minChars)",
                [cfg["selectors"], int(cfg["min_chars"])],
            )
            if not extracted or not extracted.get("ok"):
                raise StepFailed(
                    f"No article body found at {url or html_file}",
                    hint=(
                        "Add a CSS selector for this site to article.selectors, or lower "
                        "article.min_chars if the piece is genuinely short."
                    ),
                )

            spans = select_spans(
                extracted["text"],
                extracted["paragraphs"],
                min_words=int(cfg["phrase_min_words"]),
                max_words=int(cfg["phrase_max_words"]),
                max_phrases=int(cfg["max_phrases"]),
            )
            measured = page.evaluate(
                "(spans) => window.__vs.measure(spans)",
                [{"start": s.start, "end": s.end} for s in spans],
            )
            rects = measured.get("rects", []) if measured.get("ok") else []

            phrases: list[dict[str, Any]] = []
            for span, geometry in zip(spans, rects):
                if not geometry:
                    continue
                phrases.append({
                    **span.to_dict(),
                    "text": " ".join(span.text.split()),
                    "rect": geometry["rect"],
                    "union": geometry["union"],
                    "wrapped": geometry["lines"] > 1,
                })

            page_geometry = extracted["page"]
            if cfg["screenshot"]:
                # A 30,000px article would produce a screenshot no encoder wants;
                # clip to a sane height that still covers every measured phrase.
                lowest = max((p["union"]["y"] + p["union"]["h"] for p in phrases), default=0)
                height = min(
                    float(page_geometry["height"]),
                    max(float(viewport["height"]), lowest + 400),
                    _MAX_SCREENSHOT_PX,
                )
                page.screenshot(
                    path=str(screenshot_path),
                    clip={"x": 0, "y": 0, "width": float(page_geometry["width"]), "height": height},
                )
                page_geometry = {"width": page_geometry["width"], "height": height}
        finally:
            browser.close()

    body = extracted.get("readable") or extracted["text"]
    return {
        "url": url or (html_file.resolve().as_uri() if html_file else ""),
        "title": " ".join((extracted.get("title") or "").split()),
        "byline": extracted.get("byline", ""),
        "published": extracted.get("published", ""),
        "description": extracted.get("description", ""),
        "selector": extracted.get("selector"),
        "text": body,
        "word_count": len(body.split()),
        "keywords": keywords(body, extracted.get("title", "")),
        "page": page_geometry,
        "screenshot": str(screenshot_path) if cfg["screenshot"] and screenshot_path.exists() else None,
        "phrases": phrases,
    }


def run(ctx: Context) -> StepResult:
    cfg = ctx.job["article"]
    out_dir = ctx.dir_for("article")
    inp = ctx.job["input"]

    result = scrape(
        inp.get("article_url"),
        ctx.job.resolve(inp.get("article_html")),
        cfg,
        out_dir,
    )

    if result["word_count"] < 60:
        raise StepFailed(
            f"Article body is only {result['word_count']} words — too thin to script from",
            hint="Check article.selectors; the scrape probably grabbed a teaser, not the body.",
        )

    phrases = result.pop("phrases")
    article_json = out_dir / "article.json"
    phrases_json = out_dir / "phrases.json"
    article_json.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    phrases_json.write_text(json.dumps(phrases, indent=2, ensure_ascii=False), encoding="utf-8")

    logs.info(
        f"scraped {result['word_count']} words, {len(phrases)} measurable phrases",
        title=result["title"][:60] or "(untitled)",
    )
    if len(phrases) < 4:
        logs.warn("few measurable phrases; markers will be sparse")

    outputs = [article_json, phrases_json]
    if result.get("screenshot"):
        outputs.append(Path(result["screenshot"]))

    return StepResult(
        outputs=outputs,
        data={
            "article_json": str(article_json),
            "phrases_json": str(phrases_json),
            "screenshot": result.get("screenshot"),
            "page": result["page"],
            "title": result["title"],
            "keywords": result["keywords"],
            "word_count": result["word_count"],
            "phrase_count": len(phrases),
        },
    )
