"""
api.py
========
API backend functions for AJAX querying from the web frontend.
"""

# App-specific includes
import common.monitor as monitor
import common.config as config
# Standard python includes
import daiquiri
import hashlib
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from decoRouter import Router as decoRouter
# Starlette-related includes
from starlette.applications import Starlette
from starlette.authentication import requires
from starlette.responses import JSONResponse, Response

router = decoRouter()

logger = daiquiri.getLogger("api")


# Simple thread-safe LRU cache with time-based expiration
class LRUCache:
    def __init__(self, max_size: int = 50, max_age_seconds: int = 86400) -> None:
        self.cache: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self.max_size = max_size
        self.max_age_seconds = max_age_seconds  # Default: 24 hours
        self.lock = threading.Lock()

    def _is_expired(self, entry: Dict[str, Any]) -> bool:
        """Check if a cache entry has expired."""
        import time
        return bool((time.time() - entry['timestamp']) > self.max_age_seconds)

    def get(self, key: str) -> Any:
        import time
        with self.lock:
            if key in self.cache:
                entry = self.cache[key]
                # Check expiration
                if self._is_expired(entry):
                    del self.cache[key]
                    return None
                self.cache.move_to_end(key)
                return entry['value']
            return None

    def set(self, key: str, value: Any) -> None:
        import time
        with self.lock:
            entry = {'value': value, 'timestamp': time.time()}
            if key in self.cache:
                self.cache[key] = entry
                self.cache.move_to_end(key)
            else:
                # Clean up expired entries before adding new ones
                self._cleanup_expired()
                if len(self.cache) >= self.max_size:
                    self.cache.popitem(last=False)
                self.cache[key] = entry

    def _cleanup_expired(self) -> None:
        """Remove expired entries (called within lock)."""
        expired_keys = [k for k, v in self.cache.items() if self._is_expired(v)]
        for k in expired_keys:
            del self.cache[k]

    def clear(self) -> None:
        with self.lock:
            self.cache.clear()


# Global caches for MPR data
mpr_volume_cache = LRUCache(max_size=10, max_age_seconds=86400)   # 24 hour expiration
mpr_image_cache = LRUCache(max_size=200, max_age_seconds=86400)   # 24 hour expiration


async def get_task_output_folder(task_id: str) -> Optional[Path]:
    """Find task output folder in success or error directories.

    For series/study tasks, output may be stored under parent task folder.
    This function queries the bookkeeper to find the correct output folder.
    """
    try:
        config.read_config()
    except Exception:
        return None

    # First try direct task_id lookup (fast path)
    for folder in [config.mercure.success_folder, config.mercure.error_folder]:
        task_folder = Path(folder) / task_id
        if task_folder.exists():
            out_folder = task_folder / "out"
            if out_folder.exists():
                return out_folder
            return task_folder

    # If not found, query bookkeeper to find parent task folder
    try:
        folder_info = await monitor.find_output_folder(task_id)

        if folder_info and folder_info.get("exists"):
            actual_task_id = folder_info.get("task_id")
            location = folder_info.get("location")

            if location == "success":
                task_folder = Path(config.mercure.success_folder) / actual_task_id
            else:
                task_folder = Path(config.mercure.error_folder) / actual_task_id

            if task_folder.exists():
                out_folder = task_folder / "out"
                if out_folder.exists():
                    return out_folder
                return task_folder
    except Exception as e:
        logger.warning(f"Error finding output folder for task {task_id}: {e}")

    return None


def is_encapsulated_pdf(dcm_path: Path) -> bool:
    """Check if DICOM file is an encapsulated PDF."""
    try:
        import pydicom
        ds = pydicom.dcmread(dcm_path, stop_before_pixels=True)
        # SOP Class UID for Encapsulated PDF: 1.2.840.10008.5.1.4.1.1.104.1
        return str(ds.SOPClassUID) == "1.2.840.10008.5.1.4.1.1.104.1"
    except Exception:
        return False


def extract_pdf_from_dicom(dcm_path: Path) -> bytes:
    """Extract PDF bytes from encapsulated PDF DICOM."""
    try:
        import pydicom
    except ImportError:
        raise ImportError("pydicom is required to extract PDF from DICOM")
    ds = pydicom.dcmread(dcm_path)
    return bytes(ds.EncapsulatedDocument)


def get_acquisition_plane(image_orientation: Optional[List[float]]) -> str:
    """
    Determine the acquisition plane from ImageOrientationPatient.
    Returns 'axial', 'coronal', 'sagittal', or 'oblique'.
    """
    import numpy as np

    if image_orientation is None or len(image_orientation) != 6:
        return 'axial'  # Default assumption

    row_cosines = np.array(image_orientation[:3])
    col_cosines = np.array(image_orientation[3:])

    # Calculate the normal to the image plane (cross product)
    normal = np.cross(row_cosines, col_cosines)

    # Find which axis the normal is most aligned with
    abs_normal = np.abs(normal)
    max_idx = np.argmax(abs_normal)

    # If normal is along Z axis -> axial
    # If normal is along Y axis -> coronal
    # If normal is along X axis -> sagittal
    if max_idx == 2:  # Z axis
        return 'axial'
    elif max_idx == 1:  # Y axis
        return 'coronal'
    else:  # X axis
        return 'sagittal'


###################################################################################
# API endpoints
###################################################################################

@router.get("/")
async def test(request):
    return JSONResponse({"ok": True})


@router.get("/get-task-events")
@requires(["authenticated"])
async def get_series_events(request):
    logger.debug(request.query_params)
    task_id = request.query_params.get("task_id", "")
    try:
        return JSONResponse(await monitor.get_task_events(task_id))
    except monitor.MonitorHTTPError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)


@router.get("/get-series")
@requires(["authenticated"])
async def get_series(request):
    series_uid = request.query_params.get("series_uid", "")
    try:
        return JSONResponse(await monitor.get_series(series_uid))
    except monitor.MonitorHTTPError as e:
        return JSONResponse({"error": e.message}, status_code=e.status_code)


