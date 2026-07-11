"""Put the example root on sys.path so ``import saga`` works from any cwd."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
