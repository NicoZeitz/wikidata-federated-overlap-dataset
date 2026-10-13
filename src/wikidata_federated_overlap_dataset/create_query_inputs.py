"""Generate query input files from templates + the built dataset.

For every template (input/*.json) this writes dataset/_meta/input/<name>.json
with the same structure as the old inputs:

- ``value.data_agents``: one agent per database folder of the dataset
  (``A1``, ``A2``, ... in sorted folder order).
- ``extra.query_plan.steps[].to_agent``: the agent names of the step's topic
  whose region fits the step's region descriptor (a list of catalog region
  slugs). A region fits when the agent's region intersects the descriptor's
  country set — for a group/neighbor descriptor that includes all subregions
  and countries inside it plus all superregions (the same containment graph
  the builder uses); for a country descriptor the country itself plus all its
  superregions; "worldwide" fits every agent of the topic.
- ``extra.relevant_data_agents``: the union of all steps' ``to_agent``.
- ``extra.full_answer``: reconstructed by executing the template's top-level
  ``sql_query`` against the ground truth database (1x1 result -> scalar,
  otherwise a list of dicts keyed by the result column names).
- ``extra.sql_query`` / ``extra.steps_metadata``: copied from the template.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from wikidata_federated_overlap_dataset.config import load_config
from wikidata_federated_overlap_dataset.ground_truth import load_ground_truth
from wikidata_federated_overlap_dataset.regions import Region, RegionCatalog, build_region_catalog

TEMPLATE_DIR = Path("input")


def parse_region_descriptor(
    descriptor: list[str], catalog: RegionCatalog, all_countries: frozenset[int]
) -> frozenset[int]:
    """Resolve a region descriptor to the country ids it encompasses.

    The descriptor is a list of catalog region slugs (country, group, neighbor
    or "worldwide"); an empty list resolves to every country.
    """
    if not descriptor or "worldwide" in descriptor:
        return all_countries
    countries: set[int] = set()
    for slug in descriptor:
        region = catalog.by_slug.get(slug)
        if region is None:
            msg = f"unknown region slug in template: {slug!r}"
            raise SystemExit(msg)
        countries |= region.countries
    return frozenset(countries)


def agent_fits_region(region: Region, descriptor_countries: frozenset[int]) -> bool:
    """True when the agent's region intersects the descriptor's country set.

    A worldwide agent intersects everything; a region agent fits when it
    shares at least one country with the descriptor (subregions and
    superregions of a descriptor region share countries with it).
    """
    return bool(region.countries & descriptor_countries)


def collect_agents(dataset_dir: Path) -> list[dict]:
    """One agent per database folder: name A1.., path to the .db file."""
    folders = sorted(
        path for path in dataset_dir.iterdir() if path.is_dir() and path.name != "_meta"
    )
    agents = []
    for index, folder in enumerate(folders, start=1):
        db_files = list(folder.glob("*.db"))
        if len(db_files) != 1:
            msg = f"expected exactly one .db file in {folder}, found {len(db_files)}"
            raise SystemExit(msg)
        agents.append({"type": "sql", "name": f"A{index}", "path": db_files[0].as_posix()})
    return agents


def agent_region_slug(folder_name: str, topics: tuple[str, ...]) -> str:
    """Extract the region slug from a database folder name.

    Folder names are ``<topic>_<region_slug>`` with an optional ``_<k>``
    duplicate counter; the region slug itself may contain underscores, so the
    topic prefix (one of the configured topics) is stripped first.
    """
    for topic in sorted(topics, key=len, reverse=True):
        if folder_name.startswith(f"{topic}_"):
            slug = folder_name[len(topic) + 1 :]
            stem, _, last = slug.rpartition("_")
            return stem if last.isdigit() and stem else slug
    msg = f"cannot derive topic from folder name: {folder_name!r}"
    raise SystemExit(msg)


def resolve_folder_region(slug: str, catalog: RegionCatalog) -> Region:
    """Look up the catalog region of a database folder's region slug."""
    region = catalog.by_slug.get(slug)
    if region is None:
        msg = f"database folder region {slug!r} is not in the region catalog"
        raise SystemExit(msg)
    return region


def table_to_topic(topics: list) -> dict[str, str]:
    """Map every configured table to its topic (from config/topics.yaml)."""
    return {table: topic.name for topic in topics for table in topic.tables}


def fitting_agents(
    table: str,
    descriptor: list[str],
    agents: list[dict],
    agent_regions: dict[str, Region],
    catalog: RegionCatalog,
    all_countries: frozenset[int],
    topics_by_table: dict[str, str],
) -> list[str]:
    """Agent names of the table's topic whose region fits the descriptor."""
    topic = topics_by_table.get(table)
    if topic is None:
        msg = f"table {table!r} is not part of any topic in config/topics.yaml"
        raise SystemExit(msg)
    countries = parse_region_descriptor(descriptor, catalog, all_countries)
    return [
        agent["name"]
        for agent in agents
        if Path(agent["path"]).parent.name.startswith(f"{topic}_")
        and agent_fits_region(agent_regions[agent["name"]], countries)
    ]


@dataclass
class InputStats:
    """Per-input statistics printed after generation."""

    name: str
    steps: int
    relevant_agents: int
    agents_per_step: list[int]


def reconstruct_full_answer(sql_query: str, db_path: Path) -> object:
    """Execute the top-level sql on the ground truth and serialize the result.

    A single-column single-row result becomes a scalar; anything else becomes
    a list of dicts keyed by the result column names (the old inputs' answer
    format).
    """
    con = sqlite3.connect(db_path)
    try:
        cursor = con.execute(sql_query)
        columns = [description[0] for description in cursor.description]
        rows = cursor.fetchall()
    finally:
        con.close()
    if len(rows) == 1 and len(columns) == 1:
        return rows[0][0]
    return [dict(zip(columns, row, strict=True)) for row in rows]


