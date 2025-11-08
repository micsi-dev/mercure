"""
route_studies.py
================
Provides functions for routing and processing of studies (consisting of multiple series).
"""

import json
# Standard python includes
import os
import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Union

# App-specific includes
import common.config as config
import common.helper as helper
import common.log_helpers as log_helpers
import common.monitor as monitor
import common.notification as notification
import common.rule_evaluation as rule_evaluation
from common.constants import mercure_actions, mercure_events, mercure_names, mercure_rule
from common.types import (
    PatientTriggerCondition,
    StudyTriggerCondition,
    Task,
    TaskHasPatient,
    TaskHasStudy,
    TaskInfo,
    TaskPatientStudy,
)
from routing.generate_taskfile import create_patient_task, update_patient_task
from routing.common import generate_task_id

# Create local logger instance
logger = config.get_logger()


def route_studies(pending_series: Dict[str, float]) -> None:
    """
    Searches for completed studies and initiates the routing of the completed studies
    """
    # TODO: Handle studies that exceed the "force completion" timeout in the "CONDITION_RECEIVED_SERIES" mode
    studies_ready = {}
    with os.scandir(config.mercure.studies_folder) as it:
        it = list(it)  # type: ignore
        for entry in it:
            if entry.is_dir() and not is_study_locked(entry.path):
                if is_study_complete(entry.path, pending_series):
                    modificationTime = entry.stat().st_mtime
                    studies_ready[entry.name] = modificationTime
                else:
                    if not check_force_study_timeout(Path(entry.path)):
                        logger.error(f"Error during checking force study timeout for study {entry.path}")
    logger.debug(f"Studies ready for processing: {studies_ready}")
    # Process all complete studies
    for dir_entry in sorted(studies_ready):
        study_success = False
        try:
            study_success = route_study(dir_entry)
        except Exception:
            error_message = f"Problems while processing study {dir_entry}"
            logger.exception(error_message)
            # TODO: Add study events to bookkeeper
            # monitor.send_series_event(monitor.task_event.ERROR, entry, 0, "", "Exception while processing")
            monitor.send_event(
                monitor.m_events.PROCESSING,
                monitor.severity.ERROR,
                error_message,
            )
        if not study_success:
            # Move the study to the error folder to avoid repeated processing
            push_studylevel_error(dir_entry)

        # If termination is requested, stop processing after the active study has been completed
        if helper.is_terminated():
            return


def is_study_locked(folder: str) -> bool:
    """
    Returns true if the given folder is locked, i.e. if another process is already working on the study
    """
    path = Path(folder)
    folder_status = (
        (path / mercure_names.LOCK).exists()
        or (path / mercure_names.PROCESSING).exists()
        or len(list(path.glob(mercure_names.DCMFILTER))) == 0
    )
    return folder_status


def is_study_complete(folder: str, pending_series: Dict[str, float]) -> bool:
    """
    Returns true if the study in the given folder is ready for processing,
    i.e. if the completeness criteria of the triggered rule has been met
    """
    try:
        logger.debug(f"Checking completeness of study {folder}, with pending series: {pending_series}")
        # Read stored task file to determine completeness criteria

        with open(Path(folder) / mercure_names.TASKFILE, "r") as json_file:
            task: TaskHasStudy = TaskHasStudy(**json.load(json_file))

        if task.study.complete_force is True:
            return True
        if (Path(folder) / mercure_names.FORCE_COMPLETE).exists():
            task.study.complete_force = True
            with open(Path(folder) / mercure_names.TASKFILE, "w") as json_file:
                json.dump(task.dict(), json_file)
            return True

        study = task.study

        # Check if processing of the study has been enforced (e.g., via UI selection)
        if not study.complete_trigger:
            logger.error(f"Missing trigger condition in task file in study folder {folder}", task.id)  # handle_error
            return False

        complete_trigger: StudyTriggerCondition = study.complete_trigger
        complete_required_series = study.get("complete_required_series", "")

        # If trigger condition is received series but list of required series is missing, then switch to timeout mode instead
        if (study.complete_trigger == mercure_rule.STUDY_TRIGGER_CONDITION_RECEIVED_SERIES) and (
            not complete_required_series
        ):
            complete_trigger = mercure_rule.STUDY_TRIGGER_CONDITION_TIMEOUT  # type: ignore
            logger.warning(  # handle_error
                f"Missing series for trigger condition in study folder {folder}. Using timeout instead", task.id
            )

        # Check for trigger condition
        if complete_trigger == mercure_rule.STUDY_TRIGGER_CONDITION_TIMEOUT:
            return check_study_timeout(task, pending_series)
        elif complete_trigger == mercure_rule.STUDY_TRIGGER_CONDITION_RECEIVED_SERIES:
            return check_study_series(task, complete_required_series)
        else:
            logger.error(f"Invalid trigger condition in task file in study folder {folder}", task.id)  # handle_error
            return False
    except Exception:
        logger.error(f"Invalid task file in study folder {folder}")  # handle_error
        return False