@router.get("/get-tasks")
@requires(["authenticated"])
async def get_tasks(request):
    try:
        return JSONResponse(await monitor.get_tasks())
    except monitor.MonitorHTTPError as e:
        return JSONResponse({"error": e.status_code}, status_code=e.status_code)


@router.get("/get-tests")
@requires(["authenticated"])
async def get_tests(request):
    try:
        return JSONResponse(await monitor.get_tests())
    except monitor.MonitorHTTPError as e:
        return JSONResponse({"error": e.status_code}, status_code=e.status_code)


@router.get("/find-tasks")
@requires(["authenticated"])
async def find_tasks(request):
    try:
        return JSONResponse(await monitor.find_tasks(request))
    except monitor.MonitorHTTPError as e:
        return JSONResponse({"error": e.status_code}, status_code=e.status_code)


@router.get("/task-process-logs")
@requires(["authenticated"])
async def task_process_logs(request):
    task_id = request.query_params.get("task_id", "")
    try:
        return JSONResponse(await monitor.task_process_logs(task_id))
    except monitor.MonitorHTTPError as e:
        return JSONResponse({"error": e.status_code}, status_code=e.status_code)


@router.get("/task-live-logs/{task_id}")
@requires(["authenticated"])
async def task_live_logs(request):
    """Serve live logs from a currently processing task."""
    task_id = request.path_params.get("task_id", "")
    if not task_id:
        return JSONResponse({"error": "No task_id provided"}, status_code=400)

    try:
        config.read_config()
    except Exception:
        return JSONResponse({"error": "Could not read config"}, status_code=500)

    # Check if task is currently being processed
    processing_folder = Path(config.mercure.processing_folder) / task_id
    live_log_file = processing_folder / "process.log"

    if live_log_file.exists():
        try:
            logs = live_log_file.read_text(encoding="utf-8")
            return JSONResponse({
                "status": "processing",
                "logs": logs
            })
        except Exception as e:
            return JSONResponse({"error": f"Could not read log file: {e}"}, status_code=500)
    elif processing_folder.exists():
        # Task folder exists but no log file yet - processing may be starting
        return JSONResponse({
            "status": "starting",
            "logs": ""
        })
    else:
        # Task not currently processing - check bookkeeper for completed logs
        return JSONResponse({
            "status": "not_processing",
            "logs": None
        })


@router.get("/task-process-results")
@requires(["authenticated"])
async def task_process_results(request):
    task_id = request.query_params.get("task_id", "")
    try:
        return JSONResponse(await monitor.task_process_results(task_id))
    except monitor.MonitorHTTPError as e:
        return JSONResponse({"error": e.status_code}, status_code=e.status_code)


@router.get("/get-task-info")
@requires(["authenticated"])
async def get_task_info(request):
    task_id = request.query_params.get("task_id", "")

    try:
        return JSONResponse(await monitor.get_task_info(task_id))
    except monitor.MonitorHTTPError as e:
        return JSONResponse({"error": e.status_code}, status_code=e.status_code)


@router.get("/get-child-tasks")
@requires(["authenticated"])
async def get_child_tasks(request):
    parent_id = request.query_params.get("parent_id", "")
    scope = request.query_params.get("scope", "")
    try:
        return JSONResponse(await monitor.get_child_tasks(parent_id, scope))
    except monitor.MonitorHTTPError as e:
        return JSONResponse({"error": e.status_code}, status_code=e.status_code)


@router.get("/task-files-exist/{task_id}")
@requires(["authenticated"])
async def task_files_exist(request):
    """Check if task output files exist in success or error folders.

    For series/study tasks, output may be stored under parent task folder.
    This endpoint queries the bookkeeper to find the correct output folder.
    """
    task_id = request.path_params["task_id"]

    try:
        config.read_config()
    except Exception:
        return JSONResponse({"error": "Could not read config"}, status_code=500)

    result = {
        "exists": False,
        "location": None,
        "has_dicom": False,
        "actual_task_id": task_id  # The task ID where files are actually stored
    }

    # Query bookkeeper to find the output folder (handles parent lookup)
    try:
        folder_info = await monitor.find_output_folder(task_id)
        logger.debug(f"find_output_folder response for {task_id}: {folder_info}")

        if folder_info and folder_info.get("exists"):
            actual_task_id = folder_info.get("task_id", task_id)
            location = folder_info.get("location")

            result["exists"] = True
            result["location"] = location
            result["actual_task_id"] = actual_task_id

            # Check for DICOM files
            if location == "success":
                folder_path = Path(config.mercure.success_folder) / actual_task_id
            else:
                folder_path = Path(config.mercure.error_folder) / actual_task_id

            out_folder = folder_path / "out"
            check_folder = out_folder if out_folder.exists() else folder_path
            result["has_dicom"] = any(check_folder.glob("*.dcm"))
            logger.debug(f"Task {task_id} -> actual {actual_task_id}, location={location}, has_dicom={result['has_dicom']}")
        else:
            logger.debug(f"No folder found for task {task_id}: folder_info={folder_info}")

    except Exception as e:
        logger.warning(f"Error finding output folder for task {task_id}: {e}")

    return JSONResponse(result)


