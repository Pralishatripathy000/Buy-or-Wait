from pathlib import Path
from calendar import monthrange
import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "dataset"
HORIZON = 90


def money(x):
    x = round(float(x) + 1e-9, 2)
    return str(int(x)) if x.is_integer() else f"{x:.2f}"


def parts(x):
    return set(str(x).split("|")) if pd.notna(x) and str(x) else set()


def add_month(d, n=1):
    y = d.year + (d.month - 1 + n) // 12
    m = (d.month - 1 + n) % 12 + 1
    return pd.Timestamp(y, m, min(d.day, monthrange(y, m)[1]))


def load():
    names = ["requests", "sample_requests", "financial_profiles", "financial_events",
             "request_payment_options", "messages", "images", "exchange_rates"]
    return {n: pd.read_csv(DATA / f"{n}.csv") for n in names}


def converted_amount(row, home, rates):
    amount = row.amount
    if pd.isna(amount):
        return None
    if row.currency == home:
        return float(amount)
    day = pd.Timestamp(row.settlement_date if pd.notna(row.settlement_date) else row.event_date)
    z = rates[(rates.from_currency == row.currency) & (rates.to_currency == home)]
    if z.empty:
        return None
    z = z.assign(delta=(pd.to_datetime(z.rate_date) - day).abs()).sort_values("delta")
    return float(amount) * float(z.iloc[0].rate)


def recurring_templates(history, home, rates):
    templates = []
    usable = history[(history.status == "settled") & history.amount.notna()].copy()
    usable["cash_date"] = pd.to_datetime(usable.settlement_date.fillna(usable.event_date))
    for _, g in usable.groupby(["direction", "category", "description"], dropna=False):
        g = g.sort_values("cash_date")
        if len(g) < 3:
            continue
        dates = g.cash_date.tolist()
        gaps = np.diff([x.toordinal() for x in dates[-5:]])
        monthly = len(gaps) >= 2 and np.median(gaps) >= 25 and np.median(gaps) <= 35
        weekly = len(gaps) >= 3 and np.median(gaps) >= 6 and np.median(gaps) <= 8
        if not monthly and not weekly:
            continue
        recent = g.tail(3)
        vals = [converted_amount(x, home, rates) for x in recent.itertuples()]
        vals = [x for x in vals if x is not None]
        if not vals:
            continue
        last = g.iloc[-1]
        templates.append({"event_id": last.event_id, "direction": last.direction,
                          "amount": float(np.median(vals)), "last": g.iloc[-1].cash_date,
                          "frequency": "monthly" if monthly else "weekly"})
    return templates


def base_cashflows(user, start, profile, events, rates):
    end = start + pd.Timedelta(days=HORIZON)
    home = profile.home_currency
    flows = {}
    history = events[(events.user_id == user) &
                     (pd.to_datetime(events.event_date) <= start)]
    future = events[(events.user_id == user)].copy()
    future["cash_date"] = pd.to_datetime(future.settlement_date.fillna(future.event_date))
    future = future[(future.cash_date >= start) & (future.cash_date <= end)]
    for row in future.itertuples():
        if row.status in {"cancelled", "failed", "unrealized"}:
            continue
        if row.status == "pending" and row.direction == "credit":
            continue
        if row.status not in {"pending", "scheduled"}:
            continue
        amount = converted_amount(row, home, rates)
        if amount is not None:
            flows[row.cash_date] = flows.get(row.cash_date, 0) + (amount if row.direction == "credit" else -amount)
    scheduled_keys = {(r.direction, r.category, r.description, r.cash_date) for r in future.itertuples()
                      if r.status in {"pending", "scheduled"}}
    for t in recurring_templates(history, home, rates):
        d = t["last"]
        while d < start:
            d = add_month(d) if t["frequency"] == "monthly" else d + pd.Timedelta(days=7)
        while d <= end:
            duplicate = any(k[0] == t["direction"] and k[3] == d for k in scheduled_keys)
            if not duplicate:
                flows[d] = flows.get(d, 0) + (t["amount"] if t["direction"] == "credit" else -t["amount"])
            d = add_month(d) if t["frequency"] == "monthly" else d + pd.Timedelta(days=7)
    return flows


def safe(flows, start, opening, floor, payments):
    changes = dict(flows)
    for d, amount in payments:
        d = pd.Timestamp(d)
        changes[d] = changes.get(d, 0) - float(amount)
    bal = float(opening)
    low = bal
    for d in pd.date_range(start, start + pd.Timedelta(days=HORIZON)):
        bal += changes.get(d, 0)
        low = min(low, bal)
    return low >= float(floor) - 0.005, low


