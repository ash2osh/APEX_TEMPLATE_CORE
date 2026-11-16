"""Tests for sync_core.py. Run from the core root:

    python3 -m unittest discover -s tests -v

Every test builds temporary git repositories; no real template is read or written.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sync_core as sc  # noqa: E402

POSIX = os.name == "posix"
GIT_OPTIONS = [
    "-c",
    "user.name=core-test",
    "-c",
    "user.email=core-test@example.invalid",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.hooksPath=/nonexistent-hooks",
]
SHARED = {
    "scripts/alpha.py": b"print('alpha')\n",
    "scripts/run.sh": b"#!/bin/sh\necho run\n",
    "tools/beta.py": b"BETA = 1\n",
    "tests/test_gamma.py": b"def test_gamma():\n    assert True\n",
}
EXECUTABLE = frozenset({"scripts/run.sh"})


class Link(str):
    """Fixture marker: create a symbolic link whose target is this text."""


@functools.cache
def symlinks_supported() -> bool:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        try:
            os.symlink(tmp, Path(tmp) / "probe")
        except (OSError, NotImplementedError):
            return False
    return True


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *GIT_OPTIONS, *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {repo}: {proc.stderr}")
    return proc.stdout


def write_tree(root: Path, files: dict[str, bytes | Link], executable: frozenset[str] = frozenset()) -> None:
    for rel, content in files.items():
        target = root.joinpath(*rel.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, Link):
            os.symlink(str(content), target)
            continue
        target.write_bytes(content)
        if POSIX and rel in executable:
            os.chmod(target, 0o755)


def commit_all(repo: Path, message: str = "update") -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def make_repo(path: Path, files: dict[str, bytes | Link], executable: frozenset[str] = frozenset()) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    write_tree(path, files, executable)
    git(path, "add", "-A")
    # Git on Windows does not record a chmod, so set the executable mode in the index directly.
    for rel in sorted(executable):
        if rel in files and not isinstance(files[rel], Link):
            git(path, "update-index", "--chmod=+x", "--", rel)
    commit_all(path, "initial")
    return path


def make_templates(base: Path) -> tuple[Path, Path]:
    first: dict[str, bytes | Link] = {**SHARED, "scripts/gamma.py": b"ONE\n", "scripts/only_one.py": b"one\n", "scripts/skip_me.py": b"skip\n", "docs/readme.md": b"same\n"}
    second: dict[str, bytes | Link] = {**SHARED, "scripts/gamma.py": b"TWO\n", "tests/only_two.py": b"two\n", "scripts/skip_me.py": b"skip\n", "docs/readme.md": b"same\n"}
    if symlinks_supported():
        first["scripts/link.py"] = Link("alpha.py")
        second["scripts/link.py"] = Link("alpha.py")
    return make_repo(base / "t1", first, EXECUTABLE), make_repo(base / "t2", second, EXECUTABLE)


def bare_core(base: Path) -> Path:
    core = base / "core"
    core.mkdir()
    shutil.copy2(ROOT / "sync_core.py", core / "sync_core.py")
    git(core, "init", "-q", "-b", "main")
    return core


def seed_argv(first: Path, second: Path, core: Path) -> list[str]:
    return [
        "seed",
        "--core",
        str(core),
        "--template",
        f"26.1={first}@main",
        "--template",
        f"26.2={second}@main",
        "--prefix",
        "scripts/",
        "--prefix",
        "tools/",
        "--prefix",
        "tests/",
        "--exclude",
        "scripts/skip_me.py",
    ]


def make_core(base: Path, first: Path, second: Path) -> Path:
    core = bare_core(base)
    code, _, err = run_cli(*seed_argv(first, second, core))
    if code != 0:
        raise AssertionError(f"seed failed: {err}")
    commit_all(core, "seed")
    return core


def core_set_file(core: Path, rel: str, data: bytes | None, executable: bool = False) -> None:
    """Change one shared file in the core as seed would, then commit. data None removes it."""
    target = core / "files" / rel
    if data is None:
        target.unlink()
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if POSIX:
            os.chmod(target, 0o755 if executable else 0o644)
    manifest_path = core / "core-manifest.json"
    manifest = load_manifest(core)
    entries = [e for e in manifest["entries"] if e["path"] != rel]
    if data is not None:
        entries.append({"path": rel, "sha256": hashlib.sha256(data).hexdigest(), "executable": executable})
    manifest["entries"] = sorted(entries, key=lambda e: e["path"].encode("utf-8"))
    manifest_path.write_bytes((json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
    commit_all(core, f"change {rel}")


def load_manifest(core: Path) -> dict:
    return json.loads((core / "core-manifest.json").read_text(encoding="utf-8"))


def load_stamp(target: Path) -> dict:
    return json.loads((target / ".core-stamp.json").read_text(encoding="utf-8"))


def write_stamp(target: Path, stamp: dict) -> None:
    (target / ".core-stamp.json").write_bytes((json.dumps(stamp, indent=2) + "\n").encode("utf-8"))


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = sc.main(list(argv))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return code, out.getvalue(), err.getvalue()


def sync_cli(target: Path, core: Path) -> tuple[int, str, str]:
    return run_cli("sync", str(target), "--core", str(core))


def check_cli(target: Path, core: Path | None = None) -> tuple[int, str, str]:
    return run_cli("check", str(target), *(["--core", str(core)] if core is not None else []))


def diff_cli(target: Path, core: Path) -> tuple[int, str, str]:
    return run_cli("diff", str(target), "--core", str(core))


class TempBase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)


class PureFunctionTests(unittest.TestCase):
    def test_check_relpath_accepts_plain_relative_paths(self) -> None:
        for rel in ("scripts/a.py", "tools/workflow-skills/catalog.json", "tests/fixtures/x.sql"):
            self.assertEqual(sc.check_relpath(rel), rel)

    def test_check_relpath_rejects_unsafe_paths(self) -> None:
        for rel in ("", "/etc/passwd", "../escape.py", "scripts/../../x", "scripts\\a.py", "scripts/.git/config", "scripts/a:b.py", "scripts/trailing. ", "scripts//a.py", "a\x00b"):
            with self.subTest(rel=rel), self.assertRaises(sc.UnsafePath):
                sc.check_relpath(rel)

    def test_parse_template_spec_splits_label_path_and_ref(self) -> None:
        spec = sc.parse_template_spec("26.1=/home/x/repo@main")
        self.assertEqual((spec.label, spec.path, spec.ref), ("26.1", Path("/home/x/repo"), "main"))
        spec = sc.parse_template_spec("26.2=/a@b/repo@origin/main")
        self.assertEqual((spec.path, spec.ref), (Path("/a@b/repo"), "origin/main"))

    def test_parse_template_spec_rejects_malformed_values(self) -> None:
        for text in ("26.1", "=/x@main", "26.1=/x", "26.1=@main", "bad label=/x@main", "26.1=/x@-x"):
            with self.subTest(text=text), self.assertRaises(sc.UsageError):
                sc.parse_template_spec(text)

    def test_normalize_exclude_keeps_directory_rules(self) -> None:
        self.assertEqual(sc.normalize_exclude("./scripts/ci_run_tests.py"), "scripts/ci_run_tests.py")
        self.assertEqual(sc.normalize_exclude("tests/fixtures/"), "tests/fixtures/")
        self.assertTrue(sc.rule_matches("tests/fixtures/a.json", "tests/fixtures/"))
        self.assertFalse(sc.rule_matches("tests/fixtures_extra.json", "tests/fixtures/"))

    def test_unknown_command_is_a_usage_error(self) -> None:
        code, _, _ = run_cli("bogus")
        self.assertEqual(code, 2)


class SeedTests(TempBase):
    def setUp(self) -> None:
        super().setUp()
        self.first, self.second = make_templates(self.base)

    def test_selects_identical_regular_files_and_reports_counts(self) -> None:
        core = bare_core(self.base)
        code, out, err = run_cli(*seed_argv(self.first, self.second, core))
        self.assertEqual(code, 0, err)
        paths = [e["path"] for e in load_manifest(core)["entries"]]
        self.assertEqual(paths, ["scripts/alpha.py", "scripts/run.sh", "scripts/sync_core.py", "tests/test_gamma.py", "tools/beta.py"])
        self.assertIn("identical: 4 selected, 1 excluded by --exclude", out)
        self.assertIn("differing: 1 (not in core; Phase 2)", out)
        self.assertIn("one-sided: 2 (26.1 only 1, 26.2 only 1)", out)
        self.assertIn("excluded: scripts/skip_me.py", out)
        if symlinks_supported():
            self.assertIn("1 symlink or submodule skipped", out)
            self.assertNotIn("scripts/link.py", paths)

    def test_uses_committed_bytes_not_the_working_tree(self) -> None:
        core = bare_core(self.base)
        (self.first / "scripts" / "alpha.py").write_bytes(b"uncommitted edit\n")
        code, _, err = run_cli(*seed_argv(self.first, self.second, core))
        self.assertEqual(code, 0, err)
        self.assertEqual((core / "files" / "scripts" / "alpha.py").read_bytes(), SHARED["scripts/alpha.py"])

    def test_records_and_applies_the_executable_bit(self) -> None:
        core = make_core(self.base, self.first, self.second)
        by_path = {e["path"]: e for e in load_manifest(core)["entries"]}
        self.assertTrue(by_path["scripts/run.sh"]["executable"])
        self.assertFalse(by_path["scripts/alpha.py"]["executable"])
        if POSIX:
            self.assertTrue((core / "files" / "scripts" / "run.sh").stat().st_mode & 0o111)
            self.assertFalse((core / "files" / "scripts" / "alpha.py").stat().st_mode & 0o111)

    def test_records_source_labels_refs_and_commits(self) -> None:
        core = make_core(self.base, self.first, self.second)
        sources = load_manifest(core)["sources"]
        self.assertEqual([s["label"] for s in sources], ["26.1", "26.2"])
        self.assertEqual(sources[0]["commit"], git(self.first, "rev-parse", "main").strip())
        self.assertEqual(sources[1]["commit"], git(self.second, "rev-parse", "main").strip())

    def test_core_owned_checker_copy_matches_the_root_script(self) -> None:
        core = make_core(self.base, self.first, self.second)
        self.assertEqual((core / "files" / "scripts" / "sync_core.py").read_bytes(), (ROOT / "sync_core.py").read_bytes())
        entry = next(e for e in load_manifest(core)["entries"] if e["path"] == "scripts/sync_core.py")
        self.assertTrue(entry["executable"])

    def test_rebuilds_files_dir_and_drops_stale_files(self) -> None:
        core = make_core(self.base, self.first, self.second)
        (core / "files" / "stale.txt").write_text("old\n", encoding="utf-8")
        code, _, err = run_cli(*seed_argv(self.first, self.second, core))
        self.assertEqual(code, 0, err)
        self.assertFalse((core / "files" / "stale.txt").exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "case-only names need a case-sensitive filesystem")
    def test_refuses_names_that_differ_only_by_case(self) -> None:
        clash = {"scripts/Alpha.py": b"x\n", "scripts/alpha.py": b"x\n"}
        first = make_repo(self.base / "c1", clash)
        second = make_repo(self.base / "c2", clash)
        code, _, err = run_cli(*seed_argv(first, second, bare_core(self.base)))
        self.assertEqual(code, 2)
        self.assertIn("differ only by case", err)

    def test_needs_exactly_two_templates(self) -> None:
        code, _, err = run_cli("seed", "--core", str(bare_core(self.base)), "--template", f"26.1={self.first}@main")
        self.assertEqual(code, 2)
        self.assertIn("exactly two", err)

    def test_unknown_ref_is_a_usage_error(self) -> None:
        argv = seed_argv(self.first, self.second, bare_core(self.base))
        argv[argv.index(f"26.2={self.second}@main")] = f"26.2={self.second}@no-such-branch"
        code, _, err = run_cli(*argv)
        self.assertEqual(code, 2)
        self.assertIn("does not resolve", err)


class SyncTests(TempBase):
    def setUp(self) -> None:
        super().setUp()
        self.first, self.second = make_templates(self.base)
        self.core = make_core(self.base, self.first, self.second)
        self.target = make_repo(self.base / "target", {"README.md": b"target\n"})

    def test_writes_shared_files_and_stamp(self) -> None:
        code, _, err = sync_cli(self.target, self.core)
        self.assertEqual(code, 0, err)
        manifest = load_manifest(self.core)
        for entry in manifest["entries"]:
            written = self.target.joinpath(*entry["path"].split("/"))
            self.assertEqual(written.read_bytes(), (self.core / "files" / entry["path"]).read_bytes(), entry["path"])
            if POSIX:
                self.assertEqual(bool(written.stat().st_mode & 0o111), entry["executable"], entry["path"])
        stamp = load_stamp(self.target)
        self.assertEqual(stamp["coreCommit"], git(self.core, "rev-parse", "HEAD").strip())
        self.assertEqual(stamp["files"], [{"path": e["path"], "sha256": e["sha256"], "executable": e["executable"]} for e in manifest["entries"]])

    def test_second_sync_after_commit_changes_nothing(self) -> None:
        self.assertEqual(sync_cli(self.target, self.core)[0], 0)
        commit_all(self.target, "sync")
        alpha = self.target / "scripts" / "alpha.py"
        before = alpha.stat().st_mtime_ns
        stamp_before = (self.target / ".core-stamp.json").read_bytes()
        code, out, err = sync_cli(self.target, self.core)
        self.assertEqual(code, 0, err)
        self.assertEqual(alpha.stat().st_mtime_ns, before)
        self.assertEqual((self.target / ".core-stamp.json").read_bytes(), stamp_before)
        self.assertIn("0 written", out)

    def test_refuses_uncommitted_changes_and_writes_nothing(self) -> None:
        target = make_repo(self.base / "dirty", {"README.md": b"x\n", "scripts/alpha.py": b"local\n"})
        (target / "scripts" / "alpha.py").write_bytes(b"local edit\n")
        code, _, err = sync_cli(target, self.core)
        self.assertEqual(code, 1)
        self.assertIn("scripts/alpha.py", err)
        self.assertFalse((target / "tools" / "beta.py").exists())
        self.assertFalse((target / ".core-stamp.json").exists())
        self.assertEqual((target / "scripts" / "alpha.py").read_bytes(), b"local edit\n")

    def test_refuses_untracked_target_file(self) -> None:
        target = make_repo(self.base / "untracked", {"README.md": b"x\n"})
        (target / "tools").mkdir()
        (target / "tools" / "beta.py").write_bytes(b"mine\n")
        code, _, err = sync_cli(target, self.core)
        self.assertEqual(code, 1)
        self.assertIn("tools/beta.py", err)
        self.assertFalse((target / "scripts" / "alpha.py").exists())
        self.assertEqual((target / "tools" / "beta.py").read_bytes(), b"mine\n")

    @unittest.skipUnless(symlinks_supported(), "symbolic links not available")
    def test_refuses_a_symlinked_target_file_and_leaves_its_destination_alone(self) -> None:
        outside = self.base / "outside.txt"
        outside.write_bytes(b"keep\n")
        target = make_repo(self.base / "linked", {"README.md": b"x\n", "scripts/alpha.py": Link(str(outside))})
        code, _, err = sync_cli(target, self.core)
        self.assertEqual(code, 1)
        self.assertIn("symbolic link", err)
        self.assertEqual(outside.read_bytes(), b"keep\n")
        self.assertFalse((target / ".core-stamp.json").exists())

    @unittest.skipUnless(symlinks_supported(), "symbolic links not available")
    def test_refuses_a_symlinked_directory_on_the_path(self) -> None:
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        target = make_repo(self.base / "linkdir", {"README.md": b"x\n", "tools": Link(str(elsewhere))})
        code, _, err = sync_cli(target, self.core)
        self.assertEqual(code, 1)
        self.assertIn("tools", err)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_refuses_a_manifest_path_that_escapes_the_template(self) -> None:
        manifest = load_manifest(self.core)
        manifest["entries"].append({"path": "../escape.py", "sha256": "0" * 64, "executable": False})
        (self.core / "core-manifest.json").write_bytes((json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
        commit_all(self.core, "bad manifest entry")
        code, _, err = sync_cli(self.target, self.core)
        self.assertEqual(code, 1)
        self.assertIn("not allowed", err)
        self.assertFalse((self.base / "escape.py").exists())

    def test_refuses_a_dirty_core_and_writes_nothing(self) -> None:
        (self.core / "files" / "scripts" / "alpha.py").write_bytes(b"edited\n")
        code, _, err = sync_cli(self.target, self.core)
        self.assertEqual(code, 1)
        self.assertIn("not clean", err)
        self.assertFalse((self.target / "scripts" / "alpha.py").exists())

    def test_refuses_a_subdirectory_as_template_root(self) -> None:
        subdir = self.target / "docs"
        subdir.mkdir()
        code, _, err = sync_cli(subdir, self.core)
        self.assertEqual(code, 2)
        self.assertIn("not the top", err)

    def test_stale_paths_are_reported_not_deleted(self) -> None:
        self.assertEqual(sync_cli(self.target, self.core)[0], 0)
        commit_all(self.target, "first sync")
        core_set_file(self.core, "tools/beta.py", None)
        code, out, err = sync_cli(self.target, self.core)
        self.assertEqual(code, 0, err)
        self.assertIn("tools/beta.py is no longer in core", out)
        self.assertTrue((self.target / "tools" / "beta.py").exists())

    def test_updates_a_changed_core_file_and_leaves_others_alone(self) -> None:
        self.assertEqual(sync_cli(self.target, self.core)[0], 0)
        commit_all(self.target, "first sync")
        beta = self.target / "tools" / "beta.py"
        beta_mtime = beta.stat().st_mtime_ns
        core_set_file(self.core, "scripts/alpha.py", b"print('new')\n")
        code, out, err = sync_cli(self.target, self.core)
        self.assertEqual(code, 0, err)
        self.assertEqual((self.target / "scripts" / "alpha.py").read_bytes(), b"print('new')\n")
        self.assertEqual(beta.stat().st_mtime_ns, beta_mtime)
        self.assertIn("1 written", out)


class CheckTests(TempBase):
    def setUp(self) -> None:
        super().setUp()
        self.first, self.second = make_templates(self.base)
        self.core = make_core(self.base, self.first, self.second)
        self.target = make_repo(self.base / "target", {"README.md": b"target\n"})
        self.assertEqual(sync_cli(self.target, self.core)[0], 0)

    def test_clean_stamp_exits_zero(self) -> None:
        code, out, _ = check_cli(self.target)
        self.assertEqual(code, 0)
        self.assertIn("OK: 5 shared files match", out)

    def test_edited_file_is_listed(self) -> None:
        (self.target / "scripts" / "alpha.py").write_bytes(b"edited\n")
        code, out, _ = check_cli(self.target)
        self.assertEqual(code, 1)
        self.assertIn("DRIFT: modified: scripts/alpha.py", out)

    def test_missing_file_is_listed(self) -> None:
        (self.target / "tools" / "beta.py").unlink()
        code, out, _ = check_cli(self.target)
        self.assertEqual(code, 1)
        self.assertIn("DRIFT: missing: tools/beta.py", out)

    def test_missing_stamp_is_drift(self) -> None:
        (self.target / ".core-stamp.json").unlink()
        code, out, _ = check_cli(self.target)
        self.assertEqual(code, 1)
        self.assertIn("no .core-stamp.json", out)

    @unittest.skipUnless(symlinks_supported(), "symbolic links not available")
    def test_symlink_in_place_of_a_shared_file_is_drift(self) -> None:
        outside = self.base / "outside.py"
        outside.write_bytes(SHARED["scripts/alpha.py"])
        (self.target / "scripts" / "alpha.py").unlink()
        os.symlink(str(outside), self.target / "scripts" / "alpha.py")
        code, out, _ = check_cli(self.target)
        self.assertEqual(code, 1)
        self.assertIn("scripts/alpha.py is a symbolic link", out)

    def test_core_check_passes_for_a_matching_stamp(self) -> None:
        code, out, err = check_cli(self.target, self.core)
        self.assertEqual(code, 0, err)
        self.assertIn("checked against core-manifest.json", out)

    def test_core_check_catches_a_stamp_that_disagrees_with_core(self) -> None:
        stamp = load_stamp(self.target)
        for item in stamp["files"]:
            if item["path"] == "scripts/run.sh":
                item["executable"] = False
        write_stamp(self.target, stamp)
        self.assertEqual(check_cli(self.target)[0], 0, "bytes still match, so offline check passes")
        code, out, _ = check_cli(self.target, self.core)
        self.assertEqual(code, 1)
        self.assertIn("stamp differs from core", out)
        self.assertIn("scripts/run.sh", out)

    def test_core_check_rejects_a_stamp_commit_missing_from_core(self) -> None:
        stamp = load_stamp(self.target)
        stamp["coreCommit"] = "a" * 40
        write_stamp(self.target, stamp)
        code, out, _ = check_cli(self.target, self.core)
        self.assertEqual(code, 1)
        self.assertIn("is not in the core repository", out)


class DiffTests(TempBase):
    def setUp(self) -> None:
        super().setUp()
        self.first, self.second = make_templates(self.base)
        self.core = make_core(self.base, self.first, self.second)
        self.target = make_repo(self.base / "target", {"README.md": b"target\n"})
        self.assertEqual(sync_cli(self.target, self.core)[0], 0)
        commit_all(self.target, "sync")

    def test_clean_template_has_no_changes(self) -> None:
        code, out, err = diff_cli(self.target, self.core)
        self.assertEqual(code, 0, err)
        self.assertIn("0 change(s)", out)

    def test_reports_new_and_updated_files_and_writes_nothing(self) -> None:
        core_set_file(self.core, "scripts/alpha.py", b"print('new')\n")
        core_set_file(self.core, "tools/new.py", b"NEW = 1\n")
        code, out, _ = diff_cli(self.target, self.core)
        self.assertEqual(code, 1)
        self.assertIn("update scripts/alpha.py", out)
        self.assertIn("new tools/new.py", out)
        self.assertEqual((self.target / "scripts" / "alpha.py").read_bytes(), SHARED["scripts/alpha.py"])
        self.assertFalse((self.target / "tools" / "new.py").exists())

    def test_reports_uncommitted_targets_as_blocked(self) -> None:
        fresh = make_repo(self.base / "fresh", {"README.md": b"x\n"})
        self.assertEqual(sync_cli(fresh, self.core)[0], 0)
        code, out, _ = diff_cli(fresh, self.core)
        self.assertEqual(code, 1)
        self.assertIn("blocked scripts/alpha.py: uncommitted or untracked changes", out)


class RefreshTests(TempBase):
    def setUp(self) -> None:
        super().setUp()
        self.first, self.second = make_templates(self.base)
        self.core = make_core(self.base, self.first, self.second)

    def test_hand_fix_is_recorded_and_carried_by_sync(self) -> None:
        (self.core / "files" / "scripts" / "alpha.py").write_bytes(b"print('fixed')\n")
        code, out, err = run_cli("refresh", "--core", str(self.core))
        self.assertEqual(code, 0, err)
        self.assertIn("1 entry changed: scripts/alpha.py", out)
        commit_all(self.core, "fix alpha")
        target = make_repo(self.base / "target", {"README.md": b"x\n"})
        self.assertEqual(sync_cli(target, self.core)[0], 0)
        self.assertEqual((target / "scripts" / "alpha.py").read_bytes(), b"print('fixed')\n")
        self.assertEqual(check_cli(target)[0], 0)

    def test_refresh_keeps_sources_and_unchanged_entries(self) -> None:
        before = load_manifest(self.core)
        code, out, err = run_cli("refresh", "--core", str(self.core))
        self.assertEqual(code, 0, err)
        self.assertIn("0 entries changed.", out)
        self.assertEqual(load_manifest(self.core), before)

    def test_refresh_refuses_a_file_the_manifest_does_not_list(self) -> None:
        (self.core / "files" / "scripts" / "extra.py").write_bytes(b"x\n")
        before = (self.core / "core-manifest.json").read_bytes()
        code, _, err = run_cli("refresh", "--core", str(self.core))
        self.assertEqual(code, 2)
        self.assertIn("scripts/extra.py", err)
        self.assertEqual((self.core / "core-manifest.json").read_bytes(), before)

    def test_refresh_refuses_a_listed_file_that_is_gone(self) -> None:
        (self.core / "files" / "tools" / "beta.py").unlink()
        code, _, err = run_cli("refresh", "--core", str(self.core))
        self.assertEqual(code, 2)
        self.assertIn("tools/beta.py", err)

    def test_refresh_recopies_the_core_owned_checker(self) -> None:
        checker = self.core / "sync_core.py"
        checker.write_bytes(checker.read_bytes() + b"# marker\n")
        code, out, err = run_cli("refresh", "--core", str(self.core))
        self.assertEqual(code, 0, err)
        self.assertEqual((self.core / "files" / "scripts" / "sync_core.py").read_bytes(), checker.read_bytes())
        self.assertIn("scripts/sync_core.py", out)


class AddTests(TempBase):
    def setUp(self) -> None:
        super().setUp()
        self.first, self.second = make_templates(self.base)
        self.core = make_core(self.base, self.first, self.second)

    def place(self, rel: str, data: bytes = b"new = 1\n", executable: bool = False) -> Path:
        target = self.core / "files" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if POSIX:
            os.chmod(target, 0o755 if executable else 0o644)
        return target

    def add(self, *paths: str, extra: tuple[str, ...] = ()) -> tuple[int, str, str]:
        return run_cli("add", *paths, *extra, "--core", str(self.core))

    def manifest_bytes(self) -> bytes:
        return (self.core / "core-manifest.json").read_bytes()

    def test_adds_a_placed_file_and_refresh_then_changes_nothing(self) -> None:
        sources = load_manifest(self.core)["sources"]
        self.place("scripts/new_tool.py", b"NEW = 1\n")
        code, out, err = self.add("scripts/new_tool.py")
        self.assertEqual(code, 0, err)
        self.assertIn("Added 1 path(s)", out)
        manifest = load_manifest(self.core)
        self.assertEqual(manifest["sources"], sources)
        self.assertIn(
            {"path": "scripts/new_tool.py", "sha256": hashlib.sha256(b"NEW = 1\n").hexdigest(), "executable": False},
            manifest["entries"],
        )
        paths = [e["path"] for e in manifest["entries"]]
        self.assertEqual(paths, sorted(paths, key=lambda p: p.encode("utf-8")))
        code, out, err = run_cli("refresh", "--core", str(self.core))
        self.assertEqual(code, 0, err)
        self.assertIn("0 entries changed.", out)

    def test_added_path_is_carried_by_sync_to_a_template(self) -> None:
        self.place("scripts/new_tool.py", b"NEW = 1\n")
        self.assertEqual(self.add("scripts/new_tool.py")[0], 0)
        commit_all(self.core, "add new_tool")
        target = make_repo(self.base / "target", {"README.md": b"x\n"})
        self.assertEqual(sync_cli(target, self.core)[0], 0)
        self.assertEqual((target / "scripts" / "new_tool.py").read_bytes(), b"NEW = 1\n")
        self.assertEqual(check_cli(target)[0], 0)

    @unittest.skipUnless(POSIX, "the executable bit is read from the file mode on POSIX")
    def test_records_the_executable_bit_from_the_file_mode(self) -> None:
        self.place("scripts/run_tool.sh", b"#!/bin/sh\necho run\n", executable=True)
        code, _, err = self.add("scripts/run_tool.sh")
        self.assertEqual(code, 0, err)
        entry = next(e for e in load_manifest(self.core)["entries"] if e["path"] == "scripts/run_tool.sh")
        self.assertTrue(entry["executable"])

    @unittest.skipUnless(POSIX, "the executable flag must match the file mode on POSIX")
    def test_executable_flag_that_contradicts_a_posix_file_mode_is_refused(self) -> None:
        self.place("scripts/plain.py", b"x = 1\n", executable=False)
        before = self.manifest_bytes()
        code, _, err = self.add("scripts/plain.py", extra=("--executable",))
        self.assertEqual(code, 1)
        self.assertIn("not executable", err)
        self.assertEqual(self.manifest_bytes(), before)

    @unittest.skipUnless(os.name == "nt", "Windows has no executable bit; the flag decides")
    def test_windows_takes_the_executable_flag_as_the_mode(self) -> None:
        self.place("scripts/run_tool.sh", b"#!/bin/sh\n")
        self.place("scripts/plain.py", b"x = 1\n")
        self.assertEqual(self.add("scripts/run_tool.sh", extra=("--executable",))[0], 0)
        self.assertEqual(self.add("scripts/plain.py")[0], 0)
        entries = {e["path"]: e["executable"] for e in load_manifest(self.core)["entries"]}
        self.assertTrue(entries["scripts/run_tool.sh"])
        self.assertFalse(entries["scripts/plain.py"])

    def test_refuses_a_path_the_manifest_already_lists(self) -> None:
        self.place("scripts/alpha.py", b"changed\n")
        before = self.manifest_bytes()
        code, _, err = self.add("scripts/alpha.py")
        self.assertEqual(code, 1)
        self.assertIn("already listed", err)
        self.assertEqual(self.manifest_bytes(), before)

    def test_refuses_unsafe_and_out_of_scope_paths_and_writes_nothing(self) -> None:
        self.place("scripts/ok.py")
        self.place("docs/extra.md")
        before = self.manifest_bytes()
        for rel in ("../escape.py", "/abs.py", "scripts/../x.py", "scripts/bad:name.py", "scripts/trailing.py.", "scripts/.git/config", "docs/extra.md"):
            with self.subTest(rel=rel):
                code, _, err = self.add(rel)
                self.assertEqual(code, 1, err)
                self.assertEqual(self.manifest_bytes(), before)

    def test_refuses_a_case_clash_with_a_listed_path(self) -> None:
        before = self.manifest_bytes()
        code, _, err = self.add("tools/BETA.py")
        self.assertEqual(code, 1)
        self.assertIn("differs only by case from tools/beta.py", err)
        self.assertEqual(self.manifest_bytes(), before)

    def test_refuses_the_same_path_twice_in_one_call(self) -> None:
        self.place("scripts/twice.py")
        before = self.manifest_bytes()
        code, _, err = self.add("scripts/twice.py", "scripts/twice.py")
        self.assertEqual(code, 1)
        self.assertIn("more than once", err)
        self.assertEqual(self.manifest_bytes(), before)

    def test_missing_file_is_a_usage_error_and_writes_nothing(self) -> None:
        before = self.manifest_bytes()
        code, _, err = self.add("scripts/missing.py")
        self.assertEqual(code, 2)
        self.assertIn("missing files/scripts/missing.py", err)
        self.assertEqual(self.manifest_bytes(), before)

    @unittest.skipUnless(symlinks_supported(), "symbolic links need privilege on this system")
    def test_refuses_a_symbolic_link_and_writes_nothing(self) -> None:
        target = self.place("scripts/real.py")
        os.symlink(str(target), self.core / "files" / "scripts" / "linked.py")
        before = self.manifest_bytes()
        code, _, err = self.add("scripts/linked.py")
        self.assertEqual(code, 1)
        self.assertIn("symbolic link", err)
        self.assertEqual(self.manifest_bytes(), before)

    def test_unlisted_files_in_files_dir_block_the_add(self) -> None:
        self.place("scripts/wanted.py")
        self.place("scripts/stray.py")
        before = self.manifest_bytes()
        code, _, err = self.add("scripts/wanted.py")
        self.assertEqual(code, 2)
        self.assertIn("scripts/stray.py", err)
        self.assertEqual(self.manifest_bytes(), before)

    def test_add_needs_a_manifest(self) -> None:
        (self.core / "core-manifest.json").unlink()
        self.place("scripts/wanted.py")
        code, _, err = self.add("scripts/wanted.py")
        self.assertEqual(code, 2)
        self.assertIn("run seed first", err)

    def test_add_needs_at_least_one_path(self) -> None:
        code, _, _ = run_cli("add", "--core", str(self.core))
        self.assertEqual(code, 2)


class CoreRepositoryTests(unittest.TestCase):
    """Integrity of the seeded core in this repository. Runs after `seed` has been committed."""

    def setUp(self) -> None:
        self.manifest = load_manifest(ROOT)

    def test_root_checker_and_shared_copy_are_byte_identical(self) -> None:
        self.assertEqual((ROOT / "sync_core.py").read_bytes(), (ROOT / "files" / "scripts" / "sync_core.py").read_bytes())

    def test_manifest_matches_the_files_directory(self) -> None:
        self.assertEqual(self.manifest["schemaVersion"], 1)
        paths = [e["path"] for e in self.manifest["entries"]]
        self.assertEqual(paths, sorted(paths, key=lambda p: p.encode("utf-8")))
        on_disk = sorted(p.relative_to(ROOT / "files").as_posix() for p in (ROOT / "files").rglob("*") if p.is_file())
        self.assertEqual(on_disk, sorted(paths))
        for entry in self.manifest["entries"]:
            source = ROOT / "files" / entry["path"]
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), entry["sha256"], entry["path"])
            if POSIX:
                self.assertEqual(bool(source.stat().st_mode & 0o111), entry["executable"], entry["path"])

    def test_manifest_records_both_sources_with_commit_ids(self) -> None:
        sources = self.manifest["sources"]
        self.assertEqual([s["label"] for s in sources], ["26.1", "26.2"])
        for source in sources:
            self.assertIsInstance(source["ref"], str)
            self.assertTrue(source["ref"])
        for source in sources:
            self.assertRegex(source["commit"], r"^[0-9a-f]{40}$")

    def test_template_ci_runner_is_shared(self) -> None:
        # Decided 2026-11-16: the CI runner is identical in both templates and must not drift either.
        paths = {e["path"] for e in self.manifest["entries"]}
        self.assertIn("scripts/ci_run_tests.py", paths)
        self.assertIn("tests/test_ci_run_tests.py", paths)
        self.assertEqual(self.manifest["excluded"], [])


if __name__ == "__main__":
    unittest.main()
