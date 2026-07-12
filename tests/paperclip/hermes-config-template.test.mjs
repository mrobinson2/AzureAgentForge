/**
 * Hermes config-template generation unit tests (node:test, zero dependencies).
 *
 * Run:  node --test tests/paperclip/hermes-config-template.test.mjs
 *
 * Executes apps/paperclip/write-hermes-config.sh (the script the paperclip
 * container entrypoint invokes at boot) against a temp HERMES_HOME and
 * asserts the generated config.yaml has the ROUTER-COMPATIBLE shape:
 * provider custom + api_mode chat_completions + Bearer api_key. This shape
 * is an incident fix ported from the upstream private deployment — the old
 * anthropic_messages default made Hermes v0.18.x send x-api-key auth, which
 * the router's fail-closed bearer auth 401'd, killing every agent at spawn.
 */

import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT = join(
  dirname(fileURLToPath(import.meta.url)),
  "../../apps/paperclip/write-hermes-config.sh"
);

/** Run the generator with a temp HERMES_HOME; return { config, stdout, stderr, status }. */
function generate(env = {}) {
  const home = mkdtempSync(join(tmpdir(), "aaf-hermes-config-"));
  try {
    const r = spawnSync("sh", [SCRIPT], {
      env: { PATH: process.env.PATH, HERMES_HOME: home, ...env },
      encoding: "utf-8",
    });
    let config = null;
    try {
      config = readFileSync(join(home, "config.yaml"), "utf-8");
    } catch {
      /* leave null — asserted by callers */
    }
    return { config, stdout: r.stdout, stderr: r.stderr, status: r.status };
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
}

describe("write-hermes-config.sh", () => {
  test("generates the router-compatible shape (custom + chat_completions + api_key)", () => {
    const r = generate({
      OPENAI_BASE_URL: "http://router.internal/v1",
      ROUTER_API_KEY: "test-router-key",
    });
    assert.equal(r.status, 0, r.stderr);
    assert.ok(r.config, "config.yaml written");
    assert.match(r.config, /^\s*provider: custom$/m);
    assert.match(r.config, /^\s*api_mode: chat_completions$/m);
    assert.match(r.config, /^\s*api_key: test-router-key$/m);
    assert.match(r.config, /^\s*base_url: http:\/\/router\.internal\/v1$/m);
    assert.match(r.config, /^\s*cache_ttl: 1h$/m);
    // The incident shape must be gone: no anthropic_messages transport.
    assert.ok(
      !r.config.includes("anthropic_messages"),
      "anthropic_messages must not be the generated default"
    );
  });

  test("HERMES_BASE_URL overrides OPENAI_BASE_URL", () => {
    const r = generate({
      OPENAI_BASE_URL: "http://router.internal/v1",
      HERMES_BASE_URL: "http://override.internal:8080/v1",
      ROUTER_API_KEY: "k",
    });
    assert.equal(r.status, 0, r.stderr);
    assert.match(r.config, /^\s*base_url: http:\/\/override\.internal:8080\/v1$/m);
  });

  test("ROUTER_API_KEY wins over OPENAI_API_KEY", () => {
    const r = generate({
      ROUTER_API_KEY: "router-key-wins",
      OPENAI_API_KEY: "openai-key-loses",
    });
    assert.equal(r.status, 0, r.stderr);
    assert.match(r.config, /^\s*api_key: router-key-wins$/m);
  });

  test("falls back to OPENAI_API_KEY (the Terraform-mounted router secret)", () => {
    const r = generate({ OPENAI_API_KEY: "tf-mounted-router-key" });
    assert.equal(r.status, 0, r.stderr);
    assert.match(r.config, /^\s*api_key: tf-mounted-router-key$/m);
  });

  test("warns loudly when no router key is present (runs would 401 fail-closed)", () => {
    const r = generate({});
    assert.equal(r.status, 0, r.stderr); // boot proceeds; the warning is the signal
    assert.match(r.stderr, /WARNING: ROUTER_API_KEY\/OPENAI_API_KEY unset/);
    assert.ok(r.config, "config still written");
    assert.match(r.config, /^\s*api_key:\s*$/m);
  });

  test("fails non-zero when HERMES_HOME is not writable (entrypoint set -e aborts boot)", () => {
    const r = spawnSync("sh", [SCRIPT], {
      env: {
        PATH: process.env.PATH,
        HERMES_HOME: "/dev/null/not-a-dir", // mkdir -p cannot create under a file
        ROUTER_API_KEY: "k",
      },
      encoding: "utf-8",
    });
    assert.notEqual(r.status, 0, "must exit non-zero, not skip");
  });
});
