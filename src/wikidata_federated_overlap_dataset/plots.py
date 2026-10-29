"""Plots: coverage / overlap / world-map plots + csv data files."""

from __future__ import annotations

import csv
import math
import random
import re
from pathlib import Path
from types import ModuleType

from wikidata_federated_overlap_dataset.ground_truth import TableData
from wikidata_federated_overlap_dataset.planner import DatabasePlan, derived_seed, greedy_order
from wikidata_federated_overlap_dataset.regions import Region

FORMATS = ("svg", "png", "pdf")


def _save(fig, out_dir: Path, stem: str) -> None:
    """Save a figure in all three formats."""
    for fmt in FORMATS:
        fig.savefig(out_dir / f"{stem}.{fmt}", bbox_inches="tight")


def _matplotlib() -> tuple[ModuleType, ModuleType] | None:
    """Import matplotlib with the Agg backend; None when it is unavailable.

    Returns (pyplot, ticker) so callers can style axes without importing
    matplotlib themselves.
    """
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.ticker as mticker
    except ImportError:
        return None
    return plt, mticker


def plot_coverage(
    plots_dir: Path,
    topic_name: str,
    table_name: str,
    data: TableData,
    databases: list[Region],
    instances: dict[int, list[int]],
    ordering: str,
) -> None:
    """Coverage / new-unique / total-rows bar charts in one ordering + csv."""
    out_dir = plots_dir / f"plots_coverage_{ordering}"
    out_dir.mkdir(parents=True, exist_ok=True)
    row_sets = [set(instances[i]) for i in range(len(databases))]
    if ordering == "greedy":
        order = greedy_order([DatabasePlan(region=r) for r in databases], instances)
    elif ordering == "total_rows":
        # Higher db index wins ties: identical-content databases (single-country
        # topics) then order opposite to the greedy tie-break (lowest index
        # first), keeping the two orderings distinct (R9).
        order = sorted(range(len(databases)), key=lambda db: (-len(row_sets[db]), -db))
    else:
        rng = random.Random(derived_seed(0, "plot-order", topic_name, table_name))
        order = list(range(len(databases)))
        rng.shuffle(order)
    covered: set[int] = set()
    rows = []
    for position, db in enumerate(order):
        new_unique = len(row_sets[db] - covered)
        coverage = len(covered | row_sets[db]) / len(data.rows) * 100.0
        rows.append(
            {
                "position": position,
                "database": databases[db].slug,
                "coverage_pct": round(coverage, 2),
                "total_rows": len(row_sets[db]),
                "new_unique_rows": new_unique,
            }
        )
        covered |= row_sets[db]
    with (out_dir / f"{table_name}.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    _plot_coverage_chart(
        out_dir,
        table_name,
        topic_name,
        ordering,
        len(data.rows),
        rows,
    )


