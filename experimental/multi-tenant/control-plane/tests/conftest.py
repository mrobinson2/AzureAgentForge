"""Put the control-plane dir on sys.path so tests can import user_tokens
without installing the service."""

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
