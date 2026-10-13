# Wikidata Federated-Overlap Dataset

A Wikidata-derived dataset distributed across federated SQLite databases with overlapping geographic partitions. Each database covers a country or geographic group and contains a subset of Wikidata entity tables (airports, persons, companies, etc.), with tables randomly renamed and columns randomly dropped to simulate realistic schema heterogeneity.

## Quick start

```bash
uv sync              # install dependencies (Python >= 3.15)
bash run-all.sh      # full pipeline: fetch -> ground truth -> dataset -> inputs
```

Individual steps:

```bash
bash fetch-all.sh            # fetch CSVs from the QLever Wikidata endpoint
uv run build-ground-truth    # results/*.csv -> results/ground_truth.db
uv run dataset-builder       # results/ground_truth.db -> dataset/<topic>_<region>/
uv run create-query-inputs   # input/*.json templates -> dataset/_meta/input/
```

## Pipeline

1. **`fetch-all.sh`** — runs every `.rq` file in `queries/` against the QLever Wikidata SPARQL endpoint and saves the result as `results/<entity>.csv`. Skips files that already exist.
2. **`uv run build-ground-truth`** — reads all CSVs in `results/`, strips Wikidata URI prefixes (`http://www.wikidata.org/entity/Q183` -> `183`), casts numeric columns, and writes one SQLite table per entity into `results/ground_truth.db`.
3. **`uv run dataset-builder`** — plans and writes the federated dataset into `dataset/`:
   - 10 topics (geography, aviation, economics, football, ucl, basketball, people, german_states, states, provinces), each with exactly `--target` (default 15) databases.
   - Each database gets a tight region: a single country, a country group (`config/group_countries.yaml`), a neighbor pair/triple (haversine-based), or worldwide as a last resort.
   - Every row is duplicated across databases by an overlap distribution (`--overlap-distribution {uniform,pareto,exponential,gpd,zipf}`, default exponential rate 0.5), capped at `--overlap-cap` (default 2/3 of the target).
   - The greedy 80/20 rule holds: the first 20% of databases in greedy order cover ~80% of each table's distinct rows.
   - Tables and columns are randomly renamed (synonyms from `config/columns.yaml`) or dropped (`--drop-probability`, default 0.25).
   - Writes per-database `<name>.mapping.yaml` (source->chosen names) and `<name>.comments.yaml` (human-readable descriptions), plus logs, plots, and validation reports under `dataset/_meta/`.
4. **`uv run create-query-inputs`** — fills the `input/*.json` templates against the built dataset: one agent per database, per step the agents whose region fits the step's region descriptor, the reconstructed `full_answer` (executing the ground-truth SQL), and statistics written to `dataset/_meta/logs/input.md`.

## Directory structure

```
config/               — READONLY: topics.yaml, columns.yaml, group_countries.yaml, labels.yaml, world.svg
queries/              — READONLY: one SPARQL query (.rq) per entity table
results/              — fetched CSVs + ground_truth.db (built by build-ground-truth)
src/                  — all code (package wikidata_federated_overlap_dataset)
input/                — query input templates (one per benchmark question)
dataset/              — generated per-DB SQLite files + _meta/ (logs, plots, inputs)
fetch-all.sh          — fetch raw data from Wikidata
run-all.sh            — end-to-end build (fetch -> ground truth -> dataset -> inputs)
```

## Source layout (`src/wikidata_federated_overlap_dataset/`)

| Module | Purpose |
|---|---|
| `dataset_builder.py` | `uv run dataset-builder` — orchestration: setup -> per-topic planning/writing (parallel) -> validation -> logs/plots |
| `build_ground_truth.py` | `uv run build-ground-truth` — CSVs -> `results/ground_truth.db` |
| `create_query_inputs.py` | `uv run create-query-inputs` — templates + dataset -> filled benchmark inputs |
| `config.py` | dataclasses + CLI arguments loaded from `config/*.yaml` |
| `ground_truth.py` | table loading, row keys, row -> country/continent resolution |
| `regions.py` | region catalog (countries, groups, neighbor sets, worldwide) + containment graph |
| `planner.py` | region selection, overlap counts, row placement, 80/20 band verification |
| `overlap.py` | duplication-count distributions (uniform, pareto, exponential, gpd, zipf) |
| `writer.py` | per-database SQLite writing with renaming/dropping + mapping/comments files |
| `validation.py` | in-build requirement checks (region assignment, row conservation, band) |
| `plots.py` | coverage/overlap/map plots (svg/png/pdf) + csv data files |
| `logging_utils.py` | markdown log helpers |

## CLI options (`uv run dataset-builder`)

| Option | Default | Description |
|---|---|---|
| `--seed` | 42 | deterministic builds: same seed -> byte-identical output |
| `--target` | 15 | databases per topic |
| `--overlap-distribution` | exponential | `uniform`, `pareto`, `exponential`, `gpd`, `zipf` |
| `--overlap-distribution-shape` | distribution-specific | shape parameter (pareto/gpd/zipf) |
| `--overlap-distribution-rate` | 0.5 | rate parameter (exponential) |
| `--overlap-cap` | 2/3 | max fraction of databases a single row may appear in |
| `--drop-probability` | 0.25 | probability of dropping a non-required column |
| `--topics` | all | comma-separated topic subset |
| `--skew-limit` | 3.0 | max allowed max/median load ratio across databases |
| `--output-dir` | `dataset` | output directory |

## Config files (`config/`)

- **`topics.yaml`** — the 10 topics, each listing its tables and a description template (`{{REGION}}`).
- **`columns.yaml`** — per table: primary key, split column, foreign keys, required columns, value column, description, and name synonyms for tables and columns. Drives the ground-truth build, the row->region assignment, and the renaming/dropping.
- **`group_countries.yaml`** — named country groups (EU, NATO, continents, climate zones, ...) usable as database regions.
- **`labels.yaml`** — slug -> human-readable name mapping.
- **`world.svg`** — world map template for the per-topic map plots.

## Outputs

`dataset/<topic>_<region>[_k]/` — one folder per database:
- `<name>.db` — SQLite database with renamed tables and sampled rows
- `<name>.mapping.yaml` — source -> chosen table/column names
- `<name>.comments.yaml` — human-readable table/column descriptions

`dataset/_meta/` — build metadata:
- `logs/` — `build.md` (summary) + one markdown log per topic + `input.md` (query-input statistics)
- `meta_mapping.yaml` — every source table -> each database holding it with the physical table name and physical -> source column map
- `plots_coverage*/`, `plots_overlap/`, `plots_distribution/`, `plots_map/` — plots (svg/png/pdf) with csv data
- `input/` — the filled benchmark input files