def _plot_coverage_chart(
    out_dir: Path,
    table_name: str,
    topic_name: str,
    ordering: str,
    total: int,
    rows: list[dict],
) -> None:
    """Cumulative coverage curve + total/new-rows bars, styled like the old plots."""
    modules = _matplotlib()
    if modules is None:
        return
    plt, mticker = modules

    if ordering == "greedy":
        xlabel = "Databases (greedy: each step adds the most new rows)"
        title_line1 = f"SELECT * FROM {table_name}"
    elif ordering == "total_rows":
        xlabel = "Databases (ordered by total rows desc, then later databases first)"
        title_line1 = f"SELECT * FROM {table_name} — distinct-rows ordering"
    else:
        xlabel = f"Databases (random order, topic={topic_name})"
        title_line1 = f"SELECT * FROM {table_name} — random ordering"

    n_db = len(rows)
    xs, ys = [0], [0.0]
    for row in rows:
        xs.append(len(xs))
        ys.append(min(row["coverage_pct"] / 100.0, 1.0))

    head_n = max(1, math.ceil(n_db * 0.20))
    hit_100 = next((i for i, y in enumerate(ys) if y >= 0.999), n_db)
    head_cov = ys[head_n] if head_n < len(ys) else ys[-1]

    fig, ax = plt.subplots(figsize=(11, 5))
    bar_ax = ax.twinx()
    bx = range(1, n_db + 1)
    bar_ax.bar(bx, [r["total_rows"] for r in rows], color="#cbd5e1", width=0.8, label="total rows")
    bar_ax.bar(
        bx, [r["new_unique_rows"] for r in rows], color="#ea580c", width=0.8, label="new rows"
    )
    bar_ax.set_ylabel("Rows per database")
    bar_ax.yaxis.set_major_locator(mticker.MaxNLocator(integer=True))

    ax.step(xs, ys, where="post", linewidth=1.8, color="#2563eb", zorder=5)
    ax.fill_between(xs, ys, step="post", alpha=0.15, color="#2563eb", zorder=4)
    ax.axhline(0.8, color="#dc2626", linestyle="--", linewidth=0.9, label="80% coverage", zorder=6)
    ax.axvline(
        head_n, color="#16a34a", linestyle=":", linewidth=1.0, label="20% of databases", zorder=6
    )
    ax.axvline(
        hit_100,
        color="#7c3aed",
        linestyle="-.",
        linewidth=1.0,
        label=f"100% at database {hit_100}",
        zorder=6,
    )
    ax.set_zorder(bar_ax.get_zorder() + 1)
    ax.patch.set_visible(False)
    ax.set_xlim(0, max(n_db + 1, 2))
    ax.set_ylim(0, 1.05)
    ax.set_xlabel(xlabel)
    ax.set_ylabel("Cumulative fraction of distinct rows")
    ax.set_title(
        f"{title_line1}\n{total} rows  |  {n_db} databases  "
        f"|  {head_cov:.0%} at database {head_n} (20%)"
    )
    ax.yaxis.set_major_formatter(mticker.PercentFormatter(xmax=1))
    ax.set_xticks(range(1, n_db + 1))
    ax.set_xticklabels(
        [f"{i}\n{row['database']}" for i, row in enumerate(rows, start=1)],
        fontsize=6,
        rotation=45,
        ha="right",
        rotation_mode="anchor",
    )
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = bar_ax.get_legend_handles_labels()
    ax.legend(
        h1 + h2,
        l1 + l2,
        fontsize=8,
        loc="center left",
        bbox_to_anchor=(1.12, 0.5),
        borderaxespad=0,
    )
    _save(fig, out_dir, table_name)
    plt.close(fig)


def plot_overlap(
    plots_dir: Path, topic_name: str, table_name: str, assigned_counts: list[int]
) -> None:
    """Histogram of per-row duplication counts + csv, styled like the old plots."""
    out_dir = plots_dir / "plots_overlap"
    out_dir.mkdir(parents=True, exist_ok=True)
    histogram: dict[int, int] = {}
    for k in assigned_counts:
        histogram[k] = histogram.get(k, 0) + 1
    with (out_dir / f"{table_name}.csv").open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["overlap_count", "rows"])
        writer.writerows(sorted(histogram.items()))
    modules = _matplotlib()
    if modules is None:
        return
    plt, _mticker = modules

    ks = sorted(histogram)
    mean = sum(k * c for k, c in histogram.items()) / len(assigned_counts)
    median = sorted(assigned_counts)[len(assigned_counts) // 2]
    table_cells = "  ".join(f"{k}x: {histogram[k]}" for k in ks)

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.bar(ks, [histogram[k] for k in ks], color="#2563eb", width=0.8)
    ax.set_xlabel("Appearances (# databases containing row)")
    ax.set_ylabel("# rows")
    ax.set_xticks(ks)
    ax.set_title(
        f"Row overlap distribution  —  {table_name}\n"
        f"mean={mean:.2f}  median={median:g}  max={max(ks)}  "
        f"total={len(assigned_counts)}\n{table_cells}",
        fontsize=9,
    )
    fig.tight_layout()
    _save(fig, out_dir, table_name)
    plt.close(fig)


def plot_distribution(
    plots_dir: Path, chosen: list[float], defaults: dict[str, list[float]]
) -> None:
    """Overlap-distribution plots: density (chosen + defaults) and cumulative, plus csv."""
    out_dir = plots_dir / "plots_distribution"
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "chosen_density.csv").open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["k", "p_density"])
        writer.writerows(enumerate(chosen, start=1))
    modules = _matplotlib()
    if modules is None:
        return
    plt, _mticker = modules

    fig, ax = plt.subplots(figsize=(9, 5))
    k_max = len(chosen)
    ks = range(1, k_max + 1)
    width = 0.15
    for offset, (name, curve) in enumerate([("chosen", chosen), *defaults.items()]):
        ax.bar([k + offset * width for k in ks], curve, width=width, label=name)
    ax.set_xlabel("k (databases per row)")
    ax.set_ylabel("P(k)")
    ax.set_title("Overlap distributions (chosen vs defaults)")
    ax.legend()
    _save(fig, out_dir, "overlap_distributions")
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(9, 5))
    chosen_cumulative = _cumulative(chosen)
    ax.step(range(1, k_max + 1), chosen_cumulative, where="post", linewidth=1.8, label="chosen")
    for name, curve in defaults.items():
        ax.step(
            range(1, len(curve) + 1),
            _cumulative(curve),
            where="post",
            linewidth=1.8,
            label=name,
        )
    ax.set_xlabel("k (databases per row)")
    ax.set_ylabel("P(K <= k)")
    ax.set_ylim(0, 1.05)
    ax.set_title("Cumulative overlap distributions")
    ax.legend()
    _save(fig, out_dir, "overlap_distributions_cumulative")
    plt.close(fig)


