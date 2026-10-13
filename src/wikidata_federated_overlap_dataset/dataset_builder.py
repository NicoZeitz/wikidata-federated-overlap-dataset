"""Pipeline orchestration: common setup -> per-topic parallel -> validate -> build log."""

from __future__ import annotations

import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from wikidata_federated_overlap_dataset.config import load_config
from wikidata_federated_overlap_dataset.ground_truth import load_ground_truth
from wikidata_federated_overlap_dataset.logging_utils import (
    TopicLog,
    write_build_log,
    write_topic_log,
)
from wikidata_federated_overlap_dataset.overlap import OverlapSampler, default_distribution_curves
from wikidata_federated_overlap_dataset.planner import plan_topic
from wikidata_federated_overlap_dataset.plots import (
    plot_coverage,
    plot_distribution,
    plot_overlap,
    plot_world_maps,
)
from wikidata_federated_overlap_dataset.regions import build_region_catalog
from wikidata_federated_overlap_dataset.validation import validate_global, validate_written_topic
from wikidata_federated_overlap_dataset.writer import write_meta_mapping, write_topic


def main(argv: list[str] | None = None) -> int:
    """Entry point: run the full build pipeline."""
    start = time.time()
    config, topics, tables_config, groups, labels = load_config(argv)
    output_dir = config.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    meta_dir = output_dir / "_meta"
    logs_dir = meta_dir / "logs"
    plots_dir = meta_dir

    phase_timings: dict[str, float] = {}

    t0 = time.time()
    ground_truth = load_ground_truth(Path("results/ground_truth.db"), tables_config)
    phase_timings["load_ground_truth"] = time.time() - t0

    t0 = time.time()
    catalog = build_region_catalog(
        ground_truth.countries.rows, groups, labels, topics, ground_truth.tables
    )
    phase_timings["build_regions"] = time.time() - t0

    sampler = OverlapSampler.from_config(config)
    plot_distribution(
        plots_dir, sampler.bucket_probabilities(), default_distribution_curves(sampler.k_max)
    )

    country_labels = {c.id: c.iso2 for c in ground_truth.countries.rows if c.iso2}
    country_names = {c.name: c.iso2 for c in ground_truth.countries.rows if c.iso2}
    worker_payload = [
        (topic, ground_truth.tables, tables_config, catalog, config, country_labels, country_names)
        for topic in topics
    ]
    t0 = time.time()
    results = []
    with ProcessPoolExecutor() as executor:
        futures = [executor.submit(_process_topic, payload) for payload in worker_payload]
        results = [f.result() for f in futures]
    phase_timings["topic_workers"] = time.time() - t0

    t0 = time.time()
    global_reports = validate_global(topics, ground_truth.tables, tables_config, output_dir, config)
    phase_timings["global_validation"] = time.time() - t0

    topic_summaries = []
    all_warnings = []
    failed = False
    for topic, (_log, report) in zip(topics, results, strict=True):
        global_report = global_reports[topic.name]
        for e in global_report.errors:
            report.error(e)
        status = "OK" if report.ok else "FAILED"
        topic_summaries.append(
            f"- {topic.name}: {status}, {len(report.errors)} errors, "
            f"{len(report.warnings)} warnings"
        )
        all_warnings.extend(f"{topic.name}: {w}" for w in report.warnings)
        if not report.ok:
            failed = True
    write_meta_mapping(output_dir, topics)
    write_build_log(logs_dir, phase_timings, topic_summaries, all_warnings, time.time() - start)
    return 1 if failed else 0


def _process_topic(payload: tuple) -> tuple:
    """Worker: plan, write, validate, log, plot one topic (runs in its own process)."""
    topic, tables_data, tables_config, catalog, config, country_labels, country_names = payload
    topic_start = time.time()
    log = TopicLog(topic_name=topic.name)
    sampler = OverlapSampler.from_config(config)
    plan = plan_topic(topic, tables_data, tables_config, catalog, config, sampler)
    log.phases["plan"] = time.time() - topic_start
    log.regions = [db.region.slug for db in plan.databases]
    log.widening = plan.widening_log
    t0 = time.time()
    folders = write_topic(config.output_dir, topic, plan, tables_data, tables_config, config)
    log.phases["write"] = time.time() - t0
    t0 = time.time()
    report = validate_written_topic(topic, folders, plan, tables_data, tables_config, config)
    log.phases["validate"] = time.time() - t0
    log.report = report
    for name, table_plan in plan.tables.items():
        data = tables_data[name]
        log.table_stats.append(
            f"- {name}: rows={len(data.rows)}, head_rows={len(table_plan.head_rows)}, "
            f"total_instances={sum(len(v) for v in table_plan.instances.values())}"
        )
    t0 = time.time()
    regions = [db.region for db in plan.databases]
    for name, table_plan in plan.tables.items():
        data = tables_data[name]
        for ordering in ("greedy", "random", "total_rows"):
            plot_coverage(
                config.output_dir / "_meta",
                topic.name,
                name,
                data,
                regions,
                table_plan.instances,
                ordering,
            )
        plot_overlap(config.output_dir / "_meta", topic.name, name, table_plan.assigned_counts)
        plot_world_maps(
            config.output_dir / "_meta",
            topic.name,
            name,
            regions,
            country_labels,
            country_names,
            data.row_country_sets,
        )
    log.phases["plots"] = time.time() - t0
    log.phases["total"] = time.time() - topic_start
    write_topic_log(config.output_dir / "_meta" / "logs", log)
    return log, report


if __name__ == "__main__":
    raise SystemExit(main())
