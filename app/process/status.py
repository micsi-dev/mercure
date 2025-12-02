"""
status.py
=========
Helper functions for mercure's processor module
"""

# Standard python includes
from pathlib import Path

# App-specific includes
from common.constants import mercure_names


def is_ready_for_processing(folder) -> bool:
    """Checks if a case in the processing folder is ready for the processor."""
    try:
        path = Path(folder)
        # Check for DICOM files at any level (root or subdirectories for patient-level tasks)
        has_dicom_files = len(list(path.rglob("*.dcm"))) > 0
        folder_status = (
            not (path / mercure_names.LOCK).exists()
            and not (path / mercure_names.PROCESSING).exists()
            and has_dicom_files
        )
        return folder_status
    except Exception:
        # Capture exceptions that may be triggered if the folder has been removed
        # by another process in the meantime
        return False
