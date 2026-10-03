"""Tenant onboarding — contract validation + step planning, offline."""

import pytest

from onboarding import (
    JsonOnboardingStateStore,
    MemoryOnboardingStateStore,
    OnboardingExecutionError,
    OnboardingRollbackError,
    OnboardingStateError,
    OnboardingRun,
    OnboardingStep,
    OnboardingExecutor,
    PostgresOnboardingStateStore,
    plan_provisioning,
    validate_contract,
)
from provision_tenant import build_tenant_payload


def _contract(**over):
    c = {
        "slug": "acme",
        "display_name": "Acme Co",
        "primary_email": "ops@acme.test",
        "vertical": "field-service",
        "pack": "example-fieldservice",
        "daily_budget_cap": 5.00,
        "users": [
            {"email": "boss@acme.test", "role": "owner"},
            {"email": "tech@acme.test", "role": "member"},
        ],
    }
    c.update(over)
    return c


def test_valid_contract_has_no_errors():
    assert validate_contract(_contract()) == []


def test_plan_orders_steps_tenant_budget_workspace_users_pack():
    steps = plan_provisioning(_contract())
    actions = [s.action for s in steps]
    assert actions == [
        "create_tenant", "set_budget", "provision_workspace",
        "create_user", "create_user", "enable_pack",
    ]
    assert isinstance(steps[0], OnboardingStep)


def test_owner_is_provisioned_before_member():
    steps = plan_provisioning(_contract())
    user_steps = [s for s in steps if s.action == "create_user"]
    assert user_steps[0].detail["role"] == "owner"
    assert user_steps[1].detail["role"] == "member"


def test_missing_required_fields_reported():
    errs = validate_contract({"users": []})
    assert any("slug is required" in e for e in errs)
    assert any("daily_budget_cap" in e for e in errs)
    assert any("at least one user" in e for e in errs)


def test_bad_email_and_budget_and_role():
    errs = validate_contract(_contract(
        primary_email="not-an-email",
        daily_budget_cap=0,
        users=[{"email": "x@y.z", "role": "superuser"}],
    ))
    assert any("primary_email must be" in e for e in errs)
    assert any("daily_budget_cap must be a positive" in e for e in errs)
    assert any("role must be one of" in e for e in errs)


def test_no_owner_is_rejected():
    errs = validate_contract(_contract(
        users=[{"email": "a@b.c", "role": "member"}],
    ))
    assert any("owner" in e for e in errs)


def test_plan_raises_on_invalid_contract():
    with pytest.raises(ValueError) as exc:
        plan_provisioning(_contract(slug="", daily_budget_cap=-1))
    msg = str(exc.value)
    assert "slug is required" in msg
    assert "daily_budget_cap" in msg


def test_budget_bool_is_not_a_valid_cap():
    # True is an int subclass — must not sneak through as a cap of 1
    errs = validate_contract(_contract(daily_budget_cap=True))
    assert any("daily_budget_cap must be a positive" in e for e in errs)


class RecordingDriver:
    def __init__(self, *, fail_on=None, fail_compensation=None):
        self.fail_on = fail_on
        self.fail_compensation = set(fail_compensation or ())
        self.applied = []
        self.compensated = []

    def apply(self, step, *, idempotency_key, context):
        self.applied.append((step.action, idempotency_key, context["run_id"]))
        if step.action == self.fail_on:
            raise RuntimeError("provider details must stay internal")
        return {"resource": step.action}

    def compensate(self, step, *, output, context):
        self.compensated.append((step.action, output["resource"], context["run_id"]))
        if step.action in self.fail_compensation:
            raise RuntimeError("provider details must stay internal")


def test_executor_checkpoints_each_step_and_is_idempotent_after_completion():
    store = MemoryOnboardingStateStore()
    driver = RecordingDriver()
    executor = OnboardingExecutor(store, driver)

    run = executor.start(_contract(), run_id="run-1")

    assert run.status == "complete"
    assert [action for action, _, _ in driver.applied] == [
        "create_tenant", "set_budget", "provision_workspace",
        "create_user", "create_user", "enable_pack",
    ]
    assert len({key for _, key, _ in driver.applied}) == len(driver.applied)
    assert executor.resume("run-1") == run
    assert len(driver.applied) == 6


def test_failed_step_can_resume_without_repeating_acknowledged_steps():
    store = MemoryOnboardingStateStore()
    driver = RecordingDriver(fail_on="provision_workspace")
    executor = OnboardingExecutor(store, driver)

    with pytest.raises(OnboardingExecutionError) as exc:
        executor.start(_contract(), run_id="run-2")

    assert exc.value.run_id == "run-2"
    failed = store.load("run-2")
    assert failed is not None
    assert failed.status == "failed"
    assert [item.index for item in failed.completed] == [0, 1]
    assert failed.error == "step provision_workspace failed"

    driver.fail_on = None
    resumed = executor.resume("run-2")

    assert resumed.status == "complete"
    assert [action for action, _, _ in driver.applied].count("create_tenant") == 1
    assert [action for action, _, _ in driver.applied].count("set_budget") == 1
    assert [action for action, _, _ in driver.applied].count("provision_workspace") == 2


