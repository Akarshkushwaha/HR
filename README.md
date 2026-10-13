# Employee Attendance & Analytics API

A FastAPI service for employee attendance, manual corrections, and attendance analytics. It uses a real MongoDB database through PyMongo. All application code is in `app/main.py`.

## What is implemented

| Feature | Behavior |
|---|---|
| Employees | Create an employee with a unique business code; list employees with department filtering, stable ordering, and pagination. |
| Punch-in | Record PRESENT, WFH, or ON_DUTY attendance, with IST dates, overnight shifts, and the 10-minute grace rule. |
| Punch-out | Close the latest eligible record and calculate work hours, overtime, and half-day status. |
| Manual corrections | Validate the final record, recalculate derived fields, and append an audit entry containing only actual changes. |
| Employee monthly summary | Working days from the joining date, present/leave days, late and overtime totals, and attendance percentage. |
| Department monthly summary | Include eligible employees with no logs; calculate the work-hour average over records. |
| Late leaderboard | Competition ranking (1, 2, 2, 4), including ties at the requested rank cutoff. |
| Department daily trend | Every calendar day, including gaps and weekends; daily headcount, attendance rate, and a seven-day moving average. |
| Readiness and query plans | MongoDB health probe, automatic indexes, and execution-statistics explain plans for five supported queries. |

Analytics execute MongoDB aggregation pipelines and use stored derived values. Dates and shift times use IST. API instants are integer epoch milliseconds; MongoDB stores UTC BSON dates. Instants are truncated to whole seconds before calculations. Rounding is decimal half-up: two decimals for hours/percentages and four for rates. A rounded work duration below 4.50 hours is a half day. Overtime is counted only from 30 whole minutes onward.

## Requirements

- Python 3.11 or newer, Git, and MongoDB 6.0 or newer.
- Docker is needed only for the local-container setup below.
- The MongoDB user must be able to read/write the two collections and create indexes. Tests also need permission to create and drop disposable databases.

Local MongoDB is sufficient. An Atlas account or an API key is not required. An existing MongoDB server or an Atlas connection URI can also be supplied through the environment.

## Setup from a clean clone

### 1. Download the project

```sh
git clone https://github.com/Akarshkushwaha/HR.git
cd HR
```

### 2. Create a virtual environment and install dependencies

**Windows PowerShell:**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

If activation is restricted by your PowerShell policy, invoke `.\.venv\Scripts\python.exe` and `.\.venv\Scripts\uvicorn.exe` directly instead of changing the policy.

**macOS/Linux:**

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

### 3. Start MongoDB

For a new local MongoDB 7 container:

```sh
docker run -d --name hrone-assignment-mongo -p 127.0.0.1:27018:27017 mongo:7
```

If this container already exists, use `docker start hrone-assignment-mongo`. MongoDB listens on port 27017 inside the container; your computer accesses it on port 27018. This uses an official MongoDB image and needs no Dockerfile. If you already have MongoDB elsewhere, use its URI instead.

### 4. Configure the connection and start the API

**Windows PowerShell:**

```powershell
$env:MONGO_URI = 'mongodb://localhost:27018'
$env:MONGO_DB = 'attendance_db'
uvicorn app.main:app --port 8000
```

**macOS/Linux:**

```sh
export MONGO_URI='mongodb://localhost:27018'
export MONGO_DB='attendance_db'
uvicorn app.main:app --port 8000
```

Run from the repository root. The defaults, if omitted, are `mongodb://localhost:27017` and `attendance_db`. Real environment variables take priority over a local `.env`; no `.env` or credentials are committed. Indexes are created automatically and idempotently, including on pre-seeded databases. The application does not call external services.

### 5. Confirm readiness

