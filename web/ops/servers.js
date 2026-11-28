/* Servers: deploy to this server or to other servers over SSH keys, with a guided setup (connection, key, access check, automatic server setup). */
(function () {
  "use strict";
  var O = window.OPS, h = O.h, icon = O.icon, api = O.api, toast = O.toast, fail = O.fail, busy = O.busy, can = O.can, chip = O.chip, F = O.fmt;
  var STEP = {};                                                    // remembered wizard step per server

  function code(text, label) { return h("div", { class: "wz-code" }, h("code", null, text), h("button", { class: "btn ghost sm", type: "button", onclick: function () { O.copy(text, label || "Copied"); } }, icon("copy"), "Copy")); }
  function note(kind, title, body) { return h("div", { class: "wz-note " + kind }, title ? h("b", null, title) : null, h("div", null, body)); }
  function field(label, input, hint) { return h("div", { class: "field" }, h("label", null, label), input, hint ? h("div", { class: "hint" }, hint) : null); }
  function setupLabel(t) {
    var b = t.bootstrap;
    if (!b) return "Not set up yet" + (t.verified ? "" : " (runs after sudo access is granted)");
    return "Set up " + F.ago(b.at) + (b.python ? " · Python " + String(b.version || b.python).replace(/^Python /, "") : "") + (b.tested ? " · tests passed" : "");
  }
  function checkRow(c) {
    var cls = c.ok === true ? "ok" : c.ok === false ? "bad" : "warn";
    return h("div", { class: "check-row " + cls }, h("span", { class: "ic" }, icon(c.ok === true ? "check" : "alert")),
      h("div", { style: { minWidth: 0 } }, h("b", null, c.label), c.detail ? h("div", { class: "sub", style: { wordBreak: "break-word" } }, c.detail) : null, c.fix && c.ok !== true ? h("div", { class: "wz-fix" }, c.fix) : null));
  }

  /* ------------------------------------------------ list ------------------------------------------------ */
  O.pages.servers = function (page, seg, ctx) {
    if (seg[0]) return guide(page, seg[0], ctx);
    ctx.setTitle("Servers");
    var list = h("div", { class: "col" }, O.skel(3));
    page.append(h("div", { class: "page-head" }, h("div", null, h("h2", null, "Servers"), h("p", null, "Choose where deployments go: this server, or other servers reached over SSH with a key. Every server must pass the access check before anything is deployed to it.")),
      can("admin") ? h("a", { class: "btn primary", href: "#/servers/new" }, icon("plus"), "Add server") : null), list);
    function load() {
      api("GET", "/api/targets").then(function (d) {
        var kids = [];
        if (!d.ssh_ready) kids.push(note("warn", "SSH client missing on the ops server", "Install it to add remote servers: sudo apt install openssh-client"));
        d.targets.forEach(function (t) { kids.push(card(t, d)); });
        list.replaceChildren.apply(list, kids);
      }).catch(function (e) { list.replaceChildren(O.empty("alert", "Could not load servers", e.message)); });
    }
    function card(t, d) {
      var ck = t.checks, n = ck && ck.checks ? ck.checks.length : 0, good = ck && ck.checks ? ck.checks.filter(function (c) { return c.ok === true; }).length : 0;
      var status = t.verified ? chip("success", "Ready") : ck && !ck.ok ? chip("failed", "Needs attention") : chip("queued", "Not checked");
      return h("div", { class: "card srv" },
        h("div", { class: "row wrap", style: { gap: "10px" } }, h("span", { class: "srv-ic" }, icon("server")), h("div", { style: { flex: 1, minWidth: 0 } }, h("h3", { style: { margin: 0 } }, t.name),
          h("div", { class: "sub mono" }, t.kind === "local" ? "this machine (where the ops panel runs)" : t.user + "@" + t.host + ":" + t.port + (t.root ? "  ·  " + t.root : ""))), status,
          d.auto_target === t.id ? chip("info", "Auto deploy target") : null),
        h("div", { class: "row wrap sub", style: { gap: "14px", margin: "10px 0" } }, h("span", null, icon("box"), " ", setupLabel(t)),
          ck ? h("span", null, good + "/" + n + " checks passed · " + F.ago(ck.at)) : h("span", null, "never checked"), t.fingerprint ? h("span", { class: "mono", title: "Host key fingerprint" }, t.fingerprint.slice(0, 22) + "…") : null),
        h("div", { class: "row wrap" }, h("a", { class: "btn primary", href: "#/servers/" + t.id }, icon("gear"), t.verified ? "Open" : "Set up guide"),
          can("operator") ? h("button", { class: "btn", type: "button", onclick: function (e) { busy(e.target.closest("button"), api("POST", "/api/targets/" + t.id + "/check").then(function (r) { toast(r.result.ok ? "All checks passed" : "Some checks need attention", r.result.ok ? "ok" : "err"); load(); }).catch(fail)); } }, icon("refresh"), "Check access") : null,
          can("admin") && t.verified && d.auto_target !== t.id ? h("button", { class: "btn ghost", type: "button", onclick: function () { api("PUT", "/api/targets/auto/" + t.id).then(function () { toast("Auto deploy and webhooks now go to " + t.name, "ok"); load(); }).catch(fail); } }, "Use for auto deploy") : null,
          h("span", { class: "sp" }),
          can("admin") && t.kind === "ssh" ? h("button", { class: "btn ghost danger", type: "button", onclick: function () { O.confirm("Remove " + t.name + "?", "The panel forgets this server and its SSH key. Nothing is deleted on the server itself.", "Remove", true).then(function (y) { if (y) api("DELETE", "/api/targets/" + t.id).then(function () { toast("Removed", "ok"); load(); }).catch(fail); }); } }, "Remove") : null));
    }
    load();
  };

  /* ------------------------------------------------ guide ----------------------------------------------- */
  function guide(page, key, ctx) {
    var isNew = key === "new", tid = isNew ? null : +key, T = null, defaults = {}, step = 0, body = h("div", { class: "card wz-card" }, O.skel(4)), bar = h("ol", { class: "wz-steps" });
    ctx.setTitle(["Servers", isNew ? "Add server" : "Server"]);
    page.append(h("a", { class: "btn ghost sm", href: "#/servers", style: { alignSelf: "flex-start" } }, icon("back"), "All servers"),
      h("div", { class: "wz-hero" }, h("span", { class: "srv-ic big" }, icon("server")), h("div", null, h("h2", { id: "srvTitle" }, isNew ? "Add a server" : "Server guide"), h("p", { class: "sub" }, "A short guide: it explains every value. Nothing is changed on the server until you press a button."))), bar, body);
    var local = false;
    function names() { return local ? ["Access check", "Server setup", "Done"] : ["Connection", "SSH key", "Access check", "Server setup", "Done"]; }
    function paint() {
      bar.replaceChildren.apply(bar, names().map(function (n, i) { return h("li", { class: i === step ? "on" : i < step ? "done" : "", onclick: function () { if (T || i === 0) go(i); } }, h("span", { class: "n" }, i < step ? icon("check") : String(i + 1)), h("span", { class: "l" }, n)); }));
    }
    function go(i) { step = i; if (T) STEP[T.id] = i; paint(); render(); window.scrollTo(0, 0); }
    function nav(next, label, fn, skip) {
      return h("div", { class: "wz-nav" }, step > 0 ? h("button", { class: "btn ghost", type: "button", onclick: function () { go(step - 1); } }, icon("back"), "Back") : null, h("span", { class: "sp" }),
        skip ? h("button", { class: "btn ghost", type: "button", onclick: function () { go(step + 1); } }, skip) : null,
        next === false ? null : h("button", { class: "btn primary", type: "button", onclick: function (e) { fn ? fn(e) : go(step + 1); } }, label || "Continue"));
    }
    function reload(then) { return api("GET", "/api/targets/" + T.id).then(function (t) { T = t; then && then(); render(); paint(); }); }
    function render() { var s = names()[step]; ({ "Connection": sConn, "SSH key": sKey, "Access check": sCheck, "Server setup": sSetup, "Done": sDone })[s](); }

    /* step: connection */
    function sConn() {
      var t = T || { name: "", host: "", port: 22, user: "", root: "", restart_cmd: "", health_url: "" };
      var name = h("input", { type: "text", value: t.name, placeholder: "Production VPS" }), host = h("input", { type: "text", value: t.host, placeholder: "203.0.113.10 or app.example.com", spellcheck: false }),
        port = h("input", { type: "number", value: t.port || 22 }), user = h("input", { type: "text", value: t.user, placeholder: "deploy", spellcheck: false }),
        root = h("input", { type: "text", value: t.root, placeholder: defaults.root, spellcheck: false }), rc = h("input", { type: "text", value: t.restart_cmd, placeholder: defaults.restart_cmd, spellcheck: false }),
        hu = h("input", { type: "text", value: t.health_url, placeholder: defaults.health_url, spellcheck: false });
      body.replaceChildren(h("h3", null, "1. Where is the server?"),
        h("p", { class: "sub" }, "Give the address of the other server and the user the panel should log in as. The panel uses an SSH key, never a password."),
        h("div", { class: "grid g2" }, field("Name", name, "Only for you, shown in lists."), field("Server address (host or IP)", host, "Find it in your hosting dashboard (VPS / droplet / EC2 page).")),
        h("div", { class: "grid g2" }, field("SSH user", user, "A normal user such as deploy or ubuntu. It does not need to be root: only the restart uses sudo."), field("SSH port", port, "22 unless you changed it.")),
        note("info", "Optional: values that differ on this server", "Leave empty to use the defaults from Settings → Deploy (shown in grey)."),
        h("div", { class: "grid g2" }, field("Deploy folder", root, "Releases are stored here: releases/, shared/ and the current link."), field("Health check URL", hu, "Checked from that server after the restart, so 127.0.0.1 is fine.")),
        field("Restart command", rc, "Runs on that server after the new release is live, e.g. sudo systemctl restart nazar-panel"),
        nav(true, T ? "Save & continue" : "Create & continue", function (e) {
          var b = { name: name.value, host: host.value, port: +port.value || 22, user: user.value, root: root.value, restart_cmd: rc.value, health_url: hu.value };
          busy(e.target.closest("button"), (T ? api("PUT", "/api/targets/" + T.id, b) : api("POST", "/api/targets", b).then(function (r) { return api("GET", "/api/targets/" + r.id); })).then(function (t) {
            T = t; STEP[T.id] = 1; if (isNew) { isNew = false; history.replaceState(null, "", O.base + "/#/servers/" + T.id); } toast("Saved", "ok"); go(1);
          }).catch(fail));
        }));
    }

    /* step: key */
    function sKey() {
      var paste = h("textarea", { rows: 6, placeholder: "-----BEGIN OPENSSH PRIVATE KEY-----\n…", spellcheck: false, style: { fontFamily: "var(--mono)", fontSize: "12px" } }), usePaste = T.key_mode === "imported", area = h("div", { class: "col" });
      function show() {
        area.replaceChildren(usePaste ? h("div", { class: "col" }, h("p", { class: "sub" }, "Paste a private key that already works on that server. It must not have a passphrase. It is stored on the ops server with permission 600 and is never shown again."),
          paste, h("div", { class: "row" }, h("button", { class: "btn", type: "button", onclick: function (e) { busy(e.target.closest("button"), api("POST", "/api/targets/" + T.id + "/key", { mode: "paste", private_key: paste.value }).then(function (t) { T = t; paste.value = ""; toast("Key saved", "ok"); show(); }).catch(fail)); } }, "Save key"), T.key_mode === "imported" ? chip("success", "Key stored") : null))
          : h("div", { class: "col" }, h("p", { class: "sub" }, "The panel created a private key for this server (it stays on the ops server). You only have to give the server the matching public key once:"),
            h("ol", { class: "wz-how" }, h("li", null, "Log in to the server once. Use your provider's web console, or ", h("code", { class: "inline" }, "ssh " + (T.user || "user") + "@" + T.host), "."),
              h("li", null, "Paste the command below and press Enter. It adds the public key to ", h("code", { class: "inline" }, "~/.ssh/authorized_keys"), " of ", h("b", null, T.user), "."),
              h("li", null, "Come back and press Continue, then run the access check.")),
            h("div", { class: "lbl" }, "Command to run on the server"), code(T.authorize_cmd, "Command copied"),
            h("details", { class: "wz-file" }, h("summary", null, "Show only the public key"), code(T.public_key, "Public key copied")),
            can("admin") ? h("button", { class: "btn ghost sm", style: { alignSelf: "flex-start" }, type: "button", onclick: function () { O.confirm("Create a new key?", "The old key stops working. You must add the new public key to the server again.", "New key").then(function (y) { if (y) api("POST", "/api/targets/" + T.id + "/key", { mode: "generate" }).then(function (t) { T = t; usePaste = false; toast("New key created", "ok"); show(); }).catch(fail); }); } }, icon("refresh"), "Create a new key") : null));
      }
      var tabs = h("div", { class: "ptabs" }, [["Let the panel create a key (recommended)", false], ["I have my own private key", true]].map(function (o) { var b = h("button", { type: "button", class: usePaste === o[1] ? "on" : "", onclick: function () { usePaste = o[1]; tabs.querySelectorAll("button").forEach(function (x) { x.classList.remove("on"); }); b.classList.add("on"); show(); } }, o[0]); return b; }));
      body.replaceChildren(h("h3", null, "2. SSH key"), h("p", { class: "sub" }, "An SSH key is a lock-and-key pair: the server keeps the public half, the panel holds the private half. It is how the panel logs in without a password."), tabs, area, nav(true, "Continue"));
      show();
    }

    /* step: access check */
    function sCheck() {
      var out = h("div", { class: "col" }), res = T.checks;
      function paint2(r) {
        res = r; var kids = [];
        if (!r) { out.replaceChildren(note("info", "Not checked yet", "Press Run check. It logs in, checks Python, write access to the deploy folder, and whether the restart command may use sudo without a password.")); return; }
        r.checks.forEach(function (c) { kids.push(checkRow(c)); });
        var sudoBad = r.checks.filter(function (c) { return c.grant; })[0];
        if (sudoBad) {
          var pw = h("input", { type: "password", placeholder: "sudo password of " + (T.user || "that user"), autocomplete: "off" });
          kids.push(h("div", { class: "wz-opt" }, h("h4", null, "Grant access to restart the service"), h("p", { class: "sub" }, "Type the user's sudo password once. The panel installs a rule that lets this one command run without a password and then forgets the password (it is not stored or logged)."),
            h("div", { class: "row wrap" }, pw, h("button", { class: "btn primary", type: "button", onclick: function (e) { busy(e.target.closest("button"), api("POST", "/api/targets/" + T.id + "/grant-sudo", { password: pw.value }).then(function (x) { pw.value = ""; T = x.target; toast("Access granted", "ok"); paint2(x.result); if (x.bootstrap_id) { toast("Setting up the server now…", "ok"); O.go("deploy/" + x.bootstrap_id); } }).catch(fail)); } }, icon("key"), "Grant access")),
            h("details", { class: "wz-file" }, h("summary", null, "Prefer to do it yourself? Run this on the server"), code("echo '" + r.sudoers + "' | sudo tee /etc/sudoers.d/zz-nazar-ops-" + (T.user || "deploy") + " && sudo chmod 440 /etc/sudoers.d/zz-nazar-ops-" + (T.user || "deploy") + " && sudo visudo -c", "Command copied"))));
        }
        kids.unshift(r.ok ? note("info", "Ready for deployments", "Everything required passed " + F.ago(r.at) + ".") : note("warn", "Not ready yet", "Fix the red items, then run the check again."));
        if (T.fingerprint) kids.push(h("div", { class: "sub row wrap" }, "Server fingerprint: ", h("code", { class: "inline" }, T.fingerprint), can("admin") ? h("button", { class: "btn ghost sm", type: "button", onclick: function () { O.confirm("Forget the host key?", "Do this only if the server was reinstalled. The next check trusts whatever key the server presents.", "Forget").then(function (y) { if (y) api("POST", "/api/targets/" + T.id + "/forget-host").then(function (t) { T = t; toast("Host key forgotten", "ok"); render(); }).catch(fail); }); } }, "Forget host key") : null));
        out.replaceChildren.apply(out, kids);
      }
      body.replaceChildren(h("h3", null, "3. Access check"), h("p", { class: "sub" }, local ? "Checks that this server can deploy: Python, write access to the deploy folder and sudo for the restart command." : "Logs in with the key and checks everything a deployment needs. The first successful login saves the server's identity (host key) so a later impostor is refused."),
        h("div", { class: "row" }, can("operator") ? h("button", { class: "btn primary", type: "button", onclick: function (e) { busy(e.target.closest("button"), api("POST", "/api/targets/" + T.id + "/check").then(function (x) { T = x.target; paint2(x.result); paint(); }).catch(fail)); } }, icon("refresh"), res ? "Run check again" : "Run check") : null), out, nav(true, "Continue"));
      paint2(res);
    }

    /* step: server setup (runs by itself after sudo access is granted) */
    function sSetup() {
      var st = T.bootstrap, pw = h("input", { type: "password", placeholder: "sudo password (only if needed)", autocomplete: "off" });
      body.replaceChildren(h("h3", null, local ? "2. Server setup" : "4. Server setup"),
        h("p", { class: "sub" }, "After sudo access is granted the panel prepares the machine by itself: installs Python, venv, pip, git, curl and build tools, checks the Python version, creates the deploy folder, then clones your repository, installs its dependencies and runs your test command once. If anything fails you get a notification (and an email when email is set up)."),
        st ? note(st.tested ? "info" : "warn", st.tested ? "Set up and tested" : "Set up", "Finished " + F.ago(st.at) + (st.python ? ". Python: " + st.python + " (" + String(st.version || "") + ")" : "") + (st.tested ? ". The test build passed." : ". The test build was skipped (add a GitHub token in Settings, then run it again).")) : note("info", "Not run yet", "Press the button below, or grant sudo access in the access check step and it starts by itself."),
        can("admin") ? h("div", { class: "row wrap" }, pw, h("button", { class: "btn primary", type: "button", onclick: function (e) {
          busy(e.target.closest("button"), api("POST", "/api/targets/" + T.id + "/bootstrap", { password: pw.value }).then(function (r) { pw.value = ""; O.go("deploy/" + r.id); }).catch(fail));
        } }, icon("rocket"), st ? "Run setup again" : "Set up this server")) : null,
        h("div", { class: "hint" }, "The sudo password is used once, kept in memory only and never stored or written to the log. Leave it empty if the user is root or already has passwordless sudo."),
        nav(true, "Continue", null, "Skip"));
    }

    /* step: done */
    function sDone() {
      var box = h("div", { class: "col" }, O.skel(2));
      body.replaceChildren(h("h3", null, "Summary"), box, nav(false));
      api("GET", "/api/deploy/preflight?target=" + T.id).then(function (p) {
        box.replaceChildren(p.ok ? note("info", "Ready to deploy", T.name + " passed every requirement. Open Deployments, choose it in the server list and press Deploy.") : note("warn", "Not ready yet", "Fix these first:"),
          p.problems.map(function (x) { return h("div", { class: "check-row bad" }, h("span", { class: "ic" }, icon("alert")), h("div", null, h("b", null, x.title), x.detail ? h("div", { class: "sub" }, x.detail) : null, x.href ? h("a", { href: x.href, class: "btn sm", style: { marginTop: "6px" } }, "Fix it") : null)); }),
          h("div", { class: "wz-sum" }, [["Server", T.name], ["Address", local ? "this machine" : T.user + "@" + T.host + ":" + T.port], ["Server setup", setupLabel(T)], ["Access", T.verified ? "verified " + (T.checked_at ? F.ago(T.checked_at) : "") : "not verified"]].map(function (r) { return h("div", null, h("span", { class: "k" }, r[0]), h("code", { class: "inline" }, r[1])); })),
          h("div", { class: "row" }, h("a", { class: "btn primary", href: "#/deploy" }, icon("rocket"), "Go to Deployments"), h("a", { class: "btn", href: "#/servers" }, "All servers")));
      }).catch(function (e) { box.replaceChildren(O.empty("alert", "Could not check", e.message)); });
    }

    api("GET", "/api/targets").then(function (d) {
      defaults = d.defaults;
      if (isNew) { if (!can("admin")) { body.replaceChildren(O.empty("shield", "Admins only", "Ask an administrator to add servers.")); return; } paint(); render(); return; }
      T = d.targets.filter(function (t) { return t.id === tid; })[0];
      if (!T) { body.replaceChildren(O.empty("alert", "Server not found", "", h("a", { class: "btn", href: "#/servers" }, "All servers"))); return; }
      local = T.kind === "local"; var title = document.getElementById("srvTitle"); if (title) title.textContent = T.name;
      step = STEP[T.id] != null ? Math.min(STEP[T.id], names().length - 1) : (T.verified ? names().length - 1 : (local ? 0 : (T.public_key && T.checks ? 2 : 1)));
      paint(); render();
    }).catch(function (e) { body.replaceChildren(O.empty("alert", "Could not load", e.message)); });
  }
})();
