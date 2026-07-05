"""Put the governor package on sys.path so the offline tests can import it as
``governor.*`` (and its submodules as ``memory.*``) without installing the
service or its FastAPI/asyncpg dependencies."""

import pathlib
import sys

_SRC = (
    pathlib.Path(__file__).resolve().parents[2]
    / "services"
    / "memory-governor"
    / "src"
)
for _path in (_SRC, _SRC / "governor"):
    sys.path.insert(0, str(_path))
