"""The committed bundle in factory/ui/assets matches a fresh build of ui/ (ui_bundle_in_sync)."""
import tempfile
import unittest
from pathlib import Path

import ui_support as u


@unittest.skipUnless(u.NPM, "npm is required to build the UI bundle")
class BundleInSync(unittest.TestCase):
    def test_packaged_bundle_matches_a_fresh_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            built = u.npm("run", "build", "--", "--outDir", tmp, "--emptyOutDir")
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            fresh = {p.relative_to(tmp).as_posix(): p.read_bytes() for p in Path(tmp).rglob("*") if p.is_file()}
        packaged = {p.relative_to(u.ASSETS).as_posix(): p.read_bytes() for p in u.ASSETS.rglob("*") if p.is_file()}
        self.assertIn("index.html", packaged)
        self.assertEqual(sorted(packaged), sorted(fresh), "run `npm --prefix ui run build` and commit factory/ui/assets")
        self.assertEqual([n for n in fresh if fresh[n] != packaged[n]], [], "run `npm --prefix ui run build` and commit factory/ui/assets")


if __name__ == "__main__":
    unittest.main()
