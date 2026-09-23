"""
license.py
==========
Offline license check for MICSI mercure installations.

A license is a `.miclic` file produced by the MICSI license tool (the same tool and format as
miew, but signed with a separate mercure key):

    {"payload": "<json string>", "sig": "<hex ed25519 signature over the payload bytes>"}

The payload carries `licensee` (customer / site name), `id`, `issued`, `issued_unix`, `expires`,
`expires_unix` and `features` (the licensed products, e.g. ["pet", "rmt"]).

No network access is needed: the signature is checked against the public key below. Processing is
allowed while the license is valid and for GRACE_DAYS after it expires; after that new processing
jobs are rejected. DICOM receiving and routing are never blocked.
"""

# Standard python includes
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import nacl.exceptions
import nacl.signing

# Ed25519 public key of the MICSI mercure license signing key. This is deliberately a different key
# from the one used for miew licenses. There is no fallback: if the key is wrong, no license verifies.
PUBLIC_KEY_HEX = "de5304362bda91821762996892acaf5ebc0b1502969c9205a19d783580fb1419"

LICENSE_FILENAME = "license.miclic"
# Remembers the latest time seen, so that setting the system clock back does not extend a license
CLOCK_FILENAME = "license.clock"

GRACE_DAYS = 14
WARNING_DAYS = 60
# Tolerance for small clock corrections (NTP, manual adjustment) before a rollback is assumed
CLOCK_TOLERANCE_SECONDS = 24 * 3600

# Docker images of licensed MICSI products. Modules using these images additionally require the
# product to be listed in the license. Other modules only require a valid license.
PRODUCT_IMAGES = {
    "pet": "micsi/pet-module",
    "rmt": "micsi/mercure-module",
}

STATE_VALID = "valid"
STATE_WARNING = "warning"  # valid, but expires within WARNING_DAYS
STATE_GRACE = "grace"  # expired, but within the grace period; processing continues
STATE_EXPIRED = "expired"  # expired and past the grace period; processing is blocked
STATE_MISSING = "missing"
STATE_INVALID = "invalid"


@dataclass
class LicenseStatus:
    state: str
    message: str
    licensee: str = ""
    license_id: str = ""
    issued: str = ""
    expires: str = ""
    products: List[str] = field(default_factory=list)
    days_left: Optional[int] = None  # days until expiry; negative once expired

    @property
    def processing_allowed(self) -> bool:
        return self.state in (STATE_VALID, STATE_WARNING, STATE_GRACE)

    @property
    def needs_attention(self) -> bool:
        return self.state != STATE_VALID


def config_folder() -> Path:
    return Path(os.getenv("MERCURE_CONFIG_FOLDER") or "/opt/mercure/config")


def license_path() -> Path:
    return config_folder() / LICENSE_FILENAME


def verify_bytes(data: bytes, public_key_hex: Optional[str] = None) -> dict:
    """Checks the signature of a license file and returns its payload. Raises ValueError if the file
    is malformed or the signature does not match. Expiry is not checked here."""
    try:
        wrapper = json.loads(data)
        payload_str = wrapper["payload"]
        signature = bytes.fromhex(wrapper["sig"])
    except Exception:
        raise ValueError("License file is malformed")
    if not isinstance(payload_str, str):
        raise ValueError("License file is malformed")

    try:
        verify_key = nacl.signing.VerifyKey(bytes.fromhex(public_key_hex or PUBLIC_KEY_HEX))
        verify_key.verify(payload_str.encode("utf-8"), signature)
    except (nacl.exceptions.BadSignatureError, ValueError, TypeError):
        raise ValueError("License signature is not valid")

    try:
        payload = json.loads(payload_str)
    except ValueError:
        raise ValueError("License file is malformed")
    if not isinstance(payload, dict) or not isinstance(payload.get("expires_unix"), int) \
            or not isinstance(payload.get("issued_unix"), int):
        raise ValueError("License file is malformed")
    return payload


def _effective_now(folder: Path, now: float) -> float:
    """Returns the current time, or the latest time previously seen if the clock has gone backwards,
    and records the current time for future checks."""
    clock_file = folder / CLOCK_FILENAME
    last_seen = 0.0
    try:
        last_seen = float(clock_file.read_text().strip())
    except Exception:
        pass

    if now > last_seen + 3600:  # Only rewrite the file occasionally
        try:
            tmp_file = clock_file.with_suffix(".tmp")
            tmp_file.write_text(str(int(now)))
            tmp_file.replace(clock_file)
        except Exception:
            pass
    return max(now, last_seen)


