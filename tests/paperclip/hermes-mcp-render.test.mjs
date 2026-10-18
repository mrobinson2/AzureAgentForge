/**
 * MCP render tests (node:test, zero dependencies).
 *
 * Run:  node --test tests/paperclip/hermes-mcp-render.test.mjs
 *
 * Drives write-hermes-config.sh end to end with the shipped registry
 * (agents/mcp-registry.json) and asserts the governance contract of the
 * rendered `mcp_servers:` block: off by default, only requested servers,
 * secret-gated servers skipped loudly when their secret is unset, and the
 * router key never leaking into an MCP header.
 */

import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const SCRIPT = join(HERE, "../../apps/paperclip/write-hermes-config.sh");
const RENDERER = join(HERE, "../../apps/paperclip/render-hermes-mcp.py");
const REGISTRY = join(HERE, "../../agents/mcp-registry.json");

function generate(env = {}) {
  const home = mkdtempSync(join(tmpdir(), "aaf-hermes-mcp-"));
  try {
    const r = spawnSync("sh", [SCRIPT], {
      env: {
        PATH: process.env.PATH,
        HERMES_HOME: home,
        ROUTER_API_KEY: "ROUTER-SECRET-DO-NOT-LEAK",
        AAF_MCP_REGISTRY: REGISTRY,
        AAF_MCP_RENDERER: RENDERER,
        ...env,
      },
      encoding: "utf-8",
    });
    let config = null;
    try { config = readFileSync(join(home, "config.yaml"), "utf-8"); } catch { /* asserted */ }
    return { config, stdout: r.stdout, stderr: r.stderr, status: r.status };
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
}

describe("render-hermes-mcp via write-hermes-config.sh", () => {
  test("off by default: no AAF_MCP_SERVERS => no mcp_servers block, base config intact", () => {
    const r = generate({});
    assert.equal(r.status, 0, r.stderr);
    assert.ok(!r.config.includes("mcp_servers"), "no mcp block when unset");
    assert.match(r.config, /^\s*api_mode: chat_completions$/m);
  });

  test("renders a credential-free registry server with its allow-list verbatim", () => {
    const r = generate({ AAF_MCP_SERVERS: "deepwiki" });
    assert.equal(r.status, 0, r.stderr);
    assert.match(r.config, /^mcp_servers:$/m);
    assert.match(r.config, /^  deepwiki:$/m);
    assert.match(r.config, /^    url: "https:\/\/mcp\.deepwiki\.com\/mcp"$/m);
    assert.match(r.config, /^      include: \["read_wiki_structure", "read_wiki_contents", "ask_question"\]$/m);
    assert.match(r.config, /^      prompts: false$/m);
    assert.ok(!r.config.includes("  github:"), "unrequested server not rendered");
    assert.match(r.stderr, /rendered MCP servers: deepwiki/);
  });

  test("secret-gated server is skipped loudly when its secret env is unset", () => {
    const r = generate({ AAF_MCP_SERVERS: "deepwiki,github" });
    assert.equal(r.status, 0, r.stderr);
    assert.match(r.config, /^  deepwiki:$/m);
    assert.ok(!r.config.includes("  github:"), "github must be skipped without MCP_GITHUB_TOKEN");
    assert.match(r.stderr, /github' skipped: MCP_GITHUB_TOKEN unset/);
  });

  test("secret-gated server renders its header from ONLY the named env var", () => {
    const r = generate({ AAF_MCP_SERVERS: "github", MCP_GITHUB_TOKEN: "ghp_test_token" });
    assert.equal(r.status, 0, r.stderr);
    assert.match(r.config, /^  github:$/m);
    assert.match(r.config, /^      Authorization: "Bearer ghp_test_token"$/m);
    assert.match(r.config, /include: \["get_file_contents", "search_code"/);
  });

  test("the Key Vault placeholder value counts as unset", () => {
    const r = generate({ AAF_MCP_SERVERS: "github", MCP_GITHUB_TOKEN: "__unset__" });
    assert.equal(r.status, 0, r.stderr);
    assert.ok(!r.config.includes("  github:"));
  });

  test("the router key never appears in the MCP block", () => {
    const r = generate({ AAF_MCP_SERVERS: "deepwiki,github", MCP_GITHUB_TOKEN: "t" });
    const mcpPart = r.config.slice(r.config.indexOf("mcp_servers:"));
    assert.ok(!mcpPart.includes("ROUTER-SECRET-DO-NOT-LEAK"));
  });

  test("an unregistered name is a loud warning, not a silent render", () => {
    const r = generate({ AAF_MCP_SERVERS: "ghost" });
    assert.equal(r.status, 0, r.stderr);
    assert.ok(!r.config.includes("mcp_servers"));
    assert.match(r.stderr, /'ghost' is not in the registry/);
  });

  test("a missing registry does not abort boot", () => {
    const r = generate({ AAF_MCP_SERVERS: "deepwiki", AAF_MCP_REGISTRY: "/nonexistent/registry.json" });
    assert.equal(r.status, 0, r.stderr);
    assert.match(r.stderr, /registry\/renderer\/python3 missing/);
    assert.match(r.config, /^\s*api_mode: chat_completions$/m);
  });
});
