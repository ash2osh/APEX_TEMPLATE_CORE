"""Unit tests for files/scripts/template_release.py, the release switch of the shared core files."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

CORE = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("template_release", CORE / "files" / "scripts" / "template_release.py")
assert _SPEC is not None and _SPEC.loader is not None
template_release = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(template_release)


class ReadTemplateReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write_manifest(self, text: str) -> None:
        (self.root / "template-manifest.json").write_text(text, encoding="utf-8")

    def test_manifest_without_apex_release_is_26_1(self) -> None:
        # The 26.1 template manifest is schema 1 and has no apexRelease key.
        self.write_manifest(json.dumps({"schemaVersion": 1, "templateOwned": []}))
        self.assertEqual("26.1", template_release.read_template_release(self.root))
        self.assertFalse(template_release.is_apex_26_2(self.root))

    def test_manifest_with_26_2_is_26_2(self) -> None:
        self.write_manifest(json.dumps({"schemaVersion": 2, "apexRelease": "26.2", "minimumSqlclVersion": "26.3.0.0"}))
        self.assertEqual("26.2", template_release.read_template_release(self.root))
        self.assertTrue(template_release.is_apex_26_2(self.root))

    def test_explicit_26_1_is_accepted(self) -> None:
        self.write_manifest(json.dumps({"schemaVersion": 1, "apexRelease": "26.1"}))
        self.assertEqual("26.1", template_release.read_template_release(self.root))

    def test_missing_manifest_raises(self) -> None:
        with self.assertRaises(template_release.TemplateReleaseError) as ctx:
            template_release.read_template_release(self.root)
        self.assertIn("cannot read", str(ctx.exception))

    def test_invalid_json_raises(self) -> None:
        self.write_manifest("{not json")
        with self.assertRaises(template_release.TemplateReleaseError):
            template_release.read_template_release(self.root)

    def test_undecodable_bytes_raise(self) -> None:
        (self.root / "template-manifest.json").write_bytes(b"\xff\xfe\x00")
        with self.assertRaises(template_release.TemplateReleaseError):
            template_release.read_template_release(self.root)

    def test_non_object_manifest_raises(self) -> None:
        for text in ("[]", "\"26.2\"", "null"):
            with self.subTest(text=text):
                self.write_manifest(text)
                with self.assertRaisesRegex(template_release.TemplateReleaseError, "JSON object"):
                    template_release.read_template_release(self.root)

    def test_unknown_or_null_release_raises(self) -> None:
        for value in ("26.3", "26.0", "", None, 26.2, ["26.2"]):
            with self.subTest(value=value):
                self.write_manifest(json.dumps({"schemaVersion": 2, "apexRelease": value}))
                with self.assertRaisesRegex(template_release.TemplateReleaseError, "apexRelease must be one of"):
                    template_release.read_template_release(self.root)

    def test_only_the_manifest_under_root_decides(self) -> None:
        # Only the manifest under root decides; a second manifest elsewhere is ignored.
        other = Path(self.temporary.name) / "other"
        other.mkdir()
        (other / "template-manifest.json").write_text(json.dumps({"apexRelease": "26.2"}), encoding="utf-8")
        self.write_manifest(json.dumps({"schemaVersion": 1}))
        self.assertEqual("26.1", template_release.read_template_release(self.root))


class ModuleSurfaceTests(unittest.TestCase):
    def test_releases_and_names(self) -> None:
        self.assertEqual(("26.1", "26.2"), template_release.RELEASES)
        self.assertEqual("template-manifest.json", template_release.MANIFEST_NAME)
        for name in template_release.__all__:
            self.assertTrue(hasattr(template_release, name), name)


if __name__ == "__main__":
    unittest.main()
