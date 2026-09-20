// Tooltips for the charts: hover, keyboard focus or a tap on a bar shows its value. The chart is drawn
// server-side and every value is also in the table beside it, so this only adds convenience.
(function () {
  var tip = null, hideTimer = null;

  function ensure() {
    if (tip) return tip;
    tip = document.createElement("div");
    tip.className = "viz-tip";
    tip.setAttribute("role", "status");
    tip.hidden = true;
    document.body.appendChild(tip);
    return tip;
  }

  function hide() { if (tip) tip.hidden = true; }

  function show(mark) {
    var el = ensure();
    el.textContent = mark.getAttribute("data-tip");
    el.hidden = false;
    var box = mark.getBoundingClientRect();
    var w = el.offsetWidth, h = el.offsetHeight;
    var left = Math.min(Math.max(8, box.left + box.width / 2 - w / 2), window.innerWidth - w - 8);
    var top = box.top - h - 8 < 8 ? box.bottom + 8 : box.top - h - 8;
    el.style.left = left + "px";
    el.style.top = top + "px";
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hide, 4000); // touch has no "leave" event
  }

  function markOf(target) { return target.closest ? target.closest(".mark") : null; }

  document.addEventListener("pointerover", function (e) { var m = markOf(e.target); if (m && e.pointerType === "mouse") show(m); });
  document.addEventListener("pointerout", function (e) { if (markOf(e.target) && e.pointerType === "mouse") hide(); });
  document.addEventListener("pointerdown", function (e) { var m = markOf(e.target); if (m) show(m); else hide(); });
  document.addEventListener("focusin", function (e) { var m = markOf(e.target); if (m) show(m); });
  document.addEventListener("focusout", hide);
  window.addEventListener("scroll", hide, { passive: true });
})();
