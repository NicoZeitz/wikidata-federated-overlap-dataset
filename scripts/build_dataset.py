"""Build wikidata2 dataset: generate per-country and per-geo-group SQLite databases."""

import argparse
import concurrent.futures
import hashlib
import os
import random
import re
import sqlite3
import statistics
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, replace
from itertools import chain
from operator import itemgetter
from pathlib import Path

import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).parent))
from geo_groups import COUNTRY_GROUPS

_GT_DB = Path(__file__).parent.parent / "results" / "ground_truth.db"
_COLUMNS_YAML = Path(__file__).parent.parent / "columns.yaml"
_DEFAULT_OUT_DIR = Path(__file__).parent.parent / "dataset"
_DEFAULT_SEED = 42
_DEFAULT_TARGET = 1000

# Continent group slugs already in geo_groups — superseded by continent DBs from the DB.
_GEO_GROUP_CONTINENT_SLUGS = frozenset({"africa", "asia", "europe", "oceania"})


# ── helpers ───────────────────────────────────────────────────────────────────


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower())
    return s.strip("_")


def _derive_seed(base: int, *parts: str) -> int:
    """Stable integer seed derived from a base seed and string parts."""
    h = hashlib.sha256(str(base).encode())
    for p in parts:
        h.update(p.encode())
    return int(h.hexdigest()[:16], 16)


# ── data structures ───────────────────────────────────────────────────────────


@dataclass(slots=True)
class DbSpec:
    slug: str
    base_slug: str
    db_type: str  # "country" | "group" | "continent"
    valid_qids: frozenset[str]
    variation_index: int  # 0 = base DB, 1+ = variants


@dataclass(slots=True)
class ColConfig:
    source_name: str
    description: str
    synonyms: dict[str, str]  # alt_name -> description


@dataclass(slots=True)
class TableConfig:
    source_name: str
    description: str
    synonyms: dict[str, str]  # alt_name -> description
    primary_key: str | None
    split_col: str | None
    foreign_keys: dict[str, str]  # col_name -> ref_table
    required: frozenset[str]
    cols: list[ColConfig]


@dataclass(slots=True)
class ColSpec:
    source_name: str
    chosen_name: str
    description: str
    is_pk: bool
    is_fk: bool


@dataclass(slots=True)
class TableSpec:
    source_name: str
    chosen_name: str
    description: str
    split_col: str | None
    primary_key: str | None
    foreign_keys: dict[str, str]
    cols: list[ColSpec]


@dataclass(slots=True)
class DbSchema:
    spec: DbSpec
    tables: list[TableSpec]


@dataclass(slots=True)
class _GatheredTable:
    spec: TableSpec
    rows: list[tuple]
    col_idx: dict[str, int]
    valid_col_specs: list[ColSpec]


@dataclass(slots=True)
class TableIndex:
    source_name: str
    col_names: list[str]
    all_rows: list[tuple]
    by_qid: dict[str, list[int]]  # country/continent qid -> row indices
    is_continent: bool  # True when indexed by continent qid


@dataclass(slots=True)
class DbStats:
    slug: str
    db_type: str
    num_tables: int
    total_rows: int
    rows_per_table: dict[str, int]  # source_name -> rows actually written


# ── data loading ──────────────────────────────────────────────────────────────


def _load_countries(conn: sqlite3.Connection) -> dict[str, str]:
    """Return {q_id -> country_name} for all countries in ground_truth.db."""
    rows = conn.execute("SELECT country, name FROM countries").fetchall()
    return {q_id: name for q_id, name in rows}


def _load_country_populations(conn: sqlite3.Connection) -> dict[str, float]:
    """Return {q_id -> population} for all countries; missing values default to 1."""
    rows = conn.execute("SELECT country, population FROM countries").fetchall()
    return {q_id: float(pop) if pop is not None else 1.0 for q_id, pop in rows}


def _load_continent_groups(
    conn: sqlite3.Connection,
) -> tuple[dict[str, frozenset[str]], dict[str, str], dict[str, str]]:
    """Return (continent_qids, continent_names, continent_slug_to_qid)."""
    conts = conn.execute("SELECT continent, name FROM continents").fetchall()
    continent_names = {_slugify(name): name for _, name in conts}
    continent_slug_to_qid = {_slugify(name): qid for qid, name in conts}

    rows = conn.execute(
        "SELECT co.name, c.country FROM continents co LEFT JOIN countries c ON c.continent = co.continent"
    ).fetchall()

    continent_qids: dict[str, set[str]] = {}
    for cont_name, country_qid in rows:
        slug = _slugify(cont_name)
        if slug not in continent_qids:
            continent_qids[slug] = set()
        if country_qid is not None:
            continent_qids[slug].add(country_qid)

    return (
        {s: frozenset(qids) for s, qids in continent_qids.items()},
        continent_names,
        continent_slug_to_qid,
    )


