"""
micsi_query.py — MICSI overlay for the bookkeeper query API.
==========================================================

Additional query endpoints that upstream mercure does not provide. They live in
their own module, with their own router and mount point, so that syncing with
mercure-imaging/mercure never conflicts here: upstream has no such file and no
reason to create one.

Provides:

  GET /query-micsi/get_child_tasks
      Given a patient- or study-scope parent task, return its child tasks.
      Powers patient/study grouping and row expansion in the queue UI.

  GET /query-micsi/find_output_folder
      Resolve which task folder actually holds a task's output. Output for a
      series or study task may live under a patient/study parent's folder, so
      this walks up by MRN. Powers the DICOM viewer, which needs the real
      folder before it can list slices.

Both were previously carried as local modifications to bookkeeping/query.py.
That file is heavily developed upstream, and keeping ~500 lines of MICSI
additions inside it is what made the 2026-09 sync expensive. Kept here instead.

Client side lives in common/monitor.py (find_output_folder, get_child_tasks).
Mounted in bookkeeping/bookkeeper.py.
"""

import ast
import datetime
from pathlib import Path

import bookkeeping.database as db
import pydicom
import sqlalchemy
from bookkeeping.helper import CustomJSONResponse, json  # noqa: F401
from common import config
from decoRouter import Router as decoRouter
from pydicom.datadict import keyword_for_tag  # noqa: F401
from sqlalchemy import select  # noqa: F401
from starlette.applications import Starlette
from starlette.authentication import requires
from starlette.responses import JSONResponse

logger = config.get_logger()

router = decoRouter()


@router.get("/get_child_tasks")
@requires("authenticated")
async def get_child_tasks(request) -> JSONResponse:
    """Endpoint for getting child tasks of a parent task."""
    parent_id = request.query_params.get("parent_id", "")
    scope = request.query_params.get("scope", "")

    if not parent_id:
        return CustomJSONResponse([])

    if scope == "patient":
        # For patient tasks: get study and series tasks with matching MRN
        # that were created within 5 minutes before the patient task (same processing run)
        query_string = """
        WITH parent AS (
            SELECT id, time, data->'info'->>'mrn' AS mrn FROM tasks WHERE id = :parent_id
        )
        SELECT
            child_tasks.id AS task_id,
            child_tasks.series_uid,
            child_tasks.study_uid,
            dicom_series.tag_seriesdescription AS series_description,
            dicom_series.tag_modality AS modality,
            COALESCE(
                NULLIF(child_tasks.data->'info'->>'applied_rule', ''),
                (SELECT string_agg(key, ', ') FROM jsonb_object_keys(child_tasks.data->'info'->'triggered_rules') AS key)
            ) AS rule,
            COALESCE(child_tasks.data->'info'->>'uid_type', 'series') AS scope
        FROM
            tasks as child_tasks
            LEFT JOIN dicom_series ON dicom_series.series_uid = child_tasks.series_uid
            CROSS JOIN parent
        WHERE child_tasks.id != parent.id
          AND (child_tasks.data->'info'->>'uid_type' IN ('study', 'series') OR child_tasks.data->'info'->>'uid_type' IS NULL)
          AND child_tasks.data->'info'->>'uid_type' IS DISTINCT FROM 'patient'
          AND COALESCE(child_tasks.data->'info'->>'mrn', dicom_series.tag_patientid) = parent.mrn
          AND child_tasks.time BETWEEN parent.time - interval '5 minutes' AND parent.time
        ORDER BY
            CASE WHEN child_tasks.data->'info'->>'uid_type' = 'study' THEN 0 ELSE 1 END,
            child_tasks.time, child_tasks.id
        """
    else:
        # For study tasks: get series tasks created within 3 seconds
        query_string = """
        WITH parent AS (
            SELECT id, time FROM tasks WHERE id = :parent_id
        )
        SELECT
            child_tasks.id AS task_id,
            child_tasks.series_uid,
            child_tasks.study_uid,
            dicom_series.tag_seriesdescription AS series_description,
            dicom_series.tag_modality AS modality,
            COALESCE(
                NULLIF(child_tasks.data->'info'->>'applied_rule', ''),
                (SELECT string_agg(key, ', ') FROM jsonb_object_keys(child_tasks.data->'info'->'triggered_rules') AS key)
            ) AS rule,
            child_tasks.data->'info'->>'uid_type' AS scope
        FROM
            tasks as child_tasks
            LEFT JOIN dicom_series ON dicom_series.series_uid = child_tasks.series_uid
            CROSS JOIN parent
        WHERE child_tasks.id != parent.id
          AND child_tasks.time BETWEEN parent.time - interval '3 seconds' AND parent.time + interval '3 seconds'
        ORDER BY child_tasks.time, child_tasks.id
        """

    result_rows = await db.database.fetch_all(query_string, {"parent_id": parent_id})
    results = []
    for row in result_rows:
        row_dict = dict(row)
        rule = row_dict.get("rule", "")
        if rule:
            rule = rule.replace("{", "").replace("}", "")
        scope_val = (row_dict.get("scope") or "").lower()
        if scope_val == "study":
            scope_display = "STUDY"
        elif scope_val == "patient":
            scope_display = "PATIENT"
        else:
            scope_display = "SERIES"
        results.append({
            "task_id": row_dict["task_id"],
            "series_uid": row_dict.get("series_uid", ""),
            "study_uid": row_dict.get("study_uid", ""),
            "series_description": row_dict.get("series_description", ""),
            "modality": row_dict.get("modality", ""),
            "rule": rule,
            "scope": scope_display
        })

    return CustomJSONResponse(results)


