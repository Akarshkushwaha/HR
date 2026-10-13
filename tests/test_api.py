"""Contract tests against real MongoDB; all fixtures are synthetic, not kit dumps."""
import json
import os
import re
import socket
import subprocess
import sys
import urllib.request
import time as clock
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from threading import Barrier
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main as m


def ms(local):
    return int(datetime.fromisoformat(local).replace(tzinfo=m.IST).timestamp() * 1000)


def employee(code="EMP1001", department="Alpha", joined="2026-07-01", **kwargs):
    return {"emp_code": code, "name": "Test " + code, "email": code + "@example.test",
            "department": department, "joined_on": joined, **kwargs}


def add_employee(api, **kwargs):
    response = api.post("/employees", json=employee(**kwargs))
    assert response.status_code == 201, response.text
    return response.json()


def punch(api, local="2026-07-06T09:30:00", code="EMP1001", status="PRESENT"):
    return api.post("/attendance/punch-in", json={"emp_code": code, "punched_at": ms(local), "status": status})


@pytest.mark.parametrize("value", [1783312500, "1783312500000", 1783312500000.0, True, None, 4102444800001])
def test_strict_timestamps(api, value):
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP1001", "punched_at": value}).status_code == 422
    assert api.patch("/attendance/EMP1001/2026-07-06", json={"punch_in": value,
                     "reason": "clock adjustment", "regularized_by": "tester"}).status_code == 422


@pytest.mark.parametrize("fields", [
    {"emp_code": "wrong"}, {"name": ""}, {"email": "bad"}, {"department": ""},
    {"joined_on": "2026-02-30"}, {"joined_on": "20260701"},
    {"shift_start": "24:00"}, {"shift_start": "18:30"},
])
def test_employee_validation(api, fields):
    assert api.post("/employees", json={**employee(), **fields}).status_code == 422


def test_creation_pagination_and_defaults(api):
    one = add_employee(api, code="EMP1002")
    add_employee(api, code="EMP1001")
    add_employee(api, code="EMP1003", department="Beta")
    assert isinstance(one["created_at"], int) and "_id" not in one
    assert api.post("/employees", json=employee()).status_code == 409
    page = api.get("/employees", params={"department": "Alpha", "page": 2, "page_size": 1}).json()
    assert page["total"] == 2 and page["items"][0]["emp_code"] == "EMP1002"
    assert api.get("/employees", params={"page": 0}).status_code == 422
    assert api.get("/attendance", params={"page_size": 101}).status_code == 422
    assert api.get("/attendance", params={"date_from": "2026-07-03", "date_to": "2026-07-01"}).status_code == 422
    assert api.get("/attendance", params={"date_from": "2026-02-30"}).status_code == 422
    assert api.get("/attendance", params={"date_from": ""}).status_code == 422
    assert api.get("/admin/explain/attendance_list", params={"date_to": ""}).status_code == 422
    assert api.get("/attendance", params={"status": "OTHER"}).status_code == 422
    assert api.get("/health").json() == {"status": "ok"}


@pytest.mark.parametrize("local,expected", [
    ("2026-07-06T09:40:00.900", 0), ("2026-07-06T09:40:01", 10),
    ("2026-07-06T10:15:59", 45), ("2026-07-06T08:00:00", 0),
])
def test_lateness_and_truncation(api, local, expected):
    add_employee(api)
    response = punch(api, local)
    assert response.status_code == 201
    doc = response.json()
    assert doc["late_minutes"] == expected
    assert doc["punch_in"] % 1000 == 0
    assert doc["history"] == [] and "id" not in doc and "_id" not in doc


