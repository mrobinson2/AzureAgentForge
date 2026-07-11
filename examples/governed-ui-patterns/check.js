#!/usr/bin/env node
/* Governed-AI conformance checker.
 *
 * Lints pattern-consumer pages against the pattern library's contracts. Static + heuristic by
 * design: it checks that the CONTRACTS are present in source (text/markup patterns), NOT that
 * runtime behavior is correct — runtime enforcement is a separate "governance firewall" concern.
 * Zero dependencies; Node stdlib only.
 *
 * WHAT IT LINTS
 *   A "consumer" is any directory that directly contains an `index.html`. The scan root itself
 *   counts if it has one (so this ships linting its own demo page), plus every immediate
 *   subdirectory that has one. For each consumer it reads `index.html` + every sibling `*.css`
 *   and `*.js` (this checker file is excluded so its own contract text can't produce false passes).
 *
 * USAGE
 *   node check.js [dir] [--dir <path>] [--md <report.md>]
 *     dir / --dir   Scan root (default: the directory this file lives in). Point it at wherever
 *                   your pattern-consumer pages live.
 *     --md <path>   Also write the Markdown report to <path> (otherwise only stdout).
 *
 * EXIT-CODE CONTRACT (CI-able)
 *   Exit code = the number of consumer pages with at least one UNACCEPTED failing check.
 *   0  = every consumer is conformant or its gaps are baselined in KNOWN_GAPS (green — merge).
 *   N>0 = N consumer pages have unaccepted gaps (red — fail the build).
 *   So a CI step is simply:  node check.js path/to/pages   (non-zero exit fails the job).
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

/* ---------------- CLI ---------------- */
function parseArgs(argv) {
  const args = { dir: null, md: null };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--dir") args.dir = argv[++i];
    else if (a === "--md") args.md = argv[++i];
    else if (!a.startsWith("--") && !args.dir) args.dir = a;
  }
  return args;
}
const ARGS = parseArgs(process.argv);
const ROOT = path.resolve(ARGS.dir || __dirname);

/* ---------------- contract checks ----------------
 * Each check gets { html, css, js, all } (lowercased) and returns true (pass) / false.
 * `why` documents the contract; `advice` says how to fix a failure.
 */
