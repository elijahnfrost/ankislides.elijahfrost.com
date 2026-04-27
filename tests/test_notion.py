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

    def test_nested_notion_zip_is_detected_and_extracted(self):
        """Notion's "Export everything" can wrap one zip per page inside an
        outer archive. The dispatcher must still pick the Notion code path
        in that shape (rather than falling through to the "couldn't find a
        deck" diagnostic the user previously hit)."""
        # Inner zip: one page's HTML export plus its image folder.
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as zf:
            zf.writestr(
                "Spinal Cord abc/page.html",
                "<details><summary>Q1</summary>"
                "A1<img src=\"diagram.png\"></details>",
            )
            zf.writestr("Spinal Cord abc/diagram.png", b"\x89PNG\r\n\x1a\n")
        # Outer zip carrying just the inner zip — exactly the shape that
        # produced the original bug report.
        outer = io.BytesIO()
        with zipfile.ZipFile(outer, "w") as zf:
            zf.writestr("Spinal Cord.zip", inner.getvalue())
        body = outer.getvalue()
        self.assertTrue(self.api._is_notion_zip(body))
        with tempfile.TemporaryDirectory() as d:
            text, kind, mc = self.api._extract_notion_zip(body, Path(d))
            self.assertEqual(kind, "notion-html")
            self.assertEqual(mc, 1)
            self.assertIn("<details>", text)
            self.assertTrue((Path(d) / "diagram.png").is_file())

    def test_nested_zip_recursion_capped(self):
        """Pathological deep nesting must terminate without recursing
        forever (zip-bomb defence)."""
        # Build a chain of empty zips, each wrapping the next, deeper than
        # the depth cap. None of them contain html/md so detection should
        # return False — but importantly it should *return*, not blow the
        # stack.
        inner_bytes = b""
        for i in range(8):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                if inner_bytes:
                    zf.writestr(f"layer-{i}.zip", inner_bytes)
                else:
                    zf.writestr("readme.txt", b"empty")
            inner_bytes = buf.getvalue()
        self.assertFalse(self.api._is_notion_zip(inner_bytes))


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

    def test_zip_with_nested_apkg_returns_one_deck_per_apkg(self):
        """A zip with two .apkg files should surface as two separate decks
        (so the handler renders one output file per deck inside an outer
        zip), not be merged into one mega-deck."""
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
            decks, _media, contents = self.api._extract_zip_flat(body, Path(d))
        self.assertEqual(len(decks), 2)
        deck_names = [d[0] for d in decks]
        self.assertIn("deck-a", deck_names)
        self.assertIn("deck-b", deck_names)
        # Each deck's text parses as its own deck.
        per_deck_texts = {
            name: {s.text for s in ats.read_cards_from_text(text, Path("/nonexistent"))}
            for name, text in decks
        }
        self.assertEqual(per_deck_texts["deck-a"], {"Q1", "A1"})
        self.assertEqual(per_deck_texts["deck-b"], {"Q2", "A2"})
        self.assertEqual(contents.get(".apkg"), 2)

    def test_zip_with_csv_export_is_accepted(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("deck.csv", "Front\tBack\nQ1\tA1\n")
            zf.writestr("logo.png", b"\x89PNG\r\n\x1a\n")
        body = buf.getvalue()
        with tempfile.TemporaryDirectory() as d:
            decks, media, contents = self.api._extract_zip_flat(body, Path(d))
        self.assertEqual(len(decks), 1)
        self.assertEqual(decks[0][0], "deck")
        self.assertEqual(media, 1)
        self.assertEqual(contents.get(".csv"), 1)
        # The csv shape parses identically to the Anki txt export.
        sides = ats.read_cards_from_text(decks[0][1], Path("/nonexistent"))
        self.assertEqual([s.text for s in sides[-2:]], ["Q1", "A1"])

    def test_zip_with_notion_database_csv_drops_header_and_parses_rows(self):
        """A Notion database CSV is comma-separated and starts with a
        ``Name,…`` header. It should be auto-detected and the header
        should be dropped before parsing."""
        buf = io.BytesIO()
        notion_csv = (
            "Name,Tags,Created\r\n"
            "Vertebral column,Anatomy,2024-01-01\r\n"
            "Spinal cord,Anatomy,2024-01-02\r\n"
        )
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("MyDatabase.csv", notion_csv)
        body = buf.getvalue()
        with tempfile.TemporaryDirectory() as d:
            decks, _media, _contents = self.api._extract_zip_flat(
                body, Path(d)
            )
        self.assertEqual(len(decks), 1)
        sides = ats.read_cards_from_text(decks[0][1], Path("/nonexistent"))
        # Two rows -> 4 sides (front+back each), Name header should be gone.
        fronts = [s.text for s in sides[::2]]
        self.assertEqual(fronts, ["Vertebral column", "Spinal cord"])
        self.assertNotIn("Name", fronts)

    def test_csv_helper_keeps_anki_tsv_unchanged_shape(self):
        """A real Anki TSV export starts with a card row, not a header — the
        helper must not eat the first row in that case."""
        anki_txt = "Q1\tA1\nQ2\tA2\n"
        normalised = self.api._csv_text_to_tsv(anki_txt)
        sides = ats.read_cards_from_text(normalised, Path("/nonexistent"))
        self.assertEqual([s.text for s in sides], ["Q1", "A1", "Q2", "A2"])

    def test_csv_helper_handles_quoted_commas(self):
        normalised = self.api._csv_text_to_tsv(
            'Front,Back\r\n"Hello, world","A greeting"\r\n'
        )
        sides = ats.read_cards_from_text(normalised, Path("/nonexistent"))
        # "Front" header is in our whitelist, so it gets dropped.
        self.assertEqual(
            [s.text for s in sides], ["Hello, world", "A greeting"]
        )

    def test_zip_with_no_deck_returns_diagnostic_summary(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("logo.png", b"\x89PNG\r\n\x1a\n")
            zf.writestr("notes.pdf", b"%PDF-1.4")
        body = buf.getvalue()
        with tempfile.TemporaryDirectory() as d:
            decks, _media, contents = self.api._extract_zip_flat(body, Path(d))
        self.assertEqual(decks, [])
        summary = self.api._summarize_zip_contents(contents)
        self.assertIn(".png", summary)
        self.assertIn(".pdf", summary)

    def test_zip_with_two_txt_files_returns_two_decks(self):
        """Two .txt exports inside a single zip should be split into two
        separate decks rather than being merged."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("alpha.txt", "Q1\tA1\n")
            zf.writestr("beta.txt", "Q2\tA2\n")
        body = buf.getvalue()
        with tempfile.TemporaryDirectory() as d:
            decks, _media, _contents = self.api._extract_zip_flat(body, Path(d))
        self.assertEqual(len(decks), 2)
        self.assertEqual({d[0] for d in decks}, {"alpha", "beta"})

    def test_dedupe_deck_names_handles_collisions(self):
        self.assertEqual(
            self.api._dedupe_deck_names(["deck", "deck", "deck", "other"]),
            ["deck", "deck-2", "deck-3", "other"],
        )

    def test_render_multi_deck_outputs_one_entry_per_deck(self):
        """Driving the handler's multi-deck renderer with two decks should
        produce an outer zip containing exactly one PDF per deck."""
        sides_a = ats.read_cards_from_text("Q1\tA1\n", Path("/nonexistent"))
        sides_b = ats.read_cards_from_text("Q2\tA2\n", Path("/nonexistent"))
        # _render_multi_deck is an instance method on the BaseHTTPRequestHandler
        # subclass, so we bind it manually with a SimpleNamespace as `self`.
        # It only references `self` for the method name; no other state.
        handler_cls = self.api.handler
        bound = handler_cls._render_multi_deck
        outer_bytes, total = bound(
            None, [("alpha", sides_a), ("beta", sides_b)], "pdf"
        )
        self.assertGreater(total, 0)
        self.assertEqual(outer_bytes[:2], b"PK")  # zip magic
        with zipfile.ZipFile(io.BytesIO(outer_bytes)) as zf:
            names = set(zf.namelist())
        self.assertEqual(names, {"alpha.pdf", "beta.pdf"})

    def test_render_multi_deck_apkg_uses_per_deck_names(self):
        sides_a = ats.read_cards_from_text("Q1\tA1\n", Path("/nonexistent"))
        sides_b = ats.read_cards_from_text("Q2\tA2\n", Path("/nonexistent"))
        bound = self.api.handler._render_multi_deck
        outer_bytes, _ = bound(
            None, [("alpha", sides_a), ("beta", sides_b)], "apkg"
        )
        with zipfile.ZipFile(io.BytesIO(outer_bytes)) as zf:
            names = set(zf.namelist())
        self.assertEqual(names, {"alpha.apkg", "beta.apkg"})


if __name__ == "__main__":
    unittest.main()
