/* Public downloads site. Read-only, no login. All text is inserted with textContent (no innerHTML). */
(function () {
  "use strict";
  var root = document.getElementById("root"),
    tbtn = document.getElementById("tbtn");
  function paintTheme() {
    tbtn.textContent = Theme.get() === "dark" ? "☀ Light" : "☾ Dark";
  }
  tbtn.onclick = function () {
    Theme.toggle();
  };
  window.addEventListener("themechange", paintTheme);
  paintTheme();

  var SVG_NS = "http://www.w3.org/2000/svg";
  var SVG_TAGS = /^(svg|path|g|circle|rect|line|polyline|polygon|defs|use)$/;
  var PROP_KEYS = { value: 1, checked: 1, disabled: 1, hidden: 1, selected: 1 };

  function cx(v) {
    // "a b" | ["a", cond && "b"] | { a: true, b: false } -> "a b"
    if (!v) return "";
    if (typeof v === "string") return v;
    if (Array.isArray(v)) return v.map(cx).filter(Boolean).join(" ");
    return Object.keys(v)
      .filter(function (k) {
        return v[k];
      })
      .join(" ");
  }

  function addKids(el, list) {
    [].concat(list == null ? [] : list).forEach(function (c) {
      if (c == null || c === false || c === true) return;
      if (Array.isArray(c)) return addKids(el, c);
      el.appendChild(c.nodeType ? c : document.createTextNode(String(c)));
    });
  }

  /*
  h(tag)
  h(tag, "cls")
  h(tag, "cls", kids)
  h(tag, { class, style, dataset, onclick, ...attrs }, kids)
  h(tag, kids)   // when 2nd arg is a node or array
  No innerHTML support on purpose: text only goes through text nodes.
*/
  function h(tag, a, b) {
    var props = null,
      kids = b;
    if (typeof a === "string") props = a ? { class: a } : null;
    else if (a && !a.nodeType && !Array.isArray(a) && typeof a === "object")
      props = a;
    else if (a != null && b === undefined) kids = a;

    var isSvg = SVG_TAGS.test(tag);
    var e = isSvg
      ? document.createElementNS(SVG_NS, tag)
      : document.createElement(tag);

    if (props) {
      Object.keys(props).forEach(function (k) {
        var v = props[k];
        if (v == null || v === false) return;

        if (k === "class" || k === "className") {
          var c = cx(v);
          if (c) e.setAttribute("class", c); // setAttribute works for SVG too
        } else if (k === "style") {
          if (typeof v === "string") e.style.cssText = v;
          else Object.assign(e.style, v);
        } else if (k === "dataset") {
          Object.keys(v).forEach(function (d) {
            e.dataset[d] = v[d];
          });
        } else if (k.slice(0, 2) === "on" && typeof v === "function") {
          e.addEventListener(k.slice(2).toLowerCase(), v);
        } else if (k === "htmlFor") {
          e.setAttribute("for", v);
        } else if (k === "html" || k === "innerHTML") {
          return; // blocked on purpose
        } else if (
          (k === "href" || k === "src") &&
          /^\s*javascript:/i.test(v)
        ) {
          return; // block javascript: URLs
        } else if (!isSvg && PROP_KEYS[k]) {
          e[k] = v;
        } else {
          e.setAttribute(k, v === true ? "" : v);
        }
      });
    }
    addKids(e, kids);
    return e;
  }
  function svg(path) {
    var s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    s.setAttribute("viewBox", "0 0 24 24");
    s.setAttribute("class", "i");
    s.setAttribute("aria-hidden", "true");
    var p = document.createElementNS("http://www.w3.org/2000/svg", "path");
    p.setAttribute("d", path);
    s.appendChild(p);
    return s;
  }
  var P = {
    android:
      "M5 16V9h14v7a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2zM8 3l1.5 2.5M16 3l-1.5 2.5M9.5 12h.01M14.5 12h.01",
    ios: "M12 18h.01M7 2h10a2 2 0 0 1 2 2v16a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2z",
    dl: "M12 3v12m0 0l-5-5m5 5l5-5M4 21h16",
    link: "M10 14a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1M14 10a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1",
    file: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6",
  };
  function bytes(n) {
    var u = ["B", "KB", "MB", "GB"],
      i = 0;
    while (n >= 1024 && i < 3) {
      n /= 1024;
      i++;
    }
    return (i ? n.toFixed(n < 10 ? 1 : 0) : n) + " " + u[i];
  }
  function date(v) {
    return new Date(v * 1000).toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
  }
  function copy(t) {
    if (navigator.clipboard) navigator.clipboard.writeText(t);
  }
  function get(url) {
    return fetch(url, { credentials: "omit" }).then(function (r) {
      if (!r.ok)
        throw new Error(r.status === 404 ? "Not found" : "Could not load");
      return r.json();
    });
  }

  /* markdown-lite: headings, bullets, **bold**, `code`, http(s) links. Everything else is plain text. */
  function inline(text, parent) {
    var re =
        /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\(https?:\/\/[^)\s]+\)|https?:\/\/[^\s)]+)/g,
      last = 0,
      m;
    while ((m = re.exec(text))) {
      if (m.index > last)
        parent.appendChild(document.createTextNode(text.slice(last, m.index)));
      var t = m[0];
      if (t[0] === "*") parent.appendChild(h("b", "", t.slice(2, -2)));
      else if (t[0] === "`") parent.appendChild(h("code", "", t.slice(1, -1)));
      else if (t[0] === "[") {
        var mm = /\[([^\]]+)\]\((.+)\)/.exec(t),
          a = h("a", "", mm[1]);
        a.href = mm[2];
        a.rel = "noopener noreferrer nofollow";
        a.target = "_blank";
        parent.appendChild(a);
      } else {
        var a2 = h("a", "", t);
        a2.href = t;
        a2.rel = "noopener noreferrer nofollow";
        a2.target = "_blank";
        parent.appendChild(a2);
      }
      last = m.index + t.length;
    }
    if (last < text.length)
      parent.appendChild(document.createTextNode(text.slice(last)));
  }
  function notes(text) {
    var box = h("div", "notes"),
      ul = null;
    String(text || "")
      .split("\n")
      .forEach(function (line) {
        var l = line.trim();
        if (!l) {
          ul = null;
          return;
        }
        var li = /^[-*•]\s+(.*)$/.exec(l),
          hd = /^#{1,4}\s+(.*)$/.exec(l);
        if (li) {
          if (!ul) {
            ul = h("ul");
            box.appendChild(ul);
          }
          var it = h("li");
          inline(li[1], it);
          ul.appendChild(it);
        } else {
          ul = null;
          if (hd) box.appendChild(h("h4", "", hd[1]));
          else {
            var p = h("p");
            inline(l, p);
            box.appendChild(p);
          }
        }
      });
    return box;
  }

  function hashBtn(sha) {
    if (!sha) return h("span", "hash", "link");
    var b = h("button", "hash", "sha256 " + sha.slice(0, 8));
    b.type = "button";
    b.title = "Click to copy the full SHA-256";
    b.onclick = function () {
      copy(sha);
      b.textContent = "copied";
      setTimeout(function () {
        b.textContent = "sha256 " + sha.slice(0, 8);
      }, 1200);
    };
    return b;
  }
  function dlBtn(asset, label, primary) {
    var a = h("a", "btn " + (primary ? "primary" : ""), [
      svg(P.dl),
      label || "Download",
    ]);
    a.href = asset.url;
    a.setAttribute("download", "");
    return a;
  }

  var UA = /iphone|ipad|ipod/i.test(navigator.userAgent)
    ? "ios"
    : /android/i.test(navigator.userAgent)
      ? "android"
      : "";
  function copyBtn(asset, label) {
    var b = h("button", "btn", [svg(P.link), label || "Copy link"]);
    b.type = "button";
    b.onclick = function () {
      copy(asset.source_url || location.origin + asset.url);
      var t = b.lastChild;
      t.textContent = "Copied ✓";
      setTimeout(function () {
        t.textContent = label || "Copy link";
      }, 1400);
    };
    return b;
  }

  function card(platform, rel) {
    var name = platform === "android" ? "Android" : "iOS",
      c = h(
        "div",
        "dl " +
          platform +
          (rel ? "" : " soon") +
          (UA === platform && rel ? " mine" : ""),
      );
    c.appendChild(
      h("div", "plat", [
        svg(P[platform]),
        name,
        rel ? h("span", "chip ok", [h("i", "d"), "Latest"]) : null,
        UA === platform && rel ? h("span", "chip accent", "Your device") : null,
      ]),
    );
    if (!rel) {
      c.appendChild(
        h(
          "div",
          "ver",
          platform === "ios" ? "Coming soon" : "Not available yet",
        ),
      );
      return c;
    }
    var main =
      rel.assets.filter(function (a) {
        return /\.(apk|ipa)$/i.test(a.filename);
      })[0] || rel.assets[0];
    c.appendChild(h("div", "big", "v" + rel.version));
    c.appendChild(
      h(
        "div",
        "ver",
        "Released " + date(rel.published_at) + " · " + bytes(main.size),
      ),
    );
    c.appendChild(
      h("div", "acts", [
        dlBtn(main, "Download " + (main.kind || "").toUpperCase(), true),
        copyBtn(main),
      ]),
    );
    c.appendChild(
      h("div", "meta", [h("span", "", main.filename), hashBtn(main.sha256)]),
    );
    return c;
  }

  function releaseView(r) {
    var box = h("section", "rel" + (r.latest ? " is-latest" : "")),
      plat =
        r.platform === "ios"
          ? "iOS"
          : r.platform === "android"
            ? "Android"
            : "Other";
    box.appendChild(
      h("div", "side", [
        h("span", "d", date(r.published_at)),
        h("span", "pl " + r.platform, plat),
        h("span", "", r.downloads + " downloads"),
      ]),
    );
    var title = h("h3", "", ["v" + r.version]);
    if (r.latest)
      title.appendChild(h("span", "chip ok", [h("i", "d"), "Latest"]));
    if (r.channel === "beta") title.appendChild(h("span", "chip warn", "Beta"));
    box.appendChild(
      h("div", "main", [
        title,
        r.notes ? notes(r.notes) : null,
        h(
          "div",
          "assets",
          r.assets.map(function (a) {
            var n = h("a", "n", [svg(P.file), a.filename]);
            n.href = a.url;
            n.setAttribute("download", "");
            return h("div", "asset", [
              n,
              hashBtn(a.sha256),
              h("span", "dc", a.downloads + " ↓"),
              h("span", "sz", bytes(a.size)),
              copyBtn(a, "Copy"),
            ]);
          }),
        ),
      ]),
    );
    return box;
  }

  function appPage(slug) {
    return get("/applications/api/apps/" + encodeURIComponent(slug)).then(
      function (d) {
        document.title = d.app.name + " · Downloads";
        document.getElementById("site").textContent = d.app.name;
        var latest = {};
        d.releases.forEach(function (r) {
          if (r.latest) latest[r.platform] = r;
        });
        var showBeta = false,
          plat = "all",
          limit = 5,
          list = h("div", "timeline"),
          more = h("button", "btn more"),
          hasBeta = d.releases.some(function (r) {
            return r.channel === "beta";
          });
        var totalDl = d.releases.reduce(function (n, r) {
            return n + r.downloads;
          }, 0),
          top = latest.android || latest.ios || latest.other;
        var plats = ["android", "ios", "other"].filter(function (p) {
          return d.releases.some(function (r) {
            return r.platform === p;
          });
        });
        var tabs = h("div", "ptabs");
        function paint() {
          var rows = d.releases.filter(function (r) {
            return (
              (showBeta || r.channel !== "beta" || r.latest) &&
              (plat === "all" || r.platform === plat)
            );
          });
          var shown = rows.slice(0, limit);
          list.replaceChildren.apply(
            list,
            shown.length
              ? shown.map(releaseView)
              : [h("div", "note-empty", "No releases yet.")],
          );
          more.hidden = rows.length <= limit;
          more.textContent =
            "Show " +
            (rows.length - limit) +
            " older version" +
            (rows.length - limit === 1 ? "" : "s");
          tabs.replaceChildren.apply(
            tabs,
            ["all"].concat(plats).map(function (p) {
              var b = h(
                "button",
                p === plat ? "on" : "",
                p === "all"
                  ? "All"
                  : p === "ios"
                    ? "iOS"
                    : p[0].toUpperCase() + p.slice(1),
              );
              b.type = "button";
              b.onclick = function () {
                plat = p;
                limit = 5;
                paint();
              };
              return b;
            }),
          );
        }
        more.type = "button";
        more.onclick = function () {
          limit += 10;
          paint();
        };
        var tog = h("label", "tog"),
          cb = h("input");
        cb.type = "checkbox";
        cb.onchange = function () {
          showBeta = cb.checked;
          paint();
        };
        tog.appendChild(h("span", "switch", [cb, h("i")]));
        tog.appendChild(document.createTextNode("Beta builds"));
        var stats = h("div", "stats", [
          top
            ? h("span", "stat", [h("b", "", "v" + top.version), "latest"])
            : null,
          h("span", "stat", [
            h("b", "", String(d.releases.length)),
            "versions",
          ]),
          h("span", "stat", [h("b", "", String(totalDl)), "downloads"]),
        ]);
        var kids = [
          h("div", "hero", [
            h("div", "logo", h("img", { src: "/favicon.svg", alt: "" })),
            h("div", "ht", [
              h("h1", "", d.app.name),
              d.app.description ? h("p", "", d.app.description) : null,
              stats,
            ]),
          ]),
          h("div", "dl-grid", [
            card("android", latest.android),
            card("ios", latest.ios),
          ]),
          h("div", "sec-h", [
            h("h2", "", "All versions"),
            h("span", "sp"),
            tabs,
            hasBeta ? tog : null,
          ]),
          list,
          h("div", "morewrap", more),
        ];
        root.replaceChildren.apply(root, kids);
        paint();
      },
    );
  }

  function index() {
    return get("/applications/api/apps").then(function (d) {
      if (d.apps.length === 1) {
        history.replaceState(null, "", "/applications/" + d.apps[0].slug);
        return appPage(d.apps[0].slug);
      }
      document.title = "Downloads";
      root.replaceChildren(
        h("div", "hero", [
          h("h1", "", "Downloads"),
          h("p", "", "Pick an app to get the latest version or an older one."),
        ]),
        d.apps.length
          ? h(
              "div",
              "apps-grid",
              d.apps.map(function (a) {
                var l = a.latest || {},
                  el = h("a", "appcard", [
                    h("h3", "", a.name),
                    a.description ? h("p", "", a.description) : null,
                    h("div", "row wrap", [
                      l.android
                        ? h("span", "chip ok", "Android " + l.android.version)
                        : null,
                      l.ios
                        ? h("span", "chip info", "iOS " + l.ios.version)
                        : null,
                    ]),
                    h(
                      "div",
                      "sub",
                      a.release_count +
                        " releases · " +
                        a.downloads +
                        " downloads",
                    ),
                  ]);
                el.href = "/applications/" + a.slug;
                return el;
              }),
            )
          : h("div", "note-empty", "Nothing published yet."),
      );
    });
  }

  var m = /^\/applications\/([^\/]+)\/?$/.exec(location.pathname);
  (m && m[1] !== "api" ? appPage(decodeURIComponent(m[1])) : index()).catch(
    function (e) {
      root.replaceChildren(
        h("div", "note-empty", [h("h2", "", "Oops"), e.message]),
      );
    },
  );
})();
