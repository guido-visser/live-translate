(() => {
  "use strict";

  const { $, t, api, withPin, langName, sortByName } = window.LT;
  const form = $("settingsForm");

  let state = null;
  let devices = [];
  let vmixState = null; // inputs reported by vMix
  let obsState = null; // text sources and scenes reported by OBS
  let glossary = []; // every glossary entry (all spoken languages), edited in place
  let dirty = false; // changes the server doesn't have yet
  let saving = null; // the save under way
  let saveAgain = false;
  let saveFailed = false;
  let saveTimer = null;
  let savedTimer = null;

  // -- tabs --------------------------------------------------------------------------

  const tabs = [...document.querySelectorAll("[role=tab][data-tab]")];

  function showTab(name) {
    if (!tabs.some((b) => b.dataset.tab === name)) name = tabs[0].dataset.tab;
    tabs.forEach((b) => {
      const active = b.dataset.tab === name;
      b.classList.toggle("active", active);
      b.setAttribute("aria-selected", String(active));
    });
    document.querySelectorAll(".tab-panel").forEach((p) => (p.hidden = p.dataset.panel !== name));
    history.replaceState(null, "", `${location.pathname}${location.search}#${name}`);
    if (name === "about" && state?.settings.check_updates && !state.update.latest) $("checkNow").click();
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

  // Only hides rows: a ticked language that doesn't match the search stays ticked.
  function filterList(box, search, empty) {
    let shown = 0;
    box.querySelectorAll("label").forEach((label) => {
      const match = window.LT.languageMatches(label.querySelector("input").value, search.value);
      label.hidden = !match;
      if (match) shown++;
    });
    empty.hidden = shown > 0;
  }

  const filterTargets = () => filterList($("targets"), $("targetsSearch"), $("targetsNone"));
  const filterQuick = () => filterList($("quick"), $("quickSearch"), $("quickNone"));
  $("targetsSearch").addEventListener("input", filterTargets);
  $("quickSearch").addEventListener("input", filterQuick);
  for (const search of [$("targetsSearch"), $("quickSearch")]) {
    search.addEventListener("keydown", (e) => { if (e.key === "Enter") e.preventDefault(); });  // don't save the form
  }

  function renderTargets() {
    const s = state.settings;
    const main = $("sourceLang").value;
    const codes = state.target_languages.length ? state.target_languages.map((l) => l.code) : s.target_languages;
    const tts = new Set(state.tts_languages);
    checkList($("targets"), codes.filter((c) => c !== main), new Set(s.target_languages), (c) => (tts.has(c) ? " 🔊" : ""));
    filterTargets();
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
    fixed.querySelector("input").value = main;
    fixed.querySelector("span").textContent = `${langName(main)} (${t("quickMain")})`;
    box.prepend(fixed);
    filterQuick();
  }

  function renderSourceLanguages() {
    const src = $("sourceLang");
    const wanted = src.value || state.settings.source_language;
    const codes = state.source_languages.length ? state.source_languages.map((l) => l.code) : [wanted];
    src.textContent = "";
    for (const code of sortByName(codes)) src.appendChild(new Option(langName(code), code));
    src.value = wanted;
  }

  $("sourceLang").addEventListener("change", () => { renderTargets(); renderQuick(); renderVmixLanguages(); renderObsLanguages(); renderGlossary(); });
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
    await saveFirst();
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
    await saveFirst();
    try {
      await api("/api/admin/vmix/test", { method: "POST", body: JSON.stringify({ text: t("vmixTestText") }) });
      showVmixStatus(true);
    } catch (err) {
      showVmixStatus(false, err.message);
    }
  });

  // -- OBS Studio ------------------------------------------------------------------------

  const subtabs = [...document.querySelectorAll("[data-subtab]")];

  function showSubtab(name) {
    subtabs.forEach((b) => {
      b.classList.toggle("active", b.dataset.subtab === name);
      b.setAttribute("aria-selected", String(b.dataset.subtab === name));
    });
    document.querySelectorAll("[data-subpanel]").forEach((p) => (p.hidden = p.dataset.subpanel !== name));
  }

  subtabs.forEach((b) => b.addEventListener("click", () => showSubtab(b.dataset.subtab)));

  // What is saved stays listed while OBS is closed, so opening this page doesn't lose it.
  function fillObsList(select, names, wanted, first) {
    select.textContent = "";
    select.appendChild(new Option(first, ""));
    for (const name of names) select.appendChild(new Option(name, name));
    if (wanted && !names.includes(wanted)) select.appendChild(new Option(wanted, wanted));
    select.value = wanted || "";
  }

  function renderObsLists() {
    const s = state.settings.obs;
    fillObsList($("obsSource"), obsState?.inputs || [], $("obsSource").value || s.source, "—");
    fillObsList($("obsScene"), obsState?.scenes || [], $("obsScene").value || s.scene, t("obsSceneLive"));
  }

  function renderObsLanguages() {
    const select = $("obsLanguage");
    const wanted = select.value || state.settings.obs.language || "";
    const main = $("sourceLang").value;
    select.textContent = "";
    select.appendChild(new Option(`${langName(main)} (${t("quickMain")})`, ""));
    for (const code of sortByName(state.settings.target_languages.filter((c) => c !== main))) {
      select.appendChild(new Option(langName(code), code));
    }
    if (wanted && ![...select.options].some((o) => o.value === wanted)) select.appendChild(new Option(langName(wanted), wanted));
    select.value = wanted === main ? "" : wanted;
  }

  function showObsStatus(ok, message) {
    const el = $("obsStatus");
    el.className = `small ${ok ? "vmix-ok" : "vmix-error"}`;
    el.textContent = ok ? t("vmixOk") : `${t("vmixError")}: ${message}`;
  }

  async function refreshObs() {
    await saveFirst();
    try {
      obsState = await api("/api/admin/obs/inputs");
      showObsStatus(true);
    } catch (err) {
      obsState = null;
      showObsStatus(false, err.message);
    }
    renderObsLists();
  }

  $("obsRefresh").addEventListener("click", refreshObs);
  $("obsTest").addEventListener("click", async () => {
    await saveFirst();
    try {
      await api("/api/admin/obs/test", { method: "POST", body: JSON.stringify({ text: t("vmixTestText") }) });
      showObsStatus(true);
    } catch (err) {
      showObsStatus(false, err.message);
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

  // -- listeners over the internet (SSH tunnel to the church's own server) ---------------------

  function remoteFromForm() {
    return {
      url: $("remoteUrl").value.trim().replace(/\/+$/, ""),
      host: $("remoteHost").value.trim(),
      user: $("remoteUser").value.trim(),
      port: Number($("remotePort").value) || 8800,
    };
  }

  function renderServerSetup() {
    const r = remoteFromForm();
    $("setupNginx").textContent = [
      "location ~ ^/ {",
      `    proxy_pass http://127.0.0.1:${r.port};`,
      "    proxy_http_version 1.1;",
      "    proxy_set_header Upgrade $http_upgrade;",
      '    proxy_set_header Connection "upgrade";',
      "    proxy_set_header Host $host;",
      "    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
      "    proxy_set_header X-Forwarded-Proto $scheme;",
      "    proxy_buffering off;",
      "    proxy_read_timeout 3600s;",
      "    proxy_send_timeout 3600s;",
      "}",
    ].join("\n");
    const key = state.tunnel.public_key;
    // The key may only open this one tunnel: no shell, no other forwarding.
    $("setupKey").textContent = key ? `restrict,port-forwarding,permitlisten="127.0.0.1:${r.port}" ${key}` : "";
    $("setupKey").hidden = !key;
    document.querySelector('[data-copy="setupKey"]').hidden = !key;
    $("createKey").hidden = Boolean(key);
  }

  function renderTunnel() {
    const tn = state.tunnel;
    const el = $("tunnelStatus");
    const url = state.settings.remote.public_url;
    $("remoteRows").hidden = !$("remoteEnabled").checked;
    el.className = "small";
    if (tn.state === "connected") {
      if (tn.reachable) el.textContent = `${t("tunnelConnected")} ${t("tunnelReachable").replace("{url}", url)}`;
      else if (tn.reachable === false) {
        el.textContent = `${t("tunnelConnected")} ${tn.reach_error}`;
        el.className = "small warn-text";
      } else el.textContent = url ? `${t("tunnelConnected")} ${t("tunnelChecking").replace("{url}", url)}` : t("tunnelConnected");
    } else if (tn.state === "connecting") el.textContent = t("tunnelConnecting");
    else if (tn.state === "error") {
      el.textContent = `${t("tunnelError")}: ${tn.error}`;
      el.className = "small error";
    } else el.textContent = state.settings.remote.enabled ? "" : t("tunnelOff");
    renderServerSetup();
  }

  $("remoteEnabled").addEventListener("change", renderTunnel);
  for (const id of ["remoteUrl", "remoteHost", "remoteUser", "remotePort"]) $(id).addEventListener("input", renderServerSetup);

  $("createKey").addEventListener("click", async () => {
    $("createKey").disabled = true;
    try {
      state.tunnel = await api("/api/admin/tunnel/key", { method: "POST" });
    } catch (err) {
      $("tunnelStatus").textContent = err.message;
    } finally {
      $("createKey").disabled = false;
      renderTunnel();
    }
  });

  $("tunnelForget").addEventListener("click", async () => {
    state.tunnel = await api("/api/admin/tunnel/forget", { method: "POST" });
    renderTunnel();
  });

  for (const button of document.querySelectorAll("button.copy")) {
    button.addEventListener("click", async () => {
      await navigator.clipboard.writeText($(button.dataset.copy).textContent);
      button.textContent = t("copied");
      setTimeout(() => (button.textContent = t("copy")), 1500);
    });
  }

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
    $("mainStreaming").value = s.main_streaming;
    showSubtab(s.main_streaming === "obs" ? "obs" : "vmix");
    $("obsEnabled").checked = s.obs.enabled;
    $("obsHost").value = s.obs.host;
    $("obsPort").value = s.obs.port;
    $("obsPassword").value = s.obs.password;
    $("obsSource").value = s.obs.source;
    $("obsScene").value = s.obs.scene;
    renderObsLists();
    $("obsLanguage").value = s.obs.language;
    renderObsLanguages();
    $("obsMaxChars").value = s.obs.max_chars;
    $("obsAuto").checked = s.obs.auto_overlay;

    renderAddresses();
    $("wifi").value = s.wifi_name;
    $("pin").value = s.admin_pin;
    $("port").value = s.port;
    $("checkUpdates").checked = s.check_updates;
    $("windowsOnly").hidden = $("appWindowRow").hidden = $("closeActionRow").hidden = !state.windows;
    $("churchName").value = s.church_name;
    $("churchShort").value = s.church_short_name;
    $("startWithWindows").checked = s.start_with_windows;
    $("startWindow").value = s.start_window;
    $("gpuHelpers").checked = s.gpu_helpers;
    $("logEnabled").checked = s.service_log.enabled;
    $("logAudio").checked = s.service_log.audio;
    $("logFolder").textContent = state.logs_folder;
    $("dataFolder").textContent = state.data_folder;
    $("moveRow").hidden = !state.can_move_data;
    $("autoStartTranslation").checked = s.auto_start_translation;
    $("appWindow").checked = s.app_window;
    $("closeAction").value = s.close_action;
    $("remoteEnabled").checked = s.remote.enabled;
    $("remoteUrl").value = s.remote.public_url;
    $("remoteHost").value = s.remote.ssh_host;
    $("remoteUser").value = s.remote.ssh_user;
    $("remoteSshPort").value = s.remote.ssh_port;
    $("remotePort").value = s.remote.remote_port;
    renderTunnel();
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
    s.main_streaming = $("mainStreaming").value;
    s.obs.enabled = $("obsEnabled").checked;
    s.obs.host = $("obsHost").value.trim() || "127.0.0.1";
    s.obs.port = Number($("obsPort").value) || 4455;
    s.obs.password = $("obsPassword").value;
    s.obs.source = $("obsSource").value;
    s.obs.scene = $("obsScene").value;
    s.obs.language = $("obsLanguage").value;
    s.obs.max_chars = Math.max(20, Math.min(1000, Number($("obsMaxChars").value) || 160));
    s.obs.auto_overlay = $("obsAuto").checked;

    s.public_host = $("address").value === "__custom" ? "" : $("address").value;
    s.wifi_name = $("wifi").value.trim();
    s.admin_pin = $("pin").value.trim();
    s.check_updates = $("checkUpdates").checked;
    s.church_name = $("churchName").value.trim();
    s.church_short_name = $("churchShort").value.trim();
    s.start_with_windows = $("startWithWindows").checked;
    s.start_window = $("startWindow").value;
    s.gpu_helpers = $("gpuHelpers").checked;
    s.service_log.enabled = $("logEnabled").checked;
    s.service_log.audio = $("logAudio").checked;
    s.auto_start_translation = $("autoStartTranslation").checked;
    s.app_window = $("appWindow").checked;
    s.close_action = $("closeAction").value;
    const r = remoteFromForm();
    s.remote.enabled = $("remoteEnabled").checked;
    s.remote.public_url = r.url;
    s.remote.ssh_host = r.host;
    s.remote.ssh_user = r.user;
    s.remote.ssh_port = Number($("remoteSshPort").value) || 22;
    s.remote.remote_port = r.port;
    return s;
  }

  $("logOpen").addEventListener("click", () => api("/api/admin/logs/open", { method: "POST" }));

  // -- church --------------------------------------------------------------------------------

  $("churchProtect").addEventListener("click", () => {
    window.LT.protectNames(glossary, $("sourceLang").value, $("churchName").value, $("churchShort").value);
    markDirty();
    renderGlossary();
    showTab("glossary");
  });

  $("profileImport").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file) return;
    const status = $("profileStatus");
    try {
      const profile = JSON.parse(await file.text());
      if (!Array.isArray(profile.glossary)) throw new Error("no glossary");
      for (const key of state.profile_fields) {
        if (key in profile) state.settings[key] = profile[key];
      }
      fill();
      markDirty();
      status.textContent = t("profileImported");
      status.className = "small";
    } catch {
      status.textContent = t("profileBad");
      status.className = "small error";
    }
  });

  // -- fitting the song detection to this church's sound, from a logged service --------------

  let calService = null; // the chosen service with what each slider level would skip
  let calLabels = []; // stretches the operator said are music or speech
  let calPick = null; // the stretch clicked on the timeline, and where in it

  const clockTime = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
  const minutes = (s) => t("calMinutes").replace("{n}", Math.round(s / 60));
  const calUrl = (path) => withPin(`/api/admin/logs/${encodeURIComponent(calService.name)}/${path}`);
  const calLevel = () => calService.levels[$("calLevel").value];

  // The whole service as stretches that are skipped or translated at this level.
  function calStretches() {
    const stretches = [];
    let at = 0;
    for (const [start, end] of calLevel().skipped) {
      if (start > at) stretches.push({ start: at, end: start, skipped: false });
      stretches.push({ start, end, skipped: true });
      at = end;
    }
    if (at < calService.duration) stretches.push({ start: at, end: calService.duration, skipped: false });
    return stretches;
  }

  function renderCalibration() {
    if (!calService) return;
    const share = (s) => `${(s / calService.duration) * 100}%`;
    const bar = $("calTimeline");
    bar.textContent = "";
    for (const stretch of calStretches()) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = `cal-stretch${stretch.skipped ? " skipped" : ""}`;
      b.style.left = share(stretch.start);
      b.style.width = share(stretch.end - stretch.start);
      b.title = `${clockTime(stretch.start)} – ${clockTime(stretch.end)}`;
      b.setAttribute("aria-label", `${b.title} ${t(stretch.skipped ? "calNotTranslated" : "calTranslated")}`);
      b.addEventListener("click", (e) => {
        const box = b.getBoundingClientRect();
        const part = e.detail ? (e.clientX - box.left) / box.width : 0; // a key press has no position
        calPick = { ...stretch, at: stretch.start + part * (stretch.end - stretch.start) };
        renderCalibration();
      });
      bar.appendChild(b);
    }
    for (const label of calLabels) {
      const strip = document.createElement("div");
      strip.className = `cal-label ${label.kind}`;
      strip.style.left = share(label.start);
      strip.style.width = share(label.end - label.start);
      bar.appendChild(strip);
    }
    for (const mark of calService.marks) {
      const tick = document.createElement("div");
      tick.className = "cal-mark";
      tick.style.left = share(mark);
      bar.appendChild(tick);
    }
    const axis = $("calAxis");
    axis.textContent = "";
    const step = calService.duration > 3600 ? 1800 : 600;
    for (let s = 0; s < calService.duration; s += step) {
      const tick = document.createElement("span");
      tick.style.left = share(s);
      tick.textContent = clockTime(s);
      axis.appendChild(tick);
    }

    $("calSkipped").textContent = minutes(calLevel().skipped_s);
    $("calSpeech").textContent = minutes(calLevel().speech_s);
    $("calSpeech").className = calLevel().speech_s >= 30 ? "warn-text" : "";
    const inUse = Number($("calLevel").value) === state.settings.song_level;
    $("calUse").disabled = inUse;
    $("calStatus").textContent = inUse ? t("calInUse") : "";
    $("calForget").hidden = !calLabels.length;

    $("calPicked").hidden = !calPick;
    if (!calPick) return;
    // The slider can move the edges of a stretch, so show what the clicked moment is now part of.
    const now = calStretches().find((s) => s.start <= calPick.at && calPick.at <= s.end);
    if (now) Object.assign(calPick, { start: now.start, end: now.end, skipped: now.skipped });
    const verdict = t(calPick.skipped ? "calNotTranslated" : "calTranslated");
    $("calPickedText").textContent = `${clockTime(calPick.start)} – ${clockTime(calPick.end)} · ${verdict}`;
    $("calListen").hidden = !calService.audio;
    $("calNoAudio").hidden = calService.audio;
    const list = $("calSentences");
    list.textContent = "";
    const said = calService.sentences.filter((s) => calPick.start <= s.t && s.t <= calPick.end);
    for (const sentence of said.slice(0, 6)) {
      const li = document.createElement("li");
      li.textContent = `${clockTime(sentence.t)} ${sentence.text}`;
      list.appendChild(li);
    }
  }

  async function chooseService(name) {
    calService = await api(`/api/admin/logs/${encodeURIComponent(name)}/analysis`);
    calLabels = [];
    calPick = null;
    $("calFit").hidden = true;
    $("calAudio").hidden = true;
    $("calAudio").removeAttribute("src");
    $("calLevel").value = state.settings.song_level;
    renderCalibration();
  }

  async function loadServices() {
    const found = await api("/api/admin/logs");
    $("calEmpty").hidden = found.length > 0;
    $("calBody").hidden = !found.length;
    if (!found.length) return;
    const select = $("calService");
    select.textContent = "";
    for (const service of found) {
      const when = `${service.name.slice(0, 10)} ${service.name.slice(11, 16).replace("-", ":")}`;
      select.appendChild(new Option(`${when} · ${minutes(service.duration)}${service.audio ? " ♪" : ""}`, service.name));
    }
    await chooseService(found[0].name);
  }

  async function labelPicked(kind) {
    calLabels = calLabels.filter((l) => l.end <= calPick.start || l.start >= calPick.end);
    calLabels.push({ start: calPick.start, end: calPick.end, kind });
    const { level } = await api(calUrl("fit"), { method: "POST", body: JSON.stringify({ labels: calLabels }) });
    $("calLevel").value = level;
    $("calFit").textContent = t("calFitted");
    $("calFit").hidden = false;
    renderCalibration();
  }

  async function useLevel(level) {
    await api("/api/admin/singing/level", { method: "POST", body: JSON.stringify({ level }) });
    state.settings.song_level = level;
    $("calLevel").value = level;
    renderCalibration();
    $("calStatus").textContent = t("calApplied");
  }

  $("calibration").addEventListener("toggle", (e) => { if (e.target.open && !calService) loadServices(); });
  $("calService").addEventListener("change", (e) => chooseService(e.target.value));
  $("calLevel").addEventListener("input", () => { $("calFit").hidden = true; renderCalibration(); });
  $("calIsMusic").addEventListener("click", () => labelPicked("music"));
  $("calIsSpeech").addEventListener("click", () => labelPicked("speech"));
  $("calUse").addEventListener("click", () => useLevel(Number($("calLevel").value)));
  $("calDefault").addEventListener("click", () => useLevel(0));
  $("calForget").addEventListener("click", () => {
    calLabels = [];
    $("calFit").hidden = true;
    renderCalibration();
  });
  $("calListen").addEventListener("click", () => {
    const player = $("calAudio");
    player.src = calUrl(`clip?t=${calPick.at}`);
    player.hidden = false;
    player.play();
  });

  // -- updates (installing happens on the main screen) -------------------------------------

  function renderUpdate() {
    const u = state.update;
    $("version").textContent = u.current;
    $("versionLatest").textContent = u.latest || "–";
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

  $("setupAgain").addEventListener("click", async () => {
    await saveFirst();
    await api("/api/admin/setup/again", { method: "POST" });
    dirty = false;
    location.href = withPin("/admin/setup?again=1");
  });

  $("moveData").addEventListener("click", async () => {
    if (!confirm(t("aboutMoveConfirm"))) return;
    $("moveStatus").className = "small";
    try {
      await saveFirst();
      await api("/api/admin/data/move", { method: "POST" });
      dirty = false;
      $("moveData").disabled = true;
      $("moveStatus").textContent = t("aboutMoveStarted");
    } catch (err) {
      $("moveStatus").className = "small error";
      $("moveStatus").textContent = err.message;
    }
  });

  // -- saving by itself -------------------------------------------------------------------------

  // A choice is saved right away, with a moment's wait so that ticking a few languages is one save.
  // Typed text is saved when leaving the field or pressing Enter: half a server address or PIN must
  // never take effect. The page isn't redrawn afterwards, because the operator may be busy in it.
  const SAVE_DELAY_MS = 800;
  const SAVED_SHOWN_MS = 2500;

  function markDirty() {
    dirty = true;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(save, SAVE_DELAY_MS);
  }

  // A file source without a file has nothing to translate from yet: wait for the file.
  const canSave = () => kind() !== "file" || Boolean($("filePath").value.trim());

  const lists = () => JSON.stringify([state.target_languages, state.source_languages, state.quick_language_options, state.tts_languages]);

  function afterSave(old) {
    if (lists() !== old.lists) {
      renderSourceLanguages();
      renderTargets();
      renderQuick();
    }
    if (JSON.stringify(state.settings.target_languages) !== old.targets) {
      renderVmixLanguages();
      renderObsLanguages();
    }
    if (state.settings.public_host !== old.host) renderAddresses();
    renderTunnel();
    $("saved").textContent = t("saved");
    $("saved").hidden = false;
    clearTimeout(savedTimer);
    savedTimer = setTimeout(() => ($("saved").hidden = true), SAVED_SHOWN_MS);
  }

  async function send() {
    clearTimeout(saveTimer);
    if (!canSave()) return;
    dirty = false;
    const old = { lists: lists(), targets: JSON.stringify(state.settings.target_languages), host: state.settings.public_host };
    try {
      state = await window.LT.saveSettings(collect());
    } catch (err) {
      dirty = saveFailed = true;
      $("saved").hidden = true;
      $("saveError").textContent = t("notSaved").replace("{error}", err.message);
      $("saveError").hidden = false;
      return;
    }
    saveFailed = false;
    $("saveError").hidden = true;
    afterSave(old);
  }

  // One at a time, in order, and the last one has everything.
  function save() {
    if (saving) {
      saveAgain = true;
      return saving;
    }
    saving = (async () => {
      do {
        saveAgain = false;
        await send();
      } while (saveAgain);
      saving = null;
    })();
    return saving;
  }

  // For what acts on the settings: a test, a refresh, leaving the page.
  function saveFirst() {
    return dirty || saving ? save() : Promise.resolve();
  }
  window.LT.beforeClosing = saveFirst;

  // The test sentence, the search fields and file pickers are not settings, and calibrating saves its own.
  const isSetting = (e) => e.target.id !== "glossarySample" && !["search", "file"].includes(e.target.type) && !e.target.closest("#calibration");
  const isTyped = (el) => el.matches?.("input:not([type=checkbox]):not([type=radio]):not([type=range]):not([type=file])");
  form.addEventListener("input", (e) => {
    if (!isSetting(e) || !isTyped(e.target)) return;
    dirty = true;
    $("saved").hidden = true;
  });
  form.addEventListener("change", (e) => isSetting(e) && markDirty());
  form.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || !isTyped(e.target) || !isSetting(e)) return;
    e.preventDefault();
    saveFirst();
  });
  form.addEventListener("submit", (e) => e.preventDefault());

  // Back to the main screen only once it is saved, so that screen shows the new settings.
  document.querySelector("header a").addEventListener("click", async (e) => {
    document.activeElement?.blur?.();
    if (!dirty && !saving) return;
    e.preventDefault();
    await saveFirst();
    if (!saveFailed) location.href = e.currentTarget?.href || withPin("/admin");
  });
  window.addEventListener("pagehide", () => {
    if (dirty && canSave()) window.LT.saveSettings(collect()).catch(() => {});
  });
  window.addEventListener("beforeunload", (e) => { if (saveFailed) e.preventDefault(); });

  // Live level meter, so the input can be checked while choosing it.
  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(withPin(`${proto}://${location.host}/ws/admin`));
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type !== "tick") return;
      if (msg.tunnel && JSON.stringify(msg.tunnel) !== JSON.stringify(state.tunnel)) {
        state.tunnel = msg.tunnel;
        renderTunnel();
      }
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
    renderTunnel();
    renderVmixLanguages();
    renderObsLanguages();
    renderObsLists();
    renderGlossary();
    renderUpdate();
    renderCalibration();
  });
  window.LT.applyTexts();
  showTab(location.hash.slice(1));
  Promise.all([api("/api/admin/state"), api("/api/admin/devices")]).then(([s, d]) => {
    state = s;
    devices = d;
    fill();
    if (state.settings.vmix.enabled || state.settings.vmix.input) refreshVmix();
    if (state.settings.obs.enabled || state.settings.obs.source) refreshObs();
    if (location.hash === "#about") showTab("about");  // now that it knows the version
  });
  connect();
})();
