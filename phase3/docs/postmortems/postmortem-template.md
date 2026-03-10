# Postmortem: [INCIDENT TITLE]

> **Template instructions:** Fill every section. Blameless postmortems only.
> We are improving systems, not punishing people.
> Due within 5 business days of incident resolution.

---

## Incident Summary

| Field | Value |
|---|---|
| **Incident ID** | INC-YYYY-NNN |
| **Date** | YYYY-MM-DD |
| **Duration** | X hours Y minutes |
| **Severity** | P1 / P2 / P3 |
| **Services Affected** | order-service, payment-service |
| **Error Budget Impact** | X% of monthly budget consumed |
| **Users Affected** | Estimated N users |
| **Revenue Impact** | $X (if applicable) |
| **Detected By** | Alertmanager / Customer report / On-call |
| **Incident Commander** | @name |
| **Authors** | @name, @name |

---

## Impact

_Describe what users experienced. Be specific — not "degraded performance" but "users could not place orders for 43 minutes."_

---

## Timeline

All times in UTC.

| Time | Event |
|---|---|
| HH:MM | Alert fired: `OrderServiceAvailabilityCritical` |
| HH:MM | On-call engineer paged |
| HH:MM | On-call engineer acknowledged |
| HH:MM | Initial investigation started |
| HH:MM | Root cause identified: ___________ |
| HH:MM | Mitigation applied: ___________ |
| HH:MM | Service recovering — error rate dropping |
| HH:MM | SLO threshold restored |
| HH:MM | Incident resolved |
| HH:MM | All-clear confirmed — monitoring for recurrence |

**Time to Detect (TTD):** X minutes
**Time to Acknowledge (TTA):** X minutes
**Time to Mitigate (TTM):** X minutes
**Time to Resolve (TTR):** X minutes

---

## Root Cause

_One clear paragraph. What was the technical root cause? What condition made this failure possible?_

Example: A deployment of order-service v1.4.2 introduced a missing database index on the `user_id` column of the orders table. Under normal load this was not noticeable, but a 3x traffic spike triggered by a marketing campaign caused full table scans on every order lookup, exhausting the database connection pool and causing 502 errors for all order requests.

---

## Contributing Factors

_What made this worse or harder to detect? These are systemic issues, not blame._

- The deployment passed all integration tests because the test dataset was too small to trigger the slow query
- The p99 latency alert threshold was set too high (5s) to catch the degradation early
- The on-call runbook did not include a step for checking slow database queries
- No canary deployment — 100% of traffic hit the bad version immediately

---

## What Went Well

_Things that limited impact or helped resolution. Be genuine._

- The multi-window burn rate alert fired within 8 minutes of the deployment
- The Tempo traces immediately showed the slow database query span
- The rollback procedure worked cleanly and completed in under 2 minutes
- Team communication in the incident Slack channel was clear

---

## What Went Badly

_Be honest. This is how we improve._

- The deployment had no canary phase — 100% of traffic was immediately affected
- The on-call engineer took 12 minutes to acknowledge (was asleep — pager not loud enough)
- The alert runbook had no step for checking recent deployments first
- No automated rollback triggered — this should have been caught by the error rate workflow

---

## Action Items

_Every action item needs an owner and a due date. No owner = not done._

| # | Action | Owner | Due Date | Priority |
|---|---|---|---|---|
| 1 | Add database index on `orders.user_id` | @engineer | YYYY-MM-DD | P0 |
| 2 | Implement canary deployment for order-service | @platform | YYYY-MM-DD | P1 |
| 3 | Lower p99 latency alert threshold from 5s to 1s | @oncall | YYYY-MM-DD | P1 |
| 4 | Update runbook: add "check recent deployments" as step 1 | @oncall | YYYY-MM-DD | P2 |
| 5 | Tune automated rollback threshold to trigger on this pattern | @platform | YYYY-MM-DD | P1 |
| 6 | Add slow query detection alert for DB query p99 > 100ms | @platform | YYYY-MM-DD | P2 |

---

## Error Budget Impact

- **SLO affected:** order-service availability (99.9% target)
- **Budget consumed this incident:** X minutes of Y minutes monthly budget (Z%)
- **Budget remaining this month:** Y%
- **Action required per budget policy:** [None / Deployment freeze / Full freeze]

---

## Lessons Learned

_The 2-3 most important things this incident taught us about our system._

1. _____
2. _____
3. _____

---

## Appendix

### Relevant Dashboards
- [Order Service Dashboard](http://grafana/d/order-service)
- [Executive SLO View](http://grafana/d/slo-executive)

### Related Incidents
- INC-YYYY-NNN — similar latency incident in staging

### References
- [Deployment that caused the incident](https://github.com/link-to-pr)
- [Rollback PR](https://github.com/link-to-rollback)
