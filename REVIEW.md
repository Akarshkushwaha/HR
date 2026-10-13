# Starter review

| # | Where | What is wrong | How to notice it | Fix |
|---|---|---|---|---|
| 1 | Configuration/startup | No application indexes are created. | Duplicate keys succeed; large queries scan the collection. | Idempotent startup indexes, including unique employee and attendance keys. |
| 2 | `/health` | Always reports ready without checking MongoDB. | Stop MongoDB; the starter still returns 200. | Ping MongoDB; return 503 on database or index readiness failure. |
| 3 | `EmployeeIn` | Codes, names, emails, departments, dates and shift times lack contract validation. | Submit an invalid code, an empty name, `2026-02-30`, or `24:00`. | Pydantic constraints plus strict calendar validation. |
| 4 | `EmployeeIn` | Equal shift start and end are accepted. | Submit `09:30` for both. | Validate that the two values differ. |
| 5 | `create_employee` | Check-then-insert cannot guarantee uniqueness. | Send simultaneous requests with the same code. | Unique index; catch `DuplicateKeyError` and return 409. |
| 6 | `create_employee` | `created_at` uses naive host-local time and returns a datetime. | Run on a host outside UTC; inspect the response timestamp type. | UTC-aware server timestamp; serialize integer epoch milliseconds. |
| 7 | `list_employees` | Offset is `page * page_size`, skipping the first page. | Request page 1 with one employee. | Use `(page - 1) * page_size`. |
| 8 | `list_employees` | Total ignores department filtering. | Filter a department containing two of three employees. | Count the same filtered query. |
| 9 | `list_employees` | No deterministic sort. | Insert codes in reverse order and paginate. | Sort by `emp_code` ascending in MongoDB. |
| 10 | Both list endpoints | Page and page size are unbounded. | Send page 0 or page size 101. | Enforce page >= 1 and size 1–100. |
| 11 | `PunchInIn` | Arbitrary status strings are accepted. | Submit `ABSENT` or an unknown status to punch-in. | Restrict to PRESENT, WFH and ON_DUTY. |
| 12 | `PunchInIn` | Timestamp accepts coercible strings, seconds-looking numbers and floats. | Submit a Unix-seconds value or quoted milliseconds. | Strict integer with the specified epoch bounds; explicit null rejected. |
| 13 | `punch_in` | Missing employee causes a subscript error. | Punch in an unknown employee code. | Look up employee and return 404. |
| 14 | `punch_in` | `fromtimestamp` and `now` depend on host timezone; attendance dates are not IST. | An instant near IST midnight gets the wrong date on a UTC host. | UTC instants, explicit IST conversion at calendar boundaries. |
| 15 | `punch_in` | Overnight punch-ins never map to the previous attendance day. | For 22:00–06:00, punch in at 02:00 next morning. | Apply R1's strictly-earlier-than-shift-end rule. |
| 16 | Time handling | Subseconds are not truncated before calculation or storage. | Submit 09:40:00.900. | Truncate input instants and server event times to whole seconds. |
| 17 | `compute_late_minutes` | Shift start is attached to the punch's timezone/date instead of the IST attendance day. | Compare UTC-based and overnight punches. | Build explicit shift boundaries from the employee and attendance date. |
| 18 | `compute_late_minutes` | Flooring precedes the grace check. | 09:40:01 produces 0 instead of 10. | Compare elapsed seconds > 600, then floor minutes. |
| 19 | `compute_work_hours` | Python float `round` does not implement decimal half-up. | 270 seconds is 0.075 hours and must become 0.08. | Decimal division and `ROUND_HALF_UP`. |
| 20 | `compute_overtime` | Always places shift end on the same day. | Overnight out at 06:40 next morning gives excessive overtime. | Advance end by one day for overnight shifts. |
| 21 | `compute_overtime` | Counts overtime below the minimum. | Punch out 29:59 after shift end. | Return 0 unless whole overtime minutes >= 30. |
| 22 | `punch_in` | Attendance check-then-insert races. | Parallel punches for the same employee/day both succeed. | Unique `(emp_code, date)` index; duplicate maps to 409. |
| 23 | `punch_in`, `list_attendance` | Expose a MongoDB-derived `id` forbidden by the contract. | Inspect attendance JSON. | Return only the natural key; remove `_id`, never synthesize `id`. |
| 24 | Response serialization | Datetimes, including nested history changes, remain ISO strings or BSON objects. | Read punch fields and history `from`/`to`. | Recursively convert all instants to integer epoch milliseconds. |
| 25 | `list_attendance` | Materializes and sorts every matching document in Python. | Request 20 rows from 100,000 records. | Indexed server-side sort, skip, limit and filtered count. |
| 26 | `list_attendance` | Missing secondary sort makes ties unstable. | Several employees have records on the same date. | Sort date descending, then employee code ascending. |
| 27 | `list_attendance` | Invalid dates, inverted ranges and unsupported statuses are accepted. | Query February 30, an empty date filter, or `date_from > date_to`. | Validate calendar strings, ordering and status enum; return 422. |
| 28 | `list_attendance` | Legacy defaults are not supplied. | Seed a record without history or half_day. | Return history `[]`, half_day false and other specified defaults. |

Examined and retained: `load_dotenv()` already leaves real environment variables in control; synchronous PyMongo is suitable for FastAPI's synchronous endpoints, which run in its worker pool; the fixed UTC+05:30 offset is appropriate for the contract's IST; absence of authentication is explicitly within assignment scope. Extra body fields remain ignored, including client-supplied derived values, as the contract requires. MongoDB's internal `_id` remains useful for atomic updates but is never employee identity or an attendance response field.

The TODO endpoints were missing functionality rather than defects in existing endpoints. They are implemented with shared aggregation builders, stored derived values, competition ranking, calendar gap generation, bounded windows, and snapshot-conditional updates with atomic audit appends.
