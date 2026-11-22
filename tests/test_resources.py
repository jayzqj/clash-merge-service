import os
import unittest
from unittest.mock import patch

from app.resources import GIB, recommend_workers
from app.runtime import desktop_data_dir


class ResourceTests(unittest.TestCase):
    def test_current_load_limits_cpu_recommendation(self):
        idle = recommend_workers(8 * GIB, 32 * GIB, 16, 0)
        busy = recommend_workers(8 * GIB, 32 * GIB, 16, 75)
        self.assertEqual(idle["recommended_workers"], 8)
        self.assertEqual(busy["recommended_workers"], 2)

    def test_available_memory_not_installed_capacity_limits_workers(self):
        value = recommend_workers(3.75 * GIB, 32 * GIB, 16, 0)
        self.assertEqual(value["recommended_workers"], 2)
        tight = recommend_workers(1 * GIB, 32 * GIB, 16, 0)
        self.assertEqual(tight["recommended_workers"], 1)
        self.assertTrue(tight["memory_tight"])

    def test_bounds_and_single_cpu(self):
        self.assertEqual(recommend_workers(64 * GIB, 128 * GIB, 64, 0)["recommended_workers"], 8)
        self.assertEqual(recommend_workers(4 * GIB, 8 * GIB, 1, 100)["recommended_workers"], 1)

    def test_default_data_directory_is_on_d_drive(self):
        with patch.dict(os.environ):
            os.environ.pop("CLASH_MERGE_DATA_DIR", None)
            self.assertEqual(str(desktop_data_dir()).replace("\\", "/"), "D:/ClashMerge")


if __name__ == "__main__":
    unittest.main()
