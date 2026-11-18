import unittest
import time
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(HERE)]

suite = unittest.defaultTestLoader.discover(HERE, top_level_dir=HERE)

for test in suite:
    for case in test:
        start = time.time()
        result = unittest.TextTestRunner(stream=sys.stdout).run(case)
        print(f"Test {case} took {time.time() - start:.2f}s")