def _build_group_qids(
    qid_to_name: dict[str, str],
    continent_slugs: frozenset[str] = frozenset(),
) -> dict[str, frozenset[str]]:
    """
    Build {group_slug -> frozenset of country q_ids} from COUNTRY_GROUPS.

    Continent-level slugs (static set + actual DB continent slugs) are excluded.
    """
    name_to_qid = {name: qid for qid, name in qid_to_name.items()}
    exclude = _GEO_GROUP_CONTINENT_SLUGS | continent_slugs
    group_qids: dict[str, set[str]] = {}
    for country_name, groups in COUNTRY_GROUPS.items():
        qid = name_to_qid.get(country_name)
        if qid is None:
            continue
        for group in groups:
            if group in exclude:
                continue
            group_qids.setdefault(group, set()).add(qid)
    return {g: frozenset(qids) for g, qids in group_qids.items() if qids}


# ── DB list construction ──────────────────────────────────────────────────────


def _variant_slug(base: str, index: int) -> str:
    return f"{base}_{index + 1:02d}"


def _build_db_list(
    qid_to_name: dict[str, str],
    group_qids: dict[str, frozenset[str]],
    continent_qids: dict[str, frozenset[str]],
    qid_to_pop: dict[str, float],
    target: int,
    seed: int,
    weight_power: float = 0.15,
) -> list[DbSpec]:
    """
    Generate the full DbSpec list.

    Mandatory: 1 per country, 1 per geo group, 1 per continent.
    Additional: weighted sampling up to `target` total.
    Weight = (total population of valid countries) ** weight_power.
    """
    specs: list[DbSpec] = []

    for qid, name in sorted(qid_to_name.items(), key=lambda x: x[1]):
        base = _slugify(name)
        specs.append(
            DbSpec(
                slug=_variant_slug(base, 0),
                base_slug=base,
                db_type="country",
                valid_qids=frozenset({qid}),
                variation_index=0,
            )
        )

    for group_slug in sorted(group_qids):
        qids = group_qids[group_slug]
        specs.append(
            DbSpec(
                slug=_variant_slug(group_slug, 0),
                base_slug=group_slug,
                db_type="group",
                valid_qids=qids,
                variation_index=0,
            )
        )

    for cont_slug in sorted(continent_qids):
        qids = continent_qids[cont_slug]
        specs.append(
            DbSpec(
                slug=_variant_slug(cont_slug, 0),
                base_slug=cont_slug,
                db_type="continent",
                valid_qids=qids,
                variation_index=0,
            )
        )

    remaining = target - len(specs)
    if remaining <= 0:
        return specs

    rng = random.Random(_derive_seed(seed, "additional_dbs"))

    pool_slugs = [s.base_slug for s in specs]
    pool_weights = [max(1.0, sum(qid_to_pop.get(q, 1.0) for q in s.valid_qids)) ** weight_power for s in specs]
    pool_meta = {s.base_slug: (s.db_type, s.valid_qids) for s in specs}

    variation_counter: dict[str, int] = {}

    for _ in range(remaining):
        chosen_base = rng.choices(pool_slugs, weights=pool_weights, k=1)[0]
        idx = variation_counter.get(chosen_base, 0) + 1
        variation_counter[chosen_base] = idx
        db_type, valid_qids = pool_meta[chosen_base]
        specs.append(
            DbSpec(
                slug=_variant_slug(chosen_base, idx),
                base_slug=chosen_base,
                db_type=db_type,
                valid_qids=valid_qids,
                variation_index=idx,
            )
        )

    return specs


# ── schema: load columns.yaml ─────────────────────────────────────────────────


def _load_table_configs(yaml_path: Path) -> dict[str, TableConfig]:
    with yaml_path.open() as f:
        data = yaml.safe_load(f)
    configs: dict[str, TableConfig] = {}
    for table_name, table_data in data.items():
        if not isinstance(table_data, dict):
            continue
        meta = table_data.get("__meta__", {})
        pk = meta.get("primary_key")
        split_col = meta.get("split_col")
        fks: dict[str, str] = dict(meta.get("foreign_keys") or {})
        required: frozenset[str] = frozenset(meta.get("required") or [])
        table_syns: dict[str, str] = dict(meta.get("synonyms") or {})
        table_desc: str = meta.get("description", "")
        cols: list[ColConfig] = []
        for col_name, col_data in table_data.items():
            if col_name == "__meta__" or not isinstance(col_data, dict):
                continue
            col_syns: dict[str, str] = dict(col_data.get("synonyms") or {})
            cols.append(
                ColConfig(
                    source_name=col_name,
                    description=col_data.get("description", ""),
                    synonyms=col_syns,
                )
            )
        configs[table_name] = TableConfig(
            source_name=table_name,
            description=table_desc,
            synonyms=table_syns,
            primary_key=pk,
            split_col=split_col,
            foreign_keys=fks,
            required=required,
            cols=cols,
        )
    return configs