@router.get("/find_output_folder")
@requires("authenticated")
async def find_output_folder(request) -> JSONResponse:
    """Find the output folder task ID for a given task.

    For series/study tasks, output files may be stored under a parent task's folder.
    This endpoint finds the correct folder by:
    1. Checking if the task has its own folder
    2. If not, finding a parent task (patient/study) with same MRN that has a folder

    Returns: {task_id: str, location: str|null, exists: bool}
    """
    task_id = request.query_params.get("task_id", "")

    if not task_id:
        return CustomJSONResponse({"task_id": task_id, "location": None, "exists": False})

    # Check if this task has its own folder
    for location, folder in [("success", config.mercure.success_folder),
                              ("error", config.mercure.error_folder)]:
        task_folder = Path(folder) / task_id
        if task_folder.exists():
            return CustomJSONResponse({
                "task_id": task_id,
                "location": location,
                "exists": True
            })

    # Task doesn't have its own folder - find MRN and look for parent folder
    # Query to get task's MRN (from task data or dicom_series)
    mrn_query = """
    SELECT
        t.id,
        t.data->'info'->>'uid_type' as uid_type,
        COALESCE(t.data->'info'->>'mrn', ds.tag_patientid) as mrn,
        t.time
    FROM tasks t
    LEFT JOIN dicom_series ds ON ds.series_uid = t.series_uid
    WHERE t.id = :task_id
    """

    task_result = await db.database.fetch_one(mrn_query, {"task_id": task_id})

    if not task_result:
        return CustomJSONResponse({"task_id": task_id, "location": None, "exists": False})

    task_dict = dict(task_result)
    mrn = task_dict.get("mrn")
    uid_type = task_dict.get("uid_type")
    task_time = task_dict.get("time")

    # If this is already a patient task, no parent to find
    if uid_type == "patient":
        return CustomJSONResponse({"task_id": task_id, "location": None, "exists": False})

    if not mrn:
        return CustomJSONResponse({"task_id": task_id, "location": None, "exists": False})

    # Find parent task (patient or study) with same MRN within time window
    parent_query = """
    SELECT
        t.id as task_id,
        t.data->'info'->>'uid_type' as uid_type
    FROM tasks t
    LEFT JOIN dicom_series ds ON ds.series_uid = t.series_uid
    WHERE t.parent_id IS NULL
      AND t.id != :task_id
      AND COALESCE(t.data->'info'->>'mrn', ds.tag_patientid) = :mrn
      AND t.data->'info'->>'uid_type' IN ('patient', 'study')
      AND t.time BETWEEN :time_start AND :time_end
    ORDER BY
        CASE WHEN t.data->'info'->>'uid_type' = 'patient' THEN 0 ELSE 1 END,
        t.time DESC
    LIMIT 10
    """

    # Calculate time window (task_time should already be a datetime from the query)
    if isinstance(task_time, str):
        task_time = datetime.datetime.fromisoformat(task_time.replace('Z', '+00:00'))
    if task_time is None:
        return CustomJSONResponse({"task_id": task_id, "location": None, "exists": False})
    time_start = task_time - datetime.timedelta(minutes=10)
    time_end = task_time + datetime.timedelta(minutes=5)

    parent_results = await db.database.fetch_all(parent_query, {
        "task_id": task_id,
        "mrn": mrn,
        "time_start": time_start,
        "time_end": time_end
    })

    # Check each potential parent to see if it has a folder
    for parent in parent_results:
        parent_id = parent["task_id"]
        for location, folder in [("success", config.mercure.success_folder),
                                  ("error", config.mercure.error_folder)]:
            parent_folder = Path(folder) / parent_id
            if parent_folder.exists():
                return CustomJSONResponse({
                    "task_id": parent_id,
                    "location": location,
                    "exists": True
                })

    return CustomJSONResponse({"task_id": task_id, "location": None, "exists": False})


