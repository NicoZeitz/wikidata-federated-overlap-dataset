"""Ground truth loading: per-topic table data, row keys, row country sets."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from wikidata_federated_overlap_dataset.config import TableConfig

RowKey = tuple


@dataclass(frozen=True)
class Country:
    """One country from the countries table."""

    id: int
    name: str
    iso2: str
    continent: int
    lon: float
    lat: float


@dataclass(frozen=True)
class CountryTable:
    """All countries with lookup by id."""

    rows: list[Country]
    by_id: dict[int, Country]
    continent_countries: dict[int, frozenset[int]]


@dataclass
class TableData:
    """All rows of one ground truth table plus derived per-row data."""

    name: str
    columns: list[str]
    rows: list[tuple]
    row_keys: list[RowKey]
    row_country_sets: list[frozenset[int]]
    row_values: list[float]


@dataclass
class GroundTruth:
    """The loaded ground truth database content."""

    countries: CountryTable
    tables: dict[str, TableData]


def load_countries(db_path: Path) -> CountryTable:
    """Load all countries with coordinates parsed from the POINT(lon lat) string."""
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT country, name, iso2, continent, coords FROM countries"
        ).fetchall()
    finally:
        con.close()
    countries = []
    for cid, name, iso2, continent, coords in rows:
        lon, lat = _parse_point(coords)
        countries.append(
            Country(id=cid, name=name, iso2=iso2, continent=continent, lon=lon, lat=lat)
        )
    by_id = {c.id: c for c in countries}
    continent_countries: dict[int, set[int]] = {}
    for c in countries:
        continent_countries.setdefault(c.continent, set()).add(c.id)
    frozen = {k: frozenset(v) for k, v in continent_countries.items()}
    return CountryTable(rows=countries, by_id=by_id, continent_countries=frozen)


def _parse_point(coords: str | None) -> tuple[float, float]:
    """Parse a 'POINT(lon lat)' string into a (lon, lat) float pair."""
    if coords is None:
        return 0.0, 0.0
    inner = coords.removeprefix("POINT(").removesuffix(")")
    lon, lat = inner.split()
    return float(lon), float(lat)


def _build_query(table_name: str, split_col: str | None, foreign_keys: dict[str, str]) -> str:
    """Build the SELECT query resolving the row country via split_col.

    Direct case: split_col holds the country id itself (its FK target is `countries`
    or it has no FK entry). Join case: split_col references another table whose
    `country` column is the row's country (e.g. matches.venue -> venues.country).
    """
    if split_col is None:
        return f'SELECT t.*, NULL AS __country__ FROM "{table_name}" t'
    ref_table = foreign_keys.get(split_col)
    if ref_table is None or ref_table == "countries":
        return f'SELECT t.*, t."{split_col}" AS __country__ FROM "{table_name}" t'
    return (
        f'SELECT t.*, ref."country" AS __country__ FROM "{table_name}" t '
        f'JOIN "{ref_table}" ref ON t."{split_col}" = ref."{split_col}"'
    )


def load_table(
    db_path: Path,
    table: TableConfig,
    countries: CountryTable,
) -> TableData:
    """Load one table: rows, row keys (PK or full row), and row country sets."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        columns = [r[1] for r in con.execute(f'PRAGMA table_info("{table.name}")')]
        query = _build_query(table.name, table.split_col, table.foreign_keys)
        raw_rows = [tuple(r) for r in con.execute(query)]
    finally:
        con.close()
    pk_index = columns.index(table.primary_key) if table.primary_key in columns else None
    row_keys = [row if pk_index is None else (row[pk_index],) for row in raw_rows]
    if table.split_col is None:
        row_country_sets = _continent_country_sets(table, raw_rows, columns, countries)
    else:
        row_country_sets = []
        for row in raw_rows:
            country_value = row[-1]
            row_country_sets.append(
                frozenset() if country_value is None else frozenset({int(country_value)})
            )
    value_index = columns.index(table.value_column) if table.value_column in columns else None
    if table.value_inverted and value_index is not None:
        # Inverted value columns (e.g. ranking: lower = better known) are
        # negated so that a higher value always means better known (R4b).
        # Nulls rank worst: they get one below the lowest negated value, so
        # every known row beats every unknown row.
        known = [-float(row[value_index]) for row in raw_rows if row[value_index] is not None]
        null_value = (min(known) - 1.0) if known else 0.0
        row_values = [
            -float(row[value_index]) if row[value_index] is not None else null_value
            for row in raw_rows
        ]
    else:
        row_values = [
            float(row[value_index])
            if value_index is not None and row[value_index] is not None
            else 0.0
            for row in raw_rows
        ]
    return TableData(
        name=table.name,
        columns=columns,
        rows=[row[:-1] for row in raw_rows],
        row_keys=row_keys,
        row_country_sets=row_country_sets,
        row_values=row_values,
    )


def _continent_country_sets(
    table: TableConfig,
    raw_rows: list[tuple],
    columns: list[str],
    countries: CountryTable,
) -> list[frozenset[int]]:
    """Resolve country sets for split_col=null tables via the row's continent."""
    continent_index = columns.index("continent")
    sets = []
    for row in raw_rows:
        continent = row[continent_index]
        sets.append(countries.continent_countries.get(continent, frozenset()))
    return sets


def load_ground_truth(db_path: Path, tables: dict) -> GroundTruth:
    """Load the countries table plus every configured table."""
    country_table = load_countries(db_path)
    loaded = {name: load_table(db_path, table, country_table) for name, table in tables.items()}
    return GroundTruth(countries=country_table, tables=loaded)
