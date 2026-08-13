"""Text cleanup before TTS, plus segmentation into model-sized chunks.

Output quality lives or dies here. Every rule is unit-tested in
tests/test_normalize.py.
"""
import re
import unicodedata

from num2words import num2words

# Kokoro's context window is ~510 phoneme tokens; segments beyond it get
# truncated or garbled. ~400 chars of English stays comfortably under.
MAX_SEG_CHARS = 400

_ZERO_WIDTH_RE = re.compile("[­​‌‍⁠﻿]")

_CHAR_MAP = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"',
    "′": "'", "″": '"',
    " ": " ",
})

# (pattern, replacement), applied in order. Case-sensitive on purpose:
# "DR." in an all-caps heading is not worth chasing.
_ABBREVIATIONS = [
    (re.compile(r"\bMr\."), "Mister"),
    (re.compile(r"\bMrs\."), "Missus"),
    (re.compile(r"\bMs\."), "Miz"),
    (re.compile(r"\bMessrs\."), "Messieurs"),
    (re.compile(r"\bDr\."), "Doctor"),
    (re.compile(r"\bProf\."), "Professor"),
    (re.compile(r"\bRev\."), "Reverend"),
    (re.compile(r"\bHon\."), "Honorable"),
    (re.compile(r"\bCapt\."), "Captain"),
    (re.compile(r"\bCol\."), "Colonel"),
    (re.compile(r"\bGen\."), "General"),
    (re.compile(r"\bLt\."), "Lieutenant"),
    (re.compile(r"\bSgt\."), "Sergeant"),
    (re.compile(r"\bMaj\."), "Major"),
    (re.compile(r"\bMt\."), "Mount"),
    # St. is Saint before a capitalized word, Street otherwise.
    (re.compile(r"\bSt\.(?=\s+[A-Z])"), "Saint"),
    (re.compile(r"\bSt\."), "Street"),
    # etc. often ends a sentence; keep the period when the next word is capitalized.
    (re.compile(r"\betc\.(?=\s+[A-Z])"), "et cetera."),
    (re.compile(r"\betc\.?"), "et cetera"),
    (re.compile(r"\b[eE]\.g\.,?\s*"), "for example, "),
    (re.compile(r"\b[iI]\.e\.,?\s*"), "that is, "),
    (re.compile(r"\bcf\.\s*"), "compare "),
    (re.compile(r"\bvs\.?(?=\s)"), "versus"),
    (re.compile(r"\bapprox\."), "approximately"),
    (re.compile(r"\bCh\.\s*(?=\d)"), "Chapter "),
    (re.compile(r"\b[Vv]ol\.\s*(?=[\dIVXLC])"), "Volume "),
    (re.compile(r"\b[Nn]o\.\s*(?=\d)"), "Number "),
    (re.compile(r"\bpp\.\s*(?=\d)"), "pages "),
    (re.compile(r"\bp\.\s*(?=\d)"), "page "),
]

_ROMAN_VALS = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
_ROMAN_LINE_RE = re.compile(r"^([IVXLCDM]+)\.?$")
_HEADING_ROMAN_RE = re.compile(
    r"^((?:chapter|part|book|section|canto|act|scene)\s+)([IVXLCDM]+)(\.?)$", re.I
)


def _int_to_roman(n: int) -> str:
    table = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"),
             (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"),
             (5, "V"), (4, "IV"), (1, "I")]
    out = []
    for val, sym in table:
        while n >= val:
            out.append(sym)
            n -= val
    return "".join(out)


def roman_to_int(s: str) -> int | None:
    """Strictly-valid roman numeral -> int, else None."""
    if not s or any(c not in _ROMAN_VALS for c in s):
        return None
    total, prev = 0, 0
    for c in reversed(s):
        v = _ROMAN_VALS[c]
        total += v if v >= prev else -v
        prev = max(prev, v)
    return total if 0 < total < 4000 and _int_to_roman(total) == s else None


def _number_words(n: int) -> str:
    return num2words(n).replace("-", " ").replace(",", "")


