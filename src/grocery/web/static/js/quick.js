// Quick add: weight x price per kg fills in the amount paid, the same way the server calculates it
// (rounded half up to whole cents). Typing in the amount yourself stops the automatic filling until
// you clear that field again.
(function () {
  var weight = document.getElementById("weight");
  var price = document.getElementById("price_per_kg");
  var total = document.getElementById("total");
  var note = document.getElementById("total-note");
  if (!weight || !price || !total) return;

  var manual = total.value.trim() !== ""; // an amount that is already there (e.g. after an error) is kept
  var defaultNote = note ? note.textContent : "";

  function toMilli(text) { // "0,874" -> 874 (thousandths of a kg)
    var s = String(text || "").trim().replace(",", ".");
    if (!/^\d+(\.\d{1,3})?$/.test(s)) return null;
    var parts = s.split(".");
    return parseInt(parts[0], 10) * 1000 + parseInt(((parts[1] || "") + "000").slice(0, 3), 10);
  }

  function toCents(text) { // "8,49" -> 849
    var s = String(text || "").replace(/[€\s]/g, "");
    if (s.indexOf(",") > -1 && s.indexOf(".") > -1) {
      var thousands = s.lastIndexOf(",") > s.lastIndexOf(".") ? "." : ",";
      s = s.split(thousands).join("");
    }
    s = s.replace(",", ".");
    if (!/^\d+(\.\d{1,2})?$/.test(s)) return null;
    var parts = s.split(".");
    return parseInt(parts[0], 10) * 100 + parseInt(((parts[1] || "") + "00").slice(0, 2), 10);
  }

  function format(cents) { return Math.floor(cents / 100) + "." + ("0" + (cents % 100)).slice(-2); }

  function update() {
    if (manual) return;
    var w = toMilli(weight.value), p = toCents(price.value);
    if (w === null || p === null || w <= 0 || p <= 0) return;
    var cents = Math.floor((w * p + 500) / 1000);
    total.value = format(cents);
    if (note) note.textContent = weight.value.trim() + " kg x EUR " + format(p) + " per kg = EUR " + format(cents) + ". You can change the amount.";
  }

  weight.addEventListener("input", update);
  price.addEventListener("input", update);
  total.addEventListener("input", function () {
    manual = total.value.trim() !== "";
    if (!manual) { if (note) note.textContent = defaultNote; update(); }
  });
  update();
})();
