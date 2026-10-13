"""Employee Attendance & Analytics API. All application code lives in this file."""
import os
import re
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import date as CalendarDate, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal

from bson import Decimal128, ObjectId
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
Millis = Annotated[int, Field(strict=True, ge=100000000000, le=4102444800000)]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Limit = Annotated[int, Query(ge=1, le=50)]

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"),
                     tz_aware=True, serverSelectionTimeoutMS=2000, connectTimeoutMS=2000)
db = client[os.getenv("MONGO_DB", "attendance_db")]


def create_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="employee_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING),
                               ("emp_code", ASCENDING)], name="department_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)],
                                    unique=True, name="employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_employee")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING),
                                     ("emp_code", ASCENDING)], name="status_date_employee")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)],
                                    name="employee_punch")


@asynccontextmanager
async def lifespan(app):
    # An unavailable database leaves the probe usable (503). Retry on a later probe.
    app.state.indexes_ready = False
    try:
        create_indexes()
        app.state.indexes_ready = True
    except PyMongoError:
        pass
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def database_unavailable(request, exc):
    return JSONResponse(status_code=503, content={"detail": "MongoDB unavailable"})


def invalid(message, location="body"):
    raise RequestValidationError([{"type": "value_error", "loc": (location,),
                                   "msg": message, "input": None}])


def parse_date(value, location="query"):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        invalid("Expected YYYY-MM-DD", location)
    try:
        return CalendarDate.fromisoformat(value)
    except ValueError:
        invalid("Invalid calendar date", location)


def month_bounds(month):
    if not isinstance(month, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        invalid("Expected YYYY-MM", "query")
    first = parse_date(month + "-01")
    return first, first.replace(day=monthrange(first.year, first.month)[1])


def validate_range(start, end, maximum=None):
    if start is not None and end is not None:
        a, b = parse_date(start), parse_date(end)
        if b < a or (maximum and (b - a).days + 1 > maximum):
            invalid("Invalid date range", "query")
    else:
        if start is not None:
            parse_date(start)
        if end is not None:
            parse_date(end)


def instant(milliseconds=None):
    ts = datetime.now(UTC) if milliseconds is None else datetime.fromtimestamp(milliseconds / 1000, UTC)
    return ts.replace(microsecond=0)


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def json_value(value):
    """Convert BSON, including timestamps nested inside audit changes."""
    if isinstance(value, datetime):
        return int((utc(value) - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds() * 1000)
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    if isinstance(value, ObjectId):
        return str(value)  # Explain documents may contain internal BSON identifiers.
    if isinstance(value, dict):
        return {k: json_value(v) for k, v in value.items() if k != "_id"}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    return value


DEFAULTS = {"punch_in": None, "punch_out": None, "work_hours": None,
            "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}


def attendance_json(doc):
    return json_value({**DEFAULTS, **doc})


def attendance_date(ts, employee):
    local = utc(ts).astimezone(IST)
    day = local.date()
    if employee["shift_end"] <= employee["shift_start"] and local.time() < time.fromisoformat(employee["shift_end"]):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_bounds(employee, day):
    d = CalendarDate.fromisoformat(day)
    start = datetime.combine(d, time.fromisoformat(employee["shift_start"]), IST)
    end = datetime.combine(d, time.fromisoformat(employee["shift_end"]), IST)
    if employee["shift_end"] <= employee["shift_start"]:
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in, shift_start):
    seconds = (utc(punch_in) - shift_start).total_seconds()
    return int(seconds // 60) if seconds > 600 else 0


def compute_work_hours(punch_in, punch_out):
    seconds = Decimal(str((utc(punch_out) - utc(punch_in)).total_seconds()))
    return float((seconds / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out, shift_end):
    minutes = int((utc(punch_out) - shift_end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def derived(employee, doc):
    result = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if doc["status"] not in PRESENCE:
        return result
    pin, pout = doc.get("punch_in"), doc.get("punch_out")
    if pin is None:
        invalid("Presence status requires punch_in")
    if attendance_date(pin, employee) != doc["date"]:
        invalid("punch_in must remain on the attendance date")
    start, end = shift_bounds(employee, doc["date"])
    result["late_minutes"] = compute_late_minutes(pin, start)
    if pout is not None:
        seconds = (utc(pout) - utc(pin)).total_seconds()
        if seconds <= 0 or seconds > 86400:
            invalid("punch_out must be after punch_in and within 24 hours")
        result["work_hours"] = compute_work_hours(pin, pout)
        result["overtime_minutes"] = compute_overtime(pout, end)
        result["half_day"] = result["work_hours"] < 4.5
    return result


class EmployeeIn(BaseModel):
    emp_code: Annotated[str, Field(pattern=r"^EMP\d{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")] = "09:30"
    shift_end: Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")] = "18:30"
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def calendar_date(cls, v):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            raise ValueError("Expected YYYY-MM-DD")
        CalendarDate.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def distinct_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Millis = Field(default_factory=lambda: int(datetime.now(UTC).timestamp() * 1000))
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Millis = Field(default_factory=lambda: int(datetime.now(UTC).timestamp() * 1000))


class RegularizeIn(BaseModel):
    status: Status = Field(default=None)
    punch_in: Millis = Field(default=None)
    punch_out: Millis = Field(default=None)
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]


def employee_or_404(emp_code):
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")
    return emp


def snapshot_filter(doc):
    # Compare every mutable field, including history. Missing legacy fields are distinct
    # from explicitly stored defaults. A successful writer invalidates stale snapshots.
    clauses = [{"_id": doc["_id"]}]
    for key in ["status", *DEFAULTS]:
        if key in doc:
            clauses.append({key: {"$eq": doc[key], "$exists": True}})
        else:
            clauses.append({key: {"$exists": False}})
    return {"$and": clauses}


@app.get("/health")
def health():
    try:
        db.command("ping")
        if not getattr(app.state, "indexes_ready", False):
            create_indexes()
            app.state.indexes_ready = True
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable or indexes not ready")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = instant()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return json_value(doc)


@app.get("/employees")
def list_employees(department: str | None = None, page: Page = 1, page_size: PageSize = 20):
    query = {"department": department} if department is not None else {}
    docs = db.employees.find(query, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": json_value(list(docs)), "total": db.employees.count_documents(query),
            "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employee_or_404(body.emp_code)
    ts = instant(body.punched_at)
    doc = {**DEFAULTS, "history": [], "emp_code": body.emp_code,
           "date": attendance_date(ts, emp), "status": body.status, "punch_in": ts}
    doc.update(derived(emp, doc))
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already recorded for this attendance date")
    return attendance_json(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employee_or_404(body.emp_code)
    ts = instant(body.punched_at)
    doc = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts, "$ne": None}},
                                       sort=[("punch_in", -1)], hint="employee_punch")
    if doc is None:
        raise HTTPException(404, "No punch-in found")
    if doc.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")
    values = derived(emp, {**doc, "punch_out": ts})
    values["punch_out"] = ts
    updated = db.attendance_logs.find_one_and_update(snapshot_filter(doc), {"$set": values},
                                                      return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "Record changed concurrently")
    return attendance_json(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date, "path")
    emp = employee_or_404(emp_code)
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if old is None:
        raise HTTPException(404, "Attendance record not found")
    changes_in = body.model_dump(exclude_unset=True, exclude={"reason", "regularized_by"})
    final = {**DEFAULTS, **old}
    for key, value in changes_in.items():
        final[key] = instant(value) if key in ("punch_in", "punch_out") else value
    if final["status"] not in PRESENCE:
        if "punch_in" in changes_in or "punch_out" in changes_in:
            invalid("ABSENT and LEAVE cannot include punch times")
        final["punch_in"] = final["punch_out"] = None
    final.update(derived(emp, final))
    fields = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]
    changes = {k: {"from": old.get(k, DEFAULTS.get(k)), "to": final[k]}
               for k in fields if old.get(k, DEFAULTS.get(k)) != final[k]}
    if not changes:
        invalid("Correction changes nothing")
    entry = {"at": instant(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    updated = db.attendance_logs.find_one_and_update(
        snapshot_filter(old), {"$set": {k: final[k] for k in fields}, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "Record changed concurrently")
    return attendance_json(updated)


def attendance_query(emp_code=None, date_from=None, date_to=None, status=None):
    validate_range(date_from, date_to)
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if status is not None:
        query["status"] = status
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    hint = "employee_date" if emp_code is not None else "status_date_employee" if status is not None else "date_employee"
    return query, hint


@app.get("/attendance")
def list_attendance(emp_code: str | None = None, date_from: str | None = None, date_to: str | None = None,
                    status: Status | None = None, page: Page = 1, page_size: PageSize = 20):
    query, hint = attendance_query(emp_code, date_from, date_to, status)
    docs = db.attendance_logs.find(query, {"_id": 0}).hint(hint).sort([("date", -1), ("emp_code", 1)])
    items = [attendance_json(d) for d in docs.skip((page - 1) * page_size).limit(page_size)]
    return {"items": items, "total": db.attendance_logs.count_documents(query, hint=hint),
            "page": page, "page_size": page_size}


# Aggregation expressions and builders are shared by endpoints and /admin/explain.
def decimal_expr(value):
    return {"$toDecimal": value}


def half_up(value, places=2):
    # $round uses ties-to-even. All reported quantities here are non-negative.
    scale = 10 ** places
    return {"$cond": [{"$eq": [value, None]}, None, {"$divide": [
        {"$floor": {"$add": [{"$multiply": [decimal_expr(value), scale]}, Decimal128("0.5")]}}, scale]}]}


def calendar_instant(day):
    return datetime.combine(day, time(), UTC)


def weekday_expr(day):
    return {"$in": [{"$dayOfWeek": day}, [2, 3, 4, 5, 6]]}


def present_expr(weekdays=False):
    condition = {"$in": ["$status", PRESENCE]}
    if weekdays:
        condition = {"$and": [condition, weekday_expr({"$dateFromString": {"dateString": "$date"}})]}
    return {"$cond": [condition, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}


def log_stats_group():
    return {"$group": {"_id": None,
        "present_days": {"$sum": present_expr(True)},
        "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "hours_sum": {"$sum": {"$cond": [{"$in": ["$status", PRESENCE]},
                                            decimal_expr({"$ifNull": ["$work_hours", 0]}), 0]}},
        "hours_count": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", PRESENCE]},
                        {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]}, 1, 0]}}}}


def stats_lookup(first, last):
    return {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()},
                    "$expr": {"$eq": ["$emp_code", "$$code"]}}}, log_stats_group()], "as": "stats"}}


def stat(field):
    return {"$ifNull": [{"$arrayElemAt": ["$stats." + field, 0]}, 0]}


def monthly_pipeline(emp_code, month):
    first, last = month_bounds(month)
    # Enumerating calendar days is done inside MongoDB, never in Python.
    day_offsets = {"$range": [0, (last - first).days + 1]}
    eligible_day = {"$dateAdd": {"startDate": calendar_instant(first), "unit": "day", "amount": "$$n"}}
    working_days = {"$size": {"$filter": {"input": day_offsets, "as": "n", "cond": {"$and": [
        weekday_expr(eligible_day), {"$gte": [eligible_day, {"$dateFromString": {"dateString": "$joined_on"}}]}]}}}}
    pipeline = [{"$match": {"emp_code": emp_code}}, stats_lookup(first, last),
        {"$set": {"working_days": working_days, "present_days": stat("present_days")}},
        {"$project": {"_id": 0, "emp_code": 1, "month": {"$literal": month}, "working_days": 1,
            "present_days": 1, "leave_days": stat("leave_days"), "late_count": stat("late_count"),
            "total_late_minutes": stat("total_late_minutes"), "total_overtime_minutes": stat("total_overtime_minutes"),
            "attendance_pct": {"$cond": [{"$gt": ["$working_days", 0]},
                half_up({"$divide": [{"$multiply": [decimal_expr("$present_days"), 100]}, "$working_days"]}), None]}}}]
    return "employees", pipeline, "employee_code"


def summary_pipeline(month, department=None):
    first, last = month_bounds(month)
    match = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        match["department"] = department
    group = {"_id": "$department", "headcount": {"$sum": 1}}
    for field in ["present_days", "late_count", "total_late_minutes", "on_duty_count", "hours_sum", "hours_count"]:
        group[field] = {"$sum": stat(field)}
    group["leave_count"] = {"$sum": stat("leave_days")}
    pipeline = [{"$match": match}, stats_lookup(first, last), {"$group": group},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
            "avg_work_hours": {"$cond": [{"$gt": ["$hours_count", 0]},
                half_up({"$divide": ["$hours_sum", "$hours_count"]}), None]}}},
        {"$sort": {"department": 1}}]
    return "employees", pipeline, "department_joined" if department is not None else "joined_on"


