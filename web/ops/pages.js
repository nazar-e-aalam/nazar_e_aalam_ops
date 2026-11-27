/* Nazar Ops pages: deployments, environment, releases, settings */
(function () {
  "use strict";
  var O = window.OPS,
    h = O.h,
    icon = O.icon,
    api = O.api,
    toast = O.toast,
    fail = O.fail,
    busy = O.busy,
    modal = O.modal,
    F = O.fmt,
    chip = O.chip,
    can = O.can,
    S = O.S;
  var BASE = O.base;

  function stageIcon(st) {
    return st === "success"
      ? "check"
      : st === "failed"
        ? "x"
        : st === "running"
          ? "refresh"
          : st === "skipped"
            ? "back"
            : "info";
  }
  function stagesView(stages) {
    return h(
      "div",
      { class: "stages" },
      stages
        .filter(function (s) {
          return s.status !== "pending" || !/rollback/.test(s.key);
        })
        .map(function (s) {
          var d = s.started && s.finished ? F.dur(s.finished - s.started) : "";
          return h(
            "div",
            { class: "stage " + s.status },
            h("span", { class: "ic" }, icon(stageIcon(s.status))),
            h(
              "div",
              null,
              s.label,
              s.detail ? h("span", { class: "d" }, "  ·  " + s.detail) : null,
            ),
            h("span", { class: "d mono" }, d),
          );
        }),
    );
  }
  function bar(pct, warnAt) {
    var c = pct > 90 ? "bad" : pct > (warnAt || 75) ? "warn" : "";
    return h(
      "div",
      { class: "bar " + c },
      h("i", { style: { width: Math.min(100, Math.max(2, pct)) + "%" } }),
    );
  }

  /* ---------- Deployments ---------- */
  O.pages.deploy = function (page, seg, ctx) {
    if (seg[0]) return deployDetail(page, +seg[0], ctx);
    ctx.setTitle("Deployments");
    var statusBox = h("div", { class: "grid g4" }, O.skel(1)),
      headBox = h("div", { class: "card" }, O.skel(1)),
      histBox = h("div", { class: "card" }, O.skel(4)),
      relBox = h("div", { class: "card" });
    page.append(
      h(
        "div",
        { class: "page-head" },
        h(
          "div",
          null,
          h("h2", null, "Deployments"),
          h(
            "p",
            null,
            "Every commit of the panel repo is fetched fresh, tested, and swapped in atomically, on this server or another one over SSH. A failed health check rolls back automatically.",
          ),
        ),
      ),
      statusBox,
      headBox,
      h(
        "div",
        { class: "grid g2", style: { alignItems: "start" } },
        histBox,
        relBox,
      ),
    );
    var cfg = null,
      target = +(sessionStorage.getItem("ops.target") || 0),
      pre = null,
      targets = [],
      sel = h("select", {
        "aria-label": "Server to deploy to",
        class: "target-sel",
        onchange: function () {
          target = +sel.value;
          sessionStorage.setItem("ops.target", target);
          loadStatus();
          loadHead();
          loadHist();
        },
      });
    page
      .querySelector(".page-head")
      .append(
        h(
          "div",
          { class: "row", style: { gap: "8px" } },
          icon("server"),
          sel,
          can("admin")
            ? h(
                "a",
                { class: "btn sm ghost", href: "#/servers" },
                "Manage servers",
              )
            : null,
        ),
      );
    function loadStatus() {
      api("GET", "/api/deploy/status?target=" + target)
        .then(function (d) {
          cfg = d.cfg;
          pre = d.preflight;
          targets = d.targets;
          if (
            !targets.some(function (t) {
              return t.id === target;
            })
          ) {
            target = 0;
            sessionStorage.setItem("ops.target", 0);
          }
          sel.replaceChildren.apply(
            sel,
            targets.map(function (t) {
              return h(
                "option",
                { value: t.id, selected: t.id === target },
                t.name + (t.kind === "local" ? "" : " · " + t.host),
              );
            }),
          );
          paintHead();
          var s = d.status,
            cur = s.current,
            m = s.mem,
            dk = s.disk,
            ok = s.health && s.health.ok;
          statusBox.replaceChildren(
            h(
              "div",
              { class: "card tile " + (!s.health ? "" : ok ? "ok" : "bad") },
              h("i", { class: "glow" }),
              h("div", { class: "k" }, "Server"),
              h(
                "div",
                { class: "big" },
                !s.health ? "Not monitored" : ok ? "Healthy" : "Down",
              ),
              h(
                "div",
                { class: "sub mono" },
                s.health
                  ? s.health.ms != null
                    ? s.health.ms + " ms"
                    : s.health.error || ""
                  : "no health URL",
              ),
            ),
            h(
              "div",
              { class: "card tile" },
              h("i", { class: "glow" }),
              h("div", { class: "k" }, "Live release"),
              h(
                "div",
                { class: "big mono" },
                cur
                  ? cur.short_sha
                  : s.active
                    ? s.active.split("-").pop()
                    : "—",
              ),
              h(
                "div",
                { class: "sub" },
                cur
                  ? F.ago(cur.finished_at) +
                      (cur.env_version ? " · .env v" + cur.env_version : "")
                  : "nothing deployed",
              ),
            ),
            h(
              "div",
              { class: "card tile" },
              h("div", { class: "k" }, "Memory / Disk"),
              m
                ? [
                    h(
                      "div",
                      { class: "sub" },
                      F.bytes(m.total - m.available) +
                        " of " +
                        F.bytes(m.total),
                    ),
                    bar(((m.total - m.available) / m.total) * 100),
                  ]
                : h("div", { class: "sub" }, "n/a"),
              dk
                ? [
                    h(
                      "div",
                      { class: "sub", style: { marginTop: "6px" } },
                      F.bytes(dk.total - dk.free) + " of " + F.bytes(dk.total),
                    ),
                    bar(((dk.total - dk.free) / dk.total) * 100, 80),
                  ]
                : null,
            ),
            h(
              "div",
              { class: "card tile" },
              h("div", { class: "k" }, "Auto deploy"),
              h("div", { class: "big" }, cfg.auto_deploy ? "On" : "Off"),
              h(
                "div",
                { class: "sub" },
                cfg.auto_deploy
                  ? "polling " + cfg.branch + " every " + cfg.poll_seconds + "s"
                  : "manual or webhook only",
              ),
              s.load
                ? h(
                    "div",
                    { class: "sub mono" },
                    "load " +
                      s.load
                        .map(function (x) {
                          return x.toFixed(2);
                        })
                        .join(" "),
                  )
                : null,
            ),
          );
          if (!s.root_exists) headBox.dataset.warn = "1";
        })
        .catch(function (e) {
          statusBox.replaceChildren(
            O.empty("alert", "Could not load status", e.message),
          );
        });
    }
    var headData = null;
    function readyBox() {
      if (!pre || pre.ok) return null;
      return h(
        "div",
        { class: "wz-note warn", style: { marginTop: "10px" } },
        h("b", null, "Deployments are paused until this is fixed"),
        pre.problems.map(function (p) {
          return h(
            "div",
            { class: "row wrap", style: { gap: "8px", marginTop: "4px" } },
            icon("alert"),
            h(
              "span",
              { style: { flex: 1, minWidth: 0 } },
              h("b", null, p.title),
              p.detail ? h("span", { class: "sub" }, "  " + p.detail) : null,
            ),
            p.href ? h("a", { class: "btn sm", href: p.href }, "Fix") : null,
          );
        }),
      );
    }
    function paintHead() {
      var c = headData;
      if (!c) return;
      var blocked = pre && !pre.ok;
      headBox.replaceChildren(
        h("h3", null, icon("rocket"), "Latest commit on the branch"),
        h(
          "div",
          { class: "row wrap" },
          h("code", { class: "inline" }, c.sha.slice(0, 7)),
          h("b", { style: { minWidth: 0, flex: 1 } }, c.message),
          h("span", { class: "sub" }, c.author),
          c.deployed
            ? chip("success", "deployed")
            : chip("queued", "not deployed"),
          can("operator")
            ? h(
                "button",
                {
                  class: "btn primary",
                  disabled: blocked,
                  title: blocked ? "Fix the items below first" : "",
                  onclick: function (e) {
                    startDeploy(e.target.closest("button"), c);
                  },
                },
                icon("rocket"),
                c.deployed ? "Redeploy" : "Deploy now",
              )
            : null,
        ),
        readyBox(),
      );
    }
    function loadHead() {
      api("GET", "/api/deploy/head")
        .then(function (c) {
          headData = c;
          paintHead();
        })
        .catch(function (e) {
          headBox.replaceChildren(
            h("h3", null, "Latest commit"),
            O.empty(
              "alert",
              "Cannot read the repository",
              e.message,
              h(
                "a",
                { class: "btn sm", href: "#/settings/github" },
                "Check GitHub token",
              ),
            ),
          );
        });
    }
    function startDeploy(btn, c) {
      O.confirm(
        (c.deployed ? "Redeploy " : "Deploy ") + c.sha.slice(0, 7) + "?",
        "The commit is fetched, tested and swapped in. If tests fail the running version is not touched.",
        "Deploy",
      ).then(function (y) {
        if (y)
          busy(
            btn,
            api("POST", "/api/deploy", { ref: c.sha, target_id: target })
              .then(function (r) {
                O.go("deploy/" + r.id);
              })
              .catch(fail),
          );
      });
    }
    function loadHist() {
      api("GET", "/api/deploy/list?target=" + target)
        .then(function (d) {
          histBox.replaceChildren(
            h("h3", null, "History"),
            d.deployments.length
              ? d.deployments.map(O.depRow)
              : O.empty(
                  "rocket",
                  "No deployments yet",
                  "Deploy the latest commit above.",
                ),
          );
          relBox.replaceChildren(
            h("h3", null, icon("box"), "Releases on server"),
            d.releases.length
              ? d.releases.map(function (r) {
                  return h(
                    "div",
                    {
                      class: "row",
                      style: {
                        padding: "9px 0",
                        borderBottom: "1px solid var(--line)",
                      },
                    },
                    h(
                      "div",
                      { style: { minWidth: 0, flex: 1 } },
                      h(
                        "div",
                        { class: "row" },
                        h("code", { class: "inline" }, r.sha),
                        r.active ? chip("success", "live") : null,
                      ),
                      h(
                        "div",
                        {
                          class: "sub",
                          style: {
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            whiteSpace: "nowrap",
                          },
                        },
                        r.message || r.name,
                      ),
                      h(
                        "div",
                        { class: "sub" },
                        F.ago(r.mtime) + " · " + F.bytes(r.size),
                      ),
                    ),
                    !r.active && can("operator")
                      ? h(
                          "button",
                          {
                            class: "btn sm",
                            onclick: function (e) {
                              O.confirm(
                                "Roll back to " + r.sha + "?",
                                "Switches the live server to this release and restarts it. The current release stays on disk.",
                                "Roll back",
                                true,
                              ).then(function (y) {
                                if (y)
                                  busy(
                                    e.target.closest("button"),
                                    api("POST", "/api/deploy/rollback", {
                                      release: r.name,
                                      target_id: target,
                                    })
                                      .then(function (x) {
                                        O.go("deploy/" + x.id);
                                      })
                                      .catch(fail),
                                  );
                              });
                            },
                          },
                          icon("undo"),
                          "Roll back",
                        )
                      : null,
                  );
                })
              : O.empty(
                  "box",
                  "No releases on disk",
                  "They appear after the first deployment.",
                ),
          );
        })
        .catch(function () {});
    }
    loadStatus();
    loadHead();
    loadHist();
    O.poll(function () {
      loadStatus();
      loadHist();
    }, 5000);
    O.poll(loadHead, 30000);
  };

  function deployDetail(page, id, ctx) {
    ctx.setTitle(["Deployments", "#" + id]);
    var head = h("div", { class: "card" }, O.skel(1)),
      stg = h("div", { class: "card" }),
      logEl = h("div", { class: "log" }, "Waiting for output…");
    page.append(
      h(
        "a",
        {
          class: "btn ghost sm",
          href: "#/deploy",
          style: { alignSelf: "flex-start" },
        },
        icon("back"),
        "All deployments",
      ),
      head,
      h(
        "div",
        {
          class: "graph-wrap",
          style: { gridTemplateColumns: "minmax(0,1fr) 340px" },
        },
        h("div", { class: "card" }, h("h3", null, "Log"), logEl),
        stg,
      ),
    );
    var offset = 0,
      d = null,
      first = true;
    function paintLog(r) {
      if (!r.text) return;
      var stick =
        first ||
        logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 40;
      if (first) {
        logEl.replaceChildren();
        first = false;
      }
      r.text.split("\n").forEach(function (l) {
        if (!l) return;
        var m = /^(\d\d:\d\d:\d\d)\s\s(.*)$/.exec(l);
        var cls = /FAILED|ERROR|error|Traceback|failed/.test(l)
          ? "er"
          : /^\S+\s\s\$ /.test(l)
            ? "gp"
            : /Done\.|health OK/.test(l)
              ? "ok"
              : "";
        logEl.appendChild(
          h(
            "div",
            null,
            m ? h("span", { class: "ts" }, m[1] + " ") : null,
            h("span", { class: cls }, m ? m[2] : l),
          ),
        );
      });
      if (stick) logEl.scrollTop = logEl.scrollHeight;
    }
    function load() {
      api("GET", "/api/deploy/" + id)
        .then(function (x) {
          d = x;
          var live = x.status === "queued" || x.status === "running";
          head.replaceChildren(
            h(
              "div",
              { class: "row wrap", style: { gap: "14px" } },
              chip(x.status),
              h(
                "div",
                { style: { minWidth: 0, flex: 1 } },
                h(
                  "div",
                  { class: "big", style: { fontSize: "19px" } },
                  x.kind === "rollback"
                    ? "Rollback to " + x.release_name
                    : x.message || "Deployment #" + id,
                ),
                h(
                  "div",
                  { class: "sub row wrap", style: { gap: "12px" } },
                  x.short_sha
                    ? h("span", { class: "mono" }, x.short_sha)
                    : null,
                  h("span", null, x.author || ""),
                  h("span", null, x.trigger + " by " + x.triggered_by),
                  h("span", null, F.dt(x.queued_at)),
                  x.finished_at && x.started_at
                    ? h(
                        "span",
                        { class: "mono" },
                        F.dur(x.finished_at - x.started_at),
                      )
                    : null,
                  x.env_version
                    ? h("span", null, ".env v" + x.env_version)
                    : null,
                ),
              ),
              live && can("operator")
                ? h(
                    "button",
                    {
                      class: "btn danger",
                      onclick: function (e) {
                        busy(
                          e.target.closest("button"),
                          api("POST", "/api/deploy/" + id + "/cancel")
                            .then(function (r) {
                              toast(r.message, "info");
                            })
                            .catch(fail),
                        );
                      },
                    },
                    icon("stop", "fill"),
                    "Cancel",
                  )
                : null,
            ),
            x.error
              ? h(
                  "div",
                  {
                    class: "chip failed",
                    style: { marginTop: "10px", whiteSpace: "normal" },
                  },
                  icon("alert"),
                  x.error,
                )
              : null,
            x.rolled_back
              ? h(
                  "div",
                  { class: "chip warn", style: { marginTop: "8px" } },
                  "Automatically rolled back to " + x.prev_release,
                )
              : null,
          );
          stg.replaceChildren(h("h3", null, "Pipeline"), stagesView(x.stages));
          return api("GET", "/api/deploy/" + id + "/log?offset=" + offset).then(
            function (r) {
              offset = r.next;
              paintLog(r);
              if (!live && !r.text) {
                if (first) {
                  logEl.textContent = "No log output.";
                }
              }
            },
          );
        })
        .catch(function (e) {
          head.replaceChildren(
            O.empty("alert", "Could not load deployment", e.message),
          );
        });
    }
    load();
    O.poll(function () {
      if (!d || d.status === "queued" || d.status === "running") load();
    }, 1500);
  }

  /* ---------- Environment ---------- */
  O.pages.env = function (page, seg, ctx) {
    ctx.setTitle("Environment");
    var curBox = h("div", { class: "card" }, O.skel(4)),
      verBox = h("div", { class: "card" });
    page.append(
      h(
        "div",
        { class: "page-head" },
        h(
          "div",
          null,
          h("h2", null, "Environment (.env)"),
          h(
            "p",
            null,
            "The admin panel's .env. The current version is written into every new deployment (file mode 600). Values are masked here.",
          ),
        ),
      ),
      h(
        "div",
        {
          class: "grid g2",
          style: {
            gridTemplateColumns: "minmax(0,1.4fr) minmax(0,1fr)",
            alignItems: "start",
          },
        },
        curBox,
        verBox,
      ),
    );
    var revealed = null;
    function load() {
      api("GET", "/api/env")
        .then(function (d) {
          var c = d.current;
          var up = can("admin")
            ? [
                h(
                  "button",
                  {
                    class: "btn primary",
                    onclick: function () {
                      editDialog(c);
                    },
                  },
                  icon("up"),
                  "Upload / edit",
                ),
                c
                  ? h(
                      "button",
                      {
                        class: "btn",
                        onclick: function (e) {
                          toggleReveal(e.target.closest("button"));
                        },
                      },
                      icon(revealed ? "eyeoff" : "eye"),
                      revealed ? "Hide" : "Reveal",
                    )
                  : null,
                c
                  ? h(
                      "a",
                      { class: "btn", href: BASE + "/api/env/download" },
                      icon("download"),
                      ".env",
                    )
                  : null,
              ]
            : null;
          curBox.replaceChildren(
            h(
              "h3",
              null,
              icon("key"),
              c ? "Active version v" + c.version : "No .env yet",
              h("span", { class: "sp" }),
            ),
            h("div", { class: "row wrap" }, up),
            c
              ? [
                  h(
                    "div",
                    { class: "sub", style: { margin: "10px 0" } },
                    c.keys +
                      " variables · saved by " +
                      c.created_by +
                      " " +
                      F.ago(c.created_at) +
                      (c.note ? " · " + c.note : "") +
                      " · deployed to: " +
                      d.target,
                  ),
                  c.problems.length
                    ? h(
                        "div",
                        {
                          class: "chip warn",
                          style: { whiteSpace: "normal", marginBottom: "10px" },
                        },
                        c.problems[0],
                      )
                    : null,
                  h(
                    "div",
                    { class: "envvars" },
                    (revealed ? parseEnv(revealed.content) : c.variables).map(
                      function (v) {
                        return h(
                          "div",
                          { class: "v" },
                          h("b", null, v.key),
                          h("span", null, v.empty ? "(empty)" : v.value),
                        );
                      },
                    ),
                  ),
                ]
              : O.empty(
                  "key",
                  "Nothing uploaded",
                  "Upload the .env your admin panel needs and every deployment will include it.",
                ),
          );
          verBox.replaceChildren(
            h("h3", null, "Versions"),
            d.versions.length
              ? d.versions.map(function (v) {
                  return h(
                    "div",
                    {
                      class: "row",
                      style: {
                        padding: "8px 0",
                        borderBottom: "1px solid var(--line)",
                      },
                    },
                    h("code", { class: "inline" }, "v" + v.version),
                    h(
                      "div",
                      { style: { minWidth: 0, flex: 1 } },
                      h(
                        "div",
                        { class: "sub" },
                        v.key_count + " keys · " + v.created_by,
                      ),
                      h(
                        "div",
                        { class: "sub" },
                        F.ago(v.created_at) + (v.note ? " · " + v.note : ""),
                      ),
                    ),
                    v.version !== (c && c.version) && can("admin")
                      ? [
                          h(
                            "button",
                            {
                              class: "btn sm ghost",
                              onclick: function () {
                                diffDialog(v.version, c.version);
                              },
                            },
                            "Diff",
                          ),
                          h(
                            "button",
                            {
                              class: "btn sm",
                              onclick: function (e) {
                                O.confirm(
                                  "Restore v" + v.version + "?",
                                  "Creates a new version with this content. Deploy again to apply it on the server.",
                                  "Restore",
                                ).then(function (y) {
                                  if (y)
                                    busy(
                                      e.target.closest("button"),
                                      api(
                                        "POST",
                                        "/api/env/restore/" + v.version,
                                      )
                                        .then(function (r) {
                                          toast(
                                            "Restored as v" + r.version,
                                            "ok",
                                          );
                                          revealed = null;
                                          load();
                                        })
                                        .catch(fail),
                                    );
                                });
                              },
                            },
                            "Restore",
                          ),
                        ]
                      : chip("success", "active"),
                  );
                })
              : h("div", { class: "sub" }, "No versions yet."),
          );
        })
        .catch(function (e) {
          curBox.replaceChildren(O.empty("alert", "Could not load", e.message));
        });
    }
    function parseEnv(t) {
      return t
        .split("\n")
        .map(function (l) {
          var m = /^\s*(?:export\s+)?([A-Za-z_][\w.\-]*)\s*=\s*(.*)$/.exec(l);
          return m && !/^\s*#/.test(l)
            ? { key: m[1], value: m[2], empty: m[2].trim() === "" }
            : null;
        })
        .filter(Boolean);
    }
    function toggleReveal(btn) {
      if (revealed) {
        revealed = null;
        return load();
      }
      busy(
        btn,
        api("POST", "/api/env/reveal")
          .then(function (r) {
            revealed = r;
            load();
            toast("Values shown. This was logged.", "info");
          })
          .catch(fail),
      );
    }
    function diffDialog(a, b) {
      api("GET", "/api/env/diff?a=" + a + "&b=" + b)
        .then(function (d) {
          function g(t, l, c) {
            return l.length
              ? h(
                  "div",
                  { class: "row wrap" },
                  chip(c, t),
                  l.map(function (k) {
                    return h("code", { class: "inline" }, k);
                  }),
                )
              : null;
          }
          modal(
            "v" + a + " → v" + b + " (key names only)",
            h(
              "div",
              { class: "col" },
              g("added", d.added, "ok"),
              g("removed", d.removed, "bad"),
              g("changed", d.changed, "warn"),
              !d.added.length && !d.removed.length && !d.changed.length
                ? h("div", { class: "sub" }, "Identical.")
                : null,
            ),
          );
        })
        .catch(fail);
    }
    function editDialog(c) {
      var ta = h("textarea", {
          rows: 12,
          spellcheck: false,
          placeholder: "KEY=value\nDATABASE_URL=postgres://…",
          style: { minHeight: "240px" },
        }),
        note = h("input", {
          type: "text",
          placeholder: "Optional note, e.g. added SMTP settings",
        });
      var drop = h(
          "div",
          { class: "drop", tabindex: 0, role: "button" },
          icon("up"),
          h("div", null, "Drop a .env file here, or click to choose"),
        ),
        file = h("input", { type: "file", hidden: true });
      function readFile(f) {
        if (!f) return;
        if (f.size > 262144) return toast("File is larger than 256 KB", "err");
        var r = new FileReader();
        r.onload = function () {
          ta.value = r.result;
          note.value = note.value || "uploaded " + f.name;
        };
        r.readAsText(f);
      }
      drop.onclick = function () {
        file.click();
      };
      file.onchange = function () {
        readFile(file.files[0]);
      };
      drop.ondragover = function (e) {
        e.preventDefault();
        drop.classList.add("over");
      };
      drop.ondragleave = function () {
        drop.classList.remove("over");
      };
      drop.ondrop = function (e) {
        e.preventDefault();
        drop.classList.remove("over");
        readFile(e.dataTransfer.files[0]);
      };
      modal(
        "Upload or edit .env",
        h(
          "div",
          { class: "col" },
          drop,
          file,
          h(
            "div",
            { class: "field" },
            h("label", null, "Content"),
            ta,
            h(
              "div",
              { class: "hint" },
              c
                ? "Existing values are never sent to your browser unless you press Reveal. Paste or upload the full new file; it replaces the current version."
                : "Paste the file or upload it above.",
            ),
          ),
          h("div", { class: "field" }, h("label", null, "Note"), note),
        ),
        function (close) {
          var go = h("button", { class: "btn primary" }, "Save new version");
          go.onclick = function () {
            if (!ta.value.trim()) return toast("Content is empty", "err");
            busy(
              go,
              api("PUT", "/api/env", { content: ta.value, note: note.value })
                .then(function (r) {
                  close();
                  toast("Saved v" + r.version + ". Deploy to apply.", "ok");
                  revealed = null;
                  load();
                })
                .catch(fail),
            );
          };
          return [h("button", { class: "btn", onclick: close }, "Cancel"), go];
        },
        true,
      );
    }
    load();
  };

  /* ---------- Releases (public library) ---------- */
  O.pages.releases = function (page, seg, ctx) {
    ctx.setTitle("Releases");
    var box = h("div", { class: "col" }, O.skel(3)),
      syncBox = h("div", { class: "card" }, O.skel(1)),
      proxyBox = h("div", { class: "card" }, O.skel(1)),
      pubUrl = S.info.public_url + "/applications";
    page.append(
      h(
        "div",
        { class: "page-head" },
        h(
          "div",
          null,
          h("h2", null, "Releases"),
          h("p", null, "Builds published on your public downloads page."),
        ),
        h("span", { class: "sp" }),
        h(
          "button",
          {
            class: "btn",
            onclick: function () {
              O.copy(pubUrl, "Link copied");
            },
          },
          icon("copy"),
          "Copy public link",
        ),
        h(
          "a",
          { class: "btn", href: pubUrl, target: "_blank", rel: "noopener" },
          icon("ext"),
          "Open",
        ),
        can("operator")
          ? [
              h(
                "button",
                {
                  class: "btn",
                  onclick: function () {
                    importDialog();
                  },
                },
                icon("link"),
                "Import one release",
              ),
              h(
                "button",
                {
                  class: "btn primary",
                  onclick: function () {
                    uploadDialog();
                  },
                },
                icon("up"),
                "Upload build",
              ),
            ]
          : null,
      ),
      syncBox,
      proxyBox,
      box,
    );
    var data = null;
    function load() {
      api("GET", "/api/library")
        .then(function (d) {
          data = d;
          var any = false;
          box.replaceChildren.apply(
            box,
            d.apps
              .map(function (a) {
                var rels = d.releases.filter(function (r) {
                  return r.app_id === a.id;
                });
                any = any || rels.length;
                return h(
                  "div",
                  { class: "card" },
                  h(
                    "h3",
                    null,
                    icon("box"),
                    a.name,
                    h("span", { class: "sp" }),
                    h("code", { class: "inline" }, "/applications/" + a.slug),
                  ),
                  rels.length
                    ? rels.map(relCard)
                    : O.empty(
                        "box",
                        "No releases yet",
                        "Upload a build, or import one from GitHub.",
                        can("operator")
                          ? h(
                              "button",
                              {
                                class: "btn primary sm",
                                onclick: function () {
                                  uploadDialog(a.slug);
                                },
                              },
                              "Upload build",
                            )
                          : null,
                      ),
                );
              })
              .concat(
                can("admin")
                  ? [
                      h(
                        "button",
                        {
                          class: "btn",
                          style: { alignSelf: "flex-start" },
                          onclick: newApp,
                        },
                        icon("plus"),
                        "Add another app",
                      ),
                    ]
                  : [],
              ),
          );
        })
        .catch(function (e) {
          box.replaceChildren(O.empty("alert", "Could not load", e.message));
        });
    }

    /* Download proxy: edit, test (HEAD), and see what visitors get */
    function chk(label, r) {
      if (!r) return h("span", { class: "chip plain" }, label + ": off");
      return h(
        "span",
        { class: "chip " + (r.ok ? "ok" : "bad"), title: r.error || "" },
        h("i", { class: "d" }),
        label +
          ": " +
          (r.ok
            ? "works" + (r.ms != null ? " · " + r.ms + " ms" : "")
            : r.status
              ? "HTTP " + r.status
              : r.error || "failed"),
      );
    }
    function loadProxy() {
      api("GET", "/api/library/proxy")
        .then(function (p) {
          var admin = can("admin"),
            inp = h("input", {
              type: "text",
              value: p.url,
              placeholder: "https://gh-proxy.org  (empty = direct GitHub only)",
              disabled: !admin,
              spellcheck: false,
              style: { flex: 1, minWidth: "260px" },
            });
          var res = h("div", {
            class: "row wrap",
            style: { marginTop: "10px", gap: "8px" },
          });
          function show(r) {
            res.replaceChildren(
              chk("GitHub direct", r.direct),
              chk("Proxy", r.via_proxy),
              chip(
                r.using === "proxy" ? "success" : "warn",
                "Visitors get: " +
                  (r.using === "proxy" ? "proxy URL" : "direct GitHub URL"),
              ),
              h(
                "span",
                {
                  class: "sub mono",
                  style: { flex: "1 1 100%", wordBreak: "break-all" },
                },
                "tested with " + r.sample,
              ),
            );
          }
          function test(e) {
            busy(
              e.target.closest("button"),
              api("POST", "/api/library/proxy/test", { url: inp.value })
                .then(show)
                .catch(fail),
            );
          }
          proxyBox.replaceChildren(
            h("h3", null, icon("link"), "Download proxy"),
            h(
              "p",
              { class: "sub", style: { marginBottom: "10px" } },
              "Release downloads are sent to the proxy URL only if a HEAD request for that file through the proxy works; otherwise visitors get the plain GitHub URL. The check is cached for 2 minutes and a change here applies to every release immediately.",
            ),
            h(
              "div",
              { class: "row wrap" },
              inp,
              can("operator")
                ? h(
                    "button",
                    { class: "btn", onclick: test },
                    icon("refresh"),
                    "Test",
                  )
                : null,
              admin
                ? h(
                    "button",
                    {
                      class: "btn primary",
                      onclick: function (e) {
                        busy(
                          e.target.closest("button"),
                          api("PUT", "/api/library/proxy", { url: inp.value })
                            .then(function (v) {
                              show(v.report);
                              toast("Proxy saved", "ok");
                              load();
                            })
                            .catch(fail),
                        );
                      },
                    },
                    "Save",
                  )
                : null,
              admin && p.custom
                ? h(
                    "button",
                    {
                      class: "btn ghost",
                      title: "Back to " + p.default,
                      onclick: function () {
                        api("DELETE", "/api/library/proxy")
                          .then(function () {
                            toast("Reset to default", "ok");
                            loadProxy();
                            load();
                          })
                          .catch(fail);
                      },
                    },
                    "Reset",
                  )
                : null,
            ),
            res,
          );
          if (can("operator"))
            api("POST", "/api/library/proxy/test", { url: p.url })
              .then(show)
              .catch(function () {});
        })
        .catch(function (e) {
          proxyBox.replaceChildren(
            O.empty("alert", "Could not load proxy settings", e.message),
          );
        });
    }

    /* GitHub auto-sync card */
    var lastSyncAt = null,
      lastBusy = false;
    function loadSync() {
      api("GET", "/api/library/sync")
        .then(function (d) {
          var c = d.cfg,
            st = d.status,
            admin = can("admin"),
            f = {};
          if (
            lastSyncAt !== null &&
            ((st && st.at !== lastSyncAt) || (lastBusy && !d.busy))
          )
            load();
          lastSyncAt = st ? st.at : 0;
          lastBusy = d.busy;
          var state =
            d.busy || d.importing
              ? chip(
                  "running",
                  d.busy ? "Syncing…" : "Importing " + d.importing,
                )
              : !st
                ? chip("queued", c.enabled ? "Waiting for first sync" : "Off")
                : st.ok
                  ? chip("success", "Up to date")
                  : chip("failed", "Needs attention");
          var en = h("input", {
              type: "checkbox",
              checked: !!c.enabled,
              disabled: !admin,
            }),
            pre = h("input", {
              type: "checkbox",
              checked: !!c.prereleases,
              disabled: !admin,
            });
          var iv = h("input", {
            type: "number",
            min: 2,
            max: 1440,
            value: c.interval_min,
            disabled: !admin,
            style: { width: "90px" },
          });
          var app = h(
            "select",
            { disabled: !admin, style: { width: "auto" } },
            h("option", { value: "" }, "First app"),
            (data ? data.apps : []).map(function (a) {
              return h(
                "option",
                { value: a.slug, selected: a.slug === c.app },
                a.name,
              );
            }),
          );
          var errs =
            st && st.errors && st.errors.length
              ? h(
                  "details",
                  { style: { marginTop: "8px" } },
                  h(
                    "summary",
                    {
                      class: "sub",
                      style: { cursor: "pointer", color: "var(--red)" },
                    },
                    st.errors.length +
                      " problem" +
                      (st.errors.length > 1 ? "s" : "") +
                      " in the last sync",
                  ),
                  h(
                    "div",
                    {
                      class: "sub mono",
                      style: {
                        marginTop: "6px",
                        display: "flex",
                        flexDirection: "column",
                        gap: "4px",
                      },
                    },
                    st.errors.slice(0, 8).map(function (e) {
                      return h("div", null, e);
                    }),
                  ),
                )
              : null;
          syncBox.replaceChildren(
            h(
              "h3",
              null,
              icon("refresh"),
              "GitHub sync",
              h("span", { class: "sp" }),
              state,
            ),
            h(
              "p",
              { class: "sub", style: { marginBottom: "10px" } },
              "Every release of " +
                S.info.repos.app +
                " (all versions, notes and file links) is copied automatically. Newest version becomes “Latest”, new releases appear within the interval, and releases removed on GitHub disappear too. Nothing is downloaded to this server.",
            ),
            h(
              "div",
              { class: "row wrap", style: { gap: "18px" } },
              h(
                "label",
                { class: "check" },
                h("span", { class: "switch" }, en, h("i")),
                "Auto-sync",
              ),
              h("label", { class: "check" }, "every", iv, "min"),
              h("label", { class: "check" }, "into", app),
              h(
                "label",
                { class: "check" },
                h("span", { class: "switch" }, pre, h("i")),
                "Include pre-releases (as beta)",
              ),
              h("span", { class: "sp" }),
              admin
                ? h(
                    "button",
                    {
                      class: "btn",
                      onclick: function (e) {
                        busy(
                          e.target.closest("button"),
                          api("PUT", "/api/library/sync", {
                            enabled: en.checked,
                            interval_min: +iv.value || 10,
                            app: app.value,
                            prereleases: pre.checked,
                          })
                            .then(function () {
                              toast("Sync settings saved", "ok");
                              loadSync();
                            })
                            .catch(fail),
                        );
                      },
                    },
                    "Save",
                  )
                : null,
              can("operator")
                ? h(
                    "button",
                    {
                      class: "btn primary",
                      disabled: d.busy,
                      onclick: function (e) {
                        busy(
                          e.target.closest("button"),
                          api("POST", "/api/library/sync/run")
                            .then(function () {
                              toast("Sync started", "ok");
                              setTimeout(loadSync, 800);
                            })
                            .catch(fail),
                        );
                      },
                    },
                    icon("refresh"),
                    "Sync now",
                  )
                : null,
            ),
            h(
              "div",
              { class: "sub", style: { marginTop: "10px" } },
              st
                ? "Last sync " +
                    F.ago(st.finished || st.at) +
                    ": " +
                    st.checked +
                    " builds checked, " +
                    st.new_releases +
                    " new release" +
                    (st.new_releases === 1 ? "" : "s") +
                    ", " +
                    st.new_files +
                    " file link" +
                    (st.new_files === 1 ? "" : "s") +
                    " added" +
                    (st.removed ? ", " + st.removed + " removed" : "") +
                    "."
                : "No sync has run yet.",
            ),
            errs,
            d.forgotten
              ? h(
                  "div",
                  { class: "sub row wrap", style: { marginTop: "8px" } },
                  d.forgotten +
                    " release" +
                    (d.forgotten > 1
                      ? "s you deleted are"
                      : " you deleted is") +
                    " not re-imported.",
                  admin
                    ? h(
                        "button",
                        {
                          class: "btn sm ghost",
                          onclick: function () {
                            api("POST", "/api/library/sync/forget-deleted")
                              .then(function () {
                                toast("They will sync again", "ok");
                                loadSync();
                              })
                              .catch(fail);
                          },
                        },
                        "Allow them again",
                      )
                    : null,
                )
              : null,
          );
        })
        .catch(function (e) {
          syncBox.replaceChildren(
            O.empty("alert", "Could not load sync status", e.message),
          );
        });
    }
    function relCard(r) {
      var plat = h(
        "span",
        { class: "chip plain" },
        icon(r.platform === "ios" ? "apple" : "android"),
        r.platform,
      );
      return h(
        "div",
        {
          class: "relcard" + (!r.visible ? " hidden" : ""),
          style: { padding: "12px 0", borderBottom: "1px solid var(--line)" },
        },
        h(
          "div",
          { style: { flex: 1, minWidth: "220px" } },
          h(
            "div",
            { class: "row wrap" },
            h("span", { class: "v" }, "v" + r.version),
            plat,
            r.channel === "beta" ? chip("warn", "beta") : null,
            r.latest ? chip("ok", "latest") : null,
            r.status !== "ready"
              ? chip(r.status === "failed" ? "failed" : "running", r.status)
              : null,
            !r.visible ? chip("skipped", "hidden") : null,
          ),
          h(
            "div",
            { class: "sub" },
            F.dt(r.published_at || r.created_at) +
              " · " +
              r.created_by +
              " · via " +
              r.source +
              " · " +
              r.downloads +
              " downloads",
          ),
          r.error
            ? h(
                "div",
                { class: "sub", style: { color: "var(--red)" } },
                r.error,
              )
            : null,
          r.notes
            ? h(
                "div",
                {
                  class: "sub",
                  style: { marginTop: "4px", whiteSpace: "pre-wrap" },
                },
                r.notes.slice(0, 200),
              )
            : null,
          h(
            "div",
            { class: "files" },
            r.assets.map(function (a) {
              return h(
                "div",
                { class: "file" },
                icon("file"),
                h("span", { class: "n" }, a.filename),
                h("span", { class: "sub" }, F.bytes(a.size)),
                h("span", { class: "sp" }),
                a.remote ? h("span", { class: "chip info" }, "linked") : null,
                h(
                  "code",
                  { class: "inline", title: "SHA-256 " + a.sha256 },
                  a.sha256 ? a.sha256.slice(0, 8) : "no hash",
                ),
                a.source_url
                  ? h(
                      "button",
                      {
                        class: "btn sm ghost",
                        title:
                          "Copy the current download URL (gh-proxy, or direct GitHub if the proxy is down)",
                        onclick: function () {
                          navigator.clipboard
                            .writeText(a.source_url)
                            .then(function () {
                              toast("URL copied", "ok");
                            });
                        },
                      },
                      "Copy URL",
                    )
                  : null,
                h(
                  "a",
                  {
                    class: "btn sm ghost icon",
                    href: a.url,
                    "aria-label": "Download",
                  },
                  icon("download"),
                ),
              );
            }),
          ),
        ),
        can("operator")
          ? h(
              "div",
              { class: "row" },
              h(
                "button",
                {
                  class: "btn sm",
                  onclick: function () {
                    api("PATCH", "/api/library/releases/" + r.id, {
                      visible: !r.visible,
                    })
                      .then(function () {
                        toast(
                          r.visible
                            ? "Hidden from the public page"
                            : "Now public",
                          "ok",
                        );
                        load();
                      })
                      .catch(fail);
                  },
                },
                icon(r.visible ? "eyeoff" : "eye"),
                r.visible ? "Hide" : "Show",
              ),
              h(
                "button",
                {
                  class: "btn sm",
                  onclick: function () {
                    editNotes(r);
                  },
                },
                "Notes",
              ),
              can("admin")
                ? h(
                    "button",
                    {
                      class: "btn sm danger",
                      "aria-label": "Delete release",
                      onclick: function () {
                        O.confirm(
                          "Delete v" + r.version + " (" + r.platform + ")?",
                          "The files are removed from the server and the public download links stop working.",
                          "Delete",
                          true,
                        ).then(function (y) {
                          if (y)
                            api("DELETE", "/api/library/releases/" + r.id)
                              .then(function () {
                                toast("Deleted", "ok");
                                load();
                              })
                              .catch(fail);
                        });
                      },
                    },
                    icon("trash"),
                  )
                : null,
            )
          : null,
      );
    }
    function editNotes(r) {
      var ta = h("textarea", { rows: 8, value: r.notes }),
        ch = h(
          "select",
          null,
          ["stable", "beta"].map(function (c) {
            return h("option", { value: c, selected: r.channel === c }, c);
          }),
        );
      modal(
        "Release notes · v" + r.version,
        h(
          "div",
          { class: "col" },
          h(
            "div",
            { class: "field" },
            h("label", null, "Notes (shown on the public page)"),
            ta,
          ),
          h("div", { class: "field" }, h("label", null, "Channel"), ch),
        ),
        function (close) {
          var go = h("button", { class: "btn primary" }, "Save");
          go.onclick = function () {
            busy(
              go,
              api("PATCH", "/api/library/releases/" + r.id, {
                notes: ta.value,
                channel: ch.value,
              })
                .then(function () {
                  close();
                  load();
                })
                .catch(fail),
            );
          };
          return [h("button", { class: "btn", onclick: close }, "Cancel"), go];
        },
      );
    }
    function newApp() {
      var name = h("input", { type: "text", placeholder: "My App" }),
        slug = h("input", { type: "text", placeholder: "my-app (optional)" }),
        desc = h("input", { type: "text", placeholder: "Short description" });
      modal(
        "Add an app",
        h(
          "div",
          { class: "col" },
          h("div", { class: "field" }, h("label", null, "Name"), name),
          h("div", { class: "field" }, h("label", null, "URL slug"), slug),
          h("div", { class: "field" }, h("label", null, "Description"), desc),
        ),
        function (close) {
          var go = h("button", { class: "btn primary" }, "Create");
          go.onclick = function () {
            busy(
              go,
              api("POST", "/api/library/apps", {
                name: name.value,
                slug: slug.value,
                description: desc.value,
              })
                .then(function () {
                  close();
                  load();
                })
                .catch(fail),
            );
          };
          return [h("button", { class: "btn", onclick: close }, "Cancel"), go];
        },
      );
    }
    function appSelect(pre) {
      return h(
        "select",
        null,
        (data ? data.apps : []).map(function (a) {
          return h(
            "option",
            { value: a.slug, selected: a.slug === pre },
            a.name,
          );
        }),
      );
    }
    function uploadDialog(pre) {
      var app = appSelect(pre),
        ver = h("input", {
          type: "text",
          placeholder: "1.4.2",
          required: true,
        }),
        plat = h(
          "select",
          null,
          ["android", "ios", "other"].map(function (p) {
            return h("option", { value: p }, p);
          }),
        ),
        ch = h(
          "select",
          null,
          h("option", { value: "stable" }, "stable"),
          h("option", { value: "beta" }, "beta"),
        );
      var notes = h("textarea", { rows: 4, placeholder: "What's new?" }),
        file = h("input", {
          type: "file",
          multiple: true,
          accept: ".apk,.aab,.ipa,.zip,.dmg,.exe,.msi,.deb,.appimage",
        }),
        prog = h("div", { class: "progress", hidden: true }, h("i")),
        msg = h("div", { class: "sub" });
      file.onchange = function () {
        var f = file.files[0];
        if (!f) return;
        if (/\.ipa$/i.test(f.name)) plat.value = "ios";
        else if (/\.(apk|aab)$/i.test(f.name)) plat.value = "android";
        var m = /(\d+\.\d+\.\d+(?:[+-][\w.]+)?)/.exec(f.name);
        if (m && !ver.value) ver.value = m[1];
      };
      modal(
        "Upload a build",
        h(
          "div",
          { class: "col" },
          h(
            "div",
            { class: "grid g2" },
            h("div", { class: "field" }, h("label", null, "App"), app),
            h("div", { class: "field" }, h("label", null, "Version *"), ver),
          ),
          h(
            "div",
            { class: "grid g2" },
            h("div", { class: "field" }, h("label", null, "Platform"), plat),
            h("div", { class: "field" }, h("label", null, "Channel"), ch),
          ),
          h(
            "div",
            { class: "field" },
            h("label", null, "Files *"),
            file,
            h(
              "div",
              { class: "hint" },
              ".apk .aab .ipa .zip and a few desktop formats",
            ),
          ),
          h(
            "div",
            { class: "field" },
            h("label", null, "Release notes"),
            notes,
          ),
          prog,
          msg,
        ),
        function (close) {
          var go = h("button", { class: "btn primary" }, icon("up"), "Upload");
          go.onclick = function () {
            if (!file.files.length) return toast("Choose a file", "err");
            if (!ver.value.trim()) return toast("Enter a version", "err");
            var fd = new FormData();
            fd.append("app", app.value);
            fd.append("version", ver.value);
            fd.append("platform", plat.value);
            fd.append("channel", ch.value);
            fd.append("notes", notes.value);
            Array.prototype.forEach.call(file.files, function (f) {
              fd.append("files", f);
            });
            go.classList.add("busy");
            go.disabled = true;
            prog.hidden = false;
            var x = new XMLHttpRequest();
            x.open("POST", BASE + "/api/library/upload");
            x.setRequestHeader("X-Requested-With", "ops");
            x.upload.onprogress = function (e) {
              if (e.lengthComputable) {
                prog.firstChild.style.width = (e.loaded / e.total) * 100 + "%";
                msg.textContent = F.bytes(e.loaded) + " of " + F.bytes(e.total);
              }
            };
            x.onload = function () {
              var d = {};
              try {
                d = JSON.parse(x.responseText);
              } catch (e) {}
              if (x.status < 300) {
                close();
                toast("Uploaded", "ok");
                load();
              } else {
                go.classList.remove("busy");
                go.disabled = false;
                prog.hidden = true;
                fail(new Error(d.detail || "Upload failed (" + x.status + ")"));
              }
            };
            x.onerror = function () {
              go.classList.remove("busy");
              go.disabled = false;
              fail(new Error("Network error during upload"));
            };
            x.send(fd);
          };
          return [h("button", { class: "btn", onclick: close }, "Cancel"), go];
        },
      );
    }
    function importDialog() {
      var list = h("div", { class: "col", style: { gap: "8px" } }, O.skel(2)),
        app = appSelect();
      modal(
        "Import from GitHub Releases",
        h(
          "div",
          { class: "col" },
          h("div", { class: "field" }, h("label", null, "Publish into"), app),
          list,
        ),
        null,
        true,
      );
      api("GET", "/api/library/github-releases")
        .then(function (rs) {
          list.replaceChildren.apply(
            list,
            rs.length
              ? rs.map(function (r) {
                  return h(
                    "div",
                    { class: "file", style: { flexWrap: "wrap" } },
                    h(
                      "div",
                      { style: { flex: 1, minWidth: "180px" } },
                      h(
                        "div",
                        { class: "n" },
                        r.name + (r.prerelease ? " (pre-release)" : ""),
                      ),
                      h(
                        "div",
                        { class: "sub" },
                        r.tag +
                          " · " +
                          r.assets.length +
                          " file(s) · " +
                          F.ago(r.published_at),
                      ),
                    ),
                    r.imported
                      ? chip("success", "imported")
                      : r.assets.length
                        ? h(
                            "button",
                            {
                              class: "btn sm primary",
                              onclick: function (e) {
                                busy(
                                  e.target.closest("button"),
                                  api("POST", "/api/library/import-release", {
                                    tag: r.tag,
                                    app: app.value,
                                  })
                                    .then(function () {
                                      toast("Importing " + r.tag, "ok");
                                      e.target.closest(".overlay").remove();
                                      load();
                                      setTimeout(load, 4000);
                                    })
                                    .catch(fail),
                                );
                              },
                            },
                            "Import",
                          )
                        : chip("skipped", "no builds"),
                  );
                })
              : [
                  O.empty(
                    "box",
                    "No GitHub releases",
                    "Create one in the app repo or publish from a workflow artifact on the run page.",
                  ),
                ],
          );
        })
        .catch(function (e) {
          list.replaceChildren(O.empty("alert", "Could not load", e.message));
        });
    }
    load();
    loadSync();
    loadProxy();
    O.poll(function () {
      if (
        data &&
        data.releases.some(function (r) {
          return r.status === "importing";
        })
      )
        load();
    }, 3000);
    O.poll(loadSync, 5000);
  };

  /* ---------- Settings ---------- */
  var TABS = [
    ["github", "GitHub"],
    ["deploy", "Deploy"],
    ["server", "Server"],
    ["webhook", "Webhook"],
    ["email", "Email"],
    ["users", "Users"],
    ["account", "Account"],
    ["audit", "Audit log"],
  ];
  O.pages.settings = function (page, seg, ctx) {
    var tab = TABS.some(function (t) {
      return t[0] === seg[0];
    })
      ? seg[0]
      : "github";
    ctx.setTitle([
      "Settings",
      TABS.filter(function (t) {
        return t[0] === tab;
      })[0][1],
    ]);
    var body = h("div", { class: "col" });
    page.append(
      h(
        "div",
        { class: "page-head" },
        h("div", null, h("h2", null, "Settings")),
        h("span", { class: "sp" }),
        h(
          "div",
          { class: "tabs" },
          TABS.map(function (t) {
            return h(
              "button",
              {
                class: tab === t[0] ? "on" : "",
                onclick: function () {
                  O.go("settings/" + t[0]);
                },
              },
              t[1],
            );
          }),
        ),
      ),
      body,
    );
    ({
      github: tGithub,
      deploy: tDeploy,
      server: tServer,
      webhook: tWebhook,
      email: tEmail,
      users: tUsers,
      account: tAccount,
      audit: tAudit,
    })[tab](body);
  };

  function tGithub(body) {
    var out = h("div", { class: "card" }, O.skel(4));
    var tok = h("input", {
        type: "password",
        placeholder: "github_pat_… or ghp_…",
        autocomplete: "off",
        spellcheck: false,
      }),
      org = h("input", { type: "text" }),
      ra = h("input", { type: "text" }),
      rp = h("input", { type: "text" });
    org.value = S.info.org;
    ra.value = S.info.repos.app;
    rp.value = S.info.repos.panel;
    function paint(v) {
      var i = v.info || {};
      out.replaceChildren(
        h(
          "h3",
          null,
          icon("shield"),
          "Token check",
          h("span", { class: "sp" }),
          h(
            "button",
            {
              class: "btn sm",
              onclick: function (e) {
                busy(
                  e.target.closest("button"),
                  api("GET", "/api/github/verify").then(paint).catch(fail),
                );
              },
            },
            icon("refresh"),
            "Re-check",
          ),
        ),
        i.login
          ? h(
              "div",
              { class: "row", style: { marginBottom: "12px" } },
              O.avatar(i.login, i.avatar),
              h(
                "div",
                null,
                h("b", null, "@" + i.login),
                h(
                  "div",
                  { class: "sub" },
                  (i.kind || "") +
                    " token " +
                    (i.fingerprint || "") +
                    (i.expires ? " · expires " + i.expires : "") +
                    (i.scopes
                      ? " · scopes: " + (i.scopes.join(", ") || "none")
                      : ""),
                ),
              ),
              h("span", { class: "sp" }),
              chip(
                v.ok ? "success" : "failed",
                v.ok ? "All checks passed" : "Needs attention",
              ),
            )
          : null,
        v.checks.map(function (c) {
          return h(
            "div",
            { class: "check-row " + (c.ok ? "ok" : "bad") },
            h("span", { class: "ic" }, icon(c.ok ? "check" : "alert")),
            h(
              "div",
              null,
              h("b", null, c.name),
              h("div", { class: "sub" }, c.detail),
            ),
          );
        }),
      );
    }
    body.append(
      out,
      h(
        "div",
        { class: "card col" },
        h("h3", null, icon("key"), "Connection"),
        h(
          "div",
          { class: "field" },
          h("label", null, "Personal access token"),
          tok,
          h(
            "div",
            { class: "hint" },
            "Stored on the server, never shown again. Fine-grained token with: Contents (read), Actions (read and write), Metadata (read), on both repositories. Leave empty to keep the current token.",
          ),
        ),
        h(
          "div",
          { class: "grid g3" },
          h("div", { class: "field" }, h("label", null, "Organisation"), org),
          h("div", { class: "field" }, h("label", null, "App repo"), ra),
          h("div", { class: "field" }, h("label", null, "Panel repo"), rp),
        ),
        can("admin")
          ? h(
              "div",
              { class: "row" },
              h(
                "button",
                {
                  class: "btn primary",
                  onclick: function (e) {
                    busy(
                      e.target.closest("button"),
                      api("PUT", "/api/github/settings", {
                        token: tok.value,
                        org: org.value,
                        repo_app: ra.value,
                        repo_panel: rp.value,
                      })
                        .then(function (v) {
                          tok.value = "";
                          paint(v);
                          toast(
                            v.ok
                              ? "Saved and verified"
                              : "Saved, but some checks failed",
                            v.ok ? "ok" : "err",
                          );
                          S.info.org = org.value;
                          S.info.repos = { app: ra.value, panel: rp.value };
                        })
                        .catch(fail),
                    );
                  },
                },
                "Save and verify",
              ),
              h(
                "button",
                {
                  class: "btn danger",
                  onclick: function () {
                    O.confirm(
                      "Remove the saved token?",
                      "Workflows, runs and deployments stop working until a new token is added (an OPS_GITHUB_TOKEN env var is used as fallback).",
                      "Remove",
                      true,
                    ).then(function (y) {
                      if (y)
                        api("DELETE", "/api/github/token")
                          .then(function () {
                            toast("Token removed", "ok");
                            return api("GET", "/api/github/verify").then(paint);
                          })
                          .catch(fail);
                    });
                  },
                },
                "Remove token",
              ),
            )
          : h("div", { class: "sub" }, "Only admins can change these."),
      ),
    );
    api("GET", "/api/github/verify")
      .then(paint)
      .catch(function (e) {
        out.replaceChildren(O.empty("alert", "Check failed", e.message));
      });
  }

  function tDeploy(body) {
    var box = h("div", { class: "card col" }, O.skel(5));
    body.append(box);
    api("GET", "/api/deploy/status")
      .then(function (d) {
        var c = d.cfg,
          f = {};
        function txt(k, label, hint, mono) {
          var el = h("input", { type: "text", value: String(c[k]) });
          if (mono !== false) el.style.fontFamily = "var(--mono)";
          f[k] = el;
          return h(
            "div",
            { class: "field" },
            h("label", null, label),
            el,
            hint ? h("div", { class: "hint" }, hint) : null,
          );
        }
        function num(k, label, hint) {
          var el = h("input", { type: "number", value: c[k] });
          f[k] = el;
          return h(
            "div",
            { class: "field" },
            h("label", null, label),
            el,
            hint ? h("div", { class: "hint" }, hint) : null,
          );
        }
        function sw(k, label, hint) {
          var el = h("input", { type: "checkbox", checked: !!c[k] });
          f[k] = el;
          return h(
            "label",
            { class: "check" },
            h("span", { class: "switch" }, el, h("i")),
            h(
              "span",
              null,
              h("b", null, label),
              h("div", { class: "sub" }, hint),
            ),
          );
        }
        box.replaceChildren(
          h("h3", null, icon("rocket"), "Deployment target · " + d.repo),
          h(
            "div",
            { class: "grid g2" },
            txt(
              "branch",
              "Branch to deploy",
              "Pushes to this branch trigger auto-deploys.",
              false,
            ),
            txt(
              "deploy_root",
              "Deploy root",
              "Contains releases/, shared/ and the current symlink.",
            ),
          ),
          h(
            "div",
            { class: "grid g2" },
            txt(
              "env_target",
              "Write .env to",
              "Relative to the release folder.",
              false,
            ),
            txt(
              "python",
              "Python executable",
              "Used to create the virtualenv.",
            ),
          ),
          txt(
            "install_cmd",
            "Install command",
            "Runs inside the virtualenv. Skipped when requirements.txt is unchanged.",
          ),
          sw(
            "test_enabled",
            "Run tests before switching",
            "A failing test leaves the live version untouched.",
          ),
          txt(
            "test_cmd",
            "Test command",
            "Needs the test tool in requirements.txt.",
          ),
          txt(
            "pre_switch_cmd",
            "Pre-switch command (optional)",
            "e.g. database migrations, runs in the new release before it goes live.",
          ),
          txt(
            "restart_cmd",
            "Restart command",
            "Needs permission: see README (sudoers).",
          ),
          txt(
            "health_url",
            "Health check URL",
            "Must return 2xx after restart, otherwise the previous release is restored.",
          ),
          h(
            "div",
            { class: "grid g3" },
            num("health_timeout", "Health timeout (s)"),
            num("keep_releases", "Releases to keep"),
            num("command_timeout", "Command timeout (s)"),
          ),
          sw(
            "require_env",
            "Block deployments until the environment is complete",
            "Nothing is deployed while the Environment is empty or a required key has no value.",
          ),
          txt(
            "required_env",
            "Required environment keys",
            "Comma separated, e.g. SECRET_KEY, MAIL_KEY. Keys in your repo's .env.example without a value are required automatically.",
          ),
          h(
            "div",
            { class: "field" },
            h("label", null, "Server for auto deploy and webhooks"),
            (f.auto_target = h(
              "select",
              null,
              d.targets.map(function (t) {
                return h(
                  "option",
                  { value: t.id, selected: t.id === c.auto_target },
                  t.name,
                );
              }),
            )),
            h(
              "div",
              { class: "hint" },
              "Each server can override the restart command and health URL on the Servers page.",
            ),
          ),
          sw(
            "auto_deploy",
            "Auto-deploy new commits by polling",
            "Checks GitHub every few seconds below. Works without any webhook.",
          ),
          num("poll_seconds", "Poll interval (s)"),
          sw(
            "webhook_enabled",
            "Accept GitHub webhook pushes",
            "Instant deploys on push; see the Webhook tab.",
          ),
          can("admin")
            ? h(
                "div",
                { class: "row" },
                h(
                  "button",
                  {
                    class: "btn primary",
                    onclick: function (e) {
                      var cfg = {};
                      for (var k in f)
                        cfg[k] =
                          f[k].type === "checkbox" ? f[k].checked : f[k].value;
                      busy(
                        e.target.closest("button"),
                        api("PUT", "/api/deploy/config", { cfg: cfg })
                          .then(function () {
                            toast("Settings saved", "ok");
                          })
                          .catch(fail),
                      );
                    },
                  },
                  "Save",
                ),
              )
            : h("div", { class: "sub" }, "Only admins can change these."),
        );
      })
      .catch(function (e) {
        box.replaceChildren(O.empty("alert", "Could not load", e.message));
      });
  }

  function tServer(body) {
    if (!can("admin"))
      return body.append(
        O.empty(
          "shield",
          "Admins only",
          "Ask an admin to view the server setup.",
        ),
      );
    var out = h("div", { class: "col" }),
      f = {};
    function inp(k, label, val, hint, type) {
      var el = h("input", {
        type: type || "text",
        value: val,
        autocomplete: "off",
        spellcheck: false,
      });
      el.style.fontFamily = "var(--mono)";
      f[k] = el;
      return h(
        "div",
        { class: "field" },
        h("label", null, label),
        el,
        hint ? h("div", { class: "hint" }, hint) : null,
      );
    }
    var form = h(
      "div",
      { class: "card col" },
      h("h3", null, icon("server"), "Generate server configuration"),
      h(
        "p",
        { class: "sub" },
        "Systemd services, the sudo rule and the nginx site are generated from your domain and these values. The installer (scripts/install.sh) uses exactly the same generator, so what you see here is what it writes.",
      ),
      inp(
        "panel_cmd",
        "Command that starts your API + admin panel",
        "",
        "Leave empty for: <deploy root>/current/.venv/bin/uvicorn main:app --host 127.0.0.1 --port <port> --proxy-headers",
      ),
      h(
        "div",
        { class: "grid g3" },
        inp("panel_port", "API / admin port", "8000", "", "number"),
        inp(
          "service",
          "Service name",
          "nazar-panel",
          "Used in the restart command.",
        ),
        inp("linux_user", "Linux user", "nazar"),
      ),
      h(
        "div",
        { class: "grid g2" },
        inp("ops_dir", "Ops panel folder", "/opt/nazar-ops"),
        inp("ops_port", "Ops panel port", "8100", "", "number"),
      ),
      h(
        "div",
        { class: "row" },
        h(
          "button",
          {
            class: "btn primary",
            onclick: function (e) {
              busy(e.target.closest("button"), gen());
            },
          },
          icon("refresh"),
          "Generate",
        ),
      ),
    );
    body.append(form, out);
    function code(text) {
      return h("pre", { class: "code", style: { maxHeight: "340px" } }, text);
    }
    function gen() {
      var q = new URLSearchParams();
      for (var k in f) if (f[k].value !== "") q.set(k, f[k].value);
      return api("GET", "/api/server/configs?" + q.toString())
        .then(function (d) {
          var host = d.ops_host;
          out.replaceChildren(
            d.placeholder_domain
              ? h(
                  "div",
                  { class: "chip warn", style: { whiteSpace: "normal" } },
                  icon("info"),
                  "OPS_ROOT_DOMAIN is not set, so example.com is shown. Set it in .env to see your real domain.",
                )
              : null,
            h(
              "div",
              { class: "card col" },
              h("h3", null, icon("rocket"), "One command"),
              h(
                "p",
                { class: "sub" },
                "On a fresh server this does everything below, including HTTPS:",
              ),
              h(
                "div",
                { class: "copyline" },
                h("input", {
                  type: "text",
                  readOnly: true,
                  value:
                    "sudo scripts/install.sh --domain " +
                    d.domain.replace("localhost", "example.com") +
                    " --email you@example.com --ssl",
                }),
                h(
                  "button",
                  {
                    class: "btn",
                    onclick: function (e) {
                      O.copy(
                        e.target.closest(".copyline").querySelector("input")
                          .value,
                      );
                    },
                  },
                  icon("copy"),
                  "Copy",
                ),
              ),
              h(
                "div",
                { class: "sub" },
                "Then set Deploy > Restart command to: ",
                h("code", { class: "inline" }, d.restart_cmd),
              ),
            ),
            d.files.map(function (fl) {
              return h(
                "div",
                { class: "card col" },
                h(
                  "h3",
                  null,
                  icon("file"),
                  fl.name,
                  h("span", { class: "sp" }),
                  h(
                    "button",
                    {
                      class: "btn sm",
                      onclick: function () {
                        O.copy(fl.content, fl.name + " copied");
                      },
                    },
                    icon("copy"),
                    "Copy",
                  ),
                ),
                h("div", { class: "sub" }, fl.description),
                h(
                  "div",
                  { class: "row wrap" },
                  h("code", { class: "inline" }, fl.path),
                  h("span", { class: "chip plain mono" }, "mode " + fl.mode),
                ),
                code(fl.content),
              );
            }),
            h(
              "div",
              { class: "card col" },
              h(
                "h3",
                null,
                icon("list"),
                "Manual steps (if you do not use the installer)",
              ),
              code(d.commands.join("\n")),
            ),
          );
        })
        .catch(fail);
    }
    gen();
  }
  function tWebhook(body) {
    var box = h("div", { class: "card col" }, O.skel(3));
    body.append(box);
    function paint(d, secret) {
      var url = d.webhook.url,
        copyRow = function (v) {
          var i = h("input", { type: "text", readOnly: true, value: v });
          return h(
            "div",
            { class: "copyline" },
            i,
            h(
              "button",
              {
                class: "btn",
                onclick: function () {
                  O.copy(v);
                },
              },
              icon("copy"),
              "Copy",
            ),
          );
        };
      box.replaceChildren(
        h("h3", null, icon("link"), "GitHub webhook (instant deploys)"),
        h(
          "p",
          { class: "sub" },
          "Optional. Polling already works without this. A webhook just makes deployments start the second you push.",
        ),
        h(
          "div",
          { class: "field" },
          h("label", null, "Payload URL"),
          copyRow(url),
        ),
        h(
          "div",
          { class: "field" },
          h("label", null, "Secret"),
          secret
            ? copyRow(secret)
            : h(
                "div",
                { class: "sub" },
                d.webhook.has_secret
                  ? "A secret is set. Generate a new one to see it again (the old one stops working)."
                  : "No secret yet.",
              ),
        ),
        h(
          "ol",
          { class: "sub", style: { paddingLeft: "18px", lineHeight: 1.8 } },
          h(
            "li",
            null,
            "Open the panel repo on GitHub → Settings → Webhooks → Add webhook.",
          ),
          h(
            "li",
            null,
            "Paste the URL above, content type application/json, paste the secret.",
          ),
          h(
            "li",
            null,
            "Choose “Just the push event” and save. GitHub sends a ping and the status turns green.",
          ),
        ),
        can("admin")
          ? h(
              "div",
              { class: "row" },
              h(
                "button",
                {
                  class: "btn primary",
                  onclick: function (e) {
                    busy(
                      e.target.closest("button"),
                      api("POST", "/api/deploy/webhook-secret")
                        .then(function (r) {
                          d.webhook.has_secret = true;
                          paint(d, r.secret);
                        })
                        .catch(fail),
                    );
                  },
                },
                icon("refresh"),
                d.webhook.has_secret ? "Rotate secret" : "Generate secret",
              ),
            )
          : null,
      );
    }
    api("GET", "/api/deploy/status")
      .then(function (d) {
        paint(d);
      })
      .catch(function (e) {
        box.replaceChildren(O.empty("alert", "Could not load", e.message));
      });
  }

  /* ----- Email (SMTP) ----- */
  function tEmail(body) {
    if (!can("admin"))
      return body.append(
        O.empty(
          "shield",
          "Admins only",
          "Ask an admin to manage email settings.",
        ),
      );
    var box = h("div", { class: "col" }, O.skel(4));
    body.append(box);
    var DEFPORT = { starttls: 587, ssl: 465, none: 25 };
    function paint(c) {
      var f = {};
      function inp(k, label, val, hint, type, ph) {
        var el = h("input", {
          type: type || "text",
          value: val == null ? "" : String(val),
          autocomplete: "off",
          spellcheck: false,
          placeholder: ph || "",
        });
        f[k] = el;
        return h(
          "div",
          { class: "field" },
          h("label", null, label),
          el,
          hint ? h("div", { class: "hint" }, hint) : null,
        );
      }
      var sec = h(
        "select",
        null,
        [
          ["starttls", "STARTTLS (port 587)"],
          ["ssl", "SSL / TLS (port 465)"],
          ["none", "None (port 25, local relay or dev only)"],
        ].map(function (o) {
          return h(
            "option",
            { value: o[0], selected: o[0] === c.security },
            o[1],
          );
        }),
      );
      var port = h("input", {
        type: "number",
        value: c.port,
        min: 1,
        max: 65535,
      });
      sec.addEventListener("change", function () {
        var was = Object.keys(DEFPORT).some(function (k) {
          return String(DEFPORT[k]) === port.value;
        });
        if (was) port.value = DEFPORT[sec.value];
      });
      var to = h("input", {
        type: "email",
        value: S.me.email,
        placeholder: "Send the test to…",
      });
      var state = c.configured
        ? h(
            "span",
            { class: "chip success" },
            h("i", { class: "d" }),
            "Email is on" + (c.source === "env" ? " (from .env)" : ""),
          )
        : h(
            "span",
            { class: "chip skipped" },
            h("i", { class: "d" }),
            "Email is off",
          );
      box.replaceChildren(
        h(
          "div",
          { class: "card col" },
          h(
            "h3",
            null,
            icon("mail"),
            "Email (SMTP)",
            h("span", { class: "sp" }),
            state,
          ),
          h(
            "p",
            { class: "sub" },
            c.configured
              ? "Invitations are emailed and “Forgot password?” is available on the sign-in page."
              : "Email is optional. While it is off, inviting someone shows a link you can copy and send yourself, and “Forgot password?” is hidden. That is fine for a dev setup.",
          ),
          h(
            "div",
            { class: "grid g2" },
            inp("host", "SMTP server", c.host, "", "text", "smtp.example.com"),
            h("div", { class: "field" }, h("label", null, "Port"), port),
          ),
          h(
            "div",
            { class: "grid g2" },
            h(
              "div",
              { class: "field" },
              h("label", null, "Connection security"),
              sec,
            ),
            inp(
              "username",
              "Username",
              c.username,
              "Leave empty if the server needs no login.",
            ),
          ),
          h(
            "div",
            { class: "field" },
            h("label", null, "Password"),
            (f.password = h("input", {
              type: "password",
              autocomplete: "new-password",
              placeholder: c.password_set
                ? "•••••••• saved, leave empty to keep"
                : "SMTP password or app password",
            })),
            h(
              "div",
              { class: "hint" },
              "Stored on the server and never shown again.",
            ),
          ),
          h(
            "div",
            { class: "grid g2" },
            inp(
              "from_email",
              "From address",
              c.from_email,
              "",
              "email",
              "ops@example.com",
            ),
            inp("from_name", "From name", c.from_name),
          ),
          h(
            "div",
            { class: "row wrap" },
            h(
              "button",
              {
                class: "btn primary",
                onclick: function (e) {
                  busy(
                    e.target.closest("button"),
                    api("PUT", "/api/email", {
                      host: f.host.value,
                      port: Number(port.value) || 0,
                      security: sec.value,
                      username: f.username.value,
                      password:
                        f.password.value === "" ? null : f.password.value,
                      from_email: f.from_email.value,
                      from_name: f.from_name.value,
                    })
                      .then(function (r) {
                        S.info.smtp_enabled = r.configured;
                        toast("Email settings saved", "ok");
                        paint(r);
                      })
                      .catch(fail),
                  );
                },
              },
              "Save",
            ),
            c.source === "panel"
              ? h(
                  "button",
                  {
                    class: "btn danger",
                    onclick: function () {
                      O.confirm(
                        "Remove the saved email settings?",
                        "Invites will show a copyable link again and “Forgot password?” is hidden (unless OPS_SMTP_* is set in .env).",
                        "Remove",
                        true,
                      ).then(function (y) {
                        if (y)
                          api("DELETE", "/api/email")
                            .then(function (r) {
                              S.info.smtp_enabled = r.configured;
                              toast("Removed", "ok");
                              paint(r);
                            })
                            .catch(fail);
                      });
                    },
                  },
                  "Remove settings",
                )
              : null,
          ),
        ),
        c.configured
          ? h(
              "div",
              { class: "card col" },
              h("h3", null, icon("play"), "Send a test email"),
              h("div", { class: "field" }, h("label", null, "To"), to),
              h(
                "div",
                { class: "row" },
                h(
                  "button",
                  {
                    class: "btn",
                    onclick: function (e) {
                      busy(
                        e.target.closest("button"),
                        api("POST", "/api/email/test", { to: to.value })
                          .then(function (r) {
                            toast("Test email sent to " + r.to, "ok");
                          })
                          .catch(fail),
                      );
                    },
                  },
                  icon("mail"),
                  "Send test",
                ),
              ),
            )
          : null,
        h(
          "div",
          { class: "card col" },
          h("h3", null, icon("info"), "Typical settings"),
          h(
            "div",
            { class: "sub", style: { lineHeight: 1.8 } },
            "Gmail / Google Workspace: smtp.gmail.com, STARTTLS 587, an app password.",
            h("br"),
            "Amazon SES: email-smtp.<region>.amazonaws.com, STARTTLS 587, SMTP credentials.",
            h("br"),
            "Brevo, Mailgun, Postmark, Resend: use the SMTP details from their dashboard.",
            h("br"),
            "Local development: a catch-all inbox such as Mailpit or MailHog on port 1025 with security “None”.",
          ),
        ),
      );
    }
    api("GET", "/api/email")
      .then(paint)
      .catch(function (e) {
        box.replaceChildren(O.empty("alert", "Could not load", e.message));
      });
  }

  /* ----- Users + invitations ----- */
  function linkModal(title, r, what) {
    var inp = h("input", {
      type: "text",
      readOnly: true,
      value: r.link,
      "aria-label": "Link",
    });
    modal(
      title,
      h(
        "div",
        { class: "col" },
        r.sent
          ? h(
              "div",
              { class: "notice ok" },
              icon("mail"),
              "Emailed to " + (r.email || what) + ".",
            )
          : r.email_error
            ? h(
                "div",
                { class: "notice bad" },
                icon("alert"),
                "The email could not be sent: " +
                  r.email_error +
                  ". Share the link below instead.",
              )
            : h(
                "div",
                { class: "notice info" },
                icon("info"),
                "Email is not set up, so nothing was sent. Share this link yourself.",
              ),
        h(
          "div",
          { class: "field" },
          h("label", null, "Link"),
          h(
            "div",
            { class: "copyline" },
            inp,
            h(
              "button",
              {
                class: "btn",
                onclick: function () {
                  O.copy(r.link, "Link copied");
                },
              },
              icon("copy"),
              "Copy",
            ),
          ),
        ),
        h(
          "div",
          { class: "hint" },
          "It works once and is shown only now. Anyone with the link can use it, so send it privately.",
        ),
      ),
      function (close) {
        return [h("button", { class: "btn primary", onclick: close }, "Done")];
      },
    );
    setTimeout(function () {
      inp.select();
    }, 60);
  }
  function tUsers(body) {
    if (!can("admin"))
      return body.append(
        O.empty("shield", "Admins only", "Ask an admin to manage users."),
      );
    var pol = h("div", { class: "card" }, O.skel(1)),
      inv = h("div", { class: "card", hidden: true }),
      box = h("div", { class: "card" }, O.skel(3));
    body.append(pol, inv, box);
    var smtp = !!S.info.smtp_enabled;
    function loadPolicy() {
      api("GET", "/api/security")
        .then(function (c) {
          var sw = h("input", { type: "checkbox", checked: c.require_2fa });
          sw.addEventListener("change", function () {
            var want = sw.checked;
            O.confirm(
              want
                ? "Require two-step verification?"
                : "Stop requiring two-step verification?",
              want
                ? "Everyone without it, including you, must set it up at their next page load before using the panel."
                : "Users can turn it off again if they want to.",
              want ? "Require it" : "Stop requiring",
            ).then(function (y) {
              if (!y) {
                sw.checked = !want;
                return;
              }
              api("PUT", "/api/security", { require_2fa: want })
                .then(function () {
                  toast(
                    want ? "2FA is now required" : "2FA is optional again",
                    "ok",
                  );
                  if (want && !S.me.totp_enabled)
                    setTimeout(function () {
                      O.boot();
                    }, 600);
                })
                .catch(function (e) {
                  sw.checked = !want;
                  fail(e);
                });
            });
          });
          pol.replaceChildren(
            h(
              "label",
              { class: "check" },
              h("span", { class: "switch" }, sw, h("i")),
              h(
                "span",
                null,
                h("b", null, "Require two-step verification for everyone"),
                h(
                  "div",
                  { class: "sub" },
                  "People sign in with an authenticator app code as well as their password. Off by default.",
                ),
              ),
            ),
          );
        })
        .catch(function () {
          pol.hidden = true;
        });
    }
    function loadInvites() {
      api("GET", "/api/invites")
        .then(function (list) {
          inv.hidden = !list.length;
          if (!list.length) return;
          inv.replaceChildren(
            h("h3", null, icon("mail"), "Pending invitations"),
            h(
              "div",
              { class: "scroll-x" },
              h(
                "table",
                { class: "tbl" },
                h(
                  "thead",
                  null,
                  h(
                    "tr",
                    null,
                    ["Email", "Role", "Invited by", "Expires", ""].map(
                      function (t) {
                        return h("th", null, t);
                      },
                    ),
                  ),
                ),
                h(
                  "tbody",
                  null,
                  list.map(function (i) {
                    return h(
                      "tr",
                      null,
                      h(
                        "td",
                        null,
                        h("b", null, i.email),
                        i.emailed
                          ? h("span", { class: "sub" }, " · emailed")
                          : null,
                      ),
                      h("td", null, i.role),
                      h("td", { class: "sub" }, i.invited_by || ""),
                      h(
                        "td",
                        { class: "sub" },
                        i.expired
                          ? chip("failed", "expired")
                          : F.dt(i.expires_at),
                      ),
                      h(
                        "td",
                        null,
                        h(
                          "div",
                          { class: "row wrap" },
                          h(
                            "button",
                            {
                              class: "btn sm",
                              onclick: function (e) {
                                busy(
                                  e.target.closest("button"),
                                  api(
                                    "POST",
                                    "/api/invites/" + i.id + "/resend",
                                  )
                                    .then(function (r) {
                                      loadInvites();
                                      linkModal(
                                        "Invitation for " + i.email,
                                        r,
                                        i.email,
                                      );
                                    })
                                    .catch(fail),
                                );
                              },
                            },
                            smtp ? "Resend" : "New link",
                          ),
                          h(
                            "button",
                            {
                              class: "btn sm danger",
                              onclick: function () {
                                api("DELETE", "/api/invites/" + i.id)
                                  .then(function () {
                                    toast("Invitation revoked", "ok");
                                    loadInvites();
                                  })
                                  .catch(fail);
                              },
                            },
                            "Revoke",
                          ),
                        ),
                      ),
                    );
                  }),
                ),
              ),
            ),
          );
        })
        .catch(function () {});
    }
    function load() {
      api("GET", "/api/users")
        .then(function (us) {
          box.replaceChildren(
            h(
              "h3",
              { style: { flexWrap: "wrap" } },
              icon("users"),
              "Users",
              h("span", { class: "sp" }),
              h(
                "button",
                { class: "btn sm", onclick: addUser },
                icon("plus"),
                "Add with password",
              ),
              h(
                "button",
                { class: "btn primary sm", onclick: invite },
                icon("mail"),
                "Invite by email",
              ),
            ),
            h(
              "div",
              { class: "scroll-x" },
              h(
                "table",
                { class: "tbl" },
                h(
                  "thead",
                  null,
                  h(
                    "tr",
                    null,
                    ["User", "Role", "2FA", "Last sign-in", "Status", ""].map(
                      function (t) {
                        return h("th", null, t);
                      },
                    ),
                  ),
                ),
                h(
                  "tbody",
                  null,
                  us.map(function (u) {
                    var role = h(
                      "select",
                      {
                        style: { width: "auto" },
                        onchange: function () {
                          api("PATCH", "/api/users/" + u.id, {
                            role: role.value,
                          })
                            .then(function () {
                              toast("Role updated", "ok");
                            })
                            .catch(function (e) {
                              fail(e);
                              load();
                            });
                        },
                      },
                      ["admin", "operator", "viewer"].map(function (r) {
                        return h(
                          "option",
                          { value: r, selected: r === u.role },
                          r,
                        );
                      }),
                    );
                    return h(
                      "tr",
                      null,
                      h(
                        "td",
                        null,
                        h(
                          "div",
                          { class: "row" },
                          O.avatar(u.name || u.email, null, true),
                          h(
                            "div",
                            null,
                            h("b", null, u.name),
                            h("div", { class: "sub" }, u.email),
                          ),
                        ),
                      ),
                      h("td", null, role),
                      h(
                        "td",
                        null,
                        u.totp_enabled
                          ? h(
                              "span",
                              { class: "chip success" },
                              h("i", { class: "d" }),
                              "on",
                            )
                          : h("span", { class: "chip plain" }, "off"),
                      ),
                      h("td", { class: "sub" }, F.ago(u.last_login)),
                      h(
                        "td",
                        null,
                        chip(
                          u.disabled ? "skipped" : "success",
                          u.disabled ? "disabled" : "active",
                        ),
                      ),
                      h(
                        "td",
                        null,
                        h(
                          "div",
                          { class: "row wrap" },
                          h(
                            "button",
                            {
                              class: "btn sm",
                              onclick: function () {
                                resetLink(u);
                              },
                            },
                            smtp ? "Email reset link" : "Reset link",
                          ),
                          h(
                            "button",
                            {
                              class: "btn sm",
                              onclick: function () {
                                resetPw(u);
                              },
                            },
                            "Set password",
                          ),
                          u.totp_enabled
                            ? h(
                                "button",
                                {
                                  class: "btn sm",
                                  onclick: function () {
                                    O.confirm(
                                      "Reset 2FA for " + u.email + "?",
                                      "They can sign in with just their password and set it up again.",
                                      "Reset 2FA",
                                      true,
                                    ).then(function (y) {
                                      if (y)
                                        api(
                                          "POST",
                                          "/api/users/" + u.id + "/reset-2fa",
                                        )
                                          .then(function () {
                                            toast("2FA reset", "ok");
                                            load();
                                          })
                                          .catch(fail);
                                    });
                                  },
                                },
                                "Reset 2FA",
                              )
                            : null,
                          h(
                            "button",
                            {
                              class: "btn sm " + (u.disabled ? "" : "danger"),
                              onclick: function () {
                                api("PATCH", "/api/users/" + u.id, {
                                  disabled: !u.disabled,
                                })
                                  .then(load)
                                  .catch(fail);
                              },
                            },
                            u.disabled ? "Enable" : "Disable",
                          ),
                        ),
                      ),
                    );
                  }),
                ),
              ),
            ),
          );
        })
        .catch(function (e) {
          box.replaceChildren(O.empty("alert", "Could not load", e.message));
        });
    }
    function invite() {
      var em = h("input", {
          type: "email",
          placeholder: "name@company.com",
          required: true,
        }),
        rl = h(
          "select",
          null,
          ["operator", "viewer", "admin"].map(function (r) {
            return h("option", { value: r }, r);
          }),
        );
      modal(
        "Invite by email",
        h(
          "div",
          { class: "col" },
          h(
            "p",
            { class: "sub" },
            smtp
              ? "They get an email with a link where they choose their name and password."
              : "Email is not set up, so you will get a link to send them yourself.",
          ),
          h("div", { class: "field" }, h("label", null, "Email"), em),
          h(
            "div",
            { class: "field" },
            h("label", null, "Role"),
            rl,
            h(
              "div",
              { class: "hint" },
              "Viewer: read only. Operator: run workflows, deploy, publish. Admin: everything incl. .env, settings and users.",
            ),
          ),
        ),
        function (close) {
          var go = h(
            "button",
            { class: "btn primary" },
            smtp ? "Send invitation" : "Create invitation",
          );
          go.onclick = function () {
            busy(
              go,
              api("POST", "/api/invites", { email: em.value, role: rl.value })
                .then(function (r) {
                  close();
                  loadInvites();
                  linkModal("Invitation for " + r.email, r, r.email);
                })
                .catch(fail),
            );
          };
          return [h("button", { class: "btn", onclick: close }, "Cancel"), go];
        },
      );
    }
    function addUser() {
      var em = h("input", { type: "email", placeholder: "name@company.com" }),
        nm = h("input", { type: "text", placeholder: "Display name" }),
        pw = h("input", {
          type: "password",
          autocomplete: "new-password",
          placeholder: "At least 10 characters",
        }),
        rl = h(
          "select",
          null,
          ["operator", "viewer", "admin"].map(function (r) {
            return h("option", { value: r }, r);
          }),
        );
      modal(
        "Add user with a password",
        h(
          "div",
          { class: "col" },
          h("div", { class: "field" }, h("label", null, "Email"), em),
          h("div", { class: "field" }, h("label", null, "Name"), nm),
          h("div", { class: "field" }, h("label", null, "Password"), pw),
          h("div", { class: "field" }, h("label", null, "Role"), rl),
        ),
        function (close) {
          var go = h("button", { class: "btn primary" }, "Create");
          go.onclick = function () {
            busy(
              go,
              api("POST", "/api/users", {
                email: em.value,
                name: nm.value,
                password: pw.value,
                role: rl.value,
              })
                .then(function () {
                  close();
                  load();
                })
                .catch(fail),
            );
          };
          return [h("button", { class: "btn", onclick: close }, "Cancel"), go];
        },
      );
    }
    function resetLink(u) {
      api("POST", "/api/users/" + u.id + "/reset-link")
        .then(function (r) {
          r.email = u.email;
          linkModal("Password reset link for " + u.email, r, u.email);
        })
        .catch(fail);
    }
    function resetPw(u) {
      var pw = h("input", {
        type: "password",
        autocomplete: "new-password",
        placeholder: "New password",
      });
      modal(
        "Set password for " + u.email,
        h(
          "div",
          { class: "field" },
          h("label", null, "New password"),
          pw,
          h("div", { class: "hint" }, "They are signed out everywhere."),
        ),
        function (close) {
          var go = h("button", { class: "btn primary" }, "Set password");
          go.onclick = function () {
            busy(
              go,
              api("PATCH", "/api/users/" + u.id, { password: pw.value })
                .then(function () {
                  close();
                  toast("Password updated", "ok");
                })
                .catch(fail),
            );
          };
          return [h("button", { class: "btn", onclick: close }, "Cancel"), go];
        },
      );
    }
    loadPolicy();
    loadInvites();
    load();
  }

  /* ----- Account ----- */
  function tAccount(body) {
    var cur = h("input", {
        type: "password",
        autocomplete: "current-password",
      }),
      nw = h("input", { type: "password", autocomplete: "new-password" }),
      sess = h("div", { class: "col", style: { gap: "8px" } });
    var fn = h("input", {
        type: "text",
        maxLength: 40,
        value: S.me.first_name || "",
        autocomplete: "given-name",
      }),
      ln = h("input", {
        type: "text",
        maxLength: 40,
        value: S.me.last_name || "",
        autocomplete: "family-name",
      });
    var tf = h("div", { class: "card col" }, O.skel(2));
    body.append(
      h(
        "div",
        { class: "card col" },
        h("h3", null, icon("users"), "Profile"),
        h(
          "div",
          { class: "grid g3" },
          h("div", { class: "field" }, h("label", null, "First name"), fn),
          h("div", { class: "field" }, h("label", null, "Last name"), ln),
          h(
            "div",
            { class: "field" },
            h("label", null, "Email"),
            h("input", { type: "text", value: S.me.email, readOnly: true }),
          ),
        ),
        h(
          "div",
          { class: "row" },
          h(
            "button",
            {
              class: "btn primary",
              onclick: function (e) {
                busy(
                  e.target.closest("button"),
                  api("PATCH", "/api/me", {
                    first_name: fn.value,
                    last_name: ln.value,
                  })
                    .then(function (r) {
                      Object.assign(S.me, r.user);
                      toast("Profile saved", "ok");
                      O.shell();
                      O.route();
                    })
                    .catch(fail),
                );
              },
            },
            "Save profile",
          ),
        ),
      ),
      tf,
      h(
        "div",
        { class: "card col" },
        h("h3", null, icon("key"), "Change password"),
        h(
          "div",
          { class: "grid g2" },
          h(
            "div",
            { class: "field" },
            h("label", null, "Current password"),
            cur,
          ),
          h(
            "div",
            { class: "field" },
            h("label", null, "New password"),
            nw,
            h(
              "div",
              { class: "hint" },
              "At least 10 characters. Other sessions are signed out.",
            ),
          ),
        ),
        h(
          "div",
          { class: "row" },
          h(
            "button",
            {
              class: "btn primary",
              onclick: function (e) {
                busy(
                  e.target.closest("button"),
                  api("POST", "/api/me/password", {
                    current: cur.value,
                    new: nw.value,
                  })
                    .then(function () {
                      cur.value = nw.value = "";
                      toast("Password changed", "ok");
                      loadS();
                    })
                    .catch(fail),
                );
              },
            },
            "Update password",
          ),
        ),
      ),
      h("div", { class: "card" }, h("h3", null, "Your sessions"), sess),
    );
    function paintTf(st) {
      function confirmBox(title, text, label, fn2, danger) {
        var pw = h("input", {
            type: "password",
            autocomplete: "current-password",
          }),
          code = h("input", {
            type: "text",
            autocomplete: "one-time-code",
            placeholder: "6-digit code",
          });
        modal(
          title,
          h(
            "div",
            { class: "col" },
            h("p", { class: "sub" }, text),
            h("div", { class: "field" }, h("label", null, "Password"), pw),
            h(
              "div",
              { class: "field" },
              h("label", null, "Code from your app"),
              code,
            ),
          ),
          function (close) {
            var go = h(
              "button",
              { class: "btn " + (danger ? "danger solid" : "primary") },
              label,
            );
            go.onclick = function () {
              busy(go, fn2(pw.value, code.value, close));
            };
            return [
              h("button", { class: "btn", onclick: close }, "Cancel"),
              go,
            ];
          },
        );
      }
      tf.replaceChildren(
        h(
          "h3",
          null,
          icon("shield"),
          "Two-step verification",
          h("span", { class: "sp" }),
          st.enabled
            ? h("span", { class: "chip success" }, h("i", { class: "d" }), "On")
            : h(
                "span",
                { class: "chip skipped" },
                h("i", { class: "d" }),
                "Off",
              ),
        ),
        h(
          "p",
          { class: "sub" },
          st.enabled
            ? "Signing in needs a code from your authenticator app. Recovery codes left: " +
                st.recovery_left +
                "."
            : "Add an authenticator app (Google Authenticator, 1Password, Authy…) so a stolen password is not enough to sign in.",
        ),
        st.required
          ? h(
              "div",
              { class: "notice info" },
              icon("info"),
              "Your admin requires two-step verification, so it cannot be turned off.",
            )
          : null,
        h(
          "div",
          { class: "row wrap" },
          !st.enabled
            ? h(
                "button",
                {
                  class: "btn primary",
                  onclick: function () {
                    var host = h("div", null);
                    var m = modal(
                      "Set up two-step verification",
                      host,
                      null,
                      true,
                    );
                    O.twofaSetup(host, function () {
                      m.close();
                      S.me.totp_enabled = true;
                      toast("Two-step verification is on", "ok");
                      load2();
                    });
                  },
                },
                "Set up",
              )
            : null,
          st.enabled
            ? h(
                "button",
                {
                  class: "btn",
                  onclick: function () {
                    confirmBox(
                      "New recovery codes",
                      "Your old codes stop working.",
                      "Generate",
                      function (pw, code, close) {
                        return api("POST", "/api/2fa/recovery", {
                          password: pw,
                          code: code,
                        })
                          .then(function (r) {
                            close();
                            var host = h(
                              "div",
                              null,
                              O.recoveryView(r.recovery_codes, function () {
                                mm.close();
                                load2();
                              }),
                            );
                            var mm = modal("Your new recovery codes", host);
                          })
                          .catch(fail);
                      },
                    );
                  },
                },
                "New recovery codes",
              )
            : null,
          st.enabled && !st.required
            ? h(
                "button",
                {
                  class: "btn danger",
                  onclick: function () {
                    confirmBox(
                      "Turn off two-step verification?",
                      "You will sign in with just your password.",
                      "Turn off",
                      function (pw, code, close) {
                        return api("POST", "/api/2fa/disable", {
                          password: pw,
                          code: code,
                        })
                          .then(function () {
                            close();
                            S.me.totp_enabled = false;
                            toast("Two-step verification is off", "ok");
                            load2();
                          })
                          .catch(fail);
                      },
                      true,
                    );
                  },
                },
                "Turn off",
              )
            : null,
        ),
      );
    }
    function load2() {
      api("GET", "/api/2fa/status")
        .then(paintTf)
        .catch(function (e) {
          tf.replaceChildren(O.empty("alert", "Could not load", e.message));
        });
    }
    function loadS() {
      api("GET", "/api/me/sessions")
        .then(function (ss) {
          sess.replaceChildren.apply(
            sess,
            ss.map(function (s) {
              return h(
                "div",
                { class: "row wrap" },
                h(
                  "div",
                  { style: { minWidth: 0, flex: 1 } },
                  h(
                    "div",
                    null,
                    (s.ua || "Unknown device").slice(0, 70),
                    s.current ? chip("ok", "this device") : null,
                  ),
                  h(
                    "div",
                    { class: "sub" },
                    s.ip + " · active " + F.ago(s.last_seen),
                  ),
                ),
                !s.current
                  ? h(
                      "button",
                      {
                        class: "btn sm danger",
                        onclick: function () {
                          api("DELETE", "/api/me/sessions/" + s.id)
                            .then(loadS)
                            .catch(fail);
                        },
                      },
                      "Sign out",
                    )
                  : null,
              );
            }),
          );
        })
        .catch(fail);
    }
    load2();
    loadS();
  }

  function tAudit(body) {
    var box = h("div", { class: "card" }, O.skel(5)),
      q = h("input", {
        type: "search",
        placeholder: "Filter by user, action or detail",
        style: { maxWidth: "320px" },
      }),
      t = 0;
    body.append(h("div", { class: "row" }, q), box);
    function load() {
      api("GET", "/api/audit?limit=100&q=" + encodeURIComponent(q.value))
        .then(function (d) {
          box.replaceChildren(
            d.items.length
              ? h(
                  "div",
                  { class: "scroll-x" },
                  h(
                    "table",
                    { class: "tbl" },
                    h(
                      "thead",
                      null,
                      h(
                        "tr",
                        null,
                        ["When", "Who", "Action", "Detail", "IP"].map(
                          function (x) {
                            return h("th", null, x);
                          },
                        ),
                      ),
                    ),
                    h(
                      "tbody",
                      null,
                      d.items.map(function (a) {
                        return h(
                          "tr",
                          null,
                          h(
                            "td",
                            { class: "sub nowrap", title: F.dt(a.ts) },
                            F.ago(a.ts),
                          ),
                          h("td", null, a.user_email),
                          h(
                            "td",
                            null,
                            h("code", { class: "inline" }, a.action),
                          ),
                          h(
                            "td",
                            {
                              class: "sub",
                              style: {
                                wordBreak: "break-word",
                                maxWidth: "420px",
                              },
                            },
                            a.detail,
                          ),
                          h("td", { class: "sub mono" }, a.ip),
                        );
                      }),
                    ),
                  ),
                )
              : O.empty("list", "No entries"),
          );
        })
        .catch(function (e) {
          box.replaceChildren(O.empty("alert", "Could not load", e.message));
        });
    }
    q.oninput = function () {
      clearTimeout(t);
      t = setTimeout(load, 300);
    };
    load();
  }
})();