def check_study_timeout(task: TaskHasStudy, pending_series: Dict[str, float]) -> bool:
    """
    Checks if the duration since the last series of the study was received exceeds the study completion timeout
    """
    logger.debug("Checking study timeout")
    study = task.study
    last_received_string = study.last_receive_time
    logger.debug(f"Last received time: {last_received_string}, now is: {datetime.now()}")
    if not last_received_string:
        return False

    last_receive_time = datetime.strptime(last_received_string, "%Y-%m-%d %H:%M:%S")
    if datetime.now() > last_receive_time + timedelta(seconds=config.mercure.study_complete_trigger):
        # Check if there is a pending series on this study.
        # If so, we need to wait for it to timeout before we can complete the study
        for series_uid in pending_series.keys():
            try:
                example_file = next((Path(config.mercure.incoming_folder) / series_uid).glob(f"{series_uid}*.tags"))
            except StopIteration:  # No tag file with this series UID was found
                logger.error(f"No tag file for series UID {series_uid} was found")
                raise
            tags_list = json.loads(example_file.read_text())
            if tags_list["StudyInstanceUID"] == study.study_uid:
                logger.debug(f"Timeout met, but found a pending series ({series_uid}) in study {study.study_uid}")
                return False
        logger.debug("Timeout met.")
        return True
    else:
        logger.debug("Timeout not met.")
        return False


def check_force_study_timeout(folder: Path) -> bool:
    """
    Checks if the duration since the creation of the study exceeds the force study completion timeout
    """
    try:
        logger.debug("Checking force study timeout")

        with open(folder / mercure_names.TASKFILE, "r") as json_file:
            task: TaskHasStudy = TaskHasStudy(**json.load(json_file))

        study = task.study
        creation_string = study.creation_time
        if not creation_string:
            logger.error(f"Missing creation time in task file in study folder {folder}", task.id)  # handle_error
            return False
        logger.debug(f"Creation time: {creation_string}, now is: {datetime.now()}")

        creation_time = datetime.strptime(creation_string, "%Y-%m-%d %H:%M:%S")
        if datetime.now() > creation_time + timedelta(seconds=config.mercure.study_forcecomplete_trigger):
            logger.info(f"Force timeout met for study {folder}")
            if not study.complete_force_action or study.complete_force_action == "ignore":
                return True
            elif study.complete_force_action == "proceed":
                logger.info(f"Forcing study completion for study {folder}")
                (folder / mercure_names.FORCE_COMPLETE).touch()
            elif study.complete_force_action == "discard":
                logger.info(f"Moving folder to discard: {folder.name}")
                lock_file = Path(folder / mercure_names.LOCK)
                try:
                    lock = helper.FileLock(lock_file)
                except Exception:
                    logger.error(f"Unable to lock study for removal {lock_file}")  # handle_error
                    return False
                if not move_study_folder(task.id, folder.name, "DISCARD"):
                    logger.error(f"Error during moving study to discard folder {study}", task.id)  # handle_error
                    return False
                if not remove_study_folder(None, folder.name, lock):
                    logger.error(f"Unable to delete study folder {lock_file}")  # handle_error
                    return False
        else:
            logger.debug("Force timeout not met.")
        return True

    except Exception:
        logger.error(f"Could not check force study timeout for study {folder}")  # handle_error
        return False


def check_study_series(task: TaskHasStudy, required_series: str) -> bool:
    """
    Checks if all series required for study completion have been received
    """
    received_series = []

    # Fetch the list of received series descriptions from the task file
    if (task.study.received_series) and (isinstance(task.study.received_series, list)):
        received_series = task.study.received_series

    # Check if the completion criteria is fulfilled
    return rule_evaluation.parse_completion_series(task.id, required_series, received_series)


