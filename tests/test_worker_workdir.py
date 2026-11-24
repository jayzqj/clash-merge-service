"""Worker workdirs repair geodata links left over from a copied/moved workdir."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from app import purity

GEO = ("geosite.dat", "geoip.dat", "geoip.metadb", "Country.mmdb")


def _symlinks_supported() -> bool:
    d = Path(tempfile.mkdtemp())
    try:
        (d / "t").write_text("x")
        (d / "l").symlink_to(d / "t")
        return True
    except (OSError, NotImplementedError):
        return False
    finally:
        shutil.rmtree(d, ignore_errors=True)


@unittest.skipUnless(_symlinks_supported(), "symlinks not available")
class WorkerWorkdirTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.base = self.root / "new"
        self.base.mkdir()
        for name in GEO:
            (self.base / name).write_bytes(b"geo-" + name.encode())
        self.worker = self.base / "worker-1"
        self.worker.mkdir()

    def _target(self, name):
        return os.path.abspath(self.base / name)

    def test_worker_zero_is_base(self):
        self.assertEqual(purity._worker_workdir(self.base, 0), self.base)

    def test_creates_links(self):
        d = purity._worker_workdir(self.base, 1)
        for name in GEO:
            self.assertTrue((d / name).is_symlink())
            self.assertEqual(os.readlink(d / name), self._target(name))

    def test_repoints_link_into_old_workdir(self):
        old = self.root / "old"
        old.mkdir()
        for name in GEO:
            (old / name).write_bytes(b"old")
            (self.worker / name).symlink_to(old / name)
        purity._worker_workdir(self.base, 1)
        for name in GEO:
            self.assertEqual(os.readlink(self.worker / name), self._target(name))
            self.assertEqual((self.worker / name).read_bytes(), b"geo-" + name.encode())

    def test_repoints_dangling_link(self):
        for name in GEO:
            (self.worker / name).symlink_to(self.root / "gone" / name)
        purity._worker_workdir(self.base, 1)
        for name in GEO:
            self.assertTrue((self.worker / name).exists())
            self.assertEqual(os.readlink(self.worker / name), self._target(name))

    def test_removes_stale_link_when_source_missing(self):
        (self.base / "Country.mmdb").unlink()
        (self.worker / "Country.mmdb").symlink_to(self.root / "gone" / "Country.mmdb")
        purity._worker_workdir(self.base, 1)
        self.assertFalse(os.path.lexists(self.worker / "Country.mmdb"))

    def test_keeps_real_file(self):
        (self.worker / "geoip.dat").write_bytes(b"own copy")
        purity._worker_workdir(self.base, 1)
        self.assertFalse((self.worker / "geoip.dat").is_symlink())
        self.assertEqual((self.worker / "geoip.dat").read_bytes(), b"own copy")


if __name__ == "__main__":
    unittest.main()
