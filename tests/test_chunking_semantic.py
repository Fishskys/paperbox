"""Unit tests for semantic chunking (plan 2026-09-28_160551 §2 T7.2).

The embedding callable is always a local fake: ``embed_fn=None`` keeps the
length policy, so nothing here needs (or touches) the embedding server. The
vectors only have to make the *comparison* meaningful -- "identical sentences
embed identically, different topics do not" -- which is all
:func:`app.parsing.chunking.similarity_dips` looks at.
"""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from app.core.config import CHUNK_MODES, Settings
from app.parsing.chunking import (
    CHUNK_MODE_LENGTH,
    CHUNK_MODE_SEMANTIC,
    CHUNK_MODES as CHUNKING_MODES,
    MAX_TOKENS,
    SEMANTIC_MIN_TOKENS,
    SEMANTIC_SIMILARITY_THRESHOLD,
    chunk_document,
    cosine_similarity,
    similarity_dips,
    split_sentences,
)
from app.parsing.pdf import PageText
from app.parsing.structure import Section

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

TOPIC_ALPHA = "alpha"
TOPIC_BETA = "beta"


class FakeEmbedder:
    """Deterministic stand-in for ``embedding_service.embed_texts``.

    A sentence containing a marker word becomes the unit vector of that topic;
    anything else becomes a shared "background" vector. Sentences of the same
    topic therefore compare at 1.0 and sentences of different topics at 0.0,
    which puts one clean dip exactly at the topic change.
    """

    def __init__(self, topics: tuple[str, ...] = (TOPIC_ALPHA, TOPIC_BETA)) -> None:
        self.topics = topics
        self.calls: list[list[str]] = []

    def _vector(self, text: str) -> list[float]:
        size = len(self.topics) + 1
        for index, topic in enumerate(self.topics):
            if topic in text.lower():
                vector = [0.0] * size
                vector[index] = 1.0
                return vector
        return [0.1] * size

    def __call__(self, texts) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vector(text) for text in texts]


def sentence(topic: str, index: int, length: int = 6) -> str:
    """A long-ish sentence about ``topic`` (length in words)."""
    body = " ".join(f"{topic}{index}w{word}" for word in range(length))
    return f"{topic} sentence {index} {body}."


def section(
    topic: str,
    count: int,
    *,
    number: str | None = None,
    page: int = 1,
    length: int = 6,
    sentences_per_paragraph: int = 3,
) -> Section:
    """One section whose sentences are all about ``topic``."""
    sentences = [sentence(topic, index, length) for index in range(count)]
    paragraphs: list[tuple[int, str]] = []
    for start in range(0, len(sentences), sentences_per_paragraph):
        paragraphs.append((page, " ".join(sentences[start : start + sentences_per_paragraph])))
    return Section(
        title=f"{topic} study",
        page_start=page,
        page_end=page,
        number=number,
        paragraphs=paragraphs,
    )


def pages_for(*sections: Section) -> list[PageText]:
    numbers = sorted({page for item in sections for page, _ in item.paragraphs})
    return [PageText(page=number, text=f"page {number}") for number in numbers or [1]]


#: Small floor so a six-sentence section can actually be cut in a test; the
#: shipped default (200 tokens) is exercised separately.
SMALL_MIN_TOKENS = 20


# --------------------------------------------------------------------------- #
# split_sentences
# --------------------------------------------------------------------------- #


def test_split_sentences_keeps_terminators() -> None:
    assert split_sentences("One thing. Two things! Three?") == [
        "One thing.",
        "Two things!",
        "Three?",
    ]


def test_split_sentences_does_not_break_on_abbreviations_or_initials() -> None:
    text = "See Fig. 3 for details. J. Smith et al. reported 9 cases. Done."
    assert split_sentences(text) == [
        "See Fig. 3 for details.",
        "J. Smith et al. reported 9 cases.",
        "Done.",
    ]


def test_split_sentences_breaks_on_newlines_and_cjk_terminators() -> None:
    assert split_sentences("1 Introduction\nText follows.") == [
        "1 Introduction",
        "Text follows.",
    ]
    assert split_sentences("本文提出方法。实验表明有效！结束") == [
        "本文提出方法。",
        "实验表明有效！",
        "结束",
    ]


def test_split_sentences_of_blank_text_is_empty() -> None:
    assert split_sentences("   \n\n ") == []


# --------------------------------------------------------------------------- #
# cosine_similarity / similarity_dips
# --------------------------------------------------------------------------- #


def test_cosine_similarity_handles_identical_and_orthogonal_vectors() -> None:
    assert cosine_similarity([1.0, 0.0], [2.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 3.0]) == pytest.approx(0.0)
    # Degenerate and mismatched inputs must not raise.
    assert cosine_similarity([], [1.0]) == 0.0
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0


