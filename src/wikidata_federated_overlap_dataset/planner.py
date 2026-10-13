"""Per-topic planning: regions (4a), counts (4b), assignment (4c), roles (4d),
placement (4e), repair (4f), merge (4g), orders + band check (4h)."""

from __future__ import annotations

import math
import random
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field

from wikidata_federated_overlap_dataset.config import BuildConfig, TableConfig, TopicConfig
from wikidata_federated_overlap_dataset.ground_truth import TableData
from wikidata_federated_overlap_dataset.overlap import OverlapSampler
from wikidata_federated_overlap_dataset.regions import Region, RegionCatalog

MAX_WIDENING_ROUNDS = 30
MAX_DRAWS = 100
MAX_BAND_RETRIES = 8


@dataclass
class DatabasePlan:
    """One planned database: its region and the row instances per table."""

    region: Region


@dataclass
class TablePlan:
    """Planning result for one table."""

    assigned_counts: list[int]
    head_rows: set[int]
    instances: dict[int, list[int]] = field(default_factory=dict)
    band_lower: int | None = None


@dataclass
class TopicPlan:
    """Complete plan for one topic: databases + per-table instances."""

    databases: list[DatabasePlan]
    tables: dict[str, TablePlan]
    widening_log: list[str] = field(default_factory=list)


