from __future__ import annotations

from collections.abc import Sequence

import pytest

from cite.judges import LexicalJudge, ProviderJudge, get_judge
from cite.providers import parse_judgement


def test_lexical_judge_accepts_a_quoted_claim() -> None:
    judge = LexicalJudge()
    assert judge.supports(
        "Widgets cost five dollars per month",
        ["Widgets cost five dollars per month."],
    ) is True


def test_lexical_judge_rejects_unrelated_evidence() -> None:
    judge = LexicalJudge()
    assert judge.supports("Widgets cost five dollars", ["Widgets are blue."]) is False


def test_lexical_judge_threshold_is_configurable() -> None:
    judge = LexicalJudge(threshold=0.2)
    assert judge.supports("widgets blue", ["Widgets are blue."]) is True


class FakeProvider:
    name = "fake"

    def __init__(self, verdict: bool) -> None:
        self.verdict = verdict

    def judge_claim(self, claim: str, evidence: Sequence[str]) -> bool:
        assert claim
        assert evidence
        return self.verdict


@pytest.mark.parametrize("verdict", [True, False])
def test_provider_judge_delegates_to_the_provider(verdict: bool) -> None:
    judge = ProviderJudge(FakeProvider(verdict))
    assert judge.name == "fake"
    assert judge.supports("a claim", ["evidence"]) is verdict


def test_provider_judge_rejects_a_provider_without_the_method() -> None:
    with pytest.raises(ValueError, match="judge_claim"):
        ProviderJudge(object())


@pytest.mark.parametrize(
    "name, kind",
    [("lexical", LexicalJudge), ("mock", LexicalJudge), (None, LexicalJudge)],
)
def test_get_judge_defaults_to_lexical(name, kind) -> None:
    assert isinstance(get_judge(name), kind)


def test_get_judge_rejects_unknown_names() -> None:
    with pytest.raises(ValueError, match="unknown judge"):
        get_judge("oracle")


@pytest.mark.parametrize(
    "reply, expected",
    [
        ("SUPPORTED", True),
        ("supported.", True),
        ("UNSUPPORTED", False),
        ("unsupported because the source says otherwise", False),
        ("I am not sure", False),
        ("", False),
    ],
)
def test_parse_judgement_is_strict(reply: str, expected: bool) -> None:
    assert parse_judgement(reply) is expected
