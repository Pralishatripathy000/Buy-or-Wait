# Payment Affordability Agent — HackerRank Orchestrate (Sept 2026)

An agent that decides, for every incoming payment request, whether a user
can **safely** afford it — and if so, *how* (pay in full, pay partially,
use an installment plan, or wait) — without ever letting the user's
protected minimum balance be breached over a 90-day forward horizon.

---

## 1. Problem Understanding

**What "safe to afford" means.**
A payment is safe if, after making it, the user's simulated account
balance never drops below their `minimum_balance_to_keep` at any point
during the next 90 days — accounting for every other cashflow (income,
recurring bills, other pending payments) that is expected to hit the
account in that window.

**Why current balance alone is insufficient.**
A user can have plenty of money today and still be unsafe: rent, a loan
installment, or a recurring subscription due in two weeks can wipe out
today's surplus before it ever gets used. Conversely, a user who looks
tight today may have a paycheck landing tomorrow that makes a request
perfectly safe. Judging affordability from `current_balance - requested`
alone ignores both of these — you have to simulate the whole horizon, not
just the instant of payment.

**The 90-day safety rule.**
Every candidate plan (full payment, partial payment, an installment
schedule, or even "do nothing") is projected against a day-by-day balance
simulation covering the 90 days following the request date. A plan is only
offered if the simulated balance stays at or above the minimum floor on
**every single day** of that window, not just on average or at the end.

---

## 2. Architecture

The pipeline runs in six stages:

1. **Data ingestion** (`load`) — reads `requests`, `sample_requests`,
   `financial_profiles`, `financial_events`, `request_payment_options`,
   `messages`, `images`, and `exchange_rates` from `dataset/*.csv`.
