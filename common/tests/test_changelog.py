"""Tests of common/scripts/changelog.py, run by make test."""

import importlib.util
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location("changelog", Path(__file__).resolve().parents[1] / "scripts/changelog.py")
changelog = importlib.util.module_from_spec(_spec)
sys.modules["changelog"] = changelog  # dataclasses look the module up by name
_spec.loader.exec_module(changelog)

GOOD = """# Изменения

Вступление.

## [Unreleased]

## [v0.2.0] — 2026-11-02

### Несовместимо

- `Do` принимает `Request`.

## [v0.1.1] — 2026-10-25

- Исправлено.

## [v0.1.0] - 2026-10-20

- Первая версия.
"""


class MalformedHeadingTest(unittest.TestCase):
    """A heading with a typo used to join the body of the section above it: check passed, latest missed the
    version, and the release workflow failed later with "the newest section is not vX.Y.Z"."""

    def test_text_after_the_date_is_reported(self):
        text = GOOD.replace("## [v0.2.0] — 2026-11-02", "## [v0.2.0] — 2026-11-02 (hotfix)")
        s = changelog.parse(text)
        self.assertEqual([x.name for x in s], ["Unreleased", "v0.2.0", "v0.1.1", "v0.1.0"])
        found = changelog.problems(s)
        self.assertEqual(len(found), 1, found)
        self.assertIn("line 7", found[0])
        self.assertIn("(hotfix)", found[0])
        self.assertEqual(changelog.latest(s), "v0.1.1", "a malformed section is not a release")

    def test_heading_without_brackets_is_reported(self):
        text = GOOD.replace("## [v0.1.1] — 2026-10-25", "## v0.1.1 — 2026-10-25")
        found = changelog.problems(changelog.parse(text))
        self.assertEqual(len(found), 1, found)
        self.assertIn("line 13", found[0])
        self.assertIn("## [vX.Y.Z]", found[0])

    def test_group_headings_are_not_sections(self):
        s = changelog.parse(GOOD)
        self.assertIn("### Несовместимо", s[1].body)


class ParseTest(unittest.TestCase):
    def test_sections_in_order_with_bodies(self):
        s = changelog.parse(GOOD)
        self.assertEqual([x.name for x in s], ["Unreleased", "v0.2.0", "v0.1.1", "v0.1.0"])
        self.assertEqual(s[0].body, "")
        self.assertEqual(s[1].date, "2026-11-02")
        self.assertEqual(s[1].body, "### Несовместимо\n\n- `Do` принимает `Request`.")
        self.assertEqual(s[3].date, "2026-10-20")  # a plain hyphen is accepted too

    def test_latest_and_notes(self):
        s = changelog.parse(GOOD)
        self.assertEqual(changelog.latest(s), "v0.2.0")
        self.assertEqual(changelog.notes(s, "v0.1.0"), "- Первая версия.")
        self.assertIsNone(changelog.notes(s, "v9.9.9"))

    def test_no_release_yet(self):
        s = changelog.parse("# Изменения\n\n## [Unreleased]\n\n- Черновик.\n")
        self.assertEqual(changelog.latest(s), "")
        self.assertEqual(changelog.problems(s), [])


class ProblemsTest(unittest.TestCase):
    def test_good_file(self):
        self.assertEqual(changelog.problems(changelog.parse(GOOD)), [])

    def assertProblem(self, text, fragment):
        found = changelog.problems(changelog.parse(text))
        self.assertTrue(any(fragment in p for p in found), f"{fragment!r} not in {found}")

    def test_unreleased_must_be_first(self):
        self.assertProblem("## [v0.1.0] — 2026-10-20\n\n- a\n", "first section")
        self.assertProblem("## [Unreleased]\n\n## [v0.1.0] — 2026-10-20\n\n- a\n\n## [Unreleased]\n", "must be the first")

    def test_bad_version_and_date(self):
        self.assertProblem("## [Unreleased]\n\n## [0.1.0] — 2026-10-20\n\n- a\n", "not a version")
        self.assertProblem("## [Unreleased]\n\n## [v0.1] — 2026-10-20\n\n- a\n", "not a version")
        self.assertProblem("## [Unreleased]\n\n## [v0.1.0]\n\n- a\n", "needs a date")
        self.assertProblem("## [Unreleased]\n\n## [v0.1.0] — 20.10.2026\n\n- a\n", "needs a date")

    def test_order_repeats_and_empty(self):
        self.assertProblem(
            "## [Unreleased]\n\n## [v0.1.0] — 2026-10-20\n\n- a\n\n## [v0.2.0] — 2026-10-21\n\n- b\n",
            "not older")
        self.assertProblem(
            "## [Unreleased]\n\n## [v0.1.0] — 2026-10-20\n\n- a\n\n## [v0.1.0] — 2026-10-20\n\n- a\n",
            "appears twice")
        self.assertProblem("## [Unreleased]\n\n## [v0.1.0] — 2026-10-20\n", "no entries")

    def test_minor_beats_patch_numerically(self):
        # v0.10.0 is newer than v0.9.0: compared as numbers, not strings.
        text = "## [Unreleased]\n\n## [v0.10.0] — 2026-10-21\n\n- b\n\n## [v0.9.0] — 2026-10-20\n\n- a\n"
        self.assertEqual(changelog.problems(changelog.parse(text)), [])


class MainTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.changelog = self.root / "CHANGELOG.md"
        self.changelog.write_text(GOOD, encoding="utf-8")

    def main(self, *args):
        return changelog.main(["changelog.py", "--root", str(self.root), *args])

    def test_commands(self):
        self.assertEqual(self.main("latest"), 0)
        self.assertEqual(self.main("notes", "v0.1.1"), 0)
        self.assertEqual(self.main("notes", "v3.0.0"), 1)
        self.assertEqual(self.main("check"), 0)
        self.assertEqual(self.main(), 2)
        self.assertEqual(self.main("latest", "v0.1.0"), 2)

    def test_check_fails_on_a_bad_file(self):
        self.changelog.write_text("## [v0.1.0]\n", encoding="utf-8")
        self.assertEqual(self.main("check"), 1)

    def test_cli_requires_consumer_root_and_never_uses_cwd_or_source(self):
        with tempfile.TemporaryDirectory() as unrelated:
            (Path(unrelated) / "CHANGELOG.md").write_text("invalid cwd changelog")
            script = Path(__file__).resolve().parents[1] / "scripts/changelog.py"
            for args, code, output in [( ["latest"], 2, ""),
                                       (["--root", "", "latest"], 2, ""),
                                       (["--root", ".", "latest"], 2, ""),
                                       (["--root", str(self.root), "latest"], 0, "v0.2.0\n"),
                                       (["--root", str(self.root), "notes", "v0.1.1"], 0, "- Исправлено.\n"),
                                       (["--root", str(self.root), "check"], 0, "")]:
                with self.subTest(args=args):
                    result = subprocess.run([sys.executable, "-B", str(script), *args], cwd=unrelated,
                                            capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, code, result.stderr)
                    self.assertEqual(result.stdout, output)


if __name__ == "__main__":
    unittest.main()
