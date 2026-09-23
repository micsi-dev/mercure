"""
licensed_processor.py
=====================
Starts the processor trusting the license key generated for an integration test run.

The services under test run as separate processes, so the unit tests' patched license check does
not reach them. The mercure_base fixture signs a license with a key made for the run and writes the
public half next to it as test_license.pub; this points the processor at that key, then starts it
the same way app/processor.py does.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.credentials import load_credentials  # noqa: E402

load_credentials()

import common.license  # noqa: E402

common.license.PUBLIC_KEY_HEX = (Path(os.environ["MERCURE_CONFIG_FOLDER"]) / "test_license.pub").read_text().strip()

from process.processor import main  # noqa: E402

if __name__ == "__main__":
    main()
