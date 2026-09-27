// Live weighted total on the judge's scorecard. Same formula as services/results.review_value:
// each criterion rescaled to 0..1 by its own min/max, weighted, times 100. The form works without it.
(function () {
  var form = document.getElementById("scorecard"), out = document.getElementById("total");
  if (!form || !out) return;
  function update() {
    var num = 0, den = 0, complete = true;
    form.querySelectorAll("fieldset.criterion").forEach(function (f) {
      var picked = f.querySelector("input:checked"), w = parseFloat(f.dataset.weight);
      if (!picked) { complete = false; return; }
      var lo = +f.dataset.min, hi = +f.dataset.max;
      num += w * (+picked.value - lo) / (hi - lo); den += w;
    });
    out.textContent = complete && den ? (100 * num / den).toFixed(1) : "–";
  }
  form.addEventListener("change", update);
  update();
})();
