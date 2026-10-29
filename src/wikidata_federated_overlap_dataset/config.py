"""Configuration loading: config/*.yaml + CLI arguments into frozen dataclasses."""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from pathlib import Path

import yaml

# Overlap distribution defaults.
# 80/20 rule: the top 20% of the values carry 80% of the total mass. For a
# pareto tail P(K > k) = k^-alpha that means the density decays as
# k^-(alpha + 1) with LAMBDA = ln(0.2) / (ln(0.2) - ln(0.8)) = 1.161, so
# alpha = LAMBDA + 1 = 2.161.
# - exponential: rate 0.5, i.e. mean 2 (the default distribution).
# - gpd: shape xi = 1/alpha, scale sigma = 1/alpha, threshold mu = 1 gives
#   tail P(X > x) = (1 + (x-1))^(-alpha) = x^-alpha — exactly the pareto
#   tail, so the default GPD equals the default pareto.
# - zipf: pmf P(K = k) proportional to k^-s with s = alpha + 1, matching the
#   pareto bucket decay (pareto buckets approximate the density k^-(alpha+1)).
# Bucket convention: P(K = k) = F(k+1) - F(k) with the distribution truncated
# to [1, k_max + 1) — no bucket absorbs an infinite tail.
LAMBDA = math.log(0.2) / (math.log(0.2) - math.log(0.8))
PARETO_ALPHA = LAMBDA + 1.0
EXPONENTIAL_RATE = 0.5
GPD_XI = 1.0 / PARETO_ALPHA
GPD_SIGMA = 1.0 / PARETO_ALPHA
GPD_MU = 1.0
ZIPF_EXPONENT = PARETO_ALPHA + 1.0

DISTRIBUTIONS = ("uniform", "pareto", "exponential", "gpd", "zipf")


@dataclass(frozen=True)
class BuildConfig:
    """Global build parameters derived from CLI arguments."""

    seed: int
    target: int
    overlap_distribution: str
    overlap_params: dict[str, float]
    overlap_cap: float
    drop_probability: float
    output_dir: Path
    topics: tuple[str, ...]
    skew_limit: float = 3.0
    band_half_width: float = 0.05


@dataclass(frozen=True)
class TopicConfig:
    """One topic from config/topics.yaml."""

    name: str
    tables: tuple[str, ...]
    description: str


@dataclass(frozen=True)
class ColumnConfig:
    """One column of one table from config/columns.yaml."""

    name: str
    description: str
    synonyms: tuple[str, ...]


@dataclass(frozen=True)
class TableConfig:
    """One table from config/columns.yaml."""

    name: str
    primary_key: str | None
    split_col: str | None
    value_column: str | None
    value_inverted: bool
    foreign_keys: dict[str, str]
    required: tuple[str, ...]
    description: str
    table_synonyms: tuple[str, ...]
    columns: dict[str, ColumnConfig]