def _build_table_index(gt_db: Path, table_name: str, table_cfg: TableConfig) -> TableIndex:
    """Load a table once and build a {qid -> row_indices} lookup. Runs per thread."""
    conn = sqlite3.connect(f"file:{gt_db}?mode=ro", uri=True)
    try:
        col_names = [r[1] for r in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()]
        all_rows = conn.execute(f'SELECT * FROM "{table_name}"').fetchall()
        by_qid: dict[str, list[int]] = {}

        if table_cfg.split_col is None:
            cont_idx = col_names.index("continent")
            for i, row in enumerate(all_rows):
                qid = row[cont_idx]
                if qid:
                    by_qid.setdefault(qid, []).append(i)
            return TableIndex(
                source_name=table_name, col_names=col_names, all_rows=all_rows, by_qid=by_qid, is_continent=True
            )

        if table_cfg.split_col == "country":
            cidx = col_names.index("country")
            for i, row in enumerate(all_rows):
                qid = row[cidx]
                if qid:
                    by_qid.setdefault(qid, []).append(i)
        else:
            ref_table = table_cfg.foreign_keys[table_cfg.split_col]
            ref_cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{ref_table}")').fetchall()]
            ref_rows = conn.execute(f'SELECT * FROM "{ref_table}"').fetchall()
            ref_pk_idx = ref_cols.index(table_cfg.split_col)
            ref_country_idx = ref_cols.index("country")
            pk_to_country = {
                row[ref_pk_idx]: row[ref_country_idx] for row in ref_rows if row[ref_country_idx] is not None
            }
            split_idx = col_names.index(table_cfg.split_col)
            for i, row in enumerate(all_rows):
                country_qid = pk_to_country.get(row[split_idx])
                if country_qid:
                    by_qid.setdefault(country_qid, []).append(i)

        return TableIndex(
            source_name=table_name, col_names=col_names, all_rows=all_rows, by_qid=by_qid, is_continent=False
        )
    finally:
        conn.close()


def _build_all_table_indices(
    gt_db: Path,
    table_configs: dict[str, TableConfig],
) -> dict[str, TableIndex]:
    """Load all tables in parallel and return {table_name -> TableIndex}."""
    with concurrent.futures.ThreadPoolExecutor() as pool:
        futures = {pool.submit(_build_table_index, gt_db, name, cfg): name for name, cfg in table_configs.items()}
        return {futures[f]: f.result() for f in concurrent.futures.as_completed(futures)}


def _pick(seed: int, options: list[tuple[str, str]]) -> tuple[str, str]:
    """Pick one (name, description) pair from options using a derived seed."""
    return random.Random(seed).choice(options)


def _make_col_specs(
    table_cfg: TableConfig,
    db_slug: str,
    seed: int,
    p_drop_col: float,
) -> list[ColSpec]:
    always_keep = frozenset(
        ([table_cfg.primary_key] if table_cfg.primary_key else [])
        + list(table_cfg.foreign_keys)
        + list(table_cfg.required)
    )
    specs: list[ColSpec] = []
    for col in table_cfg.cols:
        must_keep = col.source_name in always_keep
        if not must_keep:
            keep_seed = _derive_seed(seed, db_slug, table_cfg.source_name, col.source_name, "keep")
            if random.Random(keep_seed).random() < p_drop_col:
                continue
        name_seed = _derive_seed(seed, db_slug, table_cfg.source_name, col.source_name, "name")
        options = [(col.source_name, col.description)] + list(col.synonyms.items())
        chosen_name, description = _pick(name_seed, options)
        specs.append(
            ColSpec(
                source_name=col.source_name,
                chosen_name=chosen_name,
                description=description,
                is_pk=col.source_name == table_cfg.primary_key,
                is_fk=col.source_name in table_cfg.foreign_keys,
            )
        )
    return specs


def _make_table_spec(
    table_cfg: TableConfig,
    db_slug: str,
    seed: int,
    p_drop_col: float,
) -> TableSpec:
    name_seed = _derive_seed(seed, db_slug, table_cfg.source_name, "name")
    options = [(table_cfg.source_name, table_cfg.description)] + list(table_cfg.synonyms.items())
    chosen_name, description = _pick(name_seed, options)
    return TableSpec(
        source_name=table_cfg.source_name,
        chosen_name=chosen_name,
        description=description,
        split_col=table_cfg.split_col,
        primary_key=table_cfg.primary_key,
        foreign_keys=dict(table_cfg.foreign_keys),
        cols=_make_col_specs(table_cfg, db_slug, seed, p_drop_col),
    )


