// Review screen: live "lines vs total" difference, add and remove lines.
(function () {
  var form = document.getElementById("review-form");
  if (!form) return;

  function toCents(text) {
    var s = String(text || "").replace(/[€\s]/g, "");
    if (!s) return null;
    if (s.indexOf(",") > -1 && s.indexOf(".") > -1) {
      var thousands = s.lastIndexOf(",") > s.lastIndexOf(".") ? "." : ",";
      s = s.split(thousands).join("");
    }
    s = s.replace(",", ".");
    if (!/^-?\d+(\.\d{1,2})?$/.test(s)) return null;
    return Math.round(parseFloat(s) * 100);
  }

  function euro(cents) {
    if (cents === null) return "-";
    var sign = cents < 0 ? "-" : "";
    var abs = Math.abs(cents);
    return sign + Math.floor(abs / 100) + "." + ("0" + (abs % 100)).slice(-2);
  }

  function update() {
    var sum = 0;
    form.querySelectorAll("[data-line]").forEach(function (line) {
      var removed = line.querySelector(".js-delete").checked;
      line.classList.toggle("deleted", removed);
      if (removed) return;
      var cents = toCents(line.querySelector(".js-total").value);
      if (cents !== null) sum += cents;
    });
    var total = toCents(form.querySelector("#total").value);
    var diff = total === null ? null : total - sum;
    document.getElementById("delta-sum").textContent = euro(sum);
    document.getElementById("delta-total").textContent = euro(total);
    document.getElementById("delta-diff").textContent = euro(diff);
    document.getElementById("delta").classList.toggle("bad", diff !== null && diff !== 0);
    document.getElementById("delta").classList.toggle("good", diff === 0);
  }

  form.addEventListener("input", update);
  form.addEventListener("change", update);

  document.getElementById("add-line").addEventListener("click", function () {
    var counter = document.getElementById("line-count");
    var index = parseInt(counter.value, 10);
    var html = document.getElementById("line-template").innerHTML.split("__i__").join(String(index));
    var holder = document.createElement("div");
    holder.innerHTML = html;
    var line = holder.firstElementChild;
    document.getElementById("lines").appendChild(line);
    counter.value = String(index + 1);
    line.querySelector("input[type=text]").focus();
    update();
  });

  update();
})();
