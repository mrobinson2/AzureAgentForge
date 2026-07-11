"""Put the memory-store package on sys.path so tests can `import app.*`
without installing the service."""

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
