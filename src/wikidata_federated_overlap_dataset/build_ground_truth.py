"""Build results/ground_truth.db from per-entity CSVs in results/."""

import sqlite3
from pathlib import Path

import pandas as pd
import yaml

_REPO_ROOT = Path(__file__).parent.parent.parent
_RESULTS_DIR = _REPO_ROOT / "results"
_GT_DB = _RESULTS_DIR / "ground_truth.db"
_COLUMNS_YAML = _REPO_ROOT / "config" / "columns.yaml"

_NUMERIC_CAST_THRESHOLD = 0.8

_WIKIDATA_URI_PREFIX = "http://www.wikidata.org/entity/Q"


def strip_uri(value: object) -> object:
    """Strip Wikidata entity URI prefix and the 'Q' prefix, keeping only the
    numeric ID (e.g. 'http://www.wikidata.org/entity/Q30' -> '30')."""
    if isinstance(value, str):
        return value.removeprefix(_WIKIDATA_URI_PREFIX)
    return value


def _find_csvs(results_dir: Path) -> list[Path]:
    return sorted(results_dir.glob("*.csv"))


def _load_csv(csv_path: Path) -> pd.DataFrame:
    return pd.read_csv(csv_path, dtype=str, keep_default_na=True)


def _cast_numeric(df: pd.DataFrame) -> pd.DataFrame:
    for col in df.columns:
        numeric: pd.Series = pd.to_numeric(df[col], errors="coerce")  # type: ignore[assignment]
        nonnull = df[col].notna().sum()
        if nonnull > 0 and numeric.notna().sum() / nonnull >= _NUMERIC_CAST_THRESHOLD:
            if (numeric.dropna() % 1 == 0).all():
                # Integer-valued (nullable Int64 keeps NULLs without promoting to float).
                df[col] = numeric.astype("Int64")
            else:
                df[col] = numeric
    return df


def _strip_uris(df: pd.DataFrame) -> pd.DataFrame:
    for col in df.select_dtypes(include="str").columns:
        df[col] = df[col].map(strip_uri)
    return df


def _write_table(conn: sqlite3.Connection, df: pd.DataFrame, table_name: str) -> None:
    df.to_sql(table_name, conn, if_exists="replace", index=False)
    print(f"  '{table_name}': {len(df)} rows, {len(df.columns)} columns")


def _load_and_write(csv_path: Path, conn: sqlite3.Connection, primary_keys: dict[str, str]) -> None:
    table_name = csv_path.stem
    print(f"Loading {csv_path.name}")
    df = _load_csv(csv_path)
    # Strip URIs/QIDs BEFORE numeric casting, so ID columns like countries.country
    # (URIs at load time) end up as INTEGER columns in the DB.
    df = _strip_uris(df)
    df = _cast_numeric(df)
    # Strict: duplicates on the primary key must be fixed at fetch level
    # (dedupe in the SPARQL query), not silently dropped here.
    pk = primary_keys.get(table_name)
    if pk and pk in df.columns:
        dupes = df[pk][df[pk].duplicated()].nunique()
        if dupes:
            examples = df.loc[df[pk].duplicated(), pk].head(5).tolist()
            raise SystemExit(
                f"[{table_name}] {dupes} duplicate primary key value(s) in '{pk}' "
                f"(examples: {examples}) — fix the fetch query to dedupe"
            )
    _write_table(conn, df, table_name)


def _build_fk_list(cfg: dict[str, object]) -> list[tuple[str, str, str, str]]:
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


def _check_db(conn: sqlite3.Connection, cfg: dict[str, object]) -> None:
    """Check null columns, FK violations, and country coverage per table."""
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    existing = {r[0] for r in cursor.fetchall()}

    fk_list = _build_fk_list(cfg)

    # Create indices on FK target columns for performance
    for _, _, ref_table, ref_col in fk_list:
        if ref_table in existing:
            index = f'CREATE INDEX IF NOT EXISTS "idx_{ref_table}_{ref_col}"'
            cursor.execute(f'{index} ON "{ref_table}"("{ref_col}")')

    # Create indices on split_col for each table (used by load_tables per-key queries)
    for table, table_cfg in cfg.items():
        if not isinstance(table_cfg, dict):
            continue
        split_col = table_cfg.get("__meta__", {}).get("split_col")
        if split_col and table in existing:
            cursor.execute(
                f'CREATE INDEX IF NOT EXISTS "idx_{table}_{split_col}" ON "{table}"("{split_col}")'
            )
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
            errors.append(
                f"FK broken: {table}.{col}->{ref_table}: {bad}/{total} ({100 * bad / total:.1f}%)"
            )

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
        has_country = "countries" in neighbours or any(
            "countries" in fk_graph.get(dep, set()) for dep in neighbours
        )
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

    cfg: dict[str, object] = {}
    if _COLUMNS_YAML.exists():
        with _COLUMNS_YAML.open() as f:
            cfg = yaml.safe_load(f) or {}

    primary_keys = {
        table: meta["__meta__"]["primary_key"]  # type: ignore[index]
        for table, meta in cfg.items()
        if isinstance(meta, dict) and meta.get("__meta__", {}).get("primary_key")
    }

    _GT_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_GT_DB)
    try:
        for csv_path in csvs:
            _load_and_write(csv_path, conn, primary_keys)
        conn.commit()
        _check_db(conn, cfg)
    finally:
        conn.close()
    print(f"Done → {_GT_DB}")


def main() -> None:
    """Entry point: build results/ground_truth.db from the fetched CSVs."""
    run()


if __name__ == "__main__":
    main()
