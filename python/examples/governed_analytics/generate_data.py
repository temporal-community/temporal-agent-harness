"""Write the fictional bank's three DuckDB databases of synthetic data.

``python -m examples.governed_analytics.generate_data [--out DIR]`` (``just data`` from the
example's directory). Seeded, so every run writes the same data.

The data carries the demo's story: a handful of branches lost about a third of their deposit
volume in the last quarter (2026-Q3) while the rest held steady, and those same branches' loans
default about three times as often. The first half is in ``analytics``, which the agent may
read; the second is in ``risk``, which needs a grant. ``customer_pii`` holds the customers'
names, SSNs and account numbers, which the agent may never read. Every SSN is in the 9xx range,
which is never issued.
"""

import argparse
import datetime
import random
from pathlib import Path

import duckdb

from examples.governed_analytics.databases import DATABASES, data_dir

SEED = 20261026
QUARTERS = ["2025-Q4", "2026-Q1", "2026-Q2", "2026-Q3"]
LAST_QUARTER_START = datetime.date(2026, 7, 1)
BRANCH_COUNT = 40
TROUBLED_COUNT = 6

REGIONS = ["Northeast", "Southeast", "Midwest", "Southwest", "West"]
CITIES = {
    "Northeast": [("Boston", "MA"), ("Hartford", "CT"), ("Albany", "NY"), ("Newark", "NJ")],
    "Southeast": [("Charlotte", "NC"), ("Atlanta", "GA"), ("Tampa", "FL"), ("Richmond", "VA")],
    "Midwest": [("Columbus", "OH"), ("Chicago", "IL"), ("Madison", "WI"), ("Omaha", "NE")],
    "Southwest": [("Austin", "TX"), ("Phoenix", "AZ"), ("Tulsa", "OK"), ("Albuquerque", "NM")],
    "West": [("Denver", "CO"), ("Portland", "OR"), ("Sacramento", "CA"), ("Boise", "ID")],
}
PRODUCTS = ["checking", "savings", "credit_card", "auto_loan", "mortgage", "personal_loan"]
LOAN_PRODUCTS = ["auto_loan", "mortgage", "personal_loan"]
FIRST_NAMES = [
    "Avery", "Jordan", "Riley", "Casey", "Morgan", "Taylor", "Quinn", "Rowan", "Harper", "Emerson",
    "Sasha", "Dakota", "Reese", "Skyler", "Parker", "Hayden", "Elliot", "Finley", "Logan", "Blake",
]
LAST_NAMES = [
    "Alvarez", "Brooks", "Chen", "Dubois", "Eriksen", "Fischer", "Garcia", "Haddad", "Ito", "Jensen",
    "Kowalski", "Lindqvist", "Moreau", "Nakamura", "Okafor", "Patel", "Quintero", "Rossi", "Silva",
    "Tanaka",
]
STREETS = ["Oak", "Maple", "Cedar", "Pine", "Elm", "Birch", "Willow", "Spruce", "Ash", "Hickory"]

SCHEMAS = {
    "analytics": [
        "CREATE TABLE branches (branch_id INTEGER, branch_name VARCHAR, region VARCHAR, "
        "city VARCHAR, state VARCHAR, opened_year INTEGER)",
        "CREATE TABLE branch_deposits (branch_id INTEGER, quarter VARCHAR, "
        "deposit_volume DECIMAL(14, 2), deposit_accounts INTEGER)",
        "CREATE TABLE branch_metrics (branch_id INTEGER, quarter VARCHAR, new_accounts INTEGER, "
        "closed_accounts INTEGER, staff INTEGER, customer_satisfaction DOUBLE)",
        "CREATE TABLE product_performance (product VARCHAR, quarter VARCHAR, "
        "revenue DECIMAL(14, 2), active_accounts INTEGER)",
        "CREATE TABLE transactions_daily (branch_id INTEGER, day DATE, transactions INTEGER, "
        "volume DECIMAL(14, 2))",
    ],
    "risk": [
        "CREATE TABLE loan_applications (application_id INTEGER, applicant_id INTEGER, "
        "branch_id INTEGER, product VARCHAR, amount DECIMAL(12, 2), applied_on DATE, "
        "approved BOOLEAN, defaulted BOOLEAN)",
        "CREATE TABLE credit_scores (applicant_id INTEGER, score INTEGER, as_of DATE)",
    ],
    "customer_pii": [
        "CREATE TABLE customers (customer_id INTEGER, full_name VARCHAR, ssn VARCHAR, "
        "street_address VARCHAR, city VARCHAR, state VARCHAR, account_number VARCHAR, "
        "branch_id INTEGER)",
    ],
}


