#!/usr/bin/env python3
"""Scripted Anthropic-Messages-API stub for the agent-loop canary smoke.

WHAT THIS IS
    A stdlib-only HTTP server that impersonates an Anthropic /v1/messages
    model backend so the e2e agent-loop canary (scripts/smoke-canary.sh) can
    run in CI with NO model credentials. It is registered as the router's
    CLAUDE tier by docker-compose.canary.yml; the model-router forwards
    Hermes's anthropic_messages traffic here via the real Anthropic SDK.

WHAT IS REAL vs STUBBED in the canary roundtrip
    REAL:     issue filing → assignment wake → hermes_local adapter spawn →
              Hermes runtime boot → router auth + tier dispatch → terminal
              tool execution inside the paperclip container → authenticated
              PaperClip API calls (disposition comment + status=done).
    STUBBED:  only the LLM inference. Instead of a model deciding what to do,
              this server replies with a fixed script:
                turn 1 → a `terminal` tool_use that posts the disposition
                          comment and PATCHes the canary issue to done
                turn 2+ → plain text + end_turn.
              The tool call is EXECUTED BY THE REAL Hermes runtime — nothing
              about the loop mechanics is faked.

CONTROL SURFACE (used by scripts/smoke-canary.sh)
    GET  /health         → 200 liveness
    POST /canary-config  → {"issueIdentifier": "ABC-1", "apiBase": ".../api"}
                           arms the stub for one roundtrip (resets counters)
    GET  /canary-state   → counters {model_requests, tool_turns, ...} so the
                           smoke can assert the model hop actually traversed
                           the router (an issue closed out-of-band would
                           otherwise pass silently)
    POST /v1/messages    → the scripted Anthropic Messages endpoint
                           (supports stream:true SSE and non-streaming JSON)

Auth: requires x-api-key == CANARY_STUB_API_KEY (default canary-stub-key) on
/v1/messages — asserts the router actually forwarded its tier credential.

Run self-checks (no network):  python3 scripts/canary/anthropic_stub.py --self-test
"""

import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("CANARY_STUB_PORT", "9785"))
API_KEY = os.environ.get("CANARY_STUB_API_KEY", "canary-stub-key")
# How long a /v1/messages request will wait for /canary-config to arrive when
# it looks like our canary task but the smoke script hasn't armed us yet
# (assignment wake can beat the config call in a race).
CONFIG_WAIT_SECONDS = float(os.environ.get("CANARY_CONFIG_WAIT_SECONDS", "20"))
MARKER = "AAF-CANARY-ROUNDTRIP"
DISPOSITION_TAG = "[aaf-canary] disposition:"

_state_lock = threading.Lock()
_state = {
    "issue_identifier": None,
    "api_base": None,
    "model_requests": 0,       # total /v1/messages calls seen
    "tool_turns": 0,           # times we replied with the terminal tool_use
    "final_turns": 0,          # times we replied end_turn after the tool ran
    "unmatched_requests": 0,   # auxiliary calls (no terminal tool / no marker)
}


def _log(msg):
    print(f"[canary-stub] {msg}", flush=True)


# ── Turn scripting (pure — exercised by --self-test) ─────────────────────────

