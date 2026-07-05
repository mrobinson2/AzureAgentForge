"""Tenant Console (P2) — the operator-facing web view over the P1 executor.

Forge Console pattern (AAF ``installer/``): FastAPI + single-file HTML,
loopback-only + per-session token, preview-first, SSE-streamed provisioning
behind a typed-confirmation gate. The console is a thin view — all provisioning
logic lives in :mod:`tenantconsole` (contract → client → steps/decommission).
"""
