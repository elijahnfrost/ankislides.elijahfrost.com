"""Smoke tests for the Notion parser, the new Anki output renderers, and the
end-to-end input-detection logic in ``api/convert.py``.

Run with::

    python -m unittest discover tests

These are deliberately lightweight — they don't require a network or
Vercel; they exercise the same code paths the production endpoint uses.
"""
from __future__ import annotations

import importlib.util
import io
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import anki_to_slides as ats  # noqa: E402

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load_api_module():
    spec = importlib.util.spec_from_file_location(
        "api_convert", REPO_ROOT / "api" / "convert.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NotionParserTests(unittest.TestCase):
    def test_html_parser_emits_one_card_per_toggle(self):
        html = (_FIXTURES / "notion_page.html").read_text("utf-8")
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "python-logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            sides = ats.read_cards_from_notion_html(html, Path(d))
        # Three toggles -> three cards -> six sides.
        self.assertEqual(len(sides), 6)
        self.assertEqual(sides[0].text, "What is Python?")
        self.assertEqual(sides[1].text, "A high-level programming language.")
        self.assertEqual([p.name for p in sides[1].images], ["python-logo.png"])

    def test_html_parser_does_not_leak_nested_details_into_parent_back(self):
        html = (_FIXTURES / "notion_page.html").read_text("utf-8")
        sides = ats.read_cards_from_notion_html(html, Path("/nonexistent"))
        cards = ats.sides_to_cards(sides)
        # Inner toggle becomes its own card BEFORE the outer in our walk,
        # because handle_endtag fires inner-first. Find by summary text.
        outer = next(c for c in cards if c[0].text == "Outer toggle")
        self.assertNotIn("Inner toggle", outer[1].text)
        self.assertNotIn("Inner back text", outer[1].text)
        self.assertIn("Outer back text", outer[1].text)
        self.assertIn("After-inner outer text", outer[1].text)

    def test_markdown_parser_handles_image_syntax_and_toggles(self):
        md = (_FIXTURES / "notion_page.md").read_text("utf-8")
        sides = ats.read_cards_from_notion_markdown(md, Path("/nonexistent"))
        # Same three toggles as the HTML fixture.
        self.assertEqual(len(sides) // 2, 3)
        # The image reference inside the first toggle should be picked up
        # via `![alt](path)` -> <img src=...>.
        first_back = sides[1]
        self.assertEqual([p.name for p in first_back.images], ["python-logo.png"])

    def test_empty_input_returns_no_cards(self):
        self.assertEqual(ats.read_cards_from_notion_html("", Path("/x")), [])
        self.assertEqual(
            ats.read_cards_from_notion_markdown("# Heading only\n", Path("/x")),
            [],
        )


class AnkiOutputRendererTests(unittest.TestCase):
    def test_apkg_bytes_round_trip_through_extractor(self):
        api = _load_api_module()
        sides = ats.read_cards_from_notion_html(
            "<details><summary>Q1</summary>A1</details>"
            "<details><summary>Q2</summary>A2</details>",
            Path("/nonexistent"),
        )
        apkg = ats.render_apkg_bytes(sides, deck_name="RoundTrip")
        # Magic: zip header.
        self.assertEqual(apkg[:4], b"PK\x03\x04")
        with tempfile.TemporaryDirectory() as d:
            text, _ = api._extract_anki_bundle(apkg, Path(d))
        reparsed = ats.read_cards_from_text(text, Path("/nonexistent"))
        self.assertEqual([s.text for s in reparsed], ["Q1", "A1", "Q2", "A2"])

    def test_apkg_includes_referenced_media(self):
        with tempfile.TemporaryDirectory() as d:
            img_path = Path(d) / "logo.png"
            img_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
            sides = ats.read_cards_from_notion_html(
                f'<details><summary>Q</summary>A<img src="logo.png"></details>',
                Path(d),
            )
            apkg = ats.render_apkg_bytes(sides, deck_name="WithMedia")
            with zipfile.ZipFile(io.BytesIO(apkg)) as zf:
                names = set(zf.namelist())
        # genanki names media as "0", "1", … and writes a media manifest.
        self.assertIn("media", names)
        # At least one numeric media blob should exist.
        self.assertTrue(any(n.isdigit() for n in names))

    def test_anki_txt_zip_contains_tsv_and_media(self):
        with tempfile.TemporaryDirectory() as d:
            img_path = Path(d) / "logo.png"
            img_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
            sides = ats.read_cards_from_notion_html(
                f'<details><summary>Q1</summary>A1<img src="logo.png"></details>'
                f'<details><summary>Q2</summary>A2</details>',
                Path(d),
            )
            zip_bytes = ats.render_anki_txt_zip_bytes(sides, stem="deck")
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = set(zf.namelist())
            tsv = zf.read("deck.txt").decode("utf-8")
        self.assertIn("deck.txt", names)
        self.assertIn("logo.png", names)
        # Two cards => two rows.
        self.assertEqual(tsv.strip().count("\n") + 1, 2)
        self.assertIn("logo.png", tsv)


class ApiDetectionTests(unittest.TestCase):
    def setUp(self):
        self.api = _load_api_module()

    def test_notion_html_detection(self):
        body = b"<html><body><details><summary>Q</summary>A</details></body></html>"
        self.assertTrue(self.api._looks_like_notion_html(body))
        self.assertFalse(self.api._is_anki_bundle(body))
        self.assertFalse(self.api._is_notion_zip(body))

    def test_notion_zip_detection_md(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("Page abc/page.md", "<details><summary>Q</summary>A</details>")
            zf.writestr("Page abc/img.png", b"\x89PNG\r\n\x1a\n")
        body = buf.getvalue()
        self.assertTrue(self.api._is_notion_zip(body))
        self.assertFalse(self.api._is_anki_bundle(body))
        with tempfile.TemporaryDirectory() as d:
            text, kind, mc = self.api._extract_notion_zip(body, Path(d))
        self.assertEqual(kind, "notion-md")
        self.assertEqual(mc, 1)
        self.assertIn("<details>", text)

    def test_notion_zip_detection_html(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("Page abc/page.html", "<details><summary>Q</summary>A</details>")
        body = buf.getvalue()
        self.assertTrue(self.api._is_notion_zip(body))
        with tempfile.TemporaryDirectory() as d:
            _, kind, _ = self.api._extract_notion_zip(body, Path(d))
        self.assertEqual(kind, "notion-html")

    def test_anki_zip_with_txt_is_not_notion(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("deck.txt", "Front\tBack\n")
            zf.writestr("logo.png", b"\x89PNG\r\n\x1a\n")
        body = buf.getvalue()
        self.assertFalse(self.api._is_notion_zip(body))


class ZipFallbackTests(unittest.TestCase):
    """The flat-zip extractor in api/convert.py also recognises .csv/.tsv
    text exports and zips that bundle one or more nested .apkg archives — both
    common shapes that previously failed with 'no .txt file found inside the
    zip'.
    """

    def setUp(self):
        self.api = _load_api_module()

    def _make_apkg(self, front: str, back: str) -> bytes:
        sides = ats.read_cards_from_notion_html(
            f"<details><summary>{front}</summary>{back}</details>",
            Path("/nonexistent"),
        )
        return ats.render_apkg_bytes(sides, deck_name="Inner")

    def test_zip_with_nested_apkg_extracts_cards(self):
        inner_a = self._make_apkg("Q1", "A1")
        inner_b = self._make_apkg("Q2", "A2")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("deck-a.apkg", inner_a)
            zf.writestr("subdir/deck-b.apkg", inner_b)
        body = buf.getvalue()
        # Plain anki-bundle detection looks at the *outer* zip and won't match.
        self.assertFalse(self.api._is_anki_bundle(body))
        with tempfile.TemporaryDirectory() as d:
            text, _media, contents = self.api._extract_zip_flat(body, Path(d))
        self.assertIsNotNone(text)
        sides = ats.read_cards_from_text(text, Path("/nonexistent"))
        texts = {s.text for s in sides}
        self.assertIn("Q1", texts)
        self.assertIn("A1", texts)
        self.assertIn("Q2", texts)
        self.assertIn("A2", texts)
        self.assertEqual(contents.get(".apkg"), 2)

    def test_zip_with_csv_export_is_accepted(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("deck.csv", "Front\tBack\nQ1\tA1\n")
            zf.writestr("logo.png", b"\x89PNG\r\n\x1a\n")
        body = buf.getvalue()
        with tempfile.TemporaryDirectory() as d:
            text, media, contents = self.api._extract_zip_flat(body, Path(d))
        self.assertIsNotNone(text)
        self.assertEqual(media, 1)
        self.assertEqual(contents.get(".csv"), 1)
        # The csv shape parses identically to the Anki txt export.
        sides = ats.read_cards_from_text(text, Path("/nonexistent"))
        self.assertEqual([s.text for s in sides[-2:]], ["Q1", "A1"])

    def test_zip_with_no_deck_returns_diagnostic_summary(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("logo.png", b"\x89PNG\r\n\x1a\n")
            zf.writestr("notes.pdf", b"%PDF-1.4")
        body = buf.getvalue()
        with tempfile.TemporaryDirectory() as d:
            text, _media, contents = self.api._extract_zip_flat(body, Path(d))
        self.assertIsNone(text)
        summary = self.api._summarize_zip_contents(contents)
        self.assertIn(".png", summary)
        self.assertIn(".pdf", summary)


if __name__ == "__main__":
    unittest.main()
