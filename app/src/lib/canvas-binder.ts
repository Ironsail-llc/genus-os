// Trusted, parent-authored binder injected into the canvas srcdoc AFTER the shim.
// It translates the model's DECLARATIVE attributes into calls on the trusted
// window.robothor shim, and renders every result with textContent — never HTML.
// The model writes no JavaScript; this is our code, not the model's.
export const CANVAS_BINDER_SOURCE = `
(function () {
  function resolvePath(obj, path) {
    if (!path) return obj;
    var cur = obj;
    var parts = String(path).split(".");
    for (var i = 0; i < parts.length; i++) {
      if (cur == null) return undefined;
      var k = parts[i];
      if (k === "length" && Array.isArray(cur)) return cur.length;
      cur = cur[k];
    }
    return cur;
  }
  function asText(v) {
    if (Array.isArray(v)) return String(v.length);
    if (v == null || typeof v === "object") return "";
    return String(v);
  }
  function scan() {
    var R = self.robothor;
    if (!R || typeof R.read !== "function") return;
    var readEls = document.querySelectorAll("[data-read]");
    for (var i = 0; i < readEls.length; i++) {
      (function (el) {
        var op = el.getAttribute("data-read");
        R.read(op).then(function (data) {
          var targets = [];
          if (el.hasAttribute("data-bind")) targets.push(el);
          var kids = el.querySelectorAll("[data-bind]");
          for (var j = 0; j < kids.length; j++) targets.push(kids[j]);
          for (var t = 0; t < targets.length; t++) {
            targets[t].textContent = asText(resolvePath(data, targets[t].getAttribute("data-bind")));
          }
        }).catch(function () {});
      })(readEls[i]);
    }
    var propEls = document.querySelectorAll("[data-propose]");
    for (var p = 0; p < propEls.length; p++) {
      (function (el) {
        el.addEventListener("click", function () {
          if (!R || typeof R.propose !== "function") return;
          R.propose(el.getAttribute("data-propose"),
            { name: el.getAttribute("data-name"), value: el.getAttribute("data-value") },
            el.textContent);
        });
      })(propEls[p]);
    }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", scan);
  else scan();
})();
`;
