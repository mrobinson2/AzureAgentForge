#!/bin/sh
# lease-guard — single-writer lease enforcement for the self-hosted site.
# See the self-hosted-primary ADR in docs/design/ ("the load-bearing piece").
#
# Every long-running service in docker-compose.yml depends_on this container with
# `condition: service_completed_successfully`. If the lease does not name THIS
# site, lease-guard exits non-zero and the whole stack refuses to come up — the
# structural guarantee against split-brain (two live sites → duplicate ingress
# answers, two runs per unit of work, duplicate memory events).
#
# Source of truth for the lease is the Key Vault secret `platform-active-site`
# ∈ {cloud, local}. We deliberately do NOT read Key Vault from inside this
# container — a home machine must not hold a service principal. Instead
# `scripts/aaf-site` (operator-run, already authenticated as a Secrets Officer)
# refreshes a local MIRROR of the lease at ~/aaf/active-site immediately before
# every bring-up, and mounts it here at /lease/active-site. The mirror is
# therefore at most seconds old and was written from an authenticated read of the
# authoritative value. If the file is missing, the operator brought the stack up
# by hand instead of via aaf-site — refuse, loudly.
set -eu

LEASE_FILE="${LEASE_FILE:-/lease/active-site}"

if [ ! -f "$LEASE_FILE" ]; then
  echo "lease-guard: FATAL — lease mirror '$LEASE_FILE' is missing." >&2
  echo "lease-guard: bring the stack up with 'aaf-site local' (it refreshes the" >&2
  echo "lease-guard: mirror from the platform-active-site Key Vault secret first)," >&2
  echo "lease-guard: never a bare 'docker compose up'. Refusing to start." >&2
  exit 1
fi

VALUE="$(tr -d '[:space:]' < "$LEASE_FILE")"

if [ "$VALUE" != "local" ]; then
  echo "lease-guard: FATAL — platform-active-site is '${VALUE:-<empty>}', not 'local'." >&2
  echo "lease-guard: the cloud site is (or should be) live. Starting the platform here" >&2
  echo "lease-guard: now would SPLIT-BRAIN it. Run 'aaf-site local' to take the lease" >&2
  echo "lease-guard: deliberately (it puts the cloud standby to sleep). Refusing to start." >&2
  exit 1
fi

echo "lease-guard: OK — platform-active-site == 'local'; this site is cleared to run."
exit 0
