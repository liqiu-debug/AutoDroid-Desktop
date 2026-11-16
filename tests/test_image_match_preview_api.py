"""Local HTTP integration: real image preview, fake cloud storage, no devices."""
import base64
import hashlib
from unittest.mock import Mock

import cv2
import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine import local_api
from engine.core import recording_service


def png_bytes(image):
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


def screenshot_fixture(duplicate=False):
    template = np.full((40, 40, 3), 230, np.uint8)
    cv2.circle(template, (17, 17), 10, (30, 30, 30), 2, cv2.LINE_AA)
    cv2.line(template, (24, 24), (34, 34), (30, 30, 30), 3, cv2.LINE_AA)
    screen = np.full((160, 220, 3), 245, np.uint8)
    screen[40:80, 30:70] = template
    if duplicate:
        screen[90:130, 150:190] = template
    return screen


def encoded(screen):
    return base64.b64encode(png_bytes(screen)).decode("ascii")


@pytest.fixture
def preview_client(tmp_path, monkeypatch):
    storage = tmp_path / "templates"
    storage.mkdir()

    class FakeCloudBridge:
        available = True
        uploads = []
        downloads = []

        def __init__(self, **kwargs):
            pass

        def upload_template(self, raw, filename):
            self.uploads.append((raw, filename))
            path = f"static/images/element_preview_{len(self.uploads)}.png"
            (storage / path.rsplit("/", 1)[-1]).write_bytes(raw)
            return path

        def ensure_template_local(self, path):
            self.downloads.append(path)
            target = storage / path.rsplit("/", 1)[-1]
            if not target.is_file():
                raise FileNotFoundError(path)
            return str(target)

    guards = []
    for target in ("_connect_recording_device", "_perform_device_operation", "CrossPlatformRunner", "execute_single_step"):
        guard = Mock(side_effect=AssertionError("preview must not acquire/tap a device"))
        monkeypatch.setattr(recording_service, target, guard)
        guards.append(guard)
    for pool in (recording_service._recording_android_device_pool, recording_service.ios_device.driver_pool):
        guard = Mock(side_effect=AssertionError("preview must not acquire a device"))
        monkeypatch.setattr(pool, "acquire", guard)
        guards.append(guard)
    monkeypatch.setattr(local_api, "CloudBridge", FakeCloudBridge)
    monkeypatch.setattr(local_api, "template_cache_dir", lambda: storage)
    app = FastAPI()
    app.include_router(local_api.router)
    with TestClient(app) as client:
        yield client, FakeCloudBridge, storage
    for guard in guards:
        guard.assert_not_called()


def crop(client, screen=None):
    response = client.post("/api/device/crop_template", json={
        "screenshot_base64": encoded(screen if screen is not None else screenshot_fixture()),
        "x1": 30, "y1": 40, "x2": 70, "y2": 80,
    })
    assert response.status_code == 200, response.text
    return response.json()


def config(metadata):
    return {"template_sha256": metadata["template_sha256"], "source_size": metadata["source_size"], "tap_anchor": [.1, .75]}


def preview(client, metadata, screen=None, options=None, **extra):
    payload = {"screenshot_base64": encoded(screen if screen is not None else screenshot_fixture()),
               "image_path": metadata["image_path"], **extra}
    if options is not None:
        payload["image_options"] = options
    return client.post("/api/device/match_image", json=payload)


def assert_diagnostic(payload):
    assert {"status", "reason", "matcher_version", "candidates", "point", "quality", "timings"}.issubset(payload)
    assert payload["matcher_version"]
    for candidate in payload["candidates"]:
        assert len(candidate["rect"]) == 4
        assert len(candidate["point"]) == 2
        assert isinstance(candidate["accepted"], bool)
        assert isinstance(candidate["metrics"], dict)
        assert "similarity" in candidate


def test_crop_metadata_fingerprints_uploaded_and_cached_template(preview_client):
    client, bridge, storage = preview_client
    metadata = crop(client)
    raw = (storage / metadata["image_path"].rsplit("/", 1)[-1]).read_bytes()
    assert metadata["template_sha256"] == hashlib.sha256(raw).hexdigest()
    assert metadata["source_size"] == [220, 160]
    assert metadata["quality"]["valid"]
    assert metadata["quality"]["size"] == [40, 40]
    assert isinstance(metadata["quality"]["warnings"], list)
    assert bridge.uploads[0][0] == raw


