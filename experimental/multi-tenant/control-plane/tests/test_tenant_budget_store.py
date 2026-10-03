"""Contract tests for durable tenant budget enforcement."""

from decimal import Decimal
from pathlib import Path

import pytest

from tenant_budget import BudgetDecision, BudgetMode
from tenant_budget_store import BudgetStoreError, PostgresTenantBudget
from user_tokens import Principal


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.last = None

    def execute(self, query, params=()):
        self.conn.queries.append(" ".join(query.split()))
        self.last = (query, params)
        if self.conn.fail_on and self.conn.fail_on in query:
            raise RuntimeError("database details must stay internal")

    def fetchone(self):
        query, params = self.last
        if "FROM tenant_budget_reservations" in query:
            reservation = self.conn.reservations.get((params[0], params[1]))
            if reservation is None:
                return None
            return (
                reservation[1], reservation[3], reservation[4], reservation[5],
                reservation[6], reservation[7], reservation[8], reservation[9],
                reservation[10],
            )
        if "FROM tenant_budget_events" in query:
            for event in self.conn.events:
                if event[0] == params[0] and event[2] == params[1]:
                    return (event[1], event[3], event[4], event[5], event[6], event[7])
            return None
        if "daily_budget_cap" in query:
            cap = self.conn.tenants.get(params[0])
            return (cap,) if cap is not None else None
        if "spent_usd" in query:
            spent = self.conn.spent.get((params[0], params[1]))
            return (spent,) if spent is not None else None
        return None

    def fetchall(self):
        query, params = self.last
        if "FROM tenant_budget_reservations" in query:
            tenant_id, day, status, _status_again, limit = params
            rows = [
                reservation
                for reservation in self.conn.reservations.values()
                if reservation[0] == tenant_id
                and str(reservation[1]) == day
                and (status is None or reservation[10] == status)
            ]
            return [
                (*row, "2026-08-15 10:00:00+00:00", "2026-08-15 10:05:00+00:00")
                for row in rows[:limit]
            ]
        if "FROM tenant_budget_events" in query:
            return [event for event in self.conn.events if event[0] == params[0] and event[1] == params[1]]
        return []

    def close(self):
        pass


class FakeConnection:
    def __init__(self):
        self.tenants = {"t1": Decimal("1.00")}
        self.spent = {}
        self.events = []
        self.reservations = {}
        self.queries = []
        self.fail_on = None
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1
        for query in reversed(self.queries):
            if query.startswith("INSERT INTO tenant_budget_daily"):
                # The store passes the projected value as the third parameter;
                # inspect the cursor state through the test-only connection.
                pass

    def rollback(self):
        self.rollbacks += 1


class RecordingCursor(FakeCursor):
    def execute(self, query, params=()):
        super().execute(query, params)
        if query.lstrip().startswith("INSERT INTO tenant_budget_daily"):
            self.conn.spent[(params[0], params[1])] = Decimal(str(params[2]))
        if query.lstrip().startswith("INSERT INTO tenant_budget_events"):
            self.conn.events.append((
                params[0], params[1], params[2], params[3], params[4],
                params[5], params[6], params[7],
            ))
        if query.lstrip().startswith("INSERT INTO tenant_budget_reservations"):
            self.conn.reservations[(params[0], params[2])] = (
                params[0], params[1], params[2], params[3], params[4], None,
                params[5], params[6], params[7], params[8], params[9],
            )
        if query.lstrip().startswith("UPDATE tenant_budget_daily"):
            self.conn.spent[(params[1], str(params[2]))] = Decimal(str(params[0]))
        if query.lstrip().startswith("UPDATE tenant_budget_reservations") and "status = 'settled'" in query:
            current = self.conn.reservations[(params[3], params[4])]
            self.conn.reservations[(params[3], params[4])] = (
                current[0], current[1], current[2], current[3], current[4],
                params[0], current[6], current[7], params[1], params[2], "settled",
            )
        if query.lstrip().startswith("UPDATE tenant_budget_reservations") and "status = 'released'" in query:
            current = self.conn.reservations[(params[1], params[2])]
            self.conn.reservations[(params[1], params[2])] = (
                current[0], current[1], current[2], current[3], current[4],
                current[5], current[6], current[7], params[0], current[9], "released",
            )


class RecordingConnection(FakeConnection):
    def cursor(self):
        return RecordingCursor(self)


