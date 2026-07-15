// Trusted bootstrap injected into the canvas srcdoc BEFORE the model's HTML. This
// is OUR code, never the model's. It gives the model a narrow, mediated API —
// window.robothor.read()/propose() — implemented purely over postMessage to the
// parent. It holds no credentials and does no network I/O; the parent mediator is
// the only thing that can reach the bridge, and only for whitelisted ops.
export const CANVAS_SHIM_SOURCE = `
(function () {
  var pending = {};
  var seq = 0;
  function rid() { seq += 1; return "c" + seq + "_" + String(Math.random()).slice(2); }
  self.addEventListener("message", function (e) {
    var d = e && e.data;
    if (!d || d.__robothor !== true || d.kind !== "read-result") return;
    var cb = pending[d.reqId];
    if (!cb) return;
    delete pending[d.reqId];
    if (d.ok) cb.resolve(d.data); else cb.reject(new Error(d.error || "read failed"));
  });
  self.robothor = {
    read: function (op, args) {
      var reqId = rid();
      return new Promise(function (resolve, reject) {
        pending[reqId] = { resolve: resolve, reject: reject };
        parent.postMessage({ __robothor: true, kind: "read", reqId: reqId, op: op, args: args || {} }, "*");
        setTimeout(function () {
          if (pending[reqId]) { delete pending[reqId]; reject(new Error("read timed out")); }
        }, 10000);
      });
    },
    propose: function (action, args, label) {
      parent.postMessage({ __robothor: true, kind: "propose", reqId: rid(), action: action, args: args || {}, label: label || "" }, "*");
    }
  };
})();
`;
