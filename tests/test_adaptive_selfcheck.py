"""The packaging self-check exercises production matching without devices."""
import json
import sys

import cv2
import numpy as np

from engine import __main__ as engine_main


def test_offline_selfcheck_uses_two_frames_and_assertion_without_starting_app(tmp_path, monkeypatch, capsys):
    template = np.full((48, 48, 3), 255, np.uint8)
    cv2.rectangle(template, (4, 4), (43, 43), (20, 40, 220), 3)
    cv2.line(template, (10, 32), (35, 12), (0, 0, 0), 3)
    screen = np.full((150, 180, 3), 255, np.uint8)
    screen[40:88, 50:98] = template
    for report in (156, 161):
        assert cv2.imwrite(str(tmp_path / f"{report}-template.png"), template)
        assert cv2.imwrite(str(tmp_path / f"{report}-screen.png"), screen)
    monkeypatch.setattr(sys, "argv", ["autodroid-engine", "--check-image-match", str(tmp_path)])
    # An accidental app import must fail, rather than opening a local gateway.
    monkeypatch.setitem(sys.modules, "engine.app", None)
    assert engine_main.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] and output["offline"]
    first, second = output["cases"]
    assert first["captures"] == first["stable_frames"] == 2
    assert second["captures"] == 1
    assert first["point"] == second["point"] == [74, 64]
    assert first["template_sha256"] == second["template_sha256"]
    assert first["branch"] == second["branch"] == "whole"
    assert "engine.app" in sys.modules and sys.modules["engine.app"] is None


def test_cli_missing_directory_argument_does_not_start_the_engine(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["autodroid-engine", "--check-image-match"])
    monkeypatch.setitem(sys.modules, "engine.app", None)
    assert engine_main.main() == 2
    assert "fixtures_dir" in capsys.readouterr().err
