/**
 * Repo-owned skill overrides must reach a deployment that already has old copies.
 *
 * The skills sync copies image → persistent volume with `cp -rn` so
 * agent-authored skills survive a deploy. The side effect: a repo-owned helper
 * fix never lands where the volume already holds the previous file. That is not
 * theoretical — the fix stopping the memory helper from defaulting to a
 * system-privileged peer shipped in an image, was deployed, and still was not
 * what agents ran, because the Azure file share kept the old copy:
 *
 *   image  → AGENT="${PAPERCLIP_AGENT_SLUG:-${GOVERNOR_AGENT_SLUG:-unknown-agent}}"
 *   share  → AGENT="${PAPERCLIP_AGENT_SLUG:-${GOVERNOR_AGENT_SLUG:-operator}}"
 *
 * These pin the delivery mechanism: repo-owned overrides are staged separately
 * and force-copied, and the memory helper is callable by name like its siblings.
 *
 * Offline: reads the Dockerfile and entrypoint.
 */

import { strict as assert } from "node:assert";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const DOCKERFILE = readFileSync(
  new URL("../../services/paperclip/Dockerfile", import.meta.url), "utf-8",
);
const ENTRYPOINT = readFileSync(
  new URL("../../services/paperclip/docker-entrypoint.sh", import.meta.url), "utf-8",
);

test("repo-owned overrides are staged in their own image directory", () => {
  // Needed so the entrypoint can tell code-we-ship from user content; the
  // merged /opt/hermes-skills cannot make that distinction.
  assert.match(
    DOCKERFILE,
    /COPY apps\/hermes\/overrides\/skills \/opt\/hermes-skill-overrides/,
  );
});

test("the entrypoint force-refreshes them, overriding the no-clobber sync", () => {
  const block = ENTRYPOINT.slice(ENTRYPOINT.indexOf("OVERRIDES_SRC="));
  assert.ok(block, "no override-refresh block found in the entrypoint");
  assert.match(block, /cp -rf "\$\{OVERRIDES_SRC\}\/\." "\$\{SKILLS_DST\}\/"/);
});

test("the refresh runs after the no-clobber syncs, so the image wins", () => {
  const builtIn = ENTRYPOINT.indexOf('cp -rn "${SKILLS_SRC}');
  const optional = ENTRYPOINT.indexOf('cp -rn "${OPT_SKILLS_SRC}');
  const refresh = ENTRYPOINT.indexOf('cp -rf "${OVERRIDES_SRC}');
  assert.ok(builtIn > -1 && optional > -1 && refresh > -1);
  assert.ok(refresh > builtIn && refresh > optional,
    "a force-copy before the no-clobber passes would be undone by them");
});

test("UI deletions are still honoured after the refresh", () => {
  // Force-copying resurrects a skill an operator deleted; the .deleted marker
  // must be processed afterwards, or the refresh silently overrides that choice.
  const refresh = ENTRYPOINT.indexOf('cp -rf "${OVERRIDES_SRC}');
  const deleted = ENTRYPOINT.indexOf("DELETED_FILE=");
  assert.ok(deleted > refresh,
    ".deleted handling must come after the override refresh");
});

test("pc-memory is callable by name, like its sibling helpers", () => {
  // Hermes spawns a fresh shell per terminal call, so env-resolved paths are
  // lost between calls — the reason pc-delegate and pc-honcho are symlinked.
  // pc-memory was documented as a command but never linked.
  for (const helper of ["pc-delegate", "pc-honcho", "pc-memory"]) {
    assert.match(
      DOCKERFILE,
      new RegExp(`ln -sf [^\\s]+ /usr/local/bin/${helper}\\b`),
      `${helper} must be on PATH`,
    );
  }
});
