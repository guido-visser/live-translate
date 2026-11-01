# Live Translate

Live translation of a church service to phones, fully local: no subscriptions, no cloud.

```
Mixer / mic / livestream audio ──► Whisper speech recognition (GPU) ──► LibreTranslate
                                                                             │
           phones on the church Wi-Fi ◄── web page with live subtitles ◄─────┤
                                      ◄── optional spoken translation (Piper) ◄┘
```

* **Operator (the PC):** start the app, pick the audio input, press **Start**. **Pause** (or the space bar) stops translating temporarily.
* **Listeners:** scan the QR code, choose a language, read along. Tap **Listen** to hear the translation through earphones. The audio keeps playing with the screen off.

Everything runs on one Windows, macOS or Linux computer. An NVIDIA GPU is recommended; it can be the livestream PC that also runs vMix/OBS.

## Install (Windows)

1. Download `LiveTranslate-Setup-<version>.exe` from the [Releases](../../releases) page (or build it, see below) and run it.
2. Start **Live Translate** from the Start menu. The **first start downloads about 3 GB** (Python, the speech-recognition runtime, LibreTranslate, language models and voices), so do this once at home or before the service with internet. After that it works offline.
3. The operator page opens in its own window (a small wrapper around WebView2, the Edge engine in Windows). Closing that window keeps Live Translate running in the tray. The page also still works in a browser at `http://127.0.0.1:8000/admin`.
4. While it runs, Live Translate sits in the **system tray** (next to the clock) with a coloured icon: grey = stopped, blue = loading, green = translating, orange = paused or testimony. Hover for the status and number of listeners; click it to open the operator page; right-click for *Show QR code* and **Exit**. Starting Live Translate again while it runs just opens the operator page. Logs: `%LOCALAPPDATA%\LiveTranslate\livetranslate.log`.

The installer adds a Windows Firewall rule for TCP port 8000 so phones can connect, and installs the current Microsoft Visual C++ runtime when the PC has one older than 14.40 (older ones make the speech and voice libraries crash). If speech recognition or a voice can't load, the operator page says why instead of the app closing; crashes inside native libraries are written to `%LOCALAPPDATA%\LiveTranslate\crash.log`.

**Updates.** When the PC has internet, Live Translate looks for a new version on the [Releases](../../releases) page shortly after it starts and every 6 hours after that. When there is one, the operator page shows a bar with **Update now**. Nothing installs by itself, and updating is only possible while translation is stopped. Windows asks for permission, Live Translate closes, the new version installs, and the app starts again. The first start after an update shows a console window for a minute while it updates its environment. Turn the check off or check by hand under **Settings → Network → Updates**. To publish an update, raise `version` in `pyproject.toml` and push a `v<version>` tag: the workflow builds the installer and attaches it to the release.

### From source (Windows, macOS, Linux)

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run `start.cmd` (Windows) or `./start.sh` (macOS/Linux).

## Using it

The operator page has the controls needed during a service; everything else is under **⚙ Settings**.

**Main screen**

* **Start / Stop translating** and **Pause** (also the space bar).
* **Spoken language**: the main language plus the quick languages (keys `1`, `2`, `3`…). Switching is immediate.
* **Level meter**, **live transcript** and **listeners** per language.
* **Audio**: input type, device, channel and gain. Gain applies immediately; other changes reopen the input.
* **vMix**: *Send translated text to vMix* and *Overlay* switches.
* Header: **Performance** (shows or hides the performance figures), **QR code** (the code people scan, with a full-screen option), **Settings**, interface language. Language names follow the interface language.

**Settings** (tabs)