def derived_seed(seed: int, *parts: object) -> int:
    """Deterministic seed derivation from the master seed and context parts."""
    import hashlib

    digest = hashlib.sha256(":".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(digest[:8], "big")


def plan_topic(
    topic: TopicConfig,
    tables_data: dict[str, TableData],
    tables_config: dict[str, TableConfig],
    catalog: RegionCatalog,
    config: BuildConfig,
    sampler: OverlapSampler,
) -> TopicPlan:
    """Run the full planning pipeline 4a-4h for one topic."""
    topic_seed = derived_seed(config.seed, topic.name)
    rng = random.Random(topic_seed)
    candidates = catalog.candidates[topic.name]
    databases = choose_database_regions(
        topic, tables_data, candidates, config.target, catalog, sampler.expected_count()
    )
    plan = TopicPlan(databases=[DatabasePlan(region=r) for r in databases], tables={})
    for table in topic.tables:
        table_plan = plan_table(
            table,
            tables_data[table],
            tables_config[table],
            plan.databases,
            config,
            sampler,
            rng,
            catalog,
            plan.widening_log,
        )
        plan.tables[table] = table_plan
    merge_balanced(plan, tables_data, config)
    verify_band(plan, tables_data, config)
    return plan


def head_size(target: int) -> int:
    """Number of head databases: ceil(target * 0.2)."""
    return math.ceil(target * 0.2)


def band_bounds(n: int) -> tuple[int, int]:
    """Widened 80/20 band: [ceil(0.75n), floor(0.85n)] or the two integers around 0.8n."""
    lower, upper = math.ceil(0.75 * n), math.floor(0.85 * n)
    if lower <= upper:
        return lower, upper
    return math.floor(0.80 * n), math.ceil(0.80 * n)


def choose_database_regions(
    topic: TopicConfig,
    tables_data: dict[str, TableData],
    candidates: list[Region],
    target: int,
    catalog: RegionCatalog,
    expected_count: float,
) -> list[Region]:
    """4a: pick target regions tight-first (head greedy + tail coverage)."""
    h_size = head_size(target)
    head_targets = {t: round(0.80 * len(tables_data[t].rows)) for t in topic.tables}
    head = _pick_head(topic.tables, tables_data, candidates, h_size, head_targets)
    tail = _pick_tail(
        topic.tables, tables_data, candidates, target - h_size, head, catalog, expected_count
    )
    return head + tail


def _head_eligible_rows(region: Region, data: TableData) -> int:
    """Rows of the table eligible for the region (region intersects the row's country set).

    Uses distinct country sets with row counts — O(distinct sets), not O(rows).
    """
    return sum(
        count for cs, count in _country_set_counts(data).items() if not cs or region.countries & cs
    )


_SET_COUNTS_CACHE: dict[int, dict[frozenset[int], int]] = {}


def _country_set_counts(data: TableData) -> dict[frozenset[int], int]:
    """Distinct row country sets with their row counts (cached per table by id)."""
    key = id(data)
    if key not in _SET_COUNTS_CACHE:
        counts: dict[frozenset[int], int] = {}
        for cs in data.row_country_sets:
            counts[cs] = counts.get(cs, 0) + 1
        _SET_COUNTS_CACHE[key] = counts
    return _SET_COUNTS_CACHE[key]


def _pick_head(
    table_names: Sequence[str],
    tables_data: dict[str, TableData],
    candidates: list[Region],
    h_size: int,
    head_targets: dict[str, int],
) -> list[Region]:
    """Greedy head selection: fair-share union growth, then cover the rest.

    Each pick's gain is the newly covered rows (rows eligible for the region
    but for no chosen region so far), capped at the remaining per-slot share
    (head_target - covered) / remaining_slots. The cap keeps the head regions
    comparable in size — without it the first pick is worldwide (gain = the
    whole target) and the other head dbs stay tiny (R9). Among regions with
    equal capped gain the densest (rows per country, R8) wins. Once every
    table's target is saturated, the remaining slots run set-cover over the
    row country sets the chosen regions cannot hold so every head row has a
    head database to live in (R6, R7). Regions may be reused across picks.
    """
    chosen: list[Region] = []
    covered = {t: 0 for t in table_names}
    chosen_countries: frozenset[int] = frozenset()
    remaining = sorted(candidates, key=lambda r: (r.tightness, r.slug))
    set_counts = _set_row_counts(table_names, tables_data)
    for index in range(h_size):
        saturated = all(covered[t] >= head_targets[t] for t in table_names)
        best: Region | None = None
        best_key: tuple[float, float] | None = None
        for region in remaining:
            if saturated:
                gain = float(
                    sum(
                        count
                        for (t, cs), count in set_counts.items()
                        if cs and not cs & chosen_countries and cs & region.countries
                    )
                )
            else:
                gain = 0.0
                for t in table_names:
                    share = (head_targets[t] - covered[t]) / (h_size - index)
                    newly = sum(
                        count
                        for (name, cs), count in set_counts.items()
                        if name == t and cs and not cs & chosen_countries and cs & region.countries
                    )
                    gain += min(newly, share)
            key = (gain, _row_density(region, table_names, tables_data))
            if best_key is None or key > best_key:
                best, best_key = region, key
        if best is None:
            break
        chosen.append(best)
        chosen_countries = frozenset().union(chosen_countries, best.countries)
        for t in table_names:
            covered[t] = sum(
                count
                for (name, cs), count in set_counts.items()
                if name == t and cs and cs & chosen_countries
            )
    return chosen


def _set_row_counts(
    table_names: Sequence[str],
    tables_data: dict[str, TableData],
) -> dict[tuple[str, frozenset[int]], int]:
    """Row count per (table, distinct row country set) for the topic's tables."""
    counts: dict[tuple[str, frozenset[int]], int] = {}
    for name in table_names:
        for cs in tables_data[name].row_country_sets:
            counts[(name, cs)] = counts.get((name, cs), 0) + 1
    return counts


def _row_density(
    region: Region,
    table_names: Sequence[str],
    tables_data: dict[str, TableData],
) -> float:
    """Eligible rows of the region across all topic tables, per country (R8 weight)."""
    rows = sum(_head_eligible_rows(region, tables_data[t]) for t in table_names)
    return rows / max(len(region.countries), 1)


def _pick_tail(
    table_names: Sequence[str],
    tables_data: dict[str, TableData],
    candidates: list[Region],
    tail_slots: int,
    head: Sequence[Region],
    catalog: RegionCatalog,
    expected_count: float,
) -> list[Region]:
    """Tail selection: tightest regions first until every row country set is covered.

    If the slots run out before full coverage, chosen regions widen into the
    smallest catalog supersets that cover the remaining sets (4a widen branch).
    """
    all_sets = [cs for t in table_names for cs in tables_data[t].row_country_sets]
    head_countries = frozenset().union(*(r.countries for r in head)) if head else frozenset()
    uncovered = {cs for cs in all_sets if cs and not cs & head_countries}
    chosen: list[Region] = []
    # Head regions stay in the pool: the head is a multiset and reusing one
    # of its regions as a tail db is the tightest option (R8) — excluding
    # them forced single-country topics onto neighbor/group regions.
    pool = sorted(candidates, key=lambda r: (r.tightness, len(r.countries), r.slug))
    while len(chosen) < tail_slots and uncovered:
        scored = [
            (
                sum(1 for cs in uncovered if cs & region.countries),
                len(region.countries),
                region,
            )
            for region in pool
        ]
        if not scored:
            break
        max_covered = max(score for score, _size, _region in scored)
        if max_covered == 0:
            break
        # Among regions near the coverage maximum prefer the smallest (R8):
        # tight multi-country regions beat worldwide when they cover ~as much.
        near = [
            (score, size, region) for score, size, region in scored if score >= 0.9 * max_covered
        ]
        best = min(near, key=lambda t: (t[0], -t[1], t[2].slug))[2]
        chosen.append(best)
        uncovered = {cs for cs in uncovered if not cs & best.countries}
    chosen.extend(
        _pick_tail_band_regions(
            table_names, tables_data, head, chosen, pool, tail_slots, expected_count
        )
    )
    fill = _fill_tail_slots(
        table_names, tables_data, head, chosen, tail_slots - len(chosen), candidates, expected_count
    )
    chosen.extend(fill)
    if uncovered:
        chosen = _widen_tail_for_coverage(chosen, uncovered, catalog)
        remaining = {cs for cs in uncovered if not any(cs & r.countries for r in chosen)}
        if remaining:
            msg = f"tail regions cannot cover {len(remaining)} row country sets"
            raise RuntimeError(msg)
    return chosen


def _pick_tail_band_regions(
    table_names: Sequence[str],
    tables_data: dict[str, TableData],
    head: Sequence[Region],
    chosen: list[Region],
    pool: list[Region],
    tail_slots: int,
    expected_count: float,
) -> list[Region]:
    """Regions that let the tail absorb the rows the 80/20 band keeps out of the head.

    The greedy front union may hold at most band_upper distinct rows per table;
    rows beyond the head target must live in tail databases, spread so that no
    single tail region joins the greedy front with too many new rows (R6), and
    every row needs enough tail databases for its duplicates (R4/R5). When the
    chosen tail regions cannot absorb them, this adds the smallest regions that
    absorb the most additional rows (R6 + R8).
    """
    band_regions: list[Region] = []
    while len(chosen) + len(band_regions) < tail_slots:
        deficit = _band_absorption_deficit(
            table_names, tables_data, head, chosen, band_regions, expected_count
        )
        if deficit is None:
            break
        tail_regions = [r for r in [*chosen, *band_regions] if r not in head]
        scored = [
            (
                _band_absorption_gain(region, deficit, tables_data, tail_regions, expected_count),
                len(region.countries),
                region,
            )
            for region in pool
        ]
        if not scored:
            break
        max_gain = max(gain for gain, _size, _region in scored)
        if max_gain <= 0:
            break
        near = [(gain, size, region) for gain, size, region in scored if gain >= 0.9 * max_gain]
        best = min(near, key=lambda t: (-t[0], t[1], t[2].slug))[2]
        band_regions.append(best)
    return band_regions


def _band_absorption_deficit(
    table_names: Sequence[str],
    tables_data: dict[str, TableData],
    head: Sequence[Region],
    chosen: list[Region],
    band_regions: list[Region],
    expected_count: float,
) -> dict[str, int] | None:
    """Per table: rows the band keeps out of the head that the tail cannot absorb.

    Two conditions per table. (1) Duplicate capacity: a row places one
    instance in the head and the rest in the tail, so rows whose tail capacity
    cannot hold their expected duplicates are forced into the head union; the
    forced count must stay at or below the band lower bound. (2) Spread: the
    remaining non-head rows must be assignable to tail regions with a per-region
    capacity that keeps the greedy front union inside the band ceiling
    (bipartite flow). Returns None when every table's non-head rows are
    absorbable.
    """
    deficit: dict[str, int] = {}
    tail_regions = [r for r in [*chosen, *band_regions] if r not in head]
    for name in table_names:
        data = tables_data[name]
        n = len(data.rows)
        band_lower, band_upper = band_bounds(n)
        set_counts = _country_set_counts(data)
        forced = sum(
            count
            for cs, count in set_counts.items()
            if _set_tail_capacity(cs, tail_regions) < math.ceil(expected_count) - 1
        )
        if forced > band_lower:
            deficit[name] = forced - band_lower
            continue
        # Prefer feasibility at the initial head target (keeps the tail
        # regions tight, R8); the band retry can lower head_target down to
        # band_lower, so that looser capacity is the fallback (R6).
        required = n - round(0.80 * n)
        flow = _spread_flow(set_counts, tail_regions, band_upper - round(0.80 * n), required)
        if flow < required:
            required = n - band_lower
            flow = _spread_flow(set_counts, tail_regions, band_upper - band_lower, required)
        if required - flow > 0:
            deficit[name] = required - flow
    return deficit or None


def _set_tail_capacity(country_set: frozenset[int], tail_regions: list[Region]) -> int:
    """Number of tail regions whose area intersects the row country set."""
    if not country_set:
        return sum(1 for region in tail_regions if region.kind == "worldwide")
    return sum(1 for region in tail_regions if region.countries & country_set)


def _spread_flow(
    set_counts: dict[frozenset[int], int],
    tail_regions: list[Region],
    capacity: int,
    required: int,
) -> int:
    """Max rows assignable to tail regions with a per-region row capacity.

    A row country set is assignable to a region iff the region intersects it.
    Edmonds-Karp at country-set granularity: source -> sets (capacity = row
    count) -> regions (capacity = per-region spread cap) -> sink. O(sets x
    regions) per augmentation, independent of the row count behind each set.
    """
    set_list = list(set_counts)
    set_index = {cs: i for i, cs in enumerate(set_list)}
    region_nodes = {i: len(set_list) + i for i in range(len(tail_regions))}
    source, sink = -1, -2
    graph: dict[int, dict[int, int]] = {}

    def add_edge(u: int, v: int, c: int) -> None:
        graph.setdefault(u, {})
        graph[u][v] = graph[u].get(v, 0) + c
        graph.setdefault(v, {}).setdefault(u, 0)

    for cs, count in set_counts.items():
        add_edge(source, set_index[cs], count)
        for i, region in enumerate(tail_regions):
            if not cs or region.countries & cs:
                add_edge(set_index[cs], region_nodes[i], count)
    for i in range(len(tail_regions)):
        add_edge(region_nodes[i], sink, capacity)
    return _edmonds_karp(graph, source, sink, stop_at=required)


def _band_absorption_gain(
    region: Region,
    deficit: dict[str, int],
    tables_data: dict[str, TableData],
    tail_regions: list[Region],
    expected_count: float,
) -> float:
    """Progress the region makes toward absorbing the deficit rows.

    For duplicate-capacity deficits the gain counts the deficit rows the
    region newly intersects — each intersection raises their tail capacity by
    one toward the required threshold, so progress is monotone even when a
    single region cannot cross the threshold alone. For spread deficits the
    gain is the exact flow improvement with the candidate appended (R6).
    """
    gain = 0.0
    needed = math.ceil(expected_count) - 1
    for name, short in deficit.items():
        data = tables_data[name]
        n = len(data.rows)
        band_lower, band_upper = band_bounds(n)
        set_counts = _country_set_counts(data)
        below = {
            cs: count
            for cs, count in set_counts.items()
            if _set_tail_capacity(cs, tail_regions) < needed
        }
        if sum(below.values()) > band_lower:
            gain += sum(count for cs, count in below.items() if cs and cs & region.countries)
            continue
        capacity = band_upper - band_lower
        required = n - band_lower
        flow = _spread_flow(set_counts, [*tail_regions, region], capacity, required)
        gain += short - (required - flow)
    return gain


def _fill_tail_slots(
    table_names: Sequence[str],
    tables_data: dict[str, TableData],
    head: Sequence[Region],
    chosen: list[Region],
    slots: int,
    candidates: list[Region],
    expected_count: float,
) -> list[Region]:
    """Allocate the remaining tail slots by sustainable duplicate supply.

    Each region earns slots proportional to its row density, but no region
    may receive more duplicates than the data can sustain: a region with E
    eligible rows supplies ~E * E[K] instances while the head keeps ~80% of
    rows and every existing db of the region consumes one instance per table
    (R7 + R9). The ledger prevents the continents failure mode where 6 india
    dbs share one row. At most a third of the fill slots go to one region
    (R9 diversity); when the ledger exhausts before the slots are filled,
    the rest is distributed round-robin by density and the per-table flow
    repair guards feasibility. Smaller regions win ties (R8).
    """
    if slots <= 0:
        return []
    head_union = frozenset().union(*(r.countries for r in head)) if head else frozenset()
    # Regions strictly containing the head union add no coverage but
    # concentrate non-head rows in one db, pushing the greedy front over
    # the 80/20 band ceiling (R6) — they never receive fill slots.
    pool = {r for r in set(head) | set(chosen) if not (head_union < r.countries)} | {
        r for r in candidates if not (head_union < r.countries)
    }
    ordered_pool = sorted(pool, key=lambda r: r.slug)
    if not ordered_pool:
        return []

    weights = {region: _row_density(region, table_names, tables_data) for region in ordered_pool}
    total_weight = sum(weights.values())
    if total_weight <= 0:
        fallback = sorted(ordered_pool, key=lambda r: (r.tightness, len(r.countries), r.slug))
        return [fallback[i % len(fallback)] for i in range(slots)]
    existing = {region.slug: 0 for region in ordered_pool}
    for region in [*head, *chosen]:
        if region.slug in existing:
            existing[region.slug] += 1
    allocation = {region: 0 for region in ordered_pool}
    # Countries first, then neighbors, groups, worldwide (R8); density ranks
    # within each kind so country dbs land where the data is.
    by_preference = sorted(
        ordered_pool,
        key=lambda r: (r.tightness, -weights[r], len(r.countries), r.slug),
    )
    share_cap = math.ceil(slots / 3)
    for region in by_preference:
        ledger = _duplicate_cap(region, table_names, tables_data, expected_count)
        limit = min(max(ledger - existing[region.slug], 0), share_cap)
        while allocation[region] < limit and sum(allocation.values()) < slots:
            allocation[region] += 1
    index = 0
    while sum(allocation.values()) < slots:
        region = by_preference[index % len(by_preference)]
        if allocation[region] < share_cap:
            allocation[region] += 1
        index += 1
        if index >= len(by_preference) * share_cap:
            break
    if sum(allocation.values()) < slots:
        # Every region is at its share cap — the caps were too tight for the
        # slot count; relax them evenly (flow repair guards feasibility).
        index = 0
        while sum(allocation.values()) < slots:
            allocation[by_preference[index % len(by_preference)]] += 1
            index += 1
    filled: list[Region] = []
    for region in by_preference:
        filled.extend([region] * allocation[region])
    return filled[:slots]


def _duplicate_cap(
    region: Region,
    table_names: Sequence[str],
    tables_data: dict[str, TableData],
    expected_count: float,
) -> int:
    """Max duplicate dbs of this region the data can sustain (supply ledger).

    The region's scarcest table has E eligible rows supplying ~E * E[K]
    instances; the head keeps ~80% of rows, leaving E * (E[K] - 0.8)
    instances for duplicate databases.
    """
    eligible = min(_head_eligible_rows(region, tables_data[t]) for t in table_names)
    return int(eligible * (expected_count - 0.8))


def _widen_tail_for_coverage(
    chosen: list[Region],
    uncovered: set[frozenset[int]],
    catalog: RegionCatalog,
) -> list[Region]:
    """Widen chosen tail regions into smallest supersets covering the uncovered sets."""
    from wikidata_federated_overlap_dataset.regions import supersets_containing

    widened = list(chosen)
    for country_set in sorted(uncovered, key=sorted):
        if any(cs & region.countries for cs in [country_set] for region in widened):
            continue
        for index, region in enumerate(widened):
            if region.kind == "worldwide":
                continue
            options = [
                superset
                for superset in supersets_containing(
                    catalog.graph, catalog.by_slug, region, next(iter(country_set))
                )
                if country_set & superset.countries
            ]
            if options:
                widened[index] = options[0]
                break
    return widened


def plan_table(
    table_name: str,
    data: TableData,
    table_config: TableConfig,
    databases: list[DatabasePlan],
    config: BuildConfig,
    sampler: OverlapSampler,
    rng: random.Random,
    catalog: RegionCatalog,
    widening_log: list[str],
) -> TablePlan:
    """4b-4f + 4h correction for one table.

    The band is verified against the REAL greedy order (R6). Coverage above the
    band retries with strict head placement and a smaller head target (fewer
    head rows shrink the greedy front union); coverage below the band retries
    with head roles recomputed on the real greedy front and a larger head
    target (bounded retries, then fail loudly per R17).

    When the head union is structurally capped below the band — small databases
    whose only eligible rows are single-instance must seed from rows outside
    head_rows (R7), which can make the 80/20 coverage unreachable — the band
    lower bound widens to the best achievable coverage (R6: the margin is
    forced wider to the next fitting number of rows) and the widening is logged.
    """
    counts = draw_counts(data, databases, config, sampler, rng, catalog, widening_log)
    assigned = correlate_counts(data, counts)
    lower, upper = band_bounds(len(data.rows))
    head_dbs = set(range(head_size(len(databases))))
    strict_head = False
    head_target = round(0.80 * len(data.rows))
    best_plan: TablePlan | None = None
    best_coverage = -1
    for _attempt in range(MAX_BAND_RETRIES + 1):
        head_rows, nonhead_homes = choose_head_rows(
            data, databases, assigned, config, head_dbs, head_target
        )
        instances = place_instances(
            data, databases, assigned, head_rows, head_dbs, strict_head, nonhead_homes
        )
        repaired, _moves = repair_coverage(
            data, databases, assigned, head_rows, instances, head_dbs
        )
        order = greedy_order(data, databases, repaired)
        front = set(order[: head_size(len(databases))])
        coverage = len(set().union(*(set(repaired[db]) for db in front)))
        if lower <= coverage <= upper:
            return TablePlan(assigned_counts=assigned, head_rows=head_rows, instances=repaired)
        if coverage < lower and coverage > best_coverage:
            best_plan = TablePlan(assigned_counts=assigned, head_rows=head_rows, instances=repaired)
            best_coverage = coverage
        if coverage > upper:
            strict_head = True
            head_target = max(lower, head_target - (coverage - upper) - 1)
            head_dbs = front
        else:
            head_dbs = front
            head_target = min(upper, head_target + (lower - coverage) + 1)
    if best_plan is not None:
        best_plan.band_lower = best_coverage
        widening_log.append(
            f"table {data.name}: head union structurally capped at {best_coverage} rows "
            f"(exclusive seed rows for small databases, R7); band lower widened "
            f"from {lower} to {best_coverage}"
        )
        return best_plan
    msg = (
        f"table {data.name}: head coverage {coverage} outside band [{lower}, {upper}] "
        f"after {MAX_BAND_RETRIES + 1} attempts"
    )
    raise RuntimeError(msg)


def draw_counts(
    data: TableData,
    databases: list[DatabasePlan],
    config: BuildConfig,
    sampler: OverlapSampler,
    rng: random.Random,
    catalog: RegionCatalog,
    widening_log: list[str],
) -> list[int]:
    """4b: draw duplication counts conditioned on [1, k_max] and row capacities.

    Each row draws from the overlap distribution conditioned on its feasible
    range [1, min(k_max, capacity)] — the number of databases whose region
    intersects the row's country set. Conditioning renormalizes instead of
    truncating, so the tail mass stays on the distribution's shape (R4) and
    no mass piles onto the capacity boundary bucket. A draw is placeable when
    the counts can give every database a row: max flow >= target. When no
    draw reaches the target flow, the best draw is returned and the flow
    repair widens unserved databases (R8: smallest such region).
    """
    n = len(data.rows)
    lower, _ = band_bounds(n)
    min_tail_supply = config.target - head_size(config.target) + lower
    set_capacity = {cs: _set_capacity(cs, databases) for cs in _country_set_counts(data)}
    caps = [set_capacity[data.row_country_sets[r]] for r in range(n)]
    best: list[int] | None = None
    best_flow = -1
    for _ in range(MAX_DRAWS):
        counts = [sampler.sample_conditioned(rng, cap) for cap in caps]
        if sum(counts) < min_tail_supply:
            continue
        flow = max_flow(data, databases, counts)
        if flow >= config.target:
            if _tail_fillable(data, databases, counts):
                return counts
            if flow > best_flow:
                best, best_flow = counts, flow
            continue
        if flow > best_flow:
            best, best_flow = counts, flow
    if best is None:
        msg = (
            f"table {data.name}: no count multiset with sum >= {min_tail_supply} "
            f"after {MAX_DRAWS} draws"
        )
        raise RuntimeError(msg)
    _repair_flow(data, databases, best, config.target, catalog, widening_log)
    return best


def _tail_fillable(
    data: TableData,
    databases: list[DatabasePlan],
    counts: list[int],
) -> bool:
    """True when every tail database can claim a fillable row in the draw.

    A tail db is filled either by a spare instance of a count >= 2 row (the
    head reservation keeps one instance, shareable up to the spare capacity)
    or by a count-1 row becoming a non-head row, which consumes one unit of
    the non-head budget: the head union must keep band_lower distinct rows,
    so at most n - band_lower rows can live outside it (R6 + R7 together).
    """
    head_dbs = set(range(head_size(len(databases))))
    tail_dbs = set(range(len(databases))) - head_dbs
    n = len(data.rows)
    band_lower, _ = band_bounds(n)
    db_eligible = _db_eligible_rows(data, databases, n)
    spare: dict[int, int] = {}
    nonhead_budget = n - band_lower
    for t in sorted(tail_dbs, key=lambda db: (len(db_eligible[db]), db)):
        fillable = next(
            (r for r in sorted(db_eligible[t]) if spare.get(r, counts[r] - 1) > 0),
            None,
        )
        if fillable is None:
            exclusive = next(
                (r for r in sorted(db_eligible[t]) if counts[r] == 1 and nonhead_budget > 0),
                None,
            )
            if exclusive is None:
                return False
            nonhead_budget -= 1
            spare[exclusive] = -1
            continue
        spare[fillable] = spare.get(fillable, counts[fillable] - 1) - 1
    return True


def _repair_flow(
    data: TableData,
    databases: list[DatabasePlan],
    counts: list[int],
    target: int,
    catalog: RegionCatalog,
    widening_log: list[str],
) -> None:
    """Widen unserved databases until max_flow reaches the target (R7 fallback).

    After a max flow, a database is unserved when no instance reaches it. Each
    round widens one unserved db into the smallest region that both contains
    it and intersects a row country set with unused supply (R8); worldwide is
    the last resort. Bounded rounds keep this deterministic.
    """
    from wikidata_federated_overlap_dataset.regions import supersets_containing

    for _ in range(MAX_WIDENING_ROUNDS):
        flow, _served, set_flow, _assignment = _max_flow_detail(data, databases, counts)
        if flow >= target:
            return
        set_supply = _set_supply(data, counts)
        unused = {cs for cs, used in set_flow.items() if used < set_supply[cs]}
        if not unused:
            return
        widened = False
        for db_index in range(len(databases)):
            region = databases[db_index].region
            if region.kind == "worldwide":
                continue
            options = [
                superset
                for country_id in sorted(set().union(*unused))
                for superset in supersets_containing(
                    catalog.graph, catalog.by_slug, region, country_id
                )
                if any(superset.countries & cs for cs in unused)
            ]
            if options:
                smallest = options[0]
                widening_log.append(
                    f"{data.name}: db{db_index} {region.slug} -> {smallest.slug} (flow repair)"
                )
                databases[db_index] = DatabasePlan(region=smallest)
                widened = True
                break
        if not widened:
            return


def _set_supply(data: TableData, counts: list[int]) -> dict[frozenset[int], int]:
    """Total instance supply per distinct row country set."""
    supply: dict[frozenset[int], int] = {}
    for r, count in enumerate(counts):
        cs = data.row_country_sets[r]
        supply[cs] = supply.get(cs, 0) + count
    return supply


def row_eligible_dbs(data: TableData, databases: list[DatabasePlan], row_index: int) -> list[int]:
    """D(r): indexes of databases whose region intersects the row's country set."""
    country_set = data.row_country_sets[row_index]
    if not country_set:
        return [i for i, db in enumerate(databases) if db.region.kind == "worldwide"]
    return [i for i, db in enumerate(databases) if db.region.countries & country_set]


def max_flow(data: TableData, databases: list[DatabasePlan], counts: list[int]) -> int:
    """Max instances placeable: Edmonds-Karp on source -> country sets -> dbs -> sink.

    Rows are aggregated by distinct country set with capacity sum(counts) per set;
    each database demands exactly 1 distinct-row instance. Returns the max flow,
    i.e. how many databases can receive a row of this table.
    """
    flow, _served, _set_flow, _assignment = _max_flow_detail(data, databases, counts)
    return flow


def _edmonds_karp(
    capacity: dict[int, dict[int, int]], source: int, sink: int, stop_at: int | None = None
) -> int:
    """Augment BFS shortest paths on `capacity` (mutated in place) until no
    sink path remains or `stop_at` flow is reached; returns the total flow.

    The returned flow never exceeds `stop_at`: the last augmentation is
    capped at the remaining amount. The residual capacities left in
    `capacity` encode the final flow and are used by callers to reconstruct
    per-set flow and db assignments.
    """
    flow = 0
    while stop_at is None or flow < stop_at:
        parent: dict[int, int] = {source: source}
        queue = deque([source])
        while queue and sink not in parent:
            u = queue.popleft()
            for v, c in capacity[u].items():
                if c > 0 and v not in parent:
                    parent[v] = u
                    queue.append(v)
        if sink not in parent:
            break
        bottleneck = capacity[parent[sink]][sink]
        v = parent[sink]
        while v != source:
            bottleneck = min(bottleneck, capacity[parent[v]][v])
            v = parent[v]
        if stop_at is not None:
            bottleneck = min(bottleneck, stop_at - flow)
        v = sink
        while v != source:
            u = parent[v]
            capacity[u][v] -= bottleneck
            capacity[v][u] += bottleneck
            v = u
        flow += bottleneck
    return flow


def _max_flow_detail(
    data: TableData, databases: list[DatabasePlan], counts: list[int]
) -> tuple[int, set[int], dict[frozenset[int], int], dict[int, frozenset[int]]]:
    """Max flow plus the served databases, flow per set, and a db -> set assignment.

    Rows are aggregated by distinct country set with capacity sum(counts) per
    set; each database demands exactly 1 distinct-row instance. Returns
    (flow, served db indexes, flow per set, one set per served db).
    """
    set_supply = _set_supply(data, counts)
    set_list = list(set_supply)
    set_dbs = [
        [
            i
            for i, db in enumerate(databases)
            if (not cs and db.region.kind == "worldwide") or (cs and db.region.countries & cs)
        ]
        for cs in set_list
    ]
    source, sink = -1, -2
    set_nodes = {index: index for index in range(len(set_list))}
    db_nodes = {db: len(set_list) + db for db in range(len(databases))}
    capacity: dict[int, dict[int, int]] = {}

    def add_edge(u: int, v: int, c: int) -> None:
        capacity.setdefault(u, {})
        capacity[u][v] = capacity[u].get(v, 0) + c
        capacity.setdefault(v, {}).setdefault(u, 0)

    for index, cs in enumerate(set_list):
        add_edge(source, set_nodes[index], set_supply[cs])
        for db in set_dbs[index]:
            add_edge(set_nodes[index], db_nodes[db], len(counts))
    for db in range(len(databases)):
        add_edge(db_nodes[db], sink, 1)
    flow = _edmonds_karp(capacity, source, sink)

    # Reconstruct per-set flow and the per-db assignment from net edge
    # flows: a set's used flow is its supply minus the residual on its
    # source edge, and a db node is served iff it receives net flow from
    # some set node. This is robust to augmenting paths that reroute
    # earlier assignments through residual edges (a path can contain
    # several db/set nodes, so path positions are not reliable).
    set_flow = {
        cs: set_supply[cs] - capacity[source].get(set_nodes[index], 0)
        for index, cs in enumerate(set_list)
    }
    served: set[int] = set()
    assignment: dict[int, frozenset[int]] = {}
    for db, node in db_nodes.items():
        for set_index, set_node in set_nodes.items():
            # Edge capacity is len(counts); residual < capacity iff net flow > 0.
            if capacity[set_node].get(node, len(counts)) < len(counts):
                served.add(db)
                assignment[db] = set_list[set_index]
                break
    return flow, served, set_flow, assignment


def correlate_counts(data: TableData, counts: list[int]) -> list[int]:
    """4c: assign the drawn counts within each country set by publicity value.

    The drawn count multiset per country set is fixed (the overlap
    distribution and the per-set instance total are unchanged), but which row
    receives which count is decided here: higher counts go to the rows with
    the higher value column (R4b), ties broken by the seeded row order.
    Tables without a value column keep the drawn assignment (all values
    equal, stable sort preserves it).
    """
    by_set: dict[frozenset[int], list[int]] = {}
    for row, country_set in enumerate(data.row_country_sets):
        by_set.setdefault(country_set, []).append(row)
    assigned = list(counts)
    for rows in by_set.values():
        if len(rows) < 2:
            continue
        sorted_rows = sorted(rows, key=lambda r: (-data.row_values[r], r))
        sorted_counts = sorted((counts[r] for r in rows), reverse=True)
        for row, count in zip(sorted_rows, sorted_counts, strict=True):
            assigned[row] = count
    return assigned


def _set_capacity(country_set: frozenset[int], databases: list[DatabasePlan]) -> int:
    """Number of databases whose region intersects the country set (empty set -> worldwide)."""
    if not country_set:
        return sum(1 for db in databases if db.region.kind == "worldwide")
    return sum(1 for db in databases if db.region.countries & country_set)


def choose_head_rows(
    data: TableData,
    databases: list[DatabasePlan],
    assigned: list[int],
    config: BuildConfig,
    head_dbs: set[int],
    head_target: int,
) -> tuple[set[int], dict[int, int]]:
    """4d: rows allowed to contribute to head coverage (forced rows + spread).

    Seed-row protection: a database whose eligible-row set is small (<= 2 rows)
    needs at least one of those rows placed outside the head, so those rows are
    excluded from head_rows unless forced — otherwise the database could end up
    empty and unrepairable (R7). Returns the head rows and the non-head rows'
    home db per row (the spread assignment, R6).
    """
    tail_dbs = set(range(len(databases))) - head_dbs
    n = len(data.rows)
    eligible = [row_eligible_dbs(data, databases, r) for r in range(n)]
    db_eligible_rows = _db_eligible_rows(data, databases, n)
    seed_rows = _protected_seed_rows(db_eligible_rows, n, round(0.80 * n), assigned)
    forced = set()
    candidates = []
    for r in range(n):
        head_overlap = [db for db in eligible[r] if db in head_dbs]
        tail_overlap = [db for db in eligible[r] if db in tail_dbs]
        if not head_overlap:
            continue
        if assigned[r] > len(tail_overlap):
            forced.add(r)
        elif r not in seed_rows:
            candidates.append(r)
    rng = random.Random(derived_seed(0, "head-rows", n, len(head_dbs)))
    remaining = head_target - len(forced)
    nonhead_homes: dict[int, int] = {}
    if remaining > 0 and len(candidates) > remaining:
        nonhead, nonhead_homes = _spread_nonhead_rows(
            data, databases, sorted(candidates), len(candidates) - remaining, rng, tail_dbs
        )
        head_rows = forced | (set(candidates) - nonhead)
    else:
        head_rows = forced | set(candidates)
    head_rows = _ensure_tail_fillable(data, databases, assigned, head_rows, head_dbs)
    return head_rows, nonhead_homes


def _spread_nonhead_rows(
    data: TableData,
    databases: list[DatabasePlan],
    candidates: list[int],
    nonhead_count: int,
    rng: random.Random,
    tail_dbs: set[int],
) -> tuple[set[int], dict[int, int]]:
    """Pick non-head rows spread evenly over the tail databases (R6).

    The greedy front union extends beyond the head databases by the non-head
    rows of its tail members, so no tail db may hold many non-head rows.
    Databases with a small eligible-row set are served first with a dedicated
    row each, so identical-eligible databases (two dbs of the same country)
    do not compete for one row and starve (R7). Returns the non-head rows and
    their home db per row; the placement uses the homes so the spread survives
    placement.
    """
    rng.shuffle(candidates)
    eligible_cache = {
        r: [db for db in row_eligible_dbs(data, databases, r) if db in tail_dbs] for r in candidates
    }
    db_eligible = _db_eligible_rows(data, databases, len(data.rows))
    counts: dict[int, int] = {db: 0 for db in tail_dbs}
    assignment: dict[int, int] = {}
    taken: set[int] = set()
    small_dbs = sorted((db for db in tail_dbs if 0 < len(db_eligible[db]) <= 2), key=lambda db: db)
    for db in small_dbs:
        row = next(
            (
                r
                for r in sorted(db_eligible[db])
                if r in eligible_cache and eligible_cache[r] and r not in taken
            ),
            None,
        )
        if row is None:
            continue
        assignment[row] = db
        taken.add(row)
        counts[db] += 1
    for row in candidates:
        if len(assignment) >= nonhead_count:
            break
        if row in taken:
            continue
        eligible = eligible_cache[row]
        if not eligible:
            continue
        target = min(eligible, key=lambda db: (counts[db], db))
        assignment[row] = target
        counts[target] += 1
    limit = math.ceil(nonhead_count / max(len(tail_dbs), 1)) + 1
    for _ in range(nonhead_count):
        overloaded = [db for db, count in counts.items() if count > limit]
        if not overloaded:
            break
        source = max(overloaded, key=lambda db: (counts[db], db))
        movable = next(
            (
                row
                for row, db in assignment.items()
                if db == source and any(counts[alt] < counts[source] for alt in eligible_cache[row])
            ),
            None,
        )
        if movable is None:
            break
        counts[source] -= 1
        alternatives = [alt for alt in eligible_cache[movable] if counts[alt] < counts[source]]
        if not alternatives:
            counts[source] += 1
            break
        target = min(alternatives, key=lambda db: (counts[db], db))
        assignment[movable] = target
        counts[target] += 1
    return set(assignment), assignment


def _ensure_tail_fillable(
    data: TableData,
    databases: list[DatabasePlan],
    assigned: list[int],
    head_rows: set[int],
    head_dbs: set[int],
) -> set[int]:
    """Swap head rows until every tail database can claim a fillable row (R7).

    A tail db claims one eligible row with a spare instance (count >= 2) or a
    single-instance non-head row not yet claimed by another tail db — smallest
    eligible sets claim first, so identical-eligible databases (two dbs of the
    same country) do not rely on the same single-instance row. Dbs that cannot
    claim get one of their eligible head rows swapped out of head_rows, making
    it a non-head row they can seed; bounded iterations keep this deterministic.
    """
    tail_dbs = set(range(len(databases))) - head_dbs
    n = len(data.rows)
    db_eligible = _db_eligible_rows(data, databases, n)
    eligible = [row_eligible_dbs(data, databases, r) for r in range(n)]
    head_eligible = set().union(*(db_eligible[t] for t in head_dbs)) if head_dbs else set()

    def unclaimed_dbs(rows: set[int]) -> set[int]:
        """Tail dbs that cannot claim a fillable row under the current head rows.

        Rows with spare instances (count >= 2) are shareable: each claim
        consumes one spare, so a row can serve several tail dbs up to its
        spare capacity (count - 1 for a head row, count for a non-head row).
        Single-instance non-head rows are exclusive claims (R5: one instance,
        one database).
        """
        spare: dict[int, int] = {}
        unclaimed: set[int] = set()

        def spare_of(row: int) -> int:
            """Spare instances of the row beyond its head reservation."""
            return spare.get(row, assigned[row] - (1 if row in rows else 0))

        for t in sorted(tail_dbs, key=lambda db: (len(db_eligible[db]), db)):
            fillable = next((r for r in sorted(db_eligible[t]) if spare_of(r) > 0), None)
            if fillable is None:
                unclaimed.add(t)
            else:
                spare[fillable] = spare_of(fillable) - 1
        return unclaimed

    stuck = unclaimed_dbs(head_rows)
    for _ in range(len(head_rows) + 1):
        if not stuck:
            return head_rows
        target = min(stuck, key=lambda t: (len(db_eligible[t]), t))
        out_row = min(
            (
                r
                for r in db_eligible[target] & head_rows
                if assigned[r] <= sum(1 for db in eligible[r] if db in tail_dbs)
            ),
            default=None,
        )
        # The swap-in head row must not steal another tail db's only non-head
        # fillable row — prefer rows no tail db can hold, then rows with spare
        # instances (their head reservation survives a tail seed).
        swap_in = next(
            (
                r
                for r in sorted(set(range(n)) - head_rows)
                if r in head_eligible and not any(r in db_eligible[t] for t in tail_dbs)
            ),
            None,
        )
        if swap_in is None:
            swap_in = next(
                (
                    r
                    for r in sorted(set(range(n)) - head_rows)
                    if r in head_eligible and assigned[r] >= 2
                ),
                None,
            )
        if out_row is None:
            return head_rows
        # No replaceable head row shrinks head_rows instead: an empty tail
        # database is fatal (R7); the smaller head union is correctable by
        # the band retry (R6).
        head_rows = (
            head_rows - {out_row} if swap_in is None else (head_rows - {out_row}) | {swap_in}
        )
        stuck = unclaimed_dbs(head_rows)
    return head_rows


def _protected_seed_rows(
    db_eligible_rows: dict[int, set[int]],
    n: int,
    head_target: int,
    assigned: list[int],
) -> set[int]:
    """Rows that must stay out of head_rows so every database can get a row (R7).

    A database whose eligible-row set could end up entirely inside head_rows
    (|eligible| <= n - head_target) needs one protected non-head row; otherwise
    the database could end up empty and unrepairable. Only single-instance rows
    need protection: a row with count >= 2 can seed the database from a spare
    instance while its head reservation stays intact.
    """
    protected = set()
    for rows_for_db in db_eligible_rows.values():
        if 0 < len(rows_for_db) <= n - head_target:
            seed = min(rows_for_db)
            if assigned[seed] == 1:
                protected.add(seed)
    return protected


def _db_eligible_rows(
    data: TableData, databases: list[DatabasePlan], n: int
) -> dict[int, set[int]]:
    """Per database: the set of rows eligible for it."""
    result: dict[int, set[int]] = {db: set() for db in range(len(databases))}
    for r in range(n):
        for db in row_eligible_dbs(data, databases, r):
            result[db].add(r)
    return result


def _flow_assignment(
    data: TableData,
    databases: list[DatabasePlan],
    assigned: list[int],
) -> dict[int, frozenset[int]]:
    """One row country set per database the max flow can serve (R7 seed)."""
    _flow, _served, _set_flow, assignment = _max_flow_detail(data, databases, assigned)
    return assignment


@dataclass
class _PlacementContext:
    """Static per-table inputs shared by the seeding and greedy passes."""

    data: TableData
    databases: list[DatabasePlan]
    assigned: list[int]
    head_rows: set[int]
    head_dbs: set[int]
    tail_dbs: set[int]
    strict_head: bool
    nonhead_homes: dict[int, int]
    nonhead_cap: int
    db_eligible_rows: dict[int, set[int]]
    band_lower: int


@dataclass
class _PlacementState:
    """Mutable bookkeeping advanced by every placed instance.

    instances: rows per database (R5: a db never holds a row twice).
    held / nonhead_held: distinct rows per db, split by head membership.
    loads: instance count per db. remaining_counts: instances still to place
    per row. head_union: distinct rows across head dbs (R6 band tracking).
    seeded_head_rows: head rows that already seeded a tail db.
    """

    instances: dict[int, list[int]]
    held: dict[int, set[int]]
    nonhead_held: dict[int, set[int]]
    loads: list[int]
    remaining_counts: list[int]
    head_union: set[int]
    seeded_head_rows: set[int]


def _record_instance(
    state: _PlacementState, db: int, row: int, head_rows: set[int], head_dbs: set[int]
) -> None:
    """Book one row instance into a database and advance every counter."""
    state.instances[db].append(row)
    state.held[db].add(row)
    if row not in head_rows:
        state.nonhead_held[db].add(row)
    elif db not in head_dbs:
        state.seeded_head_rows.add(row)
    state.remaining_counts[row] -= 1
    state.loads[db] += 1
    if db in head_dbs:
        state.head_union.add(row)


def _seed_dbs_from_flow(context: _PlacementContext, state: _PlacementState) -> None:
    """Seed one row into every database the max flow can serve (R7).

    Each flow-served database receives one row of its assigned country set.
    Role preferences are soft: head dbs prefer head rows, tail dbs seed
    non-head rows first — union drift is corrected by the band retry (R6).
    Head databases keep the strict_head rule so non-head rows do not leak
    into the head union.
    """
    assignment = _flow_assignment(context.data, context.databases, context.assigned)
    for db, country_set in assignment.items():
        wants_head = db in context.head_dbs
        candidates = _seed_candidates(context, state, country_set, db, wants_head)
        if not candidates:
            continue
        row = _pick_seed_row(context, state, candidates, db, wants_head)
        if row is None:
            continue
        _record_instance(state, db, row, context.head_rows, context.head_dbs)


def _seed_candidates(
    context: _PlacementContext,
    state: _PlacementState,
    country_set: frozenset[int],
    db: int,
    wants_head: bool,
) -> list[int]:
    """Rows of the flow-assigned country set the database may seed.

    Primary candidates respect the role rules (strict head dbs take head
    rows only; tail dbs skip single-count or already-seeded head rows). The
    fallback relaxes to any spare row of the set — the max flow guarantees
    the set's supply covers exactly the dbs routed to it, so this never
    steals supply from other dbs (R7).
    """
    data, assigned = context.data, context.assigned
    candidates = [
        r
        for r in range(len(assigned))
        if data.row_country_sets[r] == country_set
        and state.remaining_counts[r] > 0
        and not (context.strict_head and wants_head and r not in context.head_rows)
        and not (
            not wants_head
            and r in context.head_rows
            and (assigned[r] == 1 or r in state.seeded_head_rows)
        )
    ]
    if candidates:
        return candidates
    return [
        r
        for r in range(len(assigned))
        if data.row_country_sets[r] == country_set
        and state.remaining_counts[r] > 0
        and r not in state.held[db]
        and not (context.strict_head and wants_head and r not in context.head_rows)
    ]


def _pick_seed_row(
    context: _PlacementContext,
    state: _PlacementState,
    candidates: list[int],
    db: int,
    wants_head: bool,
) -> int | None:
    """Choose the row a database seeds, or None when no candidate qualifies.

    Head dbs take the best head row. Tail dbs seed non-head rows first, then
    head rows that can still spare an instance for their head reservation —
    a head row without head spare is never seeded into a tail db (hard
    filter): it would strand the row from the head union and shrink coverage
    below the 80/20 band (R6 + R7). The spread home is a soft preference so
    the non-head spread survives placement (R6).
    """
    if wants_head:
        return max(
            candidates,
            key=lambda r: (r in context.head_rows, state.remaining_counts[r], r),
        )
    head_spare = {
        r: state.remaining_counts[r]
        - (1 if r in context.head_rows and r not in state.head_union else 0)
        for r in candidates
    }
    candidates = [r for r in candidates if r not in context.head_rows or head_spare[r] >= 1]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda r: (
            r in context.head_rows,
            r in context.head_rows and head_spare[r] < 1,
            context.nonhead_homes.get(r) != db,
            state.remaining_counts[r],
            r,
        ),
    )