def test_similarity_dips_finds_an_isolated_dip() -> None:
    vectors = [[1.0, 0.0], [1.0, 0.0], [0.0, 1.0], [0.0, 1.0]]
    assert similarity_dips(vectors) == {1}


def test_similarity_dips_is_empty_when_nothing_is_low() -> None:
    vectors = [[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]]
    assert similarity_dips(vectors) == set()


def test_similarity_dips_needs_two_vectors() -> None:
    assert similarity_dips([[1.0, 0.0]]) == set()
    assert similarity_dips([]) == set()


def test_similarity_dips_respects_the_threshold() -> None:
    """A boundary that is low but not *below* the threshold is not a boundary."""
    vectors = [[1.0, 0.0], [3.0, 4.0]]  # cosine = 0.6
    assert similarity_dips(vectors, threshold=0.5) == set()
    assert similarity_dips(vectors, threshold=0.7) == {0}


# --------------------------------------------------------------------------- #
# semantic vs length policy
# --------------------------------------------------------------------------- #


def test_fake_embeddings_cut_at_the_topic_change() -> None:
    """The dip between the two topics is where the semantic chunk ends."""
    alpha = section(TOPIC_ALPHA, 3, number="1")
    beta = section(TOPIC_BETA, 3, number="2")
    pages = pages_for(alpha, beta)

    length_chunks = chunk_document(pages, [alpha, beta])
    assert len(length_chunks) == 2  # one per section, no split inside

    embedder = FakeEmbedder()
    semantic_chunks = chunk_document(
        pages,
        [alpha, beta],
        embed_fn=embedder,
        semantic_min_tokens=SMALL_MIN_TOKENS,
    )
    assert embedder.calls, "semantic mode must ask the embedder for vectors"

    # One section per topic, one chunk per section: the split happens at the
    # *section* border here, which is the point -- semantic mode must not put
    # text from the other topic into a chunk.
    assert len(semantic_chunks) == 2
    assert TOPIC_BETA not in semantic_chunks[0].text
    assert TOPIC_ALPHA not in semantic_chunks[1].text
    assert [chunk.section for chunk in semantic_chunks] == ["1 alpha study", "2 beta study"]
    assert [chunk.chunk_index for chunk in semantic_chunks] == [0, 1]


def test_a_dip_inside_one_section_splits_it() -> None:
    """Two topics inside a single section: length mode merges, semantic cuts."""
    single = Section(
        title="Mixed",
        page_start=1,
        page_end=1,
        paragraphs=[
            (
                1,
                " ".join(
                    [sentence(TOPIC_ALPHA, index) for index in range(3)]
                    + [sentence(TOPIC_BETA, index) for index in range(3)]
                ),
            )
        ],
    )
    pages = pages_for(single)

    length_chunks = chunk_document(pages, [single])
    assert len(length_chunks) == 1

    embedder = FakeEmbedder()
    semantic_chunks = chunk_document(
        pages,
        [single],
        embed_fn=embedder,
        semantic_min_tokens=SMALL_MIN_TOKENS,
    )
    assert len(semantic_chunks) == 2
    assert TOPIC_BETA not in semantic_chunks[0].text
    # The second chunk carries the overlap window, so it may still mention the
    # other topic; it must contain the beta sentences though.
    assert "beta sentence 0" in semantic_chunks[1].text
    assert all(chunk.section == "Mixed" for chunk in semantic_chunks)


def test_no_dip_means_the_length_policy_is_kept() -> None:
    alpha = section(TOPIC_ALPHA, 9, sentences_per_paragraph=1)
    pages = pages_for(alpha)
    length_chunks = chunk_document(pages, [alpha], target_tokens=60, max_tokens=80)
    embedder = FakeEmbedder()
    semantic_chunks = chunk_document(
        pages,
        [alpha],
        target_tokens=60,
        max_tokens=80,
        embed_fn=embedder,
        semantic_min_tokens=SMALL_MIN_TOKENS,
    )
    assert embedder.calls, "the section still has to be embedded"
    assert [chunk.text for chunk in semantic_chunks] == [
        chunk.text for chunk in length_chunks
    ]