def test_rollback_compensates_completed_steps_in_reverse_order():
    store = MemoryOnboardingStateStore()
    driver = RecordingDriver(fail_on="provision_workspace")
    executor = OnboardingExecutor(store, driver)

    with pytest.raises(OnboardingExecutionError):
        executor.start(_contract(), run_id="run-3")

    rolled_back = executor.rollback("run-3")

    assert rolled_back.status == "rolled_back"
    assert [action for action, _, _ in driver.compensated] == ["set_budget", "create_tenant"]
    assert rolled_back.compensated == (0, 1)


def test_rollback_attempts_every_step_and_can_resume_after_compensation_failure():
    store = MemoryOnboardingStateStore()
    driver = RecordingDriver(
        fail_on="provision_workspace",
        fail_compensation={"set_budget"},
    )
    executor = OnboardingExecutor(store, driver)

    with pytest.raises(OnboardingExecutionError):
        executor.start(_contract(), run_id="run-4")
    with pytest.raises(OnboardingRollbackError) as exc:
        executor.rollback("run-4")

    assert exc.value.actions == ("set_budget",)
    failed = store.load("run-4")
    assert failed is not None
    assert failed.status == "rollback_failed"
    assert failed.compensated == (0,)
    assert [action for action, _, _ in driver.compensated] == [
        "set_budget", "create_tenant",
    ]

    driver.fail_compensation.clear()
    completed = executor.rollback("run-4")
    assert completed.status == "rolled_back"
    assert completed.compensated == (0, 1)
    assert [action for action, _, _ in driver.compensated][-1] == "set_budget"


def test_json_state_store_allows_a_new_executor_to_resume(tmp_path):
    state_path = tmp_path / "onboarding.json"
    first_store = JsonOnboardingStateStore(state_path)
    first_driver = RecordingDriver(fail_on="provision_workspace")

    with pytest.raises(OnboardingExecutionError):
        OnboardingExecutor(first_store, first_driver).start(_contract(), run_id="run-5")

    second_driver = RecordingDriver()
    resumed = OnboardingExecutor(
        JsonOnboardingStateStore(state_path), second_driver
    ).resume("run-5")

    assert resumed.status == "complete"
    assert [action for action, _, _ in second_driver.applied] == [
        "provision_workspace", "create_user", "create_user", "enable_pack",
    ]


def test_existing_run_rejects_a_different_contract():
    store = MemoryOnboardingStateStore()
    executor = OnboardingExecutor(store, RecordingDriver())
    executor.start(_contract(), run_id="run-6")

    with pytest.raises(OnboardingStateError, match="different contract"):
        executor.start(_contract(display_name="Other Co"), run_id="run-6")


class FakeDbCursor:
    def __init__(self, conn):
        self.conn = conn
        self.last = None

    def execute(self, query, params=()):
        self.conn.queries.append(" ".join(query.split()))
        if self.conn.fail:
            raise RuntimeError("database details must stay internal")
        self.last = (query, params)
        if query.lstrip().startswith("INSERT INTO tenant_onboarding_runs"):
            self.conn.rows[params[0]] = params[2]

    def fetchone(self):
        _, params = self.last
        payload = self.conn.rows.get(params[0])
        return (payload,) if payload is not None else None

    def close(self):
        pass


class FakeDbConnection:
    def __init__(self):
        self.rows = {}
        self.queries = []
        self.commits = 0
        self.rollbacks = 0
        self.fail = False

    def cursor(self):
        return FakeDbCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_postgres_state_store_round_trips_and_upserts_checkpoint():
    conn = FakeDbConnection()
    store = PostgresOnboardingStateStore(conn)
    run = OnboardingRun(
        run_id="run-db",
        contract_fingerprint="abc",
        steps=(OnboardingStep("create_tenant", {"slug": "acme"}),),
    )

    store.save(run)
    loaded = store.load("run-db")

    assert loaded == run
    assert conn.commits == 2
    assert any("ON CONFLICT (run_id) DO UPDATE" in query for query in conn.queries)
    assert any("SELECT state_json FROM tenant_onboarding_runs" in query for query in conn.queries)


def test_postgres_state_store_hides_database_errors():
    conn = FakeDbConnection()
    conn.fail = True

    with pytest.raises(OnboardingStateError, match="could not be saved"):
        PostgresOnboardingStateStore(conn).save(
            OnboardingRun("run-db", "abc", (OnboardingStep("x", {}),))
        )

    assert conn.rollbacks == 1


def test_schema_contains_operator_onboarding_state_table():
    from pathlib import Path

    schema = (Path(__file__).parents[1] / "init_db.sql").read_text()
    assert "CREATE TABLE tenant_onboarding_runs" in schema
    assert "state_json jsonb NOT NULL" in schema
    assert "status IN ('pending', 'running', 'failed', 'complete'" in schema


@pytest.mark.parametrize("cap", [0, -1, True, float("inf"), float("nan")])
def test_provisioning_client_rejects_implicit_or_invalid_budget(cap):
    with pytest.raises(ValueError, match="daily_budget_cap"):
        build_tenant_payload(
            slug="acme",
            display_name="Acme",
            email="ops@acme.test",
            daily_budget_cap=cap,
        )


def test_provisioning_client_sends_budget_cap_to_control_plane():
    payload = build_tenant_payload(
        slug="acme",
        display_name="Acme",
        email="ops@acme.test",
        daily_budget_cap=5.0,
    )

    assert payload["daily_budget_cap"] == 5.0
