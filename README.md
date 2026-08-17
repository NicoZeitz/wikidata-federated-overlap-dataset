# Wikidata Federated-Overlap Dataset

A Wikidata-derived dataset distributed across federated SQLite databases with overlapping geographic partitions. Each database covers a country or geographic group and contains a subset of Wikidata entity tables (airports, persons, companies, etc.), with tables randomly renamed and columns randomly dropped to simulate realistic schema heterogeneity.

The actual database files and Wikidata query results live on HuggingFace: https://huggingface.co/datasets/NicoZeitz/wikidata-federated-overlap-dataset

---

## Directory structure

```
columns.yaml          — column/table metadata, synonyms, and roles (see below)
queries/              — SPARQL queries, one per entity table
results/              — raw CSVs fetched from Wikidata + ground_truth.db (HuggingFace)
dataset/              — generated per-DB SQLite files (HuggingFace)
input/                — query input JSON files for the QCP benchmark
scripts/              — Python scripts that build the dataset
fetch-all.sh          — fetch raw data from Wikidata
run-all.sh            — end-to-end build (fetch → ground truth → dataset)
create-query-input.sh — helper to generate input/ JSON files
```

---

## Shell scripts

**`fetch-all.sh`**
Runs every `.rq` file in `queries/` against the QLever Wikidata SPARQL endpoint and saves the result as `results/<entity>.csv`. Skips files that already exist.

**`run-all.sh`**
Full pipeline in one command:
1. `fetch-all.sh` — fetch CSVs
2. `scripts/build_ground_truth.py` — merge CSVs into `results/ground_truth.db`
3. `scripts/build_dataset.py` — generate all per-DB SQLite files into `dataset/`

**`create-query-input.sh`**
Wrapper around `scripts/create_query_input.py`. Given a natural-language query and a SQL query against the ground truth schema, produces a `.json` file in `input/` that describes how to distribute the query across the federated databases (which agents hold relevant data, what schema renaming applies).

---

## Python scripts

**`scripts/build_ground_truth.py`**
Reads all CSVs in `results/`, strips Wikidata URI prefixes (e.g. `http://www.wikidata.org/entity/Q183` → `Q183`), casts numeric columns, and writes everything into `results/ground_truth.db` as one SQLite table per entity.

**`scripts/build_dataset.py`**
Main dataset builder. Reads `ground_truth.db` and `columns.yaml`, then generates `--target` (default 1000) SQLite databases in `dataset/`. Each DB covers a country or geographic group, contains a random subset of relevant entity tables, and applies random column dropping and table/column renaming (using synonyms from `columns.yaml`). Also writes per-DB `<slug>.mapping.yaml` (source→chosen name map) and `<slug>.comments.yaml` (human-readable column descriptions) alongside each `.db` file.

**`scripts/create_query_input.py`**
Given a SQL query (written against the ground truth schema) and a natural-language question, figures out which generated databases contain relevant rows, translates the SQL to the per-DB renamed schema, and writes a structured `.json` file to `input/`. These files are used as benchmark inputs for the QCP adaptive termination experiments.

**`scripts/geo_groups.py`**
Authoritative mapping of every country to all geographic, political, economic, and cultural groups it belongs to (UN subregions, blocs like EU/NATO/OECD, climate zones, WWF ecozones, timezones, religion, etc.). Used by `build_dataset.py` to assign countries to group-level databases.

**`scripts/db_utils.py`**
Shared utilities: `slugify` (name → snake_case slug) and `strip_uri` (strip Wikidata entity URI prefix).

---

## columns.yaml

Defines metadata for every entity table and its columns. Used by `build_ground_truth.py` (to know primary keys, split columns, and foreign keys) and by `build_dataset.py` (to rename tables and columns in generated databases).

Structure per table:

```yaml
<table_name>:
  __meta__:
    primary_key: <column>          # unique row identifier
    split_col: <column>            # column used to partition rows by country/continent
    foreign_keys:
      <col>: <referenced_table>
    required: [cols always kept]   # never dropped during column sampling
    synonyms:
      <alt_name>: "Description when table is renamed to this."
    description: "Description when table keeps its source name."
  <column_name>:
    description: "Description when column keeps its source name."
    synonyms:
      <alt_name>: "Description when column is renamed to this."
```

During dataset generation, each table and column is randomly assigned either its source name or one of its synonyms (deterministically via a seed). The `mapping.yaml` file next to each `.db` records which names were chosen.

---

## queries/

One SPARQL query per entity table (e.g. `countries.rq`, `airports.rq`). Each query fetches one entity type from the QLever Wikidata endpoint. Results land in `results/<entity>.csv`.

---

## input/

JSON files used as benchmark inputs. Each file encodes a natural-language question, the ground-truth SQL query, and a full description of which federated agents hold relevant data and what schema renaming is in effect for that question.

---

## dataset/

One subdirectory per generated database, named `<slug>/`:
- `<slug>.db` — SQLite database with renamed tables and sampled rows
- `<slug>.mapping.yaml` — maps source table/column names to chosen names in this DB
- `<slug>.comments.yaml` — human-readable descriptions for tables and columns

`dataset/` is not stored in this repo. Download from HuggingFace: https://huggingface.co/datasets/NicoZeitz/wikidata-federated-overlap-dataset

## results/

Raw per-entity CSVs fetched from Wikidata and the merged `ground_truth.db`. Not stored in this repo — download from HuggingFace.