@pytest.mark.parametrize("out,hours,overtime,half", [
    ("2026-07-06T13:59:42", 4.5, 0, False),
    ("2026-07-06T13:59:41", 4.49, 0, True),
    ("2026-07-06T18:59:59", 9.5, 0, False),
    ("2026-07-06T19:00:00", 9.5, 30, False),
    ("2026-07-06T09:34:30", 0.08, 0, True),
])
def test_work_rounding_overtime_and_halfday(api, out, hours, overtime, half):
    add_employee(api)
    assert punch(api).status_code == 201
    response = api.post("/attendance/punch-out", json={"emp_code": "EMP1001", "punched_at": ms(out)})
    assert response.status_code == 200, response.text
    doc = response.json()
    assert (doc["work_hours"], doc["overtime_minutes"], doc["half_day"]) == (hours, overtime, half)
    assert doc["history"] == []


def test_overnight_and_punchout_rules(api):
    add_employee(api, shift_start="22:00", shift_end="06:00")
    doc = punch(api, "2026-07-07T02:00:00").json()
    assert doc["date"] == "2026-07-06" and doc["late_minutes"] == 240
    assert punch(api, "2026-07-06T21:59:00").status_code == 409
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP1001", "punched_at": doc["punch_in"]}).status_code == 422
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP1001", "punched_at": ms("2026-07-08T02:00:01")}).status_code == 422
    response = api.post("/attendance/punch-out", json={"emp_code": "EMP1001", "punched_at": ms("2026-07-07T06:40:00")})
    assert response.status_code == 200, response.text
    assert response.json()["overtime_minutes"] == 40
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP1001", "punched_at": ms("2026-07-07T06:50:00")}).status_code == 409
    assert punch(api, "2026-07-07T06:00:00").json()["date"] == "2026-07-07"
    assert punch(api, code="EMP9999").status_code == 404
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP9999"}).status_code == 404
    add_employee(api, code="EMP1002")
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP1002"}).status_code == 404


def test_corrections_and_nested_history(api):
    add_employee(api)
    initial = punch(api, "2026-07-06T10:00:00").json()
    metadata = {"reason": "clock adjustment", "regularized_by": "tester"}
    path = "/attendance/EMP1001/2026-07-06"
    r = api.patch(path, json={**metadata, "punch_in": ms("2026-07-06T09:30:00"), "work_hours": 99})
    assert r.status_code == 200, r.text
    corrected = r.json()
    change = corrected["history"][0]["changes"]
    assert change["punch_in"] == {"from": initial["punch_in"], "to": ms("2026-07-06T09:30:00")}
    assert change["late_minutes"] == {"from": 30, "to": 0}
    assert "work_hours" not in change and corrected["work_hours"] is None
    assert api.patch(path, json=metadata).status_code == 422
    assert api.patch(path, json={**metadata, "punch_in": ms("2026-07-07T09:30:00")}).status_code == 422
    assert api.patch(path, json={**metadata, "punch_out": ms("2026-07-06T09:30:00")}).status_code == 422
    assert api.patch(path, json={**metadata, "status": "LEAVE", "punch_in": initial["punch_in"]}).status_code == 422
    leave = api.patch(path, json={**metadata, "status": "LEAVE"}).json()
    assert leave["punch_in"] is None and len(leave["history"]) == 2
    assert api.patch(path, json={**metadata, "status": "WFH"}).status_code == 422
    restored = api.patch(path, json={**metadata, "status": "WFH", "punch_in": ms("2026-07-06T09:30:00"),
                                     "punch_out": ms("2026-07-06T12:30:00")}).json()
    assert restored["half_day"] is True and len(restored["history"]) == 3
    assert api.patch("/attendance/EMP9999/2026-07-06", json=metadata).status_code == 404
    assert api.patch("/attendance/EMP1001/2026-07-07", json=metadata).status_code == 404


