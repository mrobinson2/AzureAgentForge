#!/usr/bin/env python3
# Reference design — NOT deployed. Part of the multi-tenant roadmap
# (see experimental/multi-tenant/README.md). Not wired into the runnable stack;
# provided to illustrate the intended design.
"""
Tenant Provisioning Script for AzureAgentForge Platform
Usage: python provision_tenant.py --slug acme --name "Acme Corp" --email admin@example.com
"""

import argparse
import requests
import sys
import json

def provision_tenant(api_base: str, slug: str, display_name: str, email: str,
                     use_orchestrator: bool = True, plan: str = "personal"):
    """Provision a new tenant with all associated resources."""

    url = f"{api_base}/tenants"
    payload = {
        "slug": slug,
        "display_name": display_name,
        "primary_email": email,
        "use_orchestrator": use_orchestrator,
        "plan_name": plan
    }

    try:
        response = requests.post(url, json=payload, timeout=60)
        response.raise_for_status()

        tenant = response.json()
        print(f"Successfully provisioned tenant: {tenant['slug']}")
        print(f"   ID: {tenant['id']}")
        print(f"   Mem0 Namespace: {tenant['mem0_namespace']}")
        print(f"   Vector Index: {tenant['vector_index_name']}")
        print(f"   Vault Path: {tenant['agent_vault_path']}")
        return tenant

    except requests.exceptions.HTTPError as e:
        print(f"Failed to provision tenant: {e}")
        if e.response:
            print(f"   Response: {e.response.text}")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Provision a new tenant")
    parser.add_argument("--api-base", default="http://localhost:8000",
                       help="Platform API base URL")
    parser.add_argument("--slug", required=True, help="Tenant slug (e.g., 'acme')")
    parser.add_argument("--name", required=True, help="Display name")
    parser.add_argument("--email", required=True, help="Primary email")
    parser.add_argument("--orchestrator", action="store_true", default=True,
                       help="Enable orchestrator agent")
    parser.add_argument("--plan", default="personal",
                       help="Plan name (personal/family/smb-basic)")

    args = parser.parse_args()

    print(f"Provisioning tenant '{args.slug}'...")
    tenant = provision_tenant(
        api_base=args.api_base,
        slug=args.slug,
        display_name=args.name,
        email=args.email,
        use_orchestrator=args.orchestrator,
        plan=args.plan
    )

    # Output JSON for scripting
    print("\n" + json.dumps(tenant, indent=2))

if __name__ == "__main__":
    main()
