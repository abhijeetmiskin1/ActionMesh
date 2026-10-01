"""Tests for scripts/handoff_pdf.py.

The handoff PDF is a deliverable, so its generator is covered like any other
code: the escaping, the wrapping, and — most importantly — the structural
validity of the bytes it emits. A PDF that opens in no reader is worse than no
PDF, so ``validate_pdf`` is asserted on both a good and a deliberately
corrupted document.
"""

from __future__ import annotations

import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "handoff_pdf.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("handoff_pdf", SCRIPT)
    if spec is None or spec.loader is None:  # pragma: no cover - import guard
        raise unittest.SkipTest("handoff_pdf.py is not importable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HandoffPdfTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not SCRIPT.exists():  # pragma: no cover - repository guard
            raise unittest.SkipTest("scripts/handoff_pdf.py is absent")
        cls.mod = _load_module()

    # -- sanitising --------------------------------------------------------
    def test_sanitise_replaces_glyphs_outside_winansi(self):
        raw = "a\u2713 b\u2502 c\u20b9 d\u2014"
        safe = self.mod.sanitise(raw)
        self.assertNotIn("\u2713", safe)
        self.assertNotIn("\u2502", safe)
        self.assertNotIn("\u20b9", safe)
        # every surviving character must be encodable in Latin-1
        safe.encode("latin-1")

    def test_sanitise_maps_box_drawing_to_ascii(self):
        # Box-drawing glyphs become plain ASCII so the PDF stream stays valid
        # Latin-1 and the layout diagrams remain legible.
        self.assertEqual(self.mod.sanitise("\u251c\u2500\u2514"), "+--+-")

    def test_sanitise_replaces_controls_with_spaces(self):
        self.assertEqual(self.mod.sanitise("a\tb\nc"), "a    b c")

    def test_pdf_string_escapes_delimiters(self):
        rendered = self.mod.pdf_string(r"f(x) \ y")
        self.assertEqual(rendered, r"(f\(x\) \\ y)")

    # -- measuring and wrapping -------------------------------------------
    def test_wrap_never_exceeds_the_column(self):
        text = " ".join(["alpha"] * 60)
        limit = 120.0
        for line in self.mod.wrap(text, self.mod.F_REG, 9.5, limit):
            self.assertLessEqual(
                self.mod.text_width(line, self.mod.F_REG, 9.5), limit + 0.01)

    def test_wrap_hard_splits_an_unbreakable_token(self):
        token = "x" * 400
        lines = self.mod.wrap_mono(token, 20)
        self.assertGreater(len(lines), 1)
        for line in lines:
            self.assertLessEqual(len(line), 20)

    def test_mono_width_is_proportional_to_length(self):
        short = self.mod.text_width("abcd", self.mod.F_MONO, 8.0)
        long = self.mod.text_width("abcdefgh", self.mod.F_MONO, 8.0)
        self.assertAlmostEqual(long, short * 2, places=6)

    # -- document structure ------------------------------------------------
    def test_built_pdf_passes_its_own_validator(self):
        doc = self.mod.Document("unit test")
        self.mod.render_markdown(
            doc,
            "# Title\n\nSome **bold** and `code` text.\n\n"
            "```\ncommand --flag\n```\n\n"
            "| a | b |\n| --- | --- |\n| 1 | 2 |\n",
        )
        doc.stamp_footers("unit test")
        data = self.mod.build_pdf(doc.pages, doc.title)
        self.assertEqual(self.mod.validate_pdf(data), [])

    def test_every_page_and_object_is_referenced(self):
        doc = self.mod.Document("unit test")
        self.mod.render_markdown(doc, "hello world")
        data = self.mod.build_pdf(doc.pages, doc.title)
        self.assertEqual(data.count(b"/Type /Page "), 1)
        self.assertIn(b"/Type /Catalog", data)

    def test_validator_rejects_a_corrupt_header(self):
        problems = self.mod.validate_pdf(b"not a pdf at all")
        self.assertTrue(problems)
        self.assertTrue(any("header" in problem for problem in problems))

    def test_validator_rejects_a_broken_cross_reference(self):
        doc = self.mod.Document("unit test")
        self.mod.render_markdown(doc, "hello")
        data = self.mod.build_pdf(doc.pages, doc.title)
        # Point every object offset at the wrong place.
        broken = data.replace(b"0000000", b"0000001")
        self.assertTrue(self.mod.validate_pdf(broken))

    def test_render_markdown_survives_the_task_board_constructs(self):
        doc = self.mod.Document("unit test")
        self.mod.render_markdown(
            doc,
            "> quoted\n\n1. first\n2. second\n\n- bullet\n\n---\n\n#### h4\n",
        )
        self.assertTrue(doc.pages[0])

    def test_source_documents_are_rendered_into_the_bytes(self):
        doc = self.mod.Document("unit test")
        self.mod.render_markdown(doc, "# SentinelHeading\n\nbody text here")
        data = self.mod.build_pdf(doc.pages, doc.title)
        self.assertIn(b"SentinelHeading", data)


if __name__ == "__main__":
    unittest.main()
