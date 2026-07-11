"""Escalation SLA auditor — event taxonomy + pure rollup + pure renderers,
SHIP-DARK.

An autonomy story ("green acts, yellow drafts, red goes to a human") is only
credible if the human side of the handoff is fast. This module audits it:
every red/yellow-lane escalation is tracked from `escalation_opened` to
`escalation_acked` (correlated by payload.escalation_id), ack latency is
measured against a per-tenant SLA (platform default when unset), and TTL
expiry (`autonomy_decision: expired`) always counts as a breach AND an
unresolved escalation — a fail-closed approval gate's posture is never
weakened here, only made visible.

No new spine: escalations live entirely in `agent_events` (migration 0001);
the tenant dimension is payload->>'workspace'. Same shape as digest.py /
memory_digest.py: everything in this module is pure and offline-tested; the
SQL fetch lives in db.escalation_sla_rollup and the endpoint in main.py's
/escalation-sla. The auditor never acts — it measures, surfaces, and reports;
it never approves, extends, or re-routes anything.

Retro-compat (approval source): an `autonomy_decision` event IS the
ack+resolution for approval holds — `decision in (approved, approved-safe,
denied)` is treated as acked-and-resolved at payload.latency_ms, and a
decision with no paired `escalation_opened` synthesizes the escalation record
so historical holds are auditable retroactively.

Emitters: this repo's HITL action-approval seam (apps/paperclip/approval.mjs)
ships deliberately unwired and does not emit events yet, so today the
pipeline is offline-tested and dark end to end. When the approval seam (or
any other escalation surface) is wired for real volume, its call sites emit
the taxonomy below — `escalation_payload` is the build-time discipline helper
for them — and the auditor lights up with no further changes.

Honesty bar: renderers never fabricate from missing data — a zero-escalation
window renders "no escalations required your attention", unpaired opens
render as open/breached, never silently dropped.
"""

from __future__ import annotations

import logging
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any

log = logging.getLogger("governor.escalation_sla")

# ---------------------------------------------------------------------------
# Event taxonomy
# ---------------------------------------------------------------------------

EVENT_OPENED = "escalation_opened"
EVENT_ACKED = "escalation_acked"
EVENT_RESOLVED = "escalation_resolved"
# The approval gate's decision event — ack+resolution for the approval
# source, and the retro-audit path (it carries latency_ms).
EVENT_AUTONOMY_DECISION = "autonomy_decision"

ESCALATION_EVENT_TYPES = (EVENT_OPENED, EVENT_ACKED, EVENT_RESOLVED)
# Everything the rollup reads from agent_events.
ROLLUP_EVENT_TYPES = ESCALATION_EVENT_TYPES + (EVENT_AUTONOMY_DECISION,)

LANES = ("red", "yellow")
SOURCE_APPROVAL = "approval"
SOURCE_PRESEND = "presend"
SOURCE_HANDOFF = "handoff"
SOURCES = (SOURCE_APPROVAL, SOURCE_PRESEND, SOURCE_HANDOFF)

# Platform-default ack SLA: per-tenant config, floored by a platform default
# (e.g. ack <= 30 min during business hours).
DEFAULT_SLA_MINUTES = 30

# autonomy_decision payload.decision values that mean a human acted (ack =
# resolution, by construction, for the approval source).
_DECISION_ACK = ("approved", "approved-safe", "denied")
_DECISION_EXPIRED = "expired"


def escalation_payload(
    escalation_id: str,
    *,
    lane: str,
    source: str,
    workspace: str,
    **extra: Any,
) -> dict[str, Any]:
    """Canonical payload builder for the emitting call sites (approval
    hold-create, presend flag, human handoff). Raises on a bad lane/source —
    this is a build-time discipline helper for emitters, not a tolerant
    parser (the rollup side is the tolerant one)."""
    if not escalation_id:
        raise ValueError("escalation_id is required")
    if lane not in LANES:
        raise ValueError(f"lane must be one of {LANES}, got {lane!r}")
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}, got {source!r}")
    return {
        "escalation_id": str(escalation_id),
        "lane": lane,
        "source": source,
        "workspace": workspace,
        **extra,
    }


# ---------------------------------------------------------------------------
# SLA config
# ---------------------------------------------------------------------------


