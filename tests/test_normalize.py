import pytest

from app.normalize import MAX_SEG_CHARS, normalize, roman_to_int, segment


# --- character cleanup ---

def test_soft_hyphen_and_zero_width_removed():
    assert normalize("cre­ate zero​width﻿") == "create zerowidth"

def test_smart_quotes():
    assert normalize("“Hello,” she said. ‘Don’t.’") == \
        '"Hello," she said. \'Don\'t.\''

def test_ellipsis():
    assert normalize("Well… maybe") == "Well... maybe"

def test_em_dash_becomes_comma_pause():
    assert normalize("he was—or seemed—happy") == "he was, or seemed, happy"

def test_double_hyphen_dash():
    assert normalize("wait -- what") == "wait, what"

def test_numeric_range_dash():
    assert normalize("the war of 1914–1918") == "the war of 1914 to 1918"
    assert normalize("pages 3-5") == "pages 3 to 5"

def test_ampersand():
    assert normalize("Smith & Sons") == "Smith and Sons"


# --- deletions ---

def test_citation_markers_removed():
    assert normalize("proven[12] beyond doubt[3].") == "proven beyond doubt."

def test_page_number_lines_dropped():
    assert normalize("end of page\n\n42\n\nstart of next") == \
        "end of page\n\nstart of next"

def test_year_like_paragraph_still_dropped_but_long_numbers_kept():
    assert normalize("a\n\n12345\n\nb") == "a\n\n12345\n\nb"  # >4 digits: not a page number

def test_decoration_lines_dropped():
    assert normalize("scene one\n\n* * *\n\nscene two") == "scene one\n\nscene two"


# --- abbreviations ---

@pytest.mark.parametrize("src,expected", [
    ("Mr. Darcy", "Mister Darcy"),
    ("Mrs. Bennet", "Missus Bennet"),
    ("Ms. Smith", "Miz Smith"),
    ("Dr. Watson", "Doctor Watson"),
    ("Prof. Moriarty", "Professor Moriarty"),
    ("Capt. Ahab", "Captain Ahab"),
    ("St. Petersburg", "Saint Petersburg"),
    ("Baker St. was empty", "Baker Street was empty"),
    ("Mt. Everest", "Mount Everest"),
    ("cats vs. dogs", "cats versus dogs"),
    ("Ch. 3 begins", "Chapter 3 begins"),
    ("Vol. 2", "Volume 2"),
    ("No. 5", "Number 5"),
    ("see pp. 10 to 12", "see pages 10 to 12"),
])
def test_abbreviations(src, expected):
    assert normalize(src) == expected

def test_etc_mid_sentence():
    assert normalize("apples, pears, etc., and more") == \
        "apples, pears, et cetera, and more"

def test_etc_at_sentence_end():
    assert normalize("apples, etc. Then we left.") == \
        "apples, et cetera. Then we left."

def test_eg_ie():
    assert normalize("fruit, e.g. apples") == "fruit, for example, apples"
    assert normalize("the best, i.e. mine") == "the best, that is, mine"


# --- roman numerals ---

def test_roman_to_int_validity():
    assert roman_to_int("XIV") == 14
    assert roman_to_int("MCMXCIV") == 1994
    assert roman_to_int("IC") is None  # invalid form
    assert roman_to_int("VV") is None

def test_roman_line_converted():
    assert normalize("XIV\n\nThe day began.") == "Fourteen.\n\nThe day began."

def test_chapter_roman_heading():
    assert normalize("CHAPTER XIV\n\ntext") == "CHAPTER Fourteen.\n\ntext"

def test_roman_inside_prose_untouched():
    assert normalize("Louis XIV ruled France.") == "Louis XIV ruled France."


# --- whitespace / paragraphs ---

def test_whitespace_collapsed_paragraphs_preserved():
    assert normalize("a   b\t c\n\n\n\nd  e") == "a b c\n\nd e"

def test_single_newlines_become_paragraphs():
    # poetry: each line gets its own pause
    assert normalize("line one\nline two") == "line one\n\nline two"


# --- segmentation ---

def test_short_paragraph_single_segment():
    segs = segment("Hello world.\n\nSecond paragraph.")
    assert segs == [("Hello world.", True), ("Second paragraph.", True)]

def test_long_paragraph_split_on_sentences():
    para = " ".join(f"This is sentence number {i}." for i in range(40))
    segs = segment(para)
    assert len(segs) > 1
    assert all(len(t) <= MAX_SEG_CHARS for t, _ in segs)
    assert [e for _, e in segs] == [False] * (len(segs) - 1) + [True]
    assert " ".join(t for t, _ in segs) == para  # no text lost

def test_monster_sentence_hard_split():
    para = "word " * 300  # one 1500-char "sentence", no punctuation
    segs = segment(para.strip())
    assert all(len(t) <= MAX_SEG_CHARS for t, _ in segs)
    assert " ".join(t for t, _ in segs) == para.strip()

def test_clause_split_before_hard_split():
    para = ", ".join("a clause with several words here" for _ in range(30))
    segs = segment(para)
    assert all(len(t) <= MAX_SEG_CHARS for t, _ in segs)

def test_no_empty_segments():
    assert segment("") == []
    assert all(t.strip() for t, _ in segment("Hi.\n\n\n\nBye."))


# --- part splitting (synth) ---

def test_split_parts():
    from app.synth import split_parts
    segs = [(f"x" * 100, True) for _ in range(200)]  # 20k chars
    parts = split_parts(segs, target=8000)
    assert len(parts) == 3
    assert sum(len(p) for p in parts) == 200          # no segment lost
    assert [s for p in parts for s in p] == segs      # order preserved
    assert split_parts([("short", True)], target=8000) == [[("short", True)]]