@router.get("/task-dicom-files/{task_id}")
@requires(["authenticated"])
async def get_task_dicom_files(request):
    """Get list of DICOM files in a task's output folder, grouped by series."""
    task_id = request.path_params["task_id"]
    output_folder = await get_task_output_folder(task_id)

    if not output_folder:
        return JSONResponse({"error": "Task output folder not found"}, status_code=404)

    files = []
    has_pdf = False
    series_map = {}  # Map series_uid -> series info

    try:
        # Try to import pydicom for metadata extraction
        try:
            import pydicom
            has_pydicom = True
        except ImportError:
            has_pydicom = False
            logger.warning("pydicom not available, listing files without metadata")

        for idx, file_path in enumerate(sorted(output_folder.glob("*.dcm"))):
            if file_path.is_file():
                if has_pydicom:
                    try:
                        ds = pydicom.dcmread(file_path, stop_before_pixels=True)
                        sop_class = str(ds.SOPClassUID) if hasattr(ds, 'SOPClassUID') else ""
                        instance_num = int(ds.InstanceNumber) if hasattr(ds, 'InstanceNumber') else idx
                        is_pdf = sop_class == "1.2.840.10008.5.1.4.1.1.104.1"
                        if is_pdf:
                            has_pdf = True

                        # Extract series information
                        series_uid = str(ds.SeriesInstanceUID) if hasattr(ds, 'SeriesInstanceUID') else "unknown"
                        series_desc = str(ds.SeriesDescription) if hasattr(ds, 'SeriesDescription') else ""
                        series_num = int(ds.SeriesNumber) if hasattr(ds, 'SeriesNumber') else 0
                        modality = str(ds.Modality) if hasattr(ds, 'Modality') else "OT"

                        files.append({
                            "filename": file_path.name,
                            "sop_class": sop_class,
                            "instance_number": instance_num,
                            "is_pdf": is_pdf,
                            "series_uid": series_uid
                        })

                        # Build series info (include PDFs as separate series)
                        if series_uid not in series_map:
                            series_map[series_uid] = {
                                "series_uid": series_uid,
                                "series_description": series_desc if series_desc else ("PDF Report" if is_pdf else ""),
                                "series_number": series_num,
                                "modality": modality,
                                "instance_count": 0,
                                "thumbnail_file": file_path.name,  # Use first file as thumbnail
                                "is_pdf": is_pdf
                            }
                        if series_uid in series_map:
                            series_map[series_uid]["instance_count"] = int(str(series_map[series_uid]["instance_count"])) + 1

                    except Exception as e:
                        logger.warning(f"Could not read DICOM file {file_path}: {e}")
                        files.append({
                            "filename": file_path.name,
                            "sop_class": "",
                            "instance_number": idx,
                            "is_pdf": False,
                            "series_uid": "unknown"
                        })
                else:
                    # Without pydicom, just list the files
                    files.append({
                        "filename": file_path.name,
                        "sop_class": "",
                        "instance_number": idx,
                        "is_pdf": False,
                        "series_uid": "unknown"
                    })

        # Sort by instance number
        files.sort(key=lambda x: int(str(x["instance_number"])))

        # Build series list sorted by series number
        series_list = sorted(series_map.values(), key=lambda x: int(str(x["series_number"])))

        return JSONResponse({
            "files": files,
            "total_count": len(files),
            "has_pdf": has_pdf,
            "series": series_list,
            "series_count": len(series_list)
        })
    except Exception as e:
        logger.exception(f"Error listing DICOM files: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/task-dicom-file/{task_id}/{filename}")
@requires(["authenticated"])
async def get_task_dicom_file(request):
    """Serve DICOM file bytes."""
    task_id = request.path_params["task_id"]
    filename = request.path_params["filename"]

    output_folder = await get_task_output_folder(task_id)
    if not output_folder:
        return JSONResponse({"error": "Task output folder not found"}, status_code=404)

    file_path = output_folder / filename

    # Security: ensure the file is within the output folder
    try:
        file_path = file_path.resolve()
        output_folder = output_folder.resolve()
        if not str(file_path).startswith(str(output_folder)):
            return JSONResponse({"error": "Invalid file path"}, status_code=403)
    except Exception:
        return JSONResponse({"error": "Invalid file path"}, status_code=403)

    if not file_path.exists():
        return JSONResponse({"error": "File not found"}, status_code=404)

    try:
        with open(file_path, "rb") as f:
            content = f.read()
        return Response(content, media_type="application/dicom")
    except Exception as e:
        logger.exception(f"Error reading DICOM file: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/task-dicom-slices/{task_id}")
@requires(["authenticated"])
async def get_task_dicom_slices(request):
    """Get slice batch for lazy loading."""
    task_id = request.path_params["task_id"]
    start = int(request.query_params.get("start", 0))
    count = int(request.query_params.get("count", 10))

    output_folder = await get_task_output_folder(task_id)
    if not output_folder:
        return JSONResponse({"error": "Task output folder not found"}, status_code=404)

    try:
        # Try to import pydicom for metadata extraction
        try:
            import pydicom
            has_pydicom = True
        except ImportError:
            has_pydicom = False

        all_files = []
        for idx, file_path in enumerate(sorted(output_folder.glob("*.dcm"))):
            if file_path.is_file():
                if has_pydicom:
                    try:
                        ds = pydicom.dcmread(file_path, stop_before_pixels=True)
                        sop_class = str(ds.SOPClassUID) if hasattr(ds, 'SOPClassUID') else ""
                        # Skip encapsulated PDFs
                        if sop_class == "1.2.840.10008.5.1.4.1.1.104.1":
                            continue
                        instance_num = int(ds.InstanceNumber) if hasattr(ds, 'InstanceNumber') else idx
                        all_files.append({
                            "filename": file_path.name,
                            "instance_number": instance_num
                        })
                    except Exception:
                        all_files.append({
                            "filename": file_path.name,
                            "instance_number": idx
                        })
                else:
                    all_files.append({
                        "filename": file_path.name,
                        "instance_number": idx
                    })

        # Sort by instance number
        all_files.sort(key=lambda x: int(str(x["instance_number"])))

        # Get the slice range
        total = len(all_files)
        end = min(start + count, total)
        slices = []

        for i in range(start, end):
            slices.append({
                "index": i,
                "filename": all_files[i]["filename"],
                "task_id": task_id,
                "url": f"/api/task-dicom-file/{task_id}/{all_files[i]['filename']}"
            })

        return JSONResponse({
            "slices": slices,
            "total": total,
            "has_more": end < total
        })
    except Exception as e:
        logger.exception(f"Error getting DICOM slices: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/task-pdf/{task_id}/{filename}")
