"""
query.py
========
Entry functions of the bookkeeper for querying processing information.
"""

import ast
import datetime
import json
from pathlib import Path
# Standard python includes
from typing import Dict

# App-specific includes
import bookkeeping.database as db
import pydicom
import sqlalchemy
from bookkeeping.helper import CustomJSONResponse, json
from common import config
from decoRouter import Router as decoRouter
from pydicom.datadict import keyword_for_tag
from sqlalchemy import select
# Starlette-related includes
from starlette.applications import Starlette
from starlette.authentication import requires
from starlette.responses import JSONResponse

router = decoRouter()
logger = config.get_logger()
tz_conversion = ""


def set_timezone_conversion() -> None:
    global tz_conversion
    tz_conversion = ""
    if config.mercure.server_time != config.mercure.local_time:
        tz_conversion = f" AT time zone '{config.mercure.server_time}' at time zone '{config.mercure.local_time}' "

###################################################################################
# Query endpoints
###################################################################################


@router.get("/series")
@requires("authenticated")
async def get_series(request) -> JSONResponse:
    """Endpoint for retrieving series in the database."""
    series_uid = request.query_params.get("series_uid", "")
    query = db.dicom_series.select()
    if series_uid:
        query = query.where(db.dicom_series.c.series_uid == series_uid)

    result = await db.database.fetch_all(query)
    series = [dict(row) for row in result]

    for i, line in enumerate(series):
        series[i] = {
            k: line[k] for k in line if k in ("id", "time", "series_uid", "tag_seriesdescription", "tag_modality")
        }
    return CustomJSONResponse(series)


@router.get("/tasks")
@requires("authenticated")
async def get_tasks(request) -> JSONResponse:
    """Endpoint for retrieving tasks in the database."""
    query = (
        sqlalchemy.select(
            db.tasks_table.c.id, db.tasks_table.c.time, db.dicom_series.c.tag_seriesdescription, db.dicom_series.c.tag_modality
        )
        .where(db.tasks_table.c.parent_id.is_(None))  # only show tasks without parents
        .join(
            db.dicom_series,
            # sqlalchemy.or_(
            # (dicom_series.c.study_uid == tasks_table.c.study_uid),
            (db.dicom_series.c.series_uid == db.tasks_table.c.series_uid),
            # ),
            isouter=True,
        )
    )
    # query = sqlalchemy.text(
    #     """ select tasks.id as task_id, tasks.time, tasks.series_uid, tasks.study_uid,
    #         "tag_seriesdescription", "tag_modality" from tasks
    #         join dicom_series on tasks.study_uid = dicom_series.study_uid
    #           or tasks.series_uid = dicom_series.series_uid """
    # )
    results = await db.database.fetch_all(query)
    return CustomJSONResponse(results)


@router.get("/tests")
@requires("authenticated")
async def get_test_task(request) -> JSONResponse:
    query = db.tests_table.select().order_by(db.tests_table.c.time_begin.desc())
    # query = (
    #     sqlalchemy.select(
    #         tasks_table.c.id, tasks_table.c.time, dicom_series.c.tag_seriesdescription, dicom_series.c.tag_modality
    #     )
    #     .join(
    #         dicom_series,
    #         sqlalchemy.or_(
    #             (dicom_series.c.study_uid == tasks_table.c.study_uid),
    #             (dicom_series.c.series_uid == tasks_table.c.series_uid),
    #         ),
    #     )
    #     .where(dicom_series.c.tag_seriesdescription == "self_test_series " + request.query_params.get("id", ""))
    # )
    result_rows = await db.database.fetch_all(query)
    results = [dict(row) for row in result_rows]
    for k in results:
        if not k["time_end"]:
            if k["time_begin"] < datetime.datetime.now() - datetime.timedelta(minutes=10):
                k["status"] = "failed"
    return CustomJSONResponse(results)


