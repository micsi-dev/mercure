"""
test_license.py
===============
"""
import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Callable, Dict

import common.license as license
import nacl.signing
import pytest
from common.types import Config
from process import processor
from pytest_mock import MockerFixture

from .test_processor import config_partial, create_and_route

DAY = 86400
NOW = 1_800_000_000  # 2027-01-15

signing_key = nacl.signing.SigningKey.generate()
other_key = nacl.signing.SigningKey.generate()


def make_license(expires_in_days: float, products=("pet",), issued_days_ago: float = 30,
                 key: nacl.signing.SigningKey = signing_key, now: float = NOW) -> bytes:
    """Builds a license file the same way the MICSI license tool does."""
    expires_unix = int(now + expires_in_days * DAY)
    issued_unix = int(now - issued_days_ago * DAY)
    payload = json.dumps({
        "expires": time.strftime("%Y-%m-%d", time.gmtime(expires_unix)),
        "expires_unix": expires_unix,
        "features": list(products),
        "id": "MICSI-TEST",
        "issued": time.strftime("%Y-%m-%d", time.gmtime(issued_unix)),
        "issued_unix": issued_unix,
        "licensee": "Test Hospital",
    }, sort_keys=True, separators=(",", ":"))
    signature = key.sign(payload.encode()).signature.hex()
    return json.dumps({"payload": payload, "sig": signature}).encode()


@pytest.fixture(autouse=True)
def test_key(mocked):
    mocked.patch.object(license, "PUBLIC_KEY_HEX", signing_key.verify_key.encode().hex())


def install_file(fs, data: bytes) -> None:
    fs.create_file(license.license_path(), contents=data)


def test_verify_accepts_valid_license():
    payload = license.verify_bytes(make_license(365))
    assert payload["licensee"] == "Test Hospital"


def test_verify_rejects_other_key():
    with pytest.raises(ValueError, match="signature"):
        license.verify_bytes(make_license(365, key=other_key))


def test_verify_rejects_modified_payload():
    wrapper = json.loads(make_license(30))
    wrapper["payload"] = wrapper["payload"].replace('"features":["pet"]', '"features":["pet","rmt"]')
    with pytest.raises(ValueError, match="signature"):
        license.verify_bytes(json.dumps(wrapper).encode())


@pytest.mark.parametrize("data", [b"", b"not json", b"{}", b'{"payload": 1, "sig": "00"}', b'{"payload": "{}", "sig": "zz"}'])
def test_verify_rejects_malformed(data):
    with pytest.raises(ValueError):
        license.verify_bytes(data)


@pytest.mark.parametrize("expires_in_days,state,allowed", [
    (365, license.STATE_VALID, True),
    (61, license.STATE_VALID, True),
    (59, license.STATE_WARNING, True),
    (0.5, license.STATE_WARNING, True),
    (-1, license.STATE_GRACE, True),
    (-13.9, license.STATE_GRACE, True),
    (-14.1, license.STATE_EXPIRED, False),
    (-400, license.STATE_EXPIRED, False),
])
def test_evaluate_states(expires_in_days, state, allowed):
    status = license.evaluate(license.verify_bytes(make_license(expires_in_days)), NOW)
    assert status.state == state
    assert status.processing_allowed == allowed


def test_evaluate_rejects_clock_before_issue_date():
    status = license.evaluate(license.verify_bytes(make_license(365, issued_days_ago=-5)), NOW)
    assert status.state == license.STATE_INVALID
    assert not status.processing_allowed


def test_status_missing(fs):
    status = license.get_status(NOW)
    assert status.state == license.STATE_MISSING
    assert not status.processing_allowed


def test_status_bad_signature(fs):
    install_file(fs, make_license(365, key=other_key))
    status = license.get_status(NOW)
    assert status.state == license.STATE_INVALID
    assert not status.processing_allowed


def test_clock_rollback_does_not_extend_license(fs):
    install_file(fs, make_license(10))
    # The server has been seen running 30 days from now, after which the clock is set back
    assert license.get_status(NOW + 30 * DAY).state == license.STATE_EXPIRED
    assert license.get_status(NOW).state == license.STATE_EXPIRED


@pytest.mark.parametrize("docker_tag,product", [
    ("micsi/pet-module:1.2.0", "pet"),
    ("micsi/pet-module@sha256:abcd", "pet"),
    ("docker.io/micsi/pet-module:v1.2", "pet"),
    ("micsi/mercure-module:1.0", "rmt"),
    ("micsi/pet-module-dev:1.0", None),
    ("busybox:stable", None),
    ("localhost:5000/other:1", None),
])
def test_product_for_image(docker_tag, product):
    assert license.product_for_image(docker_tag) == product


def test_check_module_requires_licensed_product():
    status = license.evaluate(license.verify_bytes(make_license(365, products=["pet"])), NOW)
    assert license.check_module(status, "micsi/pet-module:1.2.0") is None
    assert "RMT" in license.check_module(status, "micsi/mercure-module:1.0")
    assert license.check_module(status, "busybox:stable") is None


def test_check_module_blocks_everything_when_expired():
    status = license.evaluate(license.verify_bytes(make_license(-20, products=["pet"])), NOW)
    assert license.check_module(status, "busybox:stable") is not None


def test_install_rejects_invalid_file(fs):
    with pytest.raises(ValueError):
        license.install(make_license(365, key=other_key))
    assert not license.license_path().exists()


def test_install_keeps_expired_license(fs):
    license.install(make_license(-400, now=time.time()))
    assert license.get_status().state == license.STATE_EXPIRED


@pytest.mark.asyncio
@pytest.mark.parametrize("license_data,runs", [
    (lambda: make_license(365, now=time.time()), True),
    (lambda: make_license(-20, now=time.time()), False),
    (lambda: None, False),
])
async def test_processing_requires_license(fs, mercure_config: Callable[[Dict], Config], mocked: MockerFixture,
                                           license_data, runs):
    config = mercure_config({"process_runner": "docker", **config_partial})
    data = license_data()
    if data is not None:
        install_file(fs, data)

    task_id = str(uuid.uuid1())
    files, _ = create_and_route(fs, mocked, task_id, config)

    async def fake_runtime(task, folder, file_count_begin, task_processing) -> bool:
        for child in (folder / "in").iterdir():
            shutil.copy(child, folder / "out" / child.name)
        return True

    fake_run = mocked.AsyncMock(side_effect=fake_runtime)
    mocked.patch("process.process_series.docker_runtime", new=fake_run)
    await processor.run_processor()

    assert fake_run.called == runs
    result_folder = Path("/var/success") if runs else Path("/var/error")
    assert set(files) <= {k.name for k in result_folder.glob("**/*") if k.is_file()}


def test_webgui_installs_valid_license(test_client):
    upload = {"license_file": ("site.miclic", make_license(365, now=time.time()))}
    response = test_client.post("/configuration/license", files=upload, follow_redirects=False)
    assert response.status_code == 303
    assert "license=1" in response.headers["location"]
    assert license.get_status().licensee == "Test Hospital"

    page = test_client.get("/configuration")
    assert "Test Hospital" in page.text


def test_webgui_rejects_invalid_license(test_client):
    upload = {"license_file": ("site.miclic", make_license(365, key=other_key))}
    response = test_client.post("/configuration/license", files=upload, follow_redirects=True)
    assert "License file was not installed" in response.text
    assert not license.license_path().exists()


def test_webgui_shows_banner_without_license(test_client):
    page = test_client.get("/configuration")
    assert "No license installed" in page.text
