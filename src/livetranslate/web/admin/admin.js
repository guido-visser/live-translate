(() => {
  "use strict";

  const { $, t, api, withPin, langName } = window.LT;

  let state = null; // last /api/admin/state
  let status = { state: "stopped", paused: false, error: "" };
  let translator = { status: "stopped", detail: "", needs_restart: false };
  let vmix = { ok: null, error: "", last_text: "", overlay: null };
  let lastTranslatorKey = null;

  // -- status & controls -----------------------------------------------------------

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
    renderSpeakers();
  }

  $("startBtn").addEventListener("click", async () => {
    await api(status.state === "stopped" ? "/api/admin/start" : "/api/admin/stop", { method: "POST" });
  });

  $("pauseBtn").addEventListener("click", async () => {
    const res = await api("/api/admin/pause", { method: "POST", body: JSON.stringify({ paused: !status.paused }) });
    status.paused = res.paused;
    renderStatus();
  });

  // -- spoken language -------------------------------------------------------------

  function mainLanguage() {
    return state?.settings.source_language;
  }

  function renderSpeakers() {
    const box = $("speakerButtons");
    const langs = state?.speaker_languages || [];
    const current = status.speaker_language || mainLanguage();
    const signature = `${window.LT.lang}|${langs.map((l) => l.code).join()}`;
    if (box.dataset.langs !== signature) {
      box.dataset.langs = signature;
      box.textContent = "";
      langs.forEach((lang, i) => {
        const b = document.createElement("button");
        b.type = "button";
        b.dataset.lang = lang.code;
        b.setAttribute("role", "radio");
        b.innerHTML = `<span></span><span class="key">${i + 1}</span>`;
        b.firstChild.textContent = langName(lang.code, lang.name);
        b.addEventListener("click", () => setSpeaker(lang.code));
        box.appendChild(b);
      });
    }
    box.querySelectorAll("button").forEach((b) => {
      const active = b.dataset.lang === current;
      b.classList.toggle("active", active);
      b.classList.toggle("foreign", b.dataset.lang !== mainLanguage());
      b.setAttribute("aria-checked", String(active));
    });
  }

  async function setSpeaker(lang) {
    if (lang === status.speaker_language) return;
    status.speaker_language = lang;
    renderSpeakers();
    try {
      const res = await api("/api/admin/speaker", { method: "POST", body: JSON.stringify({ lang }) });
      renderVmix(res.vmix);
    } catch (err) {
      $("error").textContent = err.message;
      $("error").hidden = false;
    }
  }

  const typing = () => ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName);

  document.addEventListener("keydown", (e) => {
    if (typing() || e.ctrlKey || e.altKey || e.metaKey || $("qrDialog").open) return;
    if (e.code === "Space" && document.activeElement.tagName !== "BUTTON") {
      e.preventDefault();
      if (!$("pauseBtn").disabled) $("pauseBtn").click();
      return;
    }
    const lang = state?.speaker_languages?.[Number(e.key) - 1];
    if (lang) setSpeaker(lang.code);
  });

  // -- vMix quick panel ------------------------------------------------------------

  function vmixConfigured() {
    const v = state?.settings.vmix;
    return Boolean(v && v.input && v.field);
  }

  function setSwitch(el, on, disabled) {
    el.setAttribute("aria-checked", String(Boolean(on)));
    el.classList.toggle("on", Boolean(on));
    el.disabled = Boolean(disabled);
  }

  function renderVmix(s) {
    if (s) vmix = s;
    if (!state) return;
    const configured = vmixConfigured();
    setSwitch($("vmixSend"), state.settings.vmix.enabled, !configured);
    setSwitch($("vmixOverlay"), vmix.overlay === true, !configured);
    const el = $("vmixStatus");
    if (!configured) {
      el.className = "small muted";
      el.textContent = t("vmixNotConfigured");
    } else if (vmix.ok === true) {
      el.className = "small vmix-ok";
      el.textContent = t("vmixOk");
    } else if (vmix.ok === false) {
      el.className = "small vmix-error";
      el.textContent = `${t("vmixError")}: ${vmix.error}`;
    } else {
      el.className = "small muted";
      el.textContent = t("vmixUnknown");
    }
    $("vmixLast").textContent = vmix.last_text || "";
    $("vmixLast").hidden = !vmix.last_text;
  }

  $("vmixSend").addEventListener("click", async () => {
    const settings = structuredClone(state.settings);
    settings.vmix.enabled = !settings.vmix.enabled;
    state = await window.LT.saveSettings(settings);
    renderVmix();
  });

  $("vmixOverlay").addEventListener("click", async () => {
    try {
      renderVmix(await api("/api/admin/vmix/overlay", { method: "POST", body: JSON.stringify({ on: vmix.overlay !== true }) }));
    } catch (err) {
      renderVmix({ ...vmix, ok: false, error: err.message });
    }
  });

  // -- quick audio panel ------------------------------------------------------------

  let devices = [];
  const qKind = () => document.querySelector("input[name=qkind]:checked")?.value || "device";

  function renderAudio() {
    if (!state) return;
    const audio = state.settings.audio;
    const radio = document.querySelector(`input[name=qkind][value=${audio.kind}]`);
    if (radio && document.activeElement?.name !== "qkind") radio.checked = true;
    const file = qKind() === "file";
    $("qDeviceRow").hidden = file;
    $("qChannelBox").hidden = file;
    $("qFile").hidden = !file;
    $("qFile").textContent = audio.file_path ? audio.file_path.split(/[\\/]/).pop() : "—";
    if (!file && document.activeElement !== $("qDevice")) {
      window.LT.fillDevices($("qDevice"), devices, qKind(), audio.device);
    }
    const channels = Number($("qDevice").selectedOptions[0]?.dataset.channels || 2);
    if (document.activeElement !== $("qChannel")) window.LT.fillChannels($("qChannel"), channels, audio.channel);
    if (document.activeElement !== $("qGain")) $("qGain").value = audio.gain;
    $("qGainText").textContent = `${Number($("qGain").value).toFixed(2).replace(/0$/, "")}×`;
  }

  async function saveAudio() {
    const settings = structuredClone(state.settings);
    settings.audio.kind = qKind();
    if (settings.audio.kind !== "file") {
      settings.audio.device = $("qDevice").value || null;
      settings.audio.channel = Number($("qChannel").value || 0);
    }
    settings.audio.gain = Number($("qGain").value);
    try {
      state = await window.LT.saveSettings(settings);
      $("qStatus").hidden = true;
    } catch (err) {
      $("qStatus").textContent = err.message;
      $("qStatus").hidden = false;
    }
    renderAudio();
  }

  async function loadDevices() {
    try {
      devices = await api("/api/admin/devices");
    } catch {
      devices = [];
    }
    renderAudio();
  }

  document.querySelectorAll("input[name=qkind]").forEach((r) =>
    r.addEventListener("change", () => {
      if (qKind() !== "file") window.LT.fillDevices($("qDevice"), devices, qKind(), null);
      saveAudio();
    })
  );
  $("qDevice").addEventListener("change", () => {
    const channels = Number($("qDevice").selectedOptions[0]?.dataset.channels || 2);
    window.LT.fillChannels($("qChannel"), channels, 0);
    saveAudio();
  });
  $("qChannel").addEventListener("change", saveAudio);
  $("qGain").addEventListener("input", () => {
    $("qGainText").textContent = `${Number($("qGain").value).toFixed(2).replace(/0$/, "")}×`;
  });
  $("qGain").addEventListener("change", saveAudio);
  $("qRefresh").addEventListener("click", loadDevices);

  // -- performance toggle -------------------------------------------------------------

  function showPerformance(on) {
    $("perfCard").hidden = !on;
    $("perfBtn").setAttribute("aria-pressed", String(on));
    $("perfBtn").classList.toggle("active", on);
    try { localStorage.setItem("lt.admin.perf", on ? "1" : "0"); } catch { /* ignore */ }
  }
  $("perfBtn").addEventListener("click", () => showPerformance($("perfCard").hidden));
  let perfSaved = false;
  try { perfSaved = localStorage.getItem("lt.admin.perf") === "1"; } catch { /* ignore */ }
  showPerformance(perfSaved);

  // -- QR dialog -------------------------------------------------------------------

  $("qrBtn").addEventListener("click", () => {
    $("qrImg").src = withPin(`/api/qr.svg?t=${Date.now()}`);
    $("url").textContent = state?.url || "";
    $("url").href = state?.url || "#";
    const wifi = state?.settings.wifi_name;
    $("wifiLine").textContent = wifi ? `Wi-Fi: ${wifi}` : "";
    $("qrDialog").showModal();
  });
  $("qrClose").addEventListener("click", () => $("qrDialog").close());
  $("qrFull").addEventListener("click", () => window.open("/qr", "qr", "width=900,height=900"));
  $("qrDialog").addEventListener("click", (e) => { if (e.target === $("qrDialog")) $("qrDialog").close(); });

  // -- transcript, listeners, stats, level -------------------------------------------

  function renderListeners(text = {}, audio = {}) {
    const rows = $("listenerRows");
    rows.textContent = "";
    const langs = window.LT.sortByName(new Set([...Object.keys(text), ...Object.keys(audio)]));
    if (!langs.length) {
      rows.innerHTML = `<tr><td class="muted"></td></tr>`;
      rows.querySelector("td").textContent = t("none");
      return;
    }
    for (const lang of langs) {
      const tr = document.createElement("tr");
      tr.innerHTML = "<td></td><td class=\"num\"></td><td class=\"num\"></td>";
      tr.children[0].textContent = langName(lang);
      tr.children[1].textContent = `${text[lang] || 0} ${t("text")}`;
      tr.children[2].textContent = audio[lang] ? `${audio[lang]} ${t("audioCol")}` : "";
      rows.appendChild(tr);
    }
  }

  function addTranscript(text, lang) {
    const box = $("transcript");
    const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
    const p = document.createElement("p");
    p.textContent = text;
    if (lang && lang !== mainLanguage()) {
      p.classList.add("foreign");
      p.dataset.lang = lang.toUpperCase();
    }
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

  // -- loading & live updates --------------------------------------------------------

  async function loadState() {
    state = await api("/api/admin/state");
    status = state.status;
    translator = state.translator;
    renderStatus();
    renderVmix(state.vmix);
    renderAudio();
    $("transcript").textContent = "";
    for (const seg of state.history) addTranscript(seg.text, seg.lang);
  }

  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(withPin(`${proto}://${location.host}/ws/admin`));
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === "tick") {
        renderLevel(msg.level_db);
        lastCounts = [msg.listeners, msg.audio_listeners];
        renderListeners(msg.listeners, msg.audio_listeners);
        renderStats(msg.stats, msg.gpu);
        status = msg.status;
        translator = msg.translator;
        renderStatus();
        renderVmix(msg.vmix);
        const key = `${translator.engine}:${translator.status}`;
        if (key !== lastTranslatorKey) {
          // Language names become known once the translator is ready.
          if (lastTranslatorKey !== null && translator.status === "ready") loadState();
          lastTranslatorKey = key;
        }
      } else if (msg.type === "status") {
        status = msg;
        renderStatus();
      } else if (msg.type === "segment") {
        addTranscript(msg.text, msg.lang);
      } else if (msg.type === "partial") {
        $("partial").textContent = msg.text;
      }
    };
    ws.onclose = () => setTimeout(connect, 2000);
  }

  // Settings may change in the other page (or tab): pick them up when we come back.
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") loadState(); });

  let lastCounts = [{}, {}];
  window.LT.onLanguageChange(() => { renderStatus(); renderVmix(); renderListeners(...lastCounts); renderAudio(); });
  window.LT.applyTexts();
  renderListeners();
  loadState();
  loadDevices();
  connect();
})();