def _pending_rows(context: _PlacementContext, state: _PlacementState) -> list[int]:
    """Rows with unplaced instances: multi-count rows first, then single.

    Multi-count rows are grouped by country set and ordered by descending
    publicity value (R4b): the greedy placement hands the scarce high-count
    slots to the better-known rows first; the seeded tiebreaker keeps the
    order deterministic for equal values. Both groups are ordered
    most-constrained first (fewest eligible databases).
    """
    data, databases, assigned = context.data, context.databases, context.assigned
    multi = sorted(
        (r for r in range(len(data.rows)) if state.remaining_counts[r] >= 1 and assigned[r] >= 2),
        key=lambda r: (len(row_eligible_dbs(data, databases, r)), -assigned[r], r),
    )
    single = sorted(
        (r for r in range(len(data.rows)) if state.remaining_counts[r] >= 1 and assigned[r] == 1),
        key=lambda r: (len(row_eligible_dbs(data, databases, r)), r),
    )
    multi.sort(key=lambda r: (data.row_country_sets[r], -data.row_values[r], r))
    return multi + single


def _place_row_instances(context: _PlacementContext, state: _PlacementState, r: int) -> None:
    """Place every remaining instance of one row in distinct databases (R5).

    Multi-count head rows reserve their first instance for a head database;
    single-count head rows do the same only while the head union is still
    below the 80/20 band's lower bound — afterwards they prefer empty tail
    databases (R6 + R7 together).
    """
    eligible = row_eligible_dbs(context.data, context.databases, r)
    chosen = _reserve_head_instance(context, state, r, eligible)
    for _ in range(state.remaining_counts[r] - len(chosen)):
        pool = _instance_pool(context, state, r, eligible, chosen)
        if not pool:
            msg = f"table {context.data.name}: row {r} cannot realize count {context.assigned[r]}"
            raise RuntimeError(msg)
        chosen.append(_pick_instance_db(context, state, r, pool))
    for db in chosen:
        _record_instance(state, db, r, context.head_rows, context.head_dbs)