2. **Message / image evidence** — *scoped out of this version.* See
   [Section 5 — AI Usage](#5-ai-usage) for the reasoning.
3. **Financial-state reconstruction** (`converted_amount`,
   `recurring_templates`) — normalizes every amount into the user's home
   currency and detects recurring income/expense patterns from settled
   history.
4. **Forecasting** (`base_cashflows`) — builds a date-indexed map of every
   expected cashflow over the 90-day horizon: explicitly scheduled/pending
   events plus projected recurring events, with de-duplication so a
   recurring projection never double-counts an already-scheduled instance.
5. **Plan generation & validation** (`decide`, `safe`) — builds candidate
   plans (full / partial / installments), simulates each one against the
   forecast, and keeps only the ones that stay safe for all 90 days.
6. **Output** (`run`) — applies the decision to every row of `requests`
   and writes one prediction row per request to `output.csv`.

---

## 3. Financial Logic

- **Recurring income and expenses.** Settled history is grouped by
  `(direction, category, description)`. A pattern needs at least 3
  occurrences; the gaps between the most recent (up to 5) are checked —
  a median gap of 25–35 days is classified `monthly`, 6–8 days is
  classified `weekly`. The projected amount is the median of the last
  three occurrences (robust to one-off outliers).
- **Pending debits vs. credits.** Pending *credits* (incoming money not
  yet settled) are deliberately excluded from the safety forecast —
  income you haven't received yet shouldn't be relied on to justify a
  payment. Pending *debits* are included, since an obligation you owe is
  a real risk to the balance whether or not it has cleared.
- **Minimum balance protection.** Every simulated plan is checked against
  `minimum_balance_to_keep` on every day of the horizon, not just at plan
  completion.
- **Essential vs. flexible expenses.** *Not yet distinguished* — see
  [Section 7 — Limitations](#7-known-limitations--scope-decisions).
- **Currency conversion.** Any amount not already in the user's home
  currency is converted using the exchange rate with the closest
  `rate_date` to the event's settlement (or event) date — not simply the
  most recent rate, since backdated settlements need a historically
  accurate rate.
- **Duplicate / linked events.** A recurring projection is suppressed for
  any date where a real event of the same `direction` is already
  explicitly scheduled or pending on that date, preventing the same
  cashflow from being counted twice.

---

## 4. Plan Selection

Five possible outcomes per request:

| Method | When chosen |
|---|---|
| `full_payment` | The full amount is safe today, and the user's stated payment preferences accept full payment. |
| `partial_payment` | Full payment isn't safe today, but paying what's safely available now plus the remainder on the earliest safe date is safe and accepted. |
| `installments` | An offered installment plan (within the user's `max_installment_months`) is safe end-to-end and accepted. |
| `wait` | Nothing is safe today, but the full amount becomes safe on some date at or before the deadline. |
| `not_recommended` | No safe plan exists within the deadline. |

**Tie-breaking order.** When multiple plans are simultaneously safe,
candidates are ranked by, in order:
1. Lowest total amount payable (avoids installment overhead/fees when a
   cheaper safe option exists).
2. Earliest start date.
3. Fewest number of payments.

In practice this means: full payment is preferred over partial payment,
which is preferred over installments, whenever all are equally safe —
because it settles the obligation with the least ongoing complexity and
no extra cost. Installments are only chosen when they're the only route
to safety.

---

## 5. AI Usage

- **Message/image evidence extraction: not implemented in this
  submission.** `messages.csv` and `images.csv` are loaded but not
  parsed. This was a deliberate scope decision, not an oversight — see
  Section 7.
- **Why deterministic calculations were used for financial safety.**
  The core decision (does this payment keep the user above their floor
  for 90 days?) is exact arithmetic over known/derived numbers. Handing
  a real financial safety judgment to a generative model introduces
  hallucination risk with no way to audit the result — an LLM should
  never be the final arbiter of whether real money is safe to spend.
  Determinism here is a safety requirement, not a limitation of
  imagination.
- **Prompt-injection protection.** N/A in this version, since no LLM
  calls are made on user-supplied free text (messages/images). If
  extraction is added in future work, any extracted values would be
  treated as *unverified hints* only — never allowed to directly alter
  `minimum_balance_to_keep`, override the deterministic safety check, or
  be trusted without being cross-checked against the numeric ledger data.
- **Token and cost tracking.** N/A — no LLM API calls are made by the
  current pipeline; it is pure `pandas`/`numpy` computation.

---

## 6. Evaluation

See [`evaluation/usage_report.md`](evaluation/usage_report.md) for:
- Results against the labelled sample set
- Field-by-field match rates
- Validation checks performed (row count, schema, no-null critical
  fields)
- Edge cases tested (no accepted payment methods, no recurring history,
  missing exchange rate for a currency pair, request already past
  deadline)
- Errors found and corrected during development

---

## 7. Known Limitations & Scope Decisions

Documented deliberately, not discovered by a reviewer:

1. **Messages/images are unused.** Any repayment promises, context, or
   amounts mentioned in free text or attached images are not extracted
   or factored into the decision. Rationale: unverified user claims
   should not be allowed to move a safety-critical balance calculation
   without independent verification against the numeric ledger.
2. **No essential-vs-flexible expense distinction.** Every historical or
   scheduled expense is treated as equally non-negotiable. A more
   complete system would let "flexible" spend (e.g. discretionary
   subscriptions) be candidates for deferral when it would make an
   otherwise-unsafe request safe.
3. **`spending_changes_needed` is always `"none"`.** This is a reserved
   output field; populating it (e.g. suggesting which flexible expense to
   delay) is future work tied to item 2.
4. **Recurring detection needs ≥3 settled occurrences with a consistent
   cadence.** Sparser or irregular histories won't generate a recurring
   projection, which is a conservative (safe) failure mode but may
   under-forecast some real recurring obligations.

---




## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
python main.py
```

Produces `output.csv` at the project root with one row per request:

| Column | Meaning |
|---|---|
| `request_id` | The request being decided |
| `amount_safe_to_pay` | Max amount payable today without breaching the floor |
| `affordability_status` | `affordable_now` / `affordable_with_plan` / `affordable_later` / `not_affordable` |
| `recommended_payment_method` | `full_payment` / `partial_payment` / `installments` / `wait` / `not_recommended` |
| `payment_plan` | `date:amount\|date:amount...` or `none` |
| `earliest_date_for_full_payment` | First date the full amount is forecast-safe |
| `spending_changes_needed` | Reserved field (see Limitations §3) |
| `decision_explanation` | Human-readable rationale for the recommendation |

## Testing from a Clean Setup

```bash
rm -rf .venv
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

Confirm `output.csv` is produced, its row count matches `requests.csv`,
and no exceptions are raised.