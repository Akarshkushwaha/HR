"""Optional verification using the original kit, kept outside the public repository.

Set CANDIDATE_KIT_DIR to the directory containing sample_seed.py and openapi.yaml.
Expected analytics are calculated independently of application helpers/pipelines.
"""
import os
import re
import subprocess
import sys
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pytest
import yaml
from bson import json_util

from app import main as m


PRESENT = {"PRESENT", "WFH", "ON_DUTY"}


def rounded(value, places=2):
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def normalize(value):
    if isinstance(value, datetime):
        return int(value.replace(tzinfo=timezone.utc).timestamp() * 1000)
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in value.items() if k != "_id"}
    if isinstance(value, list):
        return [normalize(v) for v in value]
    return value


@pytest.fixture
def kit_data(api):
    default = Path(__file__).resolve().parents[1] / "SDT_candidate_kit" / "candidate_kit"
    kit = Path(os.getenv("CANDIDATE_KIT_DIR", str(default))).resolve()
    if not (kit / "sample_seed.py").is_file():
        if os.getenv("CANDIDATE_KIT_DIR"):
            pytest.fail("CANDIDATE_KIT_DIR must contain sample_seed.py")
        pytest.skip("Original candidate kit not available; set CANDIDATE_KIT_DIR")
    # Use their loader unchanged, safely scoped to this fixture's disposable database.
    result = subprocess.run([sys.executable, str(kit / "sample_seed.py")],
        env={**os.environ, "MONGO_URI": os.getenv("TEST_MONGO_URI", "mongodb://localhost:27018"),
             "MONGO_DB": m.db.name}, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    employees = json_util.loads((kit / "sample_data" / "employees.json").read_text())
    logs = json_util.loads((kit / "sample_data" / "attendance_logs.json").read_text())
    assert m.db.employees.count_documents({}) == len(employees) == 6
    assert m.db.attendance_logs.count_documents({}) == len(logs) == 9
    contract = yaml.safe_load((kit / "openapi.yaml").read_text(encoding="utf-8"))
    return employees, logs, contract


def assert_schema(value, schema, contract):
    """Check the response fields/types and constraints used by the supplied contract."""
    if "$ref" in schema:
        target = contract
        for part in schema["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        return assert_schema(value, target, contract)
    if value is None and schema.get("nullable"):
        return
    for part in schema.get("allOf", []):
        assert_schema(value, part, contract)
    kind = schema.get("type")
    if kind == "object":
        assert isinstance(value, dict)
        assert set(schema.get("required", [])) <= value.keys()
        for key, child in schema.get("properties", {}).items():
            if key in value:
                assert_schema(value[key], child, contract)
    elif kind == "array":
        assert isinstance(value, list)
        for item in value:
            assert_schema(item, schema["items"], contract)
    elif kind == "integer":
        assert type(value) is int
    elif kind == "number":
        assert type(value) in (int, float)
    elif kind == "boolean":
        assert type(value) is bool
    elif kind == "string":
        assert isinstance(value, str)
        if schema.get("format") == "date":
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)
            date.fromisoformat(value)
    if "enum" in schema:
        assert value in schema["enum"]
    if "minimum" in schema:
        assert value >= schema["minimum"]
    if "maximum" in schema:
        assert value <= schema["maximum"]


def response(api, path, params, expected, contract, schema_name):
    result = api.get(path, params=params)
    assert result.status_code == 200, result.text
    body = result.json()
    assert body == expected
    assert_schema(body, {"$ref": "#/components/schemas/" + schema_name}, contract)
    return body


def presence(log, weekdays=False):
    if log["status"] not in PRESENT or (weekdays and date.fromisoformat(log["date"]).weekday() >= 5):
        return Decimal(0)
    return Decimal("0.5") if log.get("half_day", False) else Decimal(1)


def totals(logs):
    return {"present_days": float(sum(presence(log, True) for log in logs)),
            "leave_days": sum(log["status"] == "LEAVE" for log in logs),
            "late_count": sum(log.get("late_minutes", 0) > 0 for log in logs),
            "total_late_minutes": sum(log.get("late_minutes", 0) for log in logs),
            "total_overtime_minutes": sum(log.get("overtime_minutes", 0) for log in logs)}


def test_original_samples_list_and_analytics(api, kit_data):
    employees, logs, contract = kit_data
    normalized = [normalize(e) for e in sorted(employees, key=lambda e: e["emp_code"])]
    response(api, "/employees", {}, {"items": normalized, "total": 6, "page": 1, "page_size": 20}, contract, "EmployeePage")
    defaults = {"punch_in": None, "punch_out": None, "work_hours": None, "late_minutes": 0,
                "overtime_minutes": 0, "half_day": False, "history": []}
    ordered = sorted(logs, key=lambda log: (-date.fromisoformat(log["date"]).toordinal(), log["emp_code"]))
    response(api, "/attendance", {}, {"items": [normalize({**defaults, **log}) for log in ordered],
        "total": 9, "page": 1, "page_size": 20}, contract, "AttendancePage")
    month = "2026-07"
    first = date.fromisoformat(month + "-01")
    last = first.replace(day=monthrange(first.year, first.month)[1])
    monthly_logs = [log for log in logs if first.isoformat() <= log["date"] <= last.isoformat()]
    for employee in employees:
        selected = [log for log in monthly_logs if log["emp_code"] == employee["emp_code"]]
        weekdays = sum((first + timedelta(days=n)).weekday() < 5 and
            (first + timedelta(days=n)).isoformat() >= employee["joined_on"] for n in range(last.day))
        expected = {"emp_code": employee["emp_code"], "month": month, "working_days": weekdays, **totals(selected)}
        expected["attendance_pct"] = rounded(Decimal(str(expected["present_days"])) * 100 / weekdays) if weekdays else None
        response(api, f"/analytics/employees/{employee['emp_code']}/monthly", {"month": month}, expected, contract, "EmployeeMonthly")
    departments = sorted({e["department"] for e in employees})
    summaries = []
    for department in departments:
        members = [e for e in employees if e["department"] == department and e["joined_on"] <= last.isoformat()]
        codes = {e["emp_code"] for e in members}
        selected = [log for log in monthly_logs if log["emp_code"] in codes]
        hours = [Decimal(str(log["work_hours"])) for log in selected if log["status"] in PRESENT and log.get("work_hours") is not None]
        counts = totals(selected)
        if members:
            summaries.append({"department": department, "headcount": len(members), "present_days": counts["present_days"],
                "avg_work_hours": rounded(sum(hours) / len(hours)) if hours else None,
                "late_count": counts["late_count"], "total_late_minutes": counts["total_late_minutes"],
                "leave_count": counts["leave_days"], "on_duty_count": sum(log["status"] == "ON_DUTY" for log in selected)})
    response(api, "/analytics/departments/summary", {"month": month}, {"month": month, "items": summaries}, contract, "DepartmentSummary")
    for department in [None, *departments]:
        leaders = []
        for e in employees:
            stats = totals([log for log in monthly_logs if log["emp_code"] == e["emp_code"]])
            if (department is None or e["department"] == department) and stats["total_late_minutes"] > 0:
                leaders.append({"emp_code": e["emp_code"], "name": e["name"], "department": e["department"],
                    "total_late_minutes": stats["total_late_minutes"], "late_count": stats["late_count"]})
        leaders.sort(key=lambda row: (-row["total_late_minutes"], row["emp_code"]))
        for row in leaders:
            row["rank"] = 1 + sum(other["total_late_minutes"] > row["total_late_minutes"] for other in leaders)
        for limit in [1, 2, 10]:
            params = {"month": month, "limit": limit}
            if department is not None:
                params["department"] = department
            response(api, "/analytics/leaderboard/late", params,
                {"month": month, "items": [row for row in leaders if row["rank"] <= limit]}, contract, "Leaderboard")
    for department in departments:
        codes = {e["emp_code"] for e in employees if e["department"] == department}
        rows, rates = [], []
        for n in range(last.day):
            day = first + timedelta(days=n)
            selected = [log for log in monthly_logs if log["date"] == day.isoformat() and log["emp_code"] in codes]
            headcount = sum(e["emp_code"] in codes and e["joined_on"] <= day.isoformat() for e in employees)
            count = sum(presence(log) for log in selected)
            rate = rounded(count / headcount, 4) if headcount and day.weekday() < 5 else None
            rates.append(rate)
            window = [Decimal(str(r)) for r in rates[max(0, n - 6):n + 1] if r is not None]
            rows.append({"date": day.isoformat(), "is_working_day": day.weekday() < 5,
                "headcount": headcount, "present_count": float(count), "late_count": totals(selected)["late_count"],
                "attendance_rate": rate, "moving_avg_7d": rounded(sum(window) / len(window), 4) if window else None})
        response(api, f"/analytics/departments/{department}/trend", {"from": first.isoformat(), "to": last.isoformat()},
            {"department": department, "items": rows}, contract, "Trend")


def test_original_samples_writes_and_contract_routes(api, kit_data):
    employees, logs, contract = kit_data
    routes = {(route.path, method.lower()) for route in m.app.routes for method in getattr(route, "methods", [])}
    assert all((path, method) in routes for path, methods in contract["paths"].items() for method in methods)
    open_log = next(log for log in logs if log["status"] in PRESENT and log.get("punch_out") is None)
    employee = next(e for e in employees if e["emp_code"] == open_log["emp_code"])
    pin = open_log["punch_in"].replace(tzinfo=timezone.utc)
    out = pin + timedelta(hours=9)
    result = api.post("/attendance/punch-out", json={"emp_code": employee["emp_code"], "punched_at": normalize(out)})
    assert result.status_code == 200, result.text
    assert result.json()["work_hours"] == 9.0 and result.json()["history"] == []
    assert_schema(result.json(), {"$ref": "#/components/schemas/AttendanceRecord"}, contract)
    legacy = next(log for log in logs if "history" not in log)
    result = api.patch(f"/attendance/{legacy['emp_code']}/{legacy['date']}",
        json={"status": "LEAVE", "reason": "approved correction", "regularized_by": "tester"})
    assert result.status_code == 200, result.text
    assert result.json()["punch_in"] is None and len(result.json()["history"]) == 1
    assert_schema(result.json(), {"$ref": "#/components/schemas/AttendanceRecord"}, contract)