def _reserve_head_instance(
    context: _PlacementContext, state: _PlacementState, r: int, eligible: list[int]
) -> list[int]:
    """Reserve the row's first instance for a head database when required.

    Applies to head rows not yet held by any head db; single-count head rows
    skip the reservation once the head union reached the band lower bound and
    empty tail dbs exist (they prefer seeding empty tail dbs, R6 + R7).
    """
    if r not in context.head_rows or any(r in state.held[db] for db in context.head_dbs):
        return []
    head_options = [db for db in eligible if db in context.head_dbs]
    head_satisfied = len(state.head_union) >= context.band_lower
    empty_tail = [db for db in eligible if db in context.tail_dbs and not state.instances[db]]
    if head_options and not (context.assigned[r] == 1 and head_satisfied and empty_tail):
        return [min(head_options, key=lambda db: (state.loads[db], db))]
    return []


def _instance_pool(
    context: _PlacementContext,
    state: _PlacementState,
    r: int,
    eligible: list[int],
    chosen: list[int],
) -> list[int]:
    """Databases that may take the next instance of the row.

    A db that already holds the row is never eligible (R5: no duplicate rows
    within a database). Non-head rows balance over all eligible tail dbs —
    the empty-first rule would cluster them in the last empty dbs and push
    the greedy front union over the 80/20 band ceiling (R6). Head rows keep
    the empty-first rule (R7) and avoid head dbs, which would grow the head
    union. The non-head cap falls back to the least-loaded eligible db so
    the row still places (R5) with minimal union growth.
    """
    tail_options = [db for db in eligible if db in context.tail_dbs and db not in chosen]
    overflow_options = [
        db
        for db in eligible
        if db in context.head_dbs
        and db not in chosen
        and not (context.strict_head and r not in context.head_rows)
    ]
    if r in context.head_rows:
        empty = [
            db
            for db in eligible
            if not state.instances[db] and db not in chosen and r not in state.held[db]
        ]
        return (
            empty
            or [db for db in tail_options if db not in chosen and r not in state.held[db]]
            or [db for db in overflow_options if db not in chosen and r not in state.held[db]]
        )
    pool = [
        db
        for db in tail_options
        if db not in chosen
        and r not in state.held[db]
        and len(state.nonhead_held[db]) < context.nonhead_cap
    ]
    if not pool:
        pool = [db for db in tail_options if db not in chosen and r not in state.held[db]]
    return pool or [db for db in overflow_options if db not in chosen and r not in state.held[db]]


