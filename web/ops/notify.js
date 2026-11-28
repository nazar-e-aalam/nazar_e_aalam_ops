/* Notification bell (deployment / server-setup alerts), Settings > Notifications (+ email template previews) and Settings > Database. */
(function () {
  "use strict";
  var O = window.OPS, h = O.h, icon = O.icon, api = O.api, toast = O.toast, fail = O.fail, busy = O.busy, F = O.fmt;
  O.settingsTabs = O.settingsTabs || {};

  /* ---------- bell ---------- */
  var LV = { error: "failed", success: "success", warning: "queued", info: "info" };
  function mountBell() {
    var top = document.querySelector(".top"), slot = document.getElementById("topslot");
    if (!top || !slot || document.getElementById("bell")) return;
    var dot = h("span", { class: "badge", hidden: true }), pop = null;
    var btn = h("button", { id: "bell", class: "btn ghost icon", type: "button", "aria-label": "Notifications", style: { position: "relative" }, onclick: toggle }, icon("bell"), dot);
    top.insertBefore(btn, slot.nextSibling);
    function count(n) { dot.hidden = !n; dot.textContent = n > 9 ? "9+" : String(n); }
    function refresh() { return api("GET", "/api/notifications").then(function (d) { count(d.unread); return d; }).catch(function () {}); }
    function close() { if (pop) { pop.remove(); pop = null; } }
    function toggle() {
      if (pop) return close();
      var list = h("div", { class: "col", style: { gap: "0", maxHeight: "60vh", overflow: "auto" } }, O.skel(2));
      pop = h("div", { class: "card", role: "dialog", "aria-label": "Notifications", style: { position: "fixed", top: "60px", right: "16px", width: "min(380px,calc(100vw - 32px))", zIndex: 120, padding: "8px", boxShadow: "var(--shadow)" } },
        h("div", { class: "row", style: { padding: "6px 8px 8px" } }, h("b", null, "Notifications"), h("span", { class: "sp" }), h("a", { class: "btn ghost sm", href: "#/settings/notifications", onclick: close }, "Settings")), list);
      document.body.appendChild(pop);
      refresh().then(function (d) {
        if (!d) return;
        list.replaceChildren.apply(list, d.items.length ? d.items.map(function (n) {
          return h("a", { href: n.link || "#/", onclick: close, style: { display: "block", padding: "10px 8px", borderRadius: "10px", background: n.unread ? "var(--card-hi)" : "transparent" } },
            h("div", { class: "row wrap", style: { gap: "8px" } }, O.chip(LV[n.level] || "info", n.level), h("span", { class: "sub" }, F.ago(n.ts))),
            h("div", { style: { fontWeight: 700, marginTop: "4px" } }, n.title), n.body ? h("div", { class: "sub", style: { wordBreak: "break-word" } }, n.body.slice(0, 160)) : null);
        }) : [O.empty("bell", "Nothing yet", "Failed deployments and server setups show up here.")]);
        if (d.unread) api("POST", "/api/notifications/seen").then(function () { count(0); });
      });
      setTimeout(function () { document.addEventListener("click", away, true); }, 0);
    }
    function away(e) { if (pop && !pop.contains(e.target) && !btn.contains(e.target)) { close(); document.removeEventListener("click", away, true); } }
    refresh(); O.poll(refresh, 30000);
  }
  var shell = O.shell;
  O.shell = function () { shell.apply(this, arguments); mountBell(); };

  /* ---------- Settings > Notifications ---------- */
  O.settingsTabs.notifications = function (body) {
    var box = h("div", { class: "col" }, O.skel(3));
    body.append(box);
    api("GET", "/api/notifications/config").then(function (c) {
      var email = h("input", { type: "checkbox" }), succ = h("input", { type: "checkbox" }), extra = h("input", { type: "text", value: c.extra_emails || "", placeholder: "ops@example.com, oncall@example.com" });
      email.checked = !!c.email; succ.checked = !!c.on_success;
      var prev = h("iframe", { sandbox: "", title: "Email preview", style: { width: "100%", height: "620px", border: "1px solid var(--line)", borderRadius: "12px", background: "#fff" } });
      var kinds = [["deploy_failed", "Deployment failed"], ["setup_ok", "Server ready"], ["invite", "Invitation"], ["reset", "Password reset"], ["test", "Test email"]];
      function show(k) { prev.src = O.base + "/api/email/preview/" + k + "?t=" + Date.now(); }
      var tabs = h("div", { class: "ptabs" }, kinds.map(function (k, i) { var b = h("button", { type: "button", class: i ? "" : "on", onclick: function () { tabs.querySelectorAll("button").forEach(function (x) { x.classList.remove("on"); }); b.classList.add("on"); show(k[0]); } }, k[1]); return b; }));
      box.replaceChildren(
        h("div", { class: "card col" }, h("h3", null, icon("bell"), "When something fails"),
          h("p", { class: "sub" }, "A failed deployment, failed tests or a failed server setup always appears in the bell. Email is sent to every admin" + (c.recipients.length ? " (" + c.recipients.join(", ") + ")" : "") + " plus the addresses below."),
          c.mail_ready ? null : h("div", { class: "wz-note warn" }, h("b", null, "Email is not set up"), h("div", null, "Alerts stay in the bell until you set up email in ", h("a", { href: "#/settings/email" }, "Settings > Email"), ".")),
          h("label", { class: "wz-check" }, email, h("span", null, h("b", null, "Send alerts by email"))),
          h("label", { class: "wz-check" }, succ, h("span", null, h("b", null, "Also tell me when a deployment succeeds"))),
          h("div", { class: "field" }, h("label", null, "Extra recipients"), extra, h("div", { class: "hint" }, "Comma separated. Optional.")),
          h("div", { class: "row" }, h("button", { class: "btn primary", type: "button", onclick: function (e) {
            busy(e.target.closest("button"), api("PUT", "/api/notifications/config", { email: email.checked, on_success: succ.checked, extra_emails: extra.value }).then(function () { toast("Saved", "ok"); }).catch(fail));
          } }, "Save"))),
        h("div", { class: "card col" }, h("h3", null, icon("mail"), "Email templates"), h("p", { class: "sub" }, "This is what people receive. They adapt to phones and dark mode."), tabs, prev));
      show("deploy_failed");
    }).catch(function (e) { box.replaceChildren(O.empty("alert", "Admins only", e.message)); });
  };

  /* ---------- Settings > Database ---------- */
  O.settingsTabs.database = function (body) {
    var box = h("div", { class: "card col" }, O.skel(3));
    body.append(box);
    function row(k, v) { return h("div", { class: "row wrap", style: { gap: "10px" } }, h("span", { class: "sub", style: { width: "130px" } }, k), h("code", { class: "inline", style: { wordBreak: "break-all" } }, v)); }
    function load() {
      api("GET", "/api/system/database").then(function (d) {
        box.replaceChildren(h("h3", null, icon("box"), "Panel database"),
          h("div", { class: "wz-note " + (d.ok ? "info" : "warn") }, h("b", null, d.ok ? "Connected" : "Not reachable"), h("div", null, d.ok ? d.ms + " ms" : d.error)),
          row("Engine", d.engine + (d.version ? " " + d.version : "")), row("Address", d.url), row("Configured by", d.source), d.pool ? row("Pool", d.pool.size + " connections, recycled every " + d.pool.recycle + " s") : null,
          h("p", { class: "sub" }, "The panel stores users, settings, deployments and releases here. Connection details come from the OPS_DB_* lines in the panel's .env (scripts/setup.sh writes them for you). The database, tables and columns are created automatically."),
          h("div", { class: "row" }, h("button", { class: "btn", type: "button", onclick: function (e) { busy(e.target.closest("button"), Promise.resolve(load())); } }, icon("refresh"), "Check again")));
      }).catch(function (e) { box.replaceChildren(O.empty("alert", "Admins only", e.message)); });
    }
    load();
  };
})();
