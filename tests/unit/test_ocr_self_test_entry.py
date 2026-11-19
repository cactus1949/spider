"""冻结产物的验证码自测在启动服务之前退出。"""

from __future__ import annotations

import backend_entry
import pytest


@pytest.mark.unit
def test_self_test_flag_reports_success_without_starting(monkeypatch, capsys):
    monkeypatch.setattr(
        "spider_gd.core.crawler.ocr_probe.ocr_self_test",
        lambda: "ok /tmp/onnxruntime_pybind11_state.pyd",
    )
    code = backend_entry._dispatch_ocr_self_test(["spider-gd-service", "--self-test-ocr"])
    assert code == 0
    assert "onnxruntime_pybind11_state" in capsys.readouterr().out


@pytest.mark.unit
def test_self_test_flag_fails_when_native_import_fails(monkeypatch):
    def broken() -> str:
        raise ImportError("DLL load failed while importing onnxruntime_pybind11_state")

    monkeypatch.setattr("spider_gd.core.crawler.ocr_probe.ocr_self_test", broken)
    assert backend_entry._dispatch_ocr_self_test(["spider-gd-service", "--self-test-ocr"]) == 1
    assert backend_entry._dispatch_ocr_self_test(["spider-gd-service"]) is None
