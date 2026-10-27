(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const pin = new URLSearchParams(location.search).get("pin");
  const withPin = (url) => (pin ? `${url}${url.includes("?") ? "&" : "?"}pin=${encodeURIComponent(pin)}` : url);

  let uiLang = "nl";
  try { uiLang = localStorage.getItem("lt.admin.lang") || "nl"; } catch { /* ignore */ }
  let state = null; // last /api/admin/state
  let devices = [];
  let status = { state: "stopped", paused: false, error: "" };
  let translator = { status: "stopped", detail: "", needs_restart: false };
  let formDirty = false;

  const t = (key) => (window.ADMIN_I18N[uiLang] || window.ADMIN_I18N.en)[key] || window.ADMIN_I18N.en[key] || key;

  async function api(path, options = {}) {
    const res = await fetch(withPin(path), {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
    return res.json();
  }

  // -- texts ---------------------------------------------------------------------

  function applyTexts() {
    document.documentElement.lang = uiLang;
    $("uiLang").value = uiLang;
    document.querySelectorAll("[data-t]").forEach((el) => (el.textContent = t(el.dataset.t)));
    renderStatus();
    if (state) {
      renderChannelOptions();
      renderAddresses();
    }
  }

  $("uiLang").addEventListener("change", (e) => {
    uiLang = e.target.value;
    try { localStorage.setItem("lt.admin.lang", uiLang); } catch { /* ignore */ }
    applyTexts();
    renderListeners({}, {});
  });

  // -- status & controls ---------------------------------------------------------

  function renderStatus() {
    const pill = $("statePill");
    const key = status.state === "running" && status.paused ? "paused" : status.state;
    pill.textContent = t(key);
    pill.className = `pill ${key}`;

    const trKey = { ready: "trReady", starting: "trStarting", installing: "trInstalling", error: "trError" }[translator.status] || "trStopped";
    const engine = translator.engine === "nllb" ? "NLLB" : "LibreTranslate";
    $("trPill").textContent = `${engine}: ${translator.needs_restart && translator.status === "ready" ? t("trRestart") : t(trKey)}`;
    $("trPill").className = `pill tr-${translator.status}`;
    $("trDetail").textContent = translator.detail || "";
    $("trDetail").hidden = !translator.detail;

    const running = status.state !== "stopped";
    $("startBtn").textContent = running ? t("stop") : t("start");
    $("startBtn").classList.toggle("danger", running);
    $("startBtn").disabled = status.state === "loading";
    $("pauseBtn").disabled = status.state !== "running";
    $("pauseBtn").textContent = status.paused ? t("resume") : t("pause");
    $("pauseBtn").classList.toggle("warn", status.paused);
    $("error").textContent = status.error || "";
    $("error").hidden = !status.error;
  }

  $("startBtn").addEventListener("click", async () => {
    if (status.state === "stopped") {
      if (formDirty) await saveSettings();
      await api("/api/admin/start", { method: "POST" });
    } else {
      await api("/api/admin/stop", { method: "POST" });
    }
  });

  $("pauseBtn").addEventListener("click", async () => {
    const res = await api("/api/admin/pause", { method: "POST", body: JSON.stringify({ paused: !status.paused }) });
    status.paused = res.paused;
    renderStatus();
  });

  // Space bar toggles pause: handy during a service.
  document.addEventListener("keydown", (e) => {
    if (e.code === "Space" && !["INPUT", "SELECT", "TEXTAREA", "BUTTON"].includes(document.activeElement.tagName)) {
      e.preventDefault();
      if (!$("pauseBtn").disabled) $("pauseBtn").click();
    }
  });

  // -- QR ----------------------------------------------------------------------------

  function renderShare() {
    $("qrImg").src = withPin(`/api/qr.svg?t=${Date.now()}`);
    $("url").textContent = state.url;
    $("url").href = state.url;
    const wifi = state.settings.wifi_name;
    $("wifiLine").textContent = wifi ? `${t("wifiPrefix")} ${wifi}` : "";
  }
  $("qrFull").addEventListener("click", () => window.open("/qr", "qr", "width=900,height=900"));

  // -- listeners, transcript, stats -------------------------------------------------

  function langName(code) {
    const all = [...(state?.target_languages || []), ...(state?.source_languages || [])];
    return all.find((l) => l.code === code)?.name || code;
  }

  function renderListeners(text, audio) {
    const rows = $("listenerRows");
    rows.textContent = "";
    const langs = [...new Set([...Object.keys(text), ...Object.keys(audio)])].sort();
    if (!langs.length) {
      rows.innerHTML = `<tr><td class="muted">${t("none")}</td></tr>`;
      return;
    }
    for (const lang of langs) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td></td><td class="num"></td><td class="num"></td>`;
      tr.children[0].textContent = langName(lang);
      tr.children[1].textContent = `${text[lang] || 0} ${t("text")}`;
      tr.children[2].textContent = audio[lang] ? `${audio[lang]} ${t("audioCol")}` : "";
      rows.appendChild(tr);
    }
  }

  function addTranscript(text) {
    const box = $("transcript");
    const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
    const p = document.createElement("p");
    p.textContent = text;
    box.appendChild(p);
    while (box.children.length > 100) box.firstChild.remove();
    if (stick) box.scrollTop = box.scrollHeight;
  }

  function renderStats(stats, gpu) {
    const rows = [
      ["statDevice", stats.device ? stats.device.toUpperCase() : "–"],
      ["statPass", stats.asr_pass_s ? `${stats.asr_pass_s} s` : "–"],
      ["statBuffer", stats.asr_buffer_s ? `${stats.asr_buffer_s} s` : "–"],
      ["statTranslate", stats.translate_ms ? `${stats.translate_ms} ms` : "–"],
      ["statLatency", stats.latency_s ? `${stats.latency_s} s` : "–"],
    ];
    if (gpu) rows.push(["statGpu", `${gpu.util}% · ${gpu.mem_used_mb} / ${gpu.mem_total_mb} MB`]);
    $("stats").innerHTML = rows.map(([k, v]) => `<dt>${t(k)}</dt><dd class="mono">${v}</dd>`).join("");
  }

  function renderLevel(db) {
    const pct = Math.max(0, Math.min(100, ((db + 60) / 60) * 100));
    const fill = $("meterFill");
    fill.style.width = `${pct}%`;
    fill.className = db > -3 ? "clip" : db > -12 ? "hot" : "";
    $("levelText").textContent = db <= -119 ? "–∞ dB" : `${db.toFixed(0)} dB`;
  }

  // -- settings form -------------------------------------------------------------------

  const form = $("settingsForm");
  form.addEventListener("input", () => { formDirty = true; $("saved").hidden = true; });

  function kind() {
    return form.querySelector("input[name=kind]:checked")?.value || "device";
  }

  function renderDevices() {
    const select = $("device");
    const k = kind();
    const list = devices.filter((d) => d.kind === (k === "loopback" ? "loopback" : "device"));
    select.textContent = "";
    for (const d of list) {
      const o = document.createElement("option");
      o.value = d.id;
      o.textContent = `${d.name} — ${d.host_api}${d.is_default ? " ★" : ""}`;
      o.dataset.channels = d.channels;
      select.appendChild(o);
    }
    const saved = state.settings.audio.device;
    if (saved && list.some((d) => d.id === saved)) select.value = saved;
    else {
      const def = list.find((d) => d.is_default) || list[0];
      if (def) select.value = def.id;
    }
    renderChannelOptions();
  }

  function renderChannelOptions() {
    const select = $("channel");
    const current = select.value || String(state.settings.audio.channel);
    const channels = Number($("device").selectedOptions[0]?.dataset.channels || 2);
    select.textContent = "";
    const mix = new Option(t("mix"), "0");
    select.appendChild(mix);
    for (let i = 1; i <= channels; i++) select.appendChild(new Option(String(i), String(i)));
    select.value = [...select.options].some((o) => o.value === current) ? current : "0";
  }

  function renderKind() {
    const k = kind();
    $("deviceRow").hidden = k === "file";
    $("channelRow").hidden = k === "file";
    $("fileRow").hidden = k !== "file";
    renderDevices();
  }

  form.querySelectorAll("input[name=kind]").forEach((r) => r.addEventListener("change", renderKind));
  $("device").addEventListener("change", renderChannelOptions);
  $("gain").addEventListener("input", () => ($("gainText").textContent = `${$("gain").value}×`));
  $("refreshDevices").addEventListener("click", loadDevices);

  $("fileUpload").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const res = await fetch(withPin(`/api/admin/upload?name=${encodeURIComponent(file.name)}`), { method: "POST", body: file });
    const data = await res.json();
    $("filePath").value = data.path;
    formDirty = true;
    $("saved").textContent = t("uploadDone");
    $("saved").hidden = false;
  });

  function renderAddresses() {
    const select = $("address");
    const current = state.settings.public_host;
    select.textContent = "";
    for (const ip of state.addresses) select.appendChild(new Option(ip, ip === state.addresses[0] ? "" : ip));
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

  function renderEngine() {
    $("nllbRows").hidden = $("engine").value !== "nllb";
  }
  $("engine").addEventListener("change", renderEngine);

  function renderTargets() {
    const box = $("targets");
    box.textContent = "";
    const chosen = new Set(state.settings.target_languages);
    const tts = new Set(state.tts_languages);
    const options = state.target_languages.length
      ? state.target_languages
      : state.settings.target_languages.map((code) => ({ code, name: code }));
    for (const lang of options) {
      const label = document.createElement("label");
      label.className = "check";
      label.innerHTML = `<input type="checkbox" value="${lang.code}"> <span></span>`;
      label.querySelector("input").checked = chosen.has(lang.code);
      label.querySelector("span").textContent = `${lang.name}${tts.has(lang.code) ? " 🔊" : ""}`;
      box.appendChild(label);
    }
  }

  function fillForm() {
    const s = state.settings;
    form.querySelector(`input[name=kind][value=${s.audio.kind}]`).checked = true;
    $("filePath").value = s.audio.file_path;
    $("gain").value = s.audio.gain;
    $("gainText").textContent = `${s.audio.gain}×`;
    $("channel").value = String(s.audio.channel);

    const src = $("sourceLang");
    src.textContent = "";
    const sources = state.source_languages.length ? state.source_languages : [{ code: s.source_language, name: s.source_language }];
    for (const l of sources) src.appendChild(new Option(l.name, l.code));
    src.value = s.source_language;

    const model = $("model");
    model.textContent = "";
    for (const m of state.models) model.appendChild(new Option(m, m));
    model.value = s.asr.model;
    $("asrDevice").value = s.asr.device;

    $("engine").value = s.translation.engine;
    $("nllbModel").value = s.translation.nllb_model;
    $("nllbDevice").value = s.translation.nllb_device;
    renderEngine();
    renderTargets();
    $("tts").checked = s.tts_enabled;
    renderAddresses();
    $("wifi").value = s.wifi_name;
    $("pin").value = s.admin_pin;
    renderKind();
    formDirty = false;
  }

  function collectSettings() {
    const s = structuredClone(state.settings);
    s.audio.kind = kind();
    s.audio.device = s.audio.kind === "file" ? s.audio.device : $("device").value || null;
    s.audio.channel = Number($("channel").value || 0);
    s.audio.file_path = $("filePath").value.trim();
    s.audio.gain = Number($("gain").value);
    s.source_language = $("sourceLang").value;
    s.asr.model = $("model").value;
    s.asr.device = $("asrDevice").value;
    s.translation.engine = $("engine").value;
    s.translation.nllb_model = $("nllbModel").value;
    s.translation.nllb_device = $("nllbDevice").value;
    s.target_languages = [...$("targets").querySelectorAll("input:checked")].map((i) => i.value);
    s.tts_enabled = $("tts").checked;
    s.public_host = $("address").value === "__custom" ? "" : $("address").value;
    s.wifi_name = $("wifi").value.trim();
    s.admin_pin = $("pin").value.trim();
    return s;
  }

  async function saveSettings() {
    state = await api("/api/admin/settings", { method: "POST", body: JSON.stringify(collectSettings()) });
    fillForm();
    renderShare();
    $("saved").textContent = t("saved");
    $("saved").hidden = false;
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    await saveSettings();
  });

  // -- data loading & live updates --------------------------------------------------------

  async function loadDevices() {
    devices = await api("/api/admin/devices");
    if (state) renderDevices();
  }

  async function loadState() {
    state = await api("/api/admin/state");
    status = state.status;
    translator = state.translator;
    fillForm();
    renderShare();
    renderStatus();
    $("transcript").textContent = "";
    for (const seg of state.history) addTranscript(seg.text);
  }

  let lastTranslatorStatus = null;

  function connect() {
    const ws = new WebSocket(withPin(`ws://${location.host}/ws/admin`));
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === "tick") {
        renderLevel(msg.level_db);
        renderListeners(msg.listeners, msg.audio_listeners);
        renderStats(msg.stats, msg.gpu);
        status = msg.status;
        translator = msg.translator;
        renderStatus();
        const trKey = `${translator.engine}:${translator.status}`;
        if (trKey !== lastTranslatorStatus) {
          // Languages become known once the translator is ready.
          if (lastTranslatorStatus !== null && translator.status === "ready" && !formDirty) loadState();
          lastTranslatorStatus = trKey;
        }
      } else if (msg.type === "status") {
        status = msg;
        renderStatus();
      } else if (msg.type === "segment") {
        addTranscript(msg.text);
      } else if (msg.type === "partial") {
        $("partial").textContent = msg.text;
      }
    };
    ws.onclose = () => setTimeout(connect, 2000);
  }

  applyTexts();
  Promise.all([loadState(), loadDevices()]).then(() => { renderDevices(); formDirty = false; });
  connect();
})();
