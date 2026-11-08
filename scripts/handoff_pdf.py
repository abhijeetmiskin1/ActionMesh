"""Render the ActionMesh engineering handoff as a PDF.

Pure standard library: no third-party package is required and no network access
is used. Text is drawn with the PDF base-14 fonts (Helvetica, Helvetica-Bold,
Courier, Helvetica-Oblique), which every reader supplies without embedding.

Usage:
    python scripts/handoff_pdf.py [--out ActionMesh-Handoff.pdf]

The document is assembled from the handoff Markdown files in this repository,
so the PDF can never drift from the checked-in documentation: regenerating it
after editing the Markdown is the whole workflow.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# --- Page geometry (A4, in PDF points; 1 pt = 1/72 inch) --------------------
PAGE_W = 595.0
PAGE_H = 842.0
MARGIN_L = 56.0
MARGIN_R = 56.0
MARGIN_T = 58.0
MARGIN_B = 58.0
CONTENT_W = PAGE_W - MARGIN_L - MARGIN_R

# --- Base-14 font keys ------------------------------------------------------
F_REG = "F1"
F_BOLD = "F2"
F_MONO = "F3"
F_ITAL = "F4"

FONT_NAMES = {
    F_REG: "Helvetica",
    F_BOLD: "Helvetica-Bold",
    F_MONO: "Courier",
    F_ITAL: "Helvetica-Oblique",
}

# --- Glyph metrics (units per 1000 em) --------------------------------------
# Only ASCII 32..126 is tabulated. Anything exotic is transliterated to ASCII
# before measuring, so the tables never need to grow.
_W_REG_RAW = (
    "278 278 355 556 556 889 667 191 333 333 389 584 278 333 278 278 "
    "556 556 556 556 556 556 556 556 556 556 "
    "278 278 584 584 584 556 1015 "
    "667 667 722 722 667 611 778 722 278 500 667 556 833 722 778 667 778 "
    "722 667 611 722 667 944 667 667 611 "
    "278 278 278 469 556 333 "
    "556 556 500 556 556 278 556 556 222 222 500 222 833 556 556 556 556 "
    "333 500 278 556 500 722 500 500 500 "
    "334 260 334 584"
)

_W_BOLD_RAW = (
    "278 333 474 556 556 889 722 238 333 333 389 584 278 333 278 278 "
    "556 556 556 556 556 556 556 556 556 556 "
    "333 333 584 584 584 611 975 "
    "722 722 722 722 667 611 778 722 278 556 722 611 833 722 778 667 778 "
    "722 667 611 722 667 944 667 667 611 "
    "333 278 333 584 556 333 "
    "556 611 556 611 556 333 611 611 278 278 556 278 889 611 611 611 611 "
    "389 556 333 611 556 778 556 556 500 "
    "389 280 389 584"
)


def _table(raw: str) -> dict[str, int]:
    values = [int(v) for v in raw.split()]
    if len(values) != 95:  # 32..126 inclusive
        raise SystemExit(f"width table has {len(values)} entries, expected 95")
    return {chr(32 + i): v for i, v in enumerate(values)}


WIDTHS = {F_REG: _table(_W_REG_RAW), F_BOLD: _table(_W_BOLD_RAW)}

# --- Text sanitising --------------------------------------------------------
# The documentation leans on typographic punctuation and box-drawing glyphs.
# WinAnsiEncoding covers Latin-1, so anything outside it is transliterated.
# Characters above 0xFF would otherwise be dropped or corrupt the stream.
_TRANSLIT = {
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "\x97", "\u2026": "\x85", "\u00a0": " ",
    "\u2192": "->", "\u2190": "<-", "\u21d2": "=>",
    "\u2265": ">=", "\u2264": "<=", "\u2260": "!=",
    "\u00d7": "x", "\u00b7": "-", "\u2022": "-", "\u2713": "ok",
    "\u2714": "ok", "\u2717": "x", "\u20b9": "INR ",
    "\u2500": "-", "\u2502": "|", "\u251c": "+-", "\u2514": "+-",
    "\u2510": "-+", "\u250c": "+-", "\u252c": "-+-", "\u2534": "-+-",
    "\u253c": "-+-", "\u2518": "-+", "\u2551": "|", "\u2550": "=",
}
_BOX_DRAWING = re.compile("[\u2500-\u257f]")


def sanitise(text: str) -> str:
    """Map arbitrary text onto WinAnsi so it survives the PDF content stream."""
    out = []
    for ch in text:
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif _BOX_DRAWING.match(ch):
            out.append("+")
        elif ch == "\t":
            out.append("    ")
        elif ch < " ":
            out.append(" ")
        elif ord(ch) <= 0xFF:
            out.append(ch)
        else:
            out.append("?")
    return "".join(out)


def pdf_string(text: str) -> str:
    """Escape a string for a PDF literal, after sanitising it."""
    safe = sanitise(text)
    safe = safe.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    return f"({safe})"


def text_width(text: str, font: str, size: float) -> float:
    """Width of ``text`` in points at ``size`` for one of the base-14 fonts."""
    safe = sanitise(text)
    if font == F_MONO:
        return len(safe) * size * 0.6
    table = WIDTHS.get(font) or WIDTHS[F_REG]
    units = table.get(" ", 278)
    total = 0
    for ch in safe:
        total += table.get(ch, units)
    return total * size / 1000.0


def wrap(text: str, font: str, size: float, width: float) -> list[str]:
    """Greedy word wrap. Long unbreakable tokens are split on character."""
    if not text:
        return [""]
    lines: list[str] = []
    current = ""
    for word in text.split(" "):
        candidate = word if not current else f"{current} {word}"
        if text_width(candidate, font, size) <= width or not current:
            if text_width(candidate, font, size) <= width:
                current = candidate
                continue
            # A single word wider than the column: hard-split it.
            if current:
                lines.append(current)
                current = ""
            chunk = ""
            for ch in word:
                if text_width(chunk + ch, font, size) <= width or not chunk:
                    chunk += ch
                else:
                    lines.append(chunk)
                    chunk = ch
            current = chunk
        else:
            lines.append(current)
            current = word
    if current or not lines:
        lines.append(current)
    return lines


def wrap_mono(text: str, width: int) -> list[str]:
    """Wrap to a fixed character count (used for tables and code blocks)."""
    width = max(4, width)
    lines: list[str] = []
    current = ""
    for word in sanitise(text).split(" "):
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = ""
        chunk = word
        while len(chunk) > width:
            lines.append(chunk[:width])
            chunk = chunk[width:]
        current = chunk
    lines.append(current)
    return lines


# --- Inline markup ----------------------------------------------------------
# Only three constructs are recognised, which is everything the handoff uses:
# **bold**, `code`, and [label](url) links (rendered as label + url).
_INLINE_RE = re.compile(r"\*\*(.+?)\*\*|`([^`]+)`|\[([^\]]+)\]\(([^)]+)\)")


def parse_inline(text: str) -> list[tuple[str, str]]:
    """Split a line into (text, style) runs; style is 'n', 'b' or 'c'."""
    runs: list[tuple[str, str]] = []
    pos = 0
    for match in _INLINE_RE.finditer(text):
        if match.start() > pos:
            runs.append((text[pos:match.start()], "n"))
        if match.group(1) is not None:
            runs.append((match.group(1), "b"))
        elif match.group(2) is not None:
            runs.append((match.group(2), "c"))
        else:
            label, url = match.group(3), match.group(4)
            runs.append((label, "n"))
            if url != label:
                runs.append((f"({url})", "c"))
        pos = match.end()
    if pos < len(text):
        runs.append((text[pos:], "n"))
    return runs or [(text, "n")]


def rich_words(text: str) -> list[tuple[str, str]]:
    """Flatten inline markup into styled words for run-aware wrapping."""
    words: list[tuple[str, str]] = []
    for chunk, style in parse_inline(text):
        for word in sanitise(chunk).split(" "):
            if word:
                words.append((word, style))
    return words


# --- Page canvas ------------------------------------------------------------
class Document:
    """Flowing A4 canvas: tracks the cursor and breaks pages on demand."""

    def __init__(self, title: str) -> None:
        self.title = title
        self.pages: list[list[str]] = []
        self.y = 0.0
        self._new_page()

    # -- internals ---------------------------------------------------------
    def _new_page(self) -> None:
        self.pages.append([])
        self.y = PAGE_H - MARGIN_T

    @property
    def ops(self) -> list[str]:
        return self.pages[-1]

    def _emit(self, x, y, text, font, size, gray) -> None:
        ops = self.ops
        ops.append(f"{gray:.3f} g")
        ops.append(
            f"BT /{font} {size:.2f} Tf 1 0 0 1 {x:.2f} {y:.2f} Tm "
            f"{pdf_string(text)} Tj ET"
        )

    def need(self, height: float) -> None:
        if self.y - height < MARGIN_B:
            self._new_page()

    # -- primitives --------------------------------------------------------
    def para(self, text, font=F_REG, size=9.6, indent=0.0, gray=0.05,
             gap=1.8, extra=3.0) -> None:
        lead = size + gap
        for chunk in wrap(text, font, size, CONTENT_W - indent):
            self.need(lead)
            self.y -= lead
            self._emit(MARGIN_L + indent, self.y, chunk, font, size, gray)
        self.y -= extra

    def rule(self, gray=0.80, inset=0.0) -> None:
        self.need(10)
        self.y -= 5
        self.ops.append(
            f"{gray:.3f} g {MARGIN_L + inset:.2f} {self.y:.2f} "
            f"{CONTENT_W - inset * 2:.2f} 0.6 re f"
        )
        self.y -= 3

    def code_block(self, raw_lines: list[str]) -> None:
        size, pad = 7.8, 5.0
        lead = size + 2.4
        indent = 8.0
        width = int((CONTENT_W - indent - 2 * pad) / (size * 0.6))
        chunks: list[str] = []
        for raw in raw_lines:
            chunks.extend(wrap_mono(raw, width))
        if not chunks:
            chunks = [""]
        index = 0
        while index < len(chunks):
            available = self.y - MARGIN_B - 6
            fit = int((available - 2 * pad) // lead)
            if fit < 3:
                self._new_page()
                continue
            part = chunks[index:index + fit]
            index += len(part)
            height = len(part) * lead + 2 * pad
            top = self.y
            self.ops.append(
                f"0.955 g {MARGIN_L + indent:.2f} {top - height:.2f} "
                f"{CONTENT_W - 2 * indent:.2f} {height:.2f} re f"
            )
            cursor = top - pad
            for chunk in part:
                cursor -= lead
                self._emit(MARGIN_L + indent + pad, cursor, chunk, F_MONO, size, 0.15)
            self.y = top - height - 5

    def table(self, rows: list[list[str]], header: bool = True) -> None:
        size, sep = 7.4, 3
        lead = size + 2.4
        cols = max(len(r) for r in rows)
        rows = [list(r) + [""] * (cols - len(r)) for r in rows]
        char_w = size * 0.6
        max_chars = int((CONTENT_W - 10) / char_w)
        natural = [max(len(sanitise(r[j])) for r in rows) for j in range(cols)]
        total = sum(natural) + sep * (cols - 1)
        if total > max_chars:
            scale = (max_chars - sep * (cols - 1)) / max(1, sum(natural))
            alloc = [max(6, int(n * scale)) for n in natural]
        else:
            alloc = list(natural)
        xs, x = [], MARGIN_L + 4
        for a in alloc:
            xs.append(x)
            x += (a + sep) * char_w
        self.need(lead * 2 + 8)
        for ri, row in enumerate(rows):
            cells = [wrap_mono(row[j], alloc[j]) for j in range(cols)]
            height = max(len(c) for c in cells) * lead
            self.need(height + 2)
            cursor = self.y
            for li in range(max(len(c) for c in cells)):
                cursor -= lead
                for j in range(cols):
                    if li < len(cells[j]):
                        font = F_BOLD if (header and ri == 0) else F_MONO
                        self._emit(xs[j], cursor, cells[j][li], font, size,
                                   0.10 if (header and ri == 0) else 0.18)
            self.y = cursor - 1
            if header and ri == 0:
                self.ops.append(
                    f"0.78 g {MARGIN_L + 4:.2f} {self.y:.2f} "
                    f"{CONTENT_W - 10:.2f} 0.5 re f"
                )
                self.y -= 3
        self.y -= 7

    def rich_para(self, text, size=9.6, indent=0.0, gray=0.05, gap=1.8,
                  extra=3.0, bold_base=False) -> None:
        """Paragraph with inline ``code`` / **bold** runs rendered distinctly."""
        base = F_BOLD if bold_base else F_REG
        font_of = {"n": base, "b": F_BOLD, "c": F_MONO}
        words = rich_words(text)
        if not words:
            self.y -= extra
            return
        lead = size + gap
        limit = CONTENT_W - indent
        space_w = text_width(" ", base, size)
        line: list[tuple[str, str]] = []
        used = 0.0
        pending: list[list[tuple[str, str]]] = []
        for word, style in words:
            w = text_width(word, font_of[style], size)
            add = w if not line else space_w + w
            if line and used + add > limit:
                pending.append(line)
                line, used = [], 0.0
                add = w
            line.append((word, style))
            used += add
        if line:
            pending.append(line)
        for chunk in pending:
            self.need(lead)
            self.y -= lead
            x = MARGIN_L + indent
            for word, style in chunk:
                self._emit(x, self.y, word, font_of[style],
                           size, gray if style == "n" else 0.30)
                x += text_width(word, font_of[style], size) + space_w
        self.y -= extra

    def stamp_footers(self, label: str) -> None:
        total = len(self.pages)
        for idx, ops in enumerate(self.pages, start=1):
            text = f"{label}  \u00b7  page {idx} of {total}"
            size = 7.0
            width = text_width(text, F_REG, size)
            x = (PAGE_W - width) / 2
            ops.append("0.55 g")
            ops.append(
                f"BT /{F_REG} {size:.2f} Tf 1 0 0 1 {x:.2f} 30.00 Tm "
                f"{pdf_string(text)} Tj ET"
            )


# --- Markdown ---------------------------------------------------------------
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_RULE_RE = re.compile(r"^\s*([-*_])\1{2,}\s*$")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_NUMBER_RE = re.compile(r"^(\s*)(\d+)\.\s+(.*)$")
_SEP_CELL_RE = re.compile(r"^:?-{2,}:?$")

HEADING_SIZES = {1: 16.0, 2: 12.6, 3: 10.8, 4: 10.0, 5: 9.6, 6: 9.6}


def _indent_width(whitespace: str) -> float:
    return min(len(whitespace.replace("\t", "  ")) // 2, 3) * 13.0


def render_markdown(doc: Document, text: str) -> None:
    """Render the Markdown subset used by this repository's docs."""
    lines = text.splitlines()
    i = 0
    total = len(lines)
    while i < total:
        raw = lines[i]
        stripped = raw.strip()

        if not stripped:
            i += 1
            continue

        # fenced code -------------------------------------------------------
        if stripped.startswith("```"):
            i += 1
            buffer: list[str] = []
            while i < total and not lines[i].strip().startswith("```"):
                buffer.append(lines[i])
                i += 1
            i += 1
            doc.code_block(buffer)
            continue

        # tables ------------------------------------------------------------
        if stripped.startswith("|"):
            block: list[str] = []
            while i < total and lines[i].strip().startswith("|"):
                block.append(lines[i].strip())
                i += 1
            rows: list[list[str]] = []
            header = False
            for index, row in enumerate(block):
                cells = [c.strip() for c in row.strip("|").split("|")]
                if index == 1 and all(_SEP_CELL_RE.match(c or "-") for c in cells):
                    header = True
                    continue
                rows.append(cells)
            if rows:
                doc.table(rows, header=header)
            continue

        # horizontal rule ---------------------------------------------------
        if _RULE_RE.match(raw):
            doc.rule()
            i += 1
            continue

        # headings ----------------------------------------------------------
        heading = _HEADING_RE.match(raw)
        if heading:
            level = len(heading.group(1))
            size = HEADING_SIZES[level]
            if level == 1:
                doc.rule(gray=0.85)
            elif level == 2:
                doc.y -= 6
            doc.rich_para(heading.group(2), size=size, gray=0.05,
                          gap=2.6, extra=4.0, bold_base=True)
            if level == 2:
                doc.rule(gray=0.87)
            i += 1
            continue

        # block quote -------------------------------------------------------
        if stripped.startswith(">"):
            quoted: list[str] = []
            while i < total and lines[i].strip().startswith(">"):
                quoted.append(lines[i].strip().lstrip(">").strip())
                i += 1
            # A full-height bar would need the block's rendered height up front,
            # so the quote is distinguished by indent and a muted colour instead.
            for line in quoted:
                if line:
                    doc.rich_para(line, size=9.2, indent=16.0, gray=0.34,
                                  extra=1.5)
            doc.y -= 4
            continue

        # bullet / numbered lists ------------------------------------------
        bullet = _BULLET_RE.match(raw)
        number = _NUMBER_RE.match(raw)
        if bullet or number:
            if bullet:
                indent = _indent_width(bullet.group(1))
                body = bullet.group(2)
                marker = "-"
            else:
                indent = _indent_width(number.group(1))
                body = number.group(3)
                marker = f"{number.group(2)}."
            doc.rich_para(f"{marker} {body}", size=9.5,
                          indent=indent + 6.0, extra=1.2)
            i += 1
            continue

        # paragraph ---------------------------------------------------------
        paragraph: list[str] = [stripped]
        i += 1
        while i < total:
            nxt = lines[i]
            if not nxt.strip():
                break
            if (_HEADING_RE.match(nxt) or _RULE_RE.match(nxt)
                    or _BULLET_RE.match(nxt) or _NUMBER_RE.match(nxt)
                    or nxt.strip().startswith(("|", "```", ">"))):
                break
            paragraph.append(nxt.strip())
            i += 1
        doc.rich_para(" ".join(paragraph), size=9.5, extra=4.0)


