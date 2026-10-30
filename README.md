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
3. The operator page opens in the browser at `http://127.0.0.1:8000/admin`.
4. While it runs, Live Translate sits in the **system tray** (next to the clock) with a coloured icon: grey = stopped, blue = loading, green = translating, orange = paused or testimony. Hover for the status and number of listeners; click it to open the operator page; right-click for *Show QR code* and **Exit**. Starting Live Translate again while it runs just opens the operator page. Logs: `%LOCALAPPDATA%\LiveTranslate\livetranslate.log`.

The installer adds a Windows Firewall rule for TCP port 8000 so phones can connect.

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

* **Audio**: input device and channel, system audio (Windows loopback; BlackHole on macOS, "Monitor of …" on Linux) or a file; gain; speech-recognition model and processor.
* **Languages**: main language, languages listeners can choose (🔊 = has a voice), spoken translation, translation engine (LibreTranslate or NLLB).
* **Quick languages**: which spoken languages appear as buttons on the main screen.
* **Glossary**: names and church words in the main language (Dutch) with their **English** word, e.g. *gemeente* → *church* instead of "municipality". The English word also steers the other languages, so you don't need to know Russian or Ukrainian; fill those in only when a native speaker knows a better word. Speech recognition is primed with the Dutch words so names come out right. Separate several forms with a comma (*gemeente, gemeenten*). A word written with a capital only matches with that capital (*Heer* = Lord, not *de heer Jansen*). **Try it** translates a sample sentence and shows each translation back in English, so you can check the meaning. A starter list of common church words comes with the app.
* **vMix**: Web Controller address, title input and text field, overlay channel, maximum characters, overlay switching with the spoken language, test text.
* **Network**: address in the QR code, Wi-Fi name (shown with the QR code), admin PIN, port.

### Spoken language and vMix

When the spoken language is not the main language (for example a testimony in Russian):

* Phones get the original speech in that language and translations in the other languages.
* With *Send translated text to vMix* on, the main-language translation goes to the chosen vMix title as rolling subtitles (previous + current sentence, limited by *Max. characters*).
* With *Switch overlay with the spoken language* on, switching away from the main language clears the title and brings the overlay in; switching back takes it out. The *Overlay* switch on the main screen works at any time.

vMix setup: enable **Settings → Web Controller** in vMix (port 8088), add a Title input, then select it in Live Translate under **Settings → vMix**.

### If vMix/OBS starts dropping frames

Speech recognition shares the GPU. Under **Settings → Audio** pick a smaller speech-recognition **model** (`medium` or `small`) or set **Run on** to *CPU*. Changes apply immediately. The *Performance* panel shows GPU load, memory and the delay.

| Model | GPU memory (approx.) | Quality |
|---|---|---|
| large-v3-turbo (default) | ~1.5 GB | very good, fast |
| large-v3 | ~3 GB | best, slower |
| medium | ~1 GB | good |
| small | ~0.5 GB | fair; OK on CPU |

## How it works

* **Speech recognition**: [faster-whisper](https://github.com/SYSTRAN/faster-whisper). The audio is re-recognized every second; words two passes agree on are committed (LocalAgreement), and a sentence is released at punctuation or when the speaker pauses. Silero VAD skips silence and music. The typical delay is 1–3 s after the end of a sentence.
* **Translation**, two engines you can switch between on the operator page (only languages someone is actually reading or listening to are translated):
  * **LibreTranslate**: a private [LibreTranslate](https://github.com/LibreTranslate/LibreTranslate) the app installs and runs on `127.0.0.1:5001` on the CPU. It goes through English (Dutch → English → Ukrainian). You can also use an existing LibreTranslate server by putting its URL in `libretranslate_url` in the settings file.
  * **NLLB** ([Meta NLLB-200](https://huggingface.co/facebook/nllb-200-distilled-600M), run with CTranslate2): translates directly between 200 languages and is much better for Ukrainian. *600M* (~0.6 GB, ~0.6 s per sentence on the CPU) or *1.3B* (~1.4 GB, ~1 s, slightly better). It runs on the CPU by default so the GPU stays free for speech recognition and vMix. The model downloads the first time you select it. License: CC-BY-NC 4.0 (non-commercial use).
* **Glossary**: Whisper gets the words as *hotwords*. For a language without its own translation, the English word goes into the Dutch sentence ("Welkom in de church") and the engine translates it from there, in the right grammatical case. It is a strong hint, not a guarantee, so pick English words with one meaning ("donations", not "offering", which can mean a sacrifice). A translation filled in for a language is used as is: NLLB gets it in the sentence; LibreTranslate would lose Cyrillic on its way through English, so there the word becomes a `{1}` placeholder and is put back afterwards, not declined. If a placeholder gets lost, the sentence is translated without the glossary. None of this slows translation down: each sentence is still translated once per language.
* **Speech**: [Piper](https://github.com/OHF-Voice/piper1-gpl) voices (downloaded on demand), streamed per language as a continuous MP3 stream, only while someone listens.
* **Privacy**: nothing is recorded or stored. Transcripts exist only in memory; only settings are saved (`%LOCALAPPDATA%\LiveTranslate\settings.json`).
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
