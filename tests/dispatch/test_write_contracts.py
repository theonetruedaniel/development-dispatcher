import copy
import tempfile
import unittest
from pathlib import Path
from scripts.dispatch.write_contracts import validate_write_manifest, validate_write_grant
from scripts.dispatch.contracts import manifest_digest, validate_manifest


def work(root):
    return dict(schema_version=2, package_id="edit", task="Edit sample",
                task_class="implementation", workspace=str(root),
                allowed_actions=["read", "write"], required=True,
                timeout_seconds=60, max_attempts=1, verification=[dict(type="final_equals", expected="OK")],
                baseline=dict(commit="a" * 40, overlays=[]), inputs=["src/a.py"],
                outputs=["src/"], checks=["unit"], selector_mode="rules")


class Contracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_versioned_manifest_and_bound_grant(self):
        normalized = validate_write_manifest(work(self.root), self.root)
        self.assertEqual(normalized["schema_version"], 2)
        grant = dict(schema_version=2, manifest_digest=manifest_digest(normalized),
                     workspace=str(self.root), account_fingerprint="f"*64, expires_at=200,
                     allowed_actions=["read", "write"], max_attempts=1, dedicated_session=True)
        self.assertEqual(validate_write_grant(grant, normalized, "f"*64, 100), grant)
        for field, bad in [("expires_at", 99), ("manifest_digest", "0"*64),
                           ("account_fingerprint", "a"*64), ("dedicated_session", False)]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_write_grant(dict(grant, **{field: bad}), normalized, "f"*64, 100)

    def test_paths_cannot_expand_scope(self):
        for path in ["../x", "C:/x", "//host/share", "src/a:ads", "src/../x",
                     "src/CON", "src/name.", "docs/architecture/", "docs/evidence/",
                     "scripts/dispatch/", ".git/", ".dispatch-runs/", "AGENTS.md"]:
            raw = work(self.root)
            raw["outputs"] = [path]
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_write_manifest(raw, self.root)
        raw = work(self.root)
        raw["inputs"] = ["src/A.py", "src/a.py"]
        with self.assertRaises(ValueError):
            validate_write_manifest(raw, self.root)

    def test_missing_baseline_and_unknown_fields(self):
        raw = work(self.root)
        del raw["baseline"]
        with self.assertRaises(ValueError):
            validate_write_manifest(raw, self.root)
        with self.assertRaises(ValueError):
            validate_write_manifest(dict(work(self.root), command="anything"), self.root)

    def test_legacy_read_only_is_unchanged(self):
        raw = work(self.root)
        for key in ("baseline", "inputs", "outputs", "checks", "selector_mode"):
            del raw[key]
        raw.update(schema_version=1, allowed_actions=["read"])
        before = copy.deepcopy(raw)
        self.assertEqual(validate_manifest(raw, self.root)["allowed_actions"], ["read"])
        self.assertEqual(raw, before)
