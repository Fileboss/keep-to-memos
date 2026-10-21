import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from keep_to_memos import (  # noqa: E402
    format_time,
    is_empty,
    media_type,
    memo_uid,
    normalize_tag,
    note_to_markdown,
    safe_filename,
    usec_to_unix,
)
from memoexport_validator import UID_RE  # noqa: E402


class NoteToMarkdownTest(unittest.TestCase):
    def test_text_note(self):
        note = {"title": "Recette", "textContent": "Farine\n**pas** échappé\n"}
        self.assertEqual(note_to_markdown(note), "# Recette\n\nFarine\n**pas** échappé\n\n#keep")

    def test_checklist_keeps_order_and_flattens_lines(self):
        note = {"listContent": [
            {"text": "Pain", "isChecked": False},
            {"text": "Lait\ndemi-écrémé", "isChecked": True},
            {"text": "Œufs"},
        ]}
        self.assertEqual(
            note_to_markdown(note, None),
            "- [ ] Pain\n- [x] Lait demi-écrémé\n- [ ] Œufs",
        )

    def test_text_before_list(self):
        note = {"textContent": "Avant", "listContent": [{"text": "a", "isChecked": False}]}
        self.assertEqual(note_to_markdown(note, None), "Avant\n\n- [ ] a")

    def test_title_only(self):
        self.assertEqual(note_to_markdown({"title": "Seul"}, None), "# Seul")
        self.assertFalse(is_empty({"title": "Seul"}))

    def test_empty_note(self):
        self.assertTrue(is_empty({}))
        self.assertTrue(is_empty({"title": "  ", "textContent": "\n", "labels": [{"name": "x"}]}))
        self.assertTrue(is_empty({"listContent": [{"text": "", "isChecked": True}]}))
        self.assertFalse(is_empty({"attachments": [{"filePath": "a.jpg"}]}))

    def test_labels_and_extra_tag(self):
        note = {"textContent": "x", "labels": [{"name": "Maison"}, {"name": "maison"}, {"name": "!!!"}]}
        self.assertEqual(note_to_markdown(note), "x\n\n#maison #keep")
        self.assertEqual(note_to_markdown(note, "Import Keep"), "x\n\n#maison #import-keep")
        self.assertEqual(note_to_markdown(note, None), "x\n\n#maison")

    def test_annotations(self):
        note = {
            "textContent": "Vu sur https://a.example/x",
            "annotations": [
                {"url": "https://a.example/x", "title": "déjà là"},
                {"url": "https://b.example/", "title": "Site [b]"},
                {"url": "https://c.example/"},
                {"url": "https://b.example/", "title": "doublon"},
                {"title": "sans url"},
            ],
        }
        self.assertEqual(
            note_to_markdown(note, None),
            "Vu sur https://a.example/x\n\n"
            "- [Site \\[b\\]](https://b.example/)\n- [https://c.example/](https://c.example/)",
        )

    def test_missing_unknown_and_mistyped_fields(self):
        note = {"inconnu": {"a": 1}, "title": 42, "labels": "pas une liste",
                "listContent": [None, "x", {"text": "ok", "isChecked": "true"}]}
        self.assertEqual(note_to_markdown(note, None), "- [ ] ok")


class NormalizeTagTest(unittest.TestCase):
    def test_cases(self):
        cases = {
            "Courses Maison": "courses-maison",
            "Été 2024!": "été-2024",
            "  Deux   espaces ": "deux-espaces",
            "a//b/": "a/b",
            "/Maison/Réseau": "maison/réseau",
            "snake_case-ok": "snake_case-ok",
            "日本語": "日本語",
            "C++ & co.": "c-co",
            "!!!": None,
            "": None,
            "///": None,
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(normalize_tag(name), expected)

    def test_decomposed_accents_are_composed(self):
        self.assertEqual(normalize_tag("Été"), "été")


class HelpersTest(unittest.TestCase):
    def test_safe_filename_matches_memos(self):
        cases = {
            "photo.jpg": "photo.jpg",
            "a/b\\c:d.png": "a_b_c_d.png",
            " .x. ": "x",
            "": "file",
            "...": "file",
            "CON.txt": "_CON.txt",
            "com1": "_com1",
            "console.txt": "console.txt",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(safe_filename(name), expected)

    def test_memo_uid_is_valid_and_stable(self):
        uid = memo_uid("Idée cadeau.json")
        self.assertRegex(uid, UID_RE)
        self.assertEqual(len(uid), 32)
        self.assertEqual(uid, memo_uid("Idée cadeau.json"))
        self.assertEqual(uid, memo_uid("Idée cadeau.json"))
        self.assertNotEqual(uid, memo_uid("Autre.json"))

    def test_time(self):
        self.assertEqual(usec_to_unix(1_700_000_000_123_456), 1_700_000_000)
        self.assertEqual(usec_to_unix("1700000000000000"), 1_700_000_000)
        for bad in (None, True, "abc", 0, -5, 1.5):
            self.assertIsNone(usec_to_unix(bad))
        self.assertEqual(format_time(1_700_000_000), "2023-11-14T22:13:20Z")

    def test_media_type(self):
        self.assertEqual(media_type("image/jpeg", "x.jpg"), "image/jpeg")
        self.assertEqual(media_type("", "x.png"), "image/png")
        self.assertEqual(media_type("n'importe quoi", "x.jpg"), "image/jpeg")
        self.assertEqual(media_type("", "x.inconnu"), "application/octet-stream")


if __name__ == "__main__":
    unittest.main()