def test_charge_persists_and_snapshot_survives_new_store():
    conn = RecordingConnection()
    store = PostgresTenantBudget(conn)

    result = store.charge("t1", "0.40", "2026-08-15")

    assert result.decision is BudgetDecision.ALLOW
    assert result.spent == Decimal("0.40")
    assert store.snapshot("t1", "2026-08-15").remaining == Decimal("0.60")
    assert conn.commits == 2


def test_blocked_charge_is_not_recorded():
    conn = RecordingConnection()
    conn.spent[("t1", "2026-08-15")] = Decimal("0.80")

    result = PostgresTenantBudget(conn).charge("t1", "0.30", "2026-08-15")

    assert result.decision is BudgetDecision.BLOCK
    assert result.spent == Decimal("0.80")
    assert conn.spent[("t1", "2026-08-15")] == Decimal("0.80")


def test_blocked_decision_is_audited_without_changing_spend():
    conn = RecordingConnection()
    conn.spent[("t1", "2026-08-15")] = Decimal("0.80")

    PostgresTenantBudget(conn).charge("t1", "0.30", "2026-08-15", request_id="blocked-1")

    events = PostgresTenantBudget(conn).events("t1", "2026-08-15")
    assert len(events) == 1
    assert events[0].request_id == "blocked-1"
    assert events[0].decision is BudgetDecision.BLOCK
    assert events[0].spent_before == Decimal("0.80")
    assert events[0].spent_after == Decimal("0.80")
    assert conn.spent[("t1", "2026-08-15")] == Decimal("0.80")


def test_request_id_makes_retries_idempotent():
    conn = RecordingConnection()
    store = PostgresTenantBudget(conn)

    first = store.charge("t1", "0.40", "2026-08-15", request_id="retry-1")
    second = store.charge("t1", "0.40", "2026-08-15", request_id="retry-1")

    assert first == second
    assert conn.spent[("t1", "2026-08-15")] == Decimal("0.40")
    assert len(conn.events) == 1


def test_reservation_holds_budget_for_a_stable_tenant_caller_request():
    conn = RecordingConnection()

    reservation = PostgresTenantBudget(conn).reserve(
        "t1",
        "agent-7",
        "router-request-1",
        "0.40",
        "2026-08-15",
    )

    assert reservation.tenant_id == "t1"
    assert reservation.caller_id == "agent-7"
    assert reservation.request_id == "router-request-1"
    assert reservation.status == "pending"
    assert reservation.decision is BudgetDecision.ALLOW
    assert reservation.reserved == Decimal("0.40")
    assert reservation.spent_after == Decimal("0.40")
    assert conn.spent[("t1", "2026-08-15")] == Decimal("0.40")


def test_settlement_replaces_reservation_with_actual_provider_cost():
    conn = RecordingConnection()
    store = PostgresTenantBudget(conn)
    store.reserve("t1", "agent-7", "router-request-2", "0.40", "2026-08-15")

    settled = store.settle("t1", "router-request-2", "0.15")

    assert settled.status == "settled"
    assert settled.actual == Decimal("0.15")
    assert settled.spent_after == Decimal("0.15")
    assert conn.spent[("t1", "2026-08-15")] == Decimal("0.15")


def test_release_returns_failed_provider_reservation_to_available_budget():
    conn = RecordingConnection()
    store = PostgresTenantBudget(conn)
    store.reserve("t1", "agent-7", "router-request-3", "0.40", "2026-08-15")

    released = store.release("t1", "router-request-3")

    assert released.status == "released"
    assert released.actual is None
    assert released.spent_after == Decimal("0.00")
    assert conn.spent[("t1", "2026-08-15")] == Decimal("0.00")


def test_pending_reservations_are_discoverable_for_operator_reconciliation():
    conn = RecordingConnection()
    store = PostgresTenantBudget(conn)
    store.reserve("t1", "agent-7", "pending-1", "0.40", "2026-08-15")
    store.reserve("t1", "agent-8", "released-1", "0.20", "2026-08-15")
    store.release("t1", "released-1")

    reservations = store.reservations(
        "t1", "2026-08-15", status="pending", limit=25
    )

    assert [reservation.request_id for reservation in reservations] == ["pending-1"]
    assert reservations[0].caller_id == "agent-7"
    assert reservations[0].created_at == "2026-08-15 10:00:00+00:00"
    assert reservations[0].updated_at == "2026-08-15 10:05:00+00:00"
    query = conn.queries[-1]
    assert "FROM tenant_budget_reservations" in query
    assert "WHERE tenant_id = %s AND day = %s" in query
    assert "ORDER BY created_at DESC" in query
    assert "LIMIT %s" in query


