"""Validation: in-build checks + post-build validation over the written databases."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from wikidata_federated_overlap_dataset.config import BuildConfig, TableConfig, TopicConfig
from wikidata_federated_overlap_dataset.ground_truth import TableData
from wikidata_federated_overlap_dataset.planner import (
    TopicPlan,
    band_bounds,
    greedy_order,
    head_size,
)


@dataclass
class ValidationReport:
    """Collected validation results for one topic."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def error(self, message: str) -> None:
        """Record a validation error."""
        self.errors.append(message)

    def warn(self, message: str) -> None:
        """Record a validation warning."""
        self.warnings.append(message)

    @property
    def ok(self) -> bool:
        """True if no errors were recorded."""
        return not self.errors


def validate_plan(
    topic: TopicConfig,
    plan: TopicPlan,
    tables_data: dict[str, TableData],
    config: BuildConfig,
) -> ValidationReport:
    """In-build validation of the TopicPlan before writing."""
    report = ValidationReport()
    target = len(plan.databases)
    for name, table_plan in plan.tables.items():
        data = tables_data[name]
        holders_per_row = _holders_per_row(table_plan.instances, target)
        for r, count in enumerate(table_plan.assigned_counts):
            holders = holders_per_row[r]
            if len(holders) != count:
                report.error(
                    f"{name}: row {r} appears in {len(holders)} dbs, assigned count is {count}"
                )
            if len(set(holders)) != len(holders):
                report.error(f"{name}: row {r} has duplicate instances in one database")
            for db in holders:
                country_set = data.row_country_sets[r]
                region = plan.databases[db].region
                if country_set and not (region.countries & country_set):
                    report.error(
                        f"{name}: row {r} in db {db} ({region.slug}) violates region assignment"
                    )
        order = greedy_order(plan.databases, table_plan.instances)
        front = order[: head_size(target)]
        coverage = len(set().union(*(set(table_plan.instances[db]) for db in front)))
        lower, upper = band_bounds(len(data.rows))
        if table_plan.band_lower is not None:
            lower = min(lower, table_plan.band_lower)
        if not lower <= coverage <= upper:
            report.error(f"{name}: head coverage {coverage} outside band [{lower}, {upper}]")
    return report


def _holders_per_row(instances: dict[int, list[int]], target: int) -> list[list[int]]:
    """Invert the per-database instance lists: row -> databases holding it (O(total))."""
    total = sum(len(rows) for rows in instances.values())
    holders: list[list[int]] = [[] for _ in range(total)]
    for db in range(target):
        for r in instances[db]:
            holders[r].append(db)
    return holders


def validate_written_topic(
    topic: TopicConfig,
    folders: list[Path],
    plan: TopicPlan,
    tables_data: dict[str, TableData],
    tables_config: dict[str, TableConfig],
    config: BuildConfig,
) -> ValidationReport:
    """Post-build validation: reopen the written .db files and check everything."""
    report = validate_plan(topic, plan, tables_data, config)
    if len(folders) != config.target:
        report.error(f"expected {config.target} databases, found {len(folders)}")
    for name in topic.tables:
        table_config = tables_config[name]
        data = tables_data[name]
        placed: dict[tuple, int] = {}
        per_db_counts: dict[str, int] = {}
        for folder in folders:
            _validate_written_table(folder, name, table_config, data, placed, per_db_counts, report)
        if table_config.primary_key is None:
            _validate_pkless_counts(name, folders, per_db_counts, plan, report)
            continue
        source_keys = set(data.row_keys)
        missing = source_keys - set(placed)
        if missing:
            report.error(f"{name}: {len(missing)} rows missing from all databases")
        multiset = sorted(placed.values())
        if multiset != sorted(plan.tables[name].assigned_counts):
            report.warn(f"{name}: placed instance multiset differs from assigned counts")
    return report


