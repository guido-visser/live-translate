# Privacy policy

*Live Translate, last updated 19 November 2026*

Live Translate runs entirely on your own PC. **It doesn't collect, send or share any information about you, your church or your listeners.** The developer receives nothing from the app: no usage statistics, no crash reports, no audio and no text.

## What stays on the PC

* **Speech, transcripts and translations** are made on the PC and sent only to the phones and streaming software that you connect to it. By default nothing of what is said is stored.
* **Settings, church profiles, glossary and training data** are saved in the data folder you chose during installation.
* **The app log** (`livetranslate.log`) holds technical messages, such as which device was used and what went wrong. It stays in the data folder.
* **The service log** is off unless you turn it on under **Settings**. It then keeps, per service, the sentences, the translations and (if you choose so) a recording of the audio, so problems can be found afterwards. It stays in the data folder, and folders older than 30 days are removed automatically.

Nothing in these files leaves the PC unless **you** send it to someone yourself, for example when you attach a log to a bug report. If you turn on the service log with audio, it records what is said in your service: tell your speakers, as your church would for any recording.

## On the listeners' phones

The listening page is served by the PC itself, over your own network. It uses no cookies, accounts or trackers. The phone's browser remembers only the listener's own choices (language, text size, light or dark) in its local storage. These are never sent anywhere.

## When the app uses the internet

Live Translate only connects to other systems for these purposes. They receive the ordinary request for a file, which, as with any website, includes your PC's IP address. They don't receive anything from your services.

| What | Who | When |
|---|---|---|
| Speech and translation models, Python packages | Hugging Face, the Python Package Index, GitHub | While installing, and when you choose another model |
| Checking for and downloading updates | GitHub | Shortly after the app starts and every few hours after that, unless you untick **Check for new versions automatically** under **Settings → About**. Downloading only when you press **Update now** |
| Videos for training | YouTube | Only the videos you choose under **Settings → Training** |

Their own privacy policies apply to those requests.

### Systems you set up yourself

If you turn them on, Live Translate also sends to systems that **you** choose and run, such as a LibreTranslate server, vMix or OBS, or your own server for listening over the internet. What happens to the data there is up to you and whoever runs that system.

## Children

Live Translate doesn't ask for or store anyone's name, age or contact details, including children's.

## Changes and questions

Changes to this policy are made in this file, and its history can be seen on GitHub. Questions can be asked by [opening an issue](https://github.com/guido-visser/live-translate/issues).