def test_semantic_mode_keeps_every_sentence_in_order() -> None:
    """Regrouping sentences must not drop one, and must not reorder pages."""
    single = Section(
        title="Two pages",
        page_start=1,
        page_end=2,
        paragraphs=[
            (1, " ".join(sentence(TOPIC_ALPHA, index) for index in range(6))),
            (2, " ".join(sentence(TOPIC_BETA, index) for index in range(6))),
        ],
    )
    chunks = chunk_document(
        pages_for(single),
        [single],
        embed_fn=FakeEmbedder(),
        semantic_min_tokens=SMALL_MIN_TOKENS,
    )
    flat = " ".join(" ".join(chunk.text for chunk in chunks).split())
    source = " ".join(" ".join(text for _, text in single.paragraphs).split())
    for source_sentence in split_sentences(source):
        assert source_sentence in flat, f"lost: {source_sentence!r}"

    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))
    assert [chunk.page_start for chunk in chunks] == sorted(
        chunk.page_start for chunk in chunks
    )
    assert chunks[0].page_start == 1
    assert chunks[-1].page_end == 2


def test_semantic_pieces_keep_paragraph_breaks() -> None:
    """A paragraph break inside a chunk stays a break (joined with a blank line)."""
    two_paragraphs = Section(
        title="Paras",
        page_start=1,
        page_end=1,
        paragraphs=[
            (1, " ".join(sentence(TOPIC_ALPHA, index) for index in range(3))),
            (1, " ".join(sentence(TOPIC_BETA, index) for index in range(3))),
        ],
    )
    chunks = chunk_document(
        pages_for(two_paragraphs),
        [two_paragraphs],
        # Floor high enough that the dip between the two paragraphs is ignored:
        # the chunk must then contain both paragraphs, separated properly.
        embed_fn=FakeEmbedder(),
    )
    assert len(chunks) == 1
    assert "\n\n" in chunks[0].text


def test_length_mode_never_calls_the_embedder() -> None:
    alpha = section(TOPIC_ALPHA, 4, sentences_per_paragraph=1)
    pages = pages_for(alpha)
    embedder = FakeEmbedder()
    chunk_document(pages, [alpha], embed_fn=None)
    assert embedder.calls == []


def test_semantic_mode_never_crosses_a_section_boundary() -> None:
    alpha = section(TOPIC_ALPHA, 6, number="1", sentences_per_paragraph=2)
    beta = section(TOPIC_BETA, 6, number="2", sentences_per_paragraph=2)
    pages = pages_for(alpha, beta)
    chunks = chunk_document(
        pages,
        [alpha, beta],
        target_tokens=60,
        max_tokens=80,
        embed_fn=FakeEmbedder(),
        semantic_min_tokens=SMALL_MIN_TOKENS,
    )
    by_section: dict[str, list[str]] = {}
    for chunk in chunks:
        by_section.setdefault(chunk.section or "", []).append(chunk.text)
    assert set(by_section) == {"1 alpha study", "2 beta study"}
    assert TOPIC_BETA not in " ".join(by_section["1 alpha study"])
    assert TOPIC_ALPHA not in " ".join(by_section["2 beta study"])


def test_semantic_mode_keeps_page_ranges_and_ordering() -> None:
    first = section(TOPIC_ALPHA, 3, number="1", page=1)
    second = section(TOPIC_BETA, 3, number="2", page=2)
    pages = pages_for(first, second)
    chunks = chunk_document(
        pages,
        [first, second],
        embed_fn=FakeEmbedder(),
        semantic_min_tokens=SMALL_MIN_TOKENS,
    )
    assert [chunk.page_start for chunk in chunks] == [1, 2]
    assert [chunk.page_end for chunk in chunks] == [1, 2]


# --------------------------------------------------------------------------- #
# the length constraints still win
# --------------------------------------------------------------------------- #


def test_semantic_mode_honours_the_hard_cap() -> None:
    """A dip between every sentence cannot push a chunk past ``max_tokens``."""
    single = Section(
        title="Alternating",
        page_start=1,
        page_end=1,
        paragraphs=[
            (
                1,
                " ".join(
                    sentence(
                        TOPIC_ALPHA if index % 2 == 0 else TOPIC_BETA, index, length=60
                    )
                    for index in range(12)
                ),
            )
        ],
    )
    chunks = chunk_document(
        pages_for(single),
        [single],
        embed_fn=FakeEmbedder(),
        semantic_min_tokens=SMALL_MIN_TOKENS,
    )
    assert len(chunks) > 1
    assert all(chunk.token_count <= MAX_TOKENS for chunk in chunks)
    assert all(chunk.char_count <= MAX_TOKENS * 4 for chunk in chunks)