# ---------------------------------------------------------------------------
# find_task: the archive/queue query, with group_by support.
#
# Upstream's equivalent takes a binary study_filter; ours takes group_by
# ("patient" | "study" | "series" | "" for grouped), which is what backs the
# grouping selector in the queue UI. Restored here rather than re-forking
# upstream's query.py.
#
# The ORDER BY direction is whitelisted below, carried over from upstream's
# hardening: it is interpolated into SQL and is attacker-controlled.
# ---------------------------------------------------------------------------

@router.get("/find_task")
@requires("authenticated")
async def find_task(request) -> JSONResponse:
    # Extract DataTables parameters
    draw = int(request.query_params.get("draw", "1"))
    start = int(request.query_params.get("start", "0"))
    length = int(request.query_params.get("length", "10"))
    search_term = request.query_params.get("search[value]", "")  # Global search value
    group_by = request.query_params.get("group_by", "")  # Filter by scope: patient, study, series, or empty for grouped

    # Extract ordering information
    order_column_index = request.query_params.get("order[0][column]", "6")  # Default to time column (index 6)
    order_direction_raw = request.query_params.get("order[0][dir]", "desc")  # Default to descending
    # Whitelist the direction before it reaches SQL. Carried over from upstream
    # (mercure-imaging), which hardened this after our fork diverged.
    order_direction = "ASC" if order_direction_raw.upper() == "ASC" else "DESC"

    # Map datatable column index to database column
    # Column layout: 0=Expand, 1=ACC, 2=MRN, 3=UID, 4=Scope, 5=Rule, 6=Time, 7=Files, 8=ID
    column_mapping = {
        "1": "tag_accessionnumber",  # ACC
        "2": "tag_patientid",        # MRN
        "4": "parent_tasks.data->'info'->>'uid_type'",  # Scope
        "6": "parent_tasks.time",    # Time
        "8": "parent_tasks.id"       # ID
    }

    order_column = column_mapping.get(order_column_index, column_mapping["6"])
    order_sql = f"{order_column} {order_direction}, parent_tasks.id {order_direction}"

    having_term = (f"""HAVING (
                    (tag_accessionnumber ilike :search_term || '%')
                    or (tag_patientid ilike :search_term || '%')
                    or (tag_patientname ilike '%' || :search_term || '%')
                    or bool_or(child_tasks.data->'info'->>'applied_rule'::text ilike '%' || :search_term || '%')
                    or bool_or(
                        array(
                            select jsonb_object_keys(
                                                    child_tasks.data->'info'->'triggered_rules'
                                                    )
                        )::text ilike '%' || :search_term || '%'
                        )
                    or (parent_tasks.data->'info'->>'applied_rule'::text ilike '%' || :search_term || '%')
                    or (
                        array(
                            select jsonb_object_keys(
                                                    parent_tasks.data->'info'->'triggered_rules'
                                                    )
                        )::text ilike '%' || :search_term || '%'
                        )
                   )
                   """) if search_term else ""

    # Build scope filter based on group_by parameter
    scope_filter_term = ""
    if group_by == "patient":
        scope_filter_term = "AND parent_tasks.data->'info'->>'uid_type' = 'patient'"
    elif group_by == "study":
        scope_filter_term = "AND parent_tasks.data->'info'->>'uid_type' = 'study'"
    elif group_by == "series":
        # Series view: show series that were either:
        # 1. Standalone series jobs with applied_rule, OR
        # 2. Series registrations that have an associated patient/study task (same MRN, within time window)
        scope_filter_term = """AND (parent_tasks.data->'info'->>'uid_type' = 'series' OR parent_tasks.data->'info'->>'uid_type' IS NULL)
            AND (
                -- Standalone series jobs with applied_rule
                parent_tasks.data->'info'->>'applied_rule' IS NOT NULL
                OR
                -- Series processed as part of a patient/study job (has matching parent by MRN)
                EXISTS (
                    SELECT 1 FROM tasks pt
                    LEFT JOIN dicom_series pds ON pds.series_uid = pt.series_uid
                    WHERE pt.data->'info'->>'uid_type' IN ('patient', 'study')
                      AND COALESCE(pt.data->'info'->>'mrn', pds.tag_patientid) = COALESCE(parent_tasks.data->'info'->>'mrn', tag_patientid)
                      AND parent_tasks.time BETWEEN pt.time - interval '5 minutes' AND pt.time
                )
            )"""

    # Count query (for recordsTotal and recordsFiltered)
    # When group_by is set, show only tasks of that scope; otherwise show hierarchical view
    if group_by:
        # Show all tasks of the specified scope
        count_query_string = f"""
        with base as (
           SELECT
            parent_tasks.id AS task_id,
            COALESCE(parent_tasks.data->'info'->>'acc', tag_accessionnumber) AS acc,
            COALESCE(parent_tasks.data->'info'->>'mrn', tag_patientid) AS mrn,
            COALESCE(parent_tasks.data->'info'->>'patient_name', tag_patientname) AS name
           FROM
            tasks as parent_tasks
            LEFT JOIN dicom_series ON dicom_series.series_uid = parent_tasks.series_uid
           WHERE parent_tasks.parent_id is null
             {scope_filter_term}
           GROUP BY 1,2,3,4
           {having_term}
        )
        SELECT
            COUNT(DISTINCT task_id) as total_count
        FROM base
        """
    else:
        # Hierarchical view: show patient tasks OR study tasks (if no patient exists)
        count_query_string = f"""
        with base as (
           SELECT
            parent_tasks.id AS task_id,
            COALESCE(parent_tasks.data->'info'->>'acc', tag_accessionnumber) AS acc,
            COALESCE(parent_tasks.data->'info'->>'mrn', tag_patientid) AS mrn,
            COALESCE(parent_tasks.data->'info'->>'patient_name', tag_patientname) AS name
           FROM
            tasks as parent_tasks
            LEFT JOIN dicom_series ON dicom_series.series_uid = parent_tasks.series_uid
           WHERE parent_tasks.parent_id is null
             AND (
                 -- Show patient tasks
                 parent_tasks.data->'info'->>'uid_type' = 'patient'
                 OR (
                     -- Show study tasks only if no patient task exists with same MRN
                     parent_tasks.data->'info'->>'uid_type' = 'study'
                     AND NOT EXISTS (
                         SELECT 1 FROM tasks pt
                         WHERE pt.data->'info'->>'uid_type' = 'patient'
                           AND pt.data->'info'->>'mrn' = COALESCE(parent_tasks.data->'info'->>'mrn', tag_patientid)
                     )
                 )
             )
           GROUP BY 1,2,3,4
           {having_term}
        )
        SELECT
            COUNT(DISTINCT task_id) as total_count
        FROM base
        """

    # Main data query with pagination
    # When group_by is set, show only tasks of that scope; otherwise show hierarchical view
    base_select = f"""
    SELECT
        COALESCE(parent_tasks.data->'info'->>'acc', tag_accessionnumber, '') AS acc,
        COALESCE(parent_tasks.data->'info'->>'mrn', tag_patientid, '') AS mrn,
        COALESCE(parent_tasks.data->'info'->>'patient_name', tag_patientname, '') AS name,
        parent_tasks.id AS task_id,
        -- For patient/study tasks, aggregate study/series UIDs from child tasks
        CASE
            WHEN parent_tasks.data->'info'->>'uid_type' = 'patient' THEN (
                SELECT STRING_AGG(DISTINCT t.study_uid, ', ')
                FROM tasks t
                LEFT JOIN dicom_series ds ON ds.series_uid = t.series_uid
                WHERE t.id != parent_tasks.id
                  AND COALESCE(t.data->'info'->>'mrn', ds.tag_patientid) = parent_tasks.data->'info'->>'mrn'
                  AND t.time BETWEEN parent_tasks.time - interval '5 minutes' AND parent_tasks.time
                  AND t.study_uid IS NOT NULL
            )
            ELSE parent_tasks.study_uid
        END AS study_uid,
        CASE
            WHEN parent_tasks.data->'info'->>'uid_type' = 'patient' THEN (
                SELECT STRING_AGG(DISTINCT t.series_uid, ', ')
                FROM tasks t
                LEFT JOIN dicom_series ds ON ds.series_uid = t.series_uid
                WHERE t.id != parent_tasks.id
                  AND COALESCE(t.data->'info'->>'mrn', ds.tag_patientid) = parent_tasks.data->'info'->>'mrn'
                  AND t.time BETWEEN parent_tasks.time - interval '5 minutes' AND parent_tasks.time
                  AND t.series_uid IS NOT NULL
            )
            WHEN parent_tasks.data->'info'->>'uid_type' = 'study' THEN (
                SELECT STRING_AGG(DISTINCT t.series_uid, ', ')
                FROM tasks t
                WHERE t.id != parent_tasks.id
                  AND t.time BETWEEN parent_tasks.time - interval '3 seconds' AND parent_tasks.time + interval '3 seconds'
                  AND t.series_uid IS NOT NULL
            )
            ELSE parent_tasks.series_uid
        END AS series_uid,
        parent_tasks.data->'info'->>'uid_type' AS scope,
        parent_tasks.time::timestamp AS time,
        COALESCE(dicom_series.tag_seriesdescription, '') AS series_description,
        COALESCE(dicom_series.tag_modality, '') AS modality,
        -- Child count: patient tasks can have study/series children, study tasks can have series children
        -- Series tasks (uid_type is NULL or 'series') NEVER have children - they are the atomic unit
        CASE
            WHEN parent_tasks.data->'info'->>'uid_type' = 'patient' THEN (
                SELECT COUNT(*) FROM tasks t
                LEFT JOIN dicom_series ds ON ds.series_uid = t.series_uid
                WHERE t.id != parent_tasks.id
                  AND (t.data->'info'->>'uid_type' IN ('study', 'series') OR t.data->'info'->>'uid_type' IS NULL)
                  AND t.data->'info'->>'uid_type' IS DISTINCT FROM 'patient'
                  AND COALESCE(t.data->'info'->>'mrn', ds.tag_patientid) = parent_tasks.data->'info'->>'mrn'
                  AND t.time BETWEEN parent_tasks.time - interval '5 minutes' AND parent_tasks.time
            )
            WHEN parent_tasks.data->'info'->>'uid_type' = 'study' THEN (
                SELECT COUNT(*) FROM tasks t
                WHERE t.id != parent_tasks.id
                  AND (t.data->'info'->>'uid_type' IS NULL OR t.data->'info'->>'uid_type' = 'series')
                  AND t.time BETWEEN parent_tasks.time - interval '3 seconds' AND parent_tasks.time + interval '3 seconds'
            )
            ELSE 0  -- Series tasks have no children
        END AS child_count,
        COALESCE(
            NULLIF(parent_tasks.data->'info'->>'applied_rule', ''),
            (SELECT string_agg(key, ', ') FROM jsonb_object_keys(parent_tasks.data->'info'->'triggered_rules') AS key)
        ) AS rule
    FROM
        tasks as parent_tasks
        LEFT JOIN dicom_series ON dicom_series.series_uid = parent_tasks.series_uid
    """

    if group_by:
        # Show all tasks of the specified scope
        query_string = f"""{base_select}
        WHERE parent_tasks.parent_id is null
          {scope_filter_term}
        GROUP BY
            parent_tasks.id, parent_tasks.study_uid, parent_tasks.series_uid, parent_tasks.time, parent_tasks.data,
            tag_accessionnumber, tag_patientid, tag_patientname, dicom_series.tag_seriesdescription, dicom_series.tag_modality
        {having_term}
        ORDER BY
            {order_sql}
        LIMIT :length OFFSET :start
        """
    else:
        # Hierarchical view: show patient tasks OR study tasks (if no patient exists)
        query_string = f"""{base_select}
        WHERE parent_tasks.parent_id is null
          AND (
              -- Show patient tasks
              parent_tasks.data->'info'->>'uid_type' = 'patient'
              OR (
                  -- Show study tasks only if no patient task exists with same MRN
                  parent_tasks.data->'info'->>'uid_type' = 'study'
                  AND NOT EXISTS (
                      SELECT 1 FROM tasks pt
                      WHERE pt.data->'info'->>'uid_type' = 'patient'
                        AND pt.data->'info'->>'mrn' = COALESCE(parent_tasks.data->'info'->>'mrn', tag_patientid)
                  )
              )
          )
        GROUP BY
            parent_tasks.id, parent_tasks.study_uid, parent_tasks.series_uid, parent_tasks.time, parent_tasks.data,
            tag_accessionnumber, tag_patientid, tag_patientname, dicom_series.tag_seriesdescription, dicom_series.tag_modality
        {having_term}
        ORDER BY
            {order_sql}
        LIMIT :length OFFSET :start
        """
    # Get total count before filtering
    params = {"search_term": search_term} if search_term else {}

    count_result = await db.database.fetch_one(count_query_string, params)
    total_count = count_result["total_count"] if count_result else 0
    filtered_count = total_count  # In this case, total and filtered are the same since we're not implementing separate filtering

    # Execute main query with pagination parameters
    params.update({"start": start if start is not None else 0, "length": length if length > 0 else None})
    result_rows = await db.database.fetch_all(query_string, params)
    results = [dict(row) for row in result_rows]

    # Format data for DataTables
    data = []
    for item in results:
        task_id = item["task_id"]
        time = item["time"]
        acc = item["acc"] or ""
        mrn = item["mrn"] or ""

        scope_value = (item.get("scope") or "").lower()
        if scope_value == "study":
            job_scope = "STUDY"
        elif scope_value == "patient":
            job_scope = "PATIENT"
        else:
            job_scope = "SERIES"

        data.append({
            "DT_RowId": f"task_{task_id}",  # Add DataTables row identifier
            "ACC": acc,
            "MRN": mrn,
            "Scope": job_scope,
            "Time": time.isoformat(timespec='seconds') if isinstance(time, datetime.datetime) else str(time),
            "Rule": (item.get("rule") or "").replace("{", "").replace("}", ""),
            "task_id": task_id,  # Include task_id for actions/links
            "study_uid": item.get("study_uid", ""),  # Include study_uid for child task lookup
            "series_uid": item.get("series_uid", ""),  # Include series_uid for series-level tasks
            "series_description": item.get("series_description", ""),  # Series description from DICOM
            "modality": item.get("modality", ""),  # Modality from DICOM
            "child_count": item.get("child_count", 0)  # Include child count for expandable rows
        })

    # Return response in DataTables expected format
    response = {
        "draw": draw,  # Echo back the draw parameter
        "recordsTotal": total_count,  # Total records before filtering
        "recordsFiltered": filtered_count,  # Total records after filtering
        "data": data  # The data to be displayed
    }

    return CustomJSONResponse(response)


