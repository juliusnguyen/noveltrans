"""NVIDIA CUDA detection for the VieNeu-TTS GPU path (opt-in `tts-gpu` extra).

`torch` is not a base dependency (see `tts-gpu` in pyproject.toml) — both
functions here guard the import so a CPU-only install just reads as "no GPU",
never raises.
"""

from __future__ import annotations


def cuda_available() -> bool:
    try:
        import torch
    except ImportError:
        return False
    try:
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def cuda_device_name() -> str:
    """Best-effort GPU name for a Settings tooltip; "" if unavailable."""
    if not cuda_available():
        return ""
    try:
        import torch

        return torch.cuda.get_device_name(0)
    except Exception:
        return ""
