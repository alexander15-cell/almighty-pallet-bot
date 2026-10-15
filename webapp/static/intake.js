// Phone intake: preview the photos, shrink them on the phone (max 1600 px,
// JPEG) so uploads are quick on mobile data, then post them with the form.
// Without JavaScript the plain form still works - it just sends the
// originals.
(function () {
  "use strict";
  var form = document.getElementById("intake-form");
  if (!form) return;
  var preview = document.getElementById("photo-preview");
  var MAX_EDGE = 1600, MAX_PHOTOS = 10;
  var photos = [];  // Blobs, in the order they'll be uploaded

  function shrink(file) {
    return new Promise(function (resolve) {
      var url = URL.createObjectURL(file);
      var img = new Image();
      img.onload = function () {
        var scale = Math.min(1, MAX_EDGE / Math.max(img.width, img.height));
        var canvas = document.createElement("canvas");
        canvas.width = Math.round(img.width * scale);
        canvas.height = Math.round(img.height * scale);
        canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
        URL.revokeObjectURL(url);
        canvas.toBlob(function (blob) { resolve(blob || file); }, "image/jpeg", 0.85);
      };
      img.onerror = function () { URL.revokeObjectURL(url); resolve(file); };
      img.src = url;
    });
  }

  function render() {
    preview.innerHTML = "";
    photos.forEach(function (blob, i) {
      var div = document.createElement("div");
      div.className = "photo";
      var img = document.createElement("img");
      img.src = URL.createObjectURL(blob);
      var n = document.createElement("span");
      n.className = "n";
      n.textContent = String(i + 1);
      var tools = document.createElement("div");
      tools.className = "tools";
      var del = document.createElement("button");
      del.type = "button";
      del.textContent = "✕";
      del.addEventListener("click", function () { photos.splice(i, 1); render(); });
      tools.appendChild(del);
      div.appendChild(img); div.appendChild(n); div.appendChild(tools);
      preview.appendChild(div);
    });
  }

  Array.prototype.forEach.call(form.querySelectorAll("[data-photo-input]"), function (input) {
    input.addEventListener("change", function () {
      var files = Array.prototype.slice.call(input.files || []);
      input.value = "";
      Promise.all(files.map(shrink)).then(function (blobs) {
        photos = photos.concat(blobs).slice(0, MAX_PHOTOS);
        render();
      });
    });
  });

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    if (!photos.length) { window.toast("Add at least one photo.", true); return; }
    var data = new FormData();
    Array.prototype.forEach.call(form.elements, function (el) {
      if (el.name && el.type !== "file" && el.name !== "photos") data.append(el.name, el.value);
    });
    photos.forEach(function (blob, i) { data.append("photos", blob, "photo_" + i + ".jpg"); });
    var button = document.getElementById("intake-submit");
    button.disabled = true;
    button.textContent = "Uploading…";
    fetch(form.action, { method: "POST", body: data, credentials: "same-origin" })
      .then(function (r) { window.location = r.url || "/intake"; })
      .catch(function () {
        button.disabled = false;
        button.textContent = "Log item ➜";
        window.toast("Upload failed - check your connection and try again.", true);
      });
  });
})();