@log_helpers.clear_task_decorator
def route_study(study) -> bool:
    """
    Processses the study in the folder 'study'. Loads the task file and delegates the action to helper functions
    """
    logger.debug(f"Route_study {study}")
    study_folder = config.mercure.studies_folder + "/" + study
    if is_study_locked(study_folder):
        # If the study folder has been locked in the meantime, then skip and proceed with the next one
        return True

    # Create lock file in the study folder and prevent other instances from working on this study
    lock_file = Path(study_folder + "/" + study + mercure_names.LOCK)
    if lock_file.exists():
        return True
    try:
        lock = helper.FileLock(lock_file)
    except Exception:
        # Can't create lock file, so something must be seriously wrong
        try:
            task = Task.from_file(Path(study_folder) / mercure_names.TASKFILE)
            logger.error(f"Unable to create study lock file {lock_file}", task.id)  # handle_error
        except Exception:
            logger.error(f"Unable to create study lock file {lock_file}", None)  # handle_error
        return False

    try:
        # Read stored task file to determine completeness criteria
        task = Task.from_file(Path(study_folder) / mercure_names.TASKFILE)
    except Exception:
        try:
            with open(Path(study_folder) / mercure_names.TASKFILE, "r") as json_file:
                logger.error(
                    f"Invalid task file in study folder {study_folder}", json.load(json_file)["id"]
                )  # handle_error
        except Exception:
            logger.error(f"Invalid task file in study folder {study_folder}", None)  # handle_error
        return False

    logger.setTask(task.id)
    action_result = True
    info: TaskInfo = task.info
    action = info.get("action", "")

    if not action:
        logger.error(f"Missing action in study folder {study_folder}", task.id)  # handle_error
        return False

    # TODO: Clean folder for duplicate DICOMs (i.e., if series have been sent twice -- check by instance UID)

    # Check if this study should be aggregated at patient level
    applied_rule = info.get("applied_rule", "")
    if applied_rule and config.mercure.rules.get(applied_rule):
        rule_action_trigger = config.mercure.rules[applied_rule].get("action_trigger", "series")
        if rule_action_trigger == "patient":
            # Move study to patient folder instead of routing directly
            action_result = push_studylevel_patient(study, task)
            if not action_result:
                logger.error(f"Error during moving study to patient folder {study}", task.id)  # handle_error
                return False
            if not remove_study_folder(task.id, study, lock):
                logger.error(f"Error removing folder of study {study}", task.id)  # handle_error
                return False
            return True

    if action == mercure_actions.NOTIFICATION:
        action_result = push_studylevel_notification(study, task)
    elif action == mercure_actions.ROUTE:
        action_result = push_studylevel_dispatch(study, task)
    elif action == mercure_actions.PROCESS or action == mercure_actions.BOTH:
        action_result = push_studylevel_processing(study, task)
    else:
        # This point should not be reached (discard actions should be handled on the series level)
        logger.error(f"Invalid task action in study folder {study_folder}", task.id)  # handle_error
        return False

    if not action_result:
        logger.error(f"Error during processing of study {study}", task.id)  # handle_error
        return False

    if not remove_study_folder(task.id, study, lock):
        logger.error(f"Error removing folder of study {study}", task.id)  # handle_error
        return False
    return True


def push_studylevel_dispatch(study: str, task: Task) -> bool:
    """
    Pushes the study folder to the dispatchter, including the generated task file containing the destination information
    """
    trigger_studylevel_notification(study, task, mercure_events.RECEIVED)
    return move_study_folder(task.id, study, "OUTGOING")


def push_studylevel_processing(study: str, task: Task) -> bool:
    """
    Pushes the study folder to the processor, including the generated task file containing the processing instructions
    """
    trigger_studylevel_notification(study, task, mercure_events.RECEIVED)
    return move_study_folder(task.id, study, "PROCESSING")


def push_studylevel_notification(study: str, task: Task) -> bool:
    """
    Executes the study-level reception notification
    """
    trigger_studylevel_notification(study, task, mercure_events.RECEIVED)
    trigger_studylevel_notification(study, task, mercure_events.COMPLETED)
    move_study_folder(task.id, study, "SUCCESS")
    return True