def _pick_instance_db(
    context: _PlacementContext, state: _PlacementState, r: int, pool: list[int]
) -> int:
    """Pick the database for the next instance of the row from the pool.

    Non-head rows keep their spread home when possible, else balance by
    their own non-head count per db (R6). Head rows with a single instance
    prefer emptier dbs (R7); multi-count head rows balance by distinct rows
    first (R9: redundant-heavy tail dbs keep the greedy front union inside
    the 80/20 band, R6), then load.
    """
    if r not in context.head_rows:
        home = context.nonhead_homes.get(r)
        if home in pool:
            return home
        return min(
            pool,
            key=lambda db: (len(state.nonhead_held[db]), len(state.held[db]), state.loads[db], db),
        )
    if context.assigned[r] == 1:
        return min(
            pool,
            key=lambda db: (
                len(state.instances[db]),
                len(context.db_eligible_rows[db]),
                state.loads[db],
                db,
            ),
        )
    return min(pool, key=lambda db: (len(state.held[db]), state.loads[db], db))


def place_instances(
    data: TableData,
    databases: list[DatabasePlan],
    assigned: list[int],
    head_rows: set[int],
    head_dbs: set[int],
    strict_head: bool = False,
    nonhead_homes: dict[int, int] | None = None,
) -> dict[int, list[int]]:
    """4e: place every row in exactly assigned[r] distinct databases, load-balanced.

    A max-flow assignment seeds one row into every database it can serve, so
    no database starves whenever the count multiset is placeable (R7). The
    remaining instances are placed greedily, rows most-constrained first
    (fewest eligible databases). Multi-count head rows reserve their first
    instance for a head database. Single-count head rows do the same only
    while the head union is still below the 80/20 band's lower bound —
    afterwards they prefer empty tail databases (R6 + R7 together). With
    strict_head, non-head rows never enter head databases unless no tail
    database can hold them, keeping the head union inside the band ceiling.
    """
    nonhead_total = sum(1 for r in range(len(data.rows)) if r not in head_rows)
    tail_dbs = set(range(len(databases))) - head_dbs
    context = _PlacementContext(
        data=data,
        databases=databases,
        assigned=assigned,
        head_rows=head_rows,
        head_dbs=head_dbs,
        tail_dbs=tail_dbs,
        strict_head=strict_head,
        nonhead_homes=nonhead_homes or {},
        nonhead_cap=math.ceil(nonhead_total / max(len(tail_dbs), 1)) + 1,
        db_eligible_rows=_db_eligible_rows(data, databases, len(data.rows)),
        band_lower=band_bounds(len(data.rows))[0],
    )
    state = _PlacementState(
        instances={i: [] for i in range(len(databases))},
        held={i: set() for i in range(len(databases))},
        nonhead_held={i: set() for i in range(len(databases))},
        loads=[0] * len(databases),
        remaining_counts=list(assigned),
        head_union=set(),
        seeded_head_rows=set(),
    )
    _seed_dbs_from_flow(context, state)
    for row in _pending_rows(context, state):
        _place_row_instances(context, state, row)
    return state.instances


