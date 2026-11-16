"""Tests for files/scripts/rollout.sh: a dry run must load the project .env the way the real run does.

Run from the core root:

    python3 -m unittest discover -s tests -v

Each test copies rollout.sh into a throwaway repository and puts a fake python3 first on PATH,
so the dry run executes offline and nothing reads a real template or database. The core does not
ship scripts/load_env.sh (the templates do), so a stand-in loader takes its place. It records the
file it was given and exports each KEY=VALUE line. These tests check the wiring around the loader,
not the loader's own validation.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROLLOUT_SH = ROOT / "files" / "scripts" / "rollout.sh"
BASH = shutil.which("bash")
RUNNABLE = os.name == "posix" and BASH is not None
SKIP_REASON = "rollout.sh is a POSIX bash script; the fake python3 is a bash script too, so Windows is not covered here"

FAKE_PYTHON = """#!/usr/bin/env bash
printf '%s\\n' "$@" > "$FAKE_PYTHON_ARGS"
env > "$FAKE_PYTHON_ENV"
"""

STAND_IN_LOADER = """# Stand-in for scripts/load_env.sh: record the file it was given, then export each KEY=VALUE line.
printf '%s\\n' "$1" >> "$LOADER_CALLS"
while IFS= read -r project_line || [ -n "$project_line" ]; do
  case "$project_line" in
    ''|'#'*) continue ;;
  esac
  export "$project_line"
done < "$1"
"""

PROJECT_ENV = """# project settings read by the dry run
PROD_SQLCL_CONNECTION=prod_alias
PROD_EXPECTED_USER=PROD_APP_OWNER
PROD_SCHEMA=PROD_APP_OWNER
STAGING_SQLCL_CONNECTION=staging_alias
STAGING_EXPECTED_USER=STG_APP_OWNER
STAGING_SCHEMA=STG_APP_OWNER
"""

EXPECTED_FROM_ENV = {
    "PROD_SQLCL_CONNECTION": "prod_alias",
    "PROD_EXPECTED_USER": "PROD_APP_OWNER",
    "PROD_SCHEMA": "PROD_APP_OWNER",
    "STAGING_SQLCL_CONNECTION": "staging_alias",
    "STAGING_EXPECTED_USER": "STG_APP_OWNER",
    "STAGING_SCHEMA": "STG_APP_OWNER",
}


def read_env_dump(path: Path) -> dict[str, str]:
    """Parse the output of `env` written by the fake python3."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return values


@unittest.skipUnless(RUNNABLE, SKIP_REASON)
class RolloutDryRunEnvTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()  # rollout.sh reports its root with pwd -P
        scripts = self.root / "scripts"
        scripts.mkdir()
        shutil.copy2(ROLLOUT_SH, scripts / "rollout.sh")
        (scripts / "load_env.sh").write_text(STAND_IN_LOADER, encoding="utf-8", newline="\n")
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        fake_python = bin_dir / "python3"
        fake_python.write_text(FAKE_PYTHON, encoding="utf-8", newline="\n")
        fake_python.chmod(0o755)

    def write_env(self, text: str, name: str = ".env") -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def run_rollout(self, *args: str, project_env_file: str | None = None) -> subprocess.CompletedProcess[str]:
        # Drop inherited PROD_/STAGING_ settings and PROJECT_ENV_FILE so only the test's own .env can supply them.
        env = {key: value for key, value in os.environ.items() if not key.startswith(("PROD_", "STAGING_")) and key != "PROJECT_ENV_FILE"}
        env.update(
            PATH=f"{self.root / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
            FAKE_PYTHON_ARGS=str(self.root / "python-args.txt"),
            FAKE_PYTHON_ENV=str(self.root / "python-env.txt"),
            LOADER_CALLS=str(self.root / "loader-calls.txt"),
        )
        if project_env_file is not None:
            env["PROJECT_ENV_FILE"] = project_env_file
        return subprocess.run(
            [BASH, str(self.root / "scripts" / "rollout.sh"), *args],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def loader_calls(self) -> list[str]:
        path = self.root / "loader-calls.txt"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def python_args(self) -> list[str]:
        return (self.root / "python-args.txt").read_text(encoding="utf-8").splitlines()

    def python_env(self) -> dict[str, str]:
        return read_env_dump(self.root / "python-env.txt")

    def test_dry_run_passes_the_project_env_to_python(self) -> None:
        env_file = self.write_env(PROJECT_ENV)

        result = self.run_rollout("--dry-run")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.loader_calls(), [str(env_file)])
        self.assertTrue(self.python_args()[0].endswith("scripts/rollout.py"), self.python_args())
        self.assertEqual(self.python_args()[-1], "--dry-run")
        seen = self.python_env()
        for key, value in EXPECTED_FROM_ENV.items():
            self.assertEqual(seen.get(key), value, key)

    def test_dry_run_without_env_still_succeeds(self) -> None:
        result = self.run_rollout("--dry-run")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.loader_calls(), [])
        self.assertTrue(self.python_args()[0].endswith("scripts/rollout.py"), self.python_args())
        self.assertEqual(self.python_args()[-1], "--dry-run")
        self.assertNotIn("PROD_SCHEMA", self.python_env())

    def test_real_run_still_loads_the_env_before_python(self) -> None:
        env_file = self.write_env(PROJECT_ENV)

        result = self.run_rollout()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.loader_calls(), [str(env_file)])
        self.assertEqual(len(self.python_args()), 1, self.python_args())
        seen = self.python_env()
        for key, value in EXPECTED_FROM_ENV.items():
            self.assertEqual(seen.get(key), value, key)

    def test_dry_run_honours_a_project_env_file_override(self) -> None:
        alt_env = self.write_env("PROD_SCHEMA=ALT_OWNER\n", name="alt.env")

        result = self.run_rollout("--dry-run", project_env_file=str(alt_env))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.loader_calls(), [str(alt_env)])
        self.assertEqual(self.python_env().get("PROD_SCHEMA"), "ALT_OWNER")


if __name__ == "__main__":
    unittest.main()
