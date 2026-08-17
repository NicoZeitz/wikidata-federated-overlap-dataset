"""Build results/ground_truth.db from per-entity CSVs in results/."""

import sqlite3
from pathlib import Path

import pandas as pd
import yaml
from db_utils import strip_uri

_RESULTS_DIR = Path(__file__).parent.parent / "results"
_GT_DB = _RESULTS_DIR / "ground_truth.db"
_COLUMNS_YAML = Path(__file__).parent.parent / "columns.yaml"

_NUMERIC_CAST_THRESHOLD = 0.8


def _find_csvs(results_dir: Path) -> list[Path]:
    return sorted(results_dir.glob("*.csv"))


def _load_csv(csv_path: Path) -> pd.DataFrame:
    return pd.read_csv(csv_path, dtype=str, keep_default_na=True)


def _cast_numeric(df: pd.DataFrame) -> pd.DataFrame:
    for col in df.columns:
        numeric: pd.Series = pd.to_numeric(df[col], errors="coerce")  # type: ignore[assignment]
        nonnull = df[col].notna().sum()
        if nonnull > 0 and numeric.notna().sum() / nonnull >= _NUMERIC_CAST_THRESHOLD:
            df[col] = numeric
    return df


def _strip_uris(df: pd.DataFrame) -> pd.DataFrame:
    for col in df.select_dtypes(include="str").columns:
        df[col] = df[col].map(strip_uri)
    return df


def _write_table(conn: sqlite3.Connection, df: pd.DataFrame, table_name: str) -> None:
    df.to_sql(table_name, conn, if_exists="replace", index=False)
    print(f"  '{table_name}': {len(df)} rows, {len(df.columns)} columns")


def _load_and_write(csv_path: Path, conn: sqlite3.Connection) -> None:
    table_name = csv_path.stem
    print(f"Loading {csv_path.name}")
    df = _load_csv(csv_path)
    df = _cast_numeric(df)
    df = _strip_uris(df)
    _write_table(conn, df, table_name)


def _build_fk_list(cfg: dict) -> list[tuple[str, str, str, str]]:
    pks: dict[str, str] = {}
    for table, meta in cfg.items():
        if not isinstance(meta, dict):
            continue
        pk = meta.get("__meta__", {}).get("primary_key")
        if pk:
            pks[table] = pk
    fks = []
    for table, meta in cfg.items():
        if not isinstance(meta, dict):
            continue
        for col, ref_table in meta.get("__meta__", {}).get("foreign_keys", {}).items():
            ref_col = pks.get(ref_table)
            if ref_col:
                fks.append((table, col, ref_table, ref_col))
    return fks


def _check_db(conn: sqlite3.Connection, cfg: dict) -> None:
    """Check null columns, FK violations, and country coverage per table."""
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    existing = {r[0] for r in cursor.fetchall()}

    fk_list = _build_fk_list(cfg)

    # Create indices on FK target columns for performance
    for _, _, ref_table, ref_col in fk_list:
        if ref_table in existing:
            cursor.execute(f'CREATE INDEX IF NOT EXISTS "idx_{ref_table}_{ref_col}" ON "{ref_table}"("{ref_col}")')
    conn.commit()

    print("\n--- DB Checks ---")
    errors: list[str] = []

    # 1. All-NULL columns
    for table in sorted(existing):
        cursor.execute(f'SELECT COUNT(*) FROM "{table}"')
        total = cursor.fetchone()[0]
        if total == 0:
            continue
        cursor.execute(f'PRAGMA table_info("{table}")')
        cols = [r[1] for r in cursor.fetchall()]
        for col in cols:
            cursor.execute(f'SELECT COUNT(*) FROM "{table}" WHERE "{col}" IS NOT NULL')
            if cursor.fetchone()[0] == 0:
                errors.append(f"All-NULL: {table}.{col} ({total} rows)")

    # 2. FK violations (NULL is allowed; non-NULL must exist in target)
    for table, col, ref_table, ref_col in fk_list:
        if table not in existing or ref_table not in existing:
            continue
        cursor.execute(f'PRAGMA table_info("{table}")')
        if col not in {r[1] for r in cursor.fetchall()}:
            continue
        cursor.execute(f'SELECT COUNT(*) FROM "{table}" WHERE "{col}" IS NOT NULL')
        total = cursor.fetchone()[0]
        cursor.execute(
            f'SELECT COUNT(*) FROM "{table}" a '
            f'WHERE a."{col}" IS NOT NULL '
            f'AND NOT EXISTS (SELECT 1 FROM "{ref_table}" b WHERE b."{ref_col}"=a."{col}")'
        )
        bad = cursor.fetchone()[0]
        if bad > 0:
            errors.append(f"FK broken: {table}.{col}->{ref_table}: {bad}/{total} ({100 * bad / total:.1f}%)")

    # 3. Country coverage: each table (except countries/continents) must have
    #    a direct or 1-hop indirect FK path to the countries table
    fk_graph: dict[str, set[str]] = {}
    for table, _col, ref_table, _ in fk_list:
        fk_graph.setdefault(table, set()).add(ref_table)

    skip = {"countries", "continents"}
    for table in sorted(existing):
        if table in skip:
            continue
        neighbours = fk_graph.get(table, set())
        has_country = "countries" in neighbours or any("countries" in fk_graph.get(dep, set()) for dep in neighbours)
        if not has_country:
            errors.append(f"No country reference: {table}")

    if errors:
        for e in errors:
            print(f"  {e}")
    else:
        print("  All checks passed.")
    print()


def run() -> None:
    """Pipeline: find CSVs → load → cast numeric → strip URIs → write to ground_truth.db."""
    csvs = _find_csvs(_RESULTS_DIR)
    if not csvs:
        print(f"No CSVs found in {_RESULTS_DIR}. Run fetch_all.sh first.")
        return

    cfg: dict = {}
    if _COLUMNS_YAML.exists():
        with _COLUMNS_YAML.open() as f:
            cfg = yaml.safe_load(f) or {}

    _GT_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_GT_DB)
    try:
        for csv_path in csvs:
            _load_and_write(csv_path, conn)
        conn.commit()
        _check_db(conn, cfg)
    finally:
        conn.close()
    print(f"Done → {_GT_DB}")


if __name__ == "__main__":
    run()
