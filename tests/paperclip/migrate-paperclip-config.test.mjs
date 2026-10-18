/**
 * migrate-paperclip-config.py contract (node:test, zero deps).
 * Run: node --test tests/paperclip/migrate-paperclip-config.test.mjs
 */
import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync, readdirSync, existsSync, rmSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT = join(dirname(fileURLToPath(import.meta.url)), "../../apps/paperclip/migrate-paperclip-config.py");
const ENV = {
  PAPERCLIP_PUBLIC_URL: "https://app.example.test",
  PAPERCLIP_ALLOWED_HOSTNAMES: "app.example.test,second.example.test",
  PAPERCLIP_DEPLOYMENT_MODE: "authenticated",
  PAPERCLIP_DEPLOYMENT_EXPOSURE: "private",
  DATABASE_URL: "postgresql://u:SECRET@db/x",
};
const LEGACY = {
  instanceId: "dev", publicUrl: "https://old.example.test", allowedHostnames: "old.example.test",
  deploymentMode: "authenticated", deploymentExposure: "public",
};

function withDir(fn) {
  const dir = mkdtempSync(join(tmpdir(), "aaf-pc-config-"));
  try { return fn(join(dir, "instances", "default", "config.json"), dir); } finally { rmSync(dir, { recursive: true, force: true }); }
}
const run = (path, env = ENV) => spawnSync("python3", [SCRIPT, path], { env: { PATH: process.env.PATH, ...env }, encoding: "utf-8" });
const read = (path) => JSON.parse(readFileSync(path, "utf-8"));

describe("migrate-paperclip-config.py", () => {
  test("missing file: writes a schema-valid v1 config from env, database.mode=postgres, no secrets", () => withDir((p) => {
    const r = run(p);
    assert.equal(r.status, 0, r.stderr);
    const c = read(p);
    assert.equal(c.$meta.version, 1);
    assert.equal(c.database.mode, "postgres");
    assert.equal(c.database.connectionString, undefined);
    assert.ok(!readFileSync(p, "utf-8").includes("SECRET"));
    assert.deepEqual(c.server, { deploymentMode: "authenticated", exposure: "private", allowedHostnames: ["app.example.test", "second.example.test"] });
    assert.equal(c.auth.publicBaseUrl, "https://app.example.test");
    assert.equal(typeof c.logging.mode, "string");
    assert.deepEqual(c.telemetry, {});
    assert.match(r.stderr, /created v1 config/);
  }));

  test("legacy flat file: backed up and rewritten as v1; env wins, legacy fills gaps", () => withDir((p) => {
    mkdirSync(dirname(p), { recursive: true });
    writeFileSync(p, JSON.stringify(LEGACY));
    const r = run(p, { PAPERCLIP_PUBLIC_URL: "https://new.example.test" }); // only publicUrl from env
    assert.equal(r.status, 0, r.stderr);
    const c = read(p);
    assert.equal(c.$meta.version, 1);
    assert.equal(c.auth.publicBaseUrl, "https://new.example.test");
    assert.deepEqual(c.server.allowedHostnames, ["old.example.test"]);
    assert.equal(c.server.exposure, "public");
    assert.equal(c.database.mode, "postgres");
    assert.equal(c.instanceId, undefined);
    const backups = readdirSync(dirname(p)).filter((f) => f.startsWith("config.json.legacy-"));
    assert.equal(backups.length, 1);
    assert.deepEqual(JSON.parse(readFileSync(join(dirname(p), backups[0]), "utf-8")), LEGACY);
    assert.match(r.stderr, /migrated legacy config/);
  }));

  test("v1 file: env-controlled fields are synced; second run is a no-op", () => withDir((p) => {
    assert.equal(run(p).status, 0);
    const r2 = run(p, { ...ENV, PAPERCLIP_DEPLOYMENT_EXPOSURE: "public", PAPERCLIP_ALLOWED_HOSTNAMES: "x.example.test" });
    assert.equal(r2.status, 0, r2.stderr);
    assert.match(r2.stderr, /synced from env: server\.exposure, server\.allowedHostnames/);
    const c = read(p);
    assert.equal(c.server.exposure, "public");
    assert.deepEqual(c.server.allowedHostnames, ["x.example.test"]);
    const r3 = run(p, { ...ENV, PAPERCLIP_DEPLOYMENT_EXPOSURE: "public", PAPERCLIP_ALLOWED_HOSTNAMES: "x.example.test" });
    assert.match(r3.stderr, /already in sync/);
  }));

  test("v1 file with embedded-postgres mode is corrected to postgres (upstream reads mode only from the file)", () => withDir((p) => {
    mkdirSync(dirname(p), { recursive: true });
    writeFileSync(p, JSON.stringify({ $meta: { version: 1, updatedAt: "x", source: "onboard" }, database: { mode: "embedded-postgres" }, logging: { mode: "file" }, server: {}, telemetry: {} }));
    const r = run(p);
    assert.equal(r.status, 0, r.stderr);
    assert.equal(read(p).database.mode, "postgres");
    assert.match(r.stderr, /database\.mode/);
  }));

  test("unparseable file: warns, exits 1, leaves the file untouched", () => withDir((p) => {
    mkdirSync(dirname(p), { recursive: true });
    writeFileSync(p, "{ not json");
    const r = run(p);
    assert.equal(r.status, 1);
    assert.match(r.stderr, /WARNING/);
    assert.equal(readFileSync(p, "utf-8"), "{ not json");
    assert.ok(!existsSync(p + ".legacy"));
  }));
});
