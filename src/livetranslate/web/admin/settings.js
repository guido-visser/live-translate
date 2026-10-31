(() => {
  "use strict";

  const { $, t, api, withPin, langName, sortByName } = window.LT;
  const form = $("settingsForm");

  let state = null;
  let devices = [];
  let vmixState = null; // inputs reported by vMix
  let glossary = []; // every glossary entry (all spoken languages), edited in place
  let dirty = false;

  // -- tabs --------------------------------------------------------------------------

  const tabs = [...document.querySelectorAll("[role=tab]")];

  function showTab(name) {
    if (!tabs.some((b) => b.dataset.tab === name)) name = tabs[0].dataset.tab;
    tabs.forEach((b) => {
      const active = b.dataset.tab === name;
      b.classList.toggle("active", active);
      b.setAttribute("aria-selected", String(active));
    });
    document.querySelectorAll(".tab-panel").forEach((p) => (p.hidden = p.dataset.panel !== name));
    history.replaceState(null, "", `${location.pathname}${location.search}#${name}`);
  }

  tabs.forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

  // -- audio ---------------------------------------------------------------------------

  const kind = () => form.querySelector("input[name=kind]:checked")?.value || "device";

  function renderDevices() {
    window.LT.fillDevices($("device"), devices, kind(), $("device").value || state.settings.audio.device);
    renderChannels();
  }

  function renderChannels() {
    const channels = Number($("device").selectedOptions[0]?.dataset.channels || 2);
    window.LT.fillChannels($("channel"), channels, $("channel").value || state.settings.audio.channel);
  }

  function renderKind() {
    $("deviceRow").hidden = kind() === "file";
    $("channelRow").hidden = kind() === "file";
    $("fileRow").hidden = kind() !== "file";
    renderDevices();
  }

  form.querySelectorAll("input[name=kind]").forEach((r) => r.addEventListener("change", renderKind));
  $("device").addEventListener("change", renderChannels);
  $("gain").addEventListener("input", () => ($("gainText").textContent = `${$("gain").value}×`));
  $("refreshDevices").addEventListener("click", loadDevices);

  $("fileUpload").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const res = await fetch(withPin(`/api/admin/upload?name=${encodeURIComponent(file.name)}`), { method: "POST", body: file });
    $("filePath").value = (await res.json()).path;
    markDirty();
  });

  async function loadDevices() {
    devices = await api("/api/admin/devices");
    if (state) renderDevices();
  }

  // -- languages -------------------------------------------------------------------------

  // Checkbox list of languages, named and sorted in the interface language. Keeps
  // unsaved ticks when it is redrawn (e.g. after switching the interface language).
  function checkList(box, codes, saved, extra = () => "") {
    const drawn = box.querySelectorAll("input:not(:disabled)").length > 0;
    const chosen = drawn ? new Set([...box.querySelectorAll("input:checked:not(:disabled)")].map((i) => i.value)) : saved;
    box.textContent = "";
    for (const code of sortByName(codes)) {
      const label = document.createElement("label");
      label.className = "check";
      label.innerHTML = '<input type="checkbox"> <span></span>';
      label.querySelector("input").value = code;
      label.querySelector("input").checked = chosen.has(code);
      label.querySelector("span").textContent = `${langName(code)}${extra(code)}`;
      box.appendChild(label);
    }
  }

  function renderTargets() {
    const s = state.settings;
    const main = $("sourceLang").value;
    const codes = state.target_languages.length ? state.target_languages.map((l) => l.code) : s.target_languages;
    const tts = new Set(state.tts_languages);
    checkList($("targets"), codes.filter((c) => c !== main), new Set(s.target_languages), (c) => (tts.has(c) ? " 🔊" : ""));
  }

  function renderQuick() {
    const main = $("sourceLang").value;
    const box = $("quick");
    const codes = state.quick_language_options.map((l) => l.code).filter((c) => c !== main);
    checkList(box, codes, new Set(state.settings.quick_languages));
    box.querySelector("input:disabled")?.closest("label").remove();
    const fixed = document.createElement("label");
    fixed.className = "check";
    fixed.innerHTML = '<input type="checkbox" checked disabled> <span></span>';
    fixed.querySelector("span").textContent = `${langName(main)} (${t("quickMain")})`;
    box.prepend(fixed);
  }

  function renderSourceLanguages() {
    const src = $("sourceLang");
    const wanted = src.value || state.settings.source_language;
    const codes = state.source_languages.length ? state.source_languages.map((l) => l.code) : [wanted];
    src.textContent = "";
    for (const code of sortByName(codes)) src.appendChild(new Option(langName(code), code));
    src.value = wanted;
  }

  $("sourceLang").addEventListener("change", () => { renderTargets(); renderQuick(); renderVmixLanguages(); renderGlossary(); });
  $("engine").addEventListener("change", () => ($("nllbRows").hidden = $("engine").value !== "nllb"));

  // -- glossary --------------------------------------------------------------------------

  // Whisper reads roughly the first 600 characters of the word list.
  const HOTWORD_CHARS = 600;

  function glossaryInput(value, lang, placeholder, onInput) {
    const td = document.createElement("td");
    const input = document.createElement("input");
    input.type = "text";
    input.lang = lang;
    input.value = value;
    input.placeholder = placeholder;
    input.addEventListener("input", () => { onInput(input.value); checkGlossaryLength(); });
    td.appendChild(input);
    return td;
  }

  // One row per word in the main language. English comes first: it also steers the
  // other listener languages, whose columns only override it.
  function renderGlossary() {
    const main = $("sourceLang").value;
    const others = [...$("targets").querySelectorAll("input:checked")].map((i) => i.value).filter((c) => c !== "en");
    const langs = [...(main === "en" ? [] : ["en"]), ...sortByName(others)];
    const head = $("glossaryHead");
    head.textContent = "";
    for (const text of [`${t("glossaryWord")} (${langName(main)})`, ...langs.map((c) => langName(c)), ""]) {
      const th = document.createElement("th");
      th.textContent = text;
      head.appendChild(th);
    }
    const body = $("glossaryRows");
    body.textContent = "";
    const rows = glossary.filter((e) => e.lang === main);
    for (const entry of rows) {
      const tr = document.createElement("tr");
      tr.appendChild(glossaryInput(entry.term, main, t("glossaryWordHint"), (v) => (entry.term = v)));
      // Empty override boxes say what they fall back to: the English word, or a normal translation.
      const overrides = [];
      const showFallback = () => {
        const text = entry.translations.en?.trim() ? t("glossaryViaEnglish") : t("glossaryAuto");
        overrides.forEach((input) => (input.placeholder = text));
      };
      for (const code of langs) {
        const english = code === "en";
        const td = glossaryInput(entry.translations[code] || "", code, english ? t("glossaryEnglishHint") : "", (v) => {
          entry.translations[code] = v;
          if (english) showFallback();
        });
        if (!english) overrides.push(td.firstChild);
        tr.appendChild(td);
      }
      showFallback();
      const td = document.createElement("td");
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "remove";
      remove.textContent = "✕";
      remove.title = remove.ariaLabel = t("glossaryRemove");
      remove.addEventListener("click", () => {
        glossary.splice(glossary.indexOf(entry), 1);
        markDirty();
        renderGlossary();
      });
      td.appendChild(remove);
      tr.appendChild(td);
      body.appendChild(tr);
    }
    $("glossaryEmpty").hidden = rows.length > 0;
    $("glossarySample").placeholder = t("glossarySample");
    checkGlossaryLength();
  }

  function checkGlossaryLength() {
    const main = $("sourceLang").value;
    const words = glossary.filter((e) => e.lang === main).flatMap((e) => e.term.split(",").map((w) => w.trim()).filter(Boolean));
    $("glossaryLong").hidden = [...new Set(words)].join(", ").length <= HOTWORD_CHARS;
  }

  function collectGlossary() {
    return glossary
      .filter((e) => e.term.trim())
      .map((e) => ({
        lang: e.lang,
        term: e.term.trim(),
        translations: Object.fromEntries(
          Object.entries(e.translations).map(([code, text]) => [code, text.trim()]).filter(([, text]) => text),
        ),
      }));
  }

  $("targets").addEventListener("change", renderGlossary);
  $("glossaryAdd").addEventListener("click", () => {
    glossary.push({ lang: $("sourceLang").value, term: "", translations: {} });
    markDirty();
    renderGlossary();
    const inputs = $("glossaryRows").querySelectorAll("tr:last-child input");
    inputs[0]?.focus();
  });

  async function tryGlossary() {
    const text = $("glossarySample").value.trim();
    const box = $("glossaryResult");
    if (!text) return;
    if (dirty) await save();
    box.textContent = "";
    try {
      const result = await api("/api/admin/glossary/test", { method: "POST", body: JSON.stringify({ text }) });
      for (const [code, { text: translated, back }] of Object.entries(result)) {
        const dt = document.createElement("dt");
        dt.textContent = langName(code);
        const dd = document.createElement("dd");
        const main = document.createElement("div");
        main.lang = code;
        main.textContent = translated;
        dd.appendChild(main);
        if (back) {
          const check = document.createElement("div");
          check.className = "muted small";
          check.lang = "en";
          check.textContent = `${t("glossaryBack")}: ${back}`;
          dd.appendChild(check);
        }
        box.append(dt, dd);
      }
    } catch (err) {
      const dd = document.createElement("dd");
      dd.className = "error";
      dd.textContent = err.message;
      box.append(dd);
    }
  }

  $("glossaryTry").addEventListener("click", tryGlossary);
  $("glossarySample").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); tryGlossary(); }
  });

  // -- vMix ------------------------------------------------------------------------------

  function renderVmixInputs() {
    const select = $("vmixInput");
    const wanted = select.value || state.settings.vmix.input;
    select.textContent = "";
    select.appendChild(new Option("—", ""));
    for (const input of vmixState?.inputs || []) select.appendChild(new Option(`${input.number}. ${input.title}`, input.key));
    if (wanted && ![...select.options].some((o) => o.value === wanted)) {
      select.appendChild(new Option(`${t("vmixSavedInput")} (${wanted.slice(0, 8)}…)`, wanted));
    }
    select.value = wanted || "";
    renderVmixFields();
  }

  function renderVmixLanguages() {
    const select = $("vmixLanguage");
    const wanted = select.value || state.settings.vmix.language || "";
    const main = $("sourceLang").value;
    select.textContent = "";
    select.appendChild(new Option(`${langName(main)} (${t("quickMain")})`, ""));
    for (const code of sortByName(state.settings.target_languages.filter((c) => c !== main))) {
      select.appendChild(new Option(langName(code), code));
    }
    if (wanted && ![...select.options].some((o) => o.value === wanted)) select.appendChild(new Option(langName(wanted), wanted));
    select.value = wanted === main ? "" : wanted;
  }

  function renderOverlayChannels() {
    // vMix reports how many overlay channels it has; fall back to the classic four.
    const select = $("vmixOverlay");
    const wanted = select.value || String(state.settings.vmix.overlay);
    const channels = Object.keys(vmixState?.overlays || {}).map(Number).filter((n) => n > 0);
    const list = channels.length ? channels : [1, 2, 3, 4];
    if (!list.includes(Number(wanted))) list.push(Number(wanted));
    select.textContent = "";
    for (const n of list.sort((a, b) => a - b)) select.appendChild(new Option(String(n), String(n)));
    select.value = wanted;
  }

  function renderVmixFields() {
    const select = $("vmixField");
    const wanted = select.value || state.settings.vmix.field;
    const input = (vmixState?.inputs || []).find((i) => i.key === $("vmixInput").value);
    const fields = input ? input.fields : wanted ? [wanted] : [];
    select.textContent = "";
    for (const f of fields) select.appendChild(new Option(f, f));
    if (fields.includes(wanted)) select.value = wanted;
  }

  function showVmixStatus(ok, message) {
    const el = $("vmixStatus");
    el.className = `small ${ok ? "vmix-ok" : "vmix-error"}`;
    el.textContent = ok ? t("vmixOk") : `${t("vmixError")}: ${message}`;
  }

  async function refreshVmix() {
    if ($("vmixUrl").value.trim() !== state.settings.vmix.url) await save();
    try {
      vmixState = await api("/api/admin/vmix/inputs");
      showVmixStatus(true);
    } catch (err) {
      vmixState = null;
      showVmixStatus(false, err.message);
    }
    renderVmixInputs();
    renderOverlayChannels();
  }

  $("vmixRefresh").addEventListener("click", refreshVmix);
  $("vmixInput").addEventListener("change", () => { $("vmixField").value = ""; renderVmixFields(); });
  $("vmixTest").addEventListener("click", async () => {
    if (dirty) await save();
    try {
      await api("/api/admin/vmix/test", { method: "POST", body: JSON.stringify({ text: t("vmixTestText") }) });
      showVmixStatus(true);
    } catch (err) {
      showVmixStatus(false, err.message);
    }
  });

  // -- network ---------------------------------------------------------------------------

  function renderAddresses() {
    const select = $("address");
    const current = state.settings.public_host;
    select.textContent = "";
    state.addresses.forEach((ip, i) => select.appendChild(new Option(ip, i === 0 ? "" : ip)));
    if (current && !state.addresses.includes(current)) select.appendChild(new Option(current, current));
    select.appendChild(new Option(t("custom"), "__custom"));
    select.value = current && state.addresses[0] !== current ? current : "";
  }

  $("address").addEventListener("change", (e) => {
    if (e.target.value !== "__custom") return;
    const value = prompt(t("address"), state.settings.public_host || "");
    if (value) {
      e.target.add(new Option(value, value), e.target.options.length - 1);
      e.target.value = value;
    } else e.target.value = "";
  });

  // -- fill, collect, save -----------------------------------------------------------------

  function fill() {
    const s = state.settings;
    form.querySelector(`input[name=kind][value=${s.audio.kind}]`).checked = true;
    $("filePath").value = s.audio.file_path;
    $("gain").value = s.audio.gain;
    $("gainText").textContent = `${s.audio.gain}×`;
    $("model").textContent = "";
    for (const m of state.models) $("model").appendChild(new Option(m, m));
    $("model").value = s.asr.model;
    $("asrDevice").value = s.asr.device;

    $("sourceLang").textContent = "";
    renderSourceLanguages();
    $("targets").textContent = "";
    $("quick").textContent = "";
    renderTargets();
    renderQuick();
    $("tts").checked = s.tts_enabled;
    $("engine").value = s.translation.engine;
    $("nllbModel").value = s.translation.nllb_model;
    $("nllbDevice").value = s.translation.nllb_device;
    $("nllbRows").hidden = s.translation.engine !== "nllb";
    glossary = structuredClone(s.glossary);
    renderGlossary();

    $("vmixEnabled").checked = s.vmix.enabled;
    $("vmixUrl").value = s.vmix.url;
    renderVmixLanguages();
    renderOverlayChannels();
    $("vmixMaxChars").value = s.vmix.max_chars;
    $("vmixAuto").checked = s.vmix.auto_overlay;
    renderVmixInputs();

    renderAddresses();
    $("wifi").value = s.wifi_name;
    $("pin").value = s.admin_pin;
    $("port").value = s.port;
    $("checkUpdates").checked = s.check_updates;
    renderUpdate();
    renderKind();
    dirty = false;
  }

  function collect() {
    const s = structuredClone(state.settings);
    s.audio.kind = kind();
    if (s.audio.kind !== "file") s.audio.device = $("device").value || null;
    s.audio.channel = Number($("channel").value || 0);
    s.audio.file_path = $("filePath").value.trim();
    s.audio.gain = Number($("gain").value);
    s.asr.model = $("model").value;
    s.asr.device = $("asrDevice").value;

    s.source_language = $("sourceLang").value;
    s.target_languages = [...$("targets").querySelectorAll("input:checked")].map((i) => i.value);
    s.quick_languages = [...$("quick").querySelectorAll("input:checked:not(:disabled)")].map((i) => i.value);
    s.tts_enabled = $("tts").checked;
    s.translation.engine = $("engine").value;
    s.translation.nllb_model = $("nllbModel").value;
    s.translation.nllb_device = $("nllbDevice").value;
    s.glossary = collectGlossary();

    s.vmix.enabled = $("vmixEnabled").checked;
    s.vmix.url = $("vmixUrl").value.trim() || "http://127.0.0.1:8088";
    s.vmix.input = $("vmixInput").value;
    s.vmix.field = $("vmixField").value;
    s.vmix.overlay = Number($("vmixOverlay").value);
    s.vmix.language = $("vmixLanguage").value;
    s.vmix.max_chars = Math.max(20, Math.min(1000, Number($("vmixMaxChars").value) || 160));
    s.vmix.auto_overlay = $("vmixAuto").checked;

    s.public_host = $("address").value === "__custom" ? "" : $("address").value;
    s.wifi_name = $("wifi").value.trim();
    s.admin_pin = $("pin").value.trim();
    s.check_updates = $("checkUpdates").checked;
    return s;
  }

  // -- updates (installing happens on the main screen) -------------------------------------

  function renderUpdate() {
    const u = state.update;
    $("version").textContent = u.current;
    const el = $("updateStatus");
    el.textContent = "";
    if (u.state === "checking") el.textContent = "…";
    else if (u.available) {
      el.textContent = `${t("updateAvailable").replace("{v}", u.latest)} `;
      const a = document.createElement("a");
      a.href = u.url;
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = t("updateWhatsNew");
      el.appendChild(a);
    } else if (u.state === "error") el.textContent = t("checkFailed");
    else if (u.latest) el.textContent = t("upToDate");
  }

  $("checkNow").addEventListener("click", async () => {
    $("checkNow").disabled = true;
    state.update = { ...state.update, state: "checking" };
    renderUpdate();
    try {
      state.update = await api("/api/admin/update/check", { method: "POST" });
    } finally {
      $("checkNow").disabled = false;
      renderUpdate();
    }
  });

  function markDirty() {
    dirty = true;
    $("saved").hidden = true;
  }

  async function save() {
    $("saveError").hidden = true;
    try {
      state = await window.LT.saveSettings(collect());
    } catch (err) {
      $("saveError").textContent = err.message;
      $("saveError").hidden = false;
      return;
    }
    fill();
    $("saved").textContent = t("saved");
    $("saved").hidden = false;
  }

  // The test sentence is not a setting.
  const isSetting = (e) => e.target.id !== "glossarySample";
  form.addEventListener("input", (e) => isSetting(e) && markDirty());
  form.addEventListener("change", (e) => isSetting(e) && markDirty());
  form.addEventListener("submit", (e) => { e.preventDefault(); save(); });
  window.addEventListener("beforeunload", (e) => { if (dirty) e.preventDefault(); });

  // Live level meter, so the input can be checked while choosing it.
  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(withPin(`${proto}://${location.host}/ws/admin`));
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type !== "tick") return;
      const db = msg.level_db;
      $("meterFill").style.width = `${Math.max(0, Math.min(100, ((db + 60) / 60) * 100))}%`;
      $("meterFill").className = db > -3 ? "clip" : db > -12 ? "hot" : "";
      $("levelText").textContent = db <= -119 ? "–∞ dB" : `${db.toFixed(0)} dB`;
    };
    ws.onclose = () => setTimeout(connect, 2000);
  }

  window.LT.onLanguageChange(() => {
    if (!state) return;
    renderChannels();
    renderSourceLanguages();
    renderTargets();
    renderQuick();
    renderAddresses();
    renderVmixLanguages();
    renderGlossary();
    renderUpdate();
  });
  window.LT.applyTexts();
  showTab(location.hash.slice(1));
  Promise.all([api("/api/admin/state"), api("/api/admin/devices")]).then(([s, d]) => {
    state = s;
    devices = d;
    fill();
    if (state.settings.vmix.enabled || state.settings.vmix.input) refreshVmix();
  });
  connect();
})();