def build_input(
    template: dict,
    agents: list[dict],
    agent_regions: dict[str, Region],
    catalog: RegionCatalog,
    all_countries: frozenset[int],
    topics_by_table: dict[str, str],
    db_path: Path,
) -> tuple[dict, InputStats]:
    """Fill one template with agents, the reconstructed answer, and metadata."""
    steps = template["extra"]["query_plan"]["steps"]
    relevant: set[str] = set()
    agents_per_step: list[int] = []
    for step in steps:
        descriptor = step["to_agent"]
        if isinstance(descriptor, str):
            agents_per_step.append(0)
            continue
        names = fitting_agents(
            descriptor["table"],
            descriptor["region"],
            agents,
            agent_regions,
            catalog,
            all_countries,
            topics_by_table,
        )
        step["to_agent"] = names
        agents_per_step.append(len(names))
        relevant.update(names)
    template["extra"]["full_answer"] = reconstruct_full_answer(
        template["extra"]["sql_query"], db_path
    )
    filled = {
        "extra": {
            "relevant_data_agents": sorted(relevant, key=lambda name: int(name[1:])),
            "full_answer": template["extra"]["full_answer"],
            "sql_query": template["extra"]["sql_query"],
            "query_plan": template["extra"]["query_plan"],
            "steps_metadata": template["extra"]["steps_metadata"],
        },
        "value": {
            "__type__": "CollaborativeQueryAnswering",
            "query": template["extra"]["query_plan"]["query"],
            "data_agents": agents,
        },
    }
    return filled, InputStats(
        name=template["extra"]["query_plan"]["query"],
        steps=len(steps),
        relevant_agents=len(relevant),
        agents_per_step=agents_per_step,
    )


def stats_lines(stats: list[InputStats]) -> list[str]:
    """Render the per-input statistics and totals as markdown lines."""
    total_steps = sum(stat.steps for stat in stats)
    total_relevant = sum(stat.relevant_agents for stat in stats)
    all_step_agents = [count for stat in stats for count in stat.agents_per_step]
    agent_steps = [count for count in all_step_agents if count > 0]
    per_query = [stat.relevant_agents for stat in stats]

    def summary(values: list[int]) -> str:
        """avg + median + min + max over a list of counts."""
        ordered = sorted(values)
        mid = len(ordered) // 2
        median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
        return (
            f"avg {sum(values) / max(len(values), 1):.1f}, median {median:g}, "
            f"min {min(values)}, max {max(values)}"
        )

    lines = [
        "# Query input statistics",
        "",
        "## Global",
        "",
        f"- {len(stats)} inputs, {total_steps} steps "
        f"({len(agent_steps)} agent steps, {len(all_step_agents) - len(agent_steps)} "
        f"coordinator steps), {total_relevant} relevant-agent slots",
        f"- agents per query (union over steps): {summary(per_query)}",
        f"- agents per step, micro average (coordinator steps ignored): {summary(agent_steps)}",
        "",
        "## Per query",
        "",
        "| Query | Steps | Relevant agents | Agents per step |",
        "|---|---|---|---|",
    ]
    for stat in stats:
        per_step = ", ".join(str(count) for count in stat.agents_per_step)
        lines.append(f"| {stat.name} | {stat.steps} | {stat.relevant_agents} | {per_step} |")
    return lines


def print_stats(stats: list[InputStats]) -> None:
    """Print the per-input statistics and totals to the console."""
    print()
    for line in stats_lines(stats):
        print(line)


def main(argv: list[str] | None = None) -> None:
    """Generate one input file per template into dataset/_meta/input."""
    parser = argparse.ArgumentParser(prog="create-query-inputs")
    parser.add_argument("--dataset-dir", type=Path, default=Path("dataset"))
    parser.add_argument("--template-dir", type=Path, default=TEMPLATE_DIR)
    parser.add_argument(
        "--ground-truth-db",
        type=Path,
        default=Path("results/ground_truth.db"),
        help="path to the ground truth database used to reconstruct full_answer",
    )
    args = parser.parse_args(argv)

    config, topics, tables_config, groups, labels = load_config([])
    ground_truth = load_ground_truth(args.ground_truth_db, tables_config)
    catalog = build_region_catalog(
        ground_truth.countries.rows, groups, labels, topics, ground_truth.tables
    )
    all_countries = frozenset(country.id for country in ground_truth.countries.rows)
    topics_by_table = table_to_topic(topics)

    agents = collect_agents(args.dataset_dir)
    agent_regions = {
        agent["name"]: resolve_folder_region(
            agent_region_slug(Path(agent["path"]).parent.name, config.topics), catalog
        )
        for agent in agents
    }
    out_dir = args.dataset_dir / "_meta" / "input"
    out_dir.mkdir(parents=True, exist_ok=True)
    stats: list[InputStats] = []
    for template_path in sorted(args.template_dir.glob("*.json")):
        template = json.loads(template_path.read_text())
        filled, stat = build_input(
            template,
            agents,
            agent_regions,
            catalog,
            all_countries,
            topics_by_table,
            args.ground_truth_db,
        )
        out = out_dir / template_path.name
        out.write_text(json.dumps(filled, indent=2) + "\n")
        stats.append(stat)
    print_stats(stats)
    log_dir = args.dataset_dir / "_meta" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    report = "\n".join(stats_lines(stats)) + "\n"
    (log_dir / "input.md").write_text(report)


if __name__ == "__main__":
    main()