def leaderboard_pipeline(month, limit=10, department=None):
    first, last = month_bounds(month)
    pipeline = [{"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"}]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department",
                       "rank": 1, "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}}])
    return "attendance_logs", pipeline, "date_employee"


def trend_pipeline(department, start, end):
    validate_range(start, end, 92)
    first, last = parse_date(start), parse_date(end)
    pipeline = [{"$match": {"department": department}},
        {"$group": {"_id": None, "codes": {"$push": "$emp_code"}, "joined": {"$push": "$joined_on"}}},
        # Generate every day from the requested range, even if there are no logs.
        {"$set": {"offsets": {"$range": [0, (last - first).days + 1]}}}, {"$unwind": "$offsets"},
        {"$set": {"day": {"$dateAdd": {"startDate": calendar_instant(first), "unit": "day", "amount": "$offsets"}}}},
        {"$set": {"date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
                  "is_working_day": weekday_expr("$day")}},
        {"$set": {"headcount": {"$size": {"$filter": {"input": "$joined", "as": "joined",
                                                        "cond": {"$lte": ["$$joined", "$date"]}}}}}},
        {"$lookup": {"from": "attendance_logs", "let": {"day": "$date", "codes": "$codes"}, "pipeline": [
            {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
            {"$match": {"$expr": {"$in": ["$emp_code", "$$codes"]}}},
            {"$group": {"_id": None, "present_count": {"$sum": present_expr()},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}}
        ], "as": "stats"}},
        {"$set": {"present_count": stat("present_count"), "late_count": stat("late_count")}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            half_up({"$divide": [decimal_expr("$present_count"), "$headcount"]}, 4), None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {
            "$avg": "$attendance_rate", "window": {"documents": [-6, "current"]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
                       "late_count": 1, "attendance_rate": 1, "moving_avg_7d": half_up("$moving_avg_7d", 4)}},
        {"$sort": {"date": 1}}]
    return "employees", pipeline, "department_joined"


def run_pipeline(spec):
    collection, pipeline, hint = spec
    return json_value(list(db[collection].aggregate(pipeline, hint=hint, allowDiskUse=True)))


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    items = run_pipeline(monthly_pipeline(emp_code, month))
    if not items:
        raise HTTPException(404, "Employee not found")
    return items[0]


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: str | None = None):
    return {"month": month, "items": run_pipeline(summary_pipeline(month, department))}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: Limit = 10, department: str | None = None):
    return {"month": month, "items": run_pipeline(leaderboard_pipeline(month, limit, department))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, start: Annotated[str, Query(alias="from")], end: Annotated[str, Query(alias="to")]):
    items = run_pipeline(trend_pipeline(department, start, end))
    if not items:
        raise HTTPException(404, "Department not found")
    return {"department": department, "items": items}


ExplainEndpoint = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: ExplainEndpoint, emp_code: str | None = None, month: str | None = None,
    department: str | None = None, limit: Limit = 10, date_from: str | None = None, date_to: str | None = None,
    status: Status | None = None, start: Annotated[str | None, Query(alias="from")] = None,
    end: Annotated[str | None, Query(alias="to")] = None, page: Page = 1, page_size: PageSize = 20):
    if endpoint == "attendance_list":
        query, hint = attendance_query(emp_code, date_from, date_to, status)
        collection = "attendance_logs"
        command = {"find": collection, "filter": query, "projection": {"_id": 0},
                   "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size,
                   "limit": page_size, "hint": hint}
    else:
        if endpoint != "department_trend" and month is None:
            invalid("month is required", "query")
        if endpoint == "employee_monthly":
            if emp_code is None:
                invalid("emp_code is required", "query")
            spec = monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            spec = summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            spec = leaderboard_pipeline(month, limit, department)
        else:
            if department is None or start is None or end is None:
                invalid("department, from and to are required", "query")
            spec = trend_pipeline(department, start, end)
        collection, pipeline, hint = spec
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}, "hint": hint, "allowDiskUse": True}
    result = db.command("explain", command, verbosity="executionStats")
    # Raw explain output can legitimately have _id keys: preserve its complete shape.
    return {"endpoint": endpoint, "collection": collection, "explain": explain_json(result)}


def explain_json(value):
    if isinstance(value, dict):
        return {k: explain_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [explain_json(v) for v in value]
    return json_value(value)