def push_studylevel_patient(study: str, task: Task) -> bool:
    """
    Moves the completed study to a patient folder for patient-level aggregation
    """
    logger.debug(f"push_studylevel_patient for study {study}")

    # Get patient ID (MRN) from task
    patient_id = task.info.mrn
    if not patient_id or patient_id == "MISSING":
        logger.error(f"Missing patient ID for study {study}", task.id)
        return False

    # Get applied rule
    applied_rule = task.info.applied_rule
    if not applied_rule:
        logger.error(f"Missing applied_rule for study {study}", task.id)
        return False

    # Get study UID and modality
    study_uid = task.study.study_uid if task.study else task.info.uid
    tags_list = {}

    # Extract tags from first DICOM file in study folder
    study_folder = Path(config.mercure.studies_folder) / study
    try:
        first_dcm = next(study_folder.glob("*.dcm"))
        with open(first_dcm.with_suffix(".tags"), "r") as f:
            tags_list = json.load(f)
    except (StopIteration, Exception) as e:
        logger.error(f"Unable to read tags from study folder {study_folder}", task.id)
        return False

    modality = tags_list.get("Modality", "UNKNOWN")

    # Count series in the study
    series_uids = []
    series_descriptions = []
    if task.study and task.study.received_series_uid:
        series_uids = task.study.received_series_uid
    if task.study and task.study.received_series:
        series_descriptions = task.study.received_series
    series_count = len(series_uids)

    # Create or update patient folder
    patient_folder_name = f"{patient_id}_{applied_rule}"
    patient_folder = Path(config.mercure.patients_folder) / patient_folder_name
    first_study = False

    if not patient_folder.exists():
        try:
            patient_folder.mkdir(parents=True)
            first_study = True
        except Exception:
            logger.error(f"Unable to create patient folder {patient_folder}", task.id)
            return False

    lock_file = patient_folder / mercure_names.LOCK
    try:
        lock = helper.FileLock(lock_file)
    except Exception:
        logger.error(f"Unable to create lock file {lock_file}", task.id)
        return False

    if first_study:
        # Create patient task file
        new_task_id = generate_task_id()
        result = create_patient_task(
            new_task_id,
            patient_folder,
            task.info.triggered_rules if isinstance(task.info.triggered_rules, dict) else {},
            applied_rule,
            patient_id,
            tags_list,
        )
        if not result:
            logger.error(f"Unable to create patient task file for {patient_folder}", task.id)
            lock.free()
            return False
        logger.info(f"Created patient folder for patient {patient_id}")
    else:
        # Get task ID from existing patient task
        try:
            patient_task = Task.from_file(patient_folder / mercure_names.TASKFILE)
            new_task_id = patient_task.id
        except Exception:
            logger.error(f"Unable to read patient task file from {patient_folder}", task.id)
            lock.free()
            return False

    # Update patient task with information from this study
    result, _ = update_patient_task(
        new_task_id,
        patient_folder,
        study_uid,
        modality,
        series_count,
        series_uids,
        series_descriptions,
    )

    if not result:
        logger.error(f"Unable to update patient task file for {patient_folder}", task.id)
        lock.free()
        return False

    # Move study folder contents into patient folder
    # Create a subfolder for this study within the patient folder
    study_subfolder = patient_folder / study_uid
    try:
        study_subfolder.mkdir(exist_ok=True)
    except Exception:
        logger.error(f"Unable to create study subfolder {study_subfolder}", task.id)
        lock.free()
        return False

    # Move all files from study folder to study subfolder in patient folder
    study_source_folder = Path(config.mercure.studies_folder) / study
    for entry in list(os.scandir(study_source_folder)):
        if not entry.name.endswith(mercure_names.LOCK) and not entry.name.endswith(mercure_names.TASKFILE):
            try:
                shutil.move(
                    str(study_source_folder / entry.name),
                    str(study_subfolder / entry.name)
                )
            except Exception:
                logger.error(f"Problem while moving file {entry.name} to patient folder", task.id)

    lock.free()
    logger.info(f"Moved study {study_uid} to patient folder for patient {patient_id}")
    return True


def push_studylevel_error(study: str) -> None:
    """
    Pushes the study folder to the error folder after unsuccessful routing
    """
    study_folder = config.mercure.studies_folder + "/" + study
    lock_file = Path(study_folder + "/" + study + mercure_names.LOCK)
    if lock_file.exists():
        # Study normally shouldn't be locked at this point, but since it is, just exit and wait.
        # Might require manual intervention if a former process terminated without removing the lock file
        return
    try:
        lock = helper.FileLock(lock_file)
    except Exception:
        # Can't create lock file, so something must be seriously wrong
        logger.error(f"Unable to lock study for removal {lock_file}")  # handle_error
        return
    if not move_study_folder(None, study, "ERROR"):
        # At this point, we can only wait for manual intervention
        logger.error(f"Unable to move study to ERROR folder {lock_file}")  # handle_error
        return
    if not remove_study_folder(None, study, lock):
        logger.error(f"Unable to delete study folder {lock_file}")  # handle_error
        return


