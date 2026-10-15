// Shared by every page: confirm prompts, toasts, and the approve form's
// eBay category search. No inline scripts anywhere (the site's content
// policy blocks them), so all behaviour is attached here.
(function () {
  "use strict";

  var csrfMeta = document.querySelector('meta[name="csrf"]');
  window.CSRF = csrfMeta ? csrfMeta.content : "";

  window.toast = function (text, bad) {
    var el = document.getElementById("toast");
    if (!el) return;
    el.textContent = text;
    el.className = "toast" + (bad ? " bad" : "");
    el.hidden = false;
    clearTimeout(window.toast._t);
    window.toast._t = setTimeout(function () { el.hidden = true; }, 3500);
  };

  // <form data-confirm="..."> asks before submitting.
  document.addEventListener("submit", function (e) {
    var form = e.target;
    var question = form.getAttribute("data-confirm");
    if (question && !window.confirm(question)) {
      e.preventDefault();
      return;
    }
    if (e.defaultPrevented) return;  // a page script (e.g. intake.js) is handling it
    var button = form.querySelector("button:not([type=button])");
    if (button) {
      // Stop double-taps from posting twice (the server re-checks too).
      setTimeout(function () { button.disabled = true; }, 0);
    }
  });

  // Approve form: search eBay categories and fill the select.
  var catSelect = document.getElementById("cat-select");
  var catSearch = document.getElementById("cat-search");
  var catBtn = document.getElementById("cat-search-btn");
  function searchCategories(auto) {
    var q = (catSearch.value || "").trim();
    if (!q) return;
    catBtn.disabled = true;
    fetch("/api/categories?q=" + encodeURIComponent(q), { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : []; })
      .then(function (rows) {
        var keep = Array.prototype.slice.call(catSelect.options).filter(function (o) { return !o.dataset.found; });
        catSelect.innerHTML = "";
        rows.forEach(function (row, i) {
          var opt = document.createElement("option");
          opt.value = row.id;
          opt.textContent = row.path + " (" + row.id + ")";
          opt.dataset.found = "1";
          if (i === 0) opt.selected = true;
          catSelect.appendChild(opt);
        });
        keep.forEach(function (o) { catSelect.appendChild(o); });
        if (!rows.length && !auto) window.toast("No eBay categories matched - try other words.", true);
      })
      .catch(function () { window.toast("Category search failed.", true); })
      .finally(function () { catBtn.disabled = false; });
  }
  if (catSelect && catSearch && catBtn) {
    catBtn.addEventListener("click", function () { searchCategories(false); });
    catSearch.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); searchCategories(false); }
    });
    if (catSelect.dataset.suggest) searchCategories(true);  // pre-pick the AI's category guess
  }
})();