def _assign_db_schema(
    db_spec: DbSpec,
    table_configs: dict[str, TableConfig],
    table_country_qids: dict[str, frozenset[str] | None],
    seed: int,
    p_include: float,
    p_drop_col: float,
) -> DbSchema:
    eligible: list[str] = []
    for table_name in sorted(table_configs):
        country_qids = table_country_qids.get(table_name)
        if country_qids is None or bool(db_spec.valid_qids & country_qids):
            eligible.append(table_name)

    tables: list[TableSpec] = []
    for table_name in eligible:
        include_seed = _derive_seed(seed, db_spec.slug, table_name, "include")
        if random.Random(include_seed).random() < p_include:
            tables.append(_make_table_spec(table_configs[table_name], db_spec.slug, seed, p_drop_col))

    if not tables and eligible:
        fallback_seed = _derive_seed(seed, db_spec.slug, "fallback_table")
        fallback_name = random.Random(fallback_seed).choice(eligible)
        tables.append(_make_table_spec(table_configs[fallback_name], db_spec.slug, seed, p_drop_col))

    seen_names: set[str] = set()
    deduped: list[TableSpec] = []
    for t in tables:
        name = t.chosen_name if t.chosen_name not in seen_names else t.source_name
        seen_names.add(name)
        deduped.append(replace(t, chosen_name=name) if name != t.chosen_name else t)
    tables = deduped

    return DbSchema(spec=db_spec, tables=tables)


def assign_all_schemas(
    db_list: list[DbSpec],
    table_configs: dict[str, TableConfig],
    table_country_qids: dict[str, frozenset[str] | None],
    seed: int,
    p_include: float = 0.7,
    p_drop_col: float = 0.15,
) -> list[DbSchema]:
    def _assign(spec: DbSpec) -> DbSchema:
        return _assign_db_schema(spec, table_configs, table_country_qids, seed, p_include, p_drop_col)

    with concurrent.futures.ThreadPoolExecutor() as pool:
        return list(pool.map(_assign, db_list))


# ── row filtering (index-based, no repeated DB I/O) ──────────────────────────


def _get_valid_continent_qids(
    valid_qids: frozenset[str],
    db_type: str,
    base_slug: str,
    country_to_continent: dict[str, str],
    continent_slug_to_qid: dict[str, str],
) -> frozenset[str]:
    result = {country_to_continent[q] for q in valid_qids if q in country_to_continent}
    if db_type == "continent":
        qid = continent_slug_to_qid.get(base_slug)
        if qid is not None:
            result.add(qid)
    return frozenset(result)


def _precompute_continent_qids(
    db_list: list[DbSpec],
    country_to_continent: dict[str, str],
    continent_slug_to_qid: dict[str, str],
) -> dict[str, frozenset[str]]:
    return {
        spec.slug: _get_valid_continent_qids(
            spec.valid_qids,
            spec.db_type,
            spec.base_slug,
            country_to_continent,
            continent_slug_to_qid,
        )
        for spec in db_list
    }


# ── write ──────────────────────────────────────────────────────────────────────


def _gather_from_index(
    index: TableIndex,
    table_spec: TableSpec,
    valid_qids: frozenset[str],
    valid_continent_qids: frozenset[str],
    db_slug: str,
    seed: int,
    min_p: float,
    max_p: float,
) -> _GatheredTable | None:
    lookup = valid_continent_qids if index.is_continent else valid_qids
    if not lookup:
        return None
    all_indices = list(chain.from_iterable(index.by_qid.get(qid, ()) for qid in lookup))
    if not all_indices:
        return None
    rng = np.random.default_rng(_derive_seed(seed, db_slug, table_spec.source_name, "rows"))
    p = float(rng.uniform(min_p, max_p))
    k = max(1, round(p * len(all_indices)))
    idx_array = np.array(all_indices, dtype=np.intp)
    sampled = rng.choice(idx_array, size=k, replace=False)
    rows = [index.all_rows[i] for i in sampled]
    col_idx = {c: i for i, c in enumerate(index.col_names)}
    valid_col_specs = [cs for cs in table_spec.cols if cs.source_name in col_idx]
    return _GatheredTable(spec=table_spec, rows=rows, col_idx=col_idx, valid_col_specs=valid_col_specs)


def _gather_fallback_from_index(
    table_indices: dict[str, TableIndex],
    table_configs: dict[str, TableConfig],
    db_spec: DbSpec,
    valid_continent_qids: frozenset[str],
    seed: int,
) -> _GatheredTable | None:
    for tname in sorted(table_indices):
        index = table_indices[tname]
        lookup = valid_continent_qids if index.is_continent else db_spec.valid_qids
        all_indices = list(chain.from_iterable(index.by_qid.get(qid, ()) for qid in lookup))
        if not all_indices:
            continue
        rows = [index.all_rows[i] for i in all_indices]
        table_spec = _make_table_spec(table_configs[tname], db_spec.slug, seed, 0.0)
        col_idx = {c: i for i, c in enumerate(index.col_names)}
        valid_col_specs = [cs for cs in table_spec.cols if cs.source_name in col_idx]
        return _GatheredTable(spec=table_spec, rows=rows, col_idx=col_idx, valid_col_specs=valid_col_specs)
    return None