def convert_key(tag_key):
    # Remove any leading/trailing whitespace and parentheses
    tag_key = tag_key.strip('()')

    # Convert tag string to integer tuple format
    try:
        # Get human-readable keyword
        keyword = keyword_for_tag(tag_key)
        return keyword if keyword else tag_key
    except:
        logger.exception(f"Error converting tag {tag_key} to keyword")
        return tag_key


def dicom_to_readable_json(ds: pydicom.Dataset):
    """
    Converts a DICOM file to a human-readable JSON format.

    Args:
        file_path (str): Path to the DICOM file.
        output_file_path (str): Path to save the JSON output.
    """
    try:
        result = json.dumps(ds, default=convert_to_serializable)
        return json.loads(result)
    except Exception as e:
        logger.exception(f"Error converting DICOM to readable JSON: {e}")
        return {}


def convert_to_serializable(obj):
    """
    Converts non-serializable objects to serializable types.
    """
    if isinstance(obj, pydicom.dataset.Dataset):
        return {keyword_for_tag(el.tag) or el.tag.json_key[:4]+","+el.tag.json_key[4:]: obj[el.tag] for el in obj.elements()}
    if isinstance(obj, pydicom.dataelem.DataElement):
        try:
            obj.maxBytesToDisplay = 500
            obj.descripWidth = 500
            # see if the representation of this element can be converted to JSON
            # this will convert eg lists to python lists, numbers to python numbers, etc
            json.dumps(evaled := ast.literal_eval(obj.repval))
            return evaled
        except:
            return obj.repval
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


micsi_query_app = Starlette(routes=router)
