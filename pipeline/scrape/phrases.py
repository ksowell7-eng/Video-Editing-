"""Choosing which spans of the article are worth marking on screen.

The browser hands back one flat string for the article body plus the character
ranges of each paragraph. Everything here works on that string and returns
character spans, which the measure pass then turns into DOMRects. Keeping the
selection in Python — rather than inside the page — means it is ordinary,
testable code instead of a string of JavaScript nobody can exercise.

A good marker phrase is short enough to read in about a second, dense in
content words, and self-contained. Sentences get scored on those terms; a
sentence longer than the word cap contributes its best window instead of being
truncated at an arbitrary point.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])[\s ]+(?=[\"'“(]?[A-Z0-9])")
_WORD = re.compile(r"\S+")
_ALNUM = re.compile(r"[A-Za-z0-9]")

STOPWORDS = frozenset("""
a about above after again against all am an and any are aren't as at be because been
before being below between both but by can cannot could couldn't did didn't do does
doesn't doing don't down during each few for from further had hadn't has hasn't have
haven't having he her here hers herself him himself his how i if in into is isn't it
its itself just me more most my myself no nor not of off on once only or other ought
our ours ourselves out over own same shan't she should shouldn't so some such than
that the their theirs them themselves then there these they this those through to too
under until up very was wasn't we were weren't what when where which while who whom
why with won't would wouldn't you your yours yourself yourselves said says
""".split())


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    text: str
    score: float
    paragraph: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "score": round(self.score, 4),
            "paragraph": self.paragraph,
        }


def content_words(text: str) -> list[str]:
    return [
        w for w in re.findall(r"[A-Za-z][A-Za-z'-]+", text.lower())
        if w not in STOPWORDS and len(w) > 2
    ]


def keywords(text: str, title: str = "", limit: int = 12) -> list[str]:
    """Frequency-ranked content words, with title words promoted.

    These become the default b-roll search queries, so proper nouns matter more
    than raw counts — a name in the headline is a far better YouTube query than
    the most common verb in the body.
    """
    counts: dict[str, float] = {}
    for word in content_words(text):
        counts[word] = counts.get(word, 0) + 1.0
    for word in content_words(title):
        counts[word] = counts.get(word, 0) + 4.0
    # Capitalised mid-sentence tokens are usually names, teams, or places.
    for match in re.finditer(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-z]{2,})\b", text):
        word = match.group(1).lower()
        if word not in STOPWORDS:
            counts[word] = counts.get(word, 0) + 1.5
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [w for w, _ in ranked[:limit]]


def _score_window(words: Sequence[str]) -> float:
    """Density of content words, gently favouring windows with a proper noun."""
    if not words:
        return 0.0
    joined = " ".join(words)
    content = content_words(joined)
    density = len(content) / len(words)
    has_proper = any(w[:1].isupper() and w[1:2].islower() for w in words[1:])
    has_number = any(_ALNUM.search(w) and any(c.isdigit() for c in w) for w in words)
    return round(density + 0.15 * has_proper + 0.1 * has_number, 4)


def _best_window(sentence: str, offset: int, min_words: int, max_words: int) -> tuple[int, int, float] | None:
    matches = list(_WORD.finditer(sentence))
    if len(matches) < min_words:
        return None
    if len(matches) <= max_words:
        return (
            offset + matches[0].start(),
            offset + matches[-1].end(),
            _score_window([m.group() for m in matches]),
        )
    best: tuple[int, int, float] | None = None
    for i in range(0, len(matches) - max_words + 1):
        window = matches[i:i + max_words]
        score = _score_window([m.group() for m in window])
        # Prefer earlier windows on ties: the front of a sentence carries the
        # subject, which is what the narration is most likely to echo.
        if best is None or score > best[2]:
            best = (offset + window[0].start(), offset + window[-1].end(), score)
    return best


def select_spans(
    text: str,
    paragraphs: Iterable[dict[str, int]],
    *,
    min_words: int = 2,
    max_words: int = 7,
    max_phrases: int = 60,
) -> list[Span]:
    """One candidate span per sentence, ranked, capped at `max_phrases`."""
    spans: list[Span] = []
    for p_index, para in enumerate(paragraphs):
        start, end = int(para["start"]), int(para["end"])
        chunk = text[start:end]
        cursor = 0
        for sentence in _SENTENCE_SPLIT.split(chunk):
            if not sentence.strip():
                cursor += len(sentence)
                continue
            local = chunk.find(sentence, cursor)
            if local < 0:
                local = cursor
            cursor = local + len(sentence)
            window = _best_window(sentence, start + local, min_words, max_words)
            if not window:
                continue
            w_start, w_end, score = window
            phrase = text[w_start:w_end].strip()
            if len(phrase) < 8 or not _ALNUM.search(phrase):
                continue
            spans.append(Span(start=w_start, end=w_end, text=phrase, score=score, paragraph=p_index))

    spans.sort(key=lambda s: (-s.score, s.start))
    kept = spans[:max_phrases]
    kept.sort(key=lambda s: s.start)
    return kept
