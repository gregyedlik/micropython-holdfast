import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tools.build_release import build_binary_release


class BuildBinaryReleaseTests(unittest.TestCase):
    def test_builds_binary_and_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.bin"
            source.write_bytes(b"holdfast firmware\x00\x01")

            manifest = build_binary_release(
                source,
                root / "release",
                version=7,
                target="park61-ac-esp32c3",
            )

            copied = root / "release" / "firmware.bin"
            written_manifest = json.loads(
                (root / "release" / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(copied.read_bytes(), source.read_bytes())
            self.assertEqual(manifest, written_manifest)
            self.assertEqual(written_manifest["schema"], 1)
            self.assertEqual(written_manifest["version"], 7)
            self.assertEqual(written_manifest["target"], "park61-ac-esp32c3")
            self.assertEqual(written_manifest["firmware"]["size"], len(source.read_bytes()))
            self.assertEqual(
                written_manifest["firmware"]["sha256"],
                hashlib.sha256(source.read_bytes()).hexdigest(),
            )

    def test_rejects_invalid_target_and_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.bin"
            source.write_bytes(b"firmware")
            with self.assertRaises(ValueError):
                build_binary_release(source, root / "out", 0, "target")
            with self.assertRaises(ValueError):
                build_binary_release(source, root / "out", 1, "../target")


if __name__ == "__main__":
    unittest.main()