def move_study_folder(task_id: Union[str, None], study: str, destination: str) -> bool:
    """
    Moves the study subfolder to the specified destination with proper locking of the folders
    """
    logger.debug(f"Move_study_folder {study} to {destination}")
    source_folder = config.mercure.studies_folder + "/" + study
    destination_folder = None
    if destination == "PROCESSING":
        destination_folder = config.mercure.processing_folder
    elif destination == "SUCCESS":
        destination_folder = config.mercure.success_folder
    elif destination == "ERROR":
        destination_folder = config.mercure.error_folder
    elif destination == "OUTGOING":
        destination_folder = config.mercure.outgoing_folder
    elif destination == "DISCARD":
        destination_folder = config.mercure.discard_folder
    else:
        logger.error(f"Unknown destination {destination} requested for {study}", task_id)  # handle_error
        return False

    if task_id is None:
        # Create unique name of destination folder
        destination_folder += "/" + str(uuid.uuid1())
    else:
        # If a task ID exists, name the folder by it to ensure that the files can be found again.
        destination_folder += "/" + str(task_id)

    # Create the destination folder and validate that is has been created
    try:
        os.mkdir(destination_folder)
    except Exception:
        logger.error(f"Unable to create study destination folder {destination_folder}", task_id)  # handle_error
        return False

    if not Path(destination_folder).exists():
        logger.error(f"Creating study destination folder not possible {destination_folder}", task_id)  # handle_error
        return False

    # Create lock file in destination folder (to prevent any other module to work on the folder). Note that
    # the source folder has already been locked in the parent function.
    lock_file = Path(destination_folder) / mercure_names.LOCK
    try:
        lock = helper.FileLock(lock_file)
    except Exception:
        # Can't create lock file, so something must be seriously wrong
        logger.error(f"Unable to create lock file {destination_folder}/{mercure_names.LOCK}", task_id)  # handle_error
        return False

    # Move all files except the lock file
    # FIXME: if we don't use a list instead of an iterator, in testing we get an error
    # from pyfakefs about the iterator changing during the iteration
    for entry in list(os.scandir(source_folder)):
        # Move all files but exclude the lock file in the source folder
        if not entry.name.endswith(mercure_names.LOCK):
            try:
                shutil.move(source_folder + "/" + entry.name, destination_folder + "/" + entry.name)
            except Exception:
                logger.error(  # handle_error
                    f"Problem while pushing file {entry} from {source_folder} to {destination_folder}", task_id
                )

    # Remove the lock file in the target folder. Would happen automatically when leaving the function,
    # but better to do explicitly with error handling
    try:
        lock.free()
    except Exception:
        # Can't delete lock file, so something must be seriously wrong
        logger.error(f"Unable to remove lock file {lock_file}", task_id)  # handle_error
        return False

    return True


def remove_study_folder(task_id: Union[str, None], study: str, lock: helper.FileLock) -> bool:
    """
    Removes a study folder containing nothing but the lock file (called during cleanup after all files have
    been moved somewhere else already)
    """
    study_folder = config.mercure.studies_folder + "/" + study
    # Remove the lock file
    try:
        lock.free()
    except Exception:
        # Can't delete lock file, so something must be seriously wrong
        logger.error(f"Unable to remove lock file while removing study folder {study}", task_id)  # handle_error
        return False
    # Remove the empty study folder
    try:
        shutil.rmtree(study_folder)
    except Exception:
        logger.error(f"Unable to delete study folder {study_folder}", task_id)  # handle_error
    return True


def trigger_studylevel_notification(study: str, task: Task, event: mercure_events) -> bool:
    # Check if the applied_rule is available
    current_rule = task.info.applied_rule
    if not current_rule:
        logger.error(f"Missing applied_rule in task file in study {study}", task.id)  # handle_error
        return False
    notification.trigger_notification_for_rule(current_rule, task.id, event, task=task)
    return True


# ========================================================================================
# Patient-Level Routing Functions
# ========================================================================================


def route_patients(pending_studies: Dict[str, float]) -> None:
    """
    Searches for completed patients and initiates the routing of the completed patients
    """
    patients_ready = {}
    with os.scandir(config.mercure.patients_folder) as it:
        it = list(it)  # type: ignore
        for entry in it:
            if entry.is_dir() and not is_patient_locked(entry.path):
                if is_patient_complete(entry.path, pending_studies):
                    modificationTime = entry.stat().st_mtime
                    patients_ready[entry.name] = modificationTime
                else:
                    if not check_force_patient_timeout(Path(entry.path)):
                        logger.error(f"Error during checking force patient timeout for patient {entry.path}")
    logger.debug(f"Patients ready for processing: {patients_ready}")
    # Process all complete patients
    for dir_entry in sorted(patients_ready):
        patient_success = False
        try:
            patient_success = route_patient(dir_entry)
        except Exception:
            error_message = f"Problems while processing patient {dir_entry}"
            logger.exception(error_message)
            monitor.send_event(
                monitor.m_events.PROCESSING,
                monitor.severity.ERROR,
                error_message,
            )
        if not patient_success:
            # Move the patient to the error folder to avoid repeated processing
            push_patientlevel_error(dir_entry)

        # If termination is requested, stop processing after the active patient has been completed
        if helper.is_terminated():
            return


def is_patient_locked(folder: str) -> bool:
    """
    Returns true if the given folder is locked, i.e. if another process is already working on the patient
    """
    path = Path(folder)
    folder_status = (
        (path / mercure_names.LOCK).exists()
        or (path / mercure_names.PROCESSING).exists()
        or not (path / mercure_names.TASKFILE).exists()
    )
    return folder_status


