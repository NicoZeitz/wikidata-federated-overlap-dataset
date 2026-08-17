"""Create a query input file from a SQL query and natural language description."""

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from db_utils import slugify
from geo_groups import COUNTRY_GROUPS

_DEFAULT_GT_DB = Path(__file__).parent.parent / "results" / "ground_truth.db"
_DEFAULT_DATASET = Path(__file__).parent.parent / "dataset"
_DEFAULT_INPUT_DIR = Path(__file__).parent.parent / "input"
_DEFAULT_COLUMNS_YAML = Path(__file__).parent.parent / "columns.yaml"


def _load_columns_meta(columns_yaml: Path) -> tuple[dict[str, str | None], dict[str, str]]:
    """Returns (split_cols, pk_to_table) from columns.yaml."""
    with columns_yaml.open() as f:
        data = yaml.safe_load(f)
    split_cols: dict[str, str | None] = {}
    pk_to_table: dict[str, str] = {}
    for table, info in data.items():
        if not isinstance(info, dict):
            continue
        meta = info.get("__meta__", {})
        split_cols[table] = meta.get("split_col")
        pk = meta.get("primary_key")
        if pk:
            pk_to_table[pk] = table
    return split_cols, pk_to_table


def _extract_tables(sql: str) -> list[str]:
    pattern = r"\b(?:FROM|JOIN)\s+[`\"\[]?([a-zA-Z_][a-zA-Z0-9_]*)[`\"\]]?"
    return list(dict.fromkeys(re.findall(pattern, sql, re.IGNORECASE)))


def _build_from_clause(sql: str) -> str:
    from_match = re.search(r"\bFROM\b", sql, re.IGNORECASE)
    if not from_match:
        return ""
    clause = sql[from_match.start() :]
    for kw in [r"\bGROUP\s+BY\b", r"\bHAVING\b", r"\bORDER\s+BY\b", r"\bLIMIT\b"]:
        m = re.search(kw, clause, re.IGNORECASE)
        if m:
            clause = clause[: m.start()]
    return clause.strip()


def _table_aliases(sql: str, table: str) -> list[str]:
    """Return all aliases used for `table` in the SQL, e.g. teams → ['t1', 't2']."""
    pattern = rf'\b{re.escape(table)}\s+(?:AS\s+)?([a-zA-Z_][a-zA-Z0-9_]*)\b'
    return list(dict.fromkeys(re.findall(pattern, sql, re.IGNORECASE)))


def _get_country_qids(
    conn: sqlite3.Connection,
    sql: str,
    tables: list[str],
    split_cols: dict[str, str | None] | None = None,
    pk_to_table: dict[str, str] | None = None,
) -> dict[str, set[str]]:
    """Returns per-table country QIDs: {table: {qid, ...}}."""
    from_clause = _build_from_clause(sql)
    if not from_clause:
        return {}
    result: dict[str, set[str]] = {}
    for table in tables:
        qids: set[str] = set()
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
        if "country" in cols:
            aliases = _table_aliases(sql, table)
            candidates = [f"{a}.country" for a in aliases] + [f'"{table}".country', "country"]
            for country_expr in candidates:
                try:
                    rows = conn.execute(f"SELECT DISTINCT {country_expr} {from_clause}").fetchall()
                    for (qid,) in rows:
                        if qid:
                            qids.add(qid)
                    break
                except sqlite3.Error:
                    continue
        elif split_cols and pk_to_table:
            table_split = split_cols.get(table)
            if table_split and table_split != "country":
                intermediate_table = pk_to_table.get(table_split)
                if intermediate_table and split_cols.get(intermediate_table) == "country":
                    intermediate_qids: set[str] = set()
                    for split_expr in (f'"{table}".{table_split}', table_split):
                        try:
                            rows = conn.execute(f"SELECT DISTINCT {split_expr} {from_clause}").fetchall()
                            intermediate_qids = {r[0] for r in rows if r[0]}
                            break
                        except sqlite3.Error:
                            continue
                    if intermediate_qids:
                        placeholders = ",".join("?" * len(intermediate_qids))
                        country_rows = conn.execute(
                            f'SELECT DISTINCT country FROM "{intermediate_table}"'
                            f' WHERE "{table_split}" IN ({placeholders})',
                            list(intermediate_qids),
                        ).fetchall()
                        for (qid,) in country_rows:
                            if qid:
                                qids.add(qid)
        result[table] = qids
    return result


def _get_continent_names(
    conn: sqlite3.Connection,
    sql: str,
    tables: list[str],
) -> set[str]:
    """Returns continent names referenced in the query via the continents table."""
    if "continents" not in tables:
        return set()
    from_clause = _build_from_clause(sql)
    if not from_clause:
        return set()
    aliases = _table_aliases(sql, "continents")
    candidates = [f"{a}.name" for a in aliases] + ['"continents".name', "continents.name"]
    names: set[str] = set()
    for name_expr in candidates:
        try:
            rows = conn.execute(f"SELECT DISTINCT {name_expr} {from_clause}").fetchall()
            names |= {r[0] for r in rows if r[0]}
        except sqlite3.Error:
            continue
    return names


