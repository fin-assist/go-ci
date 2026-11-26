"""Verify that the organization mode only publishes to a common audience."""

import copy
import subprocess
import unittest
from unittest.mock import patch

from common.scripts import verify_private_artifact_audience as audit


DESTINATION = "fin-assist/consumer"
DEPENDENCY = "fin-assist/dependency"
APPS = "orgs/fin-assist/installations?per_page=100"
REPOS = "orgs/fin-assist/repos?type=all&per_page=100"


class PrivateArtifactAudienceTests(unittest.TestCase):
    def setUp(self):
        self.app = {"suspended_at": None, "repository_selection": "all",
                    "permissions": {"actions": "read", "contents": "read"}}
        self.responses = {
            APPS: [{"total_count": 1, "installations": [self.app]}],
            REPOS: [[{"full_name": DESTINATION, "visibility": "private"},
                     {"full_name": "fin-assist/public", "visibility": "public"}],
                    [{"full_name": DEPENDENCY, "visibility": "private"}]],
            self.collaborators(DESTINATION): [[{"id": 12, "permissions": {"pull": True}}]],
            self.collaborators(DEPENDENCY): [[{"id": 12, "permissions": {"pull": True}}],
                                             [{"id": 34, "permissions": {"pull": True}}]],
        }

    def collaborators(self, repository):
        return f"repos/{repository}/collaborators?affiliation=all&per_page=100"

    def test_organization_app_and_all_destination_users_can_read_private_dependencies(self):
        storage_reader = {"suspended_at": None, "repository_selection": "selected", "permissions": {"contents": "read"}}
        self.responses[APPS] = [{"total_count": 2, "installations": [self.app]},
                                {"total_count": 2, "installations": [storage_reader]}]
        audit.verify(DESTINATION, self.responses.__getitem__)

    def test_rejects_artifact_reader_without_matching_module_access(self):
        self.responses[self.collaborators(DESTINATION)].append([{"id": 56, "permissions": {"pull": True}}])
        with self.assertRaisesRegex(ValueError, "an artifact reader"):
            audit.verify(DESTINATION, self.responses.__getitem__)

    def test_rejects_actions_apps_without_organization_wide_contents_access(self):
        restricted_app = self.app | {"repository_selection": "selected"}
        missing_contents = self.app | {"permissions": {"actions": "write"}}
        for app in (restricted_app, missing_contents):
            with self.subTest(app=app):
                self.responses[APPS] = [{"total_count": 1, "installations": [app]}]
                with self.assertRaisesRegex(ValueError, "an Actions reader"):
                    audit.verify(DESTINATION, self.responses.__getitem__)

    def test_rejects_nonprivate_or_unverified_destination(self):
        inventory = copy.deepcopy(self.responses[REPOS])
        for visibility in ("public", "internal", None):
            with self.subTest(visibility=visibility):
                inventory[0][0]["visibility"] = visibility
                self.responses[REPOS] = inventory
                with self.assertRaisesRegex(ValueError, "destination must be private"):
                    audit.verify(DESTINATION, self.responses.__getitem__)

    def test_rejects_an_incomplete_app_inventory(self):
        self.responses[APPS][0]["total_count"] = 2
        with self.assertRaisesRegex(ValueError, "inventory is incomplete"):
            audit.verify(DESTINATION, self.responses.__getitem__)

    def test_controlled_failure_when_api_data_cannot_be_read(self):
        with patch.object(audit, "verify", side_effect=subprocess.CalledProcessError(1, ["gh", "api"])), \
             patch("sys.argv", ["audit", DESTINATION]), patch("sys.stderr"):
            self.assertEqual(audit.main(), 1)
