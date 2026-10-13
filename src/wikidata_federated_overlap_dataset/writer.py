"""SQLite writing: renamed/dropped schemas, .mapping.yaml, .comments.yaml."""

from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import yaml

from wikidata_federated_overlap_dataset.config import BuildConfig, TableConfig, TopicConfig
from wikidata_federated_overlap_dataset.ground_truth import TableData
from wikidata_federated_overlap_dataset.planner import TopicPlan, derived_seed
from wikidata_federated_overlap_dataset.regions import Region

BATCH_SIZE = 10_000


@dataclass
class PhysicalTable:
    """The physical schema of one table inside one database."""

    physical_name: str
    source_name: str
    kept_columns: list[str]
    physical_columns: dict[str, str]


def pick_name(rng: random.Random, default: str, synonyms: tuple[str, ...]) -> str:
    """Choose uniformly from default + synonyms."""
    return rng.choice([default, *synonyms])


def build_physical_schema(
    rng: random.Random,
    table_config: TableConfig,
    drop_probability: float,
    taken_names: set[str] | None = None,
) -> PhysicalTable:
    """Rename table + columns and drop non-required non-key columns with probability p."""
    physical_name = pick_name(rng, table_config.name, table_config.table_synonyms)
    while taken_names and physical_name in taken_names:
        physical_name = f"{physical_name}_2"
    if taken_names is not None:
        taken_names.add(physical_name)
    pk = table_config.primary_key
    fk_cols = set(table_config.foreign_keys)
    kept = []
    for column in table_config.columns.values():
        is_key = column.name == pk or column.name in fk_cols
        is_required = column.name in table_config.required
        if is_key or is_required or rng.random() >= drop_probability:
            kept.append(column.name)
    if pk is not None and pk not in kept:
        kept.append(pk)
    physical_columns = {c: pick_name(rng, c, table_config.columns[c].synonyms) for c in kept}
    return PhysicalTable(
        physical_name=physical_name,
        source_name=table_config.name,
        kept_columns=kept,
        physical_columns=physical_columns,
    )


def slice_row(row: tuple, columns: list[str], kept: list[str]) -> tuple:
    """Project a full source row tuple onto the kept columns."""
    index = {c: i for i, c in enumerate(columns)}
    return tuple(row[index[c]] for c in kept)


def database_folder_name(topic_name: str, region: Region, used: set[str]) -> str:
    """Unique folder name <topic>_<region_slug>[_k] for databases sharing a region."""
    base = f"{topic_name}_{region.slug}"
    if base not in used:
        used.add(base)
        return base
    k = 2
    while f"{base}_{k}" in used:
        k += 1
    name = f"{base}_{k}"
    used.add(name)
    return name


def write_topic(
    output_dir: Path,
    topic: TopicConfig,
    plan: TopicPlan,
    tables_data: dict[str, TableData],
    tables_config: dict[str, TableConfig],
    config: BuildConfig,
) -> list[Path]:
    """Write all databases of one topic; returns the list of folder paths."""
    folders = []
    used: set[str] = set()
    names = [database_folder_name(topic.name, db.region, used) for db in plan.databases]
    index_by_folder = dict(zip(names, range(len(names)), strict=True))
    for db, name in zip(plan.databases, names, strict=True):
        folder = output_dir / name
        rng = random.Random(derived_seed(config.seed, topic.name, name))
        write_database_with_index(
            folder,
            db.region,
            topic,
            plan,
            tables_data,
            tables_config,
            config,
            rng,
            index_by_folder[name],
        )
        folders.append(folder)
    return folders


def write_database_with_index(
    folder: Path,
    region: Region,
    topic: TopicConfig,
    plan: TopicPlan,
    tables_data: dict[str, TableData],
    tables_config: dict[str, TableConfig],
    config: BuildConfig,
    rng: random.Random,
    db_index: int,
) -> None:
    """Write one database folder using a precomputed database index."""
    folder.mkdir(parents=True, exist_ok=True)
    db_path = folder / f"{region.slug}.db"
    db_path.unlink(missing_ok=True)
    con = sqlite3.connect(db_path)
    mapping: dict[str, dict] = {}
    comments: dict[str, object] = {
        "__database__": topic.description.replace("{{REGION}}", region.label)
    }
    try:
        taken_names: set[str] = set()
        for table_name in topic.tables:
            table_config = tables_config[table_name]
            data = tables_data[table_name]
            physical = build_physical_schema(
                rng, table_config, config.drop_probability, taken_names
            )
            col_defs = ", ".join(f'"{physical.physical_columns[c]}"' for c in physical.kept_columns)
            con.execute(f'CREATE TABLE "{physical.physical_name}" ({col_defs})')
            instance_rows = plan.tables[table_name].instances.get(db_index, [])
            values = [
                slice_row(data.rows[r], data.columns, physical.kept_columns) for r in instance_rows
            ]
            placeholders = ", ".join("?" for _ in physical.kept_columns)
            insert_sql = f'INSERT INTO "{physical.physical_name}" VALUES ({placeholders})'
            for start in range(0, len(values), BATCH_SIZE):
                con.executemany(insert_sql, values[start : start + BATCH_SIZE])
            mapping[physical.physical_name] = {
                "source": table_config.name,
                "columns": {physical.physical_columns[c]: c for c in physical.kept_columns},
            }
            table_comment = {
                "__table__": table_config.description.replace("{{REGION}}", region.label)
            }
            for c in physical.kept_columns:
                phys = physical.physical_columns[c]
                table_comment[phys] = table_config.columns[c].description
            comments[physical.physical_name] = table_comment
        con.commit()
    finally:
        con.close()
    with (folder / f"{region.slug}.mapping.yaml").open("w") as f:
        yaml.safe_dump(mapping, f, sort_keys=False)
    with (folder / f"{region.slug}.comments.yaml").open("w") as f:
        yaml.safe_dump(comments, f, sort_keys=False, allow_unicode=True)


def write_meta_mapping(output_dir: Path, topics: list[TopicConfig]) -> None:
    """Aggregate every database's mapping into dataset/_meta/meta_mapping.yaml.

    One entry per source table, listing each database folder that holds it with
    the physical table name and the source -> physical column map.
    """
    meta_map: dict[str, dict] = {}
    for folder in sorted(p for p in output_dir.iterdir() if p.is_dir() and p.name != "_meta"):
        mapping_path = next(folder.glob("*.mapping.yaml"), None)
        if mapping_path is None:
            continue
        mapping = yaml.safe_load(mapping_path.read_text())
        for physical_name, info in mapping.items():
            meta_map.setdefault(info["source"], {})[folder.name] = {
                "name": physical_name,
                "columns": {source: physical for physical, source in info["columns"].items()},
            }
    meta_dir = output_dir / "_meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    with (meta_dir / "meta_mapping.yaml").open("w") as f:
        yaml.safe_dump(meta_map, f, sort_keys=False, allow_unicode=True)
