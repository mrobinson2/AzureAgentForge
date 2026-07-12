# Turnkey scaffold-3 — Forge Console CI/CD-Setup Page Implementation Plan

> **Technical reference for contributors.** For the operational overview, start at [README](../../../README.md) or [Architecture](../../architecture.md).

> **For agentic workers:** REQUIRED SUB-SKILL: Use a subagent-driven development workflow (recommended) or a plan-execution workflow to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "CI/CD Setup" page to the Forge Console that runs the existing `scripts/scaffold-cicd.sh` (OIDC app + state backend + GitHub vars/secrets/deploy-destroy env) as a one-click, live-streamed operation — preview-first, with provider secrets passed via the subprocess environment (never the command line), and an explicit typed confirmation before `--apply`.

**Architecture:** A pure `build_scaffold_command(params)` in `core.py` maps validated UI inputs to scaffold-cicd.sh flags (secrets excluded by construction). The existing one-at-a-time `Runner` is extended with an optional `env` so the endpoint can pass provider keys as environment variables. A new `POST /api/scaffold` endpoint builds the command, enforces an apply-confirmation gate (mirroring the destroy-approval pattern), and runs it through the Runner; output streams over the existing `/api/stream` SSE channel. The UI adds a form + Preview/Apply buttons.

**Tech Stack:** Python 3, FastAPI, the Forge Console's `core.Runner` (subprocess streaming), pytest + FastAPI `TestClient`, vanilla HTML/JS.

---

## File Structure

- **Modify** `installer/core.py`:
  - add `build_scaffold_command(params, repo_root=REPO_ROOT)` (pure, validated).
  - add an optional `env` parameter to `Runner.start` and `Runner._execute`.
  - add a `"scaffold"` entry to `STEP_TIMEOUTS`.
- **Modify** `installer/app.py`: add `ScaffoldBody`, `SCAFFOLD_APPLY_TOKEN`, and `POST /api/scaffold`.
- **Modify** `installer/static/index.html`: add a "CI/CD Setup" section (form + Preview/Apply + stream).
- **Create** `installer/tests/test_scaffold.py`: unit tests for `build_scaffold_command`, the Runner `env`, and the endpoint wiring/gate.

scaffold-cicd.sh is **preview-first** (changes nothing without `--apply`), takes config via flags (`--repo`, `--subscription`, `--app-name`, `--location`, `--state-*`, `--grant-uaa`, `--environment-subject`, `--registry`, `--key-vault`, `--smoke-url`, `--reviewers`, `--skip-github`), and reads **provider-key secrets from like-named environment variables** — so secrets must travel as env, not args.

---

## Task 1: Pure scaffold command builder

**Files:**
- Modify: `installer/core.py` (after `build_step_command`, ~line 262)
- Test: `installer/tests/test_scaffold.py` (create)

- [ ] **Step 1: Write the failing test**

Create `installer/tests/test_scaffold.py`:

```python
"""Offline tests for the scaffold-cicd Forge Console integration — no az/gh/network."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from installer import core  # noqa: E402


class TestBuildScaffoldCommand:
    def test_preview_has_no_apply_flag(self):
        cmd = core.build_scaffold_command({"repo": "me/proj"})
        assert cmd[0] == "bash"
        assert cmd[1].endswith("scripts/scaffold-cicd.sh")
        assert "--repo" in cmd and "me/proj" in cmd
        assert "--apply" not in cmd

    def test_apply_flag_when_requested(self):
        cmd = core.build_scaffold_command({"repo": "me/proj", "apply": True})
        assert "--apply" in cmd

    def test_string_flags_only_included_when_set(self):
        cmd = core.build_scaffold_command({
            "repo": "me/proj", "subscription": "sub-123", "location": "westus2",
        })
        assert cmd[cmd.index("--subscription") + 1] == "sub-123"
        assert cmd[cmd.index("--location") + 1] == "westus2"
        assert "--registry" not in cmd  # not provided → absent

    def test_bool_flags(self):
        cmd = core.build_scaffold_command({
            "repo": "me/proj", "grant_uaa": True, "skip_github": True,
            "environment_subject": True,
        })
        assert "--grant-uaa" in cmd
        assert "--skip-github" in cmd
        assert "--environment-subject" in cmd

    def test_invalid_repo_rejected(self):
        with pytest.raises(ValueError, match="OWNER/REPO"):
            core.build_scaffold_command({"repo": "not-a-repo"})

    def test_secrets_never_placed_on_command_line(self):
        # Even if a secret-shaped value sneaks into params, it must not appear as
        # an argument — secrets go via env only (verified at the endpoint layer).
        cmd = core.build_scaffold_command({"repo": "me/proj", "GPT4O_API_KEY": "super-secret"})
        assert "super-secret" not in cmd
        assert "--GPT4O_API_KEY" not in cmd
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest installer/tests/test_scaffold.py::TestBuildScaffoldCommand -v`
Expected: FAIL with `AttributeError: module 'installer.core' has no attribute 'build_scaffold_command'`.

