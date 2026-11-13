"""
test_micsi_archive.py
=====================
The MICSI archive (bookkeeping/micsi_query.py find_task, the Queue -> Archive tab with its
patient / study / series grouping) against a real PostgreSQL (pg0-embedded).

Rows are written by the real bookkeeper endpoints in the order the router writes them; the
helpers and database fixture are upstream's, from test_archive_accession_search.py.

Regression: a series-level job is a top-level task with no data (the series registration)
and a child that carries the job's info. The series view only looked at the top-level row,
so series jobs never appeared in it, and the grouped view never listed them at all. A
search also failed outright: the search filter reads child_tasks, which was never joined.
"""

from typing import Any, Dict

import pytest

from tests.test_archive_accession_search import (  # noqa: F401  (pg0_url, bookkeeper_db are fixtures)
    MRN,
    bookkeeper_db,
    call,
    pg0_url,
    route_series_level,
    route_study_level,
)

pytest.importorskip("pg0")
pytest.importorskip("databases")

pytestmark = pytest.mark.asyncio


async def archive(group_by: str = "", search: str = "") -> Dict[str, Any]:
    """What the MICSI Archive tab requests: /api/find-tasks-micsi -> micsi_query.find_task()."""
    import bookkeeping.micsi_query as micsi_query

    return await call(micsi_query.find_task, path="/find_task", query={
        "draw": "1", "start": "0", "length": "50", "search[value]": search,
        "order[0][column]": "6", "order[0][dir]": "desc", "group_by": group_by,
    })


def rows(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    assert payload["recordsTotal"] == len(payload["data"]), payload
    return {row["task_id"]: row for row in payload["data"]}


async def test_series_job_in_series_view(bookkeeper_db):
    sent = await route_series_level(bookkeeper_db, "1.2.9.1", "1.2.9", "ACC1")
    shown = rows(await archive("series"))
    assert list(shown) == [sent.parent_id]
    row = shown[sent.parent_id]
    assert row["Scope"] == "SERIES"
    assert row["MRN"] == MRN
    assert row["Rule"] == "route_series"  # from the series job (child), not the empty registration


async def test_series_job_in_grouped_view(bookkeeper_db):
    sent = await route_series_level(bookkeeper_db, "1.2.9.1", "1.2.9", "ACC1")
    assert list(rows(await archive(""))) == [sent.parent_id]


async def test_study_job_listed_once(bookkeeper_db):
    """A series buffered into a study job is not a series job of its own: the grouped view
    shows the study job only, and the study view shows it as a study."""
    study = await route_study_level(bookkeeper_db, "1.2.8.1", "1.2.8", "ACC2")
    assert list(rows(await archive(""))) == [study.job_id]
    assert list(rows(await archive("study"))) == [study.job_id]
    assert rows(await archive("study"))[study.job_id]["Rule"] == "route_study"


async def test_series_and_study_jobs_together(bookkeeper_db):
    series = await route_series_level(bookkeeper_db, "1.2.9.1", "1.2.9", "ACC1")
    study = await route_study_level(bookkeeper_db, "1.2.8.1", "1.2.8", "ACC2")
    assert set(rows(await archive(""))) == {series.parent_id, study.job_id}
    assert series.parent_id in rows(await archive("series"))
    assert series.parent_id not in rows(await archive("study"))


@pytest.mark.parametrize("group_by", ["", "series", "study", "patient"])
async def test_search_runs_in_every_view(bookkeeper_db, group_by):
    """The search filter reads child_tasks; it used to fail with 'missing FROM-clause entry'."""
    series = await route_series_level(bookkeeper_db, "1.2.9.1", "1.2.9", "ACC1")
    found = rows(await archive(group_by, search=MRN))
    if group_by in ("", "series"):
        assert list(found) == [series.parent_id]
    else:
        assert found == {}


async def test_search_by_rule_and_no_match(bookkeeper_db):
    series = await route_series_level(bookkeeper_db, "1.2.9.1", "1.2.9", "ACC1")
    assert list(rows(await archive("series", search="route_series"))) == [series.parent_id]
    assert rows(await archive("series", search="no-such-patient")) == {}
