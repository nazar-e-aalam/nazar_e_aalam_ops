/* Roots: animated, interactive workflow graph (reusable version of the original dummy).
   const g = new Roots(hostEl, { onSelect(raw){}, xgap:205, ygap:72 });
   g.setData({key,name,type:'main|workflow|job',status,meta,collapsed,links:[keys],children:[...]});
   Calling setData again updates in place: positions, zoom and manual collapse state survive live updates. */
(function (global) {
  "use strict";
  var NS = "http://www.w3.org/2000/svg";
  var RAD = { main: 6.5, workflow: 5, job: 3.6 };
  var TRAIL = 6,
    NP = 16;
  var M = matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 1;
  var COLORS = {
    running: "yellow",
    success: "green",
    failed: "red",
    cancelled: "warn",
    queued: "idle",
    skipped: "idle",
  };

  function mk(tag, attrs, parent) {
    var e = document.createElementNS(NS, tag);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  function el(tag, cls, txt, parent) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (txt != null) e.textContent = txt;
    if (parent) parent.appendChild(e);
    return e;
  }
  function short(s, n) {
    s = String(s == null ? "" : s);
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }

  /* wavy root-like path between two nodes (same maths as the dummy) */
  function wpath(a, b, t, ph, amp, k, sp, cf) {
    var ax = a.cx,
      ay = a.cy,
      bx = b.cx,
      by = b.cy,
      dx = bx - ax;
    var c1 = ax + dx * cf,
      c2 = bx - dx * cf,
      A = amp * Math.min(1, Math.hypot(dx, by - ay) / 150) * M,
      P = [];
    for (var i = 0; i <= NP; i++) {
      var s = i / NP,
        m = 1 - s;
      var x =
        m * m * m * ax +
        3 * m * m * s * c1 +
        3 * m * s * s * c2 +
        s * s * s * bx;
      var y =
        m * m * m * ay +
        3 * m * m * s * ay +
        3 * m * s * s * by +
        s * s * s * by;
      var tx =
          3 * m * m * (c1 - ax) + 6 * m * s * (c2 - c1) + 3 * s * s * (bx - c2),
        ty = 6 * m * s * (by - ay);
      var l = Math.hypot(tx, ty) || 1,
        env = Math.sin(Math.PI * s);
      var d =
        A *
        env *
        (Math.sin(s * k - t * sp + ph) +
          0.35 * Math.sin(s * k * 2.1 - t * sp * 1.7 + ph * 2));
      P.push([x - (ty / l) * d, y + (tx / l) * d]);
    }
    var out = "M" + P[0][0].toFixed(1) + " " + P[0][1].toFixed(1);
    for (var j = 1; j < NP; j++)
      out +=
        "Q" +
        P[j][0].toFixed(1) +
        " " +
        P[j][1].toFixed(1) +
        " " +
        ((P[j][0] + P[j + 1][0]) / 2).toFixed(1) +
        " " +
        ((P[j][1] + P[j + 1][1]) / 2).toFixed(1);
    return out + "L" + P[NP][0].toFixed(1) + " " + P[NP][1].toFixed(1);
  }

  function Roots(host, opts) {
    var self = this;
    this.o = Object.assign(
      {
        xgap: 205,
        ygap: 72,
        onSelect: null,
        interactive: true,
        toolbar: true,
        legend: true,
        hint: true,
      },
      opts || {},
    );
    this.host = host;
    host.classList.add("roots");
    if (!this.o.interactive) host.classList.add("static");
    host.innerHTML =
      '<svg class="r-svg" role="img" aria-label="Workflow graph"><g class="r-world"><g class="r-edges"></g><g class="r-parts"></g><g class="r-nodes"></g></g></svg><div class="r-tip" role="tooltip"></div>';
    this.svg = host.querySelector(".r-svg");
    this.world = host.querySelector(".r-world");
    this.gE = host.querySelector(".r-edges");
    this.gP = host.querySelector(".r-parts");
    this.gN = host.querySelector(".r-nodes");
    this.tip = host.querySelector(".r-tip");
    this.map = new Map();
    this.edgeMap = new Map();
    this.all = [];
    this.edges = [];
    this.tf = { x: 0, y: 0, k: 1 };
    this.ptr = null;
    this.moved = false;
    this.drag = null;
    this.hov = null;
    this.hovT = 0;
    this.query = "";
    this.sel = null;
    this.root = null;
    this.running = true;
    this.needFit = true;
    if (this.o.legend) {
      var lg = el("div", "r-legend", null, host);
      [
        ["running", "Running"],
        ["success", "Passed"],
        ["failed", "Failed"],
        ["queued", "Queued"],
        ["cancelled", "Cancelled / skipped"],
      ].forEach(function (x) {
        var s = el("span", "", null, lg),
          b = el("b", "", null, s);
        b.style.background = "var(--" + COLORS[x[0]] + ")";
        s.append(x[1]);
      });
    }
    if (this.o.interactive && this.o.hint)
      el(
        "div",
        "r-hint",
        "Hover to reveal · click to select · scroll to zoom · drag to pan",
        host,
      );
    if (this.o.toolbar && this.o.interactive) this._toolbar();
    if (this.o.interactive) this._bind();
    this._ro = new ResizeObserver(function () {
      if (self.needFit && self.root && host.clientWidth > 40) {
        self.fit();
      }
    });
    this._ro.observe(host);
    this._frame = function (ms) {
      if (!self.running) return;
      self._tick(ms / 1000);
      self.raf = requestAnimationFrame(self._frame);
    };
    this.raf = requestAnimationFrame(this._frame);
  }
  var P = Roots.prototype;

  P._toolbar = function () {
    var self = this,
      t = el("div", "r-tools", null, this.host);
    var q = el("input", "", null, t);
    q.type = "search";
    q.placeholder = "Find node";
    q.setAttribute("aria-label", "Find node");
    q.addEventListener("input", function () {
      self.setQuery(q.value);
    });
    [
      [
        "Fit",
        function () {
          self.fit();
        },
      ],
      [
        "+",
        function () {
          self.zoom(1.25);
        },
      ],
      [
        "−",
        function () {
          self.zoom(0.8);
        },
      ],
      [
        "Expand",
        function () {
          self.expandAll(true);
        },
      ],
      [
        "Collapse",
        function () {
          self.expandAll(false);
        },
      ],
    ].forEach(function (b) {
      var btn = el("button", "", b[0], t);
      btn.type = "button";
      btn.addEventListener("click", b[1]);
    });
  };

  P._bind = function () {
    var self = this,
      svg = this.svg;
    svg.addEventListener("pointerdown", function (e) {
      self.drag = { x: e.clientX, y: e.clientY, tx: self.tf.x, ty: self.tf.y };
      self.moved = false;
    });
    this._move = function (e) {
      var r = self.host.getBoundingClientRect();
      self.ptr = {
        x: (e.clientX - r.left - self.tf.x) / self.tf.k,
        y: (e.clientY - r.top - self.tf.y) / self.tf.k,
      };
      if (!self.drag) return;
      var dx = e.clientX - self.drag.x,
        dy = e.clientY - self.drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 5) {
        self.moved = true;
        self.needFit = false;
        svg.classList.add("drag");
        self.ptr = null;
        self.tip.classList.remove("on");
      }
      if (self.moved) {
        self.tf.x = self.drag.tx + dx;
        self.tf.y = self.drag.ty + dy;
        self._apply();
      }
    };
    this._up = function () {
      self.drag = null;
      svg.classList.remove("drag");
      setTimeout(function () {
        self.moved = false;
      });
    };
    window.addEventListener("pointermove", this._move);
    window.addEventListener("pointerup", this._up);
    svg.addEventListener("pointerleave", function () {
      self.ptr = null;
    });
    svg.addEventListener(
      "wheel",
      function (e) {
        e.preventDefault();
        self.needFit = false;
        var r = self.host.getBoundingClientRect(),
          cx = e.clientX - r.left,
          cy = e.clientY - r.top;
        var k = Math.min(
            2.5,
            Math.max(0.25, self.tf.k * (e.deltaY < 0 ? 1.1 : 0.9)),
          ),
          q = k / self.tf.k;
        self.tf.x = cx - (cx - self.tf.x) * q;
        self.tf.y = cy - (cy - self.tf.y) * q;
        self.tf.k = k;
        self._apply();
      },
      { passive: false },
    );
  };

  P.destroy = function () {
    this.running = false;
    cancelAnimationFrame(this.raf);
    this._ro.disconnect();
    if (this._move) {
      window.removeEventListener("pointermove", this._move);
      window.removeEventListener("pointerup", this._up);
    }
    clearTimeout(this.hovT);
  };

  /* ---------- data sync (stable by key) ---------- */
  P.setData = function (raw) {
    var self = this,
      seen = new Set(),
      first = !this.root,
      now = performance.now() / 1000;
    function sync(r, parent, d) {
      var n = self.map.get(r.key),
        isNew = !n;
      if (isNew) {
        n = {
          key: r.key,
          ph: Math.random() * 6.283,
          o: 0,
          dm: 1,
          dt: 1,
          h: 1,
          born: 0,
          peek: false,
          touched: false,
          noPeek: false,
          cx: 0,
          cy: 0,
          tx: 0,
          ty: 0,
          col: false,
        };
        self.map.set(r.key, n);
        self._dom(n, r);
      }
      n.raw = r;
      n.name = String(r.name == null ? "untitled" : r.name);
      n.type = RAD[r.type] ? r.type : "job";
      n.status = COLORS[r.status] ? r.status : "queued";
      n.meta = r.meta == null ? "" : String(r.meta);
      n.d = d;
      n.p = parent;
      n.linkKeys = r.links || [];
      if (!n.touched) n.col = !!r.collapsed;
      seen.add(r.key);
      n.children = (r.children || []).map(function (c) {
        return sync(c, n, d + 1);
      });
      n._new = isNew;
      return n;
    }
    this.root = sync(raw, null, 0);
    this.map.forEach(function (n, k) {
      if (!seen.has(k)) {
        n.g.remove();
        self.map.delete(k);
      }
    });
    this.all = [];
    (function walk(n) {
      self.all.push(n);
      n.children.forEach(walk);
    })(this.root);
    this.all.forEach(function (n) {
      self._refreshDom(n);
    });
    this._edges();
    this._layout();
    this._dim();
    this.all.forEach(function (n) {
      if (n._new) {
        var src = first || !n.p ? n : n.p;
        n.cx = src.tx;
        n.cy = src.ty;
        n.o = 0;
        n.born = first ? now + n.d * 0.14 : 0;
        n._new = false;
      }
    });
    if (first) this.needFit = true;
    if (this.needFit && this.host.clientWidth > 40) this.fit();
  };

  P._dom = function (n, r) {
    var self = this,
      rad = RAD[RAD[r.type] ? r.type : "job"],
      type = RAD[r.type] ? r.type : "job";
    var g = mk("g", { class: "nd" }, this.gN);
    g.style.display = "none"; // shown by the first tick once the node is visible
    g.innerHTML =
      '<circle r="' +
      (rad + 16) +
      '" fill="transparent"/>' +
      (type !== "job"
        ? '<circle class="sring" r="' +
          (rad + (type === "main" ? 6 : 4.5)) +
          '"/>'
        : "") +
      '<circle class="ring" r="' +
      rad +
      '"/><circle class="boom" r="' +
      rad +
      '"/><circle class="spin" r="' +
      (rad + 7) +
      '"/>' +
      '<circle class="cring" r="' +
      (rad + 10) +
      '"/><circle class="selring" r="' +
      (rad + 9) +
      '"/><circle class="core" r="' +
      rad +
      '"/>' +
      '<text class="nm ' +
      type +
      '" y="' +
      -(rad + (type === "main" ? 15 : 12)) +
      '"></text><text class="mt" y="' +
      (rad + 16) +
      '"></text><text class="more" x="' +
      (rad + 13) +
      '" y="3"></text>';
    n.g = g;
    n.tName = g.querySelector(".nm");
    n.tMeta = g.querySelector(".mt");
    n.tMore = g.querySelector(".more");
    if (!this.o.interactive) return;
    g.addEventListener("pointerenter", function () {
      clearTimeout(self.hovT);
      self._hover(n);
    });
    g.addEventListener("pointerleave", function () {
      self.hovT = setTimeout(function () {
        n.noPeek = false;
        self._hover(null);
      }, 420);
    });
    g.addEventListener("click", function () {
      if (self.moved) return;
      if (n.children.length) {
        n.touched = true;
        if (n.col) {
          n.col = false;
        } else {
          n.col = true;
          n.noPeek = true;
        }
        self._refreshDom(n);
        self._hover(n);
      }
      self.select(n.key, true);
    });
  };

  P._count = function (n) {
    var c = 0;
    (function w(x) {
      x.children.forEach(function (k) {
        c++;
        w(k);
      });
    })(n);
    return c;
  };

  P._refreshDom = function (n) {
    var cls =
      "nd " +
      n.status +
      (n.children.length ? " has" : "") +
      (n.col ? " col" : "") +
      (n._boom ? " boom" : "") +
      (this.sel === n.key ? " sel" : "") +
      " " +
      n.type;
    if (n.g.getAttribute("class") !== cls) {
      var was = n._lastStatus;
      n.g.setAttribute("class", cls);
      if (was && was !== "failed" && n.status === "failed") {
        n._boom = true;
        n.g.setAttribute("class", cls + " boom");
        setTimeout(function () {
          n._boom = false;
          n.g.setAttribute("class", cls);
        }, 1100);
      }
    }
    n._lastStatus = n.status;
    var nm = short(n.name, n.type === "job" ? 22 : 26);
    if (n.tName.textContent !== nm) n.tName.textContent = nm;
    if (n.tMeta.textContent !== n.meta) n.tMeta.textContent = short(n.meta, 30);
    var more = n.col ? "+" + this._count(n) : "";
    if (n.tMore.textContent !== more) n.tMore.textContent = more;
  };

  P._edges = function () {
    var self = this,
      keep = new Set(),
      list = [];
    function ensure(src, dst, kind) {
      var id = src.key + ">" + dst.key + (kind === "link" ? "~" : "");
      keep.add(id);
      var e = self.edgeMap.get(id);
      if (!e) {
        var g = mk("g", { class: "edge" }, self.gE),
          w = dst.type === "main" ? 2.2 : dst.type === "workflow" ? 1.7 : 1.2;
        e = {
          id: id,
          kind: kind,
          g: g,
          m: mk(
            "path",
            { class: "m", "stroke-width": kind === "link" ? 1 : w },
            g,
          ),
          pts: [],
          on: false,
          ph: dst.ph,
        };
        if (kind === "tree") {
          e.t = mk("path", { class: "t" }, g);
          for (var k = 0; k < 2; k++) {
            var tr = [];
            for (var i = 0; i < TRAIL; i++)
              tr.push(
                mk(
                  "circle",
                  { class: "pt", r: 3.2 - i * 0.45, opacity: 0 },
                  self.gP,
                ),
              );
            e.pts.push(tr);
          }
        }
        self.edgeMap.set(id, e);
      }
      e.src = src;
      e.dst = dst;
      var c = "edge " + (kind === "link" ? "link " : "") + dst.status;
      if (e.cls !== c) {
        e.cls = c;
        e.g.setAttribute("class", c);
      }
      list.push(e);
    }
    this.all.forEach(function (n) {
      if (n.p) ensure(n.p, n, "tree");
      n.linkKeys.forEach(function (k) {
        var s = self.map.get(k);
        if (s && s !== n) ensure(s, n, "link");
      });
    });
    this.edgeMap.forEach(function (e, id) {
      if (!keep.has(id)) {
        e.g.remove();
        e.pts.forEach(function (tr) {
          tr.forEach(function (p) {
            p.remove();
          });
        });
        self.edgeMap.delete(id);
      }
    });
    this.edges = list;
  };

  /* ---------- layout: only visible nodes take rows ---------- */
  var exp = function (n) {
    return !n.col || n.peek;
  };
  P._layout = function () {
    var self = this,
      i = 0;
    (function v(n, pv) {
      n.tv = pv;
      n.children.forEach(function (c) {
        v(c, pv && exp(n));
      });
    })(this.root, true);
    (function lay(n) {
      var ch = exp(n) ? n.children : [];
      if (!ch.length) n.ty = i++ * self.o.ygap;
      else {
        ch.forEach(lay);
        n.ty = (ch[0].ty + ch[ch.length - 1].ty) / 2;
      }
      n.tx = n.d * self.o.xgap;
    })(this.root);
    (function hid(n) {
      n.children.forEach(function (c) {
        if (!c.tv) {
          c.tx = n.tx;
          c.ty = n.ty;
        }
        hid(c);
      });
    })(this.root);
  };
  P._dim = function () {
    var q = this.query.trim().toLowerCase(),
      hl = null,
      hov = this.hov;
    if (hov) {
      hl = new Set();
      for (var a = hov; a; a = a.p) hl.add(a);
      (function d(n) {
        hl.add(n);
        n.children.forEach(d);
      })(hov);
    }
    this.all.forEach(function (n) {
      n.dt = q
        ? n.name.toLowerCase().indexOf(q) >= 0
          ? 1
          : 0.13
        : hl
          ? hl.has(n)
            ? 1
            : 0.16
          : 1;
    });
  };
  P.setQuery = function (q) {
    this.query = q || "";
    this._dim();
  };

  P._hover = function (n) {
    this.hov = n;
    this.all.forEach(function (x) {
      x.peek = false;
    });
    if (n)
      for (var a = n; a; a = a.p) if (!(a === n && a.noPeek)) a.peek = true;
    this._layout();
    this._dim();
    this._tip(n);
  };
  P._tip = function (n) {
    var tip = this.tip;
    if (!n) {
      tip.classList.remove("on");
      return;
    }
    tip.textContent = "";
    var r = el("div", "r", null, tip);
    el("span", "t", n.name, r);
    el(
      "span",
      "k",
      n.type === "main" ? "run" : n.type === "workflow" ? "job" : "step",
      r,
    );
    var s = el("div", "s", null, tip),
      b = el("b", "", null, s);
    b.style.background = "var(--" + COLORS[n.status] + ")";
    el(
      "span",
      "",
      n.status + (n.meta && n.meta !== n.status ? " · " + n.meta : ""),
      s,
    );
    var disp = n.raw && n.raw.ref && n.raw.ref.display;
    if (disp && disp !== n.name) el("div", "n", disp, tip);
    var nx = el("div", "n", null, tip);
    if (n.children.length) {
      nx.append("Next: ");
      el(
        "em",
        "",
        n.children
          .slice(0, 4)
          .map(function (c) {
            return c.name;
          })
          .join(", ") +
          (n.children.length > 4 ? " +" + (n.children.length - 4) : ""),
        nx,
      );
      nx.append(" · " + this._count(n) + " downstream");
    } else nx.textContent = "End of this branch.";
    tip.classList.add("on");
  };

  /* ---------- selection / camera ---------- */
  P.select = function (key, fire) {
    this.sel = key;
    var self = this;
    this.all.forEach(function (n) {
      self._refreshDom(n);
    });
    if (fire && this.o.onSelect) {
      var n = this.map.get(key);
      this.o.onSelect(n ? n.raw : null);
    }
  };
  P.expandAll = function (open) {
    this.all.forEach(function (n) {
      if (n.children.length && n.type !== "main") {
        n.col = !open;
        n.touched = true;
      }
    });
    var self = this;
    this.all.forEach(function (n) {
      self._refreshDom(n);
    });
    this._layout();
    this.needFit = true;
    setTimeout(function () {
      self.fit();
    }, 60);
  };
  P._apply = function () {
    this.world.setAttribute(
      "transform",
      "translate(" + this.tf.x + " " + this.tf.y + ") scale(" + this.tf.k + ")",
    );
  };
  P.zoom = function (f) {
    var w = this.host.clientWidth / 2,
      h = this.host.clientHeight / 2,
      k = Math.min(2.5, Math.max(0.25, this.tf.k * f)),
      q = k / this.tf.k;
    this.tf.x = w - (w - this.tf.x) * q;
    this.tf.y = h - (h - this.tf.y) * q;
    this.tf.k = k;
    this.needFit = false;
    this._apply();
  };
  P.fit = function () {
    var W = this.host.clientWidth,
      H = this.host.clientHeight;
    if (W < 40 || !this.root) return;
    this._layout();
    var minx = 1e9,
      maxx = -1e9,
      miny = 1e9,
      maxy = -1e9;
    this.all.forEach(function (n) {
      if (n.tv) {
        minx = Math.min(minx, n.tx);
        maxx = Math.max(maxx, n.tx);
        miny = Math.min(miny, n.ty);
        maxy = Math.max(maxy, n.ty);
      }
    });
    var padX = 90,
      padTop = 72,
      padBot = 64,
      w = maxx - minx,
      h = maxy - miny;
    var k = Math.min(
      1.25,
      (W - padX * 2) / Math.max(w, 1),
      (H - padTop - padBot) / Math.max(h, 1),
    );
    k = Math.max(0.3, k);
    this.tf.k = k;
    this.tf.x = Math.max(padX * 0.6, (W - w * k) / 2 - minx * k);
    this.tf.y =
      padTop + Math.max(0, (H - padTop - padBot - h * k) / 2) - miny * k;
    this._apply();
    this.needFit = false;
  };

  /* ---------- animation ---------- */
  P._tick = function (t) {
    var self = this,
      hov = this.hov,
      ptr = this.ptr;
    this.all.forEach(function (n) {
      var vis = n.tv && t >= n.born;
      n.o += ((vis ? 1 : 0) - n.o) * 0.09;
      n.dm += (n.dt - n.dm) * 0.14;
      if (!vis && n.o < 0.01) {
        if (!n.h) {
          n.g.style.display = "none";
          n.h = 1;
        }
        if (n.p) {
          n.cx = n.p.cx;
          n.cy = n.p.cy;
        }
        return;
      }
      if (n.h) {
        n.g.style.display = "";
        n.h = 0;
      }
      var A = (n.d ? 5 : 1.5) * M,
        px = 0,
        py = 0;
      if (ptr) {
        var dx = n.tx - ptr.x,
          dy = n.ty - ptr.y,
          d = Math.hypot(dx, dy) || 1;
        if (d < 120) {
          var f = (120 - d) / 120;
          px = (dx / d) * f * 22;
          py = (dy / d) * f * 22;
        }
      }
      var gx =
        n.tx +
        Math.sin(t * 0.62 + n.ph) * A +
        Math.cos(t * 0.31 + n.ph * 1.7) * A * 0.6 +
        px;
      var gy =
        n.ty +
        Math.cos(t * 0.55 + n.ph * 1.3) * A +
        Math.sin(t * 0.28 + n.ph) * A * 0.6 +
        py;
      var sx = vis ? 0.1 : 0.2;
      n.cx += (gx - n.cx) * sx;
      n.cy += (gy - n.cy) * sx;
      n.g.setAttribute(
        "transform",
        "translate(" + n.cx.toFixed(1) + " " + n.cy.toFixed(1) + ")",
      );
      n.g.setAttribute("opacity", (n.o * n.dm).toFixed(3));
    });
    this.edges.forEach(function (e) {
      var c = e.dst,
        s = e.src;
      if (c.h || s.h) {
        if (!e.hid) {
          e.g.style.display = "none";
          e.hid = true;
        }
        return;
      }
      if (e.hid) {
        e.g.style.display = "";
        e.hid = false;
      }
      if (e.kind === "link") {
        e.m.setAttribute("d", wpath(s, c, t, e.ph, 6, 5, 1, 0.5));
        e.g.setAttribute(
          "opacity",
          (Math.min(c.o, s.o) * Math.min(c.dm, s.dm)).toFixed(3),
        );
        return;
      }
      e.m.setAttribute("d", wpath(s, c, t, e.ph, 10, 7, 1.3, 0.5));
      e.t.setAttribute("d", wpath(s, c, t, e.ph + 2, 17, 5, -0.9, 0.36));
      e.g.setAttribute("opacity", (c.o * c.dm).toFixed(3));
      var run = c.status === "running" && M && c.o > 0.3;
      if (!run) {
        if (e.on) {
          e.pts.forEach(function (tr) {
            tr.forEach(function (p) {
              p.setAttribute("opacity", 0);
            });
          });
          e.on = false;
        }
        return;
      }
      e.on = true;
      var L = e.m.getTotalLength();
      e.pts.forEach(function (tr, k) {
        var base = (t * 0.36 + k * 0.5 + e.ph / 6.283) % 1;
        tr.forEach(function (p, i) {
          var u = base - i * 0.02;
          if (u < 0) {
            p.setAttribute("opacity", 0);
            return;
          }
          var q = e.m.getPointAtLength(u * L);
          p.setAttribute("cx", q.x);
          p.setAttribute("cy", q.y);
          p.setAttribute("opacity", ((1 - i / TRAIL) * c.dm).toFixed(2));
        });
      });
    });
    if (hov && !this.moved) {
      var sx2 = this.tf.x + hov.cx * this.tf.k,
        sy2 = this.tf.y + hov.cy * this.tf.k,
        w = this.tip.offsetWidth,
        hh = this.tip.offsetHeight;
      var HW = this.host.clientWidth,
        HH = this.host.clientHeight;
      var x = sx2 + 26 + w > HW - 8 ? sx2 - 26 - w : sx2 + 26;
      this.tip.style.transform =
        "translate(" +
        Math.max(8, x) +
        "px," +
        Math.max(8, Math.min(sy2 - hh / 2, HH - hh - 8)) +
        "px)";
    }
  };

  global.Roots = Roots;
})(window);