def is_patient_complete(folder: str, pending_studies: Dict[str, float]) -> bool:
    """
    Returns true if the patient in the given folder is ready for processing,
    i.e. if the completeness criteria of the triggered rule has been met
    """
    try:
        logger.debug(f"Checking completeness of patient {folder}, with pending studies: {pending_studies}")
        # Read stored task file to determine completeness criteria

        with open(Path(folder) / mercure_names.TASKFILE, "r") as json_file:
            task: TaskHasPatient = TaskHasPatient(**json.load(json_file))

        if task.patient.complete_force is True:
            return True
        if (Path(folder) / mercure_names.FORCE_COMPLETE).exists():
            task.patient.complete_force = True
            with open(Path(folder) / mercure_names.TASKFILE, "w") as json_file:
                json.dump(task.dict(), json_file)
            return True

        patient = task.patient

        # Check if processing of the patient has been enforced (e.g., via UI selection)
        if not patient.complete_trigger:
            logger.error(f"Missing trigger condition in task file in patient folder {folder}", task.id)
            return False

        complete_trigger: PatientTriggerCondition = patient.complete_trigger
        complete_required_modalities = patient.get("complete_required_modalities", "")
        complete_required_studies = patient.get("complete_required_studies", "")
        complete_required_series = patient.get("complete_required_series", "")

        # Check for trigger condition
        if complete_trigger == "timeout":
            return check_patient_timeout(task, pending_studies)
        elif complete_trigger == "received_modalities":
            return check_patient_modalities(task, complete_required_modalities)
        elif complete_trigger == "received_studies":
            return check_patient_studies(task, complete_required_studies)
        elif complete_trigger == "received_series":
            return check_patient_series(task, complete_required_series)
        else:
            logger.error(f"Invalid trigger condition in task file in patient folder {folder}", task.id)
            return False
    except Exception:
        logger.exception(f"Invalid task file in patient folder {folder}")
        return False


def check_patient_timeout(task: TaskHasPatient, pending_studies: Dict[str, float]) -> bool:
    """
    Checks if the duration since the last study of the patient was received exceeds the patient completion timeout
    """
    logger.debug("Checking patient timeout")
    patient = task.patient
    last_received_string = patient.last_receive_time
    logger.debug(f"Last received time: {last_received_string}, now is: {datetime.now()}")
    if not last_received_string:
        return False

    last_receive_time = datetime.strptime(last_received_string, "%Y-%m-%d %H:%M:%S")
    if datetime.now() > last_receive_time + timedelta(seconds=config.mercure.patient_complete_trigger):
        # Check if there is a pending study for this patient in studies_folder
        # If so, we need to wait for it to complete before we can complete the patient
        for study_folder in os.listdir(config.mercure.studies_folder):
            study_path = Path(config.mercure.studies_folder) / study_folder
            if not study_path.is_dir():
                continue
            try:
                with open(study_path / mercure_names.TASKFILE, "r") as json_file:
                    study_task = Task(**json.load(json_file))
                    if study_task.info.mrn == patient.patient_id:
                        logger.debug(f"Timeout met, but found a pending study in studies folder for patient {patient.patient_id}")
                        return False
            except Exception:
                # If we can't read the task file, skip this folder
                continue
        logger.debug("Timeout met.")
        return True
    else:
        logger.debug("Timeout not met.")
        return False


def check_force_patient_timeout(folder: Path) -> bool:
    """
    Checks if the duration since the creation of the patient exceeds the force patient completion timeout
    """
    try:
        logger.debug("Checking force patient timeout")

        with open(folder / mercure_names.TASKFILE, "r") as json_file:
            task: TaskHasPatient = TaskHasPatient(**json.load(json_file))

        patient = task.patient
        creation_string = patient.creation_time
        if not creation_string:
            logger.error(f"Missing creation time in task file in patient folder {folder}", task.id)
            return False
        logger.debug(f"Creation time: {creation_string}, now is: {datetime.now()}")

        creation_time = datetime.strptime(creation_string, "%Y-%m-%d %H:%M:%S")
        if datetime.now() > creation_time + timedelta(seconds=config.mercure.patient_forcecomplete_trigger):
            logger.info(f"Force timeout met for patient {folder}")
            if not patient.complete_force_action or patient.complete_force_action == "ignore":
                return True
            elif patient.complete_force_action == "proceed":
                logger.info(f"Forcing patient completion for patient {folder}")
                (folder / mercure_names.FORCE_COMPLETE).touch()
            elif patient.complete_force_action == "discard":
                logger.info(f"Moving folder to discard: {folder.name}")
                lock_file = Path(folder / mercure_names.LOCK)
                try:
                    lock = helper.FileLock(lock_file)
                except Exception:
                    logger.error(f"Unable to lock patient for removal {lock_file}")
                    return False
                if not move_patient_folder(task.id, folder.name, "DISCARD"):
                    logger.error(f"Error during moving patient to discard folder {patient}", task.id)
                    return False
                if not remove_patient_folder(None, folder.name, lock):
                    logger.error(f"Unable to delete patient folder {lock_file}")
                    return False
        else:
            logger.debug("Force timeout not met.")
        return True

    except Exception:
        logger.error(f"Could not check force patient timeout for patient {folder}")
        return False


