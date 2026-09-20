// Shrinks photos in the browser before upload: a 12 MP iPhone photo becomes ~400 KB.
// The server re-validates and re-encodes everything anyway; this only saves bandwidth.
(function () {
  var MAX_EDGE = 2000;
  var form = document.querySelector("form[data-resize-form]");
  if (!form || !window.DataTransfer || !window.HTMLCanvasElement) return;

  function shrink(file) {
    if (!/^image\/(jpeg|png|webp)$/.test(file.type)) return Promise.resolve(file); // PDFs pass through
    return createImageBitmap(file, { imageOrientation: "from-image" }).then(function (bitmap) {
      var scale = Math.min(1, MAX_EDGE / Math.max(bitmap.width, bitmap.height));
      var canvas = document.createElement("canvas");
      canvas.width = Math.round(bitmap.width * scale);
      canvas.height = Math.round(bitmap.height * scale);
      canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      return new Promise(function (resolve) {
        canvas.toBlob(function (blob) {
          resolve(blob ? new File([blob], file.name.replace(/\.\w+$/, "") + ".jpg", { type: "image/jpeg" }) : file);
        }, "image/jpeg", 0.85);
      });
    }).catch(function () { return file; });
  }

  var busy = false;
  form.addEventListener("submit", function (event) {
    if (busy) return;
    var input = form.querySelector("input[type=file]");
    if (!input || !input.files.length) return;
    event.preventDefault();
    busy = true;
    form.querySelector("button[type=submit]").disabled = true;
    Promise.all(Array.prototype.map.call(input.files, shrink)).then(function (files) {
      var transfer = new DataTransfer();
      files.forEach(function (f) { transfer.items.add(f); });
      input.files = transfer.files;
    }).catch(function () { /* upload the originals */ }).then(function () {
      form.submit();
    });
  });
})();