def build_disposition_command(issue_identifier, api_base):
    """The shell command the 'model' asks the real Hermes terminal tool to run.

    Prefers $PAPERCLIP_API_URL (injected by the hermes adapter / compose) so a
    stack whose agent-side API wiring is broken fails LOUDLY instead of being
    papered over; falls back to the api_base the smoke script supplied.
    """
    comment_body = (
        f"{DISPOSITION_TAG} completed - canary roundtrip verified. "
        "Wake -> adapter -> Hermes runtime -> router -> model(stub) -> "
        "terminal tool -> PaperClip API all traversed. "
        "(Model inference is stubbed by scripts/canary/anthropic_stub.py; "
        "everything else in this loop is real.)"
    )
    comment_json = json.dumps({"body": comment_body})
    done_json = json.dumps({"status": "done"})
    return (
        f'api="${{PAPERCLIP_API_URL:-{api_base}}}"; api="${{api%/}}"; '
        'case "$api" in */api) ;; *) api="$api/api" ;; esac; '
        f'curl -sS -f -X POST "$api/issues/{issue_identifier}/comments" '
        '-H "Authorization: Bearer $PAPERCLIP_API_KEY" '
        '-H "Origin: http://localhost:3100" '
        '-H "Content-Type: application/json" '
        f"-d {shell_quote(comment_json)} "
        f'&& curl -sS -f -X PATCH "$api/issues/{issue_identifier}" '
        '-H "Authorization: Bearer $PAPERCLIP_API_KEY" '
        '-H "Origin: http://localhost:3100" '
        '-H "Content-Type: application/json" '
        f"-d {shell_quote(done_json)}"
    )


