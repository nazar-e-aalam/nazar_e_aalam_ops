/* Nazar Ops: sign-in screens (login, 2FA code, forgot / reset password, accept invite, forced 2FA set-up)
   and the reusable two-factor set-up flow. Loaded after app.js, before pages.js. */
(function () {
  "use strict";
  var O = window.OPS,
    h = O.h,
    icon = O.icon,
    api = O.api,
    busy = O.busy,
    toast = O.toast,
    S = O.S,
    BASE = O.base;

  /* ---------- shared bits ---------- */
  function frame(title, sub, kids) {
    document.title = title + " · Nazar Ops";
    O.$app.replaceChildren(
      h(
        "div",
        { class: "login" },
        h(
          "div",
          { class: "box" },
          h(
            "div",
            { class: "row" },
            h("img", { class: "logo", src: BASE + "/favicon.svg", alt: "" }),
            h(
              "div",
              null,
              h("h1", null, title),
              sub ? h("div", { class: "sub" }, sub) : null,
            ),
          ),
          kids,
          h(
            "div",
            { class: "row sub" },
            h("span", { class: "sp" }),
            O.themeBtn(),
          ),
        ),
      ),
    );
  }
  function field(label, input, hint) {
    return h(
      "div",
      { class: "field" },
      h("label", null, label),
      input,
      hint ? h("div", { class: "hint" }, hint) : null,
    );
  }
  function link(text, fn, _cls='') {
    return h(
      "a",
      {
        href: "#",
        class: "link " + _cls,
        onclick: function (e) {
          e.preventDefault();
          fn();
        },
      },
      text,
    );
  }
  function form(kids, submit) {
    return h(
      "form",
      {
        class: "col",
        style: { gap: "14px" },
        onsubmit: function (e) {
          e.preventDefault();
          submit();
        },
      },
      kids,
    );
  }
  function pwFields(pw, cf, msg) {
    function check() {
      if (!cf.value) {
        msg.textContent = "";
        msg.className = "err-msg";
        return;
      }
      var same = pw.value === cf.value;
      msg.textContent = same ? "Passwords match" : "Passwords do not match";
      msg.className = same ? "ok-msg" : "err-msg";
    }
    pw.addEventListener("input", check);
    cf.addEventListener("input", check);
  }
  /* The token leaves the address bar right away (history, screenshots) but survives a refresh in this tab only. */
  function clearToken(name, token) {
    try {
      if (token) sessionStorage.setItem("ops_tok_" + name, token);
      history.replaceState(
        null,
        "",
        location.pathname + location.search + "#/" + name,
      );
    } catch (e) {
      /* ignore */
    }
  }
  function tokenOf(name) {
    var m = /[?&]token=([^&]+)/.exec(location.hash);
    if (m) return decodeURIComponent(m[1]);
    try {
      return sessionStorage.getItem("ops_tok_" + name) || "";
    } catch (e) {
      return "";
    }
  }
  function forget(name) {
    try {
      sessionStorage.removeItem("ops_tok_" + name);
    } catch (e) {
      /* ignore */
    }
  }
  function done() {
    try {
      history.replaceState(
        null,
        "",
        location.pathname + location.search + "#/",
      );
    } catch (e) {
      /* ignore */
    }
    O.boot();
  }

  /* ---------- sign in ---------- */
  O.renderLogin = function (note) {
    if (O.stop) O.stop();
    var email = h("input", {
      type: "email",
      autocomplete: "username",
      required: true,
      placeholder: "you@company.com",
      autofocus: true,
    });
    var pw = h("input", {
      type: "password",
      autocomplete: "current-password",
      required: true,
      placeholder: "Password",
    });
    var err = h("div", { class: "err-msg", role: "alert" }),
      forgot = h("div", {
        class: "row",
        style: { justifyContent: "center", marginTop: "10px" },
      });
    var btn = h(
      "button",
      { class: "btn primary", type: "submit", style: { padding: "11px" } },
      "Sign in",
    );
    frame("Nazar Ops", "Releases · deployments · workflows", [
      note ? h("div", { class: "notice ok" }, icon("check"), note) : null,
      form(
        [field("Email", email), field("Password", pw), forgot, err, btn],
        function () {
          err.textContent = "";
          busy(
            btn,
            api("POST", "/api/auth/login", {
              email: email.value,
              password: pw.value,
            })
              .then(function (d) {
                if (d.need_2fa) renderTwoFA(d.challenge);
                else done();
              })
              .catch(function (x) {
                err.textContent = x.message;
                pw.select();
              }),
          );
        },
      ),
    ]);
    api("GET", "/api/auth/options")
      .then(function (o) {
        if (o.smtp) forgot.appendChild(link("Forgot password?", renderForgot, 'forget'));
      })
      .catch(function () {});
  };

  function renderTwoFA(challenge) {
    var recovery = false,
      code = h("input", {
        type: "text",
        inputMode: "numeric",
        autocomplete: "one-time-code",
        required: true,
        placeholder: "123456",
        maxLength: 6,
        autofocus: true,
        style: {
          letterSpacing: ".3em",
          textAlign: "center",
          fontSize: "20px",
          fontFamily: "var(--mono)",
        },
      });
    var lbl = h("label", null, "Authentication code"),
      hint = h(
        "div",
        { class: "hint" },
        "Open your authenticator app and enter the 6-digit code.",
      );
    var err = h("div", { class: "err-msg", role: "alert" }),
      btn = h(
        "button",
        { class: "btn primary", type: "submit", style: { padding: "11px" } },
        "Verify",
      );
    var toggle = link("Use a recovery code instead", function () {
      recovery = !recovery;
      code.value = "";
      err.textContent = "";
      code.maxLength = recovery ? 14 : 6;
      code.placeholder = recovery ? "xxxxx-xxxxx" : "123456";
      code.inputMode = recovery ? "text" : "numeric";
      code.style.letterSpacing = recovery ? ".08em" : ".3em";
      code.style.fontSize = recovery ? "16px" : "20px";
      lbl.textContent = recovery ? "Recovery code" : "Authentication code";
      hint.textContent = recovery
        ? "Each recovery code works once."
        : "Open your authenticator app and enter the 6-digit code.";
      toggle.textContent = recovery
        ? "Use my authenticator app"
        : "Use a recovery code instead";
      code.focus();
    });
    function submit() {
      err.textContent = "";
      busy(
        btn,
        api("POST", "/api/auth/2fa", { challenge: challenge, code: code.value })
          .then(done)
          .catch(function (x) {
            err.textContent = x.message;
            code.select();
            if (/password again|expired/i.test(x.message))
              setTimeout(function () {
                O.renderLogin();
              }, 1800);
          }),
      );
    }
    code.addEventListener("input", function () {
      if (!recovery && /^\d{6}$/.test(code.value)) submit();
    });
    frame("Two-step verification", "One more step to sign in", [
      form([h("div", { class: "field" }, lbl, code, hint), err, btn], submit),
      h(
        "div",
        { class: "row wrap sub", style: { justifyContent: "space-between" } },
        toggle,
        link("Back to sign in", function () {
          O.renderLogin();
        }),
      ),
    ]);
  }

  /* ---------- forgot / reset ---------- */
  function renderForgot() {
    var email = h("input", {
      type: "email",
      required: true,
      autocomplete: "username",
      placeholder: "you@company.com",
      autofocus: true,
    });
    var err = h("div", { class: "err-msg", role: "alert" }),
      btn = h(
        "button",
        { class: "btn primary", type: "submit", style: { padding: "11px" } },
        "Send reset link",
      );
    var box = h("div", { class: "col", style: { gap: "14px" } });
    box.append(
      form(
        [
          h(
            "p",
            { class: "sub" },
            "Enter your email and we will send you a link to choose a new password.",
          ),
          field("Email", email),
          err,
          btn,
        ],
        function () {
          err.textContent = "";
          busy(
            btn,
            api("POST", "/api/auth/forgot", { email: email.value })
              .then(function () {
                box.replaceChildren(
                  h(
                    "div",
                    { class: "notice ok" },
                    icon("mail"),
                    "If an account exists for " +
                      email.value +
                      ", a reset link is on its way. It works for 60 minutes.",
                  ),
                  h(
                    "button",
                    {
                      class: "btn",
                      onclick: function () {
                        O.renderLogin();
                      },
                    },
                    "Back to sign in",
                  ),
                );
              })
              .catch(function (x) {
                err.textContent = x.message;
              }),
          );
        },
      ),
      h(
        "div",
        { class: "sub" },
        link("Back to sign in", function () {
          O.renderLogin();
        }),
      ),
    );
    frame("Forgot password", "Reset it by email", box);
  }

  function renderReset(token) {
    clearToken("reset-password", token);
    var box = h("div", { class: "col", style: { gap: "14px" } }, O.skel(2));
    frame("Choose a new password", "", box);
    api("GET", "/api/auth/reset?token=" + encodeURIComponent(token))
      .then(function (d) {
        var pw = h("input", {
            type: "password",
            autocomplete: "new-password",
            required: true,
            placeholder: "At least 10 characters",
            autofocus: true,
          }),
          cf = h("input", {
            type: "password",
            autocomplete: "new-password",
            required: true,
            placeholder: "Repeat the password",
          });
        var msg = h("div", { class: "err-msg", role: "alert" }),
          btn = h(
            "button",
            {
              class: "btn primary",
              type: "submit",
              style: { padding: "11px" },
            },
            "Update password",
          );
        pwFields(pw, cf, msg);
        box.replaceChildren(
          h("p", { class: "sub" }, "For ", h("b", null, d.email)),
          form(
            [
              field("New password", pw),
              field("Confirm password", cf),
              msg,
              btn,
            ],
            function () {
              busy(
                btn,
                api("POST", "/api/auth/reset", {
                  token: token,
                  password: pw.value,
                  confirm_password: cf.value,
                })
                  .then(function () {
                    forget("reset-password");
                    O.renderLogin(
                      "Password updated. Sign in with your new password.",
                    );
                  })
                  .catch(function (x) {
                    msg.className = "err-msg";
                    msg.textContent = x.message;
                  }),
              );
            },
          ),
        );
      })
      .catch(function (x) {
        box.replaceChildren(
          h("div", { class: "notice bad" }, icon("alert"), x.message),
          h(
            "button",
            {
              class: "btn",
              onclick: function () {
                O.renderLogin();
              },
            },
            "Back to sign in",
          ),
        );
      });
  }

  /* ---------- accept invitation ---------- */
  function renderAccept(token) {
    clearToken("accept-invite", token);
    var box = h("div", { class: "col", style: { gap: "14px" } }, O.skel(3));
    frame("Welcome to Nazar Ops", "Accept your invitation", box);
    api("GET", "/api/auth/invite?token=" + encodeURIComponent(token))
      .then(function (d) {
        var fn = h("input", {
            type: "text",
            autocomplete: "given-name",
            required: true,
            maxLength: 40,
            placeholder: "First name",
            autofocus: true,
          }),
          ln = h("input", {
            type: "text",
            autocomplete: "family-name",
            required: true,
            maxLength: 40,
            placeholder: "Last name",
          });
        var pw = h("input", {
            type: "password",
            autocomplete: "new-password",
            required: true,
            placeholder: "At least 10 characters",
          }),
          cf = h("input", {
            type: "password",
            autocomplete: "new-password",
            required: true,
            placeholder: "Repeat the password",
          });
        var msg = h("div", { class: "err-msg", role: "alert" }),
          btn = h(
            "button",
            {
              class: "btn primary",
              type: "submit",
              style: { padding: "11px" },
            },
            "Create account",
          );
        pwFields(pw, cf, msg);
        box.replaceChildren(
          h(
            "div",
            { class: "notice info" },
            icon("mail"),
            h(
              "span",
              null,
              d.invited_by ? d.invited_by + " invited " : "You were invited: ",
              h("b", null, d.email),
              " as ",
              h("b", null, d.role),
              ".",
            ),
          ),
          form(
            [
              h(
                "div",
                { class: "grid g2" },
                field("First name", fn),
                field("Last name", ln),
              ),
              field("Password", pw),
              field("Confirm password", cf),
              msg,
              btn,
            ],
            function () {
              busy(
                btn,
                api("POST", "/api/auth/accept-invite", {
                  token: token,
                  first_name: fn.value,
                  last_name: ln.value,
                  password: pw.value,
                  confirm_password: cf.value,
                })
                  .then(function () {
                    forget("accept-invite");
                    done();
                  })
                  .catch(function (x) {
                    msg.className = "err-msg";
                    msg.textContent = x.message;
                  }),
              );
            },
          ),
        );
      })
      .catch(function (x) {
        box.replaceChildren(
          h("div", { class: "notice bad" }, icon("alert"), x.message),
          h(
            "button",
            {
              class: "btn",
              onclick: function () {
                O.renderLogin();
              },
            },
            "Go to sign in",
          ),
        );
      });
  }

  /* ---------- two-factor set-up (used on the Account tab and for the forced screen) ---------- */
  function recoveryView(codes, onDone) {
    var ack = h("input", { type: "checkbox" }),
      go = h(
        "button",
        { class: "btn primary", disabled: true, onclick: onDone },
        "Done",
      );
    ack.addEventListener("change", function () {
      go.disabled = !ack.checked;
    });
    var text = codes.join("\n");
    return h(
      "div",
      { class: "col", style: { gap: "12px" } },
      h(
        "div",
        { class: "notice ok" },
        icon("check"),
        "Two-step verification is on.",
      ),
      h(
        "p",
        { class: "sub" },
        "Save these recovery codes somewhere safe. Each one signs you in once if you lose your phone. They are shown only now.",
      ),
      h(
        "div",
        { class: "codes" },
        codes.map(function (c) {
          return h("code", null, c);
        }),
      ),
      h(
        "div",
        { class: "row wrap" },
        h(
          "button",
          {
            class: "btn sm",
            onclick: function () {
              O.copy(text, "Recovery codes copied");
            },
          },
          icon("copy"),
          "Copy",
        ),
        h(
          "button",
          {
            class: "btn sm",
            onclick: function () {
              var a = h("a", {
                href: URL.createObjectURL(
                  new Blob(["Nazar Ops recovery codes\n\n" + text + "\n"], {
                    type: "text/plain",
                  }),
                ),
                download: "nazar-ops-recovery-codes.txt",
              });
              document.body.appendChild(a);
              a.click();
              a.remove();
            },
          },
          icon("download"),
          "Download",
        ),
      ),
      h(
        "label",
        { class: "check" },
        ack,
        h("span", null, "I have saved my recovery codes"),
      ),
      h("div", { class: "row" }, go),
    );
  }
  function setupFlow(host, onDone) {
    host.replaceChildren(O.skel(3));
    api("POST", "/api/2fa/setup")
      .then(function (d) {
        var code = h("input", {
          type: "text",
          inputMode: "numeric",
          autocomplete: "one-time-code",
          maxLength: 6,
          placeholder: "123456",
          style: {
            letterSpacing: ".3em",
            textAlign: "center",
            fontSize: "20px",
            fontFamily: "var(--mono)",
            maxWidth: "200px",
          },
        });
        var err = h("div", { class: "err-msg", role: "alert" }),
          btn = h(
            "button",
            { class: "btn primary", type: "submit" },
            "Turn on",
          );
        function submit() {
          err.textContent = "";
          busy(
            btn,
            api("POST", "/api/2fa/enable", { code: code.value })
              .then(function (r) {
                S.me.totp_enabled = true;
                host.replaceChildren(recoveryView(r.recovery_codes, onDone));
              })
              .catch(function (x) {
                err.textContent = x.message;
                code.select();
              }),
          );
        }
        var secret = d.secret.replace(/(.{4})/g, "$1 ").trim();
        host.replaceChildren(
          h(
            "div",
            { class: "twofa" },
            h(
              "div",
              { class: "qrbox" },
              h("img", {
                alt: "QR code for your authenticator app",
                src: "data:image/svg+xml;utf8," + encodeURIComponent(d.qr),
              }),
            ),
            h(
              "div",
              {
                class: "col",
                style: { gap: "12px", minWidth: 0, flex: "1 1 240px" },
              },
              h(
                "ol",
                {
                  class: "sub",
                  style: { paddingLeft: "18px", lineHeight: 1.7 },
                },
                h(
                  "li",
                  null,
                  "Install an authenticator app (Google Authenticator, Microsoft Authenticator, 1Password, Authy…).",
                ),
                h("li", null, "Scan this QR code, or type the key below."),
                h("li", null, "Enter the 6-digit code it shows."),
              ),
              h(
                "div",
                { class: "copyline" },
                h("input", {
                  type: "text",
                  readOnly: true,
                  value: secret,
                  "aria-label": "Setup key",
                }),
                h(
                  "button",
                  {
                    class: "btn",
                    type: "button",
                    onclick: function () {
                      O.copy(d.secret, "Key copied");
                    },
                  },
                  icon("copy"),
                  "Copy",
                ),
              ),
              form(
                [
                  field("Code from the app", code),
                  err,
                  h("div", { class: "row" }, btn),
                ],
                submit,
              ),
            ),
          ),
        );
        code.addEventListener("input", function () {
          if (/^\d{6}$/.test(code.value)) submit();
        });
      })
      .catch(function (x) {
        host.replaceChildren(
          O.empty("alert", "Could not start set-up", x.message),
        );
      });
  }
  O.twofaSetup = setupFlow;
  O.recoveryView = recoveryView;

  function renderForced() {
    var host = h("div", null);
    document.title = "Set up 2FA · Nazar Ops";
    O.$app.replaceChildren(
      h(
        "div",
        { class: "login" },
        h(
          "div",
          { class: "box", style: { width: "min(620px,100%)" } },
          h(
            "div",
            { class: "row" },
            h("img", { class: "logo", src: BASE + "/favicon.svg", alt: "" }),
            h(
              "div",
              null,
              h("h1", null, "Set up two-step verification"),
              h(
                "div",
                { class: "sub" },
                "Your admin requires it before you can continue",
              ),
            ),
          ),
          host,
          h(
            "div",
            { class: "row sub" },
            h(
              "button",
              {
                class: "btn ghost sm",
                onclick: function () {
                  api("POST", "/api/auth/logout").finally(function () {
                    S.me = null;
                    O.renderLogin();
                  });
                },
              },
              icon("out"),
              "Sign out",
            ),
            h("span", { class: "sp" }),
            O.themeBtn(),
          ),
        ),
      ),
    );
    setupFlow(host, function () {
      done();
    });
  }
  O.renderForced = renderForced;

  /* ---------- signed-out routes: #/accept-invite?token=  #/reset-password?token=  #/forgot ---------- */
  O.authRoute = function () {
    var name = location.hash.replace(/^#\/?/, "").split("?")[0];
    if (name === "accept-invite") {
      renderAccept(tokenOf("accept-invite"));
      return true;
    }
    if (name === "reset-password") {
      renderReset(tokenOf("reset-password"));
      return true;
    }
    if (name === "forgot") {
      api("GET", "/api/auth/options")
        .then(function (o) {
          if (o.smtp) renderForgot();
          else O.renderLogin();
        })
        .catch(function () {
          O.renderLogin();
        });
      return true;
    }
    return false;
  };
})();