def resolve_sla_config(raw: Any, default_minutes: int = DEFAULT_SLA_MINUTES) -> dict[str, Any]:
    """Coerce a tenant's declared `escalation_sla` (tenant-contract variable)
    into {ack_sla_minutes, business_hours, warnings}. Malformed config ->
    platform default + a warning entry, never a crash."""
    out: dict[str, Any] = {
        "ack_sla_minutes": default_minutes,
        "business_hours": None,
        "warnings": [],
    }
    if raw is None:
        return out
    if isinstance(raw, bool):  # bool is an int subclass; a bare True is junk
        out["warnings"].append(f"malformed escalation_sla {raw!r}; using default {default_minutes}m")
        return out
    if isinstance(raw, (int, float, str)):
        try:
            minutes = int(float(raw))
        except (TypeError, ValueError):
            out["warnings"].append(f"malformed escalation_sla {raw!r}; using default {default_minutes}m")
            return out
        if minutes <= 0:
            out["warnings"].append(f"non-positive escalation_sla {raw!r}; using default {default_minutes}m")
            return out
        out["ack_sla_minutes"] = minutes
        return out
    if isinstance(raw, dict):
        minutes_raw = raw.get("ack_sla_minutes", raw.get("sla_minutes"))
        if minutes_raw is not None:
            nested = resolve_sla_config(minutes_raw, default_minutes)
            out["ack_sla_minutes"] = nested["ack_sla_minutes"]
            out["warnings"].extend(nested["warnings"])
        bh = raw.get("business_hours")
        if bh is not None:
            if _valid_business_hours(bh):
                out["business_hours"] = bh
            else:
                out["warnings"].append(f"malformed business_hours {bh!r}; using calendar clock")
        return out
    out["warnings"].append(f"malformed escalation_sla {raw!r}; using default {default_minutes}m")
    return out


def _valid_business_hours(bh: Any) -> bool:
    if not isinstance(bh, dict):
        return False
    try:
        days = bh.get("days")
        if not isinstance(days, (list, tuple)) or not days:
            return False
        if not all(isinstance(d, int) and 0 <= d <= 6 for d in days):
            return False
        _parse_hhmm(bh["start"])
        _parse_hhmm(bh["end"])
    except (KeyError, TypeError, ValueError):
        return False
    return True


def _parse_hhmm(s: Any) -> dtime:
    h, m = str(s).split(":")
    return dtime(int(h), int(m))


