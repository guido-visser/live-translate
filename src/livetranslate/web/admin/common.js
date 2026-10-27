// Shared by the operator page and the settings page.
window.LT = (() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const pin = new URLSearchParams(location.search).get("pin");
  const withPin = (url) => (pin ? `${url}${url.includes("?") ? "&" : "?"}pin=${encodeURIComponent(pin)}` : url);

  // The operator's own pick wins; otherwise the installer's (or Windows') language.
  let lang = window.LT_UI_LANG || "en";
  try { lang = localStorage.getItem("lt.admin.lang") || lang; } catch { /* private mode */ }
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

  // Language names in the interface language ("Russisch" in Dutch, "Russian" in English…).
  const BCP47 = { zt: "zh-Hant", zh: "zh-Hans" };
  let names = null;
  let namesLang = null;
  function langName(code, fallback) {
    if (namesLang !== lang) {
      try { names = new Intl.DisplayNames([lang], { type: "language" }); } catch { names = null; }
      namesLang = lang;
    }
    let name = null;
    try { name = names?.of(BCP47[code] || code); } catch { /* unknown code */ }
    if (!name || name === code) name = fallback || code;
    return name.charAt(0).toLocaleUpperCase(lang) + name.slice(1);
  }

  function sortByName(codes) {
    return [...codes].sort((a, b) => langName(a).localeCompare(langName(b), lang));
  }

  // Audio device and channel lists (main screen and settings).
  function fillDevices(select, devices, kind, wanted) {
    const list = devices.filter((d) => d.kind === (kind === "loopback" ? "loopback" : "device"));
    select.textContent = "";
    for (const d of list) {
      const o = new Option(`${d.name} — ${d.host_api}${d.is_default ? " ★" : ""}`, d.id);
      o.dataset.channels = d.channels;
      select.appendChild(o);
    }
    if (wanted && list.some((d) => d.id === wanted)) select.value = wanted;
    else if (list.length) select.value = (list.find((d) => d.is_default) || list[0]).id;
  }

  function fillChannels(select, channels, current) {
    select.textContent = "";
    select.appendChild(new Option(t("mix"), "0"));
    for (let i = 1; i <= channels; i++) select.appendChild(new Option(String(i), String(i)));
    select.value = [...select.options].some((o) => o.value === String(current)) ? String(current) : "0";
  }

  return {
    $, withPin, t, api, saveSettings, applyTexts, onLanguageChange, langName, sortByName, fillDevices, fillChannels,
    get lang() { return lang; },
  };
})();