def _qids_to_slugs(conn: sqlite3.Connection, qids: set[str]) -> set[str]:
    if not qids:
        return set()
    placeholders = ",".join("?" * len(qids))
    rows = conn.execute(
        f"SELECT name FROM countries WHERE country IN ({placeholders})",
        list(qids),
    ).fetchall()
    return {slugify(name) for (name,) in rows}


def _resolve_locations(specs: list[str], regions: dict[str, set[str]]) -> set[str]:
    """Expand location specs (slugs, region names, or 'all') to a set of country slugs."""
    if specs == ["all"]:
        return set().union(*regions.values())
    result: set[str] = set()
    for loc in specs:
        key = slugify(loc)
        if key in regions:
            result |= regions[key]
        else:
            result.add(key)
    return result


def _build_regions() -> dict[str, set[str]]:
    name_to_slug = {name: slugify(name) for name in COUNTRY_GROUPS}
    group_to_slugs: dict[str, set[str]] = {}
    for country_name, groups in COUNTRY_GROUPS.items():
        slug = name_to_slug.get(country_name)
        if slug is None:
            continue
        for group in groups:
            group_to_slugs.setdefault(group, set()).add(slug)
    regions: dict[str, set[str]] = {g: s for g, s in group_to_slugs.items() if s}
    for slug in name_to_slug.values():
        regions[slug] = {slug}
    return regions


def _agent_base_slug(folder_name: str) -> str:
    m = re.match(r"^(.+)_\d{2,}$", folder_name)
    return m.group(1) if m else folder_name


def _load_mapping(db_dir: Path) -> dict | None:
    slug = db_dir.name
    mapping_file = db_dir / f"{slug}.mapping.yaml"
    if not mapping_file.exists():
        return None
    with mapping_file.open() as f:
        return yaml.safe_load(f)


def _scan_agents(dataset_path: Path) -> list[tuple[str, dict]]:
    agents = []
    if not dataset_path.exists():
        return agents
    for d in sorted(dataset_path.iterdir()):
        if not d.is_dir():
            continue
        mapping = _load_mapping(d)
        if mapping is None:
            continue
        agents.append((d.name, mapping))
    return agents


def _find_relevant_agents(
    agents: list[tuple[str, dict]],
    table_locations: dict[str, set[str]],
    regions: dict[str, set[str]],
) -> list[tuple[str, str, set[str], set[str]]]:
    """Returns list of (agent_name, folder_name, matched_tables, matched_countries)."""
    relevant = []
    for i, (folder_name, mapping) in enumerate(agents):
        agent_name = f"A{i + 1}"
        base_slug = _agent_base_slug(folder_name)
        agent_countries = regions.get(base_slug, {base_slug})
        matched_tables: set[str] = set()
        matched_countries: set[str] = set()
        for table, loc_slugs in table_locations.items():
            if table not in mapping:
                continue
            overlap = agent_countries & loc_slugs
            if overlap:
                matched_tables.add(table)
                matched_countries |= overlap
        if matched_tables:
            relevant.append((agent_name, folder_name, matched_tables, matched_countries))
    return relevant


def _serialize_answer(rows: list[tuple], col_names: list[str]) -> object:
    if len(rows) == 1 and len(rows[0]) == 1:
        return rows[0][0]
    return [dict(zip(col_names, row, strict=False)) for row in rows]