const CHECKS = [
  {
    id: "C1", name: "Honesty labeling (P1)",
    why: "Every simulated surface carries an on-screen demonstration disclaimer.",
    advice: "Add a permanent .gov-badge-style disclaimer naming what is simulated/illustrative.",
    test: (f) => /demonstration/.test(f.html) && /(simulat|illustrat|scripted|mock)/.test(f.html)
  },
  {
    id: "C2", name: "Trust receipt with deferral (P2)",
    why: "Consequential AI output lists what it did AND where it deferred to a human (receipt family incl. citations/sources-on-record).",
    advice: "Attach a receipt-family block with at least one deferral line (deferred / requires your approval / held pending).",
    test: (f) =>
      /(receipt|cited rule|sources on record|what just happened)/.test(f.all) &&
      /(defer|escalat|requires? (your |rn )?approval|held pending|awaiting your|only with (her|his|your) approval|confirms? (every|personally))/.test(f.all)
  },
  {
    id: "C3", name: "Refusal with a human path (P3)",
    why: "At least one refusal moment, and refusals route to a named human, never a dead end.",
    advice: "Add a refusal (won't answer/guess/invent/advise...) plus an escalation path (callback, consult, owner/RN/chef/prescriber...).",
    test: (f) =>
      /(refus|won'?t (answer|guess|quote|sketch|estimate|invent|auto-send)|never (advise|answer)|not mine to answer|do(es)?n'?t answer|won'?t weigh in)/.test(f.all) &&
      /(call(s|back)?|escalat|consult|verif|review|owner|chef|prescriber|\brn\b|clinician|human)/.test(f.all)
  },
  {
    id: "C4", name: "Approval gate with decided states (P4)",
    why: "Where owner-side actions exist: approve/adjust/decline with decided-state outcomes.",
    advice: "Use the Gov.gate contract (or equivalent) with all outcomes handled.",
    test: (f) => /approve/.test(f.all) && /(decided|outerhtml|✓|✎|✕)/.test(f.js)
  },
  {
    id: "C5", name: "Simulated outcomes labeled",
    why: "Interactive outcomes (sent/booked/posted) say '(simulated)' so a demo never claims a live effect.",
    advice: "Add '(simulated)' to every decided-state / success message.",
    test: (f) => !/sent|booked|posted|queued/.test(f.all) || /simulat/.test(f.all)
  },
  {
    id: "C6", name: "[hidden] guard",
    why: "If markup relies on the hidden attribute, CSS must guard it: author display rules silently defeat [hidden].",
    advice: "Add `[hidden] { display: none !important; }` to the stylesheet.",
    test: (f) => !/\bhidden\b/.test(f.html.replace(/aria-hidden/g, "")) || /\[hidden\]/.test(f.css)
  },
  {
    id: "C7", name: "A11y basics",
    why: "Skip link + at least one ARIA state/live region (a floor, not a full audit).",
    advice: "Add .skip-link and aria-live / aria-pressed where state changes.",
    test: (f) => /skip-link/.test(f.html) && /aria-(live|pressed)/.test(f.html)
  },
  {
    id: "C8", name: "SEO/GEO metadata",
    why: "Meta description + JSON-LD so pages model the discoverability fundamentals they preach.",
    advice: "Add <meta name=description> and a schema.org JSON-LD block.",
    test: (f) => /meta name="description"/.test(f.html) && /application\/ld\+json/.test(f.html)
  },
  {
    id: "C9", name: "Sealed record integrity (P7)",
    why: "Pages that SEAL records must carry the append-only rule (supersede/change-order language) and customer-facing verification verdicts.",
    advice: "State that seals are never edited (superseded / change orders get their own seal) and provide a verify path with VERIFIED/MISMATCH outcomes.",
    test: (f) => !/(seal(ed)? (quote|record)|seal this|re-seal|superseding seal)/.test(f.all) ||
      (/(supersed|change order)/.test(f.all) && /(verified|mismatch|verify)/.test(f.all))
  },
  {
    id: "C10", name: "Movement-log transparency (P8)",
    why: "Pages showing a queue position must name the cause of every movement and refuse invented dates (honest windows).",
    advice: "Every position change names its cause (declined/aged up/released...), and the timing promise is a range with an explicit no-invented-date refusal.",
    test: (f) => !/(waitlist|moved to #|position \d+ of|of \d+ ·)/.test(f.all) ||
      ((/(declin|aged up|moved out|released)/.test(f.all)) &&
       /(range, on purpose|won'?t (invent|tell you[^.]*exact)|honest (target )?window)/.test(f.all))
  },
  {
    id: "C11", name: "Charter amendment asymmetry (P9)",
    why: "Pages presenting a governance charter/never-list must carry the asymmetry: tightening instant, loosening signed, versions succeeded not edited.",
    advice: "State the tighten-instant / loosen-needs-fresh-signature rule and versioned (succeeded, never edited) history wherever a charter or never-list appears.",
    test: (f) => !/charter/.test(f.all) ||
      (/tighten/.test(f.all) && /signature/.test(f.all))
  }
];

/* ---------------- accepted gaps (baseline) ----------------
 * Documented exceptions, keyed by consumer name (its directory path relative to the scan root,
 * or the root's basename). Rendered as "known" (◐) in the report and EXCLUDED from the failure
 * exit code. Each entry maps a check id to a human reason. Shrink this list over time; never grow
 * it silently. Example shape:
 *
 *   const KNOWN_GAPS = {
 *     "public-trust-report": { C4: "read-only report by design; approval gates live in the products it reports on" }
 *   };
 */
const KNOWN_GAPS = {};

/* ---------------- runner ---------------- */
function read(p) {
  try { return fs.readFileSync(p, "utf8").toLowerCase(); } catch { return ""; }
}
function isDir(p) {
  try { return fs.statSync(p).isDirectory(); } catch { return false; }
}
function readAllMatching(dir, ext) {
  let out = "";
  let entries = [];
  try { entries = fs.readdirSync(dir); } catch { return ""; }
  for (const name of entries) {
    if (!name.toLowerCase().endsWith(ext)) continue;
    if (path.resolve(dir, name) === __filename) continue; // never lint the checker itself
    out += read(path.join(dir, name)) + "\n";
  }
  return out;
}

/* A consumer is a directory that directly contains an index.html: the scan root itself, plus
 * every immediate subdirectory. */
function findConsumers(root) {
  const dirs = [];
  if (fs.existsSync(path.join(root, "index.html"))) dirs.push(root);
  let entries = [];
  try { entries = fs.readdirSync(root); } catch { /* ignore */ }
  for (const name of entries) {
    const p = path.join(root, name);
    if (isDir(p) && fs.existsSync(path.join(p, "index.html"))) dirs.push(p);
  }
  return dirs;
}

function nameOf(dir) {
  const rel = path.relative(ROOT, dir);
  return rel === "" ? path.basename(ROOT) : rel;
}

function checkConsumer(dir) {
  const f = {
    html: read(path.join(dir, "index.html")),
    css: readAllMatching(dir, ".css"),
    js: readAllMatching(dir, ".js")
  };
  f.all = f.html + "\n" + f.js;
  if (!f.html) return null;
  const key = nameOf(dir);
  const known = KNOWN_GAPS[key] || {};
  return {
    dir: key,
    results: CHECKS.map((c) => {
      const pass = !!c.test(f);
      return { id: c.id, name: c.name, pass, advice: c.advice, known: !pass && known[c.id] ? known[c.id] : null };
    })
  };
}

const consumers = findConsumers(ROOT);
const reports = consumers.map(checkConsumer).filter(Boolean);

/* ---------------- output ---------------- */
let failsTotal = 0;
const lines = [];
lines.push("# Governance conformance report");
lines.push("");
lines.push("Generated by `check.js` — static contract linting (heuristic; checks that the pattern");
lines.push("contracts are present in source, not that runtime behavior is correct). Scan root: `" + nameOf(ROOT) + "`.");
lines.push("Regenerate: `node check.js --md CONFORMANCE.md`");
lines.push("");

if (!reports.length) {
  lines.push("_No pattern-consumer pages found under the scan root (a consumer is a directory with an `index.html`)._");
  console.log(lines.join("\n") + "\n");
  const mdIdxEmpty = process.argv.indexOf("--md");
  if (mdIdxEmpty > -1 && process.argv[mdIdxEmpty + 1]) fs.writeFileSync(process.argv[mdIdxEmpty + 1], lines.join("\n") + "\n");
  process.exit(0);
}

lines.push("| Page | " + CHECKS.map((c) => c.id).join(" | ") + " | Result |");
lines.push("|---|" + CHECKS.map(() => "---").join("|") + "|---|");

for (const r of reports) {
  const fails = r.results.filter((x) => !x.pass && !x.known);
  const knowns = r.results.filter((x) => x.known);
  if (fails.length) failsTotal++;
  lines.push("| `" + r.dir + "` | " +
    r.results.map((x) => (x.pass ? "✓" : x.known ? "◐" : "**✗**")).join(" | ") +
    " | " + (fails.length ? "**" + fails.length + " gap(s)**" :
             knowns.length ? knowns.length + " known" : "conformant") + " |");
}

lines.push("");
lines.push("## Contract key");
for (const c of CHECKS) lines.push("- **" + c.id + " — " + c.name + ":** " + c.why);
lines.push("");
lines.push("## Gaps in detail (✗ = unaccepted; fix or baseline with a reason)");
let anyGap = false;
for (const r of reports) {
  const fails = r.results.filter((x) => !x.pass && !x.known);
  if (!fails.length) continue;
  anyGap = true;
  lines.push("- **`" + r.dir + "`**: " + fails.map((x) => x.id + " (" + x.advice + ")").join("; "));
}
if (!anyGap) lines.push("_None — every page is conformant or carries a documented, reasoned exception._");
lines.push("");
lines.push("## Known/accepted gaps (◐ — documented exceptions, shrink over time)");
let anyKnown = false;
for (const r of reports) {
  const knowns = r.results.filter((x) => x.known);
  if (!knowns.length) continue;
  anyKnown = true;
  for (const k of knowns) lines.push("- **`" + r.dir + "` " + k.id + ":** " + k.known);
}
if (!anyKnown) lines.push("_None._");

const md = lines.join("\n") + "\n";
console.log(md);

const mdIdx = process.argv.indexOf("--md");
if (mdIdx > -1 && process.argv[mdIdx + 1]) {
  fs.writeFileSync(process.argv[mdIdx + 1], md);
  console.error("written: " + process.argv[mdIdx + 1]);
}
process.exit(failsTotal);