def test_reusing_request_id_for_a_different_charge_fails_closed():
    conn = RecordingConnection()
    store = PostgresTenantBudget(conn)
    store.charge("t1", "0.40", "2026-08-15", request_id="retry-1")

    with pytest.raises(BudgetStoreError, match="budget request id already used"):
        store.charge("t1", "0.50", "2026-08-15", request_id="retry-1")

    assert conn.spent[("t1", "2026-08-15")] == Decimal("0.40")


@pytest.mark.parametrize("mode, expected", [
    (BudgetMode.WARN, BudgetDecision.WARN),
    (BudgetMode.OFF, BudgetDecision.ALLOW),
])
def test_warn_and_off_record_over_cap_charge(mode, expected):
    conn = RecordingConnection()
    result = PostgresTenantBudget(conn, mode=mode).charge("t1", "1.20", "2026-08-15")

    assert result.decision is expected
    assert result.spent == Decimal("1.20")


def test_unknown_tenant_fails_closed_without_leaking_database_error():
    conn = RecordingConnection()

    with pytest.raises(BudgetStoreError, match="tenant budget is not configured"):
        PostgresTenantBudget(conn).charge("missing", "0.01", "2026-08-15")

    assert conn.rollbacks == 1


@pytest.mark.parametrize("amount", [-1, True, float("inf"), "not-money"])
def test_invalid_charge_is_rejected_before_database_access(amount):
    conn = RecordingConnection()

    with pytest.raises(ValueError):
        PostgresTenantBudget(conn).charge("t1", amount, "2026-08-15")

    assert conn.queries == []


@pytest.mark.parametrize("day", ["", "yesterday", "2026-02-30"])
def test_invalid_day_is_rejected_before_database_access(day):
    conn = RecordingConnection()

    with pytest.raises(ValueError, match="day"):
        PostgresTenantBudget(conn).charge("t1", "0.10", day)

    assert conn.queries == []


def test_principal_binds_rls_context_before_charging():
    conn = RecordingConnection()
    tenant_id = "00000000-0000-0000-0000-000000000001"
    conn.tenants[tenant_id] = Decimal("1.00")
    principal = Principal("user-1", tenant_id, "member")

    PostgresTenantBudget(conn, principal=principal).charge(tenant_id, "0.10", "2026-08-15")

    assert conn.queries[0] == "SELECT set_config('app.tenant_id', %s, true)"


def test_principal_cannot_charge_another_tenant():
    conn = RecordingConnection()

    with pytest.raises(BudgetStoreError, match="does not match requested tenant"):
        PostgresTenantBudget(
            conn,
            principal=Principal("user-1", "t1", "member"),
        ).charge("t2", "0.10", "2026-08-15")

    assert conn.queries == []


def test_sql_contract_serializes_tenant_and_daily_rows():
    conn = RecordingConnection()
    PostgresTenantBudget(conn).charge("t1", "0.10", "2026-08-15")

    assert any("WHERE id = %s FOR UPDATE" in q for q in conn.queries)
    assert any("WHERE tenant_id = %s AND day = %s FOR UPDATE" in q for q in conn.queries)
    assert any("ON CONFLICT (tenant_id, day) DO UPDATE" in q for q in conn.queries)
    assert any("INSERT INTO tenant_budget_events" in q for q in conn.queries)


def test_event_query_is_bounded_and_scoped_to_tenant_day():
    conn = RecordingConnection()

    PostgresTenantBudget(conn).events("t1", "2026-08-15", limit=25)

    query = conn.queries[-1]
    assert "FROM tenant_budget_events" in query
    assert "WHERE tenant_id = %s AND day = %s" in query
    assert "ORDER BY created_at DESC" in query
    assert "LIMIT %s" in query


def test_schema_contains_auditable_events_and_rls_backstop():
    schema = (Path(__file__).parents[1] / "init_db.sql").read_text()

    assert "CREATE TABLE tenant_budget_events" in schema
    assert "CREATE TABLE tenant_budget_reservations" in schema
    assert "UNIQUE (tenant_id, request_id)" in schema
    assert "decision IN ('allow', 'warn', 'block')" in schema
    assert "status IN ('pending', 'settled', 'released', 'blocked')" in schema
    assert "CREATE POLICY tenant_budget_events_tenant_isolation" in schema
    assert "CREATE POLICY tenant_budget_reservations_tenant_isolation" in schema