def clock_start(opened_at: datetime, business_hours: dict | None) -> datetime:
    """Business-hours-aware SLA clock start (deliberate v1 simplification):
    an out-of-hours escalation starts its clock at the tenant's next opening.
    No/malformed business_hours -> calendar clock (opened_at). Timezone
    handling is v1 naive: hours are interpreted in opened_at's own timezone."""
    if not _valid_business_hours(business_hours):
        return opened_at
    assert business_hours is not None  # narrowed by the validity check
    days = set(business_hours["days"])
    start = _parse_hhmm(business_hours["start"])
    end = _parse_hhmm(business_hours["end"])
    if opened_at.weekday() in days and start <= opened_at.timetz().replace(tzinfo=None) < end:
        return opened_at
    # Advance to the next opening (bounded scan; days is non-empty so <= 7).
    candidate = opened_at
    for _ in range(8):
        if candidate.weekday() in days:
            opening = candidate.replace(
                hour=start.hour, minute=start.minute, second=0, microsecond=0
            )
            if opening > opened_at:
                return opening
        candidate = (candidate + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    return opened_at  # unreachable with valid config; fail safe to calendar


# ---------------------------------------------------------------------------
# Pairing + rollup (pure; rows come from db.escalation_sla_rollup's fetch)
# ---------------------------------------------------------------------------


def _ts(value: Any) -> datetime | None:
    """Tolerant timestamp coercion: datetime passes through, ISO strings
    parse, junk -> None. Naive datetimes are assumed UTC."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _payload(row: dict) -> dict:
    p = row.get("payload")
    return p if isinstance(p, dict) else {}


def pair_escalations(
    rows: list[dict[str, Any]],
    *,
    business_hours: dict | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Fold a window of agent_events rows (ts, actor_peer, event_type,
    payload) into one record per escalation, correlated by
    (workspace, payload.escalation_id). Pure; tolerant of junk rows.

    - escalation_acked: first ack wins (duplicates idempotent); ack latency
      is measured from the business-hours clock start, falling back to the
      payload's own ack_latency_ms when timestamps are unusable.
    - autonomy_decision approved/approved-safe/denied: backfills ack+resolve
      for approval-source opens (retro-compat) and, with no paired open,
      synthesizes the whole record from latency_ms so historical holds are
      auditable. escalation_id falls back to payload.hold_id.
    - autonomy_decision expired: TTL drop -> breach + unresolved, always.
    - Cross-workspace isolation: tenant A's acks never pair with tenant B's
      opens (keyed by workspace).
    - Acks/resolutions with no open in window are dropped as orphans (the
      open is what the auditor counts; a dangling ack has nothing to age).
    """
    now = now or datetime.now(timezone.utc)
    esc: dict[tuple[str, str], dict[str, Any]] = {}

    def _key(payload: dict) -> tuple[str, str] | None:
        eid = payload.get("escalation_id") or payload.get("hold_id")
        if not eid:
            return None
        return (str(payload.get("workspace") or ""), str(eid))

    # Sort by ts so "first ack wins" is deterministic; junk-ts rows sort last.
    def _sort_key(r: dict) -> Any:
        t = _ts(r.get("ts"))
        return (t is None, t or now)

    for row in sorted(rows, key=_sort_key):
        et = row.get("event_type")
        payload = _payload(row)
        key = _key(payload)
        if key is None:
            continue
        ts = _ts(row.get("ts"))

        if et == EVENT_OPENED:
            if key in esc:
                continue  # duplicate open — first wins
            esc[key] = {
                "escalation_id": key[1],
                "workspace": key[0] or None,
                "lane": payload.get("lane"),
                "source": payload.get("source"),
                "opened_at": ts,
                "acked_at": None,
                "acked_by": None,
                "resolved_at": None,
                "outcome": None,
                "ack_latency_ms": None,
                "expired": False,
            }
        elif et == EVENT_ACKED:
            rec = esc.get(key)
            if rec is None or rec["acked_at"] is not None:
                continue  # orphan ack, or duplicate (first wins)
            rec["acked_at"] = ts
            rec["acked_by"] = payload.get("acked_by") or row.get("actor_peer")
            rec["ack_latency_ms"] = _latency_ms(
                rec["opened_at"], ts, payload.get("ack_latency_ms"), business_hours
            )
        elif et == EVENT_RESOLVED:
            rec = esc.get(key)
            if rec is None or rec["resolved_at"] is not None:
                continue
            rec["resolved_at"] = ts
            rec["outcome"] = payload.get("outcome")
        elif et == EVENT_AUTONOMY_DECISION:
            decision = str(payload.get("decision") or "")
            if decision not in _DECISION_ACK and decision != _DECISION_EXPIRED:
                continue  # autosent etc. — not an escalation outcome
            rec = esc.get(key)
            if rec is None:
                # Historical hold with no escalation_opened emission yet —
                # synthesize so the retro-audit sees it.
                lat = payload.get("latency_ms")
                lat = lat if isinstance(lat, (int, float)) and lat >= 0 else None
                opened = ts - timedelta(milliseconds=lat) if (ts and lat is not None) else ts
                rec = esc[key] = {
                    "escalation_id": key[1],
                    "workspace": key[0] or None,
                    "lane": payload.get("lane") or "yellow",
                    "source": SOURCE_APPROVAL,
                    "opened_at": opened,
                    "acked_at": None,
                    "acked_by": None,
                    "resolved_at": None,
                    "outcome": None,
                    "ack_latency_ms": None,
                    "expired": False,
                    "synthesized": True,
                }
            if decision == _DECISION_EXPIRED:
                # Fail-closed TTL drop: the human never showed up. Breach +
                # unresolved, never an ack.
                rec["expired"] = True
                rec["outcome"] = "expired"
                continue
            if rec["acked_at"] is None:
                rec["acked_at"] = ts
                rec["acked_by"] = payload.get("decided_by") or row.get("actor_peer")
                lat = payload.get("latency_ms")
                if isinstance(lat, (int, float)) and lat >= 0:
                    rec["ack_latency_ms"] = float(lat)
                else:
                    rec["ack_latency_ms"] = _latency_ms(
                        rec["opened_at"], ts, None, business_hours
                    )
            if rec["resolved_at"] is None:
                # decision = ack = resolution for the approval source.
                rec["resolved_at"] = ts
                rec["outcome"] = decision

    return list(esc.values())


def _latency_ms(
    opened_at: datetime | None,
    acked_at: datetime | None,
    payload_latency: Any,
    business_hours: dict | None,
) -> float | None:
    if opened_at is not None and acked_at is not None:
        start = clock_start(opened_at, business_hours)
        return max(0.0, (acked_at - start).total_seconds() * 1000.0)
    if isinstance(payload_latency, (int, float)) and payload_latency >= 0:
        return float(payload_latency)
    return None


def empty_stats(workspace: str | None, window_days: int, sla_minutes: int = DEFAULT_SLA_MINUTES) -> dict[str, Any]:
    """The honest zero: what a window with no escalation events rolls up to.
    Also the fail-open shape db.escalation_sla_rollup returns on a DB hiccup."""
    return {
        "workspace": workspace,
        "window_days": window_days,
        "sla_minutes": sla_minutes,
        "sla_warnings": [],
        "total": 0,
        "acked": 0,
        "resolved": 0,
        "unacked": 0,
        "expired": 0,
        "breaches": 0,
        "all_closed": True,
        "median_ack_ms": None,
        "escalations": [],
    }


def build_sla_stats(
    rows: list[dict[str, Any]],
    *,
    workspace: str | None,
    window_days: int,
    sla_config: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The rollup: pair the window's events, measure ack latencies against
    the SLA, count breaches (late acks + currently-unacked past SLA + every
    TTL expiry), and compute the median ack. Pure; never raises on junk."""
    now = now or datetime.now(timezone.utc)
    cfg = resolve_sla_config(sla_config)
    sla_minutes: int = cfg["ack_sla_minutes"]
    sla_ms = sla_minutes * 60_000.0
    records = pair_escalations(rows, business_hours=cfg["business_hours"], now=now)

    stats = empty_stats(workspace, window_days, sla_minutes)
    stats["sla_warnings"] = cfg["warnings"]
    for w in cfg["warnings"]:
        log.warning("escalation SLA config (workspace=%s): %s", workspace, w)

    latencies: list[float] = []
    for rec in sorted(records, key=lambda r: (r["opened_at"] is None, r["opened_at"] or now)):
        stats["total"] += 1
        acked = rec["acked_at"] is not None
        resolved = rec["resolved_at"] is not None
        if resolved and not rec["expired"]:
            stats["resolved"] += 1
        if rec["expired"]:
            # Expiry is a breach AND an unresolved escalation, always.
            breach = True
            age_ms = _age_ms(rec, now, cfg["business_hours"])
            stats["expired"] += 1
        elif acked:
            stats["acked"] += 1
            lat = rec["ack_latency_ms"]
            if lat is not None:
                latencies.append(lat)
            breach = lat is not None and lat > sla_ms
            age_ms = lat
        else:
            # Unpaired open: shown as open/breached-by-age, never dropped.
            stats["unacked"] += 1
            age_ms = _age_ms(rec, now, cfg["business_hours"])
            breach = age_ms is not None and age_ms > sla_ms
        if breach:
            stats["breaches"] += 1
        rec_out = {
            **rec,
            "opened_at": rec["opened_at"].isoformat() if rec["opened_at"] else None,
            "acked_at": rec["acked_at"].isoformat() if rec["acked_at"] else None,
            "resolved_at": rec["resolved_at"].isoformat() if rec["resolved_at"] else None,
            "acked": acked and not rec["expired"],
            "breach": breach,
            "age_minutes": round(age_ms / 60_000.0, 1) if age_ms is not None else None,
        }
        stats["escalations"].append(rec_out)

    stats["all_closed"] = stats["total"] > 0 and stats["resolved"] == stats["total"]
    if latencies:
        latencies.sort()
        stats["median_ack_ms"] = latencies[len(latencies) // 2]
    return stats


def _age_ms(rec: dict, now: datetime, business_hours: dict | None) -> float | None:
    if rec["opened_at"] is None:
        return None
    start = clock_start(rec["opened_at"], business_hours)
    return max(0.0, (now - start).total_seconds() * 1000.0)


# ---------------------------------------------------------------------------
# Renderers (pure; honest surfacing)
# ---------------------------------------------------------------------------


def _fmt_minutes(ms: float | None) -> str:
    if ms is None:
        return "?"
    return f"{max(0, round(ms / 60_000.0))}m"


def format_attention_lines(stats: dict) -> list[str]:
    """Operator-attention lines for the daily digest fold-in: breached +
    currently-unacked escalations with ages and breach flags. Empty window ->
    no lines — a clean board stays honest, nothing is fabricated."""
    escalations = stats.get("escalations") or []
    sla = stats.get("sla_minutes", DEFAULT_SLA_MINUTES)
    lines: list[str] = []

    breached = [e for e in escalations if e.get("breach")]
    if breached:
        n = len(breached)
        lines.append(f"🚨 {n} escalation{'s' if n != 1 else ''} past ack SLA ({sla}m)")
        for e in breached[:5]:
            where = "/".join(x for x in (e.get("source"), e.get("lane")) if x) or "unknown"
            ws = f", {e['workspace']}" if e.get("workspace") else ""
            if e.get("expired"):
                detail = "expired unacked (TTL drop — fail-closed, requester got no answer)"
            elif e.get("acked"):
                detail = f"acked after { _fmt_minutes(_min_ms(e)) }"
            else:
                detail = f"unacked for {e.get('age_minutes', '?')}m"
            lines.append(f"   • {e.get('escalation_id')} ({where}{ws}) — {detail}")
        if len(breached) > 5:
            lines.append(f"   …+{len(breached) - 5} more breached")

    aging = [e for e in escalations if not e.get("acked") and not e.get("breach") and not e.get("expired")]
    if aging:
        n = len(aging)
        oldest = max((e.get("age_minutes") or 0) for e in aging)
        lines.append(
            f"⏳ {n} escalation{'s' if n != 1 else ''} awaiting ack "
            f"(oldest {round(oldest)}m, SLA {sla}m)"
        )
    return lines


def _min_ms(e: dict) -> float | None:
    lat = e.get("ack_latency_ms")
    return float(lat) if isinstance(lat, (int, float)) else None


def format_summary_line(stats: dict) -> str:
    """One summary line per window — closed count, median ack, breach count —
    rendered only from events that exist. Zero escalations -> the honest
    'none required your attention'; unpaired opens are counted as still open,
    never silently dropped."""
    total = int(stats.get("total", 0) or 0)
    days = stats.get("window_days", 7)
    window = "this week" if days == 7 else f"in the last {days} days"
    if total == 0:
        return f"**Escalations:** no escalations required your attention {window}."

    resolved = int(stats.get("resolved", 0) or 0)
    breaches = int(stats.get("breaches", 0) or 0)
    expired = int(stats.get("expired", 0) or 0)
    open_count = total - resolved
    median = stats.get("median_ack_ms")
    sla = stats.get("sla_minutes", DEFAULT_SLA_MINUTES)

    bits: list[str] = []
    if resolved == total:
        # The honest brag — only when it is true.
        brag = "every escalation closed"
        if median is not None:
            brag += f", median ack {_fmt_minutes(median)}"
        bits.append(brag)
    else:
        bits.append(f"{resolved} of {total} escalation{'s' if total != 1 else ''} closed")
        if median is not None:
            bits.append(f"median ack {_fmt_minutes(median)}")
        bits.append(f"{open_count} still open")
    if breaches:
        bits.append(f"{breaches} breached the {sla}m ack SLA")
        if expired:
            bits.append(f"{expired} expired unanswered")
    return "**Escalations:** " + ", ".join(bits) + "."


def format_escalation_report(stats: dict) -> str:
    """Standalone render for the /escalation-sla endpoint (and the flag-gated
    daily /digest fold-in): header + the summary line + any attention detail
    lines."""
    ws = stats.get("workspace") or "all workspaces"
    days = stats.get("window_days", 7)
    lines = [f"⏱️ **Escalation SLA** ({days}-day window, {ws})", ""]
    lines.append(format_summary_line(stats))
    detail = format_attention_lines(stats)
    if detail:
        lines.append("")
        lines.extend(detail)
    return "\n".join(lines).strip()
