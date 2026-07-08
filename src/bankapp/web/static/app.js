/* BankApp dashboard shared runtime. No external requests — talks only to /api on this origin. */
(function () {
  "use strict";

  const App = {
    meta: null, // populated by loadMeta()
  };

  // ---- fetch helper -------------------------------------------------------
  App.api = async function (path) {
    try {
      const res = await fetch(path, { headers: { Accept: "application/json" } });
      if (!res.ok) throw new Error(`${path} -> HTTP ${res.status}`);
      return await res.json();
    } catch (err) {
      App.banner(`Could not load ${path}: ${err.message}`);
      throw err;
    }
  };

  // POST JSON to a write route. Surfaces server error detail via the banner and
  // rethrows so callers can keep the modal open on failure.
  App.post = async function (path, body) {
    let res;
    try {
      res = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(body || {}),
      });
    } catch (err) {
      App.banner(`Could not reach ${path}: ${err.message}`);
      throw err;
    }
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try { const j = await res.json(); if (j && j.detail) detail = j.detail; } catch (_) {}
      App.banner(`${path} failed: ${detail}`);
      throw new Error(detail);
    }
    return await res.json();
  };

  App.banner = function (msg) {
    const main = document.querySelector("main") || document.body;
    const el = document.createElement("div");
    el.className = "err-banner";
    const span = document.createElement("span");
    span.textContent = msg;
    const btn = document.createElement("button");
    btn.textContent = "×";
    btn.onclick = () => el.remove();
    el.appendChild(span);
    el.appendChild(btn);
    main.insertBefore(el, main.firstChild);
  };

  // Transient success notice (auto-dismisses). Distinct from the error banner.
  App.notice = function (msg, ms) {
    const main = document.querySelector("main") || document.body;
    const el = document.createElement("div");
    el.className = "notice-banner";
    el.textContent = msg;
    main.insertBefore(el, main.firstChild);
    setTimeout(() => el.remove(), ms || 3500);
  };

  // ---- meta (currencies/exponents, filter options) ------------------------
  App.loadMeta = async function () {
    if (App.meta) return App.meta;
    App.meta = await App.api("/api/meta");
    return App.meta;
  };

  App.exponentFor = function (currency) {
    const c = App.meta && App.meta.currencies ? App.meta.currencies[currency] : undefined;
    return typeof c === "number" ? c : 2;
  };

  // minor integer units -> "1,234.56 CAD"
  App.fmtMoney = function (minor, currency) {
    currency = currency || "CAD";
    const exp = App.exponentFor(currency);
    const major = (minor || 0) / Math.pow(10, exp);
    const fmt = new Intl.NumberFormat(undefined, {
      minimumFractionDigits: exp,
      maximumFractionDigits: exp,
    });
    return `${fmt.format(major)} ${currency}`;
  };

  App.fmtSignedMoney = function (minor, currency) {
    const s = App.fmtMoney(minor, currency);
    return (minor > 0 ? "+" : "") + s;
  };

  // ---- SAFE markdown (escape-first whitelist; never raw innerHTML) ---------
  App.renderMarkdown = function (md) {
    const esc = String(md == null ? "" : md)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");

    const lines = esc.split(/\r?\n/);
    const out = [];
    let inList = false;
    const closeList = () => { if (inList) { out.push("</ul>"); inList = false; } };
    const inline = (t) => t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");

    for (const raw of lines) {
      const line = raw.trimEnd();
      let m;
      if ((m = /^###\s+(.*)$/.exec(line))) { closeList(); out.push(`<h3>${inline(m[1])}</h3>`); }
      else if ((m = /^##\s+(.*)$/.exec(line))) { closeList(); out.push(`<h2>${inline(m[1])}</h2>`); }
      else if ((m = /^#\s+(.*)$/.exec(line))) { closeList(); out.push(`<h1>${inline(m[1])}</h1>`); }
      else if ((m = /^[-*]\s+(.*)$/.exec(line))) {
        if (!inList) { out.push("<ul>"); inList = true; }
        out.push(`<li>${inline(m[1])}</li>`);
      } else if (line.trim() === "") {
        closeList();
      } else {
        closeList();
        out.push(`<p>${inline(line)}</p>`);
      }
    }
    closeList();
    return out.join("\n");
  };

  // ---- theming for charts (reads the SAME CSS custom properties) -----------
  App.cssVar = function (name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  };

  App.chartTheme = function () {
    if (typeof Chart === "undefined") return;
    Chart.defaults.color = App.cssVar("--muted");
    Chart.defaults.borderColor = App.cssVar("--edge");
    Chart.defaults.font.family = App.cssVar("--mono") || "monospace";
    Chart.defaults.font.size = 11;
    Chart.defaults.animation = false;
    Chart.defaults.plugins.legend.labels.boxWidth = 12;
    Chart.defaults.maintainAspectRatio = false;
  };

  App.palette = function () {
    return [
      App.cssVar("--accent"), App.cssVar("--warn"), App.cssVar("--neg"),
      "#6ea8fe", "#c58af9", "#8aa39a", App.cssVar("--pos"),
    ];
  };

  // ---- shared nav + freshness chip ----------------------------------------
  const TABS = [
    ["/", "Overview"],
    ["/transactions.html", "Transactions"],
    ["/subscriptions.html", "Subscriptions"],
    ["/goals.html", "Goals"],
    ["/receivables.html", "Receivables"],
    ["/advice.html", "Advice"],
  ];

  App.nav = async function () {
    const here = location.pathname === "" ? "/" : location.pathname;
    const nav = document.createElement("nav");
    nav.className = "nav";
    const brand = document.createElement("div");
    brand.className = "brand";
    brand.innerHTML = "bank<b>app</b>";
    nav.appendChild(brand);
    for (const [href, label] of TABS) {
      const a = document.createElement("a");
      a.className = "tab" + (href === here ? " active" : "");
      a.href = href;
      a.textContent = label;
      nav.appendChild(a);
    }
    const spacer = document.createElement("div");
    spacer.className = "spacer";
    nav.appendChild(spacer);
    const chip = document.createElement("div");
    chip.className = "chip";
    chip.textContent = "loading...";
    nav.appendChild(chip);
    document.body.insertBefore(nav, document.body.firstChild);

    try {
      const st = await App.api("/api/status");
      const imp = st.last_import || "never";
      const ws = st.last_ws_sync || "never";
      chip.innerHTML = `<b>import</b> ${short(imp)} · <b>ws</b> ${short(ws)}`;
    } catch (e) {
      chip.textContent = "status unavailable";
    }
  };

  function short(ts) {
    if (!ts || ts === "never") return "never";
    return String(ts).slice(0, 10);
  }
  App.shortDate = short;

  // ---- empty state --------------------------------------------------------
  App.empty = function (el, msg) {
    el.innerHTML = "";
    const d = document.createElement("div");
    d.className = "empty";
    d.innerHTML = msg || "No data yet — run <code>finance refresh</code>";
    el.appendChild(d);
  };

  App.el = function (id) { return document.getElementById(id); };

  App.esc = function (s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  };

  window.App = App;
})();