def repair_coverage(
    data: TableData,
    databases: list[DatabasePlan],
    assigned: list[int],
    head_rows: set[int],
    instances: dict[int, list[int]],
    head_dbs: set[int],
) -> tuple[dict[int, list[int]], int]:
    """4f: re-route instances so every database holds >= 1 row of this table.

    A head row may leave a head database when another head database still
    holds it — the head union (and thus the 80/20 band) is unchanged. When
    the empty database is a head database, head rows are preferred so the
    union stays inside the 80/20 band ceiling (R6).
    """
    moves = 0
    empty = [i for i in range(len(databases)) if not instances[i]]
    band_lower, band_upper = band_bounds(len(data.rows))
    for db in empty:
        source: int | None = None
        row: int | None = None
        union = set().union(*(instances[i] for i in head_dbs)) if head_dbs else set()
        for src in sorted(range(len(databases)), key=lambda s: -len(instances[s])):
            if len(instances[src]) < 2:
                continue
            candidates = sorted(instances[src])
            if db in head_dbs:
                candidates.sort(key=lambda r: (r not in head_rows, r))
            for candidate in candidates:
                if db not in row_eligible_dbs(data, databases, candidate):
                    continue
                if (
                    db in head_dbs
                    and candidate not in head_rows
                    and candidate not in union
                    and len(union) >= band_upper
                ):
                    continue
                head_blocked = _is_head_guarded(candidate, src, head_dbs, head_rows)
                if head_blocked and not _head_union_survives(
                    candidate, src, instances, head_dbs, band_lower
                ):
                    continue
                source, row = src, candidate
                break
            if source is not None:
                break
        if source is None or row is None:
            msg = f"table {data.name}: cannot repair empty db {db}"
            raise RuntimeError(msg)
        instances[source].remove(row)
        instances[db].append(row)
        if db in head_dbs:
            union.add(row)
        moves += 1
    return instances, moves


