"""Security review S9: a worker's mihomo.log is truncated once it is over the size limit."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import purity


class MihomoLogModeTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.log = self.dir / "mihomo.log"

    def test_missing_log_appends(self):
        self.assertEqual(purity._log_mode(self.log), "a")

    def test_small_log_appends(self):
        self.log.write_bytes(b"x" * 100)
        with patch.object(purity, "MIHOMO_LOG_MAX_BYTES", 1024):
            self.assertEqual(purity._log_mode(self.log), "a")

    def test_oversized_log_is_truncated(self):
        self.log.write_bytes(b"x" * 2048)
        with patch.object(purity, "MIHOMO_LOG_MAX_BYTES", 1024):
            mode = purity._log_mode(self.log)
        self.assertEqual(mode, "w")
        with open(self.log, mode, encoding="utf-8") as handle:
            handle.write("new round\n")
        self.assertEqual(self.log.read_text(encoding="utf-8"), "new round\n")


if __name__ == "__main__":
    unittest.main()
