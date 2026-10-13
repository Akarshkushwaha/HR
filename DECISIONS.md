# Design decisions

1. **Indexes.** Unique `employee_code` and `employee_date` enforce identities. `date_employee` serves date ranges and attendance ordering; `status_date_employee` adds status filtering. `employee_punch` locates the latest eligible punch. Employee `department_joined` and `joined_on` indexes support departmental membership and month-end eligibility. Employee/date indexes also serve aggregation lookups. I rejected a history index: nothing queries history, and indexing it would increase write cost.

2. **Punch-in race.** Both requests validate input, read the employee and calculate the same attendance key. Both attempt insertion. MongoDB's unique index admits one insertion. That request returns 201; the other's duplicate-key exception becomes 409. No pre-insert existence check decides the winner.

3. **Ties.** `$rank` assigns competition ranks using total late minutes alone. Filtering rank after the window includes every tied employee at the cutoff. A separate final sort orders ties by employee code.

4. **Headcount.** The summary starts with eligible employees and performs a lookup of each employee's monthly log totals. Empty lookup results contribute zero totals while the employee still contributes one to headcount. Work-hour sums and record counts are combined before division.

5. **100x data.** I would introduce incremental daily departmental summaries, preserving correction-driven updates, to reduce repeated aggregation over historical attendance.
