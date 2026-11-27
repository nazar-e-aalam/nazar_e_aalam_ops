/* First-run setup wizard: domain -> DNS -> server/HTTPS -> GitHub -> save + restart. Panel stays locked until finished. */
(function () {
  "use strict";
  var O = window.OPS, S = O.S, h = O.h, icon = O.icon, api = O.api, toast = O.toast, fail = O.fail, busy = O.busy;
  var STEPS = [["domain", "Domain"], ["dns", "DNS records"], ["server", "Server & HTTPS"], ["github", "GitHub"], ["finish", "Save & finish"]];

  function load() { try { return JSON.parse(sessionStorage.getItem("ops.wz") || "{}"); } catch (e) { return {}; } }
  function store(w) { try { sessionStorage.setItem("ops.wz", JSON.stringify(w)); } catch (e) { } }

  function codeLine(text, label) {
    return h("div", { class: "wz-code" }, h("code", null, text),
      h("button", { class: "btn ghost sm", type: "button", title: "Copy", onclick: function () { O.copy(text, label || "Copied"); } }, icon("copy"), "Copy"));
  }
  function cell(text) { return h("td", null, h("span", { class: "wz-cell" }, h("code", { class: "inline" }, text), h("button", { class: "btn ghost sm icon", type: "button", "aria-label": "Copy " + text, onclick: function () { O.copy(text, "Copied"); } }, icon("copy")))); }
  function note(kind, title, body) { return h("div", { class: "wz-note " + kind }, h("b", null, title), h("div", null, body)); }
  function field(label, input, hint) { return h("div", { class: "field" }, h("label", null, label), input, hint ? h("div", { class: "hint" }, hint) : null); }

  O.pages.setup = function (page, seg, ctx) {
    ctx.setTitle("Setup guide");
    var isAdmin = O.can("admin");
    if (!isAdmin) { page.append(O.empty("shield", "Setup is not finished yet", "An administrator has to finish the first-time setup before the panel can be used. Ask them to sign in and follow the guide.")); return; }
    var wz = Object.assign({ step: 0, domain: "", sub: "ops", https: true, ip: "", keep: true }, load());
    var info = null, card = h("div", { class: "card wz-card" }, O.skel(6));
    var steps = h("ol", { class: "wz-steps" });
    page.append(h("div", { class: "wz-hero" }, h("img", { src: O.base + "/static/shared/logo.svg", alt: "", class: "wz-logo" }),
      h("div", null, h("h2", null, "Welcome to Nazar Ops"), h("p", { class: "sub" }, "A short guide to connect your domain. Nothing starts (auto deploy, release sync) until you finish. It takes about 5 minutes."))), steps, card);

    function paintSteps() {
      steps.replaceChildren.apply(steps, STEPS.map(function (s, i) {
        return h("li", { class: i === wz.step ? "on" : i < wz.step ? "done" : "", onclick: function () { if (i <= wz.step) go(i); } }, h("span", { class: "n" }, i < wz.step ? icon("check") : String(i + 1)), h("span", { class: "l" }, s[1]));
      }));
    }
    function go(i) { wz.step = i; store(wz); paintSteps(); render(); window.scrollTo(0, 0); }
    function nav(next, nextLabel, onNext, skipLabel, onSkip) {
      return h("div", { class: "wz-nav" }, wz.step > 0 ? h("button", { class: "btn ghost", type: "button", onclick: function () { go(wz.step - 1); } }, icon("back"), "Back") : null, h("span", { class: "sp" }),
        skipLabel ? h("button", { class: "btn ghost", type: "button", onclick: onSkip || function () { go(wz.step + 1); } }, skipLabel) : null,
        h("button", { class: "btn primary", type: "button", onclick: function (e) { onNext ? onNext(e) : go(wz.step + 1); } }, nextLabel || "Continue"));
    }
    function hosts() { var d = wz.domain.trim().toLowerCase(), s = (wz.sub || "ops").trim().toLowerCase(); return { root: d, panel: s + "." + d, scheme: wz.https ? "https" : "http" }; }

    /* ---- step 1: domain ---- */
    function sDomain() {
      var dom = h("input", { type: "text", value: wz.domain, placeholder: "example.com", autocomplete: "off", spellcheck: false, autofocus: true }),
        sub = h("input", { type: "text", value: wz.sub, placeholder: "ops", autocomplete: "off", spellcheck: false }),
        ip = h("input", { type: "text", value: wz.ip || info.server_ip, placeholder: "203.0.113.10", spellcheck: false }),
        prev = h("div", { class: "wz-preview" });
      var https = h("input", { type: "checkbox" }); https.checked = wz.https;
      function upd() { wz.domain = dom.value; wz.sub = sub.value; wz.ip = ip.value; wz.https = https.checked; store(wz); var x = hosts();
        prev.replaceChildren(h("div", null, h("span", { class: "k" }, "Ops panel"), h("code", { class: "inline" }, x.root ? x.scheme + "://" + x.panel : "…")), h("div", null, h("span", { class: "k" }, "Public downloads"), h("code", { class: "inline" }, x.root ? x.scheme + "://" + x.root + "/applications" : "…")), h("div", null, h("span", { class: "k" }, "Your API / admin"), h("code", { class: "inline" }, x.root ? x.scheme + "://" + x.root : "…"))); }
      [dom, sub, ip, https].forEach(function (el) { el.addEventListener("input", upd); el.addEventListener("change", upd); }); upd();
      card.replaceChildren(h("h3", null, "1. Choose your domain"),
        h("p", { class: "sub" }, "Use a domain you own (bought from GoDaddy, Namecheap, Cloudflare, Hostinger…). You need two addresses: your main domain, and a sub-domain for this panel."),
        note("info", "What is a sub-domain?", "It is a name added in front of your domain. If your domain is example.com and the sub-domain is ops, the panel will live at ops.example.com while example.com stays for your website/API."),
        h("div", { class: "grid g2" }, field("Main domain", dom, "Only the name, like example.com. No http://, no www."), field("Panel sub-domain", sub, "Default: ops")),
        field("This server's public IP address", ip, info.server_ip ? "Detected automatically. Change it if your provider gave you a different one." : "Could not detect it. Find it in your hosting dashboard (VPS / droplet / EC2 page) and type it here."),
        h("label", { class: "wz-check" }, https, h("span", null, h("b", null, "Use HTTPS (recommended)"), h("small", null, "Gets a free certificate in step 3. Turn off only for a quick test without a certificate."))),
        h("div", { class: "lbl", style: { marginTop: "6px" } }, "Your addresses will be"), prev,
        nav(true, "Continue", function () {
          if (!wz.domain.trim()) { toast("Enter your domain first", "bad"); dom.focus(); return; }
          if (!/^[a-z0-9.-]+\.[a-z]{2,}$/i.test(wz.domain.trim().replace(/^https?:\/\//, "").replace(/^www\./, "").split("/")[0])) { toast("That does not look like a domain, use something like example.com", "bad"); return; }
          wz.domain = wz.domain.trim().toLowerCase().replace(/^https?:\/\//, "").replace(/^www\./, "").split("/")[0]; store(wz); go(1);
        }, "I already configured everything", function () { O.confirm("Skip the guide?", "Use this only if your domain, DNS, nginx and .env are already set up. The panel will unlock right away.", "Skip setup").then(function (y) { if (y) api("POST", "/api/setup/finish").then(function () { S.info.setup_done = true; location.hash = "#/"; O.boot(); }).catch(fail); }); }));
    }

    /* ---- step 2: DNS ---- */
    function sDns() {
      var x = hosts(), ip = wz.ip || info.server_ip || "YOUR_SERVER_IP", res = h("div", { class: "wz-dnsres" });
      function check(e) {
        busy(e.target.closest("button"), api("POST", "/api/setup/dns", { domain: wz.domain, sub: wz.sub, ip: wz.ip || info.server_ip }).then(function (r) {
          res.replaceChildren.apply(res, r.records.map(function (c) {
            return h("div", { class: "check-row " + (c.status === "ok" ? "ok" : c.status === "found" ? "ok" : "bad") }, h("span", { class: "ic" }, icon(c.status === "ok" || c.status === "found" ? "check" : "alert")),
              h("div", null, h("b", null, c.host), h("span", { class: "sub" }, "  ·  " + c.purpose), h("div", { class: "sub" }, c.message)));
          }));
        }).catch(fail));
      }
      card.replaceChildren(h("h3", null, "2. Add DNS records"),
        h("p", { class: "sub" }, "DNS is the phone book of the internet. These records tell the world that your domain names belong to this server. You add them once, at the company where you bought the domain (or wherever its nameservers point, for example Cloudflare)."),
        h("ol", { class: "wz-how" },
          h("li", null, "Sign in to your domain provider and open ", h("b", null, "DNS settings"), " (sometimes called DNS Management, Zone Editor or Name servers → Records)."),
          h("li", null, "Click ", h("b", null, "Add record"), " and create the two records below. Copy each value with the buttons."),
          h("li", null, "Delete any old record with the same Name that points somewhere else (for example a parking page)."),
          h("li", null, "Save, wait 1 to 30 minutes, then press ", h("b", null, "Check DNS"), " below.")),
        h("div", { class: "wz-tablewrap" }, h("table", { class: "wz-table" }, h("thead", null, h("tr", null, ["Type", "Name / Host", "Value / Points to", "TTL", "Used for"].map(function (t) { return h("th", null, t); }))),
          h("tbody", null,
            h("tr", null, cell("A"), cell("@"), cell(ip), cell("Auto / 300"), h("td", { class: "sub" }, "Main domain: " + x.root)),
            h("tr", null, cell("A"), cell(wz.sub || "ops"), cell(ip), cell("Auto / 300"), h("td", { class: "sub" }, "Ops panel: " + x.panel))))),
        note("info", "Which values exactly?", [h("b", null, "Type"), " is always A (an address record). ", h("b", null, "Name"), " is ", h("code", { class: "inline" }, "@"), " for the main domain itself (some providers want you to leave it empty or type the full domain) and ", h("code", { class: "inline" }, wz.sub || "ops"), " for the panel. ", h("b", null, "Value"), " is this server's IPv4 address. Leave TTL on Auto or 300."]),
        note("warn", "Using Cloudflare?", "Set both records to DNS only (grey cloud) until HTTPS works, otherwise the certificate step can fail. You can turn the orange cloud on afterwards."),
        note("info", "Want every sub-domain at once?", [h("span", null, "Optionally add one wildcard record: Type A, Name "), h("code", { class: "inline" }, "*"), h("span", null, ", Value the same IP. Then any new name (staging, api…) already works.")]),
        h("div", { class: "row", style: { marginTop: "6px" } }, h("button", { class: "btn", type: "button", onclick: check }, icon("refresh"), "Check DNS"), h("span", { class: "sub" }, "Safe to run as often as you like.")), res,
        nav(true, "Continue", null, "Skip, I'll do it later"));
    }

    /* ---- step 3: server ---- */
    function sServer() {
      var x = hosts(), body = h("div", { class: "col" }, O.skel(3));
      card.replaceChildren(h("h3", null, "3. Server, nginx and HTTPS"),
        h("p", { class: "sub" }, "nginx is the doorman: it receives visitors on ports 80/443 and passes them to the right app. Certbot gets a free HTTPS certificate. Pick ONE of the two ways below."),
        body, nav(true, "Continue", null, "Skip, already done"));
      api("GET", "/api/server/configs?domain=" + encodeURIComponent(wz.domain) + "&sub=" + encodeURIComponent(wz.sub || "ops")).then(function (r) {
        body.replaceChildren(
          h("div", { class: "wz-opt" }, h("div", { class: "wz-badge" }, "Easiest"), h("h4", null, "Option A: one command (Ubuntu / Debian)"),
            h("p", { class: "sub" }, "Run this on the server, inside the unzipped nazar-ops folder. It installs everything, writes the nginx + systemd files and gets the certificate."),
            codeLine("sudo scripts/install.sh --domain " + x.root + " --email you@" + x.root + (wz.https ? " --ssl" : ""), "Command copied"),
            h("p", { class: "hint" }, "Replace you@" + x.root + " with your real email (Let's Encrypt sends expiry notices there).")),
          h("div", { class: "wz-opt" }, h("h4", null, "Option B: manually"),
            h("p", { class: "sub" }, "1. Open ports 80 and 443 in your firewall / cloud security group.  2. Create each file below at the shown path.  3. Run the commands in order."),
            r.files.map(function (f) {
              var det = h("details", { class: "wz-file" }, h("summary", null, h("b", null, f.name), h("span", { class: "sub" }, "  " + f.path + " · " + f.description)),
                h("pre", { class: "wz-pre" }, f.content), h("button", { class: "btn sm", type: "button", onclick: function () { O.copy(f.content, "File copied"); } }, icon("copy"), "Copy file"));
              return det;
            }),
            h("div", { class: "lbl", style: { marginTop: "10px" } }, "Commands"), r.commands.map(function (c) { return codeLine(c.split("   #")[0], "Command copied"); })),
          note("info", "How to know it worked", "Open " + x.scheme + "://" + x.panel + " in a new tab. You should see this same sign-in page with a padlock in the address bar."));
      }).catch(function (e) { body.replaceChildren(O.empty("alert", "Could not build the server files", e.message)); });
    }

    /* ---- step 4: GitHub ---- */
    function sGithub() {
      var g = info.github, tok = h("input", { type: "password", placeholder: g.token ? "Token already saved (leave empty to keep)" : "github_pat_… or ghp_…", autocomplete: "off", spellcheck: false }),
        org = h("input", { type: "text", value: g.org }), ra = h("input", { type: "text", value: g.repos.app }), rp = h("input", { type: "text", value: g.repos.panel });
      card.replaceChildren(h("h3", null, "4. Connect GitHub ", h("span", { class: "chip plain" }, "optional")),
        h("p", { class: "sub" }, "The panel runs your workflows, shows runs, deploys the panel repo and mirrors your app releases. You can also do this later in Settings → GitHub."),
        note("info", "Create a token (2 minutes)", [h("span", null, "GitHub → your profile → Settings → Developer settings → Personal access tokens → "), h("b", null, "Fine-grained tokens → Generate new token"), h("span", null, ". Select your organisation, pick the two repos, and allow: Contents (read), Actions (read and write), Metadata (read). Public repos can skip the token for release sync, but workflows and deployments need it.")]),
        field("Personal access token", tok, "Stored on the server and never shown again."),
        h("div", { class: "grid g3" }, field("Organisation / user", org), field("App repo (releases)", ra), field("Panel repo (deployed)", rp)),
        nav(true, "Save & continue", function (e) {
          busy(e.target.closest("button"), api("PUT", "/api/github/settings", { token: tok.value, org: org.value, repo_app: ra.value, repo_panel: rp.value }).then(function () { toast("GitHub saved", "ok"); return api("GET", "/api/setup").then(function (i) { info = i; go(4); }); }).catch(fail));
        }, "Skip for now"));
    }

    /* ---- step 5: finish ---- */
    function sFinish() {
      var x = hosts(), keep = h("input", { type: "checkbox" }); keep.checked = wz.keep; keep.onchange = function () { wz.keep = keep.checked; store(wz); };
      var out = h("div", { class: "col" });
      card.replaceChildren(h("h3", null, "5. Save and finish"),
        h("p", { class: "sub" }, "Last step: the settings are written to your .env file and the panel restarts once to use them."),
        h("div", { class: "wz-sum" }, [["Main domain", x.root], ["Panel address", x.scheme + "://" + x.panel], ["Public downloads", x.scheme + "://" + x.root + "/applications"], ["Settings file", info.env_file]].map(function (r) { return h("div", null, h("span", { class: "k" }, r[0]), h("code", { class: "inline" }, r[1])); })),
        h("label", { class: "wz-check" }, keep, h("span", null, h("b", null, "Keep the current address working too (recommended)"), h("small", null, "After the restart the panel answers on " + x.panel + ". With this on, the address you are using right now keeps working at /ops, so you can never lock yourself out while DNS or HTTPS is still settling. Turn it off later by setting OPS_DEV=false."))),
        !info.env_writable ? note("warn", "Cannot write the .env file", "Edit " + info.env_file + " by hand: OPS_ROOT_DOMAIN=" + x.root + ", OPS_SUBDOMAIN=" + (wz.sub || "ops") + ", OPS_HTTPS=" + (wz.https ? "true" : "false") + ". Then restart the panel and press Finish.") : null,
        out,
        h("div", { class: "wz-nav" }, h("button", { class: "btn ghost", type: "button", onclick: function () { go(3); } }, icon("back"), "Back"), h("span", { class: "sp" }),
          h("button", { class: "btn primary", type: "button", onclick: function (e) {
            busy(e.target.closest("button"), api("POST", "/api/setup/save", { domain: wz.domain, sub: wz.sub, https: wz.https, ip: wz.ip, keep_access: wz.keep }).then(function (r) {
              toast("Saved to " + r.env_file, "ok");
              var steps2 = [];
              if (r.restart_needed) {
                steps2.push(note("warn", "Restart needed", info.systemd ? "The panel will restart now to load the new domain." : "Run this on the server, then come back and press Finish: sudo systemctl restart nazar-ops"));
                if (info.systemd) steps2.push(h("button", { class: "btn primary", type: "button", onclick: function (ev) { restart(ev.target.closest("button"), out, finish); } }, icon("refresh"), "Restart now"));
              }
              steps2.push(h("button", { class: "btn primary", type: "button", onclick: finish }, icon("check"), "Finish setup"));
              out.replaceChildren.apply(out, steps2);
              if (!r.restart_needed) finish();
            }).catch(fail));
          } }, icon("check"), "Save configuration")));
      function finish() { api("POST", "/api/setup/finish").then(function () { try { sessionStorage.removeItem("ops.wz"); } catch (e) { } S.info.setup_done = true; toast("Setup complete. Welcome aboard!", "ok"); location.hash = "#/"; O.boot(); }).catch(fail); }
    }
    function restart(btn, out) {
      btn.disabled = true; btn.textContent = "Restarting…";
      api("POST", "/api/setup/restart").then(function () {
        var tries = 0, t = setInterval(function () { tries++; fetch(O.base + "/api/me", { credentials: "same-origin" }).then(function (r) { if (r.ok || r.status === 401) { clearInterval(t); toast("Panel is back", "ok"); btn.replaceWith(h("span", { class: "chip ok" }, h("i", { class: "d" }), "Restarted")); } }).catch(function () { }); if (tries > 40) { clearInterval(t); btn.textContent = "Taking long, check the service"; } }, 1500);
      }).catch(function (e) { btn.disabled = false; btn.textContent = "Restart now"; fail(e); });
    }

    function render() { [sDomain, sDns, sServer, sGithub, sFinish][wz.step](); }
    api("GET", "/api/setup").then(function (i) { info = i; if (!wz.domain && !i.placeholder) wz.domain = i.domain; if (i.sub && wz.sub === "ops") wz.sub = i.sub; paintSteps(); render(); }).catch(function (e) { card.replaceChildren(O.empty("alert", "Could not load setup", e.message)); });
  };
})();
