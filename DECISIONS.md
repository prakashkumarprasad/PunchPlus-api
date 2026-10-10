# DECISIONS.md

> Draft notes only - **rewrite all five in your own voice before submitting.**

1. **Indexes.** Five. `employees`: unique `emp_code`, `(department, emp_code)`,
   `joined_on`. `attendance_logs`: unique `(emp_code, date)`, which stops two punch-ins on
   one day, and `(date desc, emp_code)`, which gives the listing its sort. Rejected: a
   covering index of every analytics field - it would save 9,833 document reads but
   doubles each punch-in's write cost.

2. **Punch-in race.** Neither checks anything first. Both call `insert_one`, and the
   unique `(emp_code, date)` index makes the insert and the uniqueness test one atomic
   step inside MongoDB. One wins; the other gets `DuplicateKeyError`, returned as a 409.

3. **Ties.** `limit` caps the rank, not the row count. I rank with `$rank` (competition
   ranking: 1, 2, 2, 4), then keep every row whose rank is at or below it, so a tie at the
   cutoff returns more rows than the limit - which is what the contract wants.

4. **Headcount.** It starts on `employees`, not on the records, so somebody with no logs
   is still counted. It keeps everyone who joined by the month's last day and groups by
   department last; a later joiner was never in the set.

5. **100x the data.** The plans stay the same, because indexes decide them, not row
   counts. What grows is summing a whole month, so I would add that covering index, or
   pre-aggregate each month.
