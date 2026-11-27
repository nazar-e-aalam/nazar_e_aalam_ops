/* Applies the saved/system theme before first paint. Loaded synchronously in <head>. */
(function () {
  var KEY = "nazar-theme";
  function get() {
    try { var s = localStorage.getItem(KEY); if (s === "light" || s === "dark") return s; } catch (e) {}
    return window.matchMedia && matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }
  function set(t) {
    document.documentElement.setAttribute("data-theme", t);
    try { localStorage.setItem(KEY, t); } catch (e) {}
    window.dispatchEvent(new CustomEvent("themechange", { detail: t }));
  }
  document.documentElement.setAttribute("data-theme", get());
  window.Theme = { get: function () { return document.documentElement.getAttribute("data-theme"); }, set: set,
                   toggle: function () { set(window.Theme.get() === "dark" ? "light" : "dark"); } };
})();
