# Employee Attendance & Analytics API

Python 3.11+, FastAPI, PyMongo and MongoDB 6.0+ (tested with MongoDB 7). All application code is in `app/main.py`.

## Run

Create a virtual environment and install `requirements.txt`. Set `MONGO_URI` and `MONGO_DB` to your MongoDB instance and database, then run:

```sh
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

The development defaults are `mongodb://localhost:27017` and `attendance_db`. Real environment variables override any local `.env`. Indexes are created automatically, including on pre-seeded databases. `/health` returns 200 when MongoDB and the indexes are ready; otherwise 503. Interactive API documentation is at `http://localhost:8000/docs`.

For local MongoDB without installing a server (no Dockerfile needed):

```sh
docker run -d --name hrone-mongo -p 127.0.0.1:27018:27017 mongo:7
```

Set `MONGO_URI=mongodb://localhost:27018` when using that container. In PowerShell, use `$env:MONGO_URI = 'mongodb://localhost:27018'` and `$env:MONGO_DB = 'attendance_db'`.

## Tests

```sh
pip install -r requirements-dev.txt
python -m pytest -q
```

Tests require a real MongoDB server at `mongodb://localhost:27018`, or the URI in `TEST_MONGO_URI`. Each test creates and drops its own uniquely named `hrone_test_*` database. Tests use synthetic fixtures covering timestamp validation, rounding thresholds, overnight shifts, races, audit history, missing fields, weekend records, no-log employees, mid-month joiners, ranking ties, gap-filled trends and explain plans. Set `RUN_SCALE_TEST=1` to include the 100,000-record check (PowerShell: `$env:RUN_SCALE_TEST = '1'`). No mocks substitute for aggregation execution.

## Notes

Instants are UTC BSON dates in storage and integer epoch milliseconds in JSON. Calendar rules use IST. Decimal half-up rounding is explicit. Analytics run in MongoDB and read stored derived values. Corrections compare a snapshot and atomically append history; a stale writer receives 409. Explain requests use the same query builders and hints as the actual endpoints.

All requested endpoints are implemented. The supplied assignment kit, data dumps, `.env`, secrets, virtual environments and Dockerfiles are excluded from this repository. `REVIEW.md` describes the starter fixes; `DECISIONS.md` explains the main choices.

Validation: the contract suite and opt-in scale check pass against MongoDB 7. The scale check verifies index creation within 20 seconds, indexed plans for all five explain targets, and list/analytics requests on 100,000 records. Local endpoint timings were below 0.2 seconds; these are development-machine observations, not latency guarantees.
