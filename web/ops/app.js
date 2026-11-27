/* Nazar Ops: core (helpers, router, shell, overview, workflows, runs). Pages for deploy/env/releases/settings live in pages.js */
(function () {
  "use strict";
  var BASE = location.pathname.replace(/\/+$/, "").replace(/\/index\.html$/, ""); // "" on ops.domain, "/ops" in dev
  /* The native replaceChildren turns null/false into the text "null"/"false". Ignore them and flatten arrays. */
  (function () {
    var native = Element.prototype.replaceChildren;
    function flat(a, out) { a.forEach(function (c) { if (c == null || c === false) return; if (Array.isArray(c)) flat(c, out); else out.push(c); }); return out; }
    Element.prototype.replaceChildren = function () { return native.apply(this, flat(Array.prototype.slice.call(arguments), [])); };
  })();
  var $app = document.getElementById("app");
  var S = { me: null, info: null, timers: [], graph: null, route: null };
  window.OPS = { S: S, pages: {} };

  /* ---------- tiny DOM helper ---------- */
  function h(tag, props) {
    var e = document.createElement(tag), kids = Array.prototype.slice.call(arguments, 2);
    for (var k in (props || {})) {
      var v = props[k];
      if (v == null || v === false) continue;
      if (k === "class") e.className = v;
      else if (k === "html") throw new Error("html prop is disabled");
      else if (k.slice(0, 2) === "on") e.addEventListener(k.slice(2), v);
      else if (k === "style" && typeof v === "object") Object.assign(e.style, v);
      else if (k in e && k !== "list" && k !== "type" && k !== "for") e[k] = v;
      else e.setAttribute(k, v === true ? "" : v);
    }
    (function add(list) {
      list.forEach(function (c) {
        if (c == null || c === false) return;
        if (Array.isArray(c)) return add(c);
        e.appendChild(c.nodeType ? c : document.createTextNode(String(c)));
      });
    })(kids);
    return e;
  }
  var ICON = {
    home: "M3 11l9-8 9 8M5 10v10h5v-6h4v6h5V10", mail: "M3 5h18v14H3zM3 7l9 6 9-6", download: "M12 3v12m-5-5l5 5 5-5M4 21h16",
    play: "M6 4l14 8-14 8z", runs: "M4 6h16M4 12h16M4 18h10", rocket: "M5 15c-1 3-1 5-1 5s2 0 5-1M14 4c4-1 6-1 6-1s0 2-1 6c-1 3-4 6-7 7l-4-4c1-3 4-6 6-8zM15 9h.01",
    key: "M21 2l-2 2m-7.6 7.6a5 5 0 1 1-7 7 5 5 0 0 1 7-7zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3",
    box: "M21 8l-9-5-9 5v8l9 5 9-5zM3 8l9 5 9-5M12 13v8", gear: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19 12a7 7 0 0 0-.1-1.2l2-1.5-2-3.4-2.3 1a7 7 0 0 0-2-1.2L14 3h-4l-.6 2.7a7 7 0 0 0-2 1.2l-2.3-1-2 3.4 2 1.5a7 7 0 0 0 0 2.4l-2 1.5 2 3.4 2.3-1a7 7 0 0 0 2 1.2L10 21h4l.6-2.7a7 7 0 0 0 2-1.2l2.3 1 2-3.4-2-1.5c.1-.4.1-.8.1-1.2z",
    sun: "M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10zM12 1v2M12 21v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M1 12h2M21 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4",
    moon: "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z", out: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9",
    menu: "M3 6h18M3 12h18M3 18h18", check: "M5 12l5 5L20 7", x: "M6 6l12 12M18 6L6 18", refresh: "M21 12a9 9 0 1 1-3-6.7L21 8M21 3v5h-5",
    up: "M12 19V5M5 12l7-7 7 7", download: "M12 3v12m0 0l-5-5m5 5l5-5M4 21h16", trash: "M4 7h16M10 11v6M14 11v6M5 7l1 13h12l1-13M9 7V4h6v3",
    eye: "M1 12s4-8 11-8 11 8 11 8-4 8-11 8S1 12 1 12zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z", eyeoff: "M17.9 17.9A10.9 10.9 0 0 1 12 20C5 20 1 12 1 12a18 18 0 0 1 5-5.9M9.9 4.2A9 9 0 0 1 12 4c7 0 11 8 11 8a18 18 0 0 1-2.2 3.2M1 1l22 22",
    expand: "M8 3H5a2 2 0 0 0-2 2v3M21 8V5a2 2 0 0 0-2-2h-3M3 16v3a2 2 0 0 0 2 2h3M16 21h3a2 2 0 0 0 2-2v-3", shrink: "M8 3v3a2 2 0 0 1-2 2H3M21 8h-3a2 2 0 0 1-2-2V3M3 16h3a2 2 0 0 1 2 2v3M16 21v-3a2 2 0 0 1 2-2h3", panel: "M3 4h18v16H3zM15 4v16",
    link: "M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1",
    users: "M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM23 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8",
    alert: "M12 9v4M12 17h.01M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z", info: "M12 16v-4M12 8h.01M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z",
    back: "M19 12H5M12 19l-7-7 7-7", ext: "M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6M15 3h6v6M10 14L21 3", stop: "M6 6h12v12H6z", undo: "M3 7v6h6M3 13a9 9 0 1 0 3-7.7L3 8",
    server: "M4 4h16v6H4zM4 14h16v6H4zM8 7h.01M8 17h.01", plus: "M12 5v14M5 12h14", shield: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z", file: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6", copy: "M9 9h11v11H9zM5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1",
    list: "M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01", server: "M2 4h20v6H2zM2 14h20v6H2zM6 7h.01M6 17h.01", android: "M5 16V9h14v7a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2zM8 3l1.5 2.5M16 3l-1.5 2.5M9.5 12h.01M14.5 12h.01", apple: "M12 7c-1-2-3-3-5-2-3 1-3 6-1 10 1 2 2 4 4 4 1 0 1.500-.5 2-.5s1 .5 2 .5c2 0 3-2 4-4-3-1-3-5-1-6-1-2-3-2-4-1.500-.5.2-.7.5-1 .5z"
  };
  function icon(n, cls) {
    var s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("class", "i" + (cls ? " " + cls : "")); s.setAttribute("aria-hidden", "true");
    var p = document.createElementNS("http://www.w3.org/2000/svg", "path"); p.setAttribute("d", ICON[n] || ICON.info); s.appendChild(p); return s;
  }

  /* ---------- formatting ---------- */
  function ts(v) { return typeof v === "number" ? v * 1000 : new Date(v).getTime(); }
  function ago(v) {
    if (!v) return "never"; var s = (Date.now() - ts(v)) / 1000;
    if (s < 0) s = 0; if (s < 10) return "just now"; if (s < 60) return Math.floor(s) + "s ago"; if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago"; if (s < 86400 * 30) return Math.floor(s / 86400) + "d ago";
    return new Date(ts(v)).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  }
  function dt(v) { return v ? new Date(ts(v)).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—"; }
  function bytes(n) { if (n == null) return "—"; var u = ["B", "KB", "MB", "GB", "TB"], i = 0; while (n >= 1024 && i < 4) { n /= 1024; i++; } return (i ? n.toFixed(n < 10 ? 1 : 0) : n) + " " + u[i]; }
  function dur(sec) { if (sec == null) return ""; sec = Math.max(0, Math.round(sec)); if (sec < 60) return sec + "s"; var m = Math.floor(sec / 60); if (m < 60) return m + "m " + (sec % 60) + "s"; return Math.floor(m / 60) + "h " + (m % 60) + "m"; }
  function initials(s) { return (s || "?").trim().slice(0, 2).toUpperCase(); }
  var LABEL = { success: "Passed", failed: "Failed", running: "Running", queued: "Queued", cancelled: "Cancelled", skipped: "Skipped" };
  function chip(status, text) { return h("span", { class: "chip " + status }, h("i", { class: "d" }), text || LABEL[status] || status); }
  function avatar(name, url, sm) { return h("span", { class: "avatar" + (sm ? " sm" : "") }, url ? h("img", { src: url, alt: "", referrerPolicy: "no-referrer" }) : initials(name)); }
  function empty(ic, title, sub, action) { return h("div", { class: "empty" }, icon(ic), h("b", null, title), sub ? h("span", null, sub) : null, action); }
  function skel(n) { var a = []; for (var i = 0; i < (n || 3); i++) a.push(h("div", { class: "skel", style: { height: "46px", marginBottom: "8px" } })); return h("div", null, a); }

  /* ---------- api / ui feedback ---------- */
  function api(method, url, body, opts) {
    var o = { method: method, headers: { "X-Requested-With": "ops" }, credentials: "same-origin" };
    if (body instanceof FormData) o.body = body; else if (body !== undefined) { o.headers["Content-Type"] = "application/json"; o.body = JSON.stringify(body); }
    return fetch(BASE + url, o).then(function (r) {
      var raw = opts && opts.raw;
      return (raw ? r.text() : r.json().catch(function () { return {}; })).then(function (d) {
        if (r.status === 401 && url.indexOf("/api/auth/") !== 0) { S.me = null; OPS.renderLogin(); throw new Error("Session expired, sign in again"); }
        if (!r.ok) { var e = new Error((d && d.detail) || "Request failed (" + r.status + ")"); e.status = r.status; throw e; }
        return d;
      });
    });
  }
  function toast(msg, kind) {
    var t = h("div", { class: "toast " + (kind || "info"), role: "status" }, icon(kind === "ok" ? "check" : kind === "err" ? "alert" : "info"), h("span", null, msg));
    document.getElementById("toasts").appendChild(t); setTimeout(function () { t.remove(); }, kind === "err" ? 7000 : 3800);
  }
  function fail(e) { toast(e.message || String(e), "err"); }
  function busy(btn, p) {
    btn.classList.add("busy"); btn.disabled = true;
    return p.finally(function () { btn.classList.remove("busy"); btn.disabled = false; });
  }
  function modal(title, body, actions, wide) {
    var ov = h("div", { class: "overlay", onclick: function (e) { if (e.target === ov) close(); } });
    function close() { ov.remove(); document.removeEventListener("keydown", esc); }
    function esc(e) { if (e.key === "Escape") close(); }
    document.addEventListener("keydown", esc);
    var m = h("div", { class: "modal" + (wide ? " wide" : ""), role: "dialog", "aria-modal": "true", "aria-label": title },
      h("header", null, h("h2", null, title), h("button", { class: "btn ghost icon sm", "aria-label": "Close", onclick: close }, icon("x"))),
      h("div", { class: "body" }, body), actions ? h("footer", null, actions(close)) : null);
    ov.appendChild(m); document.body.appendChild(ov);
    var f = m.querySelector("input,select,textarea"); if (f) setTimeout(function () { f.focus(); }, 30);
    return { close: close, el: m };
  }
  function confirmBox(title, text, okLabel, danger) {
    return new Promise(function (res) {
      var done = false;
      var m = modal(title, h("p", null, text), function (close) {
        return [h("button", { class: "btn", onclick: function () { done = true; close(); res(false); } }, "Cancel"),
          h("button", { class: "btn " + (danger ? "danger solid" : "primary"), onclick: function () { done = true; close(); res(true); } }, okLabel || "Confirm")];
      });
      m.el.parentNode.addEventListener("remove", function () { });
      new MutationObserver(function (_, ob) { if (!document.body.contains(m.el) && !done) { done = true; res(false); ob.disconnect(); } }).observe(document.body, { childList: true });
    });
  }
  function copy(text, label) {
    var ok = function () { toast((label || "Copied") + " to clipboard", "ok"); };
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(ok, function () { toast("Copy failed", "err"); });
    else { var t = h("textarea", { value: text, style: { position: "fixed", opacity: 0 } }); document.body.appendChild(t); t.select(); try { document.execCommand("copy"); ok(); } catch (e) { toast("Copy failed", "err"); } t.remove(); }
  }
  function poll(fn, ms) { S.timers.push(setInterval(function () { if (!document.hidden) fn(); }, ms)); }
  function stopAll() { S.timers.forEach(clearInterval); S.timers = []; if (S.graph) { S.graph.destroy(); S.graph = null; } }
  function can(role) { return S.me && (role === "admin" ? S.me.role === "admin" : S.me.role !== "viewer"); }
  Object.assign(OPS, { S: S, NAV: null, h: h, icon: icon, api: api, toast: toast, fail: fail, busy: busy, modal: modal, confirm: confirmBox, copy: copy, poll: poll, can: can, base: BASE,
    fmt: { ago: ago, dt: dt, bytes: bytes, dur: dur, initials: initials }, chip: chip, avatar: avatar, empty: empty, skel: skel, LABEL: LABEL });

  /* ---------- shell + router ---------- */
  var NAV = [["", "Overview", "home"], ["workflows", "Workflows", "play"], ["runs", "Runs", "runs"], ["deploy", "Deployments", "rocket"], ["servers", "Servers", "server"],
    ["env", "Environment", "key"], ["releases", "Releases", "box"], ["settings", "Settings", "gear"]];

  OPS.NAV = NAV;

  function themeBtn() {
    var b = h("button", { class: "btn ghost sm", "aria-label": "Toggle light/dark theme", onclick: function () { Theme.toggle(); } });
    function paint() { b.replaceChildren(icon(Theme.get() === "dark" ? "sun" : "moon"), Theme.get() === "dark" ? "Light" : "Dark"); }
    paint(); window.addEventListener("themechange", paint); return b;
  }
  function shell() {
    var nav = h("nav", { class: "nav", "aria-label": "Main" }, NAV.map(function (n) {
      return h("a", { href: BASE + "/#/" + n[0], "data-r": n[0], onclick: function () { document.body.classList.remove("nav-open"); } }, icon(n[2]), n[1], n[0] === "runs" ? h("span", { class: "badge", id: "runbadge", hidden: true }) : null);
    }));
    var side = h("aside", { class: "side" },
      h("div", { class: "brand" }, h("img", { class: "logo", src: BASE + "/favicon.svg", alt: "" }), h("div", null, "Nazar Ops", h("small", null, S.info ? S.info.org : ""))), nav,
      h("div", { class: "foot" }, h("a", { class: "me", href: BASE + "/#/settings/account", title: "Account settings", "aria-label": "Open account settings", onclick: function () { document.body.classList.remove("nav-open"); } }, avatar(S.me.name || S.me.email), h("div", { class: "t" }, h("b", null, S.me.name || S.me.email), h("small", null, S.me.role + " · v" + S.info.version)), icon("gear", "chev")),
        h("div", { class: "row" }, themeBtn(), h("span", { class: "sp" }), h("button", { class: "btn ghost sm", onclick: function () { api("POST", "/api/auth/logout").finally(function () { S.me = null; OPS.renderLogin(); }); } }, icon("out"), "Sign out"))));
    var title = h("h1", { id: "ptitle" });
    var top = h("header", { class: "top" }, h("button", { class: "btn ghost icon menu-btn", "aria-label": "Menu", onclick: function () { document.body.classList.toggle("nav-open"); } }, icon("menu")), title, h("span", { class: "sp" }),
      h("span", { id: "topslot", class: "row" }),
      S.info ? h("a", { class: "btn sm", href: S.info.public_url + "/applications", target: "_blank", rel: "noopener" }, icon("ext"), "Public page") : null);
    var page = h("main", { class: "page", id: "page" });
    $app.replaceChildren(h("div", { class: "shell" }, side, h("div", { class: "main" }, top, page)), h("div", { class: "scrim", onclick: function () { document.body.classList.remove("nav-open"); } }));
  }
  function setTitle(parts) {
    var t = document.getElementById("ptitle"); if (!t) return;
    t.replaceChildren.apply(t, [].concat(parts).map(function (p, i, a) { return i < a.length - 1 ? h("span", { class: "crumb" }, p, " /") : p; }));
    document.title = [].concat(parts).slice(-1)[0] + " · Nazar Ops";
  }
  function route() {
    if (!S.me) return;
    stopAll();
    var hash = location.hash.replace(/^#\/?/, ""), seg = hash.split("?")[0].split("/").filter(Boolean), name = seg[0] || "";
    var gated = S.info && S.info.setup_done === false;               // first-run: nothing but the setup guide until it is finished
    document.body.classList.toggle("setup-mode", !!gated);
    if (gated && name !== "setup") { seg = ["setup"]; name = "setup"; if (location.hash !== "#/setup") history.replaceState(null, "", BASE + "/#/setup"); }
    document.querySelectorAll(".nav a").forEach(function (a) { a.classList.toggle("on", a.getAttribute("data-r") === name); });
    var page = document.getElementById("page"), slot = document.getElementById("topslot"); slot.replaceChildren();
    page.replaceChildren(); window.scrollTo(0, 0);
    var fn = OPS.pages[name] || OPS.pages["404"];
    try { fn(page, seg.slice(1), { setTitle: setTitle, slot: slot }); } catch (e) { console.error(e); page.replaceChildren(empty("alert", "Something broke on this page", e.message)); }
  }
  OPS.go = function (path) { location.hash = "#/" + path; };
  Object.assign(OPS, { shell: shell, route: route, themeBtn: themeBtn, $app: $app, stop: stopAll });

  /* ---------- Overview ---------- */
  OPS.pages[""] = function (page, _, ctx) {
    ctx.setTitle("Overview");
    var box = h("div", { class: "col", style: { gap: "18px" } }, skel(4)); page.appendChild(box);
    function paint(d) {
      var srv = d.server, healthy = srv.health && srv.health.ok, cur = srv.current;
      var tokenOk = d.token.set;
      var tiles = h("div", { class: "grid g4" },
        h("div", { class: "card tile " + (!srv.health ? "" : healthy ? "ok" : "bad") }, h("i", { class: "glow" }), h("div", { class: "k" }, icon("server"), "Production server"),
          h("div", { class: "big" }, !srv.health ? "Not monitored" : healthy ? "Healthy" : "Down"), h("div", { class: "sub" }, srv.health ? (srv.health.ms != null ? srv.health.ms + " ms" : srv.health.error || "") : "Set a health URL in Settings")),
        h("div", { class: "card tile" }, h("i", { class: "glow" }), h("div", { class: "k" }, icon("rocket"), "Live version"),
          h("div", { class: "big mono" }, cur ? cur.short_sha : srv.active ? srv.active.split("-").pop() : "—"), h("div", { class: "sub" }, cur ? (cur.message || "").slice(0, 46) + " · " + ago(cur.finished_at) : "Nothing deployed yet")),
        h("div", { class: "card tile " + (d.running ? "run" : "") }, h("i", { class: "glow" }), h("div", { class: "k" }, icon("play"), "App workflows"),
          h("div", { class: "big" }, d.runs_error ? "—" : d.running ? d.running + " running" : "Idle"), h("div", { class: "sub" }, d.runs_error ? d.runs_error : d.runs.length ? "Last: " + (d.runs[0].title || d.runs[0].name) : "No runs yet")),
        h("div", { class: "card tile " + (tokenOk ? "ok" : "bad") }, h("i", { class: "glow" }), h("div", { class: "k" }, icon("shield"), "GitHub token"),
          h("div", { class: "big" }, tokenOk ? "Connected" : "Missing"), h("div", { class: "sub" }, d.token.rate && d.token.rate.remaining != null ? d.token.rate.remaining + " API calls left" : tokenOk ? "Open Settings to verify" : "Add one in Settings")));
      var runs = h("div", { class: "card" }, h("h3", null, "Recent app runs", h("span", { class: "sp" }), h("a", { class: "btn sm", href: "#/runs" }, "All runs")),
        d.runs_error ? empty("alert", "Cannot load runs", d.runs_error, h("a", { class: "btn sm", href: "#/settings/github" }, "Check token")) :
          d.runs.length ? d.runs.slice(0, 6).map(runRow) : empty("runs", "No runs yet", "Start one from Workflows", h("a", { class: "btn primary sm", href: "#/workflows" }, "Run a workflow")));
      var dep = h("div", { class: "card" }, h("h3", null, "Recent deployments", h("span", { class: "sp" }), h("a", { class: "btn sm", href: "#/deploy" }, "Open")),
        d.last_deploys.length ? d.last_deploys.map(depRow) : empty("rocket", "No deployments yet", "Deploy the panel repo from Deployments"));
      var online = h("div", { class: "card" }, h("h3", null, "Signed in now"), h("div", { class: "col", style: { gap: "10px" } }, d.online.map(function (u) {
        return h("div", { class: "row" }, avatar(u.name || u.email, null, true), h("div", null, h("b", null, u.name || u.email), h("div", { class: "sub" }, u.email + " · " + u.role)), h("span", { class: "sp" }), h("span", { class: "sub" }, ago(u.last_seen)));
      })));
      var lib = h("div", { class: "card" }, h("h3", null, "Public downloads", h("span", { class: "sp" }), h("a", { class: "btn sm", href: "#/releases" }, "Manage")),
        d.library.map(function (a) {
          var l = a.latest || {};
          return h("div", { class: "row wrap", style: { padding: "6px 0" } }, h("b", null, a.name), h("span", { class: "sp" }),
            chip(l.android ? "success" : "queued", "Android " + (l.android ? l.android.version : "none")), chip(l.ios ? "success" : "queued", "iOS " + (l.ios ? l.ios.version : "none")),
            h("span", { class: "sub" }, a.downloads + " downloads"));
        }));
      var act = h("div", { class: "card" }, h("h3", null, "Activity"), h("div", { class: "feed" }, d.activity.map(function (a) {
        return h("div", { class: "it" }, h("div", null, h("span", { class: "a" }, a.user_email), " ", h("span", { class: "mute" }, a.action), a.detail ? h("div", { class: "sub mono", style: { wordBreak: "break-all" } }, a.detail.slice(0, 90)) : null), h("time", null, ago(a.ts)));
      })));
      box.replaceChildren(tiles, h("div", { class: "grid g2" }, runs, dep), h("div", { class: "grid g3" }, lib, online, act));
      var b = document.getElementById("runbadge"); if (b) { b.hidden = !d.running; b.textContent = d.running; }
    }
    function load() { api("GET", "/api/overview").then(paint).catch(function (e) { if (!box.children.length || box.querySelector(".skel")) box.replaceChildren(empty("alert", "Could not load", e.message)); }); }
    load(); poll(load, 6000);
  };
  function runRow(r, key) {
    return h("div", { class: "runrow", onclick: function () { OPS.go("runs/" + (key || "app") + "/" + r.id); }, tabindex: 0, role: "link", onkeydown: function (e) { if (e.key === "Enter") OPS.go("runs/" + (key || "app") + "/" + r.id); } },
      chip(r.status), h("div", { style: { minWidth: 0 } }, h("div", { class: "t" }, r.title || r.name),
        h("div", { class: "s" }, h("span", null, r.name + " #" + r.number), h("span", { class: "mono" }, r.branch + "@" + r.sha),
          h("span", { class: "who" }, r.avatar ? avatar(r.actor, r.avatar, true) : null, r.panel_user ? "by " + r.panel_user : r.actor ? "by " + r.actor : ""))),
      h("div", { class: "r" }, h("span", null, ago(r.created_at)), h("span", { class: "mono" }, r.duration_text)));
  }
  function depRow(d) {
    var st = d.status;
    return h("div", { class: "runrow", onclick: function () { OPS.go("deploy/" + d.id); } }, chip(st),
      h("div", { style: { minWidth: 0 } }, h("div", { class: "t" }, (d.kind === "rollback" ? "Rollback → " : "") + (d.message || d.release_name || "Deployment")),
        h("div", { class: "s" }, h("span", { class: "mono" }, d.short_sha || d.release_name), d.target_id ? h("span", { class: "chip plain" }, d.target_name) : null, h("span", null, d.trigger + " · " + d.triggered_by))), h("div", { class: "r" }, h("span", null, ago(d.queued_at))));
  }
  OPS.runRow = runRow; OPS.depRow = depRow;

  /* ---------- Workflows ---------- */
  OPS.pages.workflows = function (page, seg, ctx) {
    var key = seg[0] === "panel" ? "panel" : "app";
    ctx.setTitle("Workflows");
    var tabs = h("div", { class: "tabs" }, [["app", S.info.repos.app], ["panel", S.info.repos.panel]].map(function (t) {
      return h("button", { class: key === t[0] ? "on" : "", onclick: function () { OPS.go("workflows/" + t[0]); } }, t[1]);
    }));
    var list = h("div", { class: "grid g2" }, skel(2));
    page.append(h("div", { class: "page-head" }, h("div", null, h("h2", null, "Workflows"), h("p", null, "Run GitHub Actions workflows with inputs. Every run is tied to who started it.")), h("span", { class: "sp" }), tabs), list);
    function load() {
      api("GET", "/api/workflows/" + key).then(function (d) {
        if (!d.workflows.length) return list.replaceChildren(empty("play", "No workflows found", "Add workflow files to .github/workflows in the repo."));
        list.replaceChildren.apply(list, d.workflows.map(function (w) { return wfCard(w, key, d.default_branch); }));
      }).catch(function (e) { list.replaceChildren(empty("alert", "Could not load workflows", e.message, h("a", { class: "btn sm", href: "#/settings/github" }, "Check GitHub token"))); });
    }
    load(); poll(load, 12000);
  };
  function wfCard(w, key, branch) {
    var lr = w.last_run;
    var run = h("button", { class: "btn primary", disabled: !(w.dispatch && w.state === "active" && can("operator")), title: !w.dispatch ? "This workflow has no workflow_dispatch trigger" : "", onclick: function () { runDialog(w, key, branch); } }, icon("play", "fill"), "Run workflow");
    return h("div", { class: "card wf" },
      h("div", { class: "top2" }, h("div", { style: { minWidth: 0, flex: 1 } }, h("h4", null, w.name), h("div", { class: "path" }, w.path)), w.state !== "active" ? chip("skipped", w.state) : null, !w.dispatch && w.state === "active" ? h("span", { class: "chip plain" }, "push / schedule only") : null),
      w.inputs.length ? h("div", { class: "inputs-preview" }, w.inputs.map(function (i) { return h("span", { class: "chip plain mono" }, i.key + (i.required ? "*" : "")); })) : null,
      lr ? h("div", { class: "runrow", style: { border: "1px solid var(--line)" }, onclick: function () { OPS.go("runs/" + key + "/" + lr.id); } }, chip(lr.status), h("div", { style: { minWidth: 0 } }, h("div", { class: "t" }, "#" + lr.number + " · " + (lr.panel_user ? "by " + lr.panel_user : lr.actor || "")), h("div", { class: "s" }, h("span", { class: "mono" }, lr.branch + "@" + lr.sha))), h("div", { class: "r" }, h("span", null, ago(lr.created_at)))) : h("div", { class: "sub" }, "Never run"),
      h("div", { class: "row" }, run, h("a", { class: "btn", href: "#/runs?w=" + w.id + "&k=" + key }, "History"), h("span", { class: "sp" }), h("a", { class: "btn ghost icon", href: w.url, target: "_blank", rel: "noopener", "aria-label": "Open on GitHub" }, icon("ext"))));
  }
  function runDialog(w, key, defBranch) {
    var refSel = h("select", { id: "ref" }, h("option", { value: defBranch }, defBranch));
    api("GET", "/api/github/branches/" + key).then(function (b) { refSel.replaceChildren.apply(refSel, b.branches.map(function (n) { return h("option", { value: n, selected: n === defBranch }, n); })); }).catch(function () { });
    var fields = {}, rows = w.inputs.map(function (i) {
      var el;
      if (i.type === "boolean") { el = h("input", { type: "checkbox", checked: String(i.default) === "true" }); return h("label", { class: "check" }, el, h("span", null, h("b", null, i.key), i.description ? h("div", { class: "sub" }, i.description) : null), (fields[i.key] = { el: el, bool: true }) && null); }
      if (i.type === "choice") el = h("select", null, i.options.map(function (o) { return h("option", { value: o, selected: String(i.default) === o }, o); }));
      else el = h("input", { type: i.type === "number" ? "number" : "text", value: i.default != null ? String(i.default) : "", placeholder: i.required ? "required" : "optional", required: i.required, autocomplete: "off" });
      fields[i.key] = { el: el };
      return h("div", { class: "field" }, h("label", null, i.key + (i.required ? " *" : "")), el, i.description ? h("div", { class: "hint" }, i.description) : null);
    });
    modal("Run " + w.name, h("div", { class: "col", style: { gap: "14px" } }, h("div", { class: "field" }, h("label", { for: "ref" }, "Branch / tag"), refSel), rows.length ? rows : h("div", { class: "sub" }, "This workflow takes no inputs.")),
      function (close) {
        var go = h("button", { class: "btn primary" }, icon("play", "fill"), "Run");
        go.onclick = function () {
          var inputs = {}; for (var k in fields) inputs[k] = fields[k].bool ? fields[k].el.checked : fields[k].el.value;
          busy(go, api("POST", "/api/workflows/" + key + "/" + w.id + "/dispatch", { ref: refSel.value, inputs: inputs }).then(function (r) {
            close(); toast("Workflow started", "ok"); OPS.go(r.run_id ? "runs/" + key + "/" + r.run_id : "runs/" + key);
          }).catch(fail));
        };
        return [h("button", { class: "btn", onclick: close }, "Cancel"), go];
      });
  }

  /* ---------- Runs ---------- */
  OPS.pages.runs = function (page, seg, ctx) {
    var q = new URLSearchParams((location.hash.split("?")[1] || ""));
    if (seg.length >= 2) return runPage(page, seg[0] === "panel" ? "panel" : "app", +seg[1], ctx);
    var key = seg[0] === "panel" ? "panel" : (q.get("k") === "panel" ? "panel" : "app"), wf = +(q.get("w") || 0), status = "", pg = 1;
    ctx.setTitle("Runs");
    var body = h("div", { class: "card" }, skel(5)), pager = h("div", { class: "row" });
    var st = h("select", { "aria-label": "Status filter", style: { width: "auto" }, onchange: function () { status = st.value; pg = 1; load(); } },
      [["", "All statuses"], ["in_progress", "Running"], ["queued", "Queued"], ["success", "Passed"], ["failure", "Failed"], ["cancelled", "Cancelled"]].map(function (o) { return h("option", { value: o[0] }, o[1]); }));
    var tabs = h("div", { class: "tabs" }, [["app", S.info.repos.app], ["panel", S.info.repos.panel]].map(function (t) { return h("button", { class: key === t[0] ? "on" : "", onclick: function () { OPS.go("runs/" + t[0]); } }, t[1]); }));
    page.append(h("div", { class: "page-head" }, h("div", null, h("h2", null, "Runs"), h("p", null, wf ? "Filtered to one workflow." : "Every workflow run, newest first.")), h("span", { class: "sp" }), st, tabs), body, pager);
    function load() {
      api("GET", "/api/runs/" + key + "?workflow=" + wf + "&status=" + status + "&page=" + pg).then(function (d) {
        body.replaceChildren(d.runs.length ? d.runs.map(function (r) { return runRow(r, key); }) : empty("runs", "No runs", status ? "Nothing matches this filter." : "Run a workflow to see it here."));
        pager.replaceChildren(h("button", { class: "btn sm", disabled: pg <= 1, onclick: function () { pg--; load(); } }, "← Newer"), h("span", { class: "sub" }, "Page " + pg + " · " + d.total + " runs"), h("button", { class: "btn sm", disabled: pg * 25 >= d.total, onclick: function () { pg++; load(); } }, "Older →"));
        var n = d.runs.filter(function (r) { return r.status === "running" || r.status === "queued"; }).length; var b = document.getElementById("runbadge"); if (b && pg === 1 && !status) { b.hidden = !n; b.textContent = n; }
      }).catch(function (e) { body.replaceChildren(empty("alert", "Could not load runs", e.message)); });
    }
    load(); poll(load, 7000);
  };

  function runPage(page, key, id, ctx) {
    ctx.setTitle(["Runs", "#" + id]);
    var head = h("div", { class: "card" }, skel(1)), graphBox = h("div", { class: "roots" });
    var detail = h("div", { class: "card detail" }, h("div", { class: "sub" }, "Select a node in the graph to see details and logs."));
    var arts = h("div", { class: "card", hidden: true });
    var fsTitle = h("div", { class: "fs-title" }), fsState = h("span"), fsPanelBtn, fsBtn, fsOn = false, holder = null;
    fsPanelBtn = h("button", { class: "btn sm", type: "button", title: "Show or hide the details panel (D)", onclick: function () { wrap.classList.toggle("nodetail"); setTimeout(function () { graph.fit(); }, 60); } }, icon("panel"), "Details");
    var fsBar = h("div", { class: "fsbar" }, fsState, fsTitle, h("span", { class: "sp" }), fsPanelBtn, h("button", { class: "btn sm primary", type: "button", title: "Exit full screen (Esc)", onclick: function () { setFS(false); } }, icon("shrink"), "Exit"));
    fsBtn = h("button", { class: "fs-btn btn sm", type: "button", title: "Full screen (F)", "aria-label": "Full screen", onclick: function () { setFS(true); } }, icon("expand"), "Full screen");
    var wrap = h("div", { class: "graph-wrap" }, fsBar, graphBox, fsBtn, detail);
    function setFS(on) {
      if (on === fsOn) return; fsOn = on;
      wrap.classList.toggle("fs", on); document.body.classList.toggle("fs-open", on);
      if (on) { holder = document.createComment("graph"); wrap.parentNode.insertBefore(holder, wrap); document.body.appendChild(wrap); }   // out of .page so nothing can clip or cover it
      else if (holder && holder.parentNode) { holder.parentNode.insertBefore(wrap, holder); holder.remove(); holder = null; }
      if (on) { wrap.classList.remove("nodetail"); try { if (document.documentElement.requestFullscreen && !document.fullscreenElement) document.documentElement.requestFullscreen().catch(function () { }); } catch (e) { } }
      else { try { if (document.fullscreenElement && document.exitFullscreen) document.exitFullscreen().catch(function () { }); } catch (e) { } }
      setTimeout(function () { graph.fit(); }, 80);
    }
    function onKey(e) {
      if (/^(input|textarea|select)$/i.test((e.target.tagName || "")) || e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.key === "f" || e.key === "F") { e.preventDefault(); setFS(!fsOn); }
      else if (e.key === "Escape" && fsOn) setFS(false);
      else if ((e.key === "d" || e.key === "D") && fsOn) fsPanelBtn.click();
    }
    function onFsChange() { if (!document.fullscreenElement && fsOn) setFS(false); }
    function teardown() { if (fsOn) setFS(false); document.body.classList.remove("fs-open"); document.removeEventListener("keydown", onKey); document.removeEventListener("fullscreenchange", onFsChange); window.removeEventListener("hashchange", teardown); }
    document.addEventListener("keydown", onKey); document.addEventListener("fullscreenchange", onFsChange); window.addEventListener("hashchange", teardown);
    page.append(h("a", { class: "btn ghost sm", href: "#/runs/" + key, style: { alignSelf: "flex-start" } }, icon("back"), "All runs"), head, wrap, arts);
    var graph = S.graph = new Roots(graphBox, { onSelect: function (raw) { showNode(raw); } }), data = null, sel = null, logTimer = 0, logFor = null;

    function actions(r) {
      var live = r.status === "running" || r.status === "queued", out = [];
      if (can("operator")) {
        if (live) out.push(h("button", { class: "btn danger", onclick: function (e) { confirmBox("Cancel this run?", "GitHub will stop the workflow run.", "Cancel run", true).then(function (y) { if (y) busy(e.target.closest("button"), api("POST", "/api/runs/" + key + "/" + id + "/cancel").then(function () { toast("Cancel requested", "ok"); load(); }).catch(fail)); }); } }, icon("stop", "fill"), "Cancel"));
        else {
          out.push(h("button", { class: "btn", onclick: function (e) { busy(e.target.closest("button"), api("POST", "/api/runs/" + key + "/" + id + "/rerun").then(function () { toast("Re-running", "ok"); load(); }).catch(fail)); } }, icon("refresh"), "Re-run"));
          if (r.status === "failed") out.push(h("button", { class: "btn", onclick: function (e) { busy(e.target.closest("button"), api("POST", "/api/runs/" + key + "/" + id + "/rerun-failed").then(function () { toast("Re-running failed jobs", "ok"); load(); }).catch(fail)); } }, icon("refresh"), "Re-run failed"));
        }
      }
      out.push(h("a", { class: "btn ghost icon", href: r.url, target: "_blank", rel: "noopener", "aria-label": "Open on GitHub" }, icon("ext")));
      return out;
    }
    function paintHead(d) {
      var r = d.run, by = d.tracked && d.tracked.by;
      fsState.replaceChildren(chip(r.status)); fsTitle.replaceChildren(h("b", null, r.title || r.name), h("span", { class: "sub mono" }, r.name + " #" + r.number + " · " + r.branch + "@" + r.sha + (r.duration_text ? " · " + r.duration_text : "")));
      head.replaceChildren(h("div", { class: "row wrap", style: { gap: "14px" } }, chip(r.status), h("div", { style: { minWidth: 0, flex: 1 } }, h("div", { class: "big", style: { fontSize: "19px" } }, r.title || r.name),
        h("div", { class: "sub row wrap", style: { gap: "12px" } }, h("span", null, r.name + " #" + r.number + (r.attempt > 1 ? " (attempt " + r.attempt + ")" : "")), h("span", { class: "mono" }, r.branch + "@" + r.sha),
          h("span", { class: "who" }, avatar(r.actor, r.avatar, true), "GitHub: " + (r.actor || "?")), by ? h("span", { class: "who" }, icon("shield"), "Started in panel by " + by) : null, h("span", null, "Started " + ago(r.started_at || r.created_at)), r.duration_text ? h("span", { class: "mono" }, r.duration_text) : null)), h("div", { class: "row" }, actions(r))),
        d.tracked && d.tracked.inputs ? h("div", { class: "row wrap", style: { marginTop: "10px", gap: "6px" } }, h("span", { class: "lbl" }, "Inputs"), h("code", { class: "inline" }, d.tracked.inputs)) : null,
        d.definition_error ? h("div", { class: "chip warn", style: { marginTop: "10px" } }, "Workflow file could not be parsed, showing jobs from GitHub only") : null);
    }
    function paintArts(d) {
      var list = d.artifacts || []; arts.hidden = !list.length; if (!list.length) return;
      arts.replaceChildren(h("h3", null, icon("box"), "Artifacts"), h("div", { class: "files" }, list.map(function (a) {
        return h("div", { class: "file" }, icon("file"), h("span", { class: "n" }, a.name), h("span", { class: "sub" }, bytes(a.size)), h("span", { class: "sp" }), a.expired ? chip("skipped", "expired") : [
          can("operator") ? h("a", { class: "btn sm", href: BASE + "/api/runs/" + key + "/" + id + "/artifact/" + a.id + "/download" }, icon("download"), "Download") : null,
          can("operator") ? h("button", { class: "btn sm primary", onclick: function () { importArtifact(a); } }, icon("up"), "Publish") : null]);
      })));
    }
    function importArtifact(a) {
      var ver = h("input", { type: "text", placeholder: "1.4.2", required: true }), plat = h("select", null, ["android", "ios", "other"].map(function (p) { return h("option", { value: p, selected: /ios|ipa/i.test(a.name) ? p === "ios" : p === "android" }, p); }));
      var ch = h("select", null, h("option", { value: "stable" }, "stable"), h("option", { value: "beta" }, "beta")), appSel = h("select"), notes = h("textarea", { placeholder: "What's new in this version?", rows: 4 });
      api("GET", "/api/library").then(function (l) { appSel.replaceChildren.apply(appSel, l.apps.map(function (x) { return h("option", { value: x.slug }, x.name); })); });
      modal("Publish “" + a.name + "” to the public page", h("div", { class: "col" }, h("div", { class: "sub" }, "Copies the build files out of this artifact into the download library at /applications."),
        h("div", { class: "grid g2" }, h("div", { class: "field" }, h("label", null, "App"), appSel), h("div", { class: "field" }, h("label", null, "Version *"), ver)),
        h("div", { class: "grid g2" }, h("div", { class: "field" }, h("label", null, "Platform"), plat), h("div", { class: "field" }, h("label", null, "Channel"), ch)), h("div", { class: "field" }, h("label", null, "Release notes"), notes)),
        function (close) {
          var go = h("button", { class: "btn primary" }, "Publish");
          go.onclick = function () { busy(go, api("POST", "/api/library/import-artifact", { repo: key, artifact_id: a.id, app: appSel.value, version: ver.value, platform: plat.value, channel: ch.value, notes: notes.value }).then(function () { close(); toast("Importing… check Releases", "ok"); OPS.go("releases"); }).catch(fail)); };
          return [h("button", { class: "btn", onclick: close }, "Cancel"), go];
        });
    }
    function showNode(raw) {
      sel = raw; clearInterval(logTimer); if (!raw) return; if (fsOn && raw !== data.tree) wrap.classList.remove("nodetail");
      var ref = raw.ref || {}, jobId = ref.job_id || (ref.job_ids && ref.job_ids.length === 1 ? ref.job_ids[0] : null);
      var logEl = h("div", { class: "log", hidden: !jobId }, "Loading log…");
      detail.replaceChildren(h("div", { class: "row" }, chip(raw.status), h("b", { style: { fontSize: "15px" } }, raw.name), h("span", { class: "sp" })),
        h("dl", { class: "kv" }, h("dt", null, "Type"), h("dd", null, raw.type === "main" ? "Run" : raw.type === "workflow" ? "Job" : "Step"), raw.ref && raw.ref.display && raw.ref.display !== raw.name ? [h("dt", null, "Display name"), h("dd", null, raw.ref.display)] : null, h("dt", null, "Status"), h("dd", null, raw.status + (raw.meta ? " · " + raw.meta : ""))),
        ref.kind === "step" ? h("div", { class: "sub" }, "Showing the log of the whole job this step belongs to.") : null,
        !jobId && ref.kind === "job" && (ref.job_ids || []).length > 1 ? h("div", { class: "sub" }, "This job runs as a matrix. Expand it and pick one leg to read its log.") : null,
        !jobId && ref.kind === "job" && !(ref.job_ids || []).length ? h("div", { class: "sub" }, "Waiting for an earlier job to finish.") : null,
        logEl);
      if (!jobId) return; logFor = jobId;
      function fetchLog() {
        if (logFor !== jobId) return;
        fetch(BASE + "/api/runs/" + key + "/jobs/" + jobId + "/log", { credentials: "same-origin" }).then(function (r) { return r.text(); }).then(function (t) {
          var stick = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 30; logEl.replaceChildren();
          t.split("\n").forEach(function (l) {
            var m = /^(\d{4}-\d\d-\d\dT[\d:.]+Z)\s?(.*)$/.exec(l), line = m ? m[2] : l, cls = /##\[error\]|error:|Error:|FAILED/.test(line) ? "er" : /##\[group\]/.test(line) ? "gp" : "";
            logEl.appendChild(h("div", null, m ? h("span", { class: "ts" }, m[1].slice(11, 19) + " ") : null, h("span", { class: cls }, line.replace("##[group]", "▸ ").replace("##[endgroup]", "").replace("##[error]", ""))));
          });
          if (stick) logEl.scrollTop = logEl.scrollHeight;
        }).catch(function () { logEl.textContent = "Could not load log."; });
      }
      fetchLog(); var live = raw.status === "running" || raw.status === "queued"; if (live) logTimer = setInterval(fetchLog, 5000); S.timers.push(logTimer);
    }
    function load() {
      api("GET", "/api/runs/" + key + "/" + id).then(function (d) {
        data = d; paintHead(d); paintArts(d); graph.setData(d.tree);
        if (!sel) { graph.select("run", false); showNode(d.tree); sel = d.tree; }
      }).catch(function (e) { head.replaceChildren(empty("alert", "Could not load this run", e.message)); });
    }
    load(); poll(function () { if (!data || data.run.status === "running" || data.run.status === "queued") load(); }, 3000);
  }
})();
