(() => {
  "use strict";

  const { $, t, api, withPin } = window.LT;
  const form = $("settingsForm");

  let state = null;
  let devices = [];
  let vmixState = null; // inputs reported by vMix
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
    const select = $("device");
    const list = devices.filter((d) => d.kind === (kind() === "loopback" ? "loopback" : "device"));
    const wanted = select.value || state.settings.audio.device;
    select.textContent = "";
    for (const d of list) {
      const o = new Option(`${d.name} — ${d.host_api}${d.is_default ? " ★" : ""}`, d.id);
      o.dataset.channels = d.channels;
      select.appendChild(o);
    }
    if (wanted && list.some((d) => d.id === wanted)) select.value = wanted;
    else if (list.length) select.value = (list.find((d) => d.is_default) || list[0]).id;
    renderChannels();
  }

  function renderChannels() {
    const select = $("channel");
    const current = select.value || String(state.settings.audio.channel);
    const channels = Number($("device").selectedOptions[0]?.dataset.channels || 2);
    select.textContent = "";
    select.appendChild(new Option(t("mix"), "0"));
    for (let i = 1; i <= channels; i++) select.appendChild(new Option(String(i), String(i)));
    select.value = [...select.options].some((o) => o.value === current) ? current : "0";
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

  function checkList(box, options, chosen, extra = () => "") {
    box.textContent = "";
    for (const lang of options) {
      const label = document.createElement("label");
      label.className = "check";
      label.innerHTML = '<input type="checkbox"> <span></span>';
      label.querySelector("input").value = lang.code;
      label.querySelector("input").checked = chosen.has(lang.code);
      label.querySelector("span").textContent = `${lang.name}${extra(lang)}`;
      box.appendChild(label);
    }
  }

  function renderTargets() {
    const s = state.settings;
    const options = state.target_languages.length
      ? state.target_languages.filter((l) => l.code !== $("sourceLang").value)
      : s.target_languages.map((code) => ({ code, name: code }));
    const tts = new Set(state.tts_languages);
    checkList($("targets"), options, new Set(s.target_languages), (l) => (tts.has(l.code) ? " 🔊" : ""));
  }

  function renderQuick() {
    const main = $("sourceLang").value;
    const mainName = state.source_languages.find((l) => l.code === main)?.name || main;
    const box = $("quick");
    const options = state.quick_language_options.filter((l) => l.code !== main);
    checkList(box, options, new Set(state.settings.quick_languages));
    const fixed = document.createElement("label");
    fixed.className = "check";
    fixed.innerHTML = '<input type="checkbox" checked disabled> <span></span>';
    fixed.querySelector("span").textContent = `${mainName} (${t("quickMain")})`;
    box.prepend(fixed);
  }

  $("sourceLang").addEventListener("change", () => { renderTargets(); renderQuick(); });
  $("engine").addEventListener("change", () => ($("nllbRows").hidden = $("engine").value !== "nllb"));

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

    const src = $("sourceLang");
    src.textContent = "";
    const sources = state.source_languages.length ? state.source_languages : [{ code: s.source_language, name: s.source_language }];
    for (const l of sources) src.appendChild(new Option(l.name, l.code));
    src.value = s.source_language;
    renderTargets();
    renderQuick();
    $("tts").checked = s.tts_enabled;
    $("engine").value = s.translation.engine;
    $("nllbModel").value = s.translation.nllb_model;
    $("nllbDevice").value = s.translation.nllb_device;
    $("nllbRows").hidden = s.translation.engine !== "nllb";

    $("vmixEnabled").checked = s.vmix.enabled;
    $("vmixUrl").value = s.vmix.url;
    $("vmixOverlay").value = String(s.vmix.overlay);
    $("vmixMaxChars").value = s.vmix.max_chars;
    $("vmixAuto").checked = s.vmix.auto_overlay;
    renderVmixInputs();

    renderAddresses();
    $("wifi").value = s.wifi_name;
    $("pin").value = s.admin_pin;
    $("port").value = s.port;
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

    s.vmix.enabled = $("vmixEnabled").checked;
    s.vmix.url = $("vmixUrl").value.trim() || "http://127.0.0.1:8088";
    s.vmix.input = $("vmixInput").value;
    s.vmix.field = $("vmixField").value;
    s.vmix.overlay = Number($("vmixOverlay").value);
    s.vmix.max_chars = Math.max(20, Math.min(1000, Number($("vmixMaxChars").value) || 160));
    s.vmix.auto_overlay = $("vmixAuto").checked;

    s.public_host = $("address").value === "__custom" ? "" : $("address").value;
    s.wifi_name = $("wifi").value.trim();
    s.admin_pin = $("pin").value.trim();
    return s;
  }

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

  form.addEventListener("input", markDirty);
  form.addEventListener("change", markDirty);
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

  window.LT.onLanguageChange(() => { if (state) { renderChannels(); renderQuick(); renderAddresses(); } });
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
