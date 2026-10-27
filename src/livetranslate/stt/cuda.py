"""Make the pip-installed CUDA libraries (cuBLAS, cuDNN) findable by CTranslate2."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

_done = False


def setup_cuda_dll_paths() -> None:
    global _done
    if _done:
        return
    _done = True
    try:
        import nvidia
    except ImportError:
        return
    sub = "bin" if sys.platform == "win32" else "lib"
    for base in map(Path, nvidia.__path__):
        for lib_dir in base.glob(f"*/{sub}"):
            if sys.platform == "win32":
                os.add_dll_directory(str(lib_dir))
            # CTranslate2 loads cuDNN lazily via the normal search path.
            var = "PATH" if sys.platform == "win32" else "LD_LIBRARY_PATH"
            os.environ[var] = str(lib_dir) + os.pathsep + os.environ.get(var, "")
            log.debug("Added CUDA library dir %s", lib_dir)


def cuda_available() -> bool:
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False
