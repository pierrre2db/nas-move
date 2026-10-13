import os
import tempfile
import threading
import unittest
from pathlib import Path

from nas_move import core


def make_tree(root: Path):
    (root / "a" / "b").mkdir(parents=True)
    (root / "empty").mkdir()
    (root / "top.txt").write_text("top")
    (root / "a" / "one.bin").write_bytes(os.urandom(10_000))
    (root / "a" / "b" / "two.txt").write_text("deux")
    (root / ".DS_Store").write_text("junk")


class MoveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        core.STATE_DIR = base / "state"
        core.STATE_FILE = core.STATE_DIR / "last_job.json"
        self.src, self.dst = base / "nas1" / "projet", base / "nas2"
        self.src.mkdir(parents=True)
        self.dst.mkdir()
        make_tree(self.src)

    def tearDown(self):
        self.tmp.cleanup()

    def opts(self, **kw):
        return core.Options(src=str(self.src), dst=str(self.dst), **kw)

    def test_full_move(self):
        s = core.Mover(self.opts()).run()
        self.assertTrue(s.ok)
        self.assertEqual(s.moved, 3)
        out = self.dst / "projet"
        self.assertEqual((out / "a" / "b" / "two.txt").read_text(), "deux")
        self.assertTrue((out / "empty").is_dir())
        self.assertFalse(self.src.exists())  # source nettoyée
        self.assertIsNone(core.load_interrupted())

    def test_dry_run_changes_nothing(self):
        s = core.Mover(self.opts(dry_run=True)).run()
        self.assertEqual(s.moved, 3)
        self.assertFalse((self.dst / "projet").exists())
        self.assertTrue((self.src / "top.txt").exists())

    def test_keep_source(self):
        core.Mover(self.opts(keep_source=True)).run()
        self.assertTrue((self.src / "top.txt").exists())
        self.assertTrue((self.dst / "projet" / "top.txt").exists())

    def test_resume_after_crash(self):
        # état d'un plantage : un fichier copié mais source non supprimée,
        # un .part orphelin, un fichier tronqué
        out = self.dst / "projet"
        (out / "a" / "b").mkdir(parents=True)
        (out / "top.txt").write_text("top")
        (out / "a" / f"one.bin{core.PART_SUFFIX}").write_bytes(b"xx")
        core.save_state(self.opts(), "running")
        self.assertIsNotNone(core.load_interrupted())
        s = core.Mover(self.opts()).run()
        self.assertTrue(s.ok)
        self.assertFalse(self.src.exists())
        self.assertEqual(len(list(out.rglob("*" + core.PART_SUFFIX))), 0)
        self.assertEqual((out / "a" / "one.bin").stat().st_size, 10_000)

    def test_conflict_policies(self):
        out = self.dst / "projet"
        out.mkdir()
        (out / "top.txt").write_text("DIFFERENT")
        s = core.Mover(self.opts(on_conflict="skip")).run()
        self.assertEqual(s.skipped, 1)
        self.assertEqual((out / "top.txt").read_text(), "DIFFERENT")
        self.assertTrue((self.src / "top.txt").exists())  # jamais supprimé
        s = core.Mover(self.opts(on_conflict="rename")).run()
        self.assertEqual((out / "top (1).txt").read_text(), "top")
        self.assertFalse(self.src.exists())

    def test_overwrite(self):
        out = self.dst / "projet"
        out.mkdir()
        (out / "top.txt").write_text("DIFFERENT")
        core.Mover(self.opts(on_conflict="overwrite")).run()
        self.assertEqual((out / "top.txt").read_text(), "top")

    def test_cancel_then_resume(self):
        m = core.Mover(self.opts())
        calls = []

        def on_progress(s, cur):
            calls.append(cur)
            if len(calls) == 2:
                m.cancel()

        m.on_progress = on_progress
        s = m.run()
        self.assertTrue(s.cancelled)
        self.assertIsNotNone(core.load_interrupted())
        s2 = core.Mover(self.opts()).run()
        self.assertTrue(s2.ok)
        self.assertFalse(self.src.exists())
        self.assertEqual((self.dst / "projet" / "a" / "b" / "two.txt").read_text(), "deux")

    def test_live_progress(self):
        (self.src / "big.bin").write_bytes(os.urandom(core.CHUNK * 3))
        seen = []
        m = core.Mover(self.opts(), on_progress=lambda s, cur: seen.append((s.bytes_done, s.phase, cur, s.file_done)))
        m._last_tick = float("-inf")
        s = m.run()
        self.assertTrue(s.ok)
        phases = {p for _, p, _, _ in seen}
        self.assertTrue({"Copie", "Vérification"} <= phases)
        done = [b for b, _, _, _ in seen]
        self.assertEqual(done, sorted(done))  # jamais de recul
        self.assertEqual(s.bytes_done, s.total_bytes)

    def test_preflight(self):
        checks = core.preflight(self.opts())
        self.assertTrue(all(c.ok for c in checks), checks)
        self.assertTrue(any("écriture" in c.label for c in checks))
        self.assertEqual(list(self.dst.glob(".nasmove_test_*")), [])  # fichier test supprimé
        bad = core.check_location("/Volumes/__absent__/x", "Source", write=False)
        self.assertFalse(bad[0].ok)

    def test_timeout_helper(self):
        import time as _t
        with self.assertRaises(TimeoutError):
            core.run_with_timeout(lambda: _t.sleep(2), 0.1)
        self.assertEqual(core.run_with_timeout(lambda: 7, 1), 7)

    def test_empty_source_is_reported(self):
        empty = Path(self.tmp.name) / "vide"
        empty.mkdir()
        s = core.Mover(core.Options(src=str(empty), dst=str(self.dst), dry_run=True)).run()
        self.assertFalse(s.ok)
        self.assertIn("Aucun fichier", s.errors[0][1])

    def test_validation(self):
        self.assertTrue(core.validate(core.Options(src=str(self.src), dst=str(self.src / "a"))))
        self.assertTrue(core.validate(core.Options(src=str(self.src), dst=str(self.src))))
        self.assertTrue(core.validate(core.Options(src="/nope", dst=str(self.dst))))
        self.assertFalse(core.validate(self.opts()))

    def test_volume_detection(self):
        self.assertEqual(core.volume_root("/Volumes/NAS/x/y"), Path("/Volumes/NAS"))
        self.assertIsNone(core.volume_root("/Users/me/x"))
        self.assertFalse(core.volume_connected("/Volumes/__absent__/x"))


if __name__ == "__main__":
    unittest.main()