@router.get("/task-events")
@requires("authenticated")
async def get_task_events(request) -> JSONResponse:
    """Endpoint for getting all events related to one task."""

    task_id = request.query_params.get("task_id", "")
    subtask_query = sqlalchemy.select(db.tasks_table.c.id).where(db.tasks_table.c.parent_id == task_id)

    # Note: The space at the end is needed for the case that there are no subtasks
    subtask_ids_str = ""
    for row in await db.database.fetch_all(subtask_query):
        subtask_ids_str += f"'{row[0]}',"

    subtask_ids_filter = ""
    if subtask_ids_str:
        subtask_ids_filter = "or task_events.task_id in (" + subtask_ids_str[:-1] + ")"

    # Get all the task_events from task `task_id` or any of its subtasks
    # subtask_ids = [row[0] for row in await database.fetch_all(subtask_query)]
    # query = (
    #     task_events.select()
    #     .order_by(task_events.c.task_id, task_events.c.time)
    #     .where(sqlalchemy.or_(task_events.c.task_id == task_id, task_events.c.task_id.in_(subtask_ids)))
    # )

    query_string = f"""select *, time {tz_conversion} as local_time from task_events
        where task_events.task_id = '{task_id}' {subtask_ids_filter}
        order by task_events.task_id, task_events.time
        """
    # print("SQL Query = " + query_string)
    query = sqlalchemy.text(query_string)

    results = await db.database.fetch_all(query)
    return CustomJSONResponse(results)


@router.get("/dicom-files")
@requires("authenticated")
async def get_dicom_files(request) -> JSONResponse:
    """Endpoint for getting all events related to one series."""
    series_uid = request.query_params.get("series_uid", "")
    query = db.dicom_files.select().order_by(db.dicom_files.c.time)
    if series_uid:
        query = query.where(db.dicom_files.c.series_uid == series_uid)
    results = await db.database.fetch_all(query)
    return CustomJSONResponse(results)


@router.get("/task_process_logs")
@requires("authenticated")
async def get_task_process_logs(request) -> JSONResponse:
    """Endpoint for getting all processing logs related to one task.

    If no logs found for the given task_id, looks for a parent task
    (patient/study) with the same MRN and returns logs from that.
    """
    task_id = request.query_params.get("task_id", "")

    async def get_logs_for_task(tid):
        subtask_query = (
            db.tasks_table.select()
            .order_by(db.tasks_table.c.id)
            .where(sqlalchemy.or_(db.tasks_table.c.id == tid, db.tasks_table.c.parent_id == tid))
        )
        subtasks = await db.database.fetch_all(subtask_query)
        subtask_ids = [row[0] for row in subtasks]

        query = (db.processor_logs_table.select(db.processor_logs_table.c.task_id.in_(subtask_ids))
                                        .order_by(db.processor_logs_table.c.id))
        results = [dict(r) for r in await db.database.fetch_all(query)]
        for result in results:
            if result["logs"] is None:
                if logs_folder := config.mercure.processing_logs.logs_file_store:
                    try:
                        result["logs"] = (
                            Path(logs_folder) / result["task_id"] / f"{result['module_name']}.{result['id']}.txt"
                        ).read_text(encoding="utf-8")
                    except FileNotFoundError:
                        result["logs"] = None
        return results

    # First try direct lookup
    results = await get_logs_for_task(task_id)
    if results:
        return CustomJSONResponse(results)

    # No results - try to find parent task with same MRN
    mrn_query = """
    SELECT
        t.data->'info'->>'uid_type' as uid_type,
        COALESCE(t.data->'info'->>'mrn', ds.tag_patientid) as mrn,
        t.time
    FROM tasks t
    LEFT JOIN dicom_series ds ON ds.series_uid = t.series_uid
    WHERE t.id = :task_id
    """
    task_result = await db.database.fetch_one(mrn_query, {"task_id": task_id})

    if not task_result:
        return CustomJSONResponse([])

    task_dict = dict(task_result)
    uid_type = task_dict.get("uid_type")
    mrn = task_dict.get("mrn")
    task_time = task_dict.get("time")

    # If already a patient task, no parent to find
    if uid_type == "patient" or not mrn:
        return CustomJSONResponse([])

    # Find parent task (patient or study) with same MRN within time window
    if isinstance(task_time, str):
        task_time = datetime.datetime.fromisoformat(task_time.replace('Z', '+00:00'))
    time_start = task_time - datetime.timedelta(minutes=10)
    time_end = task_time + datetime.timedelta(minutes=5)

    parent_query = """
    SELECT t.id as task_id
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
    LIMIT 1
    """
    parent_result = await db.database.fetch_one(parent_query, {
        "task_id": task_id,
        "mrn": mrn,
        "time_start": time_start,
        "time_end": time_end
    })

    if parent_result:
        results = await get_logs_for_task(parent_result["task_id"])

    return CustomJSONResponse(results)


