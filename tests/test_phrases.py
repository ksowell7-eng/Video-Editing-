"""Phrase selection runs on the flat string the browser hands back."""


from pipeline.scrape.phrases import content_words, keywords, select_spans

TEXT = (
    "The keeper stayed rooted to his line. Ferrand curled the free kick around a "
    "four-man wall in the 88th minute, and Marseille took the cup. Nobody expected it."
)
PARAGRAPHS = [{"start": 0, "end": len(TEXT)}]


def test_spans_address_the_original_string_exactly():
    # This is the contract with the browser: offsets picked here are handed back
    # to Range.setStart/setEnd against the identical walk.
    for span in select_spans(TEXT, PARAGRAPHS):
        assert TEXT[span.start:span.end].strip() == span.text


def test_spans_come_back_in_document_order():
    spans = select_spans(TEXT, PARAGRAPHS)
    assert [s.start for s in spans] == sorted(s.start for s in spans)


def test_the_phrase_cap_is_respected():
    assert len(select_spans(TEXT, PARAGRAPHS, max_phrases=2)) == 2


def test_long_sentences_contribute_a_window_not_a_truncation():
    long_sentence = " ".join(f"word{i}" for i in range(40)) + "."
    spans = select_spans(long_sentence, [{"start": 0, "end": len(long_sentence)}], max_words=6)
    assert spans
    assert all(len(s.text.split()) <= 6 for s in spans)


def test_short_fragments_are_dropped():
    text = "Hi. Ok."
    assert select_spans(text, [{"start": 0, "end": len(text)}]) == []


def test_empty_input_is_handled():
    assert select_spans("", []) == []


def test_multiple_paragraphs_are_attributed_correctly():
    text = "First one here now. Second one there then."
    paragraphs = [{"start": 0, "end": 19}, {"start": 20, "end": len(text)}]
    spans = select_spans(text, paragraphs, min_words=2)
    assert {s.paragraph for s in spans} == {0, 1}


class TestKeywords:
    def test_title_words_are_promoted(self):
        ranked = keywords(TEXT, "Marseille lift the cup")
        assert ranked[0] == "marseille"

    def test_stopwords_never_appear(self):
        assert not ({"the", "and", "of"} & set(keywords(TEXT, "")))

    def test_content_words_skip_short_tokens(self):
        assert "it" not in content_words("it is a keeper")
