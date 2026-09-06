"""NVIDIA CUDA detection for the VieNeu-TTS GPU path (feature 082)."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

from noveltrans.tts.gpu import cuda_available, cuda_device_name


class TestCudaAvailable:
    def test_false_when_torch_is_not_installed(self):
        with patch.dict(sys.modules, {"torch": None}):
            assert cuda_available() is False

    def test_true_when_torch_reports_cuda(self):
        mock_torch = MagicMock()
        mock_torch.cuda.is_available.return_value = True
        with patch.dict(sys.modules, {"torch": mock_torch}):
            assert cuda_available() is True

    def test_false_when_torch_reports_no_cuda(self):
        mock_torch = MagicMock()
        mock_torch.cuda.is_available.return_value = False
        with patch.dict(sys.modules, {"torch": mock_torch}):
            assert cuda_available() is False

    def test_false_rather_than_raising_on_a_broken_torch_install(self):
        mock_torch = MagicMock()
        mock_torch.cuda.is_available.side_effect = RuntimeError("driver mismatch")
        with patch.dict(sys.modules, {"torch": mock_torch}):
            assert cuda_available() is False


class TestCudaDeviceName:
    def test_empty_when_cuda_is_unavailable(self):
        with patch.dict(sys.modules, {"torch": None}):
            assert cuda_device_name() == ""

    def test_returns_the_device_name_when_available(self):
        mock_torch = MagicMock()
        mock_torch.cuda.is_available.return_value = True
        mock_torch.cuda.get_device_name.return_value = "NVIDIA GeForce RTX 3060 Ti"
        with patch.dict(sys.modules, {"torch": mock_torch}):
            assert cuda_device_name() == "NVIDIA GeForce RTX 3060 Ti"

    def test_empty_rather_than_raising_if_the_name_lookup_fails(self):
        mock_torch = MagicMock()
        mock_torch.cuda.is_available.return_value = True
        mock_torch.cuda.get_device_name.side_effect = RuntimeError("no such device")
        with patch.dict(sys.modules, {"torch": mock_torch}):
            assert cuda_device_name() == ""
