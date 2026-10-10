// NetMover – kleine Helfer. Keine Inline-Skripte (CSP: script-src 'self').
(function () {
  "use strict";
  document.documentElement.classList.add("js");

  // Menü auf dem Handy auf-/zuklappen
  var toggle = document.querySelector(".menu-toggle");
  var sidebar = document.getElementById("sidebar");
  if (toggle && sidebar) {
    toggle.addEventListener("click", function (e) {
      e.stopPropagation();
      var open = sidebar.classList.toggle("open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
    document.addEventListener("click", function (e) {
      if (sidebar.classList.contains("open") && !sidebar.contains(e.target)) {
        sidebar.classList.remove("open");
        toggle.setAttribute("aria-expanded", "false");
      }
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { sidebar.classList.remove("open"); toggle.setAttribute("aria-expanded", "false"); }
    });
  }
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
    // data-show-for="feld:wert1 wert2" – mehrere Bedingungen mit „|“ (ODER)
    form.querySelectorAll("[data-show-for]").forEach(function (el) {
      el.hidden = !el.getAttribute("data-show-for").split("|").some(function (cond) {
        var spec = cond.split(":");
        var ctl = form.querySelector('[name="' + spec[0] + '"]:checked') ||
                  form.querySelector('select[name="' + spec[0] + '"]');
        return spec[1].split(" ").indexOf(ctl ? ctl.value : "") !== -1;
      });
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
