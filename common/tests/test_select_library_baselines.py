"""Check selection of the published base and preceding stable release."""

import unittest

from common.scripts.select_library_baselines import library_baselines


def tag(name, sha="a" * 40):
    return {"name": name, "commit": {"sha": sha}}


class LibraryBaselineTests(unittest.TestCase):
    def test_semver_order_ignores_prerelease_tags_and_tag_creation_order(self):
        first = [tag("v0.3.5"), tag("v1.1.0-rc1"), tag("v0.10.0")]
        second = [tag("v1.0.0", "b" * 40), tag("test-tag"), tag("v0.9.1"), tag("v0.10.0")]
        self.assertEqual(library_baselines([first, second]), [("v1.0.0", "b" * 40), ("v0.10.0", "a" * 40)])
        self.assertEqual(library_baselines([[]]), [])

    def test_rejects_moving_or_unpinned_tag(self):
        with self.assertRaisesRegex(ValueError, "changed"):
            library_baselines([[tag("v0.1.0")], [tag("v0.1.0", "b" * 40)]])
        with self.assertRaisesRegex(ValueError, "immutable"):
            library_baselines([[tag("v0.1.0", "main")]])