def evaluate(payload: dict, now: float) -> LicenseStatus:
    """Determines the license state for a verified payload at the given time."""
    status = LicenseStatus(
        state=STATE_VALID,
        message="",
        licensee=str(payload.get("licensee", "")),
        license_id=str(payload.get("id", "")),
        issued=str(payload.get("issued", "")),
        expires=str(payload.get("expires", "")),
        products=[str(p).lower() for p in payload.get("features", [])],
    )

    if now < payload["issued_unix"] - CLOCK_TOLERANCE_SECONDS:
        status.state = STATE_INVALID
        status.message = "The system clock is set earlier than the license issue date. Check the server time."
        return status

    seconds_left = payload["expires_unix"] - now
    status.days_left = math.ceil(seconds_left / 86400)

    if seconds_left > WARNING_DAYS * 86400:
        status.message = f"Licensed until {status.expires}."
    elif seconds_left > 0:
        status.state = STATE_WARNING
        status.message = f"The license expires on {status.expires} ({status.days_left} days left). Contact MICSI to renew."
    elif seconds_left > -GRACE_DAYS * 86400:
        status.state = STATE_GRACE
        grace_end = time.strftime("%Y-%m-%d", time.gmtime(payload["expires_unix"] + GRACE_DAYS * 86400))
        status.message = (f"The license expired on {status.expires}. Processing will stop after {grace_end}. "
                          "Contact MICSI to renew.")
    else:
        status.state = STATE_EXPIRED
        status.message = f"The license expired on {status.expires}. Processing is disabled. Contact MICSI to renew."
    return status


def get_status(now: Optional[float] = None) -> LicenseStatus:
    """Reads and checks the installed license."""
    folder = config_folder()
    path = folder / LICENSE_FILENAME
    if not path.exists():
        return LicenseStatus(state=STATE_MISSING, message=f"No license installed ({path}). Processing is disabled.")
    try:
        payload = verify_bytes(path.read_bytes())
    except ValueError as e:
        return LicenseStatus(state=STATE_INVALID, message=f"{e}. Processing is disabled.")
    except OSError as e:
        return LicenseStatus(state=STATE_INVALID, message=f"Unable to read license file: {e}")

    return evaluate(payload, _effective_now(folder, time.time() if now is None else now))


def product_for_image(docker_tag: str) -> Optional[str]:
    """Returns the licensed product that a module image belongs to, or None for other images."""
    repository = docker_tag.split("@")[0]
    if ":" in repository.rsplit("/", 1)[-1]:
        repository = repository.rsplit(":", 1)[0]
    for product, image in PRODUCT_IMAGES.items():
        if repository == image or repository.endswith("/" + image):
            return product
    return None


def check_module(status: LicenseStatus, docker_tag: str) -> Optional[str]:
    """Returns None if the module may run under the given license, otherwise the reason it may not."""
    if not status.processing_allowed:
        return status.message
    product = product_for_image(docker_tag or "")
    if product and product not in status.products:
        return f"The license does not include the {product.upper()} product (module image {docker_tag})."
    return None


def install(data: bytes) -> LicenseStatus:
    """Verifies a license file and installs it. Raises ValueError if it is not a valid license.
    An expired license is still installed, so that the status page can show why."""
    verify_bytes(data)
    path = license_path()
    tmp_file = path.with_suffix(".tmp")
    tmp_file.write_bytes(data)
    tmp_file.replace(path)
    return get_status()


def main(args: List[str]) -> int:
    """Prints the status of a license file: python -m common.license [file]"""
    if args:
        try:
            payload = verify_bytes(Path(args[0]).read_bytes())
        except (ValueError, OSError) as e:
            print(f"INVALID: {e}")
            return 1
        status = evaluate(payload, time.time())
    else:
        status = get_status()
    print(f"{status.state.upper()}: {status.message}")
    if status.licensee:
        print(f"  Licensee: {status.licensee}")
        print(f"  ID:       {status.license_id}")
        print(f"  Products: {', '.join(status.products) or '-'}")
        print(f"  Expires:  {status.expires}")
    return 0 if status.processing_allowed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
