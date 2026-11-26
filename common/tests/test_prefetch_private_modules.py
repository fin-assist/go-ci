"""Verify the private proxy contents and the credential boundary."""

import base64
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile


spec = importlib.util.spec_from_file_location(
    "prefetch_private_modules", Path(__file__).resolve().parents[1] / "scripts/prefetch_private_modules.py",
)
prefetch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prefetch)

MODULE = "github.com/fin-assist/contracts/gen/go"
VERSION = "v0.11.0"
OLD_VERSION = "v0.0.0-20260102030405-0123456789ab"
MOD_SUM = "h1:" + base64.b64encode(b"m" * 32).decode()
ZIP_SUM = "h1:" + base64.b64encode(b"z" * 32).decode()
LOCKS = f"{MODULE} {VERSION} {ZIP_SUM}\n{MODULE} {VERSION}/go.mod {MOD_SUM}\n"
REQUIRE = {"Require": [{"Path": MODULE, "Version": VERSION}]}


class PrivateModuleLocksTest(unittest.TestCase):
    def test_selects_locked_private_modules_and_historical_mod_metadata(self):
        result = prefetch.read_locks(
            REQUIRE, LOCKS + f"{MODULE} {OLD_VERSION}/go.mod {MOD_SUM}\nexample.com/public v1.0.0 {ZIP_SUM}\n",
        )
        self.assertEqual(result, {
            (MODULE, VERSION): {"mod": MOD_SUM, "zip": ZIP_SUM},
            (MODULE, OLD_VERSION): {"mod": MOD_SUM},
        })

    def test_rejects_invalid_private_paths_versions_and_hashes(self):
        for module, version, checksum in [
            ("GitHub.com/fin-assist/repo", VERSION, MOD_SUM),
            ("github.com/fin-assist/../elsewhere", VERSION, MOD_SUM),
            ("github.com/fin-assist/repo%2felsewhere", VERSION, MOD_SUM),
            (MODULE, "main", MOD_SUM),
            (MODULE, "v0.1.0/../../elsewhere", MOD_SUM),
            (MODULE, VERSION, "h1:short"),
        ]:
            with self.subTest(module=module, version=version, checksum=checksum), self.assertRaises(ValueError):
                prefetch.read_locks({}, f"{module} {version}/go.mod {checksum}\n")

    def test_rejects_missing_and_conflicting_checksums(self):
        for locks in ("", LOCKS.splitlines()[0], LOCKS.splitlines()[1], LOCKS + f"{MODULE} {VERSION} {MOD_SUM}\n"):
            with self.subTest(locks=locks), self.assertRaises(ValueError):
                prefetch.read_locks(REQUIRE, locks)

    def test_rejects_replacements_to_or_from_a_private_module(self):
        for old, new in ((MODULE, "./local"), ("example.com/public", MODULE)):
            go_mod = {"Replace": [{"Old": {"Path": old}, "New": {"Path": new}}]}
            with self.subTest(old=old, new=new), self.assertRaisesRegex(ValueError, "replacements"):
                prefetch.read_locks(go_mod, "")


class PrivateModulePrefetchTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.go_mod = self.root / "go.mod"
        self.go_mod.write_text(f"module example.com/app\n\ngo 1.26.0\n\nrequire {MODULE} {VERSION}\n")
        self.go_sum = self.root / "go.sum"
        self.go_sum.write_text(LOCKS)
        self.output = self.root / "proxy"
        self.downloads = []

    def fake_download(self, command, *, cwd, env, **options):
        self.assertEqual(command[:4], ["go", "mod", "download", "-json"])
        self.assertFalse((cwd / "go.mod").exists())
        self.assertNotIn("GO_MODULES_TOKEN", env)
        self.assertEqual(env["GOENV"], "off")
        self.assertEqual(env["GOWORK"], "off")
        self.assertEqual(env["GOTOOLCHAIN"], "local")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.https://github.com/fin-assist/.extraHeader")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "Authorization: Basic eC1hY2Nlc3MtdG9rZW46dGVzdC10b2tlbg==")
        self.assertEqual(env["GIT_CONFIG_KEY_1"], "http.followRedirects")
        self.assertEqual(env["GIT_CONFIG_VALUE_1"], "false")
        self.assertEqual(options["timeout"], prefetch.DOWNLOAD_TIMEOUT)

        module, version = command[4].split("@")
        self.downloads.append((module, version))
        cached = Path(env["GOMODCACHE"]) / "cache" / "download" / module / "@v"
        cached.mkdir(parents=True, exist_ok=True)
        info = {"Version": version, "Time": "2026-01-02T03:04:05Z", "Origin": "excluded"}
        (cached / f"{version}.info").write_text(json.dumps(info))
        (cached / f"{version}.mod").write_text(f"module {module}\n")
        (cached / f"{version}.zip").write_bytes(b"fixture archive")
        response = {"Path": module, "Version": version, "GoModSum": MOD_SUM, "Sum": ZIP_SUM}
        return subprocess.CompletedProcess(command, 0, json.dumps(response).encode())

    def test_fetch_uses_isolated_go_and_publishes_only_locked_files(self):
        self.go_sum.write_text(LOCKS + f"{MODULE} {OLD_VERSION}/go.mod {MOD_SUM}\n")
        real_run = subprocess.run

        def run(command, **options):
            if command == ["go", "telemetry", "off"] or command[:4] == ["go", "mod", "edit", "-json"]:
                self.assertNotIn("GIT_CONFIG_VALUE_0", options["env"])
                self.assertNotIn("GO_MODULES_TOKEN", options["env"])
                return real_run(command, **options)
            return self.fake_download(command, **options)

        with patch.object(subprocess, "run", run):
            prefetch.prefetch(self.go_mod, self.go_sum, self.output, "test-token", allowed_repositories={"contracts"})

        package = self.output / MODULE / "@v"
        self.assertCountEqual(self.downloads, [(MODULE, OLD_VERSION), (MODULE, VERSION)])
        self.assertEqual({path.name for path in package.iterdir()}, {
            f"{VERSION}.info", f"{VERSION}.mod", f"{VERSION}.zip", f"{VERSION}.ziphash",
            f"{OLD_VERSION}.info", f"{OLD_VERSION}.mod",
        })
        self.assertEqual((package / f"{VERSION}.ziphash").read_text(), ZIP_SUM)
        self.assertEqual(json.loads((package / f"{VERSION}.info").read_text()), {
            "Version": VERSION, "Time": "2026-01-02T03:04:05Z",
        })

    def test_organization_policy_follows_locks_without_a_repository_list(self):
        new_module = "github.com/fin-assist/new-library"
        foreign_module = "github.com/another-owner/foreign-library"
        self.go_mod.write_text(self.go_mod.read_text().replace(MODULE, new_module))
        self.go_sum.write_text(LOCKS.replace(MODULE, new_module) + LOCKS.replace(MODULE, foreign_module))
        real_run = subprocess.run

        def run(command, **options):
            if command == ["go", "telemetry", "off"] or command[:4] == ["go", "mod", "edit", "-json"]:
                return real_run(command, **options)
            return self.fake_download(command, **options)

        with patch.object(subprocess, "run", run):
            prefetch.prefetch(self.go_mod, self.go_sum, self.output, "test-token", allowed_repositories=None)

        self.assertEqual(self.downloads, [(new_module, VERSION)])
        self.assertTrue((self.output / new_module / "@v" / f"{VERSION}.zip").is_file())
        self.assertFalse((self.output / foreign_module).exists())

    def test_combines_historical_locks_and_accepts_missing_sum_without_private_requires(self):
        old_mod = self.root / "tag.mod"
        old_sum = self.root / "tag.sum"
        old_mod.write_text(f"module example.com/app\n\ngo 1.26.0\n\nrequire {MODULE} {OLD_VERSION}\n")
        old_sum.write_text(LOCKS.replace(VERSION, OLD_VERSION))
        blank_mod = self.root / "blank.mod"
        blank_mod.write_text("module example.com/blank\n\ngo 1.26.0\n")
        real_run = subprocess.run

        def run(command, **options):
            if command == ["go", "telemetry", "off"] or command[:4] == ["go", "mod", "edit", "-json"]:
                return real_run(command, **options)
            return self.fake_download(command, **options)

        with patch.object(subprocess, "run", run):
            prefetch.prefetch(self.go_mod, self.go_sum, self.output, "test-token", allowed_repositories={"contracts"},
                              baselines=((old_mod, old_sum), (blank_mod, self.root / "missing.sum")))
        self.assertCountEqual(self.downloads, [(MODULE, OLD_VERSION), (MODULE, VERSION)])
        self.assertTrue((self.output / MODULE / "@v" / f"{OLD_VERSION}.ziphash").is_file())

    def test_version_snapshot_preserves_locked_modfile_and_adds_no_archives(self):
        candidate = "v0.12.0-RC1"
        versions = [f"v0.1.{patch_number}" for patch_number in range(70)] + [VERSION, candidate]
        env = {"GOMODCACHE": str(self.root / "cache")}
        cached = self.root / "cache" / "cache/download" / MODULE / "@v"
        target = self.output / MODULE / "@v"
        cached.mkdir(parents=True)
        target.mkdir(parents=True)
        (target / f"{VERSION}.mod").write_text("verified modfile")
        for version in (VERSION, candidate):
            filename = prefetch.escape_version(version)
            (cached / f"{filename}.info").write_text(json.dumps({
                "Version": version, "Time": "2026-01-02T03:04:05Z", "Origin": "private origin detail",
            }))
            (cached / f"{filename}.mod").write_text("metadata modfile")

        metadata = {"Path": MODULE, "Version": candidate, "Versions": versions}
        with patch.object(prefetch, "go_json", side_effect=[metadata, {"Path": MODULE, "Version": VERSION}]) as go:
            prefetch.snapshot_metadata(MODULE, self.root, env, self.output)
        self.assertEqual(go.call_count, 2)
        self.assertEqual((target / "list").read_text().splitlines(), versions)
        self.assertEqual((target / f"{VERSION}.mod").read_text(), "verified modfile")
        self.assertEqual((target / "v0.12.0-!r!c1.mod").read_text(), "metadata modfile")
        self.assertEqual(json.loads((target.parent / "@latest").read_text()), {
            "Version": candidate, "Time": "2026-01-02T03:04:05Z",
        })
        self.assertFalse(list(target.glob("*.zip")))

        no_update = {"Path": MODULE, "Version": "latest",
                     "Error": {"Err": f'module {MODULE}: no matching versions for query "latest"'}}
        with patch.object(prefetch, "go_json", side_effect=[metadata, no_update]):
            prefetch.snapshot_metadata(MODULE, self.root, env, self.output)
        self.assertEqual((target / "list").read_text().splitlines(), versions)
        failed_lookup = {"Path": MODULE, "Error": {"Err": "connection refused"}}
        with patch.object(prefetch, "go_json", side_effect=[metadata, failed_lookup]):
            with self.assertRaisesRegex(ValueError, "unexpected latest version metadata"):
                prefetch.snapshot_metadata(MODULE, self.root, env, self.output)

    def test_follows_verified_archives_and_checks_allowlist_before_each_download(self):
        middle = 'github.com/fin-assist/go-grpc'
        leaf = 'github.com/fin-assist/go-migrate'
        downloads = []

        def verified_download(module, version, hashes, work, env, proxy):
            downloads.append((module, set(hashes)))
            target = proxy / module / '@v'
            target.mkdir(parents=True, exist_ok=True)
            dependencies = {MODULE: middle, middle: leaf}
            required = dependencies.get(module)
            mod = f'module {module}\n\ngo 1.26.0\n'
            if required:
                mod += f'require {required} {VERSION}\n'
            (target / f'{version}.mod').write_text(mod)
            if 'zip' in hashes:
                with ZipFile(target / f'{version}.zip', 'w') as archive:
                    if required:
                        lock = f'{required} {VERSION}/go.mod {MOD_SUM}\n'
                        if required == middle:
                            lock += f'{required} {VERSION} {ZIP_SUM}\n'
                        archive.writestr(f'{module}@{version}/go.sum', lock)

        for allowed in ({'contracts', 'go-grpc'}, {'contracts', 'go-grpc', 'go-migrate'}, None):
            with self.subTest(allowed=allowed), tempfile.TemporaryDirectory(dir=self.root) as work_dir:
                downloads.clear()
                work = Path(work_dir)
                proxy = work / 'proxy'
                env = prefetch.go_environment(work)
                locks = {(MODULE, VERSION): {'mod': MOD_SUM, 'zip': ZIP_SUM}}
                with patch.object(prefetch, 'download_module', side_effect=verified_download):
                    if allowed is not None and 'go-migrate' not in allowed:
                        with self.assertRaisesRegex(ValueError, r'from .*go-grpc@.*reviewed access boundary: go-migrate'):
                            prefetch.download_locked_graph(locks, work, env, env, proxy, allowed, transitive=True)
                    else:
                        prefetch.download_locked_graph(locks, work, env, env, proxy, allowed, transitive=True)
                expected = [(MODULE, {'mod', 'zip'}), (middle, {'mod', 'zip'})]
                if allowed is None or 'go-migrate' in allowed:
                    expected.append((leaf, {'mod'}))
                self.assertEqual(downloads, expected)

    def test_merge_locks_requeues_a_version_when_zip_checksum_arrives(self):
        ref = (MODULE, VERSION)
        locks = {ref: {'mod': MOD_SUM}}
        complete = {ref: {'mod': MOD_SUM, 'zip': ZIP_SUM}}
        self.assertEqual(prefetch.merge_locks(locks, complete), [ref])
        self.assertEqual(locks, complete)
        self.assertEqual(prefetch.merge_locks(locks, complete), [])
        with self.assertRaisesRegex(ValueError, 'conflicting release checksums'):
            prefetch.merge_locks(locks, {ref: {'mod': ZIP_SUM}})

    def test_graph_downloads_zip_after_mod_only_download(self):
        leaf = ('github.com/fin-assist/go-migrate', VERSION)
        parent = ('github.com/fin-assist/go-outbox', VERSION)
        mod_only = {'mod': MOD_SUM}
        full = {'mod': MOD_SUM, 'zip': ZIP_SUM}
        graph = {MODULE: {leaf: mod_only, parent: full}, parent[0]: {leaf: full}, leaf[0]: {}}
        locks = {(MODULE, VERSION): full.copy()}
        downloads = []

        def downloaded(module, version, hashes, *_):
            downloads.append((module, version, hashes.copy()))

        with patch.object(prefetch, 'download_module', side_effect=downloaded), \
             patch.object(prefetch, 'archived_requirements', side_effect=lambda module, *_: graph[module]):
            prefetch.download_locked_graph(locks, self.root, {}, {}, self.output,
                                           {'contracts', 'go-migrate', 'go-outbox'}, transitive=True)

        self.assertEqual(downloads, [(MODULE, VERSION, full), (*leaf, mod_only),
                                     (*parent, full), (*leaf, full)])

    def test_archive_requires_valid_lockfile_but_ignores_dependency_replace(self):
        source = self.output / MODULE / '@v'
        source.mkdir(parents=True)
        required = 'github.com/fin-assist/go-migrate'
        (source / f'{VERSION}.mod').write_text(
            f'module {MODULE}\n\ngo 1.26.0\n\nrequire {required} {VERSION}\nreplace {required} => ./local\n')
        archive_path = source / f'{VERSION}.zip'
        env = prefetch.go_environment(self.root)

        def requirements():
            return prefetch.archived_requirements(MODULE, VERSION, self.root, env, self.output)

        archive_path.write_text('invalid archive')
        with self.assertRaisesRegex(ValueError, 'could not read the verified module archive'):
            requirements()
        with ZipFile(archive_path, 'w'):
            pass
        with self.assertRaisesRegex(ValueError, 'missing required module checksums'):
            requirements()
        with ZipFile(archive_path, 'w') as archive:
            archive.writestr(f'{MODULE}@{VERSION}/go.sum', f'{required} {VERSION}/go.mod {MOD_SUM}\n')
        with patch.object(prefetch, 'INPUT_LIMIT', 32), self.assertRaisesRegex(ValueError, 'exceeds the size limit'):
            requirements()
        self.assertEqual(requirements(), {(required, VERSION): {'mod': MOD_SUM}})

    def test_checksum_failure_keeps_proxy_unpublished(self):
        self.go_sum.write_text(LOCKS.replace(ZIP_SUM, MOD_SUM))
        real_run = subprocess.run

        def run(command, **options):
            if command == ["go", "telemetry", "off"] or command[:4] == ["go", "mod", "edit", "-json"]:
                return real_run(command, **options)
            return self.fake_download(command, **options)

        with patch.object(subprocess, "run", run), self.assertRaisesRegex(ValueError, "checksum mismatch"):
            prefetch.prefetch(self.go_mod, self.go_sum, self.output, "test-token", allowed_repositories={"contracts"})
        self.assertFalse(self.output.exists())

    def test_unreviewed_module_is_rejected_before_cross_repo_fetch(self):
        for allowed in (set(), {"other"}):
            with self.subTest(allowed=allowed), patch.object(prefetch, "download_module") as download:
                with self.assertRaisesRegex(ValueError, "outside the reviewed"):
                    prefetch.prefetch(self.go_mod, self.go_sum, self.output, "test-token", allowed_repositories=allowed)
                download.assert_not_called()
                self.assertFalse(self.output.exists())

    def test_git_error_does_not_reveal_credentials(self):
        failure = subprocess.CalledProcessError(1, ["go"], stderr=b"test-token")
        with patch.object(subprocess, "run", side_effect=failure), self.assertRaisesRegex(ValueError, r"^could not fetch$"):
            prefetch.go_json(["mod", "download", "-json"], self.root, {}, "fetch")


if __name__ == "__main__":
    unittest.main()