def _cumulative(density: list[float]) -> list[float]:
    """Running sum of a density (for the cumulative distribution plot)."""
    total = []
    running = 0.0
    for p in density:
        running += p
        total.append(running)
    return total


SVG_NAME_ALIASES = {
    "the_bahamas": "bahamas",
    "brunei": "brunei_darussalam",
    "people_s_republic_of_china": "china",
    "ivory_coast": "cote_d_ivoire",
    "north_korea": "dem_rep_korea",
    "laos": "lao_pdr",
    "north_macedonia": "macedonia",
    "kingdom_of_the_netherlands": "netherlands",
    "republic_of_the_congo": "republic_of_congo",
    "south_korea": "republic_of_korea",
    "russia": "russian_federation",
    "eswatini": "swaziland",
    "sao_tome_and_principe": "sao_tome_and_principe",
}

# svg name=/class= values that differ from the ground-truth country names,
# mapped to the ISO2 codes of the countries they represent.
_SVG_NAME_TO_ISO2 = {
    "Bahamas": "BS",
    "Brunei Darussalam": "BN",
    "China": "CN",
    "Côte d'Ivoire": "CI",
    "Dem. Rep. Korea": "KP",
    "Lao PDR": "LA",
    "Macedonia": "MK",
    "Netherlands": "NL",
    "Republic of Congo": "CG",
    "Republic of Korea": "KR",
    "Russian Federation": "RU",
    "Swaziland": "SZ",
    "São Tomé and Principe": "ST",
}


def plot_world_maps(
    plots_dir: Path,
    topic_name: str,
    table_name: str,
    databases: list[Region],
    country_labels: dict[int, str],
    country_names: dict[str, str],
    row_country_sets: list[frozenset[int]],
) -> None:
    """World map per table: countries with rows colored by their databases.

    The template tags countries three ways: id="ISO2", name="Name" and
    class="Name". Fills are keyed by ISO2; name/class tags resolve through
    the ground-truth names plus the svg-alias table. Only countries that
    actually hold rows of the table are colored — a database's region may
    span countries the table has no rows for. Countries covered by several
    databases get a hard-stop linear gradient; a legend maps colors to
    database names.
    """
    out_dir = plots_dir / "plots_map"
    out_dir.mkdir(parents=True, exist_ok=True)
    svg_text = (Path("config") / "world.svg").read_text()
    colors = _database_colors(len(databases))
    iso_to_dbs = _iso_country_dbs(databases, country_labels, row_country_sets)
    svg_text = _inject_gradient_defs(svg_text, iso_to_dbs, colors)
    svg_text = _inject_country_fills(svg_text, iso_to_dbs, colors, country_names)
    svg_text = _inject_legend(svg_text, topic_name, databases, colors)
    _save_svg_text(svg_text, out_dir, table_name)


def _iso_country_dbs(
    databases: list[Region],
    country_labels: dict[int, str],
    row_country_sets: list[frozenset[int]],
) -> dict[str, list[int]]:
    """Map ISO2 code -> indexes of databases holding rows of that country.

    A database holds a country's rows when its region intersects one of the
    row country sets containing the country — regions may span countries the
    table has no rows for, and those stay uncolored (R16).
    """
    row_countries = set().union(*row_country_sets) if row_country_sets else set()
    iso_to_dbs: dict[str, list[int]] = {}
    for db_index, region in enumerate(databases):
        for country_id in region.countries & row_countries:
            iso2 = country_labels.get(country_id)
            if iso2:
                iso_to_dbs.setdefault(iso2, []).append(db_index)
    return iso_to_dbs


