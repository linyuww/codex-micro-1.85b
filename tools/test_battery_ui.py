#!/usr/bin/env python3
"""Regression tests for the dashboard battery icon bands."""

import unittest

import pixel_preview


class BatteryLevelTests(unittest.TestCase):
    def test_inclusive_level_boundaries(self):
        cases = {
            -1: 1,
            0: 1,
            1: 1,
            20: 1,
            21: 2,
            40: 2,
            41: 3,
            60: 3,
            61: 4,
            80: 4,
            81: 5,
            100: 5,
            101: 5,
        }
        for percent, expected in cases.items():
            with self.subTest(percent=percent):
                self.assertEqual(pixel_preview.battery_level(percent), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