def check_patient_modalities(task: TaskHasPatient, required_modalities: str) -> bool:
    """
    Checks if all modalities required for patient completion have been received
    """
    received_modalities = []

    # Fetch the list of received modalities from the task file
    if (task.patient.received_modalities) and (isinstance(task.patient.received_modalities, list)):
        received_modalities = task.patient.received_modalities

    # Check if the completion criteria is fulfilled
    return rule_evaluation.parse_completion_series(task.id, required_modalities, received_modalities)


def check_patient_studies(task: TaskHasPatient, required_studies: str) -> bool:
    """
    Checks if all studies required for patient completion have been received
    """
    received_studies = []

    # Fetch the list of received study descriptions from the task file
    if (task.patient.received_studies) and (isinstance(task.patient.received_studies, list)):
        received_studies = [study.modality for study in task.patient.received_studies]

    # Check if the completion criteria is fulfilled
    return rule_evaluation.parse_completion_series(task.id, required_studies, received_studies)


def check_patient_series(task: TaskHasPatient, required_series: str) -> bool:
    """
    Checks if all series required for patient completion have been received
    """
    received_series = []

    # Fetch the list of received series descriptions from the task file
    if (task.patient.received_series) and (isinstance(task.patient.received_series, list)):
        received_series = task.patient.received_series

    # Check if the completion criteria is fulfilled
    return rule_evaluation.parse_completion_series(task.id, required_series, received_series)


@log_helpers.clear_task_decorator
def route_patient(patient) -> bool:
    """
    Processes the patient in the folder 'patient'. Loads the task file and delegates the action to helper functions
    """
    logger.debug(f"Route_patient {patient}")
    patient_folder = config.mercure.patients_folder + "/" + patient
    if is_patient_locked(patient_folder):
        # If the patient folder has been locked in the meantime, then skip and proceed with the next one
        return True

    # Create lock file in the patient folder and prevent other instances from working on this patient
    lock_file = Path(patient_folder + "/" + patient + mercure_names.LOCK)
    if lock_file.exists():
        return True
    try:
        lock = helper.FileLock(lock_file)
    except Exception:
        # Can't create lock file, so something must be seriously wrong
        try:
            task = Task.from_file(Path(patient_folder) / mercure_names.TASKFILE)
            logger.error(f"Unable to create patient lock file {lock_file}", task.id)
        except Exception:
            logger.error(f"Unable to create patient lock file {lock_file}", None)
        return False

    try:
        # Read stored task file to determine completeness criteria
        task = Task.from_file(Path(patient_folder) / mercure_names.TASKFILE)
    except Exception:
        try:
            with open(Path(patient_folder) / mercure_names.TASKFILE, "r") as json_file:
                logger.error(
                    f"Invalid task file in patient folder {patient_folder}", json.load(json_file)["id"]
                )
        except Exception:
            logger.error(f"Invalid task file in patient folder {patient_folder}", None)
        return False

    logger.setTask(task.id)
    action_result = True
    info: TaskInfo = task.info
    action = info.get("action", "")

    if not action:
        logger.error(f"Missing action in patient folder {patient_folder}", task.id)
        return False

    if action == mercure_actions.NOTIFICATION:
        action_result = push_patientlevel_notification(patient, task)
    elif action == mercure_actions.ROUTE:
        action_result = push_patientlevel_dispatch(patient, task)
    elif action == mercure_actions.PROCESS or action == mercure_actions.BOTH:
        action_result = push_patientlevel_processing(patient, task)
    else:
        # This point should not be reached (discard actions should be handled on the series level)
        logger.error(f"Invalid task action in patient folder {patient_folder}", task.id)
        return False

    if not action_result:
        logger.error(f"Error during processing of patient {patient}", task.id)
        return False

    if not remove_patient_folder(task.id, patient, lock):
        logger.error(f"Error removing folder of patient {patient}", task.id)
        return False
    return True


def push_patientlevel_dispatch(patient: str, task: Task) -> bool:
    """
    Pushes the patient folder to the dispatcher, including the generated task file containing the destination information
    """
    trigger_patientlevel_notification(patient, task, mercure_events.RECEIVED)
    return move_patient_folder(task.id, patient, "OUTGOING")


def push_patientlevel_processing(patient: str, task: Task) -> bool:
    """
    Pushes the patient folder to the processor, including the generated task file containing the processing instructions
    """
    trigger_patientlevel_notification(patient, task, mercure_events.RECEIVED)
    return move_patient_folder(task.id, patient, "PROCESSING")


def push_patientlevel_notification(patient: str, task: Task) -> bool:
    """
    Executes the patient-level reception notification
    """
    trigger_patientlevel_notification(patient, task, mercure_events.RECEIVED)
    trigger_patientlevel_notification(patient, task, mercure_events.COMPLETED)
    move_patient_folder(task.id, patient, "SUCCESS")
    return True