def _write_sqlite(db_path: Path, gathered: list[_GatheredTable]) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        for g in gathered:
            chosen_cols = [cs.chosen_name for cs in g.valid_col_specs]
            col_defs = ", ".join(f'"{c}" TEXT' for c in chosen_cols)
            conn.execute(f'DROP TABLE IF EXISTS "{g.spec.chosen_name}"')
            conn.execute(f'CREATE TABLE "{g.spec.chosen_name}" ({col_defs})')
            col_indices = [g.col_idx[cs.source_name] for cs in g.valid_col_specs]
            getter = itemgetter(*col_indices) if len(col_indices) > 1 else None
            if getter is not None:
                rows_gen = (getter(row) for row in g.rows)
            else:
                rows_gen = ((row[col_indices[0]],) for row in g.rows)
            conn.executemany(
                f'INSERT INTO "{g.spec.chosen_name}" VALUES ({",".join("?" * len(chosen_cols))})',
                rows_gen,
            )
        conn.commit()
    finally:
        conn.close()


def _write_comments_yaml(db_dir: Path, spec: DbSpec, gathered: list[_GatheredTable]) -> None:
    label = spec.base_slug.replace("_", " ")
    table_names = ", ".join(g.spec.chosen_name for g in gathered[:3])
    suffix = "..." if len(gathered) > 3 else ""
    doc: dict = {
        "__database__": f"Database covering {label} with tables: {table_names}{suffix}.",
    }
    for g in gathered:
        entry: dict = {"__table__": g.spec.description or g.spec.chosen_name}
        for cs in g.valid_col_specs:
            entry[cs.chosen_name] = cs.description or cs.chosen_name
        doc[g.spec.chosen_name] = entry
    (db_dir / f"{spec.slug}.comments.yaml").write_text(
        yaml.dump(doc, allow_unicode=True, sort_keys=False, Dumper=yaml.CSafeDumper)
    )


def _build_mapping_dict(gathered: list[_GatheredTable]) -> dict:
    return {
        g.spec.source_name: {
            "name": g.spec.chosen_name,
            "columns": {cs.source_name: cs.chosen_name for cs in g.valid_col_specs},
        }
        for g in gathered
    }


def _write_mapping_yaml(db_dir: Path, slug: str, gathered: list[_GatheredTable]) -> None:
    (db_dir / f"{slug}.mapping.yaml").write_text(
        yaml.dump(_build_mapping_dict(gathered), allow_unicode=True, sort_keys=False, Dumper=yaml.CSafeDumper)
    )


def _write_single_db(
    schema: DbSchema,
    table_indices: dict[str, TableIndex],
    table_configs: dict[str, TableConfig],
    valid_continent_qids: frozenset[str],
    out_dir: Path,
    seed: int,
    min_p: float,
    max_p: float,
) -> tuple[str, dict, DbStats]:
    db_dir = out_dir / schema.spec.slug
    db_dir.mkdir(parents=True, exist_ok=True)

    gathered: list[_GatheredTable] = []
    for table_spec in schema.tables:
        index = table_indices.get(table_spec.source_name)
        if index is None:
            continue
        g = _gather_from_index(
            index,
            table_spec,
            schema.spec.valid_qids,
            valid_continent_qids,
            schema.spec.slug,
            seed,
            min_p,
            max_p,
        )
        if g is not None:
            gathered.append(g)
    if not gathered:
        g = _gather_fallback_from_index(
            table_indices,
            table_configs,
            schema.spec,
            valid_continent_qids,
            seed,
        )
        if g is not None:
            gathered.append(g)

    _write_sqlite(db_dir / f"{schema.spec.slug}.db", gathered)
    _write_comments_yaml(db_dir, schema.spec, gathered)
    _write_mapping_yaml(db_dir, schema.spec.slug, gathered)

    rows_per_table = {g.spec.source_name: len(g.rows) for g in gathered}
    db_stats = DbStats(
        slug=schema.spec.slug,
        db_type=schema.spec.db_type,
        num_tables=len(gathered),
        total_rows=sum(rows_per_table.values()),
        rows_per_table=rows_per_table,
    )
    return schema.spec.slug, _build_mapping_dict(gathered), db_stats