def test_concurrent_writes(api, monkeypatch):
    with ThreadPoolExecutor(max_workers=8) as pool:
        created = list(pool.map(lambda _: api.post("/employees", json=employee()).status_code, range(8)))
        assert sorted(created) == [201] + [409] * 7
        punched = list(pool.map(lambda _: punch(api).status_code, range(8)))
        assert sorted(punched) == [201] + [409] * 7
        closed = list(pool.map(lambda _: api.post("/attendance/punch-out", json={"emp_code": "EMP1001",
                           "punched_at": ms("2026-07-06T18:30:00")}).status_code, range(8)))
        assert sorted(closed) == [200] + [409] * 7
        gate = Barrier(2)
        original = m.snapshot_filter
        def gated(doc):
            gate.wait(timeout=5)
            return original(doc)
        monkeypatch.setattr(m, "snapshot_filter", gated)
        def edit(hour):
            return api.patch("/attendance/EMP1001/2026-07-06", json={"punch_out": ms(f"2026-07-06T{hour}:30:00"),
                                "reason": "clock adjustment", "regularized_by": "tester"}).status_code
        edited = list(pool.map(edit, [19, 20]))
        assert sorted(edited) == [200, 409]
    assert len(api.get("/attendance").json()["items"][0]["history"]) == 1


def seed_analytics(api):
    add_employee(api, code="EMP1001")
    add_employee(api, code="EMP1002", joined="2026-07-15")
    add_employee(api, code="EMP1003", department="Beta")
    add_employee(api, code="EMP1004", joined="2026-08-01")
    add_employee(api, code="EMP1005", department="Beta")
    add_employee(api, code="EMP1006", department="Future", joined="2026-08-01")
    add_employee(api, code="EMP1007", department="Gamma")
    records = [
        {"emp_code": "EMP1001", "date": "2026-07-06", "status": "PRESENT", "work_hours": 8.125, "late_minutes": 20},
        {"emp_code": "EMP1001", "date": "2026-07-07", "status": "WFH", "work_hours": 3.0, "half_day": True},
        {"emp_code": "EMP1001", "date": "2026-07-11", "status": "ON_DUTY", "work_hours": 5.0, "late_minutes": 30, "overtime_minutes": 45},
        {"emp_code": "EMP1001", "date": "2026-07-08", "status": "LEAVE"},
        {"emp_code": "EMP1003", "date": "2026-07-06", "status": "PRESENT", "work_hours": 7.0, "late_minutes": 50},
        {"emp_code": "EMP1005", "date": "2026-07-06", "status": "PRESENT", "work_hours": None, "late_minutes": 80},
        {"emp_code": "EMP1007", "date": "2026-07-09", "status": "PRESENT", "late_minutes": 10},
        {"emp_code": "EMP9999", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 999},
    ]
    m.db.attendance_logs.insert_many(records)
    return records


def test_monthly_summary_and_leaderboard(api):
    seed_analytics(api)
    monthly = api.get("/analytics/employees/EMP1001/monthly", params={"month": "2026-07"}).json()
    assert monthly == {"emp_code": "EMP1001", "month": "2026-07", "working_days": 23,
        "present_days": 1.5, "leave_days": 1, "late_count": 2, "total_late_minutes": 50,
        "total_overtime_minutes": 45, "attendance_pct": 6.52}
    mid = api.get("/analytics/employees/EMP1002/monthly", params={"month": "2026-07"}).json()
    assert mid["working_days"] == 13 and mid["present_days"] == 0 and mid["attendance_pct"] == 0
    future = api.get("/analytics/employees/EMP1004/monthly", params={"month": "2026-07"}).json()
    assert future["working_days"] == 0 and future["attendance_pct"] is None
    summary = api.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"]
    assert summary[0] == {"department": "Alpha", "headcount": 2, "present_days": 1.5,
        "avg_work_hours": 5.38, "late_count": 2, "total_late_minutes": 50, "leave_count": 1, "on_duty_count": 1}
    assert summary[1]["headcount"] == 2 and summary[1]["avg_work_hours"] == 7.0
    assert summary[2]["department"] == "Gamma" and summary[2]["avg_work_hours"] is None
    assert len(summary) == 3
    tied = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 2}).json()["items"]
    assert [(x["emp_code"], x["rank"]) for x in tied] == [("EMP1005", 1), ("EMP1001", 2), ("EMP1003", 2)]
    all_ranks = api.get("/analytics/leaderboard/late", params={"month": "2026-07"}).json()["items"]
    assert [x["rank"] for x in all_ranks] == [1, 2, 2, 4]
    within = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 1, "department": "Alpha"}).json()["items"]
    assert [(x["emp_code"], x["rank"]) for x in within] == [("EMP1001", 1)]
    assert api.get("/analytics/employees/EMP9999/monthly", params={"month": "2026-07"}).status_code == 404
    assert api.get("/analytics/departments/summary", params={"month": "2026-13"}).status_code == 422
    assert api.get("/analytics/departments/summary", params={"month": "2026-02", "department": "Future"}).json()["items"] == []