def _romanize_line(line: str) -> str:
    """'XIV' -> 'Fourteen.'; 'Chapter XIV' -> 'Chapter Fourteen.'"""
    m = _ROMAN_LINE_RE.match(line)
    if m:
        n = roman_to_int(m.group(1))
        if n is not None:
            return _number_words(n).capitalize() + "."
    m = _HEADING_ROMAN_RE.match(line)
    if m:
        n = roman_to_int(m.group(2).upper())
        if n is not None:
            return m.group(1) + _number_words(n).capitalize() + "."
    return line


def _replace_dashes(text: str) -> str:
    # Digit-dash-digit is a range: "1914-1918" -> "1914 to 1918".
    text = re.sub(r"(?<=\d)\s*[-–−]\s*(?=\d)", " to ", text)
    # Remaining em/en dashes (and " -- ") become a comma pause.
    text = re.sub(r"\s*(?:—|–|―|--+)\s*", ", ", text)
    return text


def _clean_line(line: str) -> str | None:
    """Clean one line; None means drop it entirely."""
    s = " ".join(line.split())
    if not s:
        return None
    if s.rstrip(".").isdigit() and len(s.rstrip(".")) <= 4:
        return None  # orphaned page number
    if not re.search(r"[A-Za-z0-9]", s):
        return None  # decoration like "* * *"
    return _romanize_line(s)


def normalize(text: str) -> str:
    """Full cleanup. Returns paragraphs separated by \\n\\n."""
    text = unicodedata.normalize("NFC", text)
    text = _ZERO_WIDTH_RE.sub("", text)
    text = text.translate(_CHAR_MAP)
    text = text.replace("…", "...")
    text = re.sub(r"\[\d+\]", "", text)  # inline citation markers
    text = _replace_dashes(text)
    for pat, repl in _ABBREVIATIONS:
        text = pat.sub(repl, text)
    text = re.sub(r"\s*&\s*", " and ", text)

    paragraphs = []
    for para in re.split(r"\n\s*\n", text):
        # Every surviving line becomes its own paragraph: prose blocks have no
        # internal newlines, and poetry reads best with a pause per line.
        for line in para.split("\n"):
            cleaned = _clean_line(line)
            if cleaned:
                paragraphs.append(cleaned)

    out = "\n\n".join(paragraphs)
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)  # no space before punctuation
    out = re.sub(r",([,.;:])", r"\1", out)      # ",," or ",." from dash rules
    return out


# --- segmentation ---------------------------------------------------------

_SENT_END_RE = re.compile(r"[.!?]+[\"')\]]*\s+")


def _sentences(para: str) -> list[str]:
    out, start = [], 0
    for m in _SENT_END_RE.finditer(para):
        out.append(para[start:m.end()].strip())
        start = m.end()
    if start < len(para):
        out.append(para[start:].strip())
    return [s for s in out if s]


def _hard_split(text: str, max_chars: int) -> list[str]:
    """Last resort: break at word boundaries near max_chars."""
    words, chunks, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars:
            chunks.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        chunks.append(cur)
    return chunks


def _pack(pieces: list[str], max_chars: int) -> list[str]:
    chunks, cur = [], ""
    for p in pieces:
        if len(p) > max_chars:
            if cur:
                chunks.append(cur)
                cur = ""
            # Oversized sentence: try clause boundaries, then hard split.
            clauses = re.split(r"(?<=[;,:])\s+", p)
            if len(clauses) > 1:
                chunks.extend(_pack(clauses, max_chars))
            else:
                chunks.extend(_hard_split(p, max_chars))
        elif cur and len(cur) + 1 + len(p) > max_chars:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur} {p}".strip()
    if cur:
        chunks.append(cur)
    return chunks


def segment(text: str, max_chars: int = MAX_SEG_CHARS) -> list[tuple[str, bool]]:
    """Normalized text -> [(chunk, is_paragraph_end)], every chunk <= max_chars."""
    segs = []
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        chunks = _pack(_sentences(para), max_chars) if len(para) > max_chars else [para]
        for i, chunk in enumerate(chunks):
            segs.append((chunk, i == len(chunks) - 1))
    return segs