* **Church**: the church's name, with a button that protects it in the glossary. **Save profile** / **Load profile** put the name, languages and glossary in one file, to move to another PC or share with another church. **Adjust song detection** shows on a logged service what the app would skip. Move the slider between "Translate more" and "Skip more", or click part of the timeline, listen if the sound was recorded, and say whether it was music or speech; the slider then moves to the position that fits best. Nothing is retrained: it only changes how readily the app calls something a song, and it applies right away.
* **Audio**: input device and channel, system audio (Windows loopback; BlackHole on macOS, "Monitor of …" on Linux) or a file; gain; speech-recognition model and processor.
* **Languages**: main language, languages listeners can choose (🔊 = has a voice), spoken translation, translation engine (LibreTranslate or NLLB).
* **Quick languages**: which spoken languages appear as buttons on the main screen.
* **Glossary**: names and church words in the main language (Dutch) with their **English** word, e.g. *gemeente* → *church* instead of "municipality". The English word also steers the other languages, so you don't need to know Russian or Ukrainian; fill those in only when a native speaker knows a better word. Speech recognition is primed with the Dutch words so names come out right. Separate several forms with a comma (*gemeente, gemeenten*). A word written with a capital only matches with that capital (*Heer* = Lord, not *de heer Jansen*). **A name that must not be translated:** fill in the same word under English (*Kandelaar* → *Kandelaar*), and the name in Cyrillic under Russian and Ukrainian so it reads and sounds right there. Add the forms it comes in (*De Kandelaar, de Kandelaar, Kandelaar*); a lowercase *kandelaar* in a Bible reading is still translated. **Try it** translates a sample sentence and shows each translation back in English, so you can check the meaning. A starter list of common church words comes with the app.
* **vMix**: Web Controller address, title input and text field, overlay channel, maximum characters, overlay switching with the spoken language, test text.
* **Network**: address in the QR code, Wi-Fi name (shown with the QR code), admin PIN, port.
* **Startup**: start with Windows (when this user signs in), with the window normal, minimized on the taskbar, or only the tray icon; begin translating as soon as the app starts (it keeps trying for 5 minutes if the audio input isn't there yet), and whether the operator page opens in its own window or in the browser. For a PC that starts entirely by itself, also turn on automatic sign-in in Windows.

### Spoken language and vMix

When the spoken language is not the main language (for example a testimony in Russian):

* Phones get the original speech in that language and translations in the other languages.
* With *Send translated text to vMix* on, the main-language translation goes to the chosen vMix title as rolling subtitles (previous + current sentence, limited by *Max. characters*).
* With *Switch overlay with the spoken language* on, switching away from the main language clears the title and brings the overlay in; switching back takes it out. The *Overlay* switch on the main screen works at any time.

vMix setup: enable **Settings → Web Controller** in vMix (port 8088), add a Title input, then select it in Live Translate under **Settings → vMix**.

### If vMix/OBS starts dropping frames

Speech recognition shares the GPU. When the GPU is short: under **Settings → Audio** pick a smaller speech-recognition **model** (`medium` or `small`) or set **Run on** to *CPU*. When the processor is short: make sure NLLB runs on the GPU (**Settings → Languages**). Changes apply immediately. The *Performance* panel shows GPU load, memory and the delay.

| Model | GPU memory (approx.) | Quality |
|---|---|---|
| large-v3-turbo (default) | ~1.5 GB | very good, fast |
| large-v3 | ~3 GB | best, slower |
| medium | ~1 GB | good |
| small | ~0.5 GB | fair; OK on CPU |

### Listening over the internet (5G)

When the church Wi-Fi is unreliable, phones can also listen over their own mobile internet. Live Translate then keeps an SSH tunnel open from this PC to a server of your own (for example a VPS with Plesk), which serves a public HTTPS address and passes listeners through the tunnel. The church router needs no changes, and the operator page can never be reached this way: the tunnel ends on a separate port on this PC that only serves the listener page.

Set it up under **Settings → Network → Listening over the internet**. Fill in the public address, the server, the SSH user and the port on the server. *Server setup* shows the nginx lines to paste into the subdomain's *Additional nginx directives* in Plesk, and this PC's key for `~/.ssh/authorized_keys`, limited to this one tunnel. The QR code then shows the public address, which works on the Wi-Fi as well. The PC needs a reliable internet connection during the service; each listener uses about 50 kbit/s of its upload. The tunnel reconnects by itself, and the settings page shows whether the public address actually reaches this PC.

## How it works

* **Speech recognition**: [faster-whisper](https://github.com/SYSTRAN/faster-whisper). The audio is re-recognized every second; words two passes agree on are committed (LocalAgreement), and a sentence is released at punctuation or when the speaker pauses. Silero VAD skips silence and instrumental music, and captions Whisper writes itself ("ZINGEN EN MUZIEK", "[muziek]", "♪") are dropped. The typical delay is 1–3 s after the end of a sentence.
* **Songs**: [YAMNet](https://github.com/tensorflow/models/tree/master/research/audioset/yamnet) (Apache 2.0, included as `stt/yamnet.onnx`) scores the last 2 seconds about once a second. While that is music without speech and the voice detector doesn't hear a steady voice, recognition pauses and nothing is translated. When unsure, it translates. Speech over music mostly gets through, less so as the music gets louder than the voice. The switch on the main screen turns this off, and the header shows when it is active.
* **Keeping the processor free for the livestream**: the sound detection and the voices run on the GPU through DirectML (about 0.1 GB per sound detection, 0.2–0.4 GB per voice in use; they fall back to the CPU with 2 threads). The app runs at below-normal priority, so a stream on the same PC goes first. Use NLLB on the GPU: on the CPU each sentence takes 3–4 CPU-seconds per language.
* **Translation**, two engines you can switch between on the operator page (only languages someone is actually reading or listening to are translated):
  * **LibreTranslate**: a private [LibreTranslate](https://github.com/LibreTranslate/LibreTranslate) the app installs and runs on `127.0.0.1:5001` on the CPU. It goes through English (Dutch → English → Ukrainian). You can also use an existing LibreTranslate server by putting its URL in `libretranslate_url` in the settings file.
  * **NLLB** ([Meta NLLB-200](https://huggingface.co/facebook/nllb-200-distilled-600M), run with CTranslate2): translates directly between 200 languages and is much better for Ukrainian. *600M* (~0.6 GB, ~0.6 s per sentence on the CPU) or *1.3B* (~1.4 GB, ~1 s, slightly better). It runs on the GPU when there is one (~0.6 GB). The model downloads the first time you select it. License: CC-BY-NC 4.0 (non-commercial use).
* **Glossary**: Whisper gets the words as *hotwords*. For a language without its own translation, the English word goes into the Dutch sentence ("Welkom in de church") and the engine translates it from there, in the right grammatical case. It is a strong hint, not a guarantee, so pick English words with one meaning ("donations", not "offering", which can mean a sacrifice). A translation filled in for a language is used as is: NLLB gets it in the sentence; LibreTranslate would lose Cyrillic on its way through English, so there the word becomes a `[1]` placeholder and is put back afterwards, not declined. A name (English word the same as the spoken word) is a placeholder with both engines. If a placeholder gets lost, the sentence is translated without the glossary. None of this slows translation down: each sentence is still translated once per language.
* **Speech**: [Piper](https://github.com/OHF-Voice/piper1-gpl) voices (downloaded on demand), streamed per language as a continuous MP3 stream, only while someone listens.
* **Privacy**: nothing is recorded or stored unless you turn on the service log. Transcripts otherwise exist only in memory; only settings are saved (`%LOCALAPPDATA%\LiveTranslate\settings.json`).
* **Service log** (off by default, **Settings → Service log**): one folder per service in `%LOCALAPPDATA%\LiveTranslate\service-logs`, with `events.jsonl` (sentences, translations, Whisper's confidence, what the sound detection heard, moments marked with **M** on the main screen) and optionally `audio.flac`. It stays on the PC and folders older than 30 days are removed. It can contain personal things such as prayers, so ask your church leadership first. The header shows when it is on.
* **Admin protection**: the operator page only works on the PC itself. To control it from a tablet, set an admin PIN and open `http://<pc-address>:8000/admin?pin=<PIN>`.

Data (models, logs, the LibreTranslate environment) lives in `%LOCALAPPDATA%\LiveTranslate` (Windows), `~/Library/Application Support/LiveTranslate` (macOS) or `~/.local/share/LiveTranslate` (Linux).

## Known limitations

* **Ukrainian with LibreTranslate**: its English → Ukrainian model is weak and sometimes produces Bible-like phrases that weren't said. Use the **NLLB** engine for Ukrainian.
* **Phones and sleeping screens**: text needs the screen on. The page tries to keep it awake, but browsers only fully allow that over HTTPS. Spoken audio does keep playing with the screen off.
* The first sentence after pressing Start can take longer while the model loads.

## Development

```bash
uv sync
uv run pytest
uv run python -m livetranslate --port 8000   # --no-tray on a headless server
```

Build the Windows installer (needs [Inno Setup 6](https://jrsoftware.org/isdl.php)):

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
```

The GitHub Actions workflow builds the installer on every push and attaches it to a release when a `v*` tag is pushed.
