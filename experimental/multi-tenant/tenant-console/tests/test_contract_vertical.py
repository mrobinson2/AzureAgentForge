"""aaf-0012 regression: tenant.vertical must not enable path traversal.

`vertical` is joined onto the playbooks-root filesystem path to locate a pack.
A malicious value ("../../etc", "../secrets", an absolute path) must be rejected
by strict validation BEFORE any filesystem I/O, and the resolved pack dir must
stay contained under the playbooks root.

Run:  pip install pyyaml && pytest tests/test_contract_vertical.py
"""

import sys
from pathlib import Path

import pytest

# Make `import tenantconsole` work from a plain checkout (mirror console/app.py).
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from tenantconsole.contract import (  # noqa: E402
    VERTICAL_RE,
    ContractError,
    load_contract,
    repo_playbooks_root,
)

VALID_VERTICAL = "example-fieldservice"  # the pack shipped under playbooks/


def _write_contract(tmp_path: Path, vertical: str) -> Path:
    doc = (
        "tenant:\n"
        "  slug: acme-co\n"
        "  display_name: Acme Co\n"
        f"  vertical: {vertical!r}\n"
        "  budgets:\n"
        "    daily_usd: 5\n"
        "    per_run_usd: 0.5\n"
    )
    p = tmp_path / "contract.yaml"
    p.write_text(doc, encoding="utf-8")
    return p


@pytest.mark.parametrize(
    "evil",
    [
        "../../etc",
        "../secrets",
        "..",
        "foo/bar",
        "/etc/passwd",
        "a..b",
        "UPPER",
        "with space",
    ],
)
def test_traversal_and_malformed_verticals_are_rejected(tmp_path, evil):
    # None of these match the strict pack-name regex; validation must not treat
    # them as a real pack (and must not traverse the filesystem to find one).
    assert not VERTICAL_RE.match(evil)
    p = _write_contract(tmp_path, evil)
    with pytest.raises(ContractError) as exc:
        load_contract(p)
    assert "vertical" in str(exc.value)


def test_valid_vertical_loads():
    # The real shipped pack still resolves cleanly through the hardened path.
    root = repo_playbooks_root()
    if not (root / VALID_VERTICAL / "pack.yaml").is_file():
        pytest.skip("example pack not present in this checkout")
    import tempfile

    doc = (
        "tenant:\n"
        "  slug: acme-co\n"
        "  display_name: Acme Co\n"
        f"  vertical: {VALID_VERTICAL}\n"
        "  budgets:\n"
        "    daily_usd: 5\n"
        "    per_run_usd: 0.5\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as fh:
        fh.write(doc)
        path = fh.name
    contract = load_contract(path)
    assert contract.vertical == VALID_VERTICAL
    # The resolved pack dir stays under the playbooks root.
    assert root.resolve() in contract.pack_dir.resolve().parents
