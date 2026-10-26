"""Text normalisation: tokenisation, a deliberately light stemmer, and sentence splitting.

The stemmer is intentionally simple. It is not a Porter stemmer and does not try to
be one: its job is to make "deploy", "deployed", and "deploying" collide, and to do it
in ~15 lines that a reviewer can audit in a minute. Anything heavier belongs in an
analysis pass, not the retrieval hot path.
"""

from __future__ import annotations

import re
from functools import lru_cache

_WORD_RE = re.compile(r"[a-z0-9]+")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[]|\d)")
_BULLET_RE = re.compile(r"^([-*+]\s+|\d+[.)]\s+|>\s+)\s*")
_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANKLINES_RE = re.compile(r"\n{3,}")
# Paired emphasis/code markers only. Single `_` and `*` are excluded on purpose:
# they pair across identifiers ("user_id and account_id") and mangle them.
_MD_INLINE_RE = re.compile(r"(\*\*|__|`)(.+?)\1")

STOPWORDS = frozenset(
    """
    a about after again all also am an and any are as at be because been before being
    both but by can could did do does doing done down during each few for from further
    get gets getting got had has have having he her here hers him his how i if in into
    is it its itself just know like made make many may me might more most much must my
    need no nor not now of off on once only or other our out over own same see she
    should so some still such take than that the their them then there these they thing
    this those through to too under until up use used using very want was way we well
    were what when where which while who why will with without would you your
    """.split()
)

# Longest first so "ization" is stripped before "s".
_SUFFIXES = (
    "ational", "ization", "iveness", "fulness", "ousness", "ements",
    "ingly", "edly", "ment", "ness", "ing", "ers", "est", "ed", "es", "er", "ly", "s",
)


def normalize(text: str) -> str:
    """Collapse horizontal whitespace and excess blank lines without destroying paragraphs."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WHITESPACE_RE.sub(" ", text)
    return _BLANKLINES_RE.sub("\n\n", text).strip()


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens. Punctuation carries no signal for BM25 here."""
    return _WORD_RE.findall(text.lower())


@lru_cache(maxsize=262_144)
def stem(word: str) -> str:
    """Strip common English suffixes. Cached because the same words recur constantly."""
    if len(word) <= 3:
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[: -len(suffix)]
            break
    # "deploye" -> "deploy" after stripping "ed"? no; but "route" -> "rout" is a
    # acceptable overshoot, and it stays consistent across query and document.
    if word.endswith("e") and len(word) > 4:
        word = word[:-1]
    return word


def terms(text: str) -> list[str]:
    """Stemmed, stopword-free term list used by the lexical index."""
    return [stem(t) for t in tokenize(text) if t not in STOPWORDS]


def unique_terms(text: str) -> set[str]:
    return set(terms(text))


def split_sentences(text: str) -> list[str]:
    """Sentence split that respects line breaks and list items.

    Block structure matters: a bullet list is a list of claims, not one run-on
    sentence. Splitting per line first keeps each bullet separately citable, which
    is what makes extractive answers readable.
    """
    text = normalize(text)
    if not text:
        return []
    sentences: list[str] = []
    for line in text.split("\n"):
        line = _BULLET_RE.sub("", line.strip()).strip()
        if not line:
            continue
        sentences.extend(part.strip() for part in _SENTENCE_RE.split(line) if part.strip())
    return sentences


def truncate(text: str, limit: int = 240) -> str:
    """Trim to a word boundary so snippets read cleanly in a UI."""
    text = normalize(text)
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut + "\u2026"


def plain_markdown(text: str) -> str:
    """Remove paired emphasis and inline-code markers for display.

    Answers are assembled from source text, and a reader should not see the
    corpus's formatting syntax. Unpaired underscores survive, so identifiers like
    ``user_id`` are not mangled.
    """
    return _MD_INLINE_RE.sub(r"\2", text)


def snippet_text(text: str, limit: int = 240) -> str:
    """Flatten a chunk into one readable line for a citation card.

    Chunks keep their Markdown block structure so retrieval can see list items
    separately; a citation card does not want that structure, it wants a sentence.
    """
    lines = []
    for line in text.splitlines():
        cleaned = _BULLET_RE.sub("", line.strip()).strip()
        if cleaned and not set(cleaned) <= {"|", "-", ":", " "}:
            lines.append(plain_markdown(cleaned))
    return truncate(" \u00b7 ".join(lines), limit)