def test_trend_gaps_headcount_weekends_and_windows(api):
    seed_analytics(api)
    response = api.get("/analytics/departments/Alpha/trend", params={"from": "2026-07-06", "to": "2026-07-16"})
    assert response.status_code == 200, response.text
    rows = response.json()["items"]
    assert len(rows) == 11
    rates = []
    for i, row in enumerate(rows):
        day = date(2026, 7, 6) + timedelta(days=i)
        expected_present = {"2026-07-06": 1, "2026-07-07": 0.5, "2026-07-11": 1}.get(day.isoformat(), 0)
        headcount = 1 if day < date(2026, 7, 15) else 2
        rate = Decimal(str(expected_present)) / headcount if day.weekday() < 5 else None
        rates.append(rate)
        window = [r for r in rates[max(0, i - 6):i + 1] if r is not None]
        moving = float((sum(window) / len(window)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)) if window else None
        assert row == {"date": day.isoformat(), "is_working_day": day.weekday() < 5, "headcount": headcount,
            "present_count": expected_present, "late_count": 1 if i in [0, 5] else 0,
            "attendance_rate": float(rate) if rate is not None else None, "moving_avg_7d": moving}
    weekend = api.get("/analytics/departments/Alpha/trend", params={"from": "2026-07-11", "to": "2026-07-12"}).json()["items"]
    assert all(r["moving_avg_7d"] is None for r in weekend)
    future = api.get("/analytics/departments/Future/trend", params={"from": "2026-07-01", "to": "2026-07-03"}).json()["items"]
    assert len(future) == 3 and all(r["headcount"] == 0 and r["attendance_rate"] is None for r in future)
    assert api.get("/analytics/departments/Unknown/trend", params={"from": "2026-07-01", "to": "2026-07-03"}).status_code == 404
    assert api.get("/analytics/departments/Alpha/trend", params={"from": "2026-07-03", "to": "2026-07-01"}).status_code == 422
    assert api.get("/analytics/departments/Alpha/trend", params={"from": "2026-01-01", "to": "2026-04-03"}).status_code == 422


def test_list_sort_defaults_and_audit(api):
    seed_analytics(api)
    body = api.get("/attendance", params={"date_from": "2026-07-06", "date_to": "2026-07-07", "page_size": 2}).json()
    assert body["total"] == 5
    assert [(r["date"], r["emp_code"]) for r in body["items"]] == [("2026-07-07", "EMP1001"), ("2026-07-06", "EMP1001")]
    assert body["items"][1]["history"] == [] and body["items"][1]["half_day"] is False


EXPLAINS = [
    ("attendance_list", {}),
    ("attendance_list", {"emp_code": "EMP1001", "status": "PRESENT", "page": 2, "page_size": 5}),
    ("attendance_list", {"status": "PRESENT", "date_from": "2026-07-01", "date_to": "2026-07-31"}),
    ("employee_monthly", {"emp_code": "EMP1001", "month": "2026-07"}),
    ("department_summary", {"month": "2026-07"}),
    ("department_summary", {"month": "2026-07", "department": "Alpha"}),
    ("late_leaderboard", {"month": "2026-07", "limit": 2}),
    ("department_trend", {"department": "Alpha", "from": "2026-07-01", "to": "2026-07-31"}),
]


