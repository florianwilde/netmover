// NetMover – kleine Helfer. Keine Inline-Skripte (CSP: script-src 'self').
(function () {
  "use strict";
  var meta = document.querySelector('meta[name="csrf-token"]');
  var csrf = meta ? meta.getAttribute("content") : "";

  // CSRF-Token für alle HTMX-Anfragen
  document.addEventListener("htmx:configRequest", function (e) {
    e.detail.headers["X-CSRF-Token"] = csrf;
  });

  // Rückfrage vor gefährlichen Aktionen: <form data-confirm="…">
  document.addEventListener("submit", function (e) {
    var msg = e.target.getAttribute && e.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) { e.preventDefault(); }
  }, true);

  // Felder je nach Auswahl ein-/ausblenden: data-show-for="select-name:wert1 wert2"
  function refresh(form) {
    form.querySelectorAll("[data-show-for]").forEach(function (el) {
      var spec = el.getAttribute("data-show-for").split(":");
      var ctl = form.querySelector('[name="' + spec[0] + '"]:checked') ||
                form.querySelector('select[name="' + spec[0] + '"]');
      var value = ctl ? ctl.value : "";
      var show = spec[1].split(" ").indexOf(value) !== -1;
      el.hidden = !show;
    });
  }
  document.querySelectorAll("form[data-dynamic]").forEach(function (form) {
    form.addEventListener("change", function () { refresh(form); });
    refresh(form);
  });

  // Öffentlichen Schlüssel kopieren
  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest("[data-copy]");
    if (!btn) return;
    var src = document.getElementById(btn.getAttribute("data-copy"));
    if (src && navigator.clipboard) {
      navigator.clipboard.writeText(src.textContent.trim()).then(function () {
        btn.textContent = "kopiert ✓";
      });
    }
  });
})();
