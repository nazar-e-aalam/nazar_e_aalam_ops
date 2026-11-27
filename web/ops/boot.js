/* Loads the session, then starts the router. Must be the last script. */
(function () {
  "use strict";
  var O = window.OPS, S = O.S;
  O.pages["404"] = function (page, _, ctx) { ctx.setTitle("Not found"); page.appendChild(O.empty("alert", "Page not found", "That page does not exist.", O.h("a", { class: "btn primary sm", href: "#/" }, "Go to overview"))); };
  var wired = false;
  O.boot = function () {
    if (O.authRoute()) return;                       // invite / reset links work whether or not someone is signed in
    O.api("GET", "/api/me").then(function (d) {
      S.me = d.user; S.info = d;
      if (d.user.must_setup_2fa) { O.renderForced(); return; }
      O.shell();
      if (!wired) { window.addEventListener("hashchange", O.route); wired = true; }
      O.route();
    }).catch(function () { O.renderLogin(); });
  };
  window.addEventListener("hashchange", function () { if (!S.me) { var n = location.hash.replace(/^#\/?/, "").split("?")[0]; if (/^(accept-invite|reset-password|forgot)$/.test(n)) O.authRoute(); } });
  O.boot();
})();