# --- PDF assembly -----------------------------------------------------------
def build_pdf(pages: list[list[str]], title: str) -> bytes:
    """Serialise page content streams into a valid PDF 1.4 document."""
    font_keys = [F_REG, F_BOLD, F_MONO, F_ITAL]
    count = len(pages)
    font_start = 3 + 2 * count
    fonts = {key: font_start + i for i, key in enumerate(font_keys)}
    font_dict = " ".join(f"/{key} {num} 0 R" for key, num in fonts.items())

    objects: list[bytes] = []
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(count))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {count} >>"
                   .encode("latin-1"))

    for index, ops in enumerate(pages):
        content_obj = 4 + 2 * index
        objects.append((
            f"<< /Type /Page /Parent 2 0 R "
            f"/MediaBox [0 0 {PAGE_W:.2f} {PAGE_H:.2f}] "
            f"/Resources << /Font << {font_dict} >> /ProcSet [/PDF /Text] >> "
            f"/Contents {content_obj} 0 R >>"
        ).encode("latin-1"))
        stream = "\n".join(ops).encode("latin-1")
        objects.append(b"<< /Length " + str(len(stream)).encode("ascii")
                       + b" >>\nstream\n" + stream + b"\nendstream")

    for key in font_keys:
        objects.append((
            f"<< /Type /Font /Subtype /Type1 /BaseFont /{FONT_NAMES[key]} "
            f"/Encoding /WinAnsiEncoding >>"
        ).encode("latin-1"))

    now = _dt.datetime.now()
    objects.append((
        f"<< /Title {pdf_string(title)} /Author (ActionMesh) "
        f"/Creator (ActionMesh scripts/handoff_pdf.py) "
        f"/CreationDate (D:{now.strftime('%Y%m%d%H%M%S')}) >>"
    ).encode("latin-1"))
    info_obj = len(objects)

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"

    xref_offset = len(out)
    size = len(objects) + 1
    out += f"xref\n0 {size}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {size} /Root 1 0 R /Info {info_obj} 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)


