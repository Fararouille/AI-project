from __future__ import annotations

from cite.text import (
    normalize,
    plain_markdown,
    split_sentences,
    stem,
    terms,
    tokenize,
    truncate,
)


def test_tokenize_lowercases_and_drops_punctuation() -> None:
    assert tokenize("Rate-Limit: 600/min!") == ["rate", "limit", "600", "min"]


def test_stem_collapses_inflections() -> None:
    assert stem("deploying") == stem("deployed") == stem("deploy")
    assert stem("policies") == "policy"
    assert stem("queries") == "query"


def test_short_words_are_untouched() -> None:
    assert stem("api") == "api"
    assert stem("sso") == "sso"


def test_terms_remove_stopwords_and_stem() -> None:
    assert terms("The events were being retried") == ["event", "retri"]


def test_normalize_preserves_paragraphs() -> None:
    messy = "a   b\r\n\r\n\r\n\r\nc"
    assert normalize(messy) == "a b\n\nc"


def test_split_sentences_handles_multiple_terminators() -> None:
    sentences = split_sentences("First one. Second one! Third one? Fourth.")
    assert sentences == ["First one.", "Second one!", "Third one?", "Fourth."]


def test_plain_markdown_strips_paired_markers() -> None:
    assert plain_markdown("**two minutes** and `GET /v2`") == "two minutes and GET /v2"
    # A lone underscore is part of the identifier, not emphasis.
    assert plain_markdown("user_id and account_id") == "user_id and account_id"


def test_truncate_cuts_on_word_boundary() -> None:
    result = truncate("alpha beta gamma delta", 12)
    assert result.endswith("\u2026")
    assert not result[:-1].endswith(" ")
    assert len(result) <= 13
