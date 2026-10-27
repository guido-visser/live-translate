// Shared by the operator page and the settings page.
window.LT = (() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const pin = new URLSearchParams(location.search).get("pin");
  const withPin = (url) => (pin ? `${url}${url.includes("?") ? "&" : "?"}pin=${encodeURIComponent(pin)}` : url);

  let lang = "nl";
  try { lang = localStorage.getItem("lt.admin.lang") || "nl"; } catch { /* private mode */ }
  const listeners = [];

  const t = (key) => (window.ADMIN_I18N[lang] || window.ADMIN_I18N.en)[key] || window.ADMIN_I18N.en[key] || key;

  function applyTexts() {
    document.documentElement.lang = lang;
    document.querySelectorAll("[data-t]").forEach((el) => (el.textContent = t(el.dataset.t)));
    const select = $("uiLang");
    if (select) select.value = lang;
    listeners.forEach((fn) => fn());
  }

  function onLanguageChange(fn) {
    listeners.push(fn);
  }

  document.addEventListener("DOMContentLoaded", () => {
    const select = $("uiLang");
    if (select) {
      select.addEventListener("change", () => {
        lang = select.value;
        try { localStorage.setItem("lt.admin.lang", lang); } catch { /* ignore */ }
        applyTexts();
      });
    }
    document.querySelectorAll("a[data-pin-link]").forEach((a) => (a.href = withPin(a.getAttribute("href"))));
  });

  async function api(path, options = {}) {
    const res = await fetch(withPin(path), { headers: { "Content-Type": "application/json" }, ...options });
    if (!res.ok) {
      let detail = await res.text();
      try { detail = JSON.parse(detail).detail || detail; } catch { /* plain text */ }
      throw new Error(detail);
    }
    return res.json();
  }

  function saveSettings(settings) {
    return api("/api/admin/settings", { method: "POST", body: JSON.stringify(settings) });
  }

  return { $, withPin, t, api, saveSettings, applyTexts, onLanguageChange, get lang() { return lang; } };
})();
