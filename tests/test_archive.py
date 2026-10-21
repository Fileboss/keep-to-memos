import contextlib
import io
import json
import shutil
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import keep_to_memos  # noqa: E402
from keep_to_memos import memo_uid  # noqa: E402
from memoexport_validator import ArchiveError, read_archive  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GOLDEN = FIXTURES / "memos-golden-1.0.zip"


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = keep_to_memos.main([str(a) for a in argv])
    return code, out.getvalue(), err.getvalue()


class ValidatorCalibrationTest(unittest.TestCase):
    """Le validateur accepte l'archive de référence de Memos et rejette ses variantes cassées."""

    def test_golden_archive_is_accepted(self):
        archive = read_archive(GOLDEN)
        self.assertEqual(set(archive["memos"]), {"Ab3kZ9q2", "Xy7LmN01", "Cm9Rt4v7"})
        self.assertEqual(archive["warnings"], [])

    def _rewrite_golden(self, tmp, transform):
        target = Path(tmp) / "broken.zip"
        with zipfile.ZipFile(GOLDEN) as src, zipfile.ZipFile(target, "w") as dst:
            for info in src.infolist():
                result = transform(info.filename, src.read(info))
                if result is not None:
                    dst.writestr(result[0], result[1])
        return target

    def test_broken_variants_are_rejected(self):
        variants = {
            "sans manifeste": lambda n, d: None if n == "manifest.json" else (n, d),
            "uid incohérent": lambda n, d: (n, d.replace(b'"uid":"Xy7LmN01"', b'"uid":"Autre"')),
            "contenu orphelin": lambda n, d: None if n == "memos/Xy7LmN01.json" else (n, d),
            "sha256 faux": lambda n, d: (n, d + b"!") if n.startswith("attachments/") else (n, d),
            "format inconnu": lambda n, d: (n, d.replace(b"memos-export", b"autre")),
        }
        with tempfile.TemporaryDirectory() as tmp:
            for label, transform in variants.items():
                with self.subTest(label):
                    with self.assertRaises(ArchiveError):
                        read_archive(self._rewrite_golden(tmp, transform))


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.keep = self.tmp / "Keep"
        shutil.copytree(FIXTURES / "Keep", self.keep)
        self.out = self.tmp / "export.zip"

    def tearDown(self):
        self._tmp.cleanup()

    def convert(self, *extra):
        code, out, err = run([self.keep, "-o", self.out, *extra])
        return code, out, err

    def test_fixtures_produce_a_valid_archive(self):
        before = int(time.time())
        code, out, _ = self.convert()
        self.assertEqual(code, 0, out)
        archive = read_archive(self.out)
        self.assertEqual(archive["warnings"], [])

        self.assertEqual(archive["names"][0], "manifest.json")
        manifest = archive["manifest"]
        self.assertEqual(manifest["generator"]["name"], "keep-to-memos")
        self.assertEqual(manifest["scope"], {"kind": "USER", "user": {"username": "keep"}})
        self.assertEqual(manifest["counts"], {"memos": 6, "attachments": 1})

        export_time = time.gmtime(before)[:6]
        for info in archive["infos"]:
            self.assertGreaterEqual(info.date_time, export_time[:5] + (0,))
            self.assertFalse(info.filename.endswith("/"))
            self.assertEqual(info.compress_type, zipfile.ZIP_DEFLATED)

        memos = archive["memos"]
        expected = {"Liste de courses.json", "Idée cadeau.json", "Titre seul.json",
                    "Vieille note.json", "Photo jardin.json", "Texte et liste.json"}
        self.assertEqual(set(memos), {memo_uid(name) for name in expected})

        courses = memos[memo_uid("Liste de courses.json")]
        self.assertEqual(courses["createTime"], "2023-11-14T22:13:20Z")
        self.assertEqual(courses["updateTime"], "2023-11-14T23:13:20Z")
        self.assertEqual((courses["state"], courses["pinned"], courses["visibility"]), ("NORMAL", True, "PRIVATE"))
        self.assertEqual(
            archive["contents"][courses["uid"]].decode(),
            "# Courses\n\n- [ ] Pain\n- [x] Lait demi-écrémé\n\n#maison #courses-été-2024 #keep",
        )

        old = memos[memo_uid("Vieille note.json")]
        self.assertEqual((old["state"], old["pinned"]), ("ARCHIVED", True))
        self.assertEqual(old["updateTime"], old["createTime"])

        photo = memos[memo_uid("Photo jardin.json")]
        [attachment] = photo["attachments"]
        self.assertEqual(attachment["filename"], "1a2b3c4d.jpg")
        self.assertEqual(attachment["type"], "image/jpeg")
        self.assertEqual(attachment["createTime"], photo["createTime"])
        self.assertEqual(attachment["path"], f"attachments/{attachment['uid']}/1a2b3c4d.jpg")
        self.assertEqual(archive["attachments"][attachment["uid"]], (self.keep / "1a2b3c4d.jpg").read_bytes())

        self.assertIn("[skip] Corbeille.json → corbeille", out)
        self.assertIn("[skip] Note vide.json → vide", out)
        self.assertFalse(self.out.with_name("export.zip.tmp").exists())

    def test_uids_are_deterministic(self):
        self.convert()
        first = read_archive(self.out)
        self.convert()
        second = read_archive(self.out)
        self.assertEqual(set(first["memos"]), set(second["memos"]))
        self.assertEqual(set(first["attachments"]), set(second["attachments"]))

    def test_options(self):
        code, _, _ = self.convert("--include-trashed", "--skip-archived", "--visibility", "protected",
                                  "--tag", "", "--username", "alice")
        self.assertEqual(code, 0)
        archive = read_archive(self.out)
        memos = archive["memos"]
        self.assertNotIn(memo_uid("Vieille note.json"), memos)
        trashed = memos[memo_uid("Corbeille.json")]
        self.assertEqual(trashed["state"], "ARCHIVED")
        self.assertEqual({m["visibility"] for m in memos.values()}, {"PROTECTED"})
        self.assertEqual({m["creator"] for m in memos.values()}, {"alice"})
        self.assertEqual(archive["contents"][trashed["uid"]].decode(), "# À jeter\n\nPlus utile.")

    def test_limit_counts_included_notes(self):
        code, out, _ = self.convert("--limit", "2")
        self.assertEqual(code, 0)
        memos = read_archive(self.out)["memos"]
        self.assertEqual(set(memos), {memo_uid("Vieille note.json"), memo_uid("Liste de courses.json")})

    def test_no_attachments(self):
        code, out, _ = self.convert("--no-attachments")
        self.assertEqual(code, 0)
        archive = read_archive(self.out)
        self.assertNotIn("attachments", archive["memos"][memo_uid("Photo jardin.json")])
        self.assertEqual(archive["manifest"]["counts"]["attachments"], 0)
        self.assertIn("1 pièce(s) jointe(s) ignorée(s)", out)

    def test_missing_attachment_warns_but_keeps_memo(self):
        (self.keep / "1a2b3c4d.jpg").unlink()
        code, out, _ = self.convert()
        self.assertEqual(code, 1)
        self.assertIn("pièce jointe introuvable : 1a2b3c4d.jpeg", out)
        memo = read_archive(self.out)["memos"][memo_uid("Photo jardin.json")]
        self.assertNotIn("attachments", memo)

    def test_invalid_json_is_reported_and_skipped(self):
        (self.keep / "Cassée.json").write_text("{pas du json", encoding="utf-8")
        (self.keep / "Liste.json").write_text("[]", encoding="utf-8")
        code, out, _ = self.convert()
        self.assertEqual(code, 1)
        self.assertIn("[fail] Cassée.json → JSON invalide", out)
        self.assertIn("[fail] Liste.json → JSON invalide", out)
        self.assertEqual(len(read_archive(self.out)["memos"]), 6)

    def test_long_content_warns(self):
        note = {"title": "Long", "textContent": "é" * 5000, "createdTimestampUsec": 1}
        (self.keep / "Long.json").write_text(json.dumps(note), encoding="utf-8")
        code, out, _ = self.convert()
        self.assertEqual(code, 1)
        self.assertRegex(out, r"\[warn\] Long\.json → k\w+ \(contenu de \d+ octets > 8192")

    def test_dry_run_writes_nothing(self):
        code, out, _ = self.convert("--dry-run")
        self.assertEqual(code, 0)
        self.assertFalse(self.out.exists())
        self.assertIn("# Rappeler le plombier", out)

    def test_default_output_name(self):
        with _chdir(self.tmp):
            code, out, _ = run([self.keep])
        self.assertEqual(code, 0)
        [produced] = self.tmp.glob("memos-export-keep-*.zip")
        self.assertRegex(produced.name, r"^memos-export-keep-\d{8}T\d{6}Z\.zip$")
        read_archive(produced)

    def test_usage_errors(self):
        self.assertEqual(run([self.tmp / "absent"])[0], 2)
        empty = self.tmp / "vide"
        empty.mkdir()
        self.assertEqual(run([empty])[0], 2)
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
            keep_to_memos.main([str(self.keep), "--limit", "0"])
        self.assertEqual(ctx.exception.code, 2)


@contextlib.contextmanager
def _chdir(path):
    import os
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
