#!/bin/sh
# Smart wrapper around the ddgs CLI.
#
# The native ddgs CLI requires a subcommand (text/news/images/videos) and a -k
# flag for keywords:  ddgs text -k "query" -m 5 -o json
#
# Agents reliably get this wrong and call it as `ddgs "query"`, which exits
# with argparse error 2 and triggers retry loops. This wrapper accepts the
# naive form and translates it to the canonical text-search invocation.
#
# Pass-through behaviour: known subcommands are executed unchanged so callers
# that do know the syntax still work.

REAL_DDGS=/opt/hermes/bin/ddgs

case "$1" in
  text|news|images|videos|version|--help|-h|--version|"")
    exec "$REAL_DDGS" "$@"
    ;;
  *)
    exec "$REAL_DDGS" text -k "$*" -m 5 -o json
    ;;
esac
