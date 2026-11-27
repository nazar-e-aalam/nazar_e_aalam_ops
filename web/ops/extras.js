/* Quality-of-life: command palette (Ctrl/Cmd+K or /), keyboard shortcuts, "g then key" navigation. */
(function () {
  "use strict";
  var O = window.OPS, h = O.h, icon = O.icon, api = O.api, toast = O.toast, fail = O.fail;
  var pal = null, pending = null;

  function go(path) { closePal(); location.hash = "#/" + path; }
  function commands() {
    var S = O.S, gated = S.info && S.info.setup_done === false, admin = O.can("admin"), c = [];
    if (gated) return [{ t: "Setup guide", s: "Finish first-time setup", i: "gear", run: function () { go("setup"); } }];
    (O.NAV || []).forEach(function (n) { c.push({ t: n[1], s: "Go to page", i: n[2], k: n[0] === "" ? "g o" : "g " + n[1][0].toLowerCase(), run: function () { go(n[0]); } }); });
    c.push({ t: "Account settings", s: "Name, password, two-factor", i: "users", run: function () { go("settings/account"); } });
    c.push({ t: "Servers", s: "Add or check deploy servers (SSH, sudo, database)", i: "server", run: function () { go("servers"); } });
    c.push({ t: "Download proxy", s: "Releases · change or test gh-proxy", i: "link", run: function () { go("releases"); } });
    if (O.can("operator")) c.push({ t: "Sync releases now", s: "Copy new GitHub releases", i: "refresh", run: function () { closePal(); api("POST", "/api/library/sync/run").then(function () { toast("Sync started", "ok"); }).catch(fail); } });
    if (admin) c.push({ t: "Open setup guide again", s: "Domain, DNS, HTTPS walkthrough", i: "gear", run: function () { closePal(); api("POST", "/api/setup/reopen").then(function () { S.info.setup_done = false; go("setup"); }).catch(fail); } });
    c.push({ t: "Open public downloads page", s: S.info && S.info.public_url, i: "ext", run: function () { closePal(); window.open((S.info.public_url || "") + "/applications", "_blank", "noopener"); } });
    c.push({ t: "Toggle light / dark theme", s: "Appearance", i: "sun", run: function () { closePal(); window.Theme && Theme.toggle(); } });
    c.push({ t: "Keyboard shortcuts", s: "Press ? anywhere", i: "list", run: function () { closePal(); help(); } });
    return c;
  }
  function closePal() { if (pal) { pal.remove(); pal = null; } }
  function openPal() {
    if (pal) return closePal();
    var all = commands(), sel = 0, list = h("div", { class: "pal-list", role: "listbox" }), input = h("input", { type: "text", placeholder: "Type a command or page…", "aria-label": "Command", spellcheck: false, autocomplete: "off" });
    function score(c, q) { var t = (c.t + " " + (c.s || "")).toLowerCase(); if (!q) return 1; if (t.indexOf(q) >= 0) return 2 + (c.t.toLowerCase().indexOf(q) === 0 ? 1 : 0); var i = 0; for (var k = 0; k < t.length && i < q.length; k++) if (t[k] === q[i]) i++; return i === q.length ? 0.5 : 0; }
    var shown = all;
    function paint() {
      var q = input.value.trim().toLowerCase();
      shown = all.map(function (c) { return [c, score(c, q)]; }).filter(function (x) { return x[1] > 0; }).sort(function (a, b) { return b[1] - a[1]; }).map(function (x) { return x[0]; });
      sel = Math.min(sel, Math.max(shown.length - 1, 0));
      list.replaceChildren.apply(list, shown.length ? shown.map(function (c, i) {
        return h("button", { class: "pal-item" + (i === sel ? " on" : ""), type: "button", role: "option", onmouseenter: function () { sel = i; mark(); }, onclick: c.run }, icon(c.i || "info"), h("span", { class: "t" }, c.t, c.s ? h("small", null, c.s) : null), c.k ? h("kbd", null, c.k) : null);
      }) : [h("div", { class: "pal-empty" }, "No matches")]);
    }
    function mark() { Array.prototype.forEach.call(list.children, function (el, i) { el.classList.toggle("on", i === sel); if (i === sel && el.scrollIntoView) el.scrollIntoView({ block: "nearest" }); }); }
    input.addEventListener("input", function () { sel = 0; paint(); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { e.preventDefault(); sel = Math.min(sel + 1, shown.length - 1); mark(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); sel = Math.max(sel - 1, 0); mark(); }
      else if (e.key === "Enter") { e.preventDefault(); if (shown[sel]) shown[sel].run(); }
      else if (e.key === "Escape") { e.preventDefault(); closePal(); }
    });
    pal = h("div", { class: "pal-overlay", onmousedown: function (e) { if (e.target === pal) closePal(); } }, h("div", { class: "pal", role: "dialog", "aria-label": "Command palette" }, h("div", { class: "pal-in" }, icon("list"), input, h("kbd", null, "Esc")), list));
    document.body.appendChild(pal); paint(); input.focus();
  }
  function help() {
    var rows = [["Ctrl / ⌘ + K  or  /", "Command palette"], ["g then o / w / r / d / v / e / s / b", "Go to Overview, Workflows, Runs, Deployments, serVers, Environment, Settings, Releases"], ["F", "Full screen on a workflow run"], ["D", "Show / hide details in full screen"], ["Esc", "Close dialog, exit full screen"], ["?", "This help"]];
    O.modal("Keyboard shortcuts", h("div", { class: "kbd-list" }, rows.map(function (r) { return h("div", null, h("kbd", null, r[0]), h("span", null, r[1])); })), function (close) { return [h("button", { class: "btn primary", onclick: close }, "Got it")]; });
  }
  var G = { v: "servers", o: "", w: "workflows", r: "runs", d: "deploy", e: "env", s: "settings", b: "releases" };
  document.addEventListener("keydown", function (e) {
    var t = e.target, typing = t && (/^(input|textarea|select)$/i.test(t.tagName) || t.isContentEditable);
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { if (!O.S.me) return; e.preventDefault(); openPal(); return; }
    if (typing || e.ctrlKey || e.metaKey || e.altKey || !O.S.me || document.querySelector(".overlay")) return;
    if (e.key === "/" ) { e.preventDefault(); openPal(); return; }
    if (e.key === "?") { e.preventDefault(); help(); return; }
    if (pending) { var k = e.key.toLowerCase(); clearTimeout(pending.t); pending = null; if (k in G && !(O.S.info && O.S.info.setup_done === false)) { e.preventDefault(); go(G[k]); } return; }
    if (e.key === "g") pending = { t: setTimeout(function () { pending = null; }, 1200) };
  });
})();
