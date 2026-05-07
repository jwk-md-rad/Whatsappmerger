"""Render docs/QUICKSTART.md to a print-friendly PDF.

Pure-Python (fpdf2) rather than pandoc/LaTeX so the script runs on any
laptop the user already has Python on. The Markdown subset we recognise
is small on purpose:

* ``# / ## / ###`` headings
* paragraphs (blank-line separated)
* fenced ``` blocks (rendered as a monospace box)
* ordered lists (``1.``, ``2.``, …)
* unordered lists (``- ``)
* horizontal rules (``---``)
* inline code spans inside paragraphs/list items (``...``)
* inline ``**bold**``

That's enough for the quickstart and stays predictable to debug.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import date
from pathlib import Path

from fpdf import FPDF


PAGE = "A4"
MARGIN = 18  # mm

# Filled in by ``_register_fonts`` based on what's available on the host.
BODY_FONT = "Helvetica"
MONO_FONT = "Courier"
UNICODE_OK = False


# Candidate Unicode TTFs in priority order, covering Linux / macOS / Windows.
_CANDIDATE_FONTS: list[tuple[str, str, str, str]] = [
    # (label, regular, bold, mono)
    (
        "DejaVu",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    ),
    (
        "Helvetica (macOS)",
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Menlo.ttc",
    ),
    (
        "Arial (Windows)",
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\consola.ttf",
    ),
]


# ASCII transliteration used when no Unicode font is available.
_TRANSLITERATIONS = str.maketrans({
    "—": "--",
    "–": "-",
    "…": "...",
    "•": "*",
    "“": '"',
    "”": '"',
    "‘": "'",
    "’": "'",
    "→": "->",
    "←": "<-",
    "©": "(c)",
    "®": "(r)",
    "™": "(tm)",
})


def _register_fonts(pdf: FPDF) -> None:
    """Use a Unicode TTF if one is on disk; else fall back to Helvetica."""
    global BODY_FONT, MONO_FONT, UNICODE_OK
    for label, reg, bold, mono in _CANDIDATE_FONTS:
        if not (os.path.isfile(reg) and os.path.isfile(mono)):
            continue
        try:
            pdf.add_font("Body", "", reg)
            pdf.add_font("Body", "B", bold)
            pdf.add_font("Body", "I", reg)  # italic falls back to regular
            pdf.add_font("Mono", "", mono)
            BODY_FONT = "Body"
            MONO_FONT = "Mono"
            UNICODE_OK = True
            return
        except Exception:
            continue
    # leaves the Helvetica/Courier defaults in place


def _safe(text: str) -> str:
    return text if UNICODE_OK else text.translate(_TRANSLITERATIONS)

H1_SIZE = 22
H2_SIZE = 14
H3_SIZE = 12
BODY_SIZE = 10.5
CODE_SIZE = 9
SMALL_SIZE = 8

INK = (30, 30, 30)
HEADING_INK = (10, 60, 30)         # subdued green, matches the web UI accent
RULE = (200, 200, 200)
CODE_BG = (244, 244, 244)
CODE_BORDER = (220, 220, 220)


class Quickstart(FPDF):
    def __init__(self) -> None:
        super().__init__(format=PAGE, unit="mm")
        self.set_margins(MARGIN, MARGIN, MARGIN)
        self.set_auto_page_break(True, margin=MARGIN)
        self.alias_nb_pages()
        _register_fonts(self)
        self.title_text = _safe("WhatsApp Photo Archive — Quickstart")

    # -- chrome --------------------------------------------------------
    def header(self) -> None:
        if self.page_no() == 1:
            return
        self.set_font(BODY_FONT, "", SMALL_SIZE)
        self.set_text_color(*INK)
        self.cell(0, 5, self.title_text, align="L")
        self.cell(0, 5, _safe(f"Page {self.page_no()} / {{nb}}"), align="R")
        self.ln(7)
        self.set_draw_color(*RULE)
        self.line(MARGIN, self.get_y(), self.w - MARGIN, self.get_y())
        self.ln(3)

    def footer(self) -> None:
        if self.page_no() == 1:
            return
        self.set_y(-12)
        self.set_font(BODY_FONT, "I", SMALL_SIZE)
        self.set_text_color(140, 140, 140)
        self.cell(0, 5, f"Generated {date.today().isoformat()}", align="C")

    # -- helpers -------------------------------------------------------
    def hr(self) -> None:
        self.ln(2)
        self.set_draw_color(*RULE)
        self.line(MARGIN, self.get_y(), self.w - MARGIN, self.get_y())
        self.ln(4)

    def h1(self, text: str) -> None:
        self.set_font(BODY_FONT, "B", H1_SIZE)
        self.set_text_color(*HEADING_INK)
        self.multi_cell(0, 11, _safe(text))
        self.ln(2)

    def h2(self, text: str) -> None:
        self.ln(3)
        self.set_font(BODY_FONT, "B", H2_SIZE)
        self.set_text_color(*HEADING_INK)
        self.multi_cell(0, 7, _safe(text))
        self.ln(1)

    def h3(self, text: str) -> None:
        self.set_font(BODY_FONT, "B", H3_SIZE)
        self.set_text_color(*INK)
        self.multi_cell(0, 6, _safe(text))
        self.ln(1)

    def paragraph(self, text: str, indent_mm: float = 0.0) -> None:
        self.set_text_color(*INK)
        x_left = MARGIN + indent_mm
        self.set_x(x_left)
        width = self.w - 2 * MARGIN - indent_mm
        for fragment in _inline_fragments(text):
            self._render_fragment(fragment, width=width, x_left=x_left)
        # write() leaves the cursor on the last line; ln must clear line_h
        # before we get inter-paragraph spacing.
        self.ln(7)

    def code_block(self, lines: list[str]) -> None:
        """Render fenced ``` block as a tinted, monospaced box.

        Each line is drawn with ``fill=True`` so the tint follows the
        actual wrapped output. No double-pass: simpler and won't drift if
        a line wraps onto two physical lines.
        """
        lines = [_safe(line) for line in lines]
        self.ln(1)
        self.set_font(MONO_FONT, "", CODE_SIZE)
        self.set_text_color(*INK)
        self.set_fill_color(*CODE_BG)
        line_h = 5
        x = MARGIN
        width = self.w - 2 * MARGIN
        # Top padding strip.
        self.cell(width, 1.5, "", fill=True)
        self.ln(1.5)
        for line in lines:
            self.set_x(x)
            self.multi_cell(width, line_h, "  " + (line if line else " "),
                            align="L", fill=True)
        # Bottom padding strip.
        self.set_x(x)
        self.cell(width, 1.5, "", fill=True)
        self.ln(4)

    def bullet(self, text: str, marker: str) -> None:
        self.set_font(BODY_FONT, "", BODY_SIZE)
        self.set_text_color(*INK)
        marker = _safe(marker)
        marker_w = self.get_string_width(marker + " ") + 1
        x = MARGIN
        width = self.w - 2 * MARGIN
        # Pre-flight: don't orphan the marker on the previous page.
        if self.get_y() + 8 > self.h - self.b_margin:
            self.add_page()
        y = self.get_y()
        self.set_xy(x, y)
        self.cell(marker_w, 5, marker, align="L")
        # rest of the line wraps at indent
        self.set_xy(x + marker_w, y)
        for fragment in _inline_fragments(text):
            self._render_fragment(
                fragment,
                width=width - marker_w,
                x_left=x + marker_w,
            )
        self.ln(6)

    def _render_fragment(self, frag: tuple[str, str], width: float, x_left: float) -> None:
        kind, payload = frag
        payload = _safe(payload)
        if kind == "text":
            self.set_font(BODY_FONT, "", BODY_SIZE)
            self.set_text_color(*INK)
            self._wrap_inline(payload, width=width, x_left=x_left)
        elif kind == "bold":
            self.set_font(BODY_FONT, "B", BODY_SIZE)
            self.set_text_color(*INK)
            self._wrap_inline(payload, width=width, x_left=x_left)
        elif kind == "italic":
            self.set_font(BODY_FONT, "I", BODY_SIZE)
            self.set_text_color(*INK)
            self._wrap_inline(payload, width=width, x_left=x_left)
        elif kind == "code":
            self.set_font(MONO_FONT, "", BODY_SIZE - 0.5)
            self.set_text_color(*INK)
            self._wrap_inline(payload, width=width, x_left=x_left)

    def _wrap_inline(self, text: str, *, width: float, x_left: float) -> None:
        """Word-wrap ``text`` while preserving the spaces between runs.

        We split into alternating word / whitespace tokens so the gap at
        the boundary of one inline run (e.g. ``click *Apps*``) survives
        through to the rendered output.
        """
        for tok in re.findall(r"\S+|\s+", text):
            if not tok:
                continue
            tok_w = self.get_string_width(tok)
            if tok.isspace():
                # Drop trailing whitespace if it would overflow.
                if self.get_x() + tok_w > x_left + width:
                    continue
                self.write(5, tok)
                continue
            if self.get_x() + tok_w > x_left + width:
                self.ln(5)
                if self.get_y() > self.h - self.b_margin:
                    self.add_page()
                self.set_x(x_left)
            self.write(5, tok)


# ---------------------------------------------------------------------------
# Inline + block parsing
# ---------------------------------------------------------------------------


_INLINE_PATTERN = re.compile(
    r"\*\*(?P<bold>[^*]+)\*\*"
    r"|`(?P<code>[^`]+)`"
    r"|(?<![\*\w])\*(?P<italic>[^*\n]+?)\*(?![\*\w])"
)


def _inline_fragments(text: str) -> list[tuple[str, str]]:
    """Split a paragraph into [('text'|'bold'|'italic'|'code', value), …]."""
    out: list[tuple[str, str]] = []
    pos = 0
    for m in _INLINE_PATTERN.finditer(text):
        if m.start() > pos:
            out.append(("text", text[pos : m.start()]))
        if m.group("bold"):
            out.append(("bold", m.group("bold")))
        elif m.group("italic"):
            out.append(("italic", m.group("italic")))
        elif m.group("code"):
            out.append(("code", m.group("code")))
        pos = m.end()
    if pos < len(text):
        out.append(("text", text[pos:]))
    return out


def parse_blocks(md: str) -> list[tuple[str, object]]:
    """Tokenise a small Markdown subset into a list of (kind, payload)."""
    blocks: list[tuple[str, object]] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        if stripped == "---":
            blocks.append(("hr", None))
            i += 1
            continue

        if stripped.startswith("```"):
            i += 1
            buf: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1  # skip closing fence
            blocks.append(("code", buf))
            continue

        if stripped.startswith("### "):
            blocks.append(("h3", stripped[4:]))
            i += 1
            continue
        if stripped.startswith("## "):
            blocks.append(("h2", stripped[3:]))
            i += 1
            continue
        if stripped.startswith("# "):
            blocks.append(("h1", stripped[2:]))
            i += 1
            continue

        if re.match(r"^\d+\.\s+", stripped) or stripped.startswith("- "):
            # consume contiguous list lines, including continuation lines
            list_kind = "ol" if re.match(r"^\d+\.\s+", stripped) else "ul"
            items: list[str] = []
            while i < len(lines):
                ln = lines[i]
                s = ln.strip()
                if not s:
                    break
                m_ol = re.match(r"^(\d+)\.\s+(.*)$", s)
                m_ul = re.match(r"^- (.*)$", s)
                if m_ol and list_kind == "ol":
                    items.append(m_ol.group(2))
                elif m_ul and list_kind == "ul":
                    items.append(m_ul.group(1))
                elif ln.startswith("  ") and items:
                    items[-1] += " " + s
                else:
                    break
                i += 1
            blocks.append((list_kind, items))
            continue

        # paragraph: gather contiguous non-blank, non-block-starter lines
        para: list[str] = [stripped]
        i += 1
        while i < len(lines):
            ln = lines[i]
            s = ln.strip()
            if not s:
                break
            if (
                s.startswith("# ")
                or s.startswith("## ")
                or s.startswith("### ")
                or s.startswith("```")
                or s == "---"
                or re.match(r"^\d+\.\s+", s)
                or s.startswith("- ")
            ):
                break
            para.append(s)
            i += 1
        blocks.append(("p", " ".join(para)))

    return blocks


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render(blocks: list[tuple[str, object]], out_path: Path) -> None:
    pdf = Quickstart()
    pdf.add_page()
    for kind, payload in blocks:
        if kind == "h1":
            pdf.h1(payload)  # type: ignore[arg-type]
        elif kind == "h2":
            pdf.h2(payload)  # type: ignore[arg-type]
        elif kind == "h3":
            pdf.h3(payload)  # type: ignore[arg-type]
        elif kind == "p":
            pdf.paragraph(payload)  # type: ignore[arg-type]
        elif kind == "code":
            pdf.code_block(payload)  # type: ignore[arg-type]
        elif kind == "ol":
            for n, item in enumerate(payload, 1):  # type: ignore[arg-type]
                pdf.bullet(item, f"{n}.")
        elif kind == "ul":
            for item in payload:  # type: ignore[arg-type]
                pdf.bullet(item, "•")
        elif kind == "hr":
            pdf.hr()
    pdf.output(str(out_path))


def main(argv: list[str] | None = None) -> int:
    src = Path(__file__).parent / "QUICKSTART.md"
    out = Path(__file__).parent / "QUICKSTART.pdf"
    if argv:
        if len(argv) >= 1:
            src = Path(argv[0])
        if len(argv) >= 2:
            out = Path(argv[1])
    md = src.read_text()
    blocks = parse_blocks(md)
    render(blocks, out)
    print(f"Wrote {out} ({out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
