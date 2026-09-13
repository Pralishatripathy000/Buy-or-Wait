# Public-sample evaluation

Development-set results, not hidden-test accuracy. Sample outputs are passed only to this evaluator, never to the prediction engine.

| Field | Correct | Total | Accuracy |
|---|---:|---:|---:|
| amount_safe_to_pay | 2 | 25 | 8.0% |
| affordability_status | 19 | 25 | 76.0% |
| recommended_payment_method | 20 | 25 | 80.0% |
| payment_plan | 19 | 25 | 76.0% |
| earliest_date_for_full_payment | 18 | 25 | 72.0% |
| spending_changes_needed | 21 | 25 | 84.0% |

Forecast amounts are inferred estimates, not guarantees. Exact amount matches use a 0.01-unit tolerance. The detailed mismatches are retained. Aggregate absolute error across different currencies is not a comparable financial loss metric. Every generated payment plan is independently revalidated against the reconstructed ledger.