"""EPUB -> ParsedBook(title, author, chapters, cover)."""
import io
import re
import warnings
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from ebooklib import epub, ITEM_COVER, ITEM_DOCUMENT, ITEM_IMAGE

from .normalize import roman_to_int

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
warnings.filterwarnings("ignore", category=UserWarning, module="ebooklib")
warnings.filterwarnings("ignore", category=FutureWarning, module="ebooklib")

MIN_CHAPTER_CHARS = 1500
# Name-based exclusion only applies below this size: a huge "index.xhtml" is
# almost certainly the book itself, not back matter.
NAME_EXCLUDE_MAX_CHARS = 15000

FRONTMATTER_RE = re.compile(
    r"cover|title.?page|halftitle|half.?title|copyright|uncopyright|imprint|"
    r"colophon|\btoc\b|contents|\bindex\b|acknowledg|about.?the.?author|"
    r"also.?by|frontispiece|advertisement|bookseller|glossary|bibliograph|"
    r"license|project.?gutenberg|dedication|epigraph",
    re.I,
)

STRIP_SELECTORS = (
    "sup, nav, table, figure, style, script, aside, "
    ".footnote, .footnotes, .endnote, .endnotes, .pagenum, .pagenumber, .page-number"
)
STRIP_ATTRS = [
    {"epub:type": "pagebreak"},
    {"epub:type": "footnote"},
    {"epub:type": "endnote"},
    {"epub:type": "rearnote"},
    {"epub:type": "noteref"},
    {"role": "doc-pagebreak"},
    {"role": "doc-noteref"},
    {"role": "doc-footnote"},
]
BLOCK_TAGS = ["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "dt", "dd", "pre"]


@dataclass
class Chapter:
    idx: int
    title: str | None  # None only mid-parse; filled with "Section N" fallback
    text: str
    chars: int
    included: bool
    href: str


@dataclass
class ParsedBook:
    title: str
    author: str | None
    chapters: list[Chapter]
    cover_jpeg: bytes | None


def _flatten_toc(items, out: dict[str, str]) -> None:
    for it in items:
        if isinstance(it, (list, tuple)):
            _flatten_toc(it, out)
        elif isinstance(it, epub.Link):
            out.setdefault(it.href.split("#")[0], it.title)
        elif isinstance(it, epub.Section) and getattr(it, "href", None):
            out.setdefault(it.href.split("#")[0], it.title)


def _extract_text(soup: BeautifulSoup) -> str:
    body = soup.body or soup
    paras = []
    for tag in body.find_all(BLOCK_TAGS):
        if tag.find(BLOCK_TAGS):  # keep only leaf blocks (e.g. skip blockquote>p shell)
            continue
        # Raw newlines in the HTML source are just line-wrapping and must
        # collapse to spaces; only <br> is a real line break. Mark it with a
        # non-whitespace sentinel so it survives the collapse.
        for br in tag.find_all("br"):
            br.replace_with("\x00")
        lines = [" ".join(seg.split()) for seg in tag.get_text(" ").split("\x00")]
        text = "\n".join(l for l in lines if l)
        if text:
            paras.append(text)
    if paras:
        return "\n\n".join(paras)
    return " ".join(body.get_text(" ").split())


# Titles that are really file paths/URLs left over from a txt/PDF conversion.
_JUNK_TITLE_RE = re.compile(r"://|^file:|\w\|/|\.(?:txt|html?|xhtml|pdf)$", re.I)


def _clean_title(t: str | None) -> str | None:
    if not t:
        return None
    t = " ".join(t.split())
    return None if not t or _JUNK_TITLE_RE.search(t) else t


def _doc_title(soup: BeautifulSoup) -> str | None:
    h = soup.find(["h1", "h2", "h3"])
    return _clean_title(h.get_text(" ")) if h else None


# Standalone paragraphs like "42", "XIV.", "Chapter 7", "PART TWO" that mark
# chapter starts inside a single-document book (typical of txt conversions).
_MARKER_RE = re.compile(r"^(?:(?:chapter|part|book)\s+)?(\d{1,3}|[IVXLCDM]{1,7})\.?$", re.I)

SPLIT_MIN_CHARS = 60_000   # only attempt marker-splitting on huge chapters
SPLIT_MIN_GAP = 4_000      # denser markers than this are page numbers, not chapters


def split_text_on_markers(text: str) -> list[tuple[str | None, str]] | None:
    """Split one huge text on chapter-marker paragraphs. Returns
    [(title_or_None, chunk_text)], or None when no credible structure exists."""
    paras = text.split("\n\n")
    marks: list[tuple[int, str]] = []
    for i, p in enumerate(paras):
        s = p.strip()
        if not s or len(s) > 20:
            continue
        m = _MARKER_RE.match(s)
        if not m:
            continue
        num = m.group(1)
        if not num.isdigit() and roman_to_int(num.upper()) is None:
            continue
        title = f"Chapter {num}" if s.rstrip(".") == num else s.rstrip(".")
        marks.append((i, title))

    if not (3 <= len(marks) <= 400):
        return None
    gaps = [sum(len(p) for p in paras[a[0]:b[0]])
            for a, b in zip(marks, marks[1:])]
    gaps.sort()
    if gaps[len(gaps) // 2] < SPLIT_MIN_GAP:
        return None  # markers too dense: page numbers, not chapters

    pieces: list[tuple[str | None, str]] = []
    preamble = "\n\n".join(paras[: marks[0][0]]).strip()
    if preamble:
        pieces.append((None, preamble))
    bounds = [i for i, _ in marks] + [len(paras)]
    for (start, title), end in zip(marks, bounds[1:]):
        chunk = "\n\n".join(paras[start + 1 : end]).strip()
        if chunk:
            pieces.append((title, chunk))
    return pieces if len(pieces) >= 3 else None


def _make_cover_jpeg(data: bytes) -> bytes | None:
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(data))
        img = img.convert("RGB")
        img.thumbnail((512, 512))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85)
        return buf.getvalue()
    except Exception:
        return None