def decide(req, profile, events, options, rates):
    start = pd.Timestamp(req.request_date)
    deadline = pd.Timestamp(req.desired_completion_date)
    requested = float(req.requested_amount)
    flows = base_cashflows(req.user_id, start, profile, events, rates)
    ok0, low0 = safe(flows, start, profile.current_available_balance,
                     profile.minimum_balance_to_keep, [])
    capacity = max(0.0, min(requested, low0 - float(profile.minimum_balance_to_keep))) if ok0 else 0.0
    capacity = round(capacity, 2)
    earliest = ""
    for d in pd.date_range(start, start + pd.Timedelta(days=HORIZON)):
        if safe(flows, start, profile.current_available_balance,
                profile.minimum_balance_to_keep, [(d, requested)])[0]:
            earliest = d.strftime("%Y-%m-%d")
            break
    accepted = parts(profile.payment_methods_user_will_consider)
    candidates = []
    if "full_payment" in accepted and capacity >= requested - 0.005:
        candidates.append((0, requested, 0, start, 1, "", "full_payment", [(start, requested)]))
    if bool(req.allows_partial_payment) and "partial_payment" in accepted and 0 < capacity < requested and earliest:
        d = pd.Timestamp(earliest)
        if d <= deadline:
            plan = [(start, capacity), (d, round(requested - capacity, 2))]
            if safe(flows, start, profile.current_available_balance, profile.minimum_balance_to_keep, plan)[0]:
                candidates.append((0, requested, 0, start, 2, "", "partial_payment", plan))
    ropts = options[options.request_id == req.request_id]
    if "installments" in accepted:
        for op in ropts[ropts.payment_method == "installments"].itertuples():
            if pd.notna(profile.max_installment_months) and op.number_of_payments > profile.max_installment_months:
                continue
            first = pd.Timestamp(op.first_payment_date)
            plan = [(first + pd.Timedelta(days=int(op.payment_frequency_days) * i), float(op.payment_amount))
                    for i in range(int(op.number_of_payments))]
            if plan[-1][0] <= deadline and safe(flows, start, profile.current_available_balance,
                                                profile.minimum_balance_to_keep, plan)[0]:
                candidates.append((0, float(op.total_payable_amount), 0, first, len(plan), op.payment_option_id,
                                   "installments", plan))
    if candidates:
        c = sorted(candidates, key=lambda x: x[:6])[0]
        method, plan = c[6], c[7]
        status = "affordable_now" if method == "full_payment" else "affordable_with_plan"
    elif earliest and pd.Timestamp(earliest) <= deadline and "full_payment" in accepted:
        method, status, plan = "wait", "affordable_later", []
    else:
        method, status, plan = "not_recommended", "not_affordable", []
    plan_text = "none" if not plan else "|".join(f"{pd.Timestamp(d).strftime('%Y-%m-%d')}:{money(a)}" for d, a in plan)
    cur = profile.home_currency
    if method == "full_payment":
        explanation = f"Pay {cur} {money(requested)} today while maintaining the minimum balance of {cur} {money(profile.minimum_balance_to_keep)}."
    elif method == "installments":
        explanation = f"Use {len(plan)} installments totaling {cur} {money(sum(a for _, a in plan))}; the 90-day forecast maintains the minimum balance."
    elif method == "partial_payment":
        explanation = f"Pay {cur} {money(plan[0][1])} today and the remaining {cur} {money(plan[1][1])} on {earliest}."
    elif method == "wait":
        explanation = f"Wait until {earliest}, when the full {cur} {money(requested)} is forecast safe without reducing protected spending."
    else:
        explanation = f"The full {cur} {money(requested)} cannot be completed safely within the deadline while maintaining {cur} {money(profile.minimum_balance_to_keep)}."
    return {"request_id": req.request_id, "amount_safe_to_pay": money(capacity),
            "affordability_status": status, "recommended_payment_method": method,
            "payment_plan": plan_text, "earliest_date_for_full_payment": earliest,
            "spending_changes_needed": "none", "decision_explanation": explanation}


def run(frame, db):
    profiles = db["financial_profiles"].set_index("user_id")
    rows = []
    for req in frame.itertuples():
        rows.append(decide(req, profiles.loc[req.user_id], db["financial_events"],
                           db["request_payment_options"], db["exchange_rates"]))
    return pd.DataFrame(rows)


if __name__ == "__main__":
    db = load()
    out = run(db["requests"], db)
    out.to_csv(ROOT / "output.csv", index=False)
    print(f"Wrote {len(out)} predictions to {ROOT / 'output.csv'}")
