/* Governed-AI Interface Pattern Library — reference behaviors.
 *
 * Exposes window.Gov with two initializers (gate, policy quote) used by the demo page and
 * copy-pastable into consumer pages. Page-only: the theme switcher (proves token-only theming).
 *
 * Vendor-neutral. All example content is fictional and labeled as a simulation.
 */
(function () {
  "use strict";

  var Gov = {};

  /* ---- P4: approval gate ----------------------------------------------
   * <div class="gov-gate" data-approve="..." data-adjust="..." data-decline="...">
   *   <p class="gg-draft">…full draft…</p>
   *   <div class="gg-actions">
   *     <button class="gg-approve">…</button><button class="gg-adjust">…</button><button class="gg-decline">…</button>
   *   </div>
   * </div>
   * Every outcome REPLACES the actions with a decided-state line naming what happened.
   */
  Gov.gate = function (el) {
    if (!el) return;
    var actions = el.querySelector(".gg-actions");
    if (!actions) return;
    function decide(cls, msg) {
      actions.outerHTML = '<p class="gg-decided ' + cls + '">' + msg + "</p>";
    }
    var map = [
      [".gg-approve", "ok", el.getAttribute("data-approve") || "✓ Approved."],
      [".gg-adjust", "adj", el.getAttribute("data-adjust") || "✎ Opened for editing."],
      [".gg-decline", "no", el.getAttribute("data-decline") || "✕ Declined."]
    ];
    map.forEach(function (m) {
      var b = actions.querySelector(m[0]);
      if (b) b.addEventListener("click", function () { decide(m[1], m[2]); });
    });
  };

  /* ---- P5: pricing-policy engine ---------------------------------------
   * Gov.quote(policy, picks) -> { lines: [[label, display]], low, high }
   * policy: { version, spread, items: { key: { label, kind: "base"|"mult", value } } }
   * Deterministic; quotes a RANGE; pair with a P2 receipt and a P4 gate downstream.
   */
  Gov.quote = function (policy, picks) {
    var subtotal = 0, lines = [];
    Object.keys(policy.items).forEach(function (k) {
      if (!picks[k]) return;
      var it = policy.items[k];
      if (it.kind === "base") {
        subtotal += it.value;
        lines.push([it.label, "$" + it.value]);
      } else {
        var before = subtotal;
        subtotal = subtotal * it.value;
        lines.push([it.label + " ×" + it.value, "+$" + Math.round(subtotal - before)]);
      }
    });
    var spread = policy.spread || 0.1;
    return {
      lines: lines,
      low: Math.round(subtotal * (1 - spread) / 5) * 5,
      high: Math.round(subtotal * (1 + spread) / 5) * 5
    };
  };

  window.Gov = Gov;

  /* ================= page demo wiring ================= */

  // P4 demo
  Gov.gate(document.getElementById("demo-gate"));

  // P5 demo
  var POLICY = {
    version: "v3 (demo)", spread: 0.1,
    items: {
      base: { label: "Base service", kind: "base", value: 180 },
      size: { label: "Large size", kind: "mult", value: 1.3 },
      cond: { label: "Heavy condition", kind: "mult", value: 1.2 }
    }
  };
  var picks = { base: true, size: false, cond: false };
  var linesEl = document.getElementById("gp-lines");
  var rangeEl = document.getElementById("gp-range");

  function renderQuote() {
    var q = Gov.quote(POLICY, picks);
    linesEl.innerHTML = q.lines.map(function (l) {
      return "<li><span>" + l[0] + "</span><span>" + l[1] + "</span></li>";
    }).join("");
    rangeEl.textContent = q.lines.length ? "$" + q.low + " – $" + q.high : "pick at least the base service";
  }
  document.querySelectorAll(".gp-pick").forEach(function (b) {
    b.addEventListener("click", function () {
      var k = b.getAttribute("data-k");
      picks[k] = !picks[k];
      b.setAttribute("aria-pressed", String(picks[k]));
      renderQuote();
    });
  });
  if (linesEl) renderQuote();

  // theme switcher (library page only — demonstrates token-only reskinning)
  document.querySelectorAll(".tb-btn").forEach(function (b) {
    b.addEventListener("click", function () {
      document.querySelectorAll(".tb-btn").forEach(function (x) { x.setAttribute("aria-pressed", "false"); });
      b.setAttribute("aria-pressed", "true");
      document.documentElement.setAttribute("data-theme", b.getAttribute("data-theme"));
    });
  });
})();

/* ---------- P7–P9 demos ---------- */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };

  /* P7 sealed record */
  if ($("p7-verify")) {
    $("p7-verify").addEventListener("click", function () {
      $("p7-out").innerHTML = "<span class=\"p7-ok\">VERIFIED (simulated)</span> — record matches: " +
        "$1,420–$1,690, no signed change orders. If anyone asks for more, this record stands.";
    });
    $("p7-test").addEventListener("click", function () {
      $("p7-out").innerHTML = "<span class=\"p7-bad\">MISMATCH (simulated)</span> — $2,890 is outside " +
        "the sealed range and no signed change order explains it. The sealed record wins; " +
        "here is the named human path.";
    });
  }

  /* P8 movement log */
  if ($("p8-decline")) {
    $("p8-decline").addEventListener("click", function () {
      var num = $("p8-num");
      var n = parseInt(num.textContent, 10) - 1;
      num.textContent = String(n);
      var li = document.createElement("li");
      li.innerHTML = "<b>NOW</b> Moved to #" + n +
        " — a family ahead declined (simulated). Declines never push anyone down, only up.";
      $("p8-log").appendChild(li);
      if (n <= 2) {
        this.disabled = true;
        li.innerHTML += " At the top: no invented dates — a real opening gets a personal call.";
      }
    });
  }

  /* P9 signed charter */
  if ($("p9-tighten")) {
    $("p9-tighten").addEventListener("click", function () {
      $("p9-row").innerHTML = "Quotes under $500: <b>your approval — tightened just now</b>";
      $("p9-out").textContent = "Applied instantly, logged, unsigned (simulated) — restrictions never wait.";
      this.disabled = true;
    });
    $("p9-loosen").addEventListener("click", function () {
      $("p9-sign").hidden = false;
      this.disabled = true;
    });
    $("p9-confirm").addEventListener("click", function () {
      var name = ($("p9-name").value || "").trim();
      if (!name) { $("p9-name").placeholder = "A signature is the whole point"; return; }
      $("p9-ver").textContent = "v5";
      $("p9-row").innerHTML = "Quotes under $500: <b>autonomous — reversible (signed by " + name + ")</b>";
      $("p9-sign").hidden = true;
      $("p9-out").textContent = "v5 signed and in force (simulated). v4 keeps its place in history — " +
        "versions are never edited, only succeeded.";
    });
  }
})();
