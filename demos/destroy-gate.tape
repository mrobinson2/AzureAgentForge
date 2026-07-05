# destroy-gate.tape — the destroy-aware approval gate.
#
# The installer console will run an `apply` straight through when the saved
# plan only creates/updates, but a plan that deletes or REPLACES a resource
# trips a second, louder approval gate. plan_has_destroy() is the pure-Python
# core of that gate — deterministic, no Azure creds, no terraform invocation.
#
# Render with:  vhs demos/destroy-gate.tape
# Output:       docs/assets/destroy-gate.gif
#
# SANITIZATION: clean generic prompt (PS1='demo$ ') and relative paths only.

Output docs/assets/destroy-gate.gif

Require bash
Require python3

Set Shell bash
Set FontSize 17
Set Width 1200
Set Height 760
Set Padding 22
Set Theme "Catppuccin Mocha"
Set TypingSpeed 28ms
Set PlaybackSpeed 1.0

Hide
Type "PS1='demo$ '; PROMPT_COMMAND=; clear" Enter
Show

Sleep 600ms
Type "# A safe plan (create-only) sails through; a delete trips the gate."
Enter
Sleep 1s

# Inline snippet drives installer/core.py: plan_has_destroy on two sample plans.
Type `./.forge-venv/bin/python - <<'PY'`
Enter
Type "from installer.core import plan_has_destroy, DESTROY_APPROVAL_TOKEN"
Enter
Type "safe = {'resource_changes': [{'address': 'azurerm_resource_group.main', 'change': {'actions': ['create']}}]}"
Enter
Type "danger = {'resource_changes': ["
Enter
Type "    {'address': 'azurerm_postgresql_flexible_server.db', 'change': {'actions': ['delete']}},"
Enter
Type "    {'address': 'azurerm_key_vault.kv', 'change': {'actions': ['delete', 'create']}}]}"
Enter
Type "for name, plan in (('create-only plan', safe), ('plan with a delete', danger)):"
Enter
Type "    has, addrs = plan_has_destroy(plan)"
Enter
Type "    if has:"
Enter
Type `        print(f'  {name}: destroy detected -> approval required (type \"{DESTROY_APPROVAL_TOKEN}\")')`
Enter
Type "        [print(f'      - {a}') for a in addrs]"
Enter
Type "    else:"
Enter
Type "        print(f'  {name}: no destroy -> apply proceeds')"
Enter
Type "PY"
Enter
Sleep 4.5s

Type "# The same rule, locked down by the offline test suite (no creds needed)."
Enter
Sleep 800ms
# --disable-warnings keeps the warnings summary (which prints venv file paths)
# out of the recording — public repo, no absolute paths in any frame.
Type "./.forge-venv/bin/python -m pytest installer/tests/test_core.py -q -k destroy -p no:cacheprovider --disable-warnings"
Enter
Sleep 4s