def _head_union_survives(
    row: int,
    source_db: int,
    instances: dict[int, list[int]],
    head_dbs: set[int],
    band_lower: int,
) -> bool:
    """True if moving the row out of source_db keeps the head union >= band_lower.

    The head union is the set of distinct rows held by the head databases;
    removing one instance shrinks it only when no other head db holds the row.
    """
    if any(row in instances[db] for db in head_dbs if db != source_db):
        return True
    union = set().union(*(instances[db] for db in head_dbs))
    return len(union) - 1 >= band_lower


def _is_head_guarded(row: int, db: int, head_dbs: set[int], head_rows: set[int]) -> bool:
    """True if moving the row out of db would remove its only head-set instance."""
    return db in head_dbs and row in head_rows


def _front_coverage(
    data: TableData, databases: list[DatabasePlan], instances: dict[int, list[int]], head_count: int
) -> int:
    """Distinct rows held by the greedy front (the first head_count dbs)."""
    order = greedy_order(data, databases, instances)
    return len(set().union(*(set(instances[db]) for db in order[:head_count])))


def merge_balanced(plan: TopicPlan, tables_data: dict[str, TableData], config: BuildConfig) -> None:
    """4g: balance total loads across databases via productive re-routing moves.

    Move targets are restricted to tail databases: moving an instance into a head
    database could add a non-head row to the head union and break the 80/20 band.
    A move is also skipped when it would drop a table's greedy-front coverage
    below its (widened) band lower bound — the front union can shrink when the
    source db leaves the front with a row no other front db holds. Each move
    strictly decreases sum(load^2), so the loop terminates; the number of moves
    is bounded by the initial load imbalance.
    """
    target = len(plan.databases)
    head_count = head_size(target)
    head_dbs = set(range(head_count))
    total_load = [sum(len(plan.tables[t].instances[i]) for t in plan.tables) for i in range(target)]
    table_names = sorted(plan.tables)
    eligible_cache: dict[tuple[str, int], list[int]] = {}
    while True:
        max_load = max(total_load)
        median_load = sorted(total_load)[target // 2]
        if max_load <= config.skew_limit * median_load:
            return
        x = total_load.index(max_load)
        moved = False
        for t in table_names:
            table_plan = plan.tables[t]
            data = tables_data[t]
            lower, _ = band_bounds(len(data.rows))
            if table_plan.band_lower is not None:
                lower = min(lower, table_plan.band_lower)
            for r in table_plan.instances[x]:
                if _is_head_guarded(r, x, head_dbs, table_plan.head_rows):
                    continue
                cache_key = (t, r)
                if cache_key not in eligible_cache:
                    eligible_cache[cache_key] = row_eligible_dbs(data, plan.databases, r)
                pool = [
                    db
                    for db in eligible_cache[cache_key]
                    if db != x
                    and db not in head_dbs
                    and r not in table_plan.instances[db]
                    and total_load[db] <= total_load[x] - 2
                ]
                if not pool:
                    continue
                y = min(pool, key=lambda db: (total_load[db], db))
                table_plan.instances[x].remove(r)
                table_plan.instances[y].append(r)
                coverage = _front_coverage(data, plan.databases, table_plan.instances, head_count)
                if coverage < lower:
                    table_plan.instances[y].remove(r)
                    table_plan.instances[x].append(r)
                    continue
                total_load[x] -= 1
                total_load[y] += 1
                moved = True
                break
            if moved:
                break
        if not moved:
            return


def verify_band(plan: TopicPlan, tables_data: dict[str, TableData], config: BuildConfig) -> None:
    """4h: compute greedy order, total order, and check the 80/20 band per table.

    Tables whose head union was structurally capped during planning (R7 seed
    rows) verify against their widened band lower bound instead.
    """
    for name, table_plan in plan.tables.items():
        data = tables_data[name]
        order = greedy_order(data, plan.databases, table_plan.instances)
        covered: set[int] = set()
        for db in order[: head_size(config.target)]:
            covered.update(table_plan.instances[db])
        lower, upper = band_bounds(len(data.rows))
        if table_plan.band_lower is not None:
            lower = min(lower, table_plan.band_lower)
        if not lower <= len(covered) <= upper:
            msg = f"table {name}: head coverage {len(covered)} outside band [{lower}, {upper}]"
            raise RuntimeError(msg)


def greedy_order(
    data: TableData,
    databases: list[DatabasePlan],
    instances: dict[int, list[int]],
) -> list[int]:
    """R6 greedy order: repeatedly take the db adding the most new unique rows."""
    row_sets = [set(instances[i]) for i in range(len(databases))]
    remaining = list(range(len(databases)))
    covered: set[int] = set()
    order = []
    while remaining:
        best = max(remaining, key=lambda db: (len(row_sets[db] - covered), -db))
        order.append(best)
        remaining.remove(best)
        covered |= row_sets[best]
    return order