def push_patientlevel_error(patient: str) -> None:
    """
    Pushes the patient folder to the error folder after unsuccessful routing
    """
    patient_folder = config.mercure.patients_folder + "/" + patient
    lock_file = Path(patient_folder + "/" + patient + mercure_names.LOCK)
    if lock_file.exists():
        # Patient normally shouldn't be locked at this point, but since it is, just exit and wait.
        # Might require manual intervention if a former process terminated without removing the lock file
        return
    try:
        lock = helper.FileLock(lock_file)
    except Exception:
        # Can't create lock file, so something must be seriously wrong
        logger.error(f"Unable to lock patient for removal {lock_file}")
        return
    if not move_patient_folder(None, patient, "ERROR"):
        # At this point, we can only wait for manual intervention
        logger.error(f"Unable to move patient to ERROR folder {lock_file}")
        return
    if not remove_patient_folder(None, patient, lock):
        logger.error(f"Unable to delete patient folder {lock_file}")
        return


def move_patient_folder(task_id: Union[str, None], patient: str, destination: str) -> bool:
    """
    Moves the patient subfolder to the specified destination with proper locking of the folders
    """
    logger.debug(f"Move_patient_folder {patient} to {destination}")
    source_folder = config.mercure.patients_folder + "/" + patient
    destination_folder = None
    if destination == "PROCESSING":
        destination_folder = config.mercure.processing_folder
    elif destination == "SUCCESS":
        destination_folder = config.mercure.success_folder
    elif destination == "ERROR":
        destination_folder = config.mercure.error_folder
    elif destination == "OUTGOING":
        destination_folder = config.mercure.outgoing_folder
    elif destination == "DISCARD":
        destination_folder = config.mercure.discard_folder
    else:
        logger.error(f"Unknown destination {destination} requested for {patient}", task_id)
        return False

    if task_id is None:
        # Create unique name of destination folder
        destination_folder += "/" + str(uuid.uuid1())
    else:
        # If a task ID exists, name the folder by it to ensure that the files can be found again.
        destination_folder += "/" + str(task_id)

    # Create the destination folder and validate that is has been created
    try:
        os.mkdir(destination_folder)
    except Exception:
        logger.error(f"Unable to create patient destination folder {destination_folder}", task_id)
        return False

    if not Path(destination_folder).exists():
        logger.error(f"Creating patient destination folder not possible {destination_folder}", task_id)
        return False

    # Create lock file in destination folder (to prevent any other module to work on the folder). Note that
    # the source folder has already been locked in the parent function.
    lock_file = Path(destination_folder) / mercure_names.LOCK
    try:
        lock = helper.FileLock(lock_file)
    except Exception:
        # Can't create lock file, so something must be seriously wrong
        logger.error(f"Unable to create lock file {destination_folder}/{mercure_names.LOCK}", task_id)
        return False

    # Move all files except the lock file
    for entry in list(os.scandir(source_folder)):
        # Move all files but exclude the lock file in the source folder
        if not entry.name.endswith(mercure_names.LOCK):
            try:
                shutil.move(source_folder + "/" + entry.name, destination_folder + "/" + entry.name)
            except Exception:
                logger.error(
                    f"Problem while pushing file {entry} from {source_folder} to {destination_folder}", task_id
                )

    # Remove the lock file in the target folder. Would happen automatically when leaving the function,
    # but better to do explicitly with error handling
    try:
        lock.free()
    except Exception:
        # Can't delete lock file, so something must be seriously wrong
        logger.error(f"Unable to remove lock file {lock_file}", task_id)
        return False

    return True


def remove_patient_folder(task_id: Union[str, None], patient: str, lock: helper.FileLock) -> bool:
    """
    Removes a patient folder containing nothing but the lock file (called during cleanup after all files have
    been moved somewhere else already)
    """
    patient_folder = config.mercure.patients_folder + "/" + patient
    # Remove the lock file
    try:
        lock.free()
    except Exception:
        # Can't delete lock file, so something must be seriously wrong
        logger.error(f"Unable to remove lock file while removing patient folder {patient}", task_id)
        return False
    # Remove the empty patient folder
    try:
        shutil.rmtree(patient_folder)
    except Exception:
        logger.error(f"Unable to delete patient folder {patient_folder}", task_id)
    return True


def trigger_patientlevel_notification(patient: str, task: Task, event: mercure_events) -> bool:
    # Check if the applied_rule is available
    current_rule = task.info.applied_rule
    if not current_rule:
        logger.error(f"Missing applied_rule in task file in patient {patient}", task.id)
        return False
    notification.trigger_notification_for_rule(current_rule, task.id, event, task=task)
    return True