def shell_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def _iter_text(value):
    """Yield every text fragment in an Anthropic message content value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for block in value:
            if isinstance(block, dict):
                if isinstance(block.get("text"), str):
                    yield block["text"]
                # tool_result content can nest blocks
                if isinstance(block.get("content"), (str, list)):
                    yield from _iter_text(block["content"])


def request_text(body):
    parts = []
    parts.extend(_iter_text(body.get("system", "")))
    for msg in body.get("messages", []):
        parts.extend(_iter_text(msg.get("content", "")))
    return "\n".join(parts)


def has_terminal_tool(body):
    for tool in body.get("tools") or []:
        if isinstance(tool, dict) and tool.get("name") == "terminal":
            return True
    return False


def already_ran_tool(body):
    """True if a prior assistant turn in this transcript already carried our
    canary terminal tool_use (i.e. the tool result is coming back)."""
    for msg in body.get("messages", []):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") == "terminal"
                and DISPOSITION_TAG in json.dumps(block.get("input", {}))
            ):
                return True
    return False


def decide_turn(body, issue_identifier, api_base):
    """Return ('tool', command) | ('final', text) | ('aux', text)."""
    text = request_text(body)
    if MARKER not in text:
        return ("aux", "ok")
    if already_ran_tool(body):
        return ("final", "Canary disposition posted and issue closed. Done.")
    if has_terminal_tool(body) and issue_identifier:
        return ("tool", build_disposition_command(issue_identifier, api_base))
    # Canary task but no terminal tool exposed — make the failure diagnosable.
    return (
        "aux",
        "canary-stub: task recognised but no `terminal` tool was offered; "
        "cannot post the disposition. Check the agent's enabled toolsets.",
    )


# ── Anthropic response rendering ─────────────────────────────────────────────

_msg_counter = [0]


def _next_msg_id():
    _msg_counter[0] += 1
    return f"msg_canary_{_msg_counter[0]:06d}"


def render_content_blocks(kind, payload):
    if kind == "tool":
        return [
            {"type": "text", "text": "Executing the canary disposition via the terminal tool."},
            {
                "type": "tool_use",
                "id": f"toolu_canary_{_msg_counter[0]:06d}",
                "name": "terminal",
                "input": {"command": payload, "timeout": 120},
            },
        ]
    return [{"type": "text", "text": payload}]


def non_stream_response(model, kind, payload):
    stop_reason = "tool_use" if kind == "tool" else "end_turn"
    return {
        "id": _next_msg_id(),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": render_content_blocks(kind, payload),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 64, "output_tokens": 64},
    }


def sse_events(model, kind, payload):
    """Yield (event_name, data_dict) tuples in Anthropic SSE wire order."""
    stop_reason = "tool_use" if kind == "tool" else "end_turn"
    msg_id = _next_msg_id()
    yield (
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 64, "output_tokens": 1},
            },
        },
    )
    blocks = render_content_blocks(kind, payload)
    for idx, block in enumerate(blocks):
        if block["type"] == "text":
            yield (
                "content_block_start",
                {"type": "content_block_start", "index": idx,
                 "content_block": {"type": "text", "text": ""}},
            )
            yield (
                "content_block_delta",
                {"type": "content_block_delta", "index": idx,
                 "delta": {"type": "text_delta", "text": block["text"]}},
            )
        else:  # tool_use
            yield (
                "content_block_start",
                {"type": "content_block_start", "index": idx,
                 "content_block": {"type": "tool_use", "id": block["id"],
                                    "name": block["name"], "input": {}}},
            )
            yield (
                "content_block_delta",
                {"type": "content_block_delta", "index": idx,
                 "delta": {"type": "input_json_delta",
                           "partial_json": json.dumps(block["input"])}},
            )
        yield ("content_block_stop", {"type": "content_block_stop", "index": idx})
    yield (
        "message_delta",
        {"type": "message_delta",
         "delta": {"stop_reason": stop_reason, "stop_sequence": None},
         "usage": {"output_tokens": 64}},
    )
    yield ("message_stop", {"type": "message_stop"})


# ── HTTP server ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # route BaseHTTPRequestHandler noise through our logger
        _log(fmt % args)

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            self._json(200, {"status": "ok", "service": "canary-stub"})
        elif path == "/canary-state":
            with _state_lock:
                self._json(200, dict(_state))
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/canary-config":
            try:
                body = self._read_body()
            except Exception as e:
                self._json(400, {"error": f"bad json: {e}"})
                return
            ident = body.get("issueIdentifier")
            api_base = body.get("apiBase")
            if not ident:
                self._json(400, {"error": "issueIdentifier required"})
                return
            with _state_lock:
                _state["issue_identifier"] = ident
                _state["api_base"] = api_base or "http://127.0.0.1:3099/api"
                _state["model_requests"] = 0
                _state["tool_turns"] = 0
                _state["final_turns"] = 0
                _state["unmatched_requests"] = 0
            _log(f"armed for issue {ident} (api_base={api_base})")
            self._json(200, {"status": "armed", "issueIdentifier": ident})
        elif path in ("/v1/messages", "/anthropic/v1/messages"):
            self._handle_messages()
        else:
            self._json(404, {"error": "not found"})

    def _handle_messages(self):
        provided = (self.headers.get("x-api-key") or "").strip()
        if provided != API_KEY:
            _log(f"REJECTED /v1/messages: bad x-api-key ({provided[:8]!r}...)")
            self._json(401, {"type": "error",
                             "error": {"type": "authentication_error",
                                       "message": "invalid x-api-key"}})
            return
        try:
            body = self._read_body()
        except Exception as e:
            self._json(400, {"type": "error",
                             "error": {"type": "invalid_request_error",
                                       "message": f"bad json: {e}"}})
            return

        model = body.get("model", "claude-canary")
        with _state_lock:
            _state["model_requests"] += 1
            ident = _state["issue_identifier"]
            api_base = _state["api_base"]

        # The assignment wake can reach us before smoke-canary.sh arms the
        # stub; if this request carries the canary marker, wait briefly.
        if ident is None and MARKER in request_text(body):
            deadline = time.monotonic() + CONFIG_WAIT_SECONDS
            while time.monotonic() < deadline:
                time.sleep(0.5)
                with _state_lock:
                    ident = _state["issue_identifier"]
                    api_base = _state["api_base"]
                if ident:
                    break

        kind, payload = decide_turn(body, ident, api_base)
        with _state_lock:
            if kind == "tool":
                _state["tool_turns"] += 1
            elif kind == "final":
                _state["final_turns"] += 1
            else:
                _state["unmatched_requests"] += 1
        _log(
            f"/v1/messages turn={kind} stream={bool(body.get('stream'))} "
            f"model={model} msgs={len(body.get('messages', []))} "
            f"tools={len(body.get('tools') or [])}"
        )

        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            # SSE has no fixed length; close delimits the response.
            self.send_header("Connection", "close")
            self.end_headers()
            for event, data in sse_events(model, kind, payload):
                chunk = f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
                self.wfile.write(chunk)
                self.wfile.flush()
            self.close_connection = True
        else:
            self._json(200, non_stream_response(model, kind, payload))


# ── Self-test (no network) ───────────────────────────────────────────────────

def self_test():
    failures = []

    def check(name, cond):
        print(("  PASS " if cond else "  FAIL ") + name)
        if not cond:
            failures.append(name)

    task_prompt = (
        f"You are Canary. ## Assigned Task\nIssue: AAF-1\nTitle: [{MARKER}] roundtrip\n"
        f"{MARKER}: post the disposition comment and mark this issue done."
    )
    tools = [{"name": "terminal", "description": "run shell",
              "input_schema": {"type": "object",
                               "properties": {"command": {"type": "string"}}}}]

    # 1. first canary turn → terminal tool_use with disposition + done curls
    body1 = {"model": "claude-canary", "max_tokens": 512, "stream": False,
             "system": "sys", "tools": tools,
             "messages": [{"role": "user", "content": task_prompt}]}
    kind, payload = decide_turn(body1, "AAF-1", "http://127.0.0.1:3099/api")
    check("turn1 is tool", kind == "tool")
    check("turn1 posts disposition comment", DISPOSITION_TAG in payload)
    check("turn1 targets the issue", "/issues/AAF-1" in payload)
    check("turn1 marks done", '\\"status\\": \\"done\\"' in json.dumps(payload))
    resp = non_stream_response("claude-canary", kind, payload)
    check("non-stream stop_reason tool_use", resp["stop_reason"] == "tool_use")
    check("non-stream has tool_use block",
          any(b["type"] == "tool_use" and b["name"] == "terminal"
              for b in resp["content"]))

    # 2. second turn (tool result back) → end_turn
    body2 = {
        "model": "claude-canary", "max_tokens": 512, "tools": tools,
        "messages": [
            {"role": "user", "content": task_prompt},
            {"role": "assistant", "content": resp["content"]},
            {"role": "user", "content": [
                {"type": "tool_result",
                 "tool_use_id": resp["content"][1]["id"],
                 "content": [{"type": "text", "text": '{"exit_code": 0}'}]}]},
        ],
    }
    kind2, payload2 = decide_turn(body2, "AAF-1", "http://127.0.0.1:3099/api")
    check("turn2 is final", kind2 == "final")

    # 3. auxiliary request (no marker) never gets the tool script
    body3 = {"model": "claude-canary", "max_tokens": 64,
             "messages": [{"role": "user", "content": "summarize this session"}]}
    kind3, _ = decide_turn(body3, "AAF-1", "http://127.0.0.1:3099/api")
    check("aux request is aux", kind3 == "aux")

    # 4. canary task but stub not armed → no tool turn (aux, diagnosable)
    kind4, payload4 = decide_turn(body1, None, None)
    check("unarmed stub does not emit tool turn", kind4 != "tool")

    # 5. SSE event ordering is valid Anthropic wire shape
    events = list(sse_events("claude-canary", "tool", payload))
    names = [e[0] for e in events]
    check("sse starts with message_start", names[0] == "message_start")
    check("sse ends with message_stop", names[-1] == "message_stop")
    check("sse has input_json_delta",
          any(d.get("delta", {}).get("type") == "input_json_delta"
              for _, d in events))
    check(
        "sse tool json reassembles",
        json.loads(
            next(d["delta"]["partial_json"] for _, d in events
                 if d.get("delta", {}).get("type") == "input_json_delta")
        )["command"] == payload,
    )

    # 6. the disposition command survives shell quoting (no unescaped quotes)
    check("command shell-quotes JSON payloads", payload.count("-d '") == 2)

    print(f"[canary-stub] self-test: {len(failures)} failure(s)")
    return 1 if failures else 0


def main():
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    _log(f"listening on :{PORT} (POST /canary-config to arm; GET /canary-state)")
    server.serve_forever()


if __name__ == "__main__":
    main()