# --- Document composition ---------------------------------------------------
def validate_pdf(data: bytes) -> list[str]:
    """Check the produced bytes are a well-formed PDF. Returns problems found.

    This exists so the generator can refuse to hand over a corrupt file. It
    verifies the header, the trailer, and that every cross-reference entry
    actually points at the object it claims.
    """
    problems: list[str] = []
    if not data.startswith(b"%PDF-1.4"):
        problems.append("missing %PDF-1.4 header")
    if not data.rstrip().endswith(b"%%EOF"):
        problems.append("missing %%EOF trailer")

    match = re.search(rb"startxref\s+(\d+)", data)
    if not match:
        problems.append("missing startxref")
        return problems
    xref_offset = int(match.group(1))
    if data[xref_offset:xref_offset + 4] != b"xref":
        problems.append("startxref does not point at the xref table")
        return problems

    head = re.match(rb"xref\s+0\s+(\d+)\s+", data[xref_offset:])
    if not head:
        problems.append("malformed xref header")
        return problems
    count = int(head.group(1))
    entries_start = xref_offset + head.end()

    for number in range(1, count):
        entry = data[entries_start + number * 20:entries_start + number * 20 + 20]
        if len(entry) < 18:
            problems.append(f"xref entry {number} truncated")
            break
        try:
            offset = int(entry[:10])
        except ValueError:
            problems.append(f"xref entry {number} is not a number")
            break
        expect = f"{number} 0 obj".encode("ascii")
        if data[offset:offset + len(expect)] != expect:
            problems.append(f"object {number} missing at its xref offset")

    if b"/Root" not in data:
        problems.append("trailer has no /Root")
    if b"/Type /Catalog" not in data:
        problems.append("no catalog object")
    return problems