def check_explains(api):
    for endpoint, params in EXPLAINS:
        response = api.get("/admin/explain/" + endpoint, params=params)
        assert response.status_code == 200, response.text
        raw = response.json()["explain"]
        encoded = json.dumps(raw)
        assert "IXSCAN" in encoded, encoded
        assert "COLLSCAN" not in encoded, encoded
        assert '"collectionScans": 0' in encoded or endpoint == "attendance_list"


def test_explains_and_required_parameters(api):
    seed_analytics(api)
    check_explains(api)
    for endpoint in ["employee_monthly", "department_summary", "late_leaderboard", "department_trend", "other"]:
        assert api.get("/admin/explain/" + endpoint).status_code == 422


def test_health_database_unavailable(monkeypatch):
    class Unavailable:
        def command(self, *args, **kwargs):
            raise m.PyMongoError("unavailable")
    monkeypatch.setattr(m, "db", Unavailable())
    assert TestClient(m.app).get("/health").status_code == 503


def test_calendar_month_boundaries(api):
    add_employee(api, joined="2024-02-15")
    february = api.get("/analytics/employees/EMP1001/monthly", params={"month": "2024-02"}).json()
    assert february["working_days"] == 11
    december = api.get("/analytics/employees/EMP1001/monthly", params={"month": "9999-12"})
    assert december.status_code == 200, december.text
    assert december.json()["working_days"] == 23


def test_default_clock_read_only_fields_and_exact_day_limit(api):
    supplied = {**employee(), "created_at": 100000000000, "_id": "client-id"}
    created = api.post("/employees", json=supplied)
    assert created.status_code == 201 and created.json()["created_at"] != supplied["created_at"]
    assert "_id" not in created.json()
    before = int(datetime.now(timezone.utc).timestamp()) * 1000
    entered = api.post("/attendance/punch-in", json={"emp_code": "EMP1001", "work_hours": 99,
        "late_minutes": 999, "overtime_minutes": 999, "half_day": True, "history": [{"by": "client"}]})
    assert entered.status_code == 201
    doc = entered.json()
    assert before <= doc["punch_in"] <= int(datetime.now(timezone.utc).timestamp()) * 1000
    assert doc["work_hours"] is None and doc["overtime_minutes"] == 0 and doc["half_day"] is False
    assert doc["history"] == []
    closed = api.post("/attendance/punch-out", json={"emp_code": "EMP1001", "punched_at": doc["punch_in"] + 86400000})
    assert closed.status_code == 200 and closed.json()["work_hours"] == 24


def test_most_recent_punchout_does_not_close_an_older_open_record(api):
    add_employee(api)
    assert punch(api, "2026-07-06T09:30:00").status_code == 201
    assert punch(api, "2026-07-07T09:30:00").status_code == 201
    body = {"emp_code": "EMP1001", "punched_at": ms("2026-07-07T18:30:00")}
    assert api.post("/attendance/punch-out", json=body).status_code == 200
    assert api.post("/attendance/punch-out", json=body).status_code == 409
    older = api.get("/attendance", params={"date_from": "2026-07-06", "date_to": "2026-07-06"}).json()["items"][0]
    assert older["punch_out"] is None


def test_readme_workflow(api):
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    bodies = [json.loads(block) for block in re.findall(r"```json\n(.*?)\n\s*```", readme, re.S)]
    assert len(bodies) == 4
    assert api.post("/employees", json=bodies[0]).status_code == 201
    assert api.post("/attendance/punch-in", json=bodies[1]).status_code == 201
    closed = api.post("/attendance/punch-out", json=bodies[2])
    assert closed.status_code == 200, closed.text
    doc = closed.json()
    assert (doc["date"], doc["work_hours"], doc["late_minutes"], doc["overtime_minutes"], doc["half_day"], doc["history"]) == (
        "2026-07-06", 9.0, 0, 0, False, [])
    corrected = api.patch("/attendance/EMP2001/2026-07-06", json=bodies[3])
    assert corrected.status_code == 200
    assert corrected.json()["work_hours"] == 10.0 and corrected.json()["overtime_minutes"] == 60
    assert len(corrected.json()["history"]) == 1