def run_script(args: argparse.Namespace) -> None:
    if args.location is not None and "all" in args.location and len(args.location) > 1:
        sys.exit("Error: 'all' cannot be combined with other --location values")

    split_cols, pk_to_table = _load_columns_meta(_DEFAULT_COLUMNS_YAML)
    regions = _build_regions()

    table_location_overrides: dict[str, list[str]] = {}
    for spec in args.table_location or []:
        if "=" not in spec:
            sys.exit(f"Error: --table-location requires TABLE=LOCATION format, got: {spec!r}")
        table, _, loc_str = spec.partition("=")
        table_location_overrides.setdefault(table.strip(), []).extend(loc_str.strip().split(","))

    conn = sqlite3.connect(f"file:{args.ground_truth_db}?mode=ro", uri=True)
    try:
        cursor = conn.execute(args.sql_query)
        rows = cursor.fetchall()
        col_names = [d[0] for d in cursor.description] if cursor.description else []
        full_answer = _serialize_answer(rows, col_names)

        sql_tables = set(args.tables) if args.tables else set(_extract_tables(args.sql_query))
        per_table_qids = _get_country_qids(conn, args.sql_query, list(sql_tables), split_cols, pk_to_table)
        autodetect_slugs: dict[str, set[str]] = {
            table: _qids_to_slugs(conn, qids) for table, qids in per_table_qids.items()
        }
        continent_names = _get_continent_names(conn, args.sql_query, list(sql_tables))
        if continent_names:
            autodetect_slugs["continents"] = _resolve_locations(list(continent_names), regions)
    finally:
        conn.close()

    table_locations: dict[str, set[str]] = {}
    for table in sql_tables:
        if table in table_location_overrides:
            table_locations[table] = _resolve_locations(table_location_overrides[table], regions)
        elif args.location is not None:
            table_locations[table] = _resolve_locations(args.location, regions)
        else:
            table_locations[table] = autodetect_slugs.get(table, set())

    all_relevant_slugs = set().union(*table_locations.values()) if table_locations else set()

    print("=========================================================================================================")
    print(f"Query:             {args.nl_query}")
    print(f"SQL tables:        {sorted(sql_tables)}")
    for table in sorted(table_locations):
        slugs = table_locations[table]
        print(f"  {table}: {sorted(slugs)}")
    print(f"Full answer:       {full_answer}")

    agents = _scan_agents(args.dataset_path)

    if not agents:
        print(f"Warning: no agents found in {args.dataset_path}")

    dataset_basename = args.dataset_path.name
    data_agents = [
        {
            "type": "sql",
            "name": f"A{i + 1}",
            "path": f"{dataset_basename}/{folder_name}/{folder_name}.db",
        }
        for i, (folder_name, _) in enumerate(agents)
    ]

    relevant_agents = _find_relevant_agents(agents, table_locations, regions)
    relevant_agent_names = [name for name, *_ in relevant_agents]

    if not relevant_agents:
        msg = f"No relevant agents found (tables={sql_tables}, locations={all_relevant_slugs})"
        if not args.force:
            sys.exit(f"Error: {msg}")
        print(f"Warning: {msg}", file=sys.stderr)

    print(f"Relevant agents:    {len(relevant_agents)}/{len(agents)}")
    if args.verbose:
        for agent_name, folder_name, matched_tables, matched_countries in relevant_agents:
            tables_str = sorted(matched_tables)
            locations_str = sorted(matched_countries)
            print(f"  {agent_name}  {folder_name}.db  tables={tables_str}  locations={locations_str}")

    output = {
        "extra": {
            "relevant_data_agents": relevant_agent_names,
            "full_answer": full_answer,
            "sql_query": args.sql_query,
            "query_plan": {
                "query": args.nl_query,
                "steps": [
                    {
                        "id": "s1",
                        "query": f"TODO: {args.nl_query}",
                        "to_agent": relevant_agent_names,
                        "dependencies": [],
                        "merge_task": "TODO:",
                        "answer": None,
                    }
                ],
            },
        },
        "value": {
            "__type__": "CollaborativeQueryAnswering",
            "query": args.nl_query,
            "data_agents": data_agents,
        },
    }

    args.input_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^\w\s-]", "", args.nl_query).strip()
    safe_name = re.sub(r"\s+", "_", safe_name)[:80]
    out_path = args.input_dir / f"{safe_name}.json"
    if out_path.exists() and not args.overwrite:
        sys.exit(f"Error: {out_path} already exists. Use --overwrite to replace.")
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    print(f"Written: {out_path}")

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a query input file for the wikidata2 dataset.")
    parser.add_argument(
        "--ground-truth-db",
        type=Path,
        default=_DEFAULT_GT_DB,
        metavar="DB",
        help="Path to ground_truth.db",
    )
    parser.add_argument("--sql-query", required=True, metavar="SQL")
    parser.add_argument("--nl-query", required=True, metavar="QUERY")
    parser.add_argument(
        "--dataset-path",
        type=Path,
        default=_DEFAULT_DATASET,
        metavar="PATH",
        help="Path to the generated dataset folder",
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=_DEFAULT_INPUT_DIR,
        metavar="DIR",
        help="Output directory for the generated JSON file",
    )
    parser.add_argument(
        "--tables",
        nargs="+",
        metavar="TABLE",
        default=None,
        help="Override auto-detected tables (space-separated table names)",
    )
    parser.add_argument(
        "--location",
        nargs="+",
        metavar="LOCATION",
        default=None,
        help=(
            "Default location override for all tables: country slugs, region names, or 'all'. "
            "Overridden per-table by --table-location."
        ),
    )
    parser.add_argument(
        "--table-location",
        action="append",
        metavar="TABLE=LOCATION",
        default=None,
        help=(
            "Per-table location override, repeat for each table "
            "(e.g. --table-location matches=all --table-location teams=united-kingdom). "
            "Takes precedence over --location for specified tables."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Write output file even if no relevant agents are found",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite output file if it already exists",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print each relevant agent with matched tables and countries",
    )
    return parser.parse_args()


if __name__ == "__main__":
    run_script(_parse_args())