def test_local_preview_matches_supplied_frame_and_noncentral_anchor(preview_client):
    client, bridge, _ = preview_client
    metadata = crop(client)
    response = preview(client, metadata, options=config(metadata))
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_diagnostic(payload)
    assert payload["status"] == "matched"
    assert payload["point"] == [34, 70]
    assert bridge.downloads == [metadata["image_path"]]
    legacy = preview(client, metadata)
    assert legacy.json()["status"] == "matched"
    assert legacy.json()["point"] == [50, 60]


def test_jpeg_recording_frame_retains_pixel_geometry_and_legacy_route_alias(preview_client):
    client, _, _ = preview_client
    ok, jpeg = cv2.imencode(".jpg", screenshot_fixture(), [cv2.IMWRITE_JPEG_QUALITY, 90])
    assert ok
    screenshot = base64.b64encode(jpeg).decode("ascii")
    response = client.post("/device/crop_template", json={
        "screenshot_base64": screenshot, "x1": 30, "y1": 40, "x2": 70, "y2": 80,
    })
    assert response.status_code == 200, response.text
    metadata = response.json()
    assert metadata["source_size"] == [220, 160]
    response = client.post("/device/match_image", json={
        "screenshot_base64": screenshot, "image_path": metadata["image_path"], "image_options": config(metadata),
    })
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "matched"
    assert response.json()["point"] == [34, 70]


def test_local_preview_exposes_ambiguous_candidates_and_honors_explicit_region(preview_client):
    client, _, _ = preview_client
    metadata = crop(client)
    image = screenshot_fixture(duplicate=True)
    response = preview(client, metadata, image, config(metadata))
    assert response.status_code == 200, response.text
    payload = response.json()
    assert_diagnostic(payload)
    assert payload["status"] == "ambiguous"
    assert payload["point"] is None
    assert sum(c["accepted"] for c in payload["candidates"]) >= 2
    selected = preview(client, metadata, image, {**config(metadata), "search_region": [0, 0, .5, 1]})
    assert selected.json()["status"] == "matched"
    assert selected.json()["point"] == [34, 70]


def test_local_preview_exposes_hash_mismatch_without_matching(preview_client):
    client, _, _ = preview_client
    metadata = crop(client)
    response = preview(client, metadata, options={**config(metadata), "template_sha256": "0" * 64})
    assert response.status_code == 200
    payload = response.json()
    assert_diagnostic(payload)
    assert (payload["status"], payload["reason"]) == ("invalid_template", "template_hash_mismatch")
    assert payload["point"] is None


def test_flat_crop_quality_is_not_silently_accepted(preview_client):
    client, _, _ = preview_client
    metadata = crop(client, np.full((160, 220, 3), 255, np.uint8))
    assert metadata["quality"]["valid"] is False
    response = preview(client, metadata, options=config(metadata))
    assert response.status_code == 200
    assert response.json()["status"] == "invalid_template"
    assert response.json()["point"] is None


def test_local_preview_rejects_invalid_config_and_malformed_frame(preview_client):
    client, _, _ = preview_client
    metadata = crop(client)
    for options in ({"tap_anchor": [.1, .2]}, {**config(metadata), "tap_anchor": [-1, .5]},
                    {**config(metadata), "source_size": [0, 160]},
                    {**config(metadata), "search_region": [0, 0, 0, 1]}, {"android": config(metadata)}):
        response = preview(client, metadata, options=options)
        assert response.status_code == 400, response.text
        assert "detail" in response.json()
    # Frontend strips the data URL prefix; API accepts raw base64, not a URL.
    for screenshot in ("not-base64", "data:image/png;base64," + encoded(screenshot_fixture())):
        response = preview(client, metadata, screenshot_base64=screenshot)
        assert response.status_code == 400, response.text


def test_local_preview_rejects_paths_before_cache_or_network_lookup(preview_client):
    client, bridge, _ = preview_client
    for path in ("../outside.png", "static/images/../../outside.png", "/tmp/outside.png",
                 "https://example.invalid/template.png", "images/template.png", "static/images/template.svg"):
        response = preview(client, {"image_path": path})
        assert response.status_code == 400, response.text
    assert bridge.downloads == []


def test_local_preview_missing_template_is_an_explicit_error(preview_client):
    client, bridge, _ = preview_client
    response = preview(client, {"image_path": "static/images/missing.png"})
    assert response.status_code == 502
    assert "模板读取失败" in response.json()["detail"]
    assert bridge.downloads == ["static/images/missing.png"]