@dataclass(frozen=True)
class GroupConfig:
    """One country group from config/group_countries.yaml."""

    name: str
    label: str
    countries: tuple[str, ...]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments for the dataset builder."""
    parser = argparse.ArgumentParser(prog="dataset-builder")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--target", type=int, default=15)
    parser.add_argument(
        "--overlap-distribution",
        choices=DISTRIBUTIONS,
        default="exponential",
        help="overlap distribution: uniform, truncated pareto, truncated exponential"
        " (mean 2), truncated generalized pareto or zipf",
    )
    parser.add_argument("--overlap-distribution-shape", type=float, default=None)
    parser.add_argument("--overlap-distribution-rate", type=float, default=None)
    parser.add_argument("--overlap-cap", type=float, default=2.0 / 3.0)
    parser.add_argument("--drop-probability", type=float, default=0.25)
    parser.add_argument("--output-dir", type=Path, default=Path("dataset"))
    parser.add_argument("--topics", type=str, default=None, help="comma-separated topic subset")
    parser.add_argument("--skew-limit", type=float, default=3.0)
    parser.add_argument(
        "--band-half-width",
        type=float,
        default=0.05,
        help="half width of the 80/20 head band: the head must cover [0.8 - w, 0.8 + w] of each table's rows",
    )
    return parser.parse_args(argv)


def default_overlap_params(distribution: str) -> dict[str, float]:
    """Return the default parameters for the chosen overlap distribution."""
    if distribution == "pareto":
        return {"shape": PARETO_ALPHA}
    if distribution == "exponential":
        return {"rate": EXPONENTIAL_RATE}
    if distribution == "gpd":
        return {"shape": GPD_XI, "scale": GPD_SIGMA, "threshold": GPD_MU}
    if distribution == "zipf":
        return {"shape": ZIPF_EXPONENT}
    return {}


def validate_overlap_params(
    distribution: str,
    shape: float | None,
    rate: float | None,
) -> dict[str, float]:
    """Validate that only the parameters allowed for the distribution are given."""
    allowed = {
        "uniform": set(),
        "pareto": {"shape"},
        "exponential": {"rate"},
        "gpd": {"shape"},
        "zipf": {"shape"},
    }
    given = {"shape": shape, "rate": rate}
    given = {k: v for k, v in given.items() if v is not None}
    invalid = set(given) - allowed[distribution]
    if invalid:
        msg = f"parameters {sorted(invalid)} are not allowed for distribution '{distribution}'"
        raise SystemExit(msg)
    return {**default_overlap_params(distribution), **given}


def load_topics(path: Path, only: tuple[str, ...] | None) -> list[TopicConfig]:
    """Load topic configs from config/topics.yaml, optionally filtered."""
    raw = yaml.safe_load(path.read_text())
    topics = [
        TopicConfig(name=k, tables=tuple(v["tables"]), description=v["description"])
        for k, v in raw.items()
    ]
    if only:
        unknown = set(only) - {t.name for t in topics}
        if unknown:
            msg = f"unknown topics: {sorted(unknown)}"
            raise SystemExit(msg)
        topics = [t for t in topics if t.name in only]
    return topics


def load_tables(path: Path, topics: list[TopicConfig]) -> dict[str, TableConfig]:
    """Load table configs for all tables referenced by the topics."""
    raw = yaml.safe_load(path.read_text())
    needed = {name for topic in topics for name in topic.tables}
    tables: dict[str, TableConfig] = {}
    for name, spec in raw.items():
        if name not in needed:
            continue
        meta = spec["__meta__"]
        columns = {
            col: ColumnConfig(
                name=col,
                description=spec[col]["description"],
                synonyms=tuple(spec[col].get("synonyms", {})),
            )
            for col in spec
            if col != "__meta__"
        }
        tables[name] = TableConfig(
            name=name,
            primary_key=meta.get("primary_key"),
            split_col=meta.get("split_col"),
            value_column=meta.get("value_column"),
            value_inverted=bool(meta.get("value_inverted", False)),
            foreign_keys=dict(meta.get("foreign_keys") or {}),
            required=tuple(meta.get("required") or ()),
            description=meta["description"],
            table_synonyms=tuple(meta.get("synonyms", {})),
            columns=columns,
        )
    missing = needed - set(tables)
    if missing:
        msg = f"tables missing from columns.yaml: {sorted(missing)}"
        raise SystemExit(msg)
    return tables


def load_groups(path: Path) -> list[GroupConfig]:
    """Load country group configs from config/group_countries.yaml."""
    raw = yaml.safe_load(path.read_text())
    return [
        GroupConfig(name=k, label=v["label"], countries=tuple(v["countries"]))
        for k, v in raw.items()
    ]


def load_labels(path: Path) -> dict[str, str]:
    """Load the slug -> human-readable label mapping."""
    return yaml.safe_load(path.read_text())


def load_config(
    argv: list[str] | None = None,
) -> tuple[
    BuildConfig, list[TopicConfig], dict[str, TableConfig], list[GroupConfig], dict[str, str]
]:
    """Load the full configuration: CLI + all yaml files."""
    args = parse_args(argv)
    if not 1.0 / args.target <= args.overlap_cap <= 1.0:
        msg = f"--overlap-cap must lie in [1/{args.target}, 1.0], got {args.overlap_cap}"
        raise SystemExit(msg)
    if not 0.0 <= args.drop_probability <= 1.0:
        msg = f"--drop-probability must lie in [0, 1], got {args.drop_probability}"
        raise SystemExit(msg)
    only = tuple(args.topics.split(",")) if args.topics else None
    topics = load_topics(Path("config/topics.yaml"), only)
    tables = load_tables(Path("config/columns.yaml"), topics)
    groups = load_groups(Path("config/group_countries.yaml"))
    labels = load_labels(Path("config/labels.yaml"))
    params = validate_overlap_params(
        args.overlap_distribution,
        args.overlap_distribution_shape,
        args.overlap_distribution_rate,
    )
    config = BuildConfig(
        seed=args.seed,
        target=args.target,
        overlap_distribution=args.overlap_distribution,
        overlap_params=params,
        overlap_cap=args.overlap_cap,
        drop_probability=args.drop_probability,
        output_dir=args.output_dir,
        topics=tuple(t.name for t in topics),
        skew_limit=args.skew_limit,
        band_half_width=args.band_half_width,
    )
    return config, topics, tables, groups, labels
