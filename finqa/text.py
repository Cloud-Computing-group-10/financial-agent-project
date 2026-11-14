"""Filing HTML -> sectioned text chunks (plan Layer 2)."""
from __future__ import annotations

import re
import warnings

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

ITEM_RE = re.compile(r"^item\s*(\d+[a-z]?)\s*[\.:\-–—]?\s*(.*)$", re.I)
PART_RE = re.compile(r"^part\s+(i{1,2})\b", re.I)
NUMERIC_LINE = re.compile(r"^[\s\$\(\)\-–—%,.\d|*]+$")

PART1_TITLES = {"1": "Financial Statements", "2": "Management's Discussion and Analysis",
                "3": "Quantitative and Qualitative Disclosures About Market Risk", "4": "Controls and Procedures"}

TARGET_WORDS = 180
MAX_WORDS = 260


def html_to_lines(html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "head"]):
        t.decompose()
    for t in soup.find_all(style=re.compile(r"display:\s*none", re.I)):
        t.decompose()
    # ix:header holds hidden XBRL metadata
    for t in soup.find_all(re.compile(r"^ix:header$", re.I)):
        t.decompose()
    # Flatten tables into one line per row: "cell | cell | cell"
    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            cells = [c for c in cells if c and c not in {"$", "%", ")"}]
            if cells:
                rows.append(" | ".join(cells))
        table.replace_with(soup.new_string("\n" + "\n".join(rows) + "\n"))
    # Block-level elements -> newlines
    text = soup.get_text("\n")
    lines = []
    for raw in text.split("\n"):
        line = re.sub(r"\s+", " ", raw.replace("\xa0", " ")).strip()
        # page furniture: running "Table of Contents" links and bare page numbers
        if re.fullmatch(r"(?i)table of contents|\d{1,3}|page \d{1,3}", line):
            continue
        if line:
            lines.append(line)
    return _merge_fragments(lines)


def _merge_fragments(lines: list[str]) -> list[str]:
    """Inline tags (spans) split sentences across lines; glue lowercase continuations back."""
    out: list[str] = []
    for line in lines:
        if out and out[-1] and not out[-1].endswith((".", ":", ";", "?", "!")) and line[:1].islower() \
                and " | " not in line and " | " not in out[-1]:
            out[-1] = out[-1] + " " + line
        else:
            out.append(line)
    return out


def _is_heading(line: str) -> bool:
    if len(line) > 90 or len(line) < 4 or " | " in line:
        return False
    if line.endswith((".", ",", ";", ":")) and not ITEM_RE.match(line):
        return False
    if NUMERIC_LINE.match(line):
        return False
    words = line.split()
    caps = sum(1 for w in words if w[:1].isupper() or not w[:1].isalpha())
    return len(words) <= 12 and caps / len(words) >= 0.6


def sectionize(lines: list[str]) -> list[dict]:
    """Assign each line a section ('Part I Item 2. MD&A') and nearest heading."""
    part = "I"
    section = "Cover"
    heading = ""
    major = ""
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        is_toc_row = " | " in line  # table-of-contents rows look like "ITEM 2 | MD&A | 25"
        pm = PART_RE.match(line)
        if pm and len(line) < 80 and not is_toc_row:
            part = pm.group(1).upper()
            # Part I always opens with Item 1 (some filings never print that heading on its own line)
            section = "Part I Item 1. Financial Statements" if part == "I" else "Part II"
            heading = major = ""
            i += 1
            continue
        im = ITEM_RE.match(line)
        if im and len(line) < 120 and not is_toc_row:
            num, title = im.group(1).upper(), im.group(2).strip(" .")
            if not title and i + 1 < len(lines) and len(lines[i + 1]) < 120:
                title = lines[i + 1].strip(" .")
                i += 1
            if part == "I" and num in PART1_TITLES and not title:
                title = PART1_TITLES[num]
            section = f"Part {part} Item {num}. {title}".strip()
            heading = major = ""
            i += 1
            continue
        if _is_heading(line):
            # Keep a two-level heading path: the last ALL-CAPS heading (e.g. a segment name such as
            # "AEROSPACE") plus the current sub-heading ("Q2 2023 compared to Q2 2022"); without the
            # parent, passages like "Sales increased $443 million ..." lose which segment they describe.
            letters = [ch for ch in line if ch.isalpha()]
            if letters and all(ch.isupper() for ch in letters):
                major = line
                heading = line
            else:
                heading = f"{major} › {line}" if major else line
            i += 1
            continue
        out.append({"section": section, "heading": heading, "text": line})
        i += 1
    return out


def chunk(sectioned: list[dict]) -> list[dict]:
    """Group consecutive lines with the same section+heading into ~180-word chunks."""
    chunks: list[dict] = []
    buf: list[str] = []
    key = None

    def flush():
        if buf:
            text = "\n".join(buf).strip()
            if len(text.split()) >= 25 and not _mostly_numeric(text):
                chunks.append({"section": key[0], "heading": key[1], "text": text})
        buf.clear()

    for item in sectioned:
        k = (item["section"], item["heading"])
        if k != key:
            flush()
            key = k
        buf.append(item["text"])
        words = sum(len(b.split()) for b in buf)
        if words >= TARGET_WORDS and item["text"].endswith((".", ":")) or words >= MAX_WORDS:
            flush()
    flush()
    for n, c in enumerate(chunks):
        c["ordinal"] = n
    return chunks


def _mostly_numeric(text: str) -> bool:
    tokens = text.split()
    nums = sum(1 for t in tokens if re.fullmatch(r"[\$\(\)\-–—%,.\d|]+", t))
    return nums / max(len(tokens), 1) > 0.6


def filing_chunks(html: str) -> list[dict]:
    return chunk(sectionize(html_to_lines(html)))


def filing_plaintext(html: str) -> str:
    """Whole document as plain text (used by the full-document baseline)."""
    return "\n".join(html_to_lines(html))