def test_fresh_process_startup_environment_and_preseed(api, startup_dir):
    # A fresh process must read injected environment values, build indexes on seeded
    # collections, and start using only application files from a clean directory.
    tmp_path = startup_dir
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    source = Path(__file__).resolve().parents[1] / "app"
    for name in ["main.py", "__init__.py"]:
        (app_dir / name).write_text((source / name).read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / ".env").write_text("MONGO_URI=mongodb://localhost:9\nMONGO_DB=incorrect_database\n")
    add_employee(api)
    m.db.employees.drop_indexes()
    m.db.attendance_logs.drop_indexes()
    with socket.socket() as port_probe:
        port_probe.bind(("127.0.0.1", 0))
        port = port_probe.getsockname()[1]
    environment = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    environment.update({"MONGO_URI": os.getenv("TEST_MONGO_URI", "mongodb://localhost:27018"), "MONGO_DB": m.db.name})
    started = clock.monotonic()
    process = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)],
                               cwd=tmp_path, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        url = f"http://127.0.0.1:{port}"
        while clock.monotonic() - started < 20:
            try:
                with urllib.request.urlopen(url + "/health", timeout=1) as result:
                    if result.status == 200:
                        break
            except (OSError, TimeoutError):
                assert process.poll() is None, "API exited before readiness"
        else:
            pytest.fail("Fresh API did not become ready within 20 seconds")
        with urllib.request.urlopen(url + "/employees", timeout=2) as result:
            assert json.load(result)["items"][0]["emp_code"] == "EMP1001"
        assert m.db.employees.index_information()["employee_code"]["unique"] is True
        assert m.db.attendance_logs.index_information()["employee_date"]["unique"] is True
    finally:
        process.terminate()
        process.wait(timeout=5)


@pytest.mark.skipif(os.getenv("RUN_SCALE_TEST") != "1", reason="Opt-in 100,000-record check")
def test_100k_index_plans_and_startup(api):
    employees = [{**employee(code=f"EMP{i:04d}"), "shift_start": "09:30", "shift_end": "18:30",
                   "created_at": datetime.now(timezone.utc)} for i in range(1000, 1500)]
    m.db.employees.insert_many(employees)
    for offset in range(200):
        day = (date(2026, 1, 1) + timedelta(days=offset)).isoformat()
        records = [{"emp_code": emp["emp_code"], "date": day, "status": "PRESENT", "work_hours": 8.0,
                    "late_minutes": offset % 11, "overtime_minutes": 0, "half_day": False} for emp in employees]
        m.db.attendance_logs.insert_many(records)
    m.db.employees.drop_indexes()
    m.db.attendance_logs.drop_indexes()
    start = clock.monotonic()
    m.create_indexes()
    assert clock.monotonic() - start < 20
    check_explains(api)
    for path, params in [
        ("/attendance", {}),
        ("/analytics/employees/EMP1001/monthly", {"month": "2026-07"}),
        ("/analytics/departments/summary", {"month": "2026-07"}),
        ("/analytics/leaderboard/late", {"month": "2026-07"}),
        ("/analytics/departments/Alpha/trend", {"from": "2026-07-01", "to": "2026-07-31"}),
    ]:
        start = clock.monotonic()
        response = api.get(path, params=params)
        assert response.status_code == 200, response.text
        elapsed = clock.monotonic() - start
        print(f"{path}: {elapsed:.3f}s on 100,000 records")
        assert elapsed < 10