@router.get("/task_process_results")
@requires("authenticated")
async def get_task_process_results(request) -> JSONResponse:
    """Endpoint for getting all processing results from a task.

    If no results found for the given task_id, looks for a parent task
    (patient/study) with the same MRN and returns results from that.
    """
    task_id = request.query_params.get("task_id", "")

    # First try direct lookup
    query = (db.processor_outputs_table.select()
                                       .where(db.processor_outputs_table.c.task_id == task_id)
                                       .order_by(db.processor_outputs_table.c.id))
    results = [dict(r) for r in await db.database.fetch_all(query)]

    if results:
        return CustomJSONResponse(results)

    # No results - try to find parent task with same MRN
    mrn_query = """
    SELECT
        t.data->'info'->>'uid_type' as uid_type,
        COALESCE(t.data->'info'->>'mrn', ds.tag_patientid) as mrn,
        t.time
    FROM tasks t
    LEFT JOIN dicom_series ds ON ds.series_uid = t.series_uid
    WHERE t.id = :task_id
    """
    task_result = await db.database.fetch_one(mrn_query, {"task_id": task_id})

    if not task_result:
        return CustomJSONResponse([])

    task_dict = dict(task_result)
    uid_type = task_dict.get("uid_type")
    mrn = task_dict.get("mrn")
    task_time = task_dict.get("time")

    # If already a patient task, no parent to find
    if uid_type == "patient" or not mrn:
        return CustomJSONResponse([])

    # Find parent task (patient or study) with same MRN within time window
    if isinstance(task_time, str):
        task_time = datetime.datetime.fromisoformat(task_time.replace('Z', '+00:00'))
    time_start = task_time - datetime.timedelta(minutes=10)
    time_end = task_time + datetime.timedelta(minutes=5)

    parent_query = """
    SELECT t.id as task_id
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
    LIMIT 1
    """
    parent_result = await db.database.fetch_one(parent_query, {
        "task_id": task_id,
        "mrn": mrn,
        "time_start": time_start,
        "time_end": time_end
    })

    if parent_result:
        parent_id = parent_result["task_id"]
        query = (db.processor_outputs_table.select()
                                           .where(db.processor_outputs_table.c.task_id == parent_id)
                                           .order_by(db.processor_outputs_table.c.id))
        results = [dict(r) for r in await db.database.fetch_all(query)]

    return CustomJSONResponse(results)


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
    order_direction = request.query_params.get("order[0][dir]", "desc")  # Default to descending

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
    order_sql = f"{order_column} {order_direction.upper()}, parent_tasks.id {order_direction.upper()}"

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


