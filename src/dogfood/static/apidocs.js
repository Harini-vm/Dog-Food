// Renders /api/openapi.json as a readable list. Local file, no CDN: the docs work offline.
(function () {
  var root = document.getElementById("ops");
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text) e.textContent = text; return e; }
  fetch("/api/openapi.json").then(function (r) { return r.json(); }).then(function (spec) {
    root.textContent = "";
    var groups = {};
    Object.keys(spec.paths).sort().forEach(function (path) {
      var item = spec.paths[path];
      Object.keys(item).forEach(function (method) {
        var op = item[method], tag = (op.tags || ["other"])[0];
        (groups[tag] = groups[tag] || []).push({ path: path, method: method.toUpperCase(), op: op });
      });
    });
    Object.keys(groups).sort().forEach(function (tag) {
      root.appendChild(el("h2", "", tag));
      groups[tag].forEach(function (x) {
        var card = el("section", "card");
        var h = el("h3", "mono"); h.appendChild(el("span", "chip brand", x.method)); h.appendChild(document.createTextNode(" " + x.path));
        card.appendChild(h);
        if (x.op.summary) card.appendChild(el("p", "", x.op.summary));
        if (x.op.description) card.appendChild(el("p", "muted", x.op.description));
        var params = (x.op.parameters || []).map(function (p) { return p.name + " (" + p.in + (p.required ? ", required" : "") + ")"; });
        if (params.length) card.appendChild(el("p", "muted", "Parameters: " + params.join(", ")));
        if (x.op.requestBody) card.appendChild(el("p", "muted", "Body: JSON"));
        root.appendChild(card);
      });
    });
  }).catch(function () { root.textContent = "Could not load /api/openapi.json."; });
})();
