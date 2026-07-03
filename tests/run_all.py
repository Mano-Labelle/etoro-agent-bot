#!/usr/bin/env python3
"""Lance toute la suite de tests unitaires (aucun réseau).

Usage: python3 tests/run_all.py
Découvre tous les fichiers test_*.py du dossier tests/.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

if __name__ == "__main__":
    suite = unittest.TestLoader().discover(HERE, pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
