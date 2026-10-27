(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const store = {
    get(key, fallback) {
      try { const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); } catch { return fallback; }
    },
    set(key, value) {
      try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode */ }
    },
  };

  const state = {
    languages: [],
    lang: store.get("lt.lang", null),
    running: false,
    paused: false,
    connected: false,
    audioOn: false,
    font: store.get("lt.font", 0),
    theme: store.get("lt.theme", null),
    segments: [],
  };

  let ws = null;
  let retry = 0;
  let typingTimer = null;

  // -- text ------------------------------------------------------------------

  function t(key) {
    const dict = window.I18N[state.lang] || window.I18N.en;
    return dict[key] || window.I18N.en[key] || key;
  }

  function langInfo(code) {
    return state.languages.find((l) => l.code === code);
  }

  function applyTexts() {
    const info = langInfo(state.lang);
    document.documentElement.lang = state.lang || "en";
    document.documentElement.dir = info && info.rtl ? "rtl" : "ltr";
    document.title = t("title");
    $("langName").textContent = info ? info.name : "…";
    $("audioLabel").textContent = state.audioOn ? t("listening") : t("listen");
    $("empty").textContent = state.running ? t("waiting") : t("notStarted");
    $("jump").title = t("newText");
    updateBanner();
  }

  function updateBanner() {
    const banner = $("banner");
    let text = "";
    if (!state.connected) text = t("reconnecting");
    else if (state.paused && state.running) text = t("paused");
    banner.textContent = text;
    banner.hidden = !text;
  }

  // -- feed ------------------------------------------------------------------

  const feed = $("feed");

  function nearBottom() {
    return window.innerHeight + window.scrollY >= document.body.scrollHeight - 120;
  }

  function scrollToEnd() {
    window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
    $("jump").hidden = true;
  }

  function renderSegments() {
    feed.querySelectorAll(".seg").forEach((el) => el.remove());
    for (const seg of state.segments) feed.appendChild(segmentEl(seg));
    markCurrent();
    $("empty").hidden = state.segments.length > 0;
  }

  function segmentEl(seg) {
    const p = document.createElement("p");
    p.className = "seg";
    p.dataset.id = seg.id;
    p.textContent = seg.text;
    return p;
  }

  function markCurrent() {
    const all = feed.querySelectorAll(".seg");
    all.forEach((el, i) => el.classList.toggle("current", i === all.length - 1));
  }

  function addSegment(seg) {
    const stick = nearBottom();
    if (state.segments.some((s) => s.id === seg.id)) return;
    // Keep id order even if translations finish out of order.
    let index = state.segments.length;
    while (index > 0 && state.segments[index - 1].id > seg.id) index--;
    state.segments.splice(index, 0, seg);
    const el = segmentEl(seg);
    const next = feed.querySelector(`.seg[data-id="${state.segments[index + 1]?.id}"]`);
    feed.insertBefore(el, next || null);
    while (state.segments.length > 200) {
      const old = state.segments.shift();
      feed.querySelector(`.seg[data-id="${old.id}"]`)?.remove();
    }
    $("empty").hidden = true;
    markCurrent();
    setTyping(false);
    if (stick) scrollToEnd();
    else $("jump").hidden = false;
  }

  function setPartial(text) {
    const el = $("partial");
    el.textContent = text;
    el.hidden = !text;
  }

  function setTyping(on) {
    $("typing").hidden = !on;
    clearTimeout(typingTimer);
    if (on) typingTimer = setTimeout(() => ($("typing").hidden = true), 2500);
  }

  // -- connection --------------------------------------------------------------

  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws/listen`);
    ws.onopen = () => {
      retry = 0;
      state.connected = true;
      updateBanner();
      if (state.lang) send({ type: "lang", lang: state.lang });
    };
    ws.onmessage = (ev) => handle(JSON.parse(ev.data));
    ws.onclose = () => {
      state.connected = false;
      updateBanner();
      setTimeout(connect, Math.min(1000 * 2 ** retry++, 10000));
    };
    ws.onerror = () => ws.close();
  }

  function send(msg) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
  }

  function handle(msg) {
    switch (msg.type) {
      case "init":
      case "languages":
        state.languages = msg.languages;
        if (msg.type === "init") {
          state.running = msg.running;
          state.paused = msg.paused;
        }
        renderLanguages();
        if (!state.lang || !langInfo(state.lang)) {
          if (state.lang && msg.type === "languages") stopAudio();
          showPicker(false);
        } else {
          updateAudioButton();
        }
        applyTexts();
        break;
      case "history":
        if (msg.lang !== state.lang) return;
        state.segments = msg.segments;
        renderSegments();
        scrollToEnd();
        break;
      case "segment":
        if (msg.lang && msg.lang !== state.lang) return;
        addSegment({ id: msg.id, text: msg.text });
        break;
      case "partial":
        setPartial(msg.text);
        if (msg.text && nearBottom()) window.scrollTo(0, document.body.scrollHeight);
        break;
      case "typing":
        setTyping(true);
        break;
      case "status":
        state.running = msg.running;
        state.paused = msg.paused;
        if (!msg.running || msg.paused) { setPartial(""); setTyping(false); }
        applyTexts();
        break;
    }
  }

  // -- language picker -----------------------------------------------------------

  function renderLanguages() {
    const list = $("langList");
    list.textContent = "";
    for (const lang of state.languages) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "lang-btn" + (lang.code === state.lang ? " active" : "");
      b.textContent = lang.name;
      b.dir = lang.rtl ? "rtl" : "ltr";
      b.addEventListener("click", () => chooseLanguage(lang.code));
      list.appendChild(b);
    }
  }

  function showPicker(closable) {
    renderLanguages();
    $("pickerClose").hidden = !closable;
    $("picker").hidden = false;
  }

  function hidePicker() {
    $("picker").hidden = true;
  }

  function chooseLanguage(code) {
    const changed = code !== state.lang;
    state.lang = code;
    store.set("lt.lang", code);
    hidePicker();
    keepAwake();
    if (changed) {
      state.segments = [];
      renderSegments();
      setPartial("");
      send({ type: "lang", lang: code });
      if (state.audioOn) startAudio();
    }
    updateAudioButton();
    applyTexts();
  }

  $("langChip").addEventListener("click", () => showPicker(Boolean(state.lang)));
  $("pickerClose").addEventListener("click", hidePicker);

  // -- spoken audio --------------------------------------------------------------

  const audio = $("audio");
  let audioWatch = null;

  function updateAudioButton() {
    const info = langInfo(state.lang);
    const available = Boolean(info && info.tts);
    $("audioBtn").hidden = !available;
    if (!available && state.audioOn) stopAudio();
  }

  function startAudio() {
    state.audioOn = true;
    audio.src = `/audio/${state.lang}.mp3?t=${Date.now()}`;
    const play = audio.play();
    if (play) play.catch(() => { stopAudio(); toast(t("audioError")); });
    if ("mediaSession" in navigator) {
      navigator.mediaSession.metadata = new MediaMetadata({ title: t("title"), artist: langInfo(state.lang)?.name || "" });
    }
    clearInterval(audioWatch);
    // Stay close to live: if the browser buffered too much, jump ahead.
    audioWatch = setInterval(() => {
      try {
        if (audio.buffered.length) {
          const end = audio.buffered.end(audio.buffered.length - 1);
          if (end - audio.currentTime > 4) audio.currentTime = end - 0.5;
        }
      } catch { /* not seekable */ }
    }, 3000);
    renderAudioState();
  }

  function stopAudio() {
    state.audioOn = false;
    clearInterval(audioWatch);
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
    renderAudioState();
  }

  function renderAudioState() {
    const btn = $("audioBtn");
    btn.setAttribute("aria-pressed", String(state.audioOn));
    btn.classList.toggle("on", state.audioOn);
    btn.querySelector(".icon").textContent = state.audioOn ? "🔊" : "🔈";
    $("audioLabel").textContent = state.audioOn ? t("listening") : t("listen");
  }

  audio.addEventListener("error", () => {
    if (state.audioOn) setTimeout(() => state.audioOn && startAudio(), 2000);
  });

  $("audioBtn").addEventListener("click", () => {
    if (state.audioOn) stopAudio();
    else {
      startAudio();
      if (!store.get("lt.audioHint", false)) {
        toast(t("audioHint"));
        store.set("lt.audioHint", true);
      }
    }
  });

  // -- screen awake --------------------------------------------------------------

  let wakeLock = null;
  async function keepAwake() {
    try {
      if ("wakeLock" in navigator && window.isSecureContext) {
        wakeLock = await navigator.wakeLock.request("screen");
        return;
      }
    } catch { /* fall through */ }
    // Plain http on the local network has no Wake Lock; a tiny playing video
    // keeps many phones awake instead.
    try {
      const video = $("keepAwake");
      if (!video.srcObject) {
        const canvas = document.createElement("canvas");
        canvas.width = canvas.height = 2;
        const ctx = canvas.getContext("2d");
        setInterval(() => { ctx.fillStyle = `#00000${Date.now() % 2}`; ctx.fillRect(0, 0, 2, 2); }, 1000);
        video.srcObject = canvas.captureStream(1);
      }
      await video.play();
    } catch { /* best effort */ }
  }
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && state.lang && (wakeLock === null || wakeLock.released)) keepAwake();
  });

  // -- text size & theme ------------------------------------------------------------

  function applyFont() {
    document.documentElement.style.setProperty("--font-scale", String(1 + state.font * 0.15));
  }
  function applyTheme() {
    if (state.theme) document.documentElement.dataset.theme = state.theme;
    else delete document.documentElement.dataset.theme;
  }

  $("fontBtn").addEventListener("click", () => ($("fontPanel").hidden = !$("fontPanel").hidden));
  $("fontPanel").addEventListener("click", (ev) => {
    const step = ev.target.closest("[data-font]")?.dataset.font;
    if (step) {
      state.font = Math.max(-2, Math.min(5, state.font + Number(step)));
      store.set("lt.font", state.font);
      applyFont();
    }
  });
  $("themeBtn").addEventListener("click", () => {
    const dark = state.theme ? state.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    state.theme = dark ? "light" : "dark";
    store.set("lt.theme", state.theme);
    applyTheme();
  });

  $("jump").addEventListener("click", scrollToEnd);
  window.addEventListener("scroll", () => { if (nearBottom()) $("jump").hidden = true; }, { passive: true });

  function toast(text) {
    const el = document.createElement("div");
    el.className = "toast";
    el.textContent = text;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 5000);
  }

  applyFont();
  applyTheme();
  applyTexts();
  connect();
})();