Open [http://localhost:8000/health](http://localhost:8000/health). A ready service returns HTTP 200 and `{"status":"ok"}`. HTTP 503 means MongoDB or index creation is unavailable; check the URI, database permissions, and running server.

Use [Swagger UI](http://localhost:8000/docs) to send requests, or view the generated API definition at [http://localhost:8000/openapi.json](http://localhost:8000/openapi.json). A fresh database starts empty; create an employee before punching attendance.

## Try an attendance workflow

In Swagger UI, expand the relevant endpoint, select **Try it out**, enter the JSON, then select **Execute**.

1. Create an employee with `POST /employees`:

   ```json
   {
     "emp_code": "EMP2001",
     "name": "Demo Employee",
     "email": "demo@example.test",
     "department": "Demo",
     "shift_start": "09:30",
     "shift_end": "18:30",
     "joined_on": "2026-01-01"
   }
   ```

2. Punch in with `POST /attendance/punch-in`:

   ```json
   {"emp_code": "EMP2001", "status": "PRESENT", "punched_at": 1783310400000}
   ```

3. Punch out with `POST /attendance/punch-out`:

   ```json
   {"emp_code": "EMP2001", "punched_at": 1783342800000}
   ```

These punches represent 09:30–18:30 IST on 6 July 2026. Expect `date: "2026-07-06"`, `work_hours: 9.0`, zero late/overtime minutes, `half_day: false`, and `history: []`. Omitting `punched_at` uses the current time instead. Repeating either punch request returns 409.

Send this correction to `PATCH /attendance/EMP2001/2026-07-06`:

```json
{
  "punch_out": 1783346400000,
  "reason": "Approved clock correction",
  "regularized_by": "demo.reviewer"
}
```

The corrected out time is 19:30 IST. Expect 10.0 work hours, 60 overtime minutes, and one appended history entry. Inspect `GET /attendance?emp_code=EMP2001`, then try the July analytics below. The example timestamps are milliseconds, not seconds.

## Endpoint reference

| Method | Path | Inputs / purpose |
|---|---|---|
| GET | `/health` | Readiness probe. |
| POST | `/employees` | Employee fields; shifts default to 09:30/18:30. |
| GET | `/employees` | Optional `department`, `page`, `page_size`; employee code ascending. |
| POST | `/attendance/punch-in` | `emp_code`, optional `punched_at` and presence `status`. |
| POST | `/attendance/punch-out` | `emp_code`, optional `punched_at`. |
| GET | `/attendance` | Optional `emp_code`, `date_from`, `date_to`, `status`, `page`, `page_size`; date descending, then employee code ascending. |
| PATCH | `/attendance/{emp_code}/{date}` | Required `reason` and `regularized_by`; optional status/punch changes. |
| GET | `/analytics/employees/{emp_code}/monthly` | Required `month`, such as `2026-07`. |
| GET | `/analytics/departments/summary` | Required `month`; optional exact-match `department`. |
| GET | `/analytics/leaderboard/late` | Required `month`; optional `department` and `limit` (1–50, default 10). |
| GET | `/analytics/departments/{department}/trend` | Required `from` and `to` dates, inclusive, at most 92 days. |
| GET | `/admin/explain/{endpoint}` | Supported query name and its usual query parameters. |

Calendar dates must be `YYYY-MM-DD`; months must be `YYYY-MM`. Pagination starts at page 1, defaults to 20 items, and permits at most 100. Totals reflect filters. Department matching is case-sensitive. Missing resources return 404, conflicts 409, and invalid input 422. Client-supplied derived fields are ignored. MongoDB `_id` values are internal and do not appear in employee or attendance responses.

Corrections require a punch-in for presence statuses. Setting ABSENT or LEAVE clears punches and cannot include punch times. Punch-out must be after punch-in and within 24 hours. No-change corrections return 422. Normal punches do not add history; each successful correction appends one entry. Unique indexes protect duplicate creates/punches; conditional atomic updates prevent concurrent punch-outs or corrections from silently overwriting another writer.

## Run the tests

Keep MongoDB running. With the virtual environment active:

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

The test URI defaults to `mongodb://localhost:27018`; override `TEST_MONGO_URI` if needed. Tests create and drop only uniquely named `hrone_test_*` databases. They do not use `MONGO_DB` as their test database.

Include the 100,000-record test:

**PowerShell:**

```powershell
$env:RUN_SCALE_TEST = '1'
python -m pytest -q -s
```

**macOS/Linux:**

```sh
RUN_SCALE_TEST=1 python -m pytest -q -s
```

Tests cover strict timestamps, calendar validation, threshold rounding, overnight shifts, duration boundaries, parallel writes, audit history, legacy fields, pagination, weekend attendance, no-log employees, joining dates, ties, trend gaps/windows, and explain plans. Aggregations run against real MongoDB.

### Check the original candidate samples locally

The original kit and its data are excluded from the public repository. If you have the kit, point `CANDIDATE_KIT_DIR` at the directory containing `sample_seed.py`, `openapi.yaml`, and `sample_data/`:

```powershell
$env:CANDIDATE_KIT_DIR = 'C:\path\to\candidate_kit'
python -m pytest -q tests/test_candidate_kit.py
```

On macOS/Linux, use `CANDIDATE_KIT_DIR='/path/to/candidate_kit' python -m pytest -q tests/test_candidate_kit.py`. These checks run the original seed script in a disposable database, verify all six employees and nine records, compare analytics with independent calculations, and check response fields/types against the supplied contract. These two tests are skipped when the kit is unavailable. The scale test is skipped unless enabled.

## Inspect query plans and evaluate the service

Examples to open while the API is running:

```text
/admin/explain/attendance_list?emp_code=EMP2001&page=1&page_size=20
/admin/explain/employee_monthly?emp_code=EMP2001&month=2026-07
/admin/explain/department_summary?month=2026-07
/admin/explain/late_leaderboard?month=2026-07&limit=10
/admin/explain/department_trend?department=Demo&from=2026-07-01&to=2026-07-31
```

Prefix each path with `http://localhost:8000`. Responses contain the query name, collection, and MongoDB explain output with `executionStats`. Explain and normal endpoints share query builders and hints. Look for `IXSCAN` and no `COLLSCAN`; the scale test checks these plans on 100,000 records.

To evaluate against another dataset, set `MONGO_URI` and `MONGO_DB` to the evaluator's database, seed `employees` and `attendance_logs` using the specified BSON/date shapes, then start the same Uvicorn command. The evaluator supplies its own MongoDB and data; access to the developer's laptop is unnecessary.

## Files and verification

| File | Purpose |
|---|---|
| `app/main.py` | All application code, validation, atomic writes, indexes, and aggregations. |
| `requirements.txt` | Runtime dependencies. |
| `REVIEW.md` | Starter defects, failing examples, fixes, and items reviewed and retained. |
| `DECISIONS.md` | Five design answers within the requested 150–200-word range. |
| `tests/` and `requirements-dev.txt` | Integration tests and test dependencies. |

All requested endpoints are implemented; no functional section is intentionally omitted. The kit, data dumps, `.env`, secrets, virtual environments, and Dockerfiles are not committed. The repository remains public for review. Submission through the recruitment form is a separate step and has not been performed.

The complete local verification run passed **40 tests** with MongoDB 7, the original kit available, and the scale test enabled. It includes supplied samples, synthetic edge cases, concurrency, the README workflow, fresh-process startup within 20 seconds, environment-variable precedence, indexed explain plans, and 100,000 records. A clean clone without the kit and without the scale flag runs 37 tests and skips the three optional checks. Local list/analytics timings were below 0.2 seconds in the initial scale run; these are observations, not guarantees. The employer's hidden tests are unavailable, so these checks do not claim a guaranteed grading result.