def write_all_dbs(
    schemas: list[DbSchema],
    table_indices: dict[str, TableIndex],
    table_configs: dict[str, TableConfig],
    continent_qids_per_db: dict[str, frozenset[str]],
    out_dir: Path,
    seed: int,
    min_p: float = 0.5,
    max_p: float = 1.0,
    workers: int | None = None,
) -> tuple[list[tuple[str, dict]], list[DbStats]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    n_workers = min(32, (os.cpu_count() or 4) * 2, len(schemas)) if workers is None else workers

    raw_results: list[tuple[str, dict, DbStats]] = []
    lock = threading.Lock()
    done: list[int] = [0]
    t0 = time.perf_counter()

    def _task(schema: DbSchema) -> tuple[str, dict, DbStats]:
        result = _write_single_db(
            schema,
            table_indices,
            table_configs,
            continent_qids_per_db[schema.spec.slug],
            out_dir,
            seed,
            min_p,
            max_p,
        )
        with lock:
            done[0] += 1
            n = done[0]
            if n % 100 == 0 or n == len(schemas):
                elapsed = time.perf_counter() - t0
                print(f"  {n}/{len(schemas)} DBs written ({elapsed:.1f}s)", flush=True)
        return result

    with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as pool:
        for result in pool.map(_task, schemas):
            raw_results.append(result)

    mappings = [(slug, mapping) for slug, mapping, _ in raw_results]
    db_stats = [stats for _, _, stats in raw_results]
    return mappings, db_stats


# ── global meta ───────────────────────────────────────────────────────────────


def _write_global_mapping(results: list[tuple[str, dict]], out_dir: Path) -> None:
    # Inverted structure: source_table -> db_slug -> {name, columns}
    # Enables direct translation: SELECT col FROM table -> look up table, iterate DBs
    table_map: dict[str, dict] = {}
    for slug, mapping in sorted(results):
        for source_table, table_info in mapping.items():
            table_map.setdefault(source_table, {})[slug] = table_info
    (out_dir / "meta_mapping.yaml").write_text(
        yaml.dump(table_map, allow_unicode=True, sort_keys=False, Dumper=yaml.CSafeDumper)
    )


def _write_stats_md(
    out_dir: Path,
    db_stats_list: list[DbStats],
    schemas: list[DbSchema],
    table_indices: dict[str, TableIndex],
    seed: int,
    target: int,
    elapsed_total: float,
    elapsed_loading: float,
    elapsed_schema: float,
    elapsed_writing: float,
    generated_at: str,
) -> None:
    n = len(db_stats_list)
    type_counts: dict[str, int] = {"country": 0, "group": 0, "continent": 0}
    for s in db_stats_list:
        type_counts[s.db_type] += 1

    tables_per_db = [s.num_tables for s in db_stats_list]
    rows_per_db = [s.total_rows for s in db_stats_list]

    table_row_lists: dict[str, list[int]] = {}
    for s in db_stats_list:
        for tname, rcount in s.rows_per_table.items():
            table_row_lists.setdefault(tname, []).append(rcount)

    gt_rows = {name: len(idx.all_rows) for name, idx in table_indices.items()}

    table_col_counts: dict[str, list[int]] = {}
    for schema in schemas:
        for t in schema.tables:
            table_col_counts.setdefault(t.source_name, []).append(len(t.cols))

    def _stat(values: list, comma: bool = False) -> str:
        if not values:
            return "n/a"
        mn, mx = min(values), max(values)
        avg = statistics.mean(values)
        med = statistics.median(values)
        fmt = ",.0f" if comma else ".1f"
        return f"min={mn:,.0f} avg={avg:{fmt}} median={med:,.0f} max={mx:,.0f}"

    lines: list[str] = []
    lines.append("# Dataset Statistics\n")
    lines.append(f"**Generated:** {generated_at}  ")
    lines.append(f"**Seed:** `{seed}` | **Target:** `{target}`  ")
    lines.append(
        f"**Total databases:** {n} "
        f"({type_counts['country']} country, {type_counts['group']} group, "
        f"{type_counts['continent']} continent)  "
    )
    lines.append(
        f"**Generation time:** {elapsed_total:.1f}s "
        f"(loading: {elapsed_loading:.1f}s, schema: {elapsed_schema:.1f}s, "
        f"writing: {elapsed_writing:.1f}s)\n"
    )

    lines.append("## DB Distribution\n")
    base_info: dict[str, list] = {}
    for s in db_stats_list:
        base = s.slug.rsplit("_", 1)[0]
        if base not in base_info:
            base_info[base] = [s.db_type, 0]
        base_info[base][1] += 1
    top_bases = sorted(base_info.items(), key=lambda x: -x[1][1])[:20]
    lines.append("Top 20 most-replicated bases:\n")
    lines.append("| Base | Type | Variants |")
    lines.append("|------|------|----------|")
    for base, (db_type, cnt) in top_bases:
        lines.append(f"| `{base}` | {db_type} | {cnt} |")
    lines.append("")

    lines.append("## Tables Per DB\n")
    lines.append(f"{_stat(tables_per_db)}\n")
    dist = Counter(tables_per_db)
    lines.append("| Tables | DBs |")
    lines.append("|--------|-----|")
    for k in sorted(dist):
        lines.append(f"| {k} | {dist[k]} |")
    lines.append("")

    lines.append("## Rows Per DB\n")
    lines.append(f"{_stat(rows_per_db, comma=True)}\n")
    if n:
        sorted_rows = sorted(rows_per_db)
        p25 = sorted_rows[n // 4]
        p75 = sorted_rows[min(3 * n // 4, n - 1)]
        lines.append(f"P25={p25:,} | P75={p75:,}\n")

    sorted_dbs = sorted(db_stats_list, key=lambda s: s.total_rows, reverse=True)
    lines.append("### Largest 15 DBs\n")
    lines.append("| DB | Type | Tables | Rows |")
    lines.append("|----|------|--------|------|")
    for s in sorted_dbs[:15]:
        lines.append(f"| `{s.slug}` | {s.db_type} | {s.num_tables} | {s.total_rows:,} |")
    lines.append("")

    lines.append("### Smallest 15 DBs\n")
    lines.append("| DB | Type | Tables | Rows |")
    lines.append("|----|------|--------|------|")
    for s in sorted_dbs[-15:]:
        lines.append(f"| `{s.slug}` | {s.db_type} | {s.num_tables} | {s.total_rows:,} |")
    lines.append("")

    lines.append("## Per-Table Statistics\n")
    lines.append("| Table | GT Rows | DBs | Incl% | Rows/DB min | avg | median | max | Cols/DB avg | min | max |")
    lines.append("|-------|---------|-----|-------|-------------|-----|--------|-----|-------------|-----|-----|")
    for tname in sorted(table_row_lists, key=lambda x: -len(table_row_lists[x])):
        rows_list = table_row_lists[tname]
        cols_list = table_col_counts.get(tname, [])
        cnt = len(rows_list)
        pct = 100.0 * cnt / n if n else 0
        gt = gt_rows.get(tname, 0)
        r_avg = statistics.mean(rows_list)
        r_med = statistics.median(rows_list)
        c_avg = statistics.mean(cols_list) if cols_list else 0
        c_min = min(cols_list) if cols_list else 0
        c_max = max(cols_list) if cols_list else 0
        lines.append(
            f"| `{tname}` | {gt:,} | {cnt} | {pct:.0f}% "
            f"| {min(rows_list):,} | {r_avg:,.0f} | {r_med:,.0f} | {max(rows_list):,} "
            f"| {c_avg:.1f} | {c_min} | {c_max} |"
        )
    lines.append("")

    lines.append("## Ground Truth Table Sizes\n")
    lines.append("| Table | Rows | Indexed Keys |")
    lines.append("|-------|------|--------------|")
    for tname in sorted(gt_rows, key=lambda x: -gt_rows[x]):
        idx = table_indices[tname]
        label = "continents" if idx.is_continent else "countries"
        lines.append(f"| `{tname}` | {gt_rows[tname]:,} | {len(idx.by_qid)} {label} |")
    lines.append("")

    out_path = out_dir / "dataset_stats.md"
    out_path.write_text("\n".join(lines))


# ── output ────────────────────────────────────────────────────────────────────


def _print_db_list(specs: list[DbSpec]) -> None:
    base_counts: dict[str, int] = {}
    counts = {"country": 0, "group": 0, "continent": 0}
    for s in specs:
        counts[s.db_type] += 1
        base_counts[s.base_slug] = base_counts.get(s.base_slug, 0) + 1

    multi = {b: c for b, c in base_counts.items() if c > 1}
    if multi:
        print("\nDBs with variants:")
        for base, cnt in sorted(multi.items(), key=lambda x: (-x[1], x[0])):
            print(f"  {base}: {cnt}")

    print(
        f"\nTotal: {len(specs)} databases "
        f"({counts['country']} country, {counts['group']} group, {counts['continent']} continent)"
    )


def _print_gt_table_sizes(table_indices: dict[str, TableIndex]) -> None:
    print("\n  Ground truth table sizes:")
    for tname, idx in sorted(table_indices.items(), key=lambda x: -len(x[1].all_rows)):
        label = "continents" if idx.is_continent else "countries"
        print(f"    {tname:25s}: {len(idx.all_rows):>10,} rows  {len(idx.by_qid):3d} {label}")


def _print_schema_summary(schemas: list[DbSchema]) -> None:
    table_counts: dict[str, int] = {}
    table_col_counts: dict[str, list[int]] = {}
    tables_per_db: list[int] = []
    for schema in schemas:
        tables_per_db.append(len(schema.tables))
        for t in schema.tables:
            table_counts[t.source_name] = table_counts.get(t.source_name, 0) + 1
            table_col_counts.setdefault(t.source_name, []).append(len(t.cols))
    n = len(schemas)
    print("\nTable inclusion rate (across all DBs):")
    for tname, cnt in sorted(table_counts.items(), key=lambda x: -x[1]):
        col_list = table_col_counts[tname]
        print(
            f"  {tname:25s}: {cnt:4d}/{n} ({100 * cnt / n:3.0f}%)  "
            f"cols avg={statistics.mean(col_list):.1f} "
            f"min={min(col_list)} max={max(col_list)}"
        )
    print(
        f"\nTables/DB: min={min(tables_per_db)} "
        f"avg={statistics.mean(tables_per_db):.1f} "
        f"median={statistics.median(tables_per_db):.0f} "
        f"max={max(tables_per_db)}"
    )


def _print_write_summary(db_stats_list: list[DbStats]) -> None:
    if not db_stats_list:
        return
    tables_per_db = [s.num_tables for s in db_stats_list]
    rows_per_db = [s.total_rows for s in db_stats_list]
    print(
        f"\nTables/DB: min={min(tables_per_db)} "
        f"avg={statistics.mean(tables_per_db):.1f} "
        f"median={statistics.median(tables_per_db):.0f} "
        f"max={max(tables_per_db)}"
    )
    print(
        f"Rows/DB:   min={min(rows_per_db):,} "
        f"avg={statistics.mean(rows_per_db):,.0f} "
        f"median={statistics.median(rows_per_db):,.0f} "
        f"max={max(rows_per_db):,}"
    )
    sorted_dbs = sorted(db_stats_list, key=lambda s: s.total_rows, reverse=True)
    print("\n  Largest DBs:")
    for s in sorted_dbs[:5]:
        print(f"    {s.slug:40s}: {s.total_rows:>8,} rows, {s.num_tables} tables")
    print("  Smallest DBs:")
    for s in sorted_dbs[-5:]:
        print(f"    {s.slug:40s}: {s.total_rows:>8,} rows, {s.num_tables} tables")


# ── main pipeline ─────────────────────────────────────────────────────────────


def run_script(args: argparse.Namespace) -> None:
    """Pipeline: load → DB list → schema → write DBs → global mapping + stats."""
    t_start = time.perf_counter()

    conn = sqlite3.connect(args.ground_truth)
    try:
        qid_to_name = _load_countries(conn)
        qid_to_pop = _load_country_populations(conn)
        continent_qids, _continent_names, continent_slug_to_qid = _load_continent_groups(conn)
        group_qids = _build_group_qids(qid_to_name, frozenset(continent_qids))
        db_list = _build_db_list(
            qid_to_name,
            group_qids,
            continent_qids,
            qid_to_pop,
            args.target,
            args.seed,
            args.weight_power,
        )
        _print_db_list(db_list)
    finally:
        conn.close()

    table_configs = _load_table_configs(args.columns_yaml)

    print("\nLoading tables into memory...", flush=True)
    t0 = time.perf_counter()
    table_indices = _build_all_table_indices(args.ground_truth, table_configs)
    elapsed_loading = time.perf_counter() - t0
    _print_gt_table_sizes(table_indices)
    print(f"  [{elapsed_loading:.1f}s] {len(table_indices)} tables loaded", flush=True)

    table_country_qids: dict[str, frozenset[str] | None] = {
        name: (None if idx.is_continent else frozenset(idx.by_qid.keys())) for name, idx in table_indices.items()
    }

    countries_idx = table_indices["countries"]
    country_col = countries_idx.col_names.index("country")
    continent_col = countries_idx.col_names.index("continent")
    country_to_continent: dict[str, str] = {
        row[country_col]: row[continent_col] for row in countries_idx.all_rows if row[continent_col] is not None
    }

    continent_qids_per_db = _precompute_continent_qids(
        db_list,
        country_to_continent,
        continent_slug_to_qid,
    )

    t0 = time.perf_counter()
    schemas = assign_all_schemas(
        db_list,
        table_configs,
        table_country_qids,
        args.seed,
        args.p_include,
        args.p_drop_col,
    )
    elapsed_schema = time.perf_counter() - t0
    _print_schema_summary(schemas)
    print(f"  [{elapsed_schema:.1f}s] {len(schemas)} schemas assigned", flush=True)

    schemas.sort(key=lambda s: len(s.spec.valid_qids), reverse=True)

    print(f"\nWriting {len(schemas)} DBs to {args.out_dir} ...", flush=True)
    t0 = time.perf_counter()
    mappings, db_stats_list = write_all_dbs(
        schemas,
        table_indices,
        table_configs,
        continent_qids_per_db,
        args.out_dir,
        args.seed,
        args.min_row_p,
        args.max_row_p,
        args.workers,
    )
    elapsed_writing = time.perf_counter() - t0
    _print_write_summary(db_stats_list)

    generated_at = time.strftime("%Y-%m-%d %H:%M:%S")
    elapsed_total = time.perf_counter() - t_start

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(_write_global_mapping, mappings, args.out_dir)
        f2 = pool.submit(
            _write_stats_md,
            args.out_dir,
            db_stats_list,
            schemas,
            table_indices,
            args.seed,
            args.target,
            elapsed_total,
            elapsed_loading,
            elapsed_schema,
            elapsed_writing,
            generated_at,
        )
        f1.result()
        f2.result()

    print(f"\nTotal time: {elapsed_total:.1f}s")
    print(f"Done. Mapping -> {args.out_dir / 'meta_mapping.yaml'} | Stats -> {args.out_dir / 'dataset_stats.md'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build wikidata2 dataset databases")
    parser.add_argument("--seed", type=int, default=_DEFAULT_SEED)
    parser.add_argument("--weight-power", type=float, default=0.15)
    parser.add_argument("--target", type=int, default=_DEFAULT_TARGET)
    parser.add_argument("--ground-truth", type=Path, default=_GT_DB)
    parser.add_argument("--columns-yaml", type=Path, default=_COLUMNS_YAML)
    parser.add_argument("--out-dir", type=Path, default=_DEFAULT_OUT_DIR)
    parser.add_argument("--p-include", type=float, default=0.5)
    parser.add_argument("--p-drop-col", type=float, default=0.5)
    parser.add_argument("--min-row-p", type=float, default=0.5)
    parser.add_argument("--max-row-p", type=float, default=1.0)
    parser.add_argument("--workers", type=int, default=None)
    args = parser.parse_args()
    run_script(args)


if __name__ == "__main__":
    main()