@router.get("/get_task_info")
@requires("authenticated")
async def get_task_info(request) -> JSONResponse:
    response: Dict = {}

    task_id = request.query_params.get("task_id", "")
    if not task_id:
        return CustomJSONResponse(response)
    # First, get general information about the series/study
    query = (
        select(db.dicom_series, db.tasks_table.c.data)
        .select_from(db.tasks_table)
        .join(db.dicom_series, db.dicom_series.c.series_uid == db.tasks_table.c.series_uid, isouter=True)
        .where(
            db.tasks_table.c.id == task_id,
            db.tasks_table.c.parent_id.is_(None)
        )
        .limit(1)
    )
    result = await db.database.fetch_one(query)
    # info_rows = await db.database.fetch_all(info_query)
    if result:
        result_dict = dict(result)
        rename = {
            "series_uid": "SeriesUID",
            "study_uid": "StudyUID",
            "tag_patientname": "PatientName",
            "tag_patientid": "PatientID",
            "tag_accessionnumber": "AccessionNumber",
            "tag_seriesnumber": "SeriesNumber",
            "tag_studyid": "StudyID",
            "tag_patientbirthdate": "PatientBirthDate",
            "tag_patientsex": "PatientSex",
            "tag_acquisitiondate": "AcquisitionDate",
            "tag_acquisitiontime": "AcquisitionTime",
            "tag_modality": "Modality",
            "tag_bodypartexamined": "BodyPartExamined",
            "tag_studydescription": "StudyDescription",
            "tag_seriesdescription": "SeriesDescription",
            "tag_protocolname": "ProtocolName",
            "tag_codevalue": "CodeValue",
            "tag_codemeaning": "CodeMeaning",
            "tag_sequencename": "SequenceName",
            "tag_scanningsequence": "ScanningSequence",
            "tag_sequencevariant": "SequenceVariant",
            "tag_slicethickness": "SliceThickness",
            "tag_contrastbolusagent": "ContrastBolusAgent",
            "tag_referringphysicianname": "ReferringPhysicianName",
            "tag_manufacturer": "Manufacturer",
            "tag_manufacturermodelname": "ManufacturerModelName",
            "tag_magneticfieldstrength": "MagneticFieldStrength",
            "tag_deviceserialnumber": "DeviceSerialNumber",
            "tag_softwareversions": "SoftwareVersions",
            "tag_stationname": "StationName",
        }

        response["information"] = {
            rename.get(x, x): result_dict.get(x)
            for x in result_dict.keys() if x not in ('id', 'time', 'data')
        }
        try:
            if 'data' in result_dict and isinstance(result_dict['data'], str):
                data = json.loads(result_dict.get('data', '{}'))
                if data is not None:
                    tags = dict(data).get("tags", None)
                    if tags is not None:
                        ds = pydicom.Dataset.from_json(tags)
                        response["sample_tags_received"] = dicom_to_readable_json(ds)
        except:
            logger.exception("Error parsing data")

    # Now, get the task files embedded into the task or its subtasks
    query = (
        db.tasks_table.select()
        .order_by(db.tasks_table.c.id)
        .where(sqlalchemy.or_(db.tasks_table.c.id == task_id, db.tasks_table.c.parent_id == task_id))
    )
    result_rows = await db.database.fetch_all(query)
    results = [dict(row) for row in result_rows]
    for item in results:
        if item["data"] and set(item["data"].keys()) != {"id", "tags"}:
            task_id = "task " + item["id"]
            response[task_id] = item["data"]

        task_folder = None
        for k in [Path(config.mercure.success_folder), Path(config.mercure.error_folder)]:
            if (found_folder := k / item["id"]).exists():
                task_folder = found_folder
                break
        else:
            continue

        try:
            sample_file = next(task_folder.rglob("*.dcm"))
            tags = dicom_to_readable_json(pydicom.dcmread(sample_file, stop_before_pixels=True))
            if task_id not in response:
                response[task_id] = {}
            response[task_id]["sample_tags_result"] = tags
        except (StopIteration, json.JSONDecodeError):
            pass

    return CustomJSONResponse(response)


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


query_app = Starlette(routes=router)
