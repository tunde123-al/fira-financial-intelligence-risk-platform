# Index review (2026-10-03)

Synthetic data: 300,000 alerts (63% resolved, 37% open, ~2/3 of open assigned across 20 users) and 100,000 cases (92% closed). Median of 7 runs of `EXPLAIN (ANALYZE)`, parallel workers off. Each candidate was tested alone against the schema without it.

| candidate | used by | size MB | my_open_alerts_by_triage | queue_top_by_triage | queue_top_critical_high | recently_resolved | my_open_cases |
|---|---|---|---|---|---|---|---|
| ix_malerts_assignee_triage | my_open_alerts_by_triage | 3.72 | 9.956 -> 0.914 ms (used) | 60.533 -> 61.224 ms | 61.03 -> 59.782 ms | 118.602 -> 117.045 ms | 0.603 -> 0.582 ms |
| ix_malerts_open_triage | my_open_alerts_by_triage, queue_top_by_triage, queue_top_critical_high | 2.41 | 9.956 -> 24.032 ms (used) | 60.533 -> 0.146 ms (used) | 61.03 -> 0.124 ms (used) | 118.602 -> 119.424 ms | 0.603 -> 0.605 ms |
| ix_malerts_open_triage_all | my_open_alerts_by_triage, queue_top_by_triage, queue_top_critical_high | 14.25 | 9.956 -> 63.436 ms (used) | 60.533 -> 0.103 ms (used) | 61.03 -> 0.114 ms (used) | 118.602 -> 119.852 ms | 0.603 -> 0.617 ms |
| ix_malerts_resolved_at | recently_resolved | 1.31 | 9.956 -> 9.932 ms | 60.533 -> 61.303 ms | 61.03 -> 58.981 ms | 118.602 -> 0.588 ms (used) | 0.603 -> 0.598 ms |
| ix_cases_assigned_status | my_open_cases | 0.7 | 9.956 -> 9.86 ms | 60.533 -> 61.074 ms | 61.03 -> 60.192 ms | 118.602 -> 118.98 ms | 0.603 -> 0.031 ms (used) |

Shipped set together (ix_malerts_assignee_triage, ix_malerts_open_triage, ix_malerts_resolved_at, ix_cases_assigned_status):

- my_open_alerts_by_triage: 9.956 -> 1.051 ms, Incremental Sort; Index Scan using ix_malerts_assignee_triage; Limit
- queue_top_by_triage: 60.533 -> 0.161 ms, Incremental Sort; Index Scan using ix_malerts_open_triage; Limit
- queue_top_critical_high: 61.03 -> 0.158 ms, Incremental Sort; Index Scan using ix_malerts_open_triage; Limit
- recently_resolved: 118.602 -> 0.571 ms, Index Scan using ix_malerts_resolved_at; Limit
- my_open_cases: 0.603 -> 0.038 ms, Bitmap Heap Scan; Bitmap Index Scan using ix_cases_assigned_status; Limit

Baseline plans (no candidate index):

- my_open_alerts_by_triage: 9.956 ms, Bitmap Heap Scan; Bitmap Index Scan using ix_malerts_assigned; Bitmap Index Scan using ix_malerts_status_triggered; BitmapAnd; Limit; Sort
- queue_top_by_triage: 60.533 ms, Bitmap Heap Scan; Bitmap Index Scan using ix_malerts_status_triggered; Limit; Sort
- queue_top_critical_high: 61.03 ms, Bitmap Heap Scan; Bitmap Index Scan using ix_malerts_status_triggered; Limit; Sort
- recently_resolved: 118.602 ms, Limit; Seq Scan; Sort
- my_open_cases: 0.603 ms, Bitmap Heap Scan; Bitmap Index Scan using ix_cases_assigned; Bitmap Index Scan using ix_cases_status; BitmapAnd; Limit