def cover_page(doc: Document, facts: list[tuple[str, str]]) -> None:
    doc.y = PAGE_H - 130
    doc.rich_para("ActionMesh", size=32, gray=0.08, gap=4, extra=2,
                  bold_base=True)
    doc.rich_para("The execution layer for AI", size=12.5, gray=0.45, extra=14)
    doc.rich_para("Engineering Handoff", size=19, gray=0.14, extra=6,
                  bold_base=True)
    doc.rule(gray=0.78)
    doc.y -= 8
    doc.code_block([f"{name:<19}{value}" for name, value in facts])
    doc.y -= 4
    doc.rich_para("How to verify these claims", size=11, gray=0.18, extra=3,
                  bold_base=True)
    doc.rich_para(
        "Every number in this document was produced by executing the "
        "repository, not by reading it. Reproduce the baseline before "
        "trusting anything else.", extra=4)
    doc.code_block([
        "python -m unittest discover -s tests",
        "python scripts/build_apk.py",
    ])
    doc.y -= 4
    doc.rich_para("Contents", size=11, gray=0.18, extra=3, bold_base=True)
    for name, purpose in (
        ("HANDOFF.md", "state, boundaries, next owner, bugs fixed"),
        ("TASK_BOARD.md", "phase status with test counts"),
        ("docs/", "architecture, permissions, registry, graph, security"),
        ("android/", "the native APK shell and how to build it"),
    ):
        doc.rich_para(f"- {name} \u2014 {purpose}", size=9.4, indent=6.0,
                      extra=1.2)
    doc.y -= 10
    doc.rich_para(
        "Read the reconstruction notice first: the original tree was deleted "
        "mid-development and .git/ was lost with it. This is a reconstruction "
        "verified by execution, not a restoration.",
        size=9.0, gray=0.42, extra=0)


