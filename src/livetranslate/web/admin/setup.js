// The first-time wizard. Nothing is saved until the last step, so going back is always safe.
(() => {
  "use strict";

  const { $, t, api, withPin, langName, sortByName, languageMatches } = window.LT;

  // Essential steps can't be skipped: the app can't translate without knowing these languages.
  const STEPS = [
    { id: "welcome" },
    { id: "church", optional: true },
    { id: "spoken", ready: () => !!s.source_language },
    { id: "listeners", ready: () => s.target_languages.length > 0 },
    { id: "audio", optional: true },
    { id: "streaming", optional: true },
    { id: "extras", optional: true },
    { id: "prepare" },
  ];

  let state = null;
  let s = null; // the settings being put together
  let devices = [];
  let step = 0;
  let polling = null;
  let finished = null;
  const prepareRows = new Map(); // kept between updates, so the bars can move instead of being redrawn
  let prepareKeys = "";
  let followInterface = false; // the spoken language follows the interface language until someone picks one

  // -- language lists ------------------------------------------------------------------------

  const catalog = () => state.catalogs[s.translation.engine];
  const spokenCodes = () => catalog().filter((c) => state.whisper_languages.includes(c));

  function drawList(box, codes, type, isChosen, onChange, search, empty) {
    box.textContent = "";
    for (const code of sortByName(codes)) {
      const label = document.createElement("label");
      label.className = "wizard-item";
      const input = document.createElement("input");
      input.type = type;
      input.name = box.id;
      input.value = code;
      input.checked = isChosen(code);
      input.addEventListener("change", () => onChange(code, input.checked));
      const name = document.createElement("span");
      name.textContent = langName(code);
      label.append(input, name);
      const own = window.LT.autonym(code);
      if (own && own !== langName(code)) {
        const small = document.createElement("span");
        small.className = "muted small";
        small.textContent = own;
        label.append(small);
      }
      if (type === "checkbox" && box.id === "wizTargetsList" && state.voices.includes(code)) {
        const voice = document.createElement("span");
        voice.textContent = "🔊";
        voice.title = t("wizTts");
        label.append(voice);
      }
      box.appendChild(label);
    }
    filter(box, search, empty);
  }

  function filter(box, search, empty) {
    let shown = 0;
    box.querySelectorAll("label").forEach((label) => {
      label.hidden = !languageMatches(label.querySelector("input").value, search.value);
      if (!label.hidden) shown++;
    });
    if (empty) empty.hidden = shown > 0;
  }

  function toggle(list, code, on) {
    const without = list.filter((c) => c !== code);
    return on ? [...without, code] : without;
  }

  function drawSpoken() {
    drawList($("wizSpokenList"), spokenCodes(), "radio", (c) => c === s.source_language, (code) => {
      followInterface = false;
      s.source_language = code;
      s.target_languages = s.target_languages.filter((c) => c !== code);
      s.quick_languages = s.quick_languages.filter((c) => c !== code);
      drawQuick();
      drawTargets(); // the language that was spoken before can now be translated to
      updateFoot();
    }, $("wizSpokenSearch"), $("wizSpokenNone"));
    drawQuick();
  }

  function drawQuick() {
    const codes = spokenCodes().filter((c) => c !== s.source_language);
    drawList($("wizQuickList"), codes, "checkbox", (c) => s.quick_languages.includes(c), (code, on) => {
      s.quick_languages = toggle(s.quick_languages, code, on);
    }, $("wizQuickSearch"));
  }

  function drawTargets() {
    const codes = catalog().filter((c) => c !== s.source_language);
    drawList($("wizTargetsList"), codes, "checkbox", (c) => s.target_languages.includes(c), (code, on) => {
      s.target_languages = toggle(s.target_languages, code, on);
      drawChosen();
      updateFoot();
    }, $("wizTargetsSearch"), $("wizTargetsNone"));
    drawChosen();
  }

  // Also shows the choices the search is hiding right now.
  function drawChosen() {
    const names = sortByName(s.target_languages).map((c) => langName(c));
    $("wizChosen").textContent = names.length ? `${t("wizChosen")}: ${names.join(", ")}` : t("wizChooseOne");
  }

  $("wizSpokenSearch").addEventListener("input", () => filter($("wizSpokenList"), $("wizSpokenSearch"), $("wizSpokenNone")));
  $("wizQuickSearch").addEventListener("input", () => filter($("wizQuickList"), $("wizQuickSearch")));
  $("wizTargetsSearch").addEventListener("input", () => filter($("wizTargetsList"), $("wizTargetsSearch"), $("wizTargetsNone")));

  document.querySelectorAll("input[name=wizEngine]").forEach((radio) => radio.addEventListener("change", () => {
    s.translation.engine = radio.value;
    const offered = new Set(catalog());
    s.target_languages = s.target_languages.filter((c) => offered.has(c));
    s.quick_languages = s.quick_languages.filter((c) => offered.has(c));
    drawTargets();
    updateFoot();
  }));

  // -- the other steps ---------------------------------------------------------------------

  const wizKind = () => document.querySelector("input[name=wizKind]:checked")?.value || "device";

  function drawDevices() {
    window.LT.fillDevices($("wizDevice"), devices, wizKind(), $("wizDevice").value || s.audio.device);
    drawChannels();
  }

  function drawChannels() {
    const channels = Number($("wizDevice").selectedOptions[0]?.dataset.channels || 2);
    window.LT.fillChannels($("wizChannel"), channels, $("wizChannel").value || s.audio.channel);
  }

  document.querySelectorAll("input[name=wizKind]").forEach((radio) => radio.addEventListener("change", () => {
    $("wizDevice").value = "";
    drawDevices();
  }));
  $("wizDevice").addEventListener("change", drawChannels);

  function fillInputs() {
    $("wizChurchName").value = s.church_name;
    $("wizChurchShort").value = s.church_short_name;
    $("wizTts").checked = s.tts_enabled;
    document.querySelector(`input[name=wizEngine][value=${s.translation.engine}]`).checked = true;
    document.querySelector(`input[name=wizKind][value=${s.audio.kind === "loopback" ? "loopback" : "device"}]`).checked = true;
    document.querySelector(`input[name=wizStream][value=${s.main_streaming}]`).checked = true;
    $("wizWindowsOnly").hidden = !state.windows;
    $("wizStartWindows").checked = s.start_with_windows;
    $("wizAutoStart").checked = s.auto_start_translation;
    $("wizWifi").value = s.wifi_name;
    drawSpoken();
    drawTargets();
    drawDevices();
  }

  function readInputs() {
    s.church_name = $("wizChurchName").value.trim();
    s.church_short_name = $("wizChurchShort").value.trim();
    s.tts_enabled = $("wizTts").checked;
    s.audio.kind = wizKind();
    s.audio.device = $("wizDevice").value || null;
    s.audio.channel = Number($("wizChannel").value || 0);
    s.main_streaming = document.querySelector("input[name=wizStream]:checked").value;
    s.start_with_windows = $("wizStartWindows").checked;
    s.auto_start_translation = $("wizAutoStart").checked;
    s.wifi_name = $("wizWifi").value.trim();
  }

  $("wizProfile").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file) return;
    try {
      const profile = JSON.parse(await file.text());
      if (!Array.isArray(profile.glossary)) throw new Error("no glossary");
      for (const key of state.profile_fields) {
        if (key in profile) s[key] = profile[key];
      }
      if (profile.song_model) {
        await api("/api/admin/training/model", { method: "POST", body: JSON.stringify({ model: profile.song_model }) });
      }
      followInterface = false;
      fillInputs();
      $("wizProfileStatus").textContent = `✓ ${t("wizProfileLoaded")}`;
    } catch {
      $("wizProfileStatus").textContent = t("profileBad");
    }
  });

  // -- walking through the steps ---------------------------------------------------------------

  function updateFoot() {
    const current = STEPS[step];
    const inPrepare = current.id === "prepare";
    $("wizBack").hidden = step === 0 || inPrepare;
    $("wizSkip").hidden = !current.optional;
    if (!inPrepare) {
      $("wizNext").hidden = false;
      $("wizNext").textContent = t(current.id === "welcome" ? "wizStart" : current.id === "extras" ? "wizInstall" : "wizNext");
      $("wizNext").disabled = current.ready ? !current.ready() : false;
    }
    const total = STEPS.length - 1; // the welcome screen doesn't count as a question
    $("wizardCount").textContent = step === 0 ? t("wizFirstTime") : t("wizStepOf").replace("{n}", step).replace("{m}", total);
    $("wizardFill").style.width = `${(step / total) * 100}%`;
  }

  function show(index) {
    step = index;
    document.querySelectorAll("[data-step]").forEach((el) => (el.hidden = el.dataset.step !== STEPS[index].id));
    updateFoot();
    scrollTo(0, 0);
  }

  async function next(skipped) {
    if (skipped) {
      // Skipping means leaving this step as it was before: the defaults, not half-filled fields.
      const id = STEPS[step].id;
      if (id === "church") $("wizChurchName").value = $("wizChurchShort").value = "";
      if (id === "streaming") document.querySelector("input[name=wizStream][value=none]").checked = true;
      if (id === "extras") {
        $("wizStartWindows").checked = $("wizAutoStart").checked = false;
        $("wizWifi").value = "";
      }
    }
    readInputs();
    if (STEPS[step + 1].id === "prepare") await beginPreparing();
    else show(step + 1);
  }

  async function beginPreparing() {
    $("wizError").hidden = true;
    $("wizNext").disabled = true;
    try {
      window.LT.protectNames(s.glossary, s.source_language, s.church_name, s.church_short_name);
      state = await window.LT.saveSettings(s);
      s = structuredClone(state.settings);
      await api("/api/admin/setup/prepare", { method: "POST" });
    } catch (err) {
      $("wizError").textContent = err.message;
      $("wizError").hidden = false;
      $("wizNext").disabled = false;
      return;
    }
    show(STEPS.length - 1);
    followPreparation();
  }

  // -- preparing ---------------------------------------------------------------------------------

  const megabytes = (bytes) => Math.round(bytes / 1e6).toLocaleString(window.LT.lang); // not GB: 0.1 GB is a long wait

  function preparationRow() {
    const row = document.createElement("div");
    const head = document.createElement("div");
    head.className = "wizard-row-head";
    const name = document.createElement("span");
    const detail = document.createElement("span");
    detail.className = "muted small";
    head.append(name, detail);
    const bar = document.createElement("div");
    bar.className = "wizard-progress";
    const fill = document.createElement("div");
    bar.append(fill);
    // The heading already says what to do; the technical message is only for whoever helps out.
    const more = document.createElement("details");
    more.className = "wizard-detail small muted";
    const summary = document.createElement("summary");
    const message = document.createElement("span");
    more.append(summary, message);
    row.append(head, bar, more);
    return { row, name, detail, fill, more, summary, message };
  }

  function drawPreparation(status) {
    const rows = $("wizPrepareRows");
    const keys = status.steps.map((item) => item.key).join();
    if (keys !== prepareKeys) {
      prepareKeys = keys;
      rows.textContent = "";
      prepareRows.clear();
      for (const item of status.steps) {
        prepareRows.set(item.key, preparationRow());
        rows.append(prepareRows.get(item.key).row);
      }
    }
    for (const item of status.steps) {
      const parts = prepareRows.get(item.key);
      const { row, name, detail, fill, more, summary, message } = parts;
      row.className = `wizard-row ${item.state}`;
      name.textContent = t(`wizPrep_${item.key}`);
      detail.textContent = "";
      if (item.state === "done") detail.textContent = "✓";
      else if (item.state === "waiting") detail.textContent = t("wizWaiting");
      else if (item.state === "busy" && item.total) detail.textContent = `${megabytes(Math.min(item.done, item.total))} / ${megabytes(item.total)} MB`;
      else if (item.state === "busy") detail.textContent = t("wizWorking");
      const unknown = item.state === "busy" && !item.total;
      const share = item.state === "done" ? 100 : item.total ? Math.min(100, (item.done / item.total) * 100) : 0;
      // Only progress glides. From the "busy" look, or when starting over, the bar would run backwards.
      const jump = unknown || fill.className === "unknown" || share < (parts.share ?? 0);
      fill.style.transition = jump ? "none" : "";
      fill.className = unknown ? "unknown" : "";
      fill.style.width = unknown ? "" : `${share}%`;
      if (jump) void fill.offsetWidth; // makes the jump happen before the transition is back
      parts.share = share;
      more.hidden = item.state !== "error";
      summary.textContent = t("wizErrorDetails");
      message.textContent = item.error;
    }
    const failed = status.finished && !status.ok;
    $("wizPrepareTitle").textContent = t(status.ok ? "wizDone" : failed ? "wizFailed" : "wizPreparing");
    $("wizPrepareText").textContent = t(status.ok ? "wizDoneText" : failed ? "wizFailedText" : "wizPreparingText");
    $("wizTips").hidden = !status.ok;
    $("wizSkip").hidden = !failed;
    $("wizSkip").textContent = t(failed ? "wizContinueAnyway" : "wizSkip");
    $("wizNext").hidden = !status.finished;
    $("wizNext").disabled = false;
    $("wizNext").textContent = t(status.ok ? "wizOpen" : "retry");
    finished = status.finished ? status : null;
  }

  function followPreparation() {
    clearInterval(polling);
    const tick = async () => {
      try {
        const status = await api("/api/admin/setup/status");
        drawPreparation(status);
        if (status.finished) clearInterval(polling);
      } catch { /* the next tick tries again */ }
    };
    tick();
    polling = setInterval(tick, 500);
  }

  async function finish() {
    await api("/api/admin/setup/finish", { method: "POST" });
    location.href = withPin("/admin");
  }

  $("wizNext").addEventListener("click", async () => {
    if (STEPS[step].id !== "prepare") return next(false);
    if (finished?.ok) return finish();
    finished = null;
    $("wizNext").disabled = true;
    await api("/api/admin/setup/prepare", { method: "POST" });
    followPreparation();
  });
  $("wizSkip").addEventListener("click", () => (STEPS[step].id === "prepare" ? finish() : next(true)));
  $("wizBack").addEventListener("click", () => { readInputs(); show(step - 1); });

  window.LT.onLanguageChange(() => {
    if (!state) return;
    if (followInterface) {
      s.source_language = window.LT.lang;
      s.target_languages = s.target_languages.filter((c) => c !== s.source_language);
    }
    drawSpoken();
    drawTargets();
    drawChannels();
    updateFoot();
    $("wizSkip").textContent = t("wizSkip");
    if (STEPS[step].id === "prepare") api("/api/admin/setup/status").then(drawPreparation);
  });

  window.LT.applyTexts();
  Promise.all([api("/api/admin/state"), api("/api/admin/devices"), api("/api/admin/setup/status")]).then(([st, d, status]) => {
    state = st;
    devices = d;
    s = structuredClone(state.settings);
    if (!new URLSearchParams(location.search).has("again")) {
      // A first time: no languages assumed. These are only what the settings start out with.
      followInterface = true;
      s.source_language = window.LT.lang; // the interface languages can all be spoken
      s.target_languages = [];
      s.quick_languages = [];
      s.translation.engine = "nllb"; // better translations, and it can do every language in the lists
    }
    fillInputs();
    if (status.started) {
      show(STEPS.length - 1);
      followPreparation();
    } else {
      show(0);
    }
  });
})();