@requires(["authenticated"])
async def get_task_pdf(request):
    """Extract and serve PDF from encapsulated PDF DICOM."""
    task_id = request.path_params["task_id"]
    filename = request.path_params["filename"]

    output_folder = await get_task_output_folder(task_id)
    if not output_folder:
        return JSONResponse({"error": "Task output folder not found"}, status_code=404)

    file_path = output_folder / filename

    # Security: ensure the file is within the output folder
    try:
        file_path = file_path.resolve()
        output_folder = output_folder.resolve()
        if not str(file_path).startswith(str(output_folder)):
            return JSONResponse({"error": "Invalid file path"}, status_code=403)
    except Exception:
        return JSONResponse({"error": "Invalid file path"}, status_code=403)

    if not file_path.exists():
        return JSONResponse({"error": "File not found"}, status_code=404)

    try:
        pdf_bytes = extract_pdf_from_dicom(file_path)
        return Response(pdf_bytes, media_type="application/pdf")
    except Exception as e:
        logger.exception(f"Error extracting PDF: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/task-dicom-volume-info/{task_id}")
@requires(["authenticated"])
async def get_task_dicom_volume_info(request):
    """Get volume metadata for 3D/4D DICOM rendering."""
    task_id = request.path_params["task_id"]
    series_uid = request.query_params.get("series_uid")  # Optional series filter
    output_folder = await get_task_output_folder(task_id)

    if not output_folder:
        return JSONResponse({"error": "Task output folder not found"}, status_code=404)

    try:
        try:
            import pydicom
            import numpy as np
            has_pydicom = True
        except ImportError:
            return JSONResponse({"error": "pydicom not available"}, status_code=501)

        files_info = []
        first_ds = None
        modality = "OT"
        rows = 0
        columns = 0
        pixel_spacing = [1.0, 1.0]
        slice_thickness = 1.0
        image_orientation: List[float] = [1, 0, 0, 0, 1, 0]
        window_center = None
        window_width = None
        bits_allocated = 16
        photometric_interpretation = "MONOCHROME2"
        rescale_slope = 1.0
        rescale_intercept = 0.0

        for file_path in sorted(output_folder.glob("*.dcm")):
            if file_path.is_file():
                try:
                    ds = pydicom.dcmread(file_path, stop_before_pixels=True)
                    sop_class = str(ds.SOPClassUID) if hasattr(ds, 'SOPClassUID') else ""
                    # Skip encapsulated PDFs
                    if sop_class == "1.2.840.10008.5.1.4.1.1.104.1":
                        continue

                    # Filter by series UID if specified
                    if series_uid:
                        file_series_uid = str(ds.SeriesInstanceUID) if hasattr(ds, 'SeriesInstanceUID') else ""
                        if file_series_uid != series_uid:
                            continue

                    instance_num = int(ds.InstanceNumber) if hasattr(ds, 'InstanceNumber') else 0

                    # Get slice position for ordering
                    slice_location = None
                    image_position = None
                    if hasattr(ds, 'SliceLocation'):
                        slice_location = float(ds.SliceLocation)
                    if hasattr(ds, 'ImagePositionPatient'):
                        image_position = [float(x) for x in ds.ImagePositionPatient]

                    # Get temporal position for 4D
                    temporal_position = None
                    if hasattr(ds, 'TemporalPositionIdentifier'):
                        temporal_position = int(ds.TemporalPositionIdentifier)
                    elif hasattr(ds, 'TemporalPositionIndex'):
                        temporal_position = int(ds.TemporalPositionIndex)

                    files_info.append({
                        "filename": file_path.name,
                        "instance_number": instance_num,
                        "slice_location": slice_location,
                        "image_position": image_position,
                        "temporal_position": temporal_position
                    })

                    # Get metadata from first valid file
                    if first_ds is None:
                        first_ds = ds
                        modality = str(ds.Modality) if hasattr(ds, 'Modality') else "OT"
                        rows = int(ds.Rows) if hasattr(ds, 'Rows') else 0
                        columns = int(ds.Columns) if hasattr(ds, 'Columns') else 0

                        if hasattr(ds, 'PixelSpacing'):
                            pixel_spacing = [float(x) for x in ds.PixelSpacing]
                        if hasattr(ds, 'SliceThickness'):
                            slice_thickness = float(ds.SliceThickness)
                        if hasattr(ds, 'ImageOrientationPatient'):
                            image_orientation = [float(x) for x in ds.ImageOrientationPatient]  # type: ignore[assignment]
                        if hasattr(ds, 'WindowCenter'):
                            wc = ds.WindowCenter
                            window_center = float(wc[0]) if isinstance(wc, (list, pydicom.multival.MultiValue)) else float(wc)
                        if hasattr(ds, 'WindowWidth'):
                            ww = ds.WindowWidth
                            window_width = float(ww[0]) if isinstance(ww, (list, pydicom.multival.MultiValue)) else float(ww)
                        if hasattr(ds, 'BitsAllocated'):
                            bits_allocated = int(ds.BitsAllocated)
                        if hasattr(ds, 'PhotometricInterpretation'):
                            photometric_interpretation = str(ds.PhotometricInterpretation)
                        if hasattr(ds, 'RescaleSlope'):
                            rescale_slope = float(ds.RescaleSlope)
                        if hasattr(ds, 'RescaleIntercept'):
                            rescale_intercept = float(ds.RescaleIntercept)

                except Exception as e:
                    logger.warning(f"Could not read DICOM file {file_path}: {e}")
                    continue

        if not files_info:
            return JSONResponse({"error": "No valid DICOM files found"}, status_code=404)

        # Sort files by slice location or instance number
        def sort_key(f):
            if f["slice_location"] is not None:
                return (f.get("temporal_position") or 0, f["slice_location"])
            if f["image_position"] is not None:
                # Use dot product with image orientation normal for slice ordering
                return (f.get("temporal_position") or 0, sum(f["image_position"]))
            return (f.get("temporal_position") or 0, f["instance_number"])

        files_info.sort(key=sort_key)

        # Determine if 4D
        temporal_positions = set(f["temporal_position"] for f in files_info if f["temporal_position"] is not None)
        is_4d = len(temporal_positions) > 1
        num_timepoints = len(temporal_positions) if is_4d else 1

        # Calculate number of slices per timepoint
        num_slices = len(files_info) // num_timepoints if num_timepoints > 0 else len(files_info)

        # Determine default window settings based on modality
        if window_center is None or window_width is None:
            if modality == "CT":
                window_center = 40
                window_width = 400
            elif modality in ["MR", "MRI"]:
                window_center = 250
                window_width = 500
            elif modality in ["PT", "PET"]:
                window_center = 5
                window_width = 10
            else:
                window_center = 127
                window_width = 256

        # Calculate percentiles from a sample of slices for auto-windowing
        percentile_min = None
        percentile_max = None
        try:
            # Sample up to 5 slices evenly distributed
            sample_indices = []
            if len(files_info) <= 5:
                sample_indices = list(range(len(files_info)))
            else:
                step = len(files_info) // 5
                sample_indices = [i * step for i in range(5)]

            all_pixels = []
            for idx in sample_indices:
                sample_path = output_folder / str(files_info[idx]["filename"])
                if sample_path.exists():
                    sample_ds = pydicom.dcmread(sample_path)
                    if hasattr(sample_ds, 'pixel_array'):
                        arr: Any = sample_ds.pixel_array.astype(float)
                        if hasattr(sample_ds, 'RescaleSlope'):
                            arr = arr * float(sample_ds.RescaleSlope)
                        if hasattr(sample_ds, 'RescaleIntercept'):
                            arr = arr + float(sample_ds.RescaleIntercept)
                        # Subsample to reduce memory
                        all_pixels.extend(arr.flatten()[::10].tolist())

            if all_pixels:
                percentile_min = float(np.percentile(all_pixels, 0.1))
                percentile_max = float(np.percentile(all_pixels, 98))
        except Exception as e:
            logger.warning(f"Could not calculate percentiles: {e}")

        return JSONResponse({
            "task_id": task_id,
            "modality": modality,
            "dimensions": {
                "rows": rows,
                "columns": columns,
                "slices": num_slices,
                "timepoints": num_timepoints
            },
            "spacing": {
                "pixel_spacing": pixel_spacing,
                "slice_thickness": slice_thickness
            },
            "orientation": {
                "image_orientation_patient": image_orientation,
                "acquisition_plane": get_acquisition_plane(image_orientation)
            },
            "windowing": {
                "default_center": window_center,
                "default_width": window_width,
                "percentile_min": percentile_min,
                "percentile_max": percentile_max
            },
            "pixel_info": {
                "bits_allocated": bits_allocated,
                "photometric_interpretation": photometric_interpretation,
                "rescale_slope": rescale_slope,
                "rescale_intercept": rescale_intercept
            },
            "is_4d": is_4d,
            "files": [{"filename": f["filename"], "temporal_position": f["temporal_position"]} for f in files_info],
            "total_files": len(files_info)
        })

    except Exception as e:
        logger.exception(f"Error getting volume info: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


def apply_colormap(arr: Any, cmap_name: Optional[str]) -> Any:
    """Apply a colormap to a grayscale array (0-255) and return RGB array."""
    import numpy as np

    if cmap_name == 'gray' or cmap_name is None:
        # Return as grayscale (no colormap)
        return arr

    # Create lookup tables for different colormaps
    lut: Any = np.zeros((256, 3), dtype=np.uint8)

    if cmap_name == 'inverted':
        # Inverted grayscale
        for i in range(256):
            lut[i] = [255 - i, 255 - i, 255 - i]
    elif cmap_name == 'hot':
        # Hot colormap: black -> red -> yellow -> white
        for i in range(256):
            if i < 85:
                lut[i] = [int(i * 3), 0, 0]
            elif i < 170:
                lut[i] = [255, int((i - 85) * 3), 0]
            else:
                lut[i] = [255, 255, int((i - 170) * 3)]
    elif cmap_name == 'cool':
        # Cool colormap: cyan -> magenta
        for i in range(256):
            lut[i] = [i, 255 - i, 255]
    elif cmap_name == 'jet':
        # Jet colormap: blue -> cyan -> green -> yellow -> red
        for i in range(256):
            if i < 32:
                lut[i] = [0, 0, 128 + i * 4]
            elif i < 96:
                lut[i] = [0, (i - 32) * 4, 255]
            elif i < 160:
                lut[i] = [(i - 96) * 4, 255, 255 - (i - 96) * 4]
            elif i < 224:
                lut[i] = [255, 255 - (i - 160) * 4, 0]
            else:
                lut[i] = [255 - (i - 224) * 4, 0, 0]
    else:
        # Default to grayscale
        return arr

    # Apply LUT
    if len(arr.shape) == 2:
        rgb = lut[arr]
        return rgb
    return arr


@router.get("/task-dicom-image/{task_id}/{filename}")
@requires(["authenticated"])
async def get_task_dicom_image(request):
    """Render full-resolution DICOM image with windowing as PNG."""
    task_id = request.path_params["task_id"]
    filename = request.path_params["filename"]

    # Get windowing parameters from query string
    window_center = request.query_params.get("wc")
    window_width = request.query_params.get("ww")
    colormap = request.query_params.get("cmap", "gray")

    output_folder = await get_task_output_folder(task_id)
    if not output_folder:
        return JSONResponse({"error": "Task output folder not found"}, status_code=404)

    file_path = output_folder / filename

    # Security: ensure the file is within the output folder
    try:
        file_path = file_path.resolve()
        output_folder = output_folder.resolve()
        if not str(file_path).startswith(str(output_folder)):
            return JSONResponse({"error": "Invalid file path"}, status_code=403)
    except Exception:
        return JSONResponse({"error": "Invalid file path"}, status_code=403)

    if not file_path.exists():
        return JSONResponse({"error": "File not found"}, status_code=404)

    try:
        try:
            import pydicom
            from PIL import Image
            import io
            import numpy as np
        except ImportError as e:
            return JSONResponse({"error": f"Missing dependency: {e}"}, status_code=501)

        ds = pydicom.dcmread(file_path)

        if not hasattr(ds, 'pixel_array'):
            return JSONResponse({"error": "No pixel data"}, status_code=404)

        arr: Any = ds.pixel_array.astype(float)

        # Apply rescale slope/intercept if present (for CT Hounsfield units)
        if hasattr(ds, 'RescaleSlope'):
            arr = arr * float(ds.RescaleSlope)
        if hasattr(ds, 'RescaleIntercept'):
            arr = arr + float(ds.RescaleIntercept)

        # Apply windowing
        if window_center is not None and window_width is not None:
            wc = float(window_center)
            ww = float(window_width)
        else:
            # Use DICOM default or auto-window
            if hasattr(ds, 'WindowCenter') and hasattr(ds, 'WindowWidth'):
                wc_val = ds.WindowCenter
                ww_val = ds.WindowWidth
                wc = float(wc_val[0]) if isinstance(wc_val, (list, pydicom.multival.MultiValue)) else float(wc_val)
                ww = float(ww_val[0]) if isinstance(ww_val, (list, pydicom.multival.MultiValue)) else float(ww_val)
            else:
                # Auto-window based on data range
                wc = (arr.max() + arr.min()) / 2
                ww = arr.max() - arr.min()

        # Apply window/level transformation
        lower = wc - ww / 2
        upper = wc + ww / 2
        arr = np.clip(arr, lower, upper)
        arr = ((arr - lower) / (upper - lower + 1e-10)) * 255
        arr = arr.astype(np.uint8)

        # Handle photometric interpretation
        if hasattr(ds, 'PhotometricInterpretation'):
            if ds.PhotometricInterpretation == 'MONOCHROME1':
                arr = 255 - arr  # Invert

        # Handle color images
        if len(arr.shape) == 3:
            if arr.shape[0] <= 4:  # channels first
                arr = np.transpose(arr, (1, 2, 0))

        # Apply colormap
        arr = apply_colormap(arr, colormap)

        img = Image.fromarray(arr)  # type: ignore[no-untyped-call]

        # Convert to PNG
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)

        return Response(buf.read(), media_type="image/png")

    except Exception as e:
        logger.exception(f"Error rendering DICOM image: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


def load_mpr_volume(task_id: str, series_uid: Optional[str], output_folder: Path) -> Optional[Dict[str, Any]]:
    """Load and cache the 3D volume for MPR reconstruction."""
    import pydicom
    import numpy as np

    volume_cache_key = f"{task_id}_{series_uid}"
    cached = mpr_volume_cache.get(volume_cache_key)
    if cached is not None:
        return cached  # type: ignore[no-any-return]

    # Load all DICOM files and build volume
    files_data = []
    image_orientation = None

    for file_path in sorted(output_folder.glob("*.dcm")):
        if file_path.is_file():
            try:
                ds = pydicom.dcmread(file_path, stop_before_pixels=True)
                sop_class = str(ds.SOPClassUID) if hasattr(ds, 'SOPClassUID') else ""
                if sop_class == "1.2.840.10008.5.1.4.1.1.104.1":
                    continue

                # Filter by series UID if specified
                if series_uid:
                    file_series_uid = str(ds.SeriesInstanceUID) if hasattr(ds, 'SeriesInstanceUID') else ""
                    if file_series_uid != series_uid:
                        continue

                # Get ImageOrientationPatient from first valid file
                if image_orientation is None and hasattr(ds, 'ImageOrientationPatient'):
                    image_orientation = [float(x) for x in ds.ImageOrientationPatient]

                # Get position for proper 3D sorting
                image_position = None
                if hasattr(ds, 'ImagePositionPatient'):
                    image_position = [float(x) for x in ds.ImagePositionPatient]

                slice_loc = float(ds.SliceLocation) if hasattr(ds, 'SliceLocation') else 0
                instance_num = int(ds.InstanceNumber) if hasattr(ds, 'InstanceNumber') else 0

                files_data.append({
                    "path": file_path,
                    "slice_location": slice_loc,
                    "instance_number": instance_num,
                    "image_position": image_position
                })
            except Exception:
                continue

    if not files_data:
        return None

    # Determine the acquisition plane
    acquisition_plane = get_acquisition_plane(image_orientation)

    # Sort by image position along the acquisition axis, or fall back to slice location
    if files_data[0]["image_position"] is not None:
        positions = np.array([f["image_position"] for f in files_data])
        variance = np.var(positions, axis=0)
        sort_axis = np.argmax(variance)
        files_data.sort(key=lambda x: x["image_position"][sort_axis] if x["image_position"] else 0)  # type: ignore[index]
    else:
        files_data.sort(key=lambda x: (x["slice_location"], x["instance_number"]))

    # Load first file to get dimensions and metadata
    first_ds = pydicom.dcmread(str(files_data[0]["path"]))
    rows = first_ds.Rows
    cols = first_ds.Columns
    num_slices = len(files_data)

    # Extract only the metadata we need (avoid storing full pydicom Dataset)
    dicom_metadata: Dict[str, Any] = {
        "window_center": None,
        "window_width": None,
        "photometric_interpretation": None
    }
    if hasattr(first_ds, 'WindowCenter'):
        wc_val = first_ds.WindowCenter
        dicom_metadata["window_center"] = float(wc_val[0]) if isinstance(wc_val, (list, pydicom.multival.MultiValue)) else float(wc_val)
    if hasattr(first_ds, 'WindowWidth'):
        ww_val = first_ds.WindowWidth
        dicom_metadata["window_width"] = float(ww_val[0]) if isinstance(ww_val, (list, pydicom.multival.MultiValue)) else float(ww_val)
    if hasattr(first_ds, 'PhotometricInterpretation'):
        dicom_metadata["photometric_interpretation"] = str(first_ds.PhotometricInterpretation)

    # Build 3D volume
    volume: Any = np.zeros((num_slices, rows, cols), dtype=np.float32)

    for i, fd in enumerate(files_data):
        ds = pydicom.dcmread(str(fd["path"]))
        slice_arr: Any = ds.pixel_array.astype(np.float32)
        if hasattr(ds, 'RescaleSlope'):
            slice_arr = slice_arr * float(ds.RescaleSlope)
        if hasattr(ds, 'RescaleIntercept'):
            slice_arr = slice_arr + float(ds.RescaleIntercept)
        volume[i] = slice_arr

    result = {
        "volume": volume,
        "files_data": files_data,
        "acquisition_plane": acquisition_plane,
        "image_orientation": image_orientation,
        "dicom_metadata": dicom_metadata,
        "rows": rows,
        "cols": cols,
        "num_slices": num_slices
    }

    mpr_volume_cache.set(volume_cache_key, result)
    return result


@router.get("/task-dicom-mpr/{task_id}")
@requires(["authenticated"])
async def get_task_dicom_mpr(request):
    """Generate MPR (coronal/sagittal) slice from volume."""
    task_id = request.path_params["task_id"]
    orientation = request.query_params.get("orientation", "axial")  # axial, coronal, sagittal
    slice_index = int(request.query_params.get("slice", 0))
    window_center = request.query_params.get("wc")
    window_width = request.query_params.get("ww")
    colormap = request.query_params.get("cmap", "gray")
    series_uid = request.query_params.get("series_uid")  # Optional series filter

    output_folder = await get_task_output_folder(task_id)
    if not output_folder:
        return JSONResponse({"error": "Task output folder not found"}, status_code=404)

    # Check image cache first
    cache_key = f"{task_id}_{series_uid}_{orientation}_{slice_index}_{window_center}_{window_width}_{colormap}"
    cached_image = mpr_image_cache.get(cache_key)
    if cached_image is not None:
        return Response(content=cached_image, media_type="image/png")

    try:
        try:
            import pydicom
            from PIL import Image
            import io
            import numpy as np
        except ImportError as e:
            return JSONResponse({"error": f"Missing dependency: {e}"}, status_code=501)

        # Load volume from cache or build it
        vol_data = load_mpr_volume(task_id, series_uid, output_folder)
        if vol_data is None:
            return JSONResponse({"error": "No valid DICOM files"}, status_code=404)

        volume = vol_data["volume"]
        files_data = vol_data["files_data"]
        acquisition_plane = vol_data["acquisition_plane"]
        dicom_metadata = vol_data["dicom_metadata"]
        rows = vol_data["rows"]
        cols = vol_data["cols"]
        num_slices = vol_data["num_slices"]

        # For the "native" view (same as acquisition), just return the specific slice
        if orientation == acquisition_plane:
            if slice_index < 0 or slice_index >= num_slices:
                slice_index = num_slices // 2
            arr = volume[slice_index]
        else:
            # Extract MPR slice from cached volume

            # MPR Reconstruction Guide:
            # =========================
            # DICOM Patient Coordinate System:
            #   +X = patient left, +Y = patient posterior, +Z = patient superior
            #
            # Volume axes after loading: [slice_idx, row_idx, col_idx]
            # The meaning depends on ImageOrientationPatient (IOP).
            #
            # Standard display conventions (radiological):
            #   Axial: looking from feet - left=patient right, top=anterior
            #   Coronal: looking from front - left=patient right, top=superior
            #   Sagittal: looking from right - left=anterior, top=superior
            #
            # For each acquisition plane, we need to:
            #   1. Extract the correct 2D slice from the volume
            #   2. Transform it to match the standard display convention

            if acquisition_plane == 'axial':
                # Axial acquisition: IOP typically [1,0,0,0,1,0]
                # Volume: [Z(slices), Y(rows:A->P), X(cols:R->L)]
                if orientation == 'coronal':
                    # Coronal from axial: slice along Y (rows)
                    # Result [Z, X] - need Z vertical (S at top), X horizontal (R at left)
                    max_idx = rows - 1
                    slice_index = min(max(0, slice_index), max_idx)
                    arr = volume[:, slice_index, :]  # [Z, X]
                    arr = np.flipud(arr)  # Flip so superior is at top
                elif orientation == 'sagittal':
                    # Sagittal from axial: slice along X (cols)
                    # Result [Z, Y] - need Z vertical (S at top), Y horizontal (A at left)
                    max_idx = cols - 1
                    slice_index = min(max(0, slice_index), max_idx)
                    arr = volume[:, :, slice_index]  # [Z, Y]
                    arr = np.flipud(arr)  # Flip so superior is at top
                else:
                    return JSONResponse({"error": "Invalid orientation"}, status_code=400)

            elif acquisition_plane == 'coronal':
                # Coronal acquisition: IOP typically [1,0,0,0,0,-1]
                # Row dir=[1,0,0]=+X, Col dir=[0,0,-1]=-Z
                # Volume: [Y(slices:A->P), Z(rows:S->I), X(cols:R->L)]
                if orientation == 'axial':
                    # Axial from coronal: slice along Z (rows)
                    # arr = volume[:, row_idx, :] gives [Y, X]
                    # Need: Y vertical (A at top), X horizontal (R at left)
                    max_idx = rows - 1
                    slice_index = min(max(0, slice_index), max_idx)
                    arr = volume[:, slice_index, :]  # [Y, X]
                    # Y=0 is anterior (should be top), X=0 is right (should be left)
                    # This is already correct for radiological axial view
                elif orientation == 'sagittal':
                    # Sagittal from coronal: slice along X (cols)
                    # arr = volume[:, :, col_idx] gives [Y, Z]
                    # Need: Z vertical (S at top), Y horizontal (A at left)
                    max_idx = cols - 1
                    slice_index = min(max(0, slice_index), max_idx)
                    arr = volume[:, :, slice_index]  # [Y, Z]
                    # Transpose to get [Z, Y], then check orientation
                    arr = arr.T  # Now [Z, Y] - Z vertical, Y horizontal
                    # Z: row 0 = superior (from coronal row 0), should be at top ✓
                    # Y: col 0 = anterior (from slice 0), should be at left ✓
                else:
                    return JSONResponse({"error": "Invalid orientation"}, status_code=400)

            elif acquisition_plane == 'sagittal':
                # Sagittal acquisition: IOP typically [0,1,0,0,0,-1]
                # Row dir=[0,1,0]=+Y, Col dir=[0,0,-1]=-Z
                # Volume: [X(slices:R->L), Z(rows:S->I), Y(cols:A->P)]
                if orientation == 'axial':
                    # Axial from sagittal: slice along Z (rows)
                    # arr = volume[:, row_idx, :] gives [X, Y]
                    # Need: Y vertical (A at top), X horizontal (R at left)
                    max_idx = rows - 1
                    slice_index = min(max(0, slice_index), max_idx)
                    arr = volume[:, slice_index, :]  # [X, Y]
                    arr = arr.T  # Now [Y, X] - correct axes
                    # May need flips depending on actual slice ordering
                elif orientation == 'coronal':
                    # Coronal from sagittal: slice along Y (cols)
                    # arr = volume[:, :, col_idx] gives [X, Z]
                    # Need: Z vertical (S at top), X horizontal (R at left)
                    max_idx = cols - 1
                    slice_index = min(max(0, slice_index), max_idx)
                    arr = volume[:, :, slice_index]  # [X, Z]
                    arr = arr.T  # Now [Z, X] - correct axes
                else:
                    return JSONResponse({"error": "Invalid orientation"}, status_code=400)
            else:
                # Oblique - fall back to axial-like behavior
                if orientation == 'coronal':
                    max_idx = rows - 1
                    slice_index = min(max(0, slice_index), max_idx)
                    arr = volume[:, slice_index, :]
                    arr = np.flipud(arr)
                elif orientation == 'sagittal':
                    max_idx = cols - 1
                    slice_index = min(max(0, slice_index), max_idx)
                    arr = volume[:, :, slice_index]
                    arr = np.flipud(arr)
                else:
                    return JSONResponse({"error": "Invalid orientation"}, status_code=400)

        # Note: Rescale slope/intercept is already applied during volume construction
        # and for native orientation slices, so no additional rescaling needed here.

        # Apply windowing
        if window_center is not None and window_width is not None:
            wc = float(window_center)
            ww = float(window_width)
        else:
            if dicom_metadata["window_center"] is not None and dicom_metadata["window_width"] is not None:
                wc = dicom_metadata["window_center"]
                ww = dicom_metadata["window_width"]
            else:
                wc = (arr.max() + arr.min()) / 2
                ww = arr.max() - arr.min()

        lower = wc - ww / 2
        upper = wc + ww / 2
        arr = np.clip(arr, lower, upper)
        arr = ((arr - lower) / (upper - lower + 1e-10)) * 255
        arr = arr.astype(np.uint8)

        if dicom_metadata["photometric_interpretation"] == 'MONOCHROME1':
            arr = 255 - arr

        # Apply colormap
        arr = apply_colormap(arr, colormap)

        img = Image.fromarray(arr)  # type: ignore[no-untyped-call]

        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        img_bytes = buf.read()

        # Cache the rendered image for faster subsequent access
        mpr_image_cache.set(cache_key, img_bytes)

        return Response(content=img_bytes, media_type="image/png")

    except Exception as e:
        logger.exception(f"Error generating MPR: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/task-dicom-thumbnail/{task_id}/{filename}")
@requires(["authenticated"])
async def get_task_dicom_thumbnail(request):
    """Generate and serve a thumbnail for a DICOM image."""
    task_id = request.path_params["task_id"]
    filename = request.path_params["filename"]

    output_folder = await get_task_output_folder(task_id)
    if not output_folder:
        return JSONResponse({"error": "Task output folder not found"}, status_code=404)

    file_path = output_folder / filename

    # Security: ensure the file is within the output folder
    try:
        file_path = file_path.resolve()
        output_folder = output_folder.resolve()
        if not str(file_path).startswith(str(output_folder)):
            return JSONResponse({"error": "Invalid file path"}, status_code=403)
    except Exception:
        return JSONResponse({"error": "Invalid file path"}, status_code=403)

    if not file_path.exists():
        return JSONResponse({"error": "File not found"}, status_code=404)

    try:
        try:
            import pydicom
            from PIL import Image
            import io
            import numpy as np
        except ImportError as e:
            return JSONResponse({"error": f"Missing dependency: {e}"}, status_code=501)

        ds = pydicom.dcmread(file_path)

        # Get pixel data and convert to image
        if hasattr(ds, 'pixel_array'):
            arr = ds.pixel_array
            # Normalize to 8-bit
            if arr.dtype != np.uint8:
                arr = arr.astype(float)
                arr = (arr - arr.min()) / (arr.max() - arr.min() + 1e-10) * 255
                arr = arr.astype(np.uint8)

            # Handle different shapes
            if len(arr.shape) == 3:
                if arr.shape[0] <= 4:  # RGB or RGBA
                    arr = np.transpose(arr, (1, 2, 0))

            img = Image.fromarray(arr)  # type: ignore[no-untyped-call]
            img.thumbnail((100, 100))

            # Convert to PNG bytes
            buf = io.BytesIO()
            img.save(buf, format='PNG')
            buf.seek(0)

            return Response(buf.read(), media_type="image/png")
        else:
            # Return a placeholder for non-image DICOM
            return JSONResponse({"error": "No pixel data"}, status_code=404)

    except Exception as e:
        logger.debug(f"Could not generate thumbnail for {filename}: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


api_app = Starlette(routes=router)