def generate(out: Path) -> None:
    """Write ``<database>.duckdb`` for each database into ``out``, replacing any already there."""
    rng = random.Random(SEED)
    out.mkdir(parents=True, exist_ok=True)
    tables = _tables(rng)
    for database in DATABASES:
        path = out / f"{database}.duckdb"
        for stale in (path, path.with_name(path.name + ".wal")):
            stale.unlink(missing_ok=True)
        with duckdb.connect(str(path)) as connection:
            for ddl in SCHEMAS[database]:
                connection.execute(ddl)
            for ddl in SCHEMAS[database]:
                table = ddl.split()[2]
                rows = tables[table]
                placeholders = ", ".join("?" * len(rows[0]))
                connection.executemany(f"INSERT INTO {table} VALUES ({placeholders})", rows)


def _tables(rng: random.Random) -> dict[str, list[tuple[object, ...]]]:
    branches = []
    for branch_id in range(1, BRANCH_COUNT + 1):
        region = REGIONS[(branch_id - 1) % len(REGIONS)]
        city, state = rng.choice(CITIES[region])
        branches.append(
            (branch_id, f"{city} {branch_id:02d}", region, city, state, rng.randint(1985, 2018))
        )
    troubled = set(rng.sample(range(1, BRANCH_COUNT + 1), TROUBLED_COUNT))

    deposits, metrics = [], []
    for branch_id, *_ in branches:
        volume = rng.uniform(8e6, 60e6)
        accounts = int(volume / rng.uniform(18_000, 30_000))
        for quarter in QUARTERS:
            if quarter == QUARTERS[-1] and branch_id in troubled:
                change = rng.uniform(0.62, 0.74)
            else:
                change = rng.gauss(1.01, 0.025)
            volume *= change
            accounts = max(50, int(accounts * (change + rng.gauss(0, 0.01))))
            deposits.append((branch_id, quarter, round(volume, 2), accounts))
            closed_rate = 0.09 if quarter == QUARTERS[-1] and branch_id in troubled else 0.03
            metrics.append(
                (
                    branch_id,
                    quarter,
                    int(accounts * rng.uniform(0.02, 0.05)),
                    int(accounts * rng.uniform(closed_rate * 0.8, closed_rate * 1.2)),
                    rng.randint(8, 30),
                    round(rng.uniform(3.2, 4.8), 2),
                )
            )

    products = []
    for product in PRODUCTS:
        revenue = rng.uniform(5e6, 40e6)
        active = rng.randint(20_000, 200_000)
        for quarter in QUARTERS:
            revenue *= rng.gauss(1.02, 0.03)
            active = int(active * rng.gauss(1.01, 0.02))
            products.append((product, quarter, round(revenue, 2), active))

    transactions = []
    for branch_id, *_ in branches:
        base = rng.randint(300, 1500)
        for offset in range(92):
            day = LAST_QUARTER_START + datetime.timedelta(days=offset)
            # The troubled branches' activity falls away over the quarter.
            fade = 1 - 0.4 * offset / 92 if branch_id in troubled else 1.0
            weekend = 0.55 if day.weekday() >= 5 else 1.0
            count = max(0, int(base * fade * weekend * rng.gauss(1, 0.08)))
            transactions.append((branch_id, day, count, round(count * rng.uniform(120, 480), 2)))

    applications, scores, customers = [], [], []
    applicant_id = 0
    for branch_id, _, _, city, state, _ in branches:
        for _ in range(rng.randint(90, 140)):
            applicant_id += 1
            score = max(480, min(850, int(rng.gauss(690, 70))))
            approved = score >= 600 and rng.random() < 0.85
            default_rate = 0.17 if branch_id in troubled else 0.05
            default_rate *= 1.6 if score < 650 else 0.8
            defaulted = approved and rng.random() < default_rate
            product = rng.choice(LOAN_PRODUCTS)
            amount = {
                "auto_loan": rng.uniform(8_000, 60_000),
                "mortgage": rng.uniform(120_000, 900_000),
                "personal_loan": rng.uniform(2_000, 40_000),
            }[product]
            applied_on = datetime.date(2025, 10, 1) + datetime.timedelta(days=rng.randint(0, 364))
            applications.append(
                (
                    applicant_id,  # one application per applicant, so the ids line up
                    applicant_id,
                    branch_id,
                    product,
                    round(amount, 2),
                    applied_on,
                    approved,
                    defaulted if approved else None,
                )
            )
            scores.append((applicant_id, score, applied_on))
            customers.append(
                (
                    applicant_id,
                    f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}",
                    f"9{rng.randint(0, 99):02d}-{rng.randint(1, 99):02d}-{rng.randint(1, 9999):04d}",
                    f"{rng.randint(10, 9999)} {rng.choice(STREETS)} St",
                    city,
                    state,
                    f"{rng.randint(10**9, 10**10 - 1)}",
                    branch_id,
                )
            )

    return {
        "branches": branches,
        "branch_deposits": deposits,
        "branch_metrics": metrics,
        "product_performance": products,
        "transactions_daily": transactions,
        "loan_applications": applications,
        "credit_scores": scores,
        "customers": customers,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=None, help="default: the example's data/")
    out = parser.parse_args().out or data_dir()
    generate(out)
    print(f"wrote {', '.join(f'{d}.duckdb' for d in DATABASES)} to {out}")


if __name__ == "__main__":
    main()
