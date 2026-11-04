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
    document.querySelectorAll("[data-t-name]").forEach((el) => (el.textContent = t(el.dataset.tName).replace("{name}", el.dataset.name)));
    document.querySelectorAll("[data-t-placeholder]").forEach((el) => (el.placeholder = t(el.dataset.tPlaceholder)));
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
    const body = JSON.stringify(settings);
    // keepalive: it still arrives when the page is being left. Browsers refuse it above 64 KB.
    return api("/api/admin/settings", { method: "POST", body, keepalive: body.length < 60000 });
  }

  // Language names in the interface language ("Duits" in Dutch, "German" in English…).
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

  // A language's name in itself ("Deutsch"), for people looking for their own language.
  function autonym(code) {
    try {
      const name = new Intl.DisplayNames([BCP47[code] || code], { type: "language" }).of(BCP47[code] || code);
      return name && name !== code ? name.charAt(0).toLocaleUpperCase(code) + name.slice(1) : "";
    } catch {
      return "";
    }
  }

  const folded = (text) => text.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase();

  // Found under its name here, in English, in itself, or its code ("duits", "german", "deutsch", "de").
  function languageMatches(code, query) {
    const wanted = folded(query.trim());
    if (!wanted) return true;
    let english = "";
    try { english = new Intl.DisplayNames(["en"], { type: "language" }).of(BCP47[code] || code); } catch { /* unknown code */ }
    return [langName(code), autonym(code), english, code].some((name) => folded(name).includes(wanted));
  }

  // A form with a capital only counts with that capital, and a name can follow a word in a sentence
  // with its first word in lowercase ("in the Grace Church"). Works for any language's articles.
  function nameForms(name) {
    const [first, ...rest] = name.split(/\s+/);
    const lower = first.toLocaleLowerCase();
    return rest.length && lower !== first ? [name, [lower, ...rest].join(" ")] : [name];
  }

  // Glossary rows with the English word the same as the name: that keeps a name from being translated.
  // One row for the full name and one for all the other names, so a spelling filled in for another
  // alphabet applies to each of them. Comma-separated, like everywhere in the glossary.
  function protectNames(glossary, main, fullName, otherNames) {
    const known = new Set(glossary.filter((e) => e.lang === main).flatMap((e) => e.term.split(",").map((f) => f.trim())));
    const split = (names) => names.split(",").map((n) => n.trim()).filter((n) => n && !known.has(n));
    for (const names of [split(fullName), split(otherNames)]) {
      if (names.length) glossary.push({ lang: main, term: [...new Set(names.flatMap(nameForms))].join(", "), translations: { en: names[0] } });
    }
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

  // The cross of the app window, called from window.py. Whatever goes wrong, the window can still close:
  // the app then stays in the tray.
  let closeDialog = null;

  async function closeWith(action, remember) {
    try {
      await api("/api/admin/close", { method: "POST", body: JSON.stringify({ action, remember }) });
    } finally {
      window.pywebview.api.close_window();
    }
  }

  function buildCloseDialog() {
    const add = (parent, tag, key, className) => {
      const el = parent.appendChild(document.createElement(tag));
      if (key) el.textContent = t(key);
      if (className) el.className = className;
      return el;
    };
    const dialog = document.body.appendChild(document.createElement("dialog"));
    dialog.className = "close-dialog";
    add(dialog, "h2", "closeTitle");
    add(dialog, "p", "closeStopHelp");
    add(dialog, "p", "closeTrayHelp");
    const label = add(dialog, "label", null, "check");
    const remember = add(label, "input");
    remember.type = "checkbox";
    label.append(" ");
    add(label, "span", "closeRemember");
    add(dialog, "p", "closeRememberHelp", "muted small");
    const buttons = add(dialog, "div", null, "dialog-buttons");
    const button = (key, className, onClick) => {
      const el = add(buttons, "button", key, className);
      el.type = "button";
      el.addEventListener("click", onClick);
      return el;
    };
    const choose = (action) => () => {
      buttons.querySelectorAll("button").forEach((el) => (el.disabled = true));
      closeWith(action, remember.checked);
    };
    button("closeStop", "danger", choose("exit"));
    button("closeTray", "primary", choose("tray")).autofocus = true;  // Enter never stops a service
    button("closeCancel", "", () => dialog.close());
    return dialog;
  }

  async function askClose() {
    if (closeDialog?.open) {
      window.pywebview.api.show_window();
      return;
    }
    let action = "tray";
    document.activeElement?.blur?.();  // what is being typed counts too
    try {
      await window.LT.beforeClosing?.();
      action = (await api("/api/admin/close")).action;
    } catch {
      window.pywebview.api.close_window();
      return;
    }
    if (action !== "ask") {
      closeWith(action, false);
      return;
    }
    closeDialog?.remove();  // the language can have changed
    closeDialog = buildCloseDialog();
    window.pywebview.api.show_window();  // closing from the taskbar works while minimized too
    closeDialog.showModal();
  }

  return {
    askClose,
    $, withPin, t, api, saveSettings, applyTexts, onLanguageChange, langName, sortByName, fillDevices, fillChannels,
    autonym, languageMatches, protectNames,
    get lang() { return lang; },
  };
})();
