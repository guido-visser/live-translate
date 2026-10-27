# Live Translate

Live translation of a church service to phones, fully local: no subscriptions, no cloud.

```
Mixer / mic / livestream audio ──► Whisper speech recognition (GPU) ──► LibreTranslate
                                                                             │
           phones on the church Wi-Fi ◄── web page with live subtitles ◄─────┤
                                      ◄── optional spoken translation (Piper) ◄┘
```

* **Operator (the PC):** start the app, pick the audio input, press **Start**. Press **Pause** (or the space bar) during songs.
* **Listeners:** scan the QR code, choose a language, read along. Tap **Listen** to hear the translation through earphones. The audio keeps playing with the screen off.

Everything runs on one Windows, macOS or Linux computer. An NVIDIA GPU is recommended; it can be the livestream PC that also runs vMix/OBS.

## Install (Windows)

1. Download `LiveTranslate-Setup-<version>.exe` from the [Releases](../../releases) page (or build it, see below) and run it.
2. Start **Live Translate** from the Start menu. The **first start downloads about 3 GB** (Python, the speech-recognition runtime, LibreTranslate, language models and voices), so do this once at home or before the service with internet. After that it works offline.
3. The operator page opens in the browser at `http://127.0.0.1:8000/admin`.

The installer adds a Windows Firewall rule for TCP port 8000 so phones can connect.

### From source (Windows, macOS, Linux)

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run `start.cmd` (Windows) or `./start.sh` (macOS/Linux).

## Using it on Sunday

1. **Audio source** – choose:
   * *Input device*: the audio interface or mixer feed (best quality). For a multi-channel interface pick the channel that carries the speaker.
   * *System audio*: what the PC is playing (Windows WASAPI loopback). On macOS use [BlackHole](https://github.com/ExistentialAudio/BlackHole); on Linux choose the "Monitor of …" input.
   * *File (test)*: play a recording to rehearse.
   Watch the level meter. Speech should be in the green/orange, not red.
2. **Speaker's language** (default Dutch) and the **languages listeners may choose**. 🔊 marks languages that also have a voice.
   **Translation engine**: choose *NLLB* for the best Ukrainian (see below); *LibreTranslate* is the lighter default.
3. **Network**: check the address in the QR code is the PC's address on the *church Wi-Fi*. Enter the Wi-Fi name; it is shown on the QR page.
4. Press **Start translation**. Open **Full-screen QR code** on a projector/screen, or print it.
5. During songs press **Pause (song)** (or the space bar) so music isn't "translated".

### If vMix/OBS starts dropping frames

Speech recognition shares the GPU. In *Speech recognition* pick a smaller **model** (`medium` or `small`) or set **Run on** to *CPU*. Changes apply immediately. The *Performance* panel shows GPU load, memory and the delay.

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
uv run python -m livetranslate --port 8000
```

Build the Windows installer (needs [Inno Setup 6](https://jrsoftware.org/isdl.php)):

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
```

The GitHub Actions workflow builds the installer on every push and attaches it to a release when a `v*` tag is pushed.
