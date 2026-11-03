"""The last step of the first-time wizard: download what the chosen settings need, with progress.

The same files would otherwise be fetched the moment they are first used, which on a first
Sunday means waiting while the service has started.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
from pathlib import Path

log = logging.getLogger(__name__)

WHISPER_PATTERNS = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]


def folder_size(path: Path) -> int:
    total = 0
    for file in path.rglob("*"):
        try:
            if file.is_file() and not file.is_symlink():
                total += file.stat().st_size
        except OSError:  # a download renamed it in the meantime
            pass
    return total


def repo_size(repo: str, patterns: list[str]) -> int | None:
    """What a download from Hugging Face will come to, to show progress; None when unknown."""
    from huggingface_hub import HfApi

    try:
        files = HfApi().model_info(repo, files_metadata=True, timeout=15).siblings or []
    except Exception:
        log.info("Could not look up the size of %s", repo, exc_info=True)
        return None
    sizes = [f.size or 0 for f in files if any(fnmatch.fnmatch(f.rfilename, p) for p in patterns)]
    return sum(sizes) or None


class Step:
    def __init__(self, key: str) -> None:
        self.key = key
        self.state = "waiting"  # waiting | busy | done | error
        self.error = ""
        self.folder: Path | None = None  # watched for progress while downloading
        self.total: int | None = None
        self.done = 0

    def status(self) -> dict:
        done = self.done
        if self.folder is not None and self.state == "busy" and self.folder.exists():
            done = folder_size(self.folder)
        return {"key": self.key, "state": self.state, "error": self.error, "done": done, "total": self.total}


class Preparation:
    def __init__(self, services) -> None:
        self.services = services
        self.steps: list[Step] = []
        self.task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def start(self) -> None:
        """Runs what is still to do; again after a failure, downloads carry on where they stopped."""
        if self.running:
            return
        settings = self.services.settings
        self.steps = [Step("speech"), Step("translation")]
        if settings.tts_enabled and any(self.services.speech.has_voice(l) for l in settings.target_languages):
            self.steps.append(Step("voices"))
        self.task = asyncio.create_task(self._run())

    def forget(self) -> None:
        """The wizard is starting over, so it shouldn't open on the previous result."""
        if not self.running:
            self.steps = []

    def status(self) -> dict:
        steps = [step.status() for step in self.steps]
        return {
            "started": bool(steps),
            "finished": bool(steps) and not self.running,
            "ok": bool(steps) and all(s["state"] == "done" for s in steps),
            "steps": steps,
        }

    async def _run(self) -> None:
        jobs = {"speech": self._speech, "translation": self._translation, "voices": self._voices}
        for step in self.steps:
            step.state = "busy"
            try:
                await jobs[step.key](step)
                step.state = "done"
            except Exception as exc:  # the wizard offers to try again or go on without it
                log.exception("Preparing %s failed", step.key)
                step.state, step.error = "error", str(exc)

    async def _speech(self, step: Step) -> None:
        from faster_whisper.utils import _MODELS, download_model

        pipeline = self.services.pipeline
        model = self.services.settings.asr.model
        pipeline.models_dir.mkdir(parents=True, exist_ok=True)
        repo = _MODELS.get(model, model)
        step.total = await asyncio.to_thread(repo_size, repo, WHISPER_PATTERNS)
        step.folder = pipeline.models_dir / f"models--{repo.replace('/', '--')}"
        await asyncio.to_thread(download_model, model, cache_dir=str(pipeline.models_dir))

    async def _translation(self, step: Step) -> None:
        from .translate.nllb import DOWNLOAD_PATTERNS, MODELS

        translator = self.services.translator
        if translator.active is translator.nllb:
            repo = MODELS[translator.nllb.model]
            step.total = await asyncio.to_thread(repo_size, repo, DOWNLOAD_PATTERNS)
            step.folder = translator.nllb.models_dir / repo.replace("/", "--")
        await translator.start()
        if translator.status == "error":
            raise RuntimeError(translator.detail)
        self.services.hub.to_listeners(self.services.languages_message())

    async def _voices(self, step: Step) -> None:
        speech = self.services.speech
        languages = [l for l in self.services.settings.target_languages if speech.has_voice(l)]
        step.total = await asyncio.to_thread(speech.bank.download_size, languages)

        def arrived(size: int) -> None:
            step.done += size

        for lang in languages:
            await asyncio.to_thread(speech.bank._files, lang, arrived)