def _extract_cover(book: epub.EpubBook) -> bytes | None:
    candidates = []
    for item in book.get_items_of_type(ITEM_COVER):
        candidates.append(item)
    meta = book.get_metadata("OPF", "cover")
    for _, attrs in meta:
        item = book.get_item_with_id(attrs.get("content", ""))
        if item is not None:
            candidates.append(item)
    for item in book.get_items_of_type(ITEM_IMAGE):
        props = getattr(item, "properties", None) or []
        if "cover-image" in props or "cover" in item.get_name().lower():
            candidates.append(item)
    for item in candidates:
        jpeg = _make_cover_jpeg(item.get_content())
        if jpeg:
            return jpeg
    return None


def parse(epub_path: str | Path) -> ParsedBook:
    book = epub.read_epub(str(epub_path), options={"ignore_ncx": True})

    title = (book.get_metadata("DC", "title") or [("Untitled", {})])[0][0]
    authors = book.get_metadata("DC", "creator")
    author = authors[0][0] if authors else None

    toc_titles: dict[str, str] = {}
    _flatten_toc(book.toc, toc_titles)

    chapters: list[Chapter] = []
    for idref, _linear in book.spine:
        item = book.get_item_with_id(idref)
        if item is None or item.get_type() != ITEM_DOCUMENT:
            continue
        soup = BeautifulSoup(item.get_content(), "lxml")
        for tag in soup.select(STRIP_SELECTORS):
            tag.decompose()
        for attrs in STRIP_ATTRS:
            for tag in soup.find_all(attrs=attrs):
                tag.decompose()

        href = item.get_name()
        text = _extract_text(soup)
        chars = len(text)
        heading = _doc_title(soup)
        toc_title = _clean_title(toc_titles.get(href) or toc_titles.get(Path(href).name))
        ch_title = heading or toc_title

        basename = Path(href).name
        name_hit = bool(FRONTMATTER_RE.search(basename) or
                        (ch_title and FRONTMATTER_RE.search(ch_title)))

        # Calibre-style split books: an untitled document (no heading, no TOC
        # entry) following an included chapter is a continuation of it, not a
        # new chapter — merge, whatever its size, so small tail fragments
        # aren't dropped by the length heuristic.
        if (ch_title is None and text and not name_hit
                and chapters and chapters[-1].included):
            prev = chapters[-1]
            prev.text = prev.text + "\n\n" + text
            prev.chars = len(prev.text)
            continue

        included = chars >= MIN_CHAPTER_CHARS and not (
            name_hit and chars < NAME_EXCLUDE_MAX_CHARS
        )
        chapters.append(Chapter(len(chapters), ch_title, text, chars, included, href))

    # Single-document books (txt/PDF conversions) put everything in one spine
    # item; split huge chapters on internal "42" / "XIV" / "Chapter 7" marker
    # paragraphs when a credible chapter structure exists.
    expanded: list[Chapter] = []
    for ch in chapters:
        pieces = (split_text_on_markers(ch.text)
                  if ch.included and ch.chars > SPLIT_MIN_CHARS else None)
        if not pieces:
            expanded.append(ch)
            continue
        for j, (p_title, p_text) in enumerate(pieces):
            if j == 0 and p_title is None:
                p_title = ch.title  # preamble keeps the original title, if any
            expanded.append(Chapter(0, p_title, p_text, len(p_text), True, ch.href))
    chapters = expanded
    for i, ch in enumerate(chapters):
        ch.idx = i

    # Number untitled content chapters separately from excluded scraps so the
    # player list starts at "Section 1".
    seq_inc = seq_exc = 0
    for ch in chapters:
        if ch.title is None:
            if ch.included:
                seq_inc += 1
                ch.title = f"Section {seq_inc}"
            else:
                seq_exc += 1
                ch.title = f"Fragment {seq_exc}"

    try:
        cover = _extract_cover(book)
    except Exception:
        cover = None  # a cover is never worth failing the whole book over
    return ParsedBook(title=title, author=author, chapters=chapters,
                      cover_jpeg=cover)