def _inject_gradient_defs(
    svg_text: str, iso_to_dbs: dict[str, list[int]], colors: list[str]
) -> str:
    """Insert one hard-stop linearGradient per multi-database country after <svg>."""
    gradients = []
    for iso2, db_indexes in sorted(iso_to_dbs.items()):
        if len(db_indexes) < 2:
            continue
        fraction = 100.0 / len(db_indexes)
        stops = "".join(
            f'<stop offset="{position * fraction:.2f}%" stop-color="{colors[db_index]}"/>'
            f'<stop offset="{(position + 1) * fraction:.2f}%" stop-color="{colors[db_index]}"/>'
            for position, db_index in enumerate(db_indexes)
        )
        gradients.append(
            f'<linearGradient id="grad_{iso2}" gradientUnits="objectBoundingBox" '
            f'x1="0" y1="0" x2="1" y2="0">{stops}</linearGradient>'
        )
    if not gradients:
        return svg_text
    defs_block = f"<defs>{''.join(gradients)}</defs>"
    return re.sub(r"(<svg\b[^>]*>)", r"\1" + defs_block, svg_text, count=1)


def _inject_country_fills(
    svg_text: str,
    iso_to_dbs: dict[str, list[int]],
    colors: list[str],
    country_names: dict[str, str],
) -> str:
    """Set each covered country's fill via its id=, name= or class= attribute."""
    fills = {
        iso2: colors[db_indexes[0]] if len(db_indexes) == 1 else f"url(#grad_{iso2})"
        for iso2, db_indexes in iso_to_dbs.items()
    }
    name_to_iso = {**country_names, **_SVG_NAME_TO_ISO2}

    def inject(match: re.Match) -> str:
        tag = match.group(0)
        iso2 = _tag_iso2(tag, name_to_iso)
        if iso2 not in fills:
            return tag
        fill = fills[iso2]
        if "fill=" in tag:
            return re.sub(r'fill="[^"]*"', f'fill="{fill}"', tag)
        return tag[:-1] + f' fill="{fill}">'

    return re.sub(r"<path\b[^>]*>", inject, svg_text)


def _tag_iso2(tag: str, name_to_iso: dict[str, str]) -> str | None:
    """Resolve a path tag's country: id="XX" first, then name=/class= via aliases."""
    id_match = re.search(r'id="([A-Z]{2})"', tag)
    if id_match:
        return id_match.group(1)
    for attribute in ("name", "class"):
        value_match = re.search(rf'{attribute}="([^"]+)"', tag)
        if value_match and value_match.group(1) in name_to_iso:
            return name_to_iso[value_match.group(1)]
    return None


def _inject_legend(
    svg_text: str, topic_name: str, databases: list[Region], colors: list[str]
) -> str:
    """Add a color -> database-name legend in the empty Pacific area."""
    items = []
    lx, ly, row_h, max_label = 1450, 560, 13, 34
    for db_index, region in enumerate(databases):
        label = region.slug if len(region.slug) <= max_label else region.slug[: max_label - 1] + "…"
        x = lx
        y = ly + db_index * row_h
        items.append(
            f'<rect x="{x}" y="{y}" width="9" height="9" fill="{colors[db_index]}"/>'
            f'<text x="{x + 12}" y="{y + 8}" font-size="9" '
            f'font-family="sans-serif" fill="#111">{label}</text>'
        )
    legend = f'<g id="legend_{topic_name}">' + "".join(items) + "</g>"
    return svg_text.replace("</svg>", legend + "\n</svg>")


def _save_svg_text(svg_text: str, out_dir: Path, stem: str) -> None:
    """Write an SVG string in all three formats (svg directly, png/pdf via converter)."""
    import shutil
    import subprocess

    (out_dir / f"{stem}.svg").write_text(svg_text)
    svg_path = out_dir / f"{stem}.svg"
    converter = shutil.which("rsvg-convert") or shutil.which("inkscape")
    if converter is None:
        return
    if converter.endswith("rsvg-convert"):
        png_cmd = [converter, "-w", "2000", "-o", str(out_dir / f"{stem}.png")]
        pdf_cmd = [converter, "-f", "pdf", "-o", str(out_dir / f"{stem}.pdf")]
    else:
        png_cmd = [
            converter,
            "--export-type=png",
            "--export-width=2000",
            f"--export-filename={out_dir / f'{stem}.png'}",
        ]
        pdf_cmd = [converter, "--export-type=pdf", f"--export-filename={out_dir / f'{stem}.pdf'}"]
    subprocess.run([*png_cmd, str(svg_path)], check=True)
    subprocess.run([*pdf_cmd, str(svg_path)], check=True)


def _database_colors(n: int) -> list[str]:
    """n visually distinct colors."""
    import colorsys

    colors = []
    for i in range(n):
        hue = i / max(n, 1)
        r, g, b = colorsys.hsv_to_rgb(hue, 0.75, 0.9)
        colors.append(f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}")
    return colors
