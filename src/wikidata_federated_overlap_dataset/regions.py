"""Region catalog: countries, groups, neighbor sets, worldwide + containment graph."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

import networkx as nx

from wikidata_federated_overlap_dataset.ground_truth import Country

KIND_COUNTRY = "country"
KIND_GROUP = "group"
KIND_NEIGHBOR = "neighbor"
KIND_WORLDWIDE = "worldwide"

KIND_ORDER = {KIND_COUNTRY: 0, KIND_NEIGHBOR: 1, KIND_GROUP: 2, KIND_WORLDWIDE: 3}


@dataclass(frozen=True)
class Region:
    """One allowed database region with its country set and human-readable label."""

    kind: str
    slug: str
    label: str
    countries: frozenset[int]
    country_slugs: frozenset[str] = frozenset()

    @property
    def tightness(self) -> int:
        """Sort rank: countries first, then neighbors, groups, worldwide last."""
        return KIND_ORDER[self.kind]


@dataclass
class RegionCatalog:
    """All regions plus the containment graph and per-topic candidates."""

    regions: list[Region]
    by_slug: dict[str, Region]
    graph: nx.DiGraph
    candidates: dict[str, list[Region]]


def slugify(text: str) -> str:
    """Convert a human-readable name into a lowercase ASCII slug."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", ascii_text.lower()).strip("_")


def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle distance between two (lon, lat) points in kilometers."""
    from math import asin, cos, radians, sin, sqrt

    lon1_r, lat1_r, lon2_r, lat2_r = map(radians, (lon1, lat1, lon2, lat2))
    dlon, dlat = lon2_r - lon1_r, lat2_r - lat1_r
    a = sin(dlat / 2) ** 2 + cos(lat1_r) * cos(lat2_r) * sin(dlon / 2) ** 2
    return 2.0 * 6371.0 * asin(sqrt(a))


def build_regions(
    countries: list, groups: list, labels: dict[str, str]
) -> tuple[list[Region], dict[str, Region]]:
    """Build the full region catalog: countries, groups, neighbor sets, worldwide."""
    regions: list[Region] = []
    by_slug: dict[str, Region] = {}
    id_to_slug = {c.id: slugify(c.name) for c in countries}

    def add(kind: str, slug: str, label: str, country_ids: frozenset[int]) -> None:
        if slug in by_slug:
            return
        slugs = frozenset(id_to_slug[cid] for cid in country_ids)
        region = Region(
            kind=kind, slug=slug, label=label, countries=country_ids, country_slugs=slugs
        )
        regions.append(region)
        by_slug[slug] = region

    for country in countries:
        label = labels.get(slugify(country.name), country.name)
        add(KIND_COUNTRY, slugify(country.name), label, frozenset({country.id}))
    for group in groups:
        ids = frozenset(labels_to_ids(group.countries, labels, countries))
        add(KIND_GROUP, group.name, group.label, ids)
    neighbor_sets = compute_neighbor_sets(countries)
    for nset in neighbor_sets:
        names = sorted(countries_by_id(countries)[cid].name for cid in nset)
        label = " + ".join(labels.get(slugify(n), n) for n in names)
        slug = "_".join(slugify(n) for n in names)
        add(KIND_NEIGHBOR, slug, label, frozenset(nset))
    all_ids = frozenset(c.id for c in countries)
    add(KIND_WORLDWIDE, "worldwide", "Worldwide", all_ids)
    return regions, by_slug


def countries_by_id(countries: list[Country]) -> dict[int, Country]:
    """Index countries by id."""
    return {c.id: c for c in countries}


def labels_to_ids(
    slugs: tuple[str, ...] | list[str], labels: dict[str, str], countries: list
) -> list[int]:
    """Resolve country slugs (labels.yaml keys) to country ids via name slugs."""
    by_name_slug = {slugify(c.name): c.id for c in countries}
    return [by_name_slug[slug] for slug in slugs]


def compute_neighbor_sets(countries: list, n: int = 2) -> list[frozenset[int]]:
    """Per country the n nearest neighbors; unique frozensets of pairs and n+1 groups."""
    sets: set[frozenset[int]] = set()
    for a in countries:
        nearest = sorted(
            (b for b in countries if b.id != a.id),
            key=lambda b: (haversine_km(a.lon, a.lat, b.lon, b.lat), b.id),
        )[:n]
        for b in nearest:
            sets.add(frozenset({a.id, b.id}))
        sets.add(frozenset({a.id, *(b.id for b in nearest)}))
    return sorted(sets, key=lambda s: sorted(s))


def build_containment_graph(regions: list[Region]) -> nx.DiGraph:
    """Edge R -> S iff S is the smallest proper superset of R (worldwide is the root)."""
    graph = nx.DiGraph()
    for region in regions:
        graph.add_node(region.slug)
    ordered = sorted(regions, key=lambda r: (-len(r.countries), r.slug))
    for region in regions:
        best = None
        for other in ordered:
            if other.slug == region.slug:
                continue
            if region.countries < other.countries and (
                best is None or len(other.countries) < len(best.countries)
            ):
                best = other
        if best is not None:
            graph.add_edge(region.slug, best.slug)
    return graph


def topic_candidates(topic_tables: list, regions: list[Region], tables_data: dict) -> list[Region]:
    """Regions that intersect at least one row's country set of every topic table.

    Uses the distinct country sets per table (not per row) — there are at most
    ~200 distinct sets, so this is O(regions x tables x distinct_sets).
    """
    distinct_sets = {
        table: {cs for cs in tables_data[table].row_country_sets if cs} for table in topic_tables
    }
    usable = []
    for region in regions:
        ok = all(
            any(region.countries & cs for cs in distinct_sets[table]) for table in topic_tables
        )
        if ok:
            usable.append(region)
    return usable


def build_region_catalog(
    countries: list,
    groups: list,
    labels: dict[str, str],
    topics: list,
    tables_data: dict,
) -> RegionCatalog:
    """Build the complete region catalog with containment graph and per-topic candidates."""
    regions, by_slug = build_regions(countries, groups, labels)
    graph = build_containment_graph(regions)
    candidates = {
        topic.name: topic_candidates(topic.tables, regions, tables_data) for topic in topics
    }
    return RegionCatalog(regions=regions, by_slug=by_slug, graph=graph, candidates=candidates)


def supersets_containing(
    graph: nx.DiGraph, by_slug: dict[str, Region], region: Region, country_id: int
) -> list[Region]:
    """All catalog regions that are strict supersets of `region` and contain `country_id`."""
    result = []
    for slug in nx.descendants(graph, region.slug):
        candidate = by_slug[slug]
        if country_id in candidate.countries:
            result.append(candidate)
    return sorted(result, key=lambda r: (len(r.countries), r.slug))
