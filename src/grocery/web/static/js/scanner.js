// Barcode scanning with the phone camera. Uses the browser's BarcodeDetector when it exists
// (Chrome/Android) and the vendored ZXing library otherwise (iOS Safari).
(function () {
  var GA = (window.GA = window.GA || {});

  function checkDigitOk(digits) {
    if (!/^\d+$/.test(digits) || [8, 12, 13, 14].indexOf(digits.length) < 0) return false;
    var body = digits.slice(0, -1).split("").reverse(), sum = 0;
    for (var i = 0; i < body.length; i++) sum += parseInt(body[i], 10) * (i % 2 === 0 ? 3 : 1);
    return (10 - (sum % 10)) % 10 === parseInt(digits.slice(-1), 10);
  }

  var stream = null, timer = null, running = false;

  function stop() {
    running = false;
    if (timer) { clearTimeout(timer); timer = null; }
    if (stream) { stream.getTracks().forEach(function (t) { t.stop(); }); stream = null; }
  }

  function makeDecoder() {
    if ("BarcodeDetector" in window) {
      var detector = new window.BarcodeDetector({ formats: ["ean_13", "ean_8", "upc_a", "upc_e"] });
      return function (video) {
        return detector.detect(video).then(function (codes) { return codes.length ? codes[0].rawValue : null; });
      };
    }
    if (!window.ZXing) return null;
    var hints = new Map();
    hints.set(ZXing.DecodeHintType.POSSIBLE_FORMATS, [
      ZXing.BarcodeFormat.EAN_13, ZXing.BarcodeFormat.EAN_8, ZXing.BarcodeFormat.UPC_A, ZXing.BarcodeFormat.UPC_E,
    ]);
    hints.set(ZXing.DecodeHintType.TRY_HARDER, true);
    var reader = new ZXing.MultiFormatReader();
    reader.setHints(hints);
    var canvas = document.createElement("canvas");
    var ctx = canvas.getContext("2d", { willReadFrequently: true });
    return function (video) {
      var w = Math.min(video.videoWidth, 900), h = Math.round(video.videoHeight * (w / video.videoWidth));
      if (!w || !h) return Promise.resolve(null);
      canvas.width = w; canvas.height = h;
      ctx.drawImage(video, 0, 0, w, h);
      try {
        var bitmap = new ZXing.BinaryBitmap(new ZXing.HybridBinarizer(new ZXing.HTMLCanvasElementLuminanceSource(canvas)));
        return Promise.resolve(reader.decode(bitmap).getText());
      } catch (e) {
        return Promise.resolve(null); // nothing readable in this frame
      }
    };
  }

  // start(videoElement, onCode, onError): calls onCode(digits) once with a barcode whose check digit is valid.
  function start(video, onCode, onError) {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      onError("This browser cannot use the camera here. Type the number instead.");
      return;
    }
    var decode = makeDecoder();
    if (!decode) { onError("The barcode reader did not load. Type the number instead."); return; }
    navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" }, width: { ideal: 1280 } }, audio: false })
      .then(function (s) {
        stream = s; running = true;
        video.srcObject = s;
        return video.play();
      })
      .then(function () {
        (function loop() {
          if (!running) return;
          decode(video).then(function (text) {
            if (!running) return;
            var digits = text ? String(text).replace(/\D/g, "") : "";
            if (digits && checkDigitOk(digits)) { stop(); if (navigator.vibrate) navigator.vibrate(60); onCode(digits); return; }
            timer = setTimeout(loop, 150);
          }).catch(function () { timer = setTimeout(loop, 300); });
        })();
      })
      .catch(function (err) {
        stop();
        onError(err && err.name === "NotAllowedError"
          ? "Camera permission was denied. Allow the camera in Settings, or type the number."
          : "The camera could not be started. Type the number instead.");
      });
  }

  GA.scanner = { start: start, stop: stop, checkDigitOk: checkDigitOk };
})();