- [ ] **Step 3: Write minimal implementation**

In `installer/core.py`, add near the top with the other imports (if `re` isn't already imported): `import re`. Then add after `build_step_command` (~line 262):

```python
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def build_scaffold_command(params: dict, repo_root: Path = REPO_ROOT) -> list[str]:
    """Map validated UI params to a scripts/scaffold-cicd.sh invocation.

    Preview-first: `--apply` is only added when params['apply'] is truthy.
    Provider-key SECRETS are NOT handled here — they pass via the subprocess
    environment (see /api/scaffold), so nothing secret ever lands on argv."""
    repo = (params.get("repo") or "").strip()
    if repo and not _REPO_RE.match(repo):
        raise ValueError(f"invalid repo (want OWNER/REPO): {repo!r}")

    cmd = ["bash", str((repo_root / "scripts" / "scaffold-cicd.sh").resolve())]

    str_flags = {
        "repo": "--repo", "subscription": "--subscription", "app_name": "--app-name",
        "location": "--location", "state_rg": "--state-rg",
        "state_account": "--state-account", "state_container": "--state-container",
        "registry": "--registry", "key_vault": "--key-vault",
        "smoke_url": "--smoke-url", "reviewers": "--reviewers",
    }
    for key, flag in str_flags.items():
        val = (params.get(key) or "").strip()
        if val:
            cmd += [flag, val]

    bool_flags = {
        "grant_uaa": "--grant-uaa", "environment_subject": "--environment-subject",
        "skip_github": "--skip-github", "apply": "--apply",
    }
    for key, flag in bool_flags.items():
        if params.get(key):
            cmd.append(flag)

    return cmd
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest installer/tests/test_scaffold.py::TestBuildScaffoldCommand -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add installer/core.py installer/tests/test_scaffold.py
git commit -m "feat(installer): pure build_scaffold_command (turnkey scaffold-3)"
```

---

## Task 2: Runner env passthrough

**Files:**
- Modify: `installer/core.py` (`Runner.start`, `Runner._execute`, `STEP_TIMEOUTS`)
- Test: `installer/tests/test_scaffold.py`

- [ ] **Step 1: Write the failing test**

Append to `installer/tests/test_scaffold.py`:

```python
import time


def _wait_done(run, timeout=10.0):
    deadline = time.time() + timeout
    while run.status == "running" and time.time() < deadline:
        time.sleep(0.02)
    return run


class TestRunnerEnv:
    def test_env_reaches_subprocess(self):
        runner = core.Runner()
        run = runner.start(
            "scaffold",
            ["sh", "-c", 'printf %s "$SCAFFOLD_ENV_TEST"'],
            env={"SCAFFOLD_ENV_TEST": "envvalue"},
        )
        _wait_done(run)
        assert run.status == "succeeded"
        assert any("envvalue" in line for line in run.lines)

    def test_no_env_still_runs(self):
        runner = core.Runner()
        run = runner.start("scaffold", ["sh", "-c", "printf ok"])
        _wait_done(run)
        assert run.status == "succeeded"
        assert any("ok" in line for line in run.lines)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest installer/tests/test_scaffold.py::TestRunnerEnv -v`
Expected: FAIL — `test_env_reaches_subprocess` fails because `Runner.start` doesn't accept `env=` yet (`TypeError: start() got an unexpected keyword argument 'env'`).

- [ ] **Step 3: Add the env parameter**

In `installer/core.py`, change `Runner.start` to accept and forward `env`:

```python
    def start(self, step: str, cmd: list[str], cwd: Path = REPO_ROOT,
              timeout: Optional[float] = None, env: Optional[dict] = None) -> StepRun:
        with self._lock:
            if self.busy():
                raise RuntimeError(f"a step is already running: {self.current.step}")
            run = StepRun(step=step)
            self.current = run
        if timeout is None:
            timeout = STEP_TIMEOUTS.get(step, DEFAULT_STEP_TIMEOUT)
        thread = threading.Thread(target=self._execute, args=(run, cmd, cwd, timeout, env), daemon=True)
        thread.start()
        return run
```

Change `Runner._execute`'s signature and the `Popen` env to merge `env`:

```python
    def _execute(self, run: StepRun, cmd: list[str], cwd: Path,
                 timeout: Optional[float] = None, env: Optional[dict] = None) -> None:
        self._emit(run, f"$ {' '.join(cmd)}")
        timer = None
        timed_out = threading.Event()
        proc_env = {**os.environ, "TF_IN_AUTOMATION": "1"}
        if env:
            proc_env.update(env)
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env=proc_env,
            )
```

(The rest of `_execute` — watchdog timer, read loop, status — is unchanged.)

Add a `scaffold` timeout in `STEP_TIMEOUTS`:

```python
    "scaffold": 900,
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest installer/tests/test_scaffold.py::TestRunnerEnv -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Run the full installer suite (no regressions)**

Run: `python -m pytest installer/tests -q`
Expected: PASS — existing core/detect-destroy/smoke tests plus the new scaffold tests.

- [ ] **Step 6: Commit**

```bash
git add installer/core.py installer/tests/test_scaffold.py
git commit -m "feat(installer): Runner env passthrough + scaffold timeout (turnkey scaffold-3)"
```

---

## Task 3: /api/scaffold endpoint + apply gate

**Files:**
- Modify: `installer/app.py` (new model + constant + endpoint, near `run_step`)
- Test: `installer/tests/test_scaffold.py`

- [ ] **Step 1: Write the failing test**

Append to `installer/tests/test_scaffold.py`:

```python
from fastapi.testclient import TestClient

from installer import app as appmod  # noqa: E402


def _client_and_headers():
    return TestClient(appmod.app), {"x-forge-token": appmod.SESSION_TOKEN}


class TestScaffoldEndpoint:
    def test_preview_wires_command_and_env(self, monkeypatch):
        captured = {}

        def fake_start(step, cmd, cwd=appmod.core.REPO_ROOT, timeout=None, env=None):
            captured.update(step=step, cmd=cmd, env=env)
            run = appmod.core.StepRun(step=step)
            return run

        monkeypatch.setattr(appmod.runner, "start", fake_start)
        client, headers = _client_and_headers()
        resp = client.post("/api/scaffold", headers=headers, json={
            "params": {"repo": "me/proj"},
            "secrets": {"GPT4O_API_KEY": "super-secret"},
        })
        assert resp.status_code == 200
        assert resp.json()["preview"] is True
        assert "--apply" not in captured["cmd"]
        assert "super-secret" not in captured["cmd"]          # secret not on argv
        assert captured["env"]["GPT4O_API_KEY"] == "super-secret"  # secret via env

    def test_apply_requires_confirmation_token(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("runner.start must not be called without confirmation")

        monkeypatch.setattr(appmod.runner, "start", boom)
        client, headers = _client_and_headers()
        resp = client.post("/api/scaffold", headers=headers, json={
            "params": {"repo": "me/proj"}, "apply": True,
        })
        assert resp.status_code == 428
        assert appmod.SCAFFOLD_APPLY_TOKEN in resp.json()["detail"]

    def test_apply_runs_with_correct_token(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            appmod.runner, "start",
            lambda step, cmd, cwd=appmod.core.REPO_ROOT, timeout=None, env=None:
                (captured.update(cmd=cmd) or appmod.core.StepRun(step=step)),
        )
        client, headers = _client_and_headers()
        resp = client.post("/api/scaffold", headers=headers, json={
            "params": {"repo": "me/proj"}, "apply": True,
            "confirm": appmod.SCAFFOLD_APPLY_TOKEN,
        })
        assert resp.status_code == 200
        assert "--apply" in captured["cmd"]

    def test_invalid_repo_returns_422(self, monkeypatch):
        monkeypatch.setattr(appmod.runner, "start", lambda *a, **k: None)
        client, headers = _client_and_headers()
        resp = client.post("/api/scaffold", headers=headers, json={"params": {"repo": "bad"}})
        assert resp.status_code == 422

    def test_requires_session_token(self):
        client, _ = _client_and_headers()
        resp = client.post("/api/scaffold", json={"params": {"repo": "me/proj"}})
        assert resp.status_code == 401
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest installer/tests/test_scaffold.py::TestScaffoldEndpoint -v`
Expected: FAIL — the `/api/scaffold` route returns 404 (not defined) / `appmod.SCAFFOLD_APPLY_TOKEN` is missing.

- [ ] **Step 3: Add the endpoint**

In `installer/app.py`, after the `run_step` handler, add:

```python
SCAFFOLD_APPLY_TOKEN = "scaffold-apply"


class ScaffoldBody(BaseModel):
    params: dict = {}
    secrets: dict[str, str] = {}
    apply: bool = False
    confirm: str = ""


@app.post("/api/scaffold")
def scaffold(body: ScaffoldBody, request: Request) -> dict:
    """Run scripts/scaffold-cicd.sh (CI/CD one-time setup), streamed.

    Preview-first: without apply it changes nothing. An apply mutates Azure
    identity/RBAC and GitHub repo config, so it requires a distinct typed token.
    Provider-key secrets pass via the subprocess environment, never on argv."""
    _guard(request)
    if body.apply and body.confirm != SCAFFOLD_APPLY_TOKEN:
        raise HTTPException(
            428,
            f"type '{SCAFFOLD_APPLY_TOKEN}' to run scaffold with --apply "
            "(it mutates Azure identity/RBAC and GitHub config)",
        )
    params = {**body.params, "apply": body.apply}
    try:
        cmd = core.build_scaffold_command(params)
    except ValueError as e:
        raise HTTPException(422, str(e))
    env = {k: str(v) for k, v in (body.secrets or {}).items() if v}
    try:
        run = runner.start("scaffold", cmd, env=env)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"started": run.step, "preview": not body.apply}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest installer/tests/test_scaffold.py::TestScaffoldEndpoint -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add installer/app.py installer/tests/test_scaffold.py
git commit -m "feat(installer): /api/scaffold endpoint with preview-first apply gate (turnkey scaffold-3)"
```

---

## Task 4: CI/CD Setup UI page

**Files:**
- Modify: `installer/static/index.html`

The console is a single static page that drives the API + the `/api/stream` SSE channel. This task adds a "CI/CD Setup" section following the existing section/tab pattern. HTML isn't unit-tested; verification is manual.

- [ ] **Step 1: Read the existing UI pattern**

Run: `grep -n 'api/run\|api/stream\|x-forge-token\|<section\|data-tab\|addEventListener' installer/static/index.html | head -40`
Read enough around an existing action (e.g. the deploy "Run" button) to match its fetch + token + streaming wiring.

- [ ] **Step 2: Add the CI/CD Setup section**

Insert a new section matching the page's existing markup style (reuse the existing CSS classes and the same token/stream helpers). The form collects the non-secret inputs and the provider-key secrets, then POSTs `/api/scaffold`:

```html
<section id="cicd-setup">
  <h2>CI/CD Setup</h2>
  <p>One-time setup for the reference deploy pipeline: an Entra OIDC app, the
     Terraform state backend, and GitHub variables/secrets + the
     <code>deploy-destroy</code> approval environment. <strong>Preview first;</strong>
     apply mutates Azure identity/RBAC and GitHub config.</p>

  <label>GitHub repo (OWNER/REPO) <input id="sc-repo" placeholder="me/AzureAgentForge"></label>
  <label>Subscription ID <input id="sc-subscription" placeholder="(current az account)"></label>
  <label>App name <input id="sc-app-name" placeholder="aaf-deploy"></label>
  <label>Location <input id="sc-location" placeholder="eastus"></label>
  <label><input type="checkbox" id="sc-grant-uaa"> Grant User Access Administrator</label>
  <label><input type="checkbox" id="sc-skip-github"> Azure only (skip GitHub)</label>

  <fieldset>
    <legend>Provider secrets (sent via env, never logged)</legend>
    <label>GPT4O_API_KEY <input id="sc-sec-gpt4o" type="password"></label>
    <label>AI_FOUNDRY_API_KEY <input id="sc-sec-foundry" type="password"></label>
  </fieldset>

  <button id="sc-preview">Preview</button>
  <button id="sc-apply">Apply…</button>
  <pre id="sc-output"></pre>
</section>

<script>
(function () {
  function scaffoldBody(apply, confirm) {
    const params = {
      repo: document.getElementById("sc-repo").value,
      subscription: document.getElementById("sc-subscription").value,
      app_name: document.getElementById("sc-app-name").value,
      location: document.getElementById("sc-location").value,
      grant_uaa: document.getElementById("sc-grant-uaa").checked,
      skip_github: document.getElementById("sc-skip-github").checked,
    };
    const secrets = {};
    const g = document.getElementById("sc-sec-gpt4o").value;
    const f = document.getElementById("sc-sec-foundry").value;
    if (g) secrets.GPT4O_API_KEY = g;
    if (f) secrets.AI_FOUNDRY_API_KEY = f;
    return { params, secrets, apply, confirm };
  }

  async function runScaffold(apply) {
    let confirm = "";
    if (apply) {
      confirm = window.prompt("Apply mutates Azure identity/RBAC and GitHub config.\n" +
                              "Type 'scaffold-apply' to proceed:");
      if (confirm !== "scaffold-apply") return;
    }
    // TOKEN and streamSubscribe are the page's existing session-token + SSE helpers.
    const resp = await fetch("/api/scaffold", {
      method: "POST",
      headers: { "content-type": "application/json", "x-forge-token": TOKEN },
      body: JSON.stringify(scaffoldBody(apply, confirm)),
    });
    if (!resp.ok) {
      document.getElementById("sc-output").textContent =
        "error: " + (await resp.text());
      return;
    }
    streamSubscribe("sc-output"); // reuse the existing SSE stream consumer
  }

  document.getElementById("sc-preview").addEventListener("click", () => runScaffold(false));
  document.getElementById("sc-apply").addEventListener("click", () => runScaffold(true));
})();
</script>
```

Adapt `TOKEN` and `streamSubscribe` to the page's actual helper names (found in Step 1). If the page renders output by reading `/api/stream` into a specific element, point the consumer at `#sc-output`.

- [ ] **Step 3: Manual verification**

Run the console and exercise the page:

```bash
python -m installer   # or the project's documented `./forge` launch
```

Then in the printed URL: open **CI/CD Setup**, fill the repo, click **Preview** — confirm the streamed output shows the scaffold plan and that no secret values appear in the streamed `$ …` command line. (Do not click Apply unless you intend to create real Azure/GitHub resources.)

- [ ] **Step 4: Commit**

```bash
git add installer/static/index.html
git commit -m "feat(installer): CI/CD Setup page (turnkey scaffold-3)"
```

---

## Self-Review

**Spec coverage** (against §2.3 of the v1.3 design):
- Forge Console page that streams `scaffold-cicd.sh` → Tasks 3 + 4. ✅
- Inputs collected in the UI; secrets via stdin/env not echoed → Task 1 (secrets excluded from argv) + Task 3 (secrets via env) + Task 4 (password inputs). ✅
- Same streamed-subprocess pattern as init→validate→plan→apply → reuses `Runner` + `/api/stream` (Tasks 2–4). ✅
- Offline handler unit tests (command construction + validation) → Tasks 1–3. ✅
- Optional connect-`<surface>` helper → out of scope for this plan (noted; a follow-on).

**Placeholder scan:** none in the testable code. The UI task (Task 4) intentionally adapts to the page's existing `TOKEN`/`streamSubscribe` helpers discovered in Step 1 — that's a real instruction, not a placeholder, because the exact helper names live in the page being modified.

**Type consistency:** `build_scaffold_command(params) -> list[str]` is used identically in Task 1 (tests) and Task 3 (endpoint). `Runner.start(..., env=None)` signature matches between Task 2 (definition) and Task 3 (call + the endpoint test's `fake_start`). `SCAFFOLD_APPLY_TOKEN` is referenced consistently in the endpoint and its tests. `StepRun(step=...)` construction in the tests matches the dataclass in `core.py`.