def test_min_chunk_length_ignores_overeager_breaks() -> None:
    """With the shipped floor a short section stays one chunk."""
    short = Section(
        title="Short",
        page_start=1,
        page_end=1,
        paragraphs=[
            (
                1,
                " ".join(
                    [
                        sentence(TOPIC_ALPHA, 0),
                        sentence(TOPIC_BETA, 1),
                        sentence(TOPIC_ALPHA, 2),
                    ]
                ),
            )
        ],
    )
    pages = pages_for(short)
    length_chunks = chunk_document(pages, [short])
    semantic_chunks = chunk_document(pages, [short], embed_fn=FakeEmbedder())
    assert len(length_chunks) == 1
    assert [chunk.text for chunk in semantic_chunks] == [
        chunk.text for chunk in length_chunks
    ]


def test_single_sentence_section_needs_no_embedding() -> None:
    single = Section(
        title="One",
        page_start=1,
        page_end=1,
        paragraphs=[(1, "Just the one sentence.")],
    )
    embedder = FakeEmbedder()
    chunks = chunk_document(pages_for(single), [single], embed_fn=embedder)
    assert len(chunks) == 1
    assert embedder.calls == [], "nothing to compare: no request is made"


# --------------------------------------------------------------------------- #
# degradation
# --------------------------------------------------------------------------- #


def test_embedding_failure_degrades_to_length_mode(caplog: pytest.LogCaptureFixture) -> None:
    alpha = section(TOPIC_ALPHA, 3, sentences_per_paragraph=1)
    pages = pages_for(alpha)
    length_chunks = chunk_document(pages, [alpha])
    expected = [chunk.text for chunk in length_chunks]

    def exploding(texts) -> list[list[float]]:
        raise RuntimeError("embedding server is down")

    with caplog.at_level(logging.WARNING):
        chunks = chunk_document(pages, [alpha], embed_fn=exploding)

    assert [chunk.text for chunk in chunks] == expected
    assert "semantic chunking fell back to length mode" in caplog.text
    record = next(
        item
        for item in caplog.records
        if item.getMessage() == "semantic chunking fell back to length mode"
    )
    fields = getattr(record, "extra_fields", {})
    assert fields["section"] == "alpha study"
    assert "embedding server is down" in fields["error"]


def test_vector_count_mismatch_degrades_to_length_mode(
    caplog: pytest.LogCaptureFixture,
) -> None:
    alpha = section(TOPIC_ALPHA, 3, sentences_per_paragraph=1)
    pages = pages_for(alpha)
    expected = [chunk.text for chunk in chunk_document(pages, [alpha])]

    with caplog.at_level(logging.WARNING):
        chunks = chunk_document(pages, [alpha], embed_fn=lambda texts: [[1.0, 0.0]])

    assert [chunk.text for chunk in chunks] == expected
    record = next(
        item
        for item in caplog.records
        if item.getMessage() == "semantic chunking fell back to length mode"
    )
    assert getattr(record, "extra_fields", {})["vectors"] == 1


# --------------------------------------------------------------------------- #
# configuration contract
# --------------------------------------------------------------------------- #


def test_config_and_chunking_agree_on_chunk_modes() -> None:
    """``config.CHUNK_MODES`` is duplicated on purpose -- keep it in sync."""
    assert CHUNKING_MODES == CHUNK_MODES
    assert CHUNK_MODE_LENGTH in CHUNK_MODES
    assert CHUNK_MODE_SEMANTIC in CHUNK_MODES


def test_config_semantic_defaults_match_the_chunking_constants() -> None:
    """The deployed defaults are the constants, or the A/B tune is meaningless."""
    assert Settings.model_fields["chunk_semantic_threshold"].default == (
        SEMANTIC_SIMILARITY_THRESHOLD
    )
    assert Settings.model_fields["chunk_semantic_min_tokens"].default == (
        SEMANTIC_MIN_TOKENS
    )


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"CHUNK_SEMANTIC_THRESHOLD": "0"}, "CHUNK_SEMANTIC_THRESHOLD"),
        ({"CHUNK_SEMANTIC_THRESHOLD": "1.2"}, "CHUNK_SEMANTIC_THRESHOLD"),
        ({"CHUNK_SEMANTIC_MIN_TOKENS": "-1"}, "zero or positive"),
    ],
)
def test_config_rejects_unusable_semantic_values(env, message) -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(**env)

    assert message in str(excinfo.value)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"semantic_threshold": 0.0}, "semantic_threshold"),
        ({"semantic_threshold": 1.5}, "semantic_threshold"),
        ({"semantic_threshold": -0.2}, "semantic_threshold"),
        ({"semantic_min_tokens": -1}, "semantic_min_tokens"),
    ],
)
def test_invalid_semantic_parameters_are_rejected(kwargs: dict, message: str) -> None:
    alpha = section(TOPIC_ALPHA, 2)
    with pytest.raises(ValueError, match=message):
        chunk_document(pages_for(alpha), [alpha], **kwargs)