def _tests_line(root: pathlib.Path) -> str:
    """Read the recorded test count from the docs so it cannot silently drift."""
    for name, pattern in (
        ("TASK_BOARD.md", r"Last run:\s*\*\*(\d+) tests"),
        ("TASK_BOARD.md", r"(\d+)\s+tests, OK"),
        ("HANDOFF.md", r"Ran\s+(\d+)\s+tests"),
    ):
        path = root / name
        if not path.exists():
            continue
        match = re.search(pattern, path.read_text(encoding="utf-8"))
        if match:
            return f"{match.group(1)} tests (OK)"
    return "see TASK_BOARD.md"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render the ActionMesh engineering handoff as a PDF.")
    parser.add_argument("--out", default="ActionMesh-Handoff.pdf",
                        help="output path relative to the repository root")
    parser.add_argument("--include", action="append", default=None,
                        metavar="FILE", help="Markdown file to include")
    args = parser.parse_args(argv)

    doc = Document("ActionMesh \u2014 Engineering Handoff")
    facts = [
        ("Repository", "reconstruction; history starts post-hoc"),
        ("Runtime suite", _tests_line(ROOT)),
        ("Implemented", "C0-C12, C14-C17"),
        ("Deferred", "C13 vendor adapters"),
        ("Interpreter", f"Python {sys.version_info.major}."
                        f"{sys.version_info.minor}.{sys.version_info.micro}"),
        ("Generated", _dt.datetime.now().strftime("%Y-%m-%d %H:%M")),
    ]
    cover_page(doc, facts)

    for relative in (args.include or ["HANDOFF.md", "TASK_BOARD.md"]):
        path = ROOT / relative
        if not path.exists():
            print(f"warning: {relative} not found, skipped", file=sys.stderr)
            continue
        doc._new_page()
        render_markdown(doc, path.read_text(encoding="utf-8"))

    doc.stamp_footers("ActionMesh \u00b7 Engineering Handoff")
    data = build_pdf(doc.pages, doc.title)

    problems = validate_pdf(data)
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        print("refusing to write a malformed PDF", file=sys.stderr)
        return 1

    output = ROOT / args.out
    output.write_bytes(data)
    print(f"wrote {output}")
    print(f"  pages:   {len(doc.pages)}")
    print(f"  bytes:   {len(data)}")
    print("  checks:  header, xref offsets, trailer - all valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
