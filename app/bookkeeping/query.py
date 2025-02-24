"""
query.py
========
Entry functions of the bookkeeper for querying processing information.
"""

import datetime
from pathlib import Path
# Standard python includes
from typing import Dict

# App-specific includes
import bookkeeping.database as db
import sqlalchemy
from bookkeeping.helper import CustomJSONResponse, json
from common import config
from decoRouter import Router as decoRouter
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
    """Endpoint for getting all processing logs related to one series."""
    task_id = request.query_params.get("task_id", "")

    subtask_query = (
        db.tasks_table.select()
        .order_by(db.tasks_table.c.id)
        .where(sqlalchemy.or_(db.tasks_table.c.id == task_id, db.tasks_table.c.parent_id == task_id))
    )

    subtasks = await db.database.fetch_all(subtask_query)
    subtask_ids = [row[0] for row in subtasks]

    query = (db.processor_logs_table.select(db.processor_logs_table.c.task_id.in_(subtask_ids))
                                    .order_by(db.processor_logs_table.c.id))
    results = [dict(r) for r in await db.database.fetch_all(query)]
    for result in results:
        if result["logs"] is None:
            if logs_folder := config.mercure.processing_logs.logs_file_store:
                result["logs"] = (
                    Path(logs_folder) / result["task_id"] / f"{result['module_name']}.{result['id']}.txt"
                ).read_text(encoding="utf-8")
    return CustomJSONResponse(results)


@router.get("/task_process_results")
@requires("authenticated")
async def get_task_process_results(request) -> JSONResponse:
    """Endpoint for getting all processing results from a task."""
    task_id = request.query_params.get("task_id", "")

    query = (db.processor_outputs_table.select()
                                       .where(db.processor_outputs_table.c.task_id == task_id)
                                       .order_by(db.processor_outputs_table.c.id))
    results = [dict(r) for r in await db.database.fetch_all(query)]
    return CustomJSONResponse(results)


@router.get("/find_task")
@requires("authenticated")
async def find_task(request) -> JSONResponse:
    search_term = request.query_params.get("search_term", "")
    study_filter = request.query_params.get("study_filter", "false")
    filter_term = ""
    if search_term:
        filter_term = (f"""and ((tag_accessionnumber ilike :search_term || '%') """
                       + f"""or (tag_patientid ilike :search_term || '%') """
                       + f"""or (tag_patientname ilike '%' || :search_term || '%'))""")

    study_filter_term = ""
    if study_filter == "true":
        study_filter_term = "and tasks.study_uid is not null"

    query_string = f"""
SELECT
    tag_accessionnumber AS acc,
    tag_patientid AS mrn,
    parent_tasks.id AS task_id,
    parent_tasks.data->'info'->>'uid_type' AS scope,
    parent_tasks.time::timestamp AS time,
    STRING_AGG(child_tasks.data->'info'->>'applied_rule', ', ' ORDER BY child_tasks.id) AS rule,
    STRING_AGG(child_tasks.data->'info'->>'triggered_rules', ',' ORDER BY child_tasks.id) AS triggered_rules
FROM (select * from tasks limit 512) as parent_tasks
    LEFT JOIN dicom_series ON dicom_series.series_uid = parent_tasks.series_uid
    LEFT JOIN tasks as child_tasks ON (child_tasks.parent_id = parent_tasks.id)
WHERE parent_tasks.parent_id IS NULL {filter_term} {study_filter_term}
GROUP BY 1,2,3,4,5
ORDER BY parent_tasks.time DESC, parent_tasks.id DESC
"""
    # print(query_string)
    response: Dict = {}
    logger.info(query_string)
    result_rows = await db.database.fetch_all(query_string, {"search_term": search_term} if search_term else None)
    results = [dict(row) for row in result_rows]

    for item in results:
        task_id = item["task_id"]
        time = item["time"]
        acc = item["acc"]
        mrn = item["mrn"]

        if item["scope"] == "study":
            job_scope = "STUDY"
        else:
            job_scope = "SERIES"

        if item["rule"]:
            item["rule"] = item["rule"].strip()
            if item["rule"] == ",":
                item["rule"] = ""

        rule_information = ""
        if item["rule"]:
            rule_information = item["rule"]
        else:
            if item["triggered_rules"]:
                try:
                    json_data = json.loads("[" + item["triggered_rules"] + "]")
                    for entry in json_data:
                        rule_information += ", ".join(list(entry.keys())) + ", "
                    if rule_information:
                        rule_information = rule_information[:-2]
                except json.JSONDecodeError:
                    rule_information = "ERROR"
            else:
                rule_information = ""

        response[task_id] = {
            "ACC": acc,
            "MRN": mrn,
            "Scope": job_scope,
            "Time": time,
            "Rule": rule_information,
        }

    return CustomJSONResponse(response)


@router.get("/get_task_info")
@requires("authenticated")
async def get_task_info(request) -> JSONResponse:
    response: Dict = {}

    task_id = request.query_params.get("task_id", "")
    if not task_id:
        return CustomJSONResponse(response)
    # First, get general information about the series/study
    query = (
        select(db.dicom_series)
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

        response["information"] = {rename.get(x, x): result_dict.get(x) for x in result.keys() if x not in ('id', 'time')}
    # Now, get the task files embedded into the task or its subtasks
    query = (
        db.tasks_table.select()
        .order_by(db.tasks_table.c.id)
        .where(sqlalchemy.or_(db.tasks_table.c.id == task_id, db.tasks_table.c.parent_id == task_id))
    )
    result_rows = await db.database.fetch_all(query)
    results = [dict(row) for row in result_rows]

    for item in results:
        if item["data"]:
            task_id = "task " + item["id"]
            response[task_id] = item["data"]

        if not response.get("tags") and (task_folder := Path(config.mercure.success_folder) / item["id"]).exists():
            try:
                tags_file = next(task_folder.rglob("*.tags"))
                tags = json.loads(tags_file.read_text())
                if task_id not in response:
                    response[task_id] = {}
                response[task_id]["sample_tags"] = tags
            except (StopIteration, json.JSONDecodeError):
                pass

    return CustomJSONResponse(response)

query_app = Starlette(routes=router)
