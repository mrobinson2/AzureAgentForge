"""Tenant Console — playbook packs + the headless provisioning executor.

Public surface re-exported from the pack loader (:mod:`tenantconsole.playbook`)
and the P1 executor (contract → client → steps/decommission).
"""

from tenantconsole.playbook import (  # noqa: F401
    AgentSpec,
    Pack,
    example_variables,
    find_placeholders,
    load_pack,
    load_seed_memories,
    load_smoke_fixture,
    render_agent,
    render_structure,
    render_template,
    validate_pack,
)
from tenantconsole.contract import (  # noqa: F401
    ChannelBinding,
    ContractError,
    TenantContract,
    load_contract,
)
from tenantconsole.client import ApiClient, ApiError  # noqa: F401
from tenantconsole.jwt_mint import decode_jwt, mint_jwt  # noqa: F401
from tenantconsole.steps import (  # noqa: F401
    Provisioner,
    Reporter,
    StepResult,
    agent_env,
    agent_slug,
    run_provision,
)
from tenantconsole.decommission import (  # noqa: F401
    ConfirmationError,
    confirmation_phrase,
    run_decommission,
)
