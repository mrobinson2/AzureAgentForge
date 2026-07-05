"""Destroy detector for the reference deploy pipeline.

Reads a ``terraform show -json`` plan and decides whether applying it would
delete or replace any resource. The actual verdict logic lives in
``installer.core.plan_has_destroy`` so the GitHub Actions pipeline and the
Forge Console agree on exactly one definition of "destructive".

Usage (inside the workflow):

    terraform show -json tfplan > plan.json
    python -m installer.detect_destroy plan.json

The script:
  * prints a human-readable summary,
  * appends ``has_destroy`` and ``destroyed`` to ``$GITHUB_OUTPUT`` (so the
    apply/gate jobs can branch on it),
  * appends a short report to ``$GITHUB_STEP_SUMMARY`` when present,
  * always exits 0 — gating is done by job ``if:`` conditions, not exit codes,
    so a destructive plan still completes the plan job and reaches the gate.
"""

from __future__ import annotations

import json
import os
import sys

from installer.core import plan_has_destroy


def _emit_output(name: str, value: str) -> None:
    """Append a key to $GITHUB_OUTPUT using the multiline-safe heredoc form."""
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a", encoding="utf-8") as fh:
        if "\n" in value:
            fh.write(f"{name}<<__AAF_EOF__\n{value}\n__AAF_EOF__\n")
        else:
            fh.write(f"{name}={value}\n")


def _emit_summary(text: str) -> None:
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    with open(summary, "a", encoding="utf-8") as fh:
        fh.write(text + "\n")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m installer.detect_destroy <plan.json>", file=sys.stderr)
        return 0  # non-fatal: never break the pipeline on bad invocation
    try:
        with open(argv[1], encoding="utf-8") as fh:
            plan = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        # Fail SAFE: if we cannot read the plan, treat it as destructive so a
        # human looks at it rather than auto-applying something unparsed.
        print(f"[detect-destroy] could not read plan ({e}) — failing safe to DESTROY", file=sys.stderr)
        _emit_output("has_destroy", "true")
        _emit_output("destroyed", "<plan unreadable — manual review required>")
        _emit_summary("### ⚠️ Plan unreadable — routing to manual approval (fail-safe)")
        return 0

    has_destroy, destroyed = plan_has_destroy(plan)

    if has_destroy:
        print(f"[detect-destroy] DESTRUCTIVE plan — {len(destroyed)} resource(s) deleted/replaced:")
        for addr in destroyed:
            print(f"  - {addr}")
        _emit_summary("### 🛑 Destructive plan — manual approval required")
        _emit_summary(f"{len(destroyed)} resource(s) would be **deleted or replaced**:\n")
        _emit_summary("\n".join(f"- `{a}`" for a in destroyed))
    else:
        print("[detect-destroy] no deletes or replacements — safe to auto-apply.")
        _emit_summary("### ✅ Non-destructive plan — auto-apply")
        _emit_summary("No resources would be deleted or replaced.")

    _emit_output("has_destroy", "true" if has_destroy else "false")
    _emit_output("destroyed", "\n".join(destroyed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