def _validate_pkless_counts(
    name: str,
    folders: list[Path],
    per_db_counts: dict[str, int],
    plan: TopicPlan,
    report: ValidationReport,
) -> None:
    """PK-less tables are validated by per-database row counts (keys are unstable
    under column drops), against the planned instance counts."""
    for db_index, rows in plan.tables[name].instances.items():
        folder = folders[db_index]
        written = per_db_counts.get(folder.name, 0)
        if written != len(rows):
            report.error(f"{name}: {folder.name} holds {written} rows, plan says {len(rows)}")


def _validate_written_table(
    folder: Path,
    table_name: str,
    table_config: TableConfig,
    data: TableData,
    placed: dict[tuple, int],
    per_db_counts: dict[str, int],
    report: ValidationReport,
) -> None:
    """Validate one table inside one written database: schema, rows, uniqueness."""
    db_files = list(folder.glob("*.db"))
    if not db_files:
        report.error(f"{folder.name}: no .db file")
        return
    con = sqlite3.connect(db_files[0])
    con.row_factory = sqlite3.Row
    try:
        mapping_path = next(folder.glob("*.mapping.yaml"))
        mapping = yaml.safe_load(mapping_path.read_text())
        phys = None
        for physical_name, spec in mapping.items():
            if spec["source"] == table_name:
                phys = (physical_name, spec)
                break
        if phys is None:
            report.error(f"{folder.name}: table {table_name} missing")
            return
        physical_name, spec = phys
        cols = [r[1] for r in con.execute(f'PRAGMA table_info("{physical_name}")')]
        if not cols:
            report.error(f"{folder.name}/{physical_name}: empty schema")
            return
        rows = [tuple(r) for r in con.execute(f'SELECT * FROM "{physical_name}"')]
        if not rows:
            report.error(f"{folder.name}/{physical_name}: empty table")
            return
        per_db_counts[folder.name] = len(rows)
        source_cols = list(spec["columns"].values())
        pk_source = table_config.primary_key
        if pk_source is None or pk_source not in source_cols:
            return
        key_index = source_cols.index(pk_source)
        seen: set[tuple] = set()
        for row in rows:
            key = (row[key_index],)
            if key in seen:
                report.error(f"{folder.name}/{physical_name}: duplicate row {key}")
            seen.add(key)
            placed[key] = placed.get(key, 0) + 1
    finally:
        con.close()


def validate_global(
    topics: list,
    tables_data: dict[str, TableData],
    tables_config: dict[str, TableConfig],
    output_dir: Path,
    config: BuildConfig,
) -> dict[str, ValidationReport]:
    """Global row-conservation check across all topics (step 9)."""
    reports: dict[str, ValidationReport] = {}
    for topic in topics:
        folders = sorted(output_dir.glob(f"{topic.name}_*"))
        reports[topic.name] = ValidationReport()
        for table_name in topic.tables:
            data = tables_data[table_name]
            if tables_config[table_name].primary_key is None:
                continue
            found_keys: set[tuple] = set()
            pk_source = tables_config[table_name].primary_key
            if pk_source is None:
                continue
            for folder in folders:
                for db_file in folder.glob("*.db"):
                    found_keys |= _collect_row_keys(db_file, table_name, pk_source)
            expected = {key for key in data.row_keys}
            missing = expected - found_keys
            if missing:
                reports[topic.name].error(
                    f"{table_name}: {len(missing)} rows missing from all databases"
                )
    return reports


def _collect_row_keys(db_file: Path, table_name: str, pk_source: str) -> set[tuple]:
    """Collect source row keys of one table from one written database via its mapping.

    The PK column is resolved through the mapping (physical -> source column) so
    dropped/renamed columns do not affect key extraction.
    """
    folder = db_file.parent
    mapping_path = next(folder.glob("*.mapping.yaml"))
    mapping = yaml.safe_load(mapping_path.read_text())
    for physical_name, spec in mapping.items():
        if spec["source"] != table_name:
            continue
        source_cols = list(spec["columns"].values())
        if pk_source not in source_cols:
            return set()
        physical_pk = next(phys for phys, src in spec["columns"].items() if src == pk_source)
        con = sqlite3.connect(db_file)
        try:
            rows = con.execute(f'SELECT "{physical_pk}" FROM "{physical_name}"')
            return {(r[0],) for r in rows}
        finally:
            con.close()
    return set()
