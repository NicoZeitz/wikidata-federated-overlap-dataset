"""Cap-aware overlap sampling: truncated pareto / exponential / GPD, zipf, uniform.

Bucket convention: P(K = k) = F(k+1) - F(k) for k in [1, k_max] with the
continuous distributions truncated to the support [1, k_max + 1) — every
bucket has width one, the buckets are strictly decreasing for the heavy-tailed
shapes, and no bucket absorbs an infinite tail (the old convention piled the
remaining mass onto the last bucket, a visible spike in the overlap plots).
zipf is discrete: its pmf k^-s is evaluated directly per bucket and normalized
by the sampler (CDF-difference bucketing would zero its last bucket).
"""

from __future__ import annotations

import bisect
import math
import random
from dataclasses import dataclass

from scipy import stats
from scipy.stats import make_distribution, truncate

from wikidata_federated_overlap_dataset.config import BuildConfig


@dataclass(frozen=True)
class OverlapSampler:
    """Inverse-CDF sampler of duplication counts conditioned on the integer range [1, k_max]."""

    distribution: str
    params: dict[str, float]
    k_max: int
    _cum: tuple[float, ...]
    _total: float

    @staticmethod
    def from_config(config: BuildConfig) -> OverlapSampler:
        """Build the sampler from the build config (distribution, params, cap, target)."""
        k_max = max(1, math.floor(config.overlap_cap * config.target))
        buckets = [
            _bucket_probability(config.overlap_distribution, config.overlap_params, k, k_max)
            for k in range(1, k_max + 1)
        ]
        cum = []
        total = 0.0
        for p in buckets:
            total += p
            cum.append(total)
        return OverlapSampler(
            distribution=config.overlap_distribution,
            params=config.overlap_params,
            k_max=k_max,
            _cum=tuple(cum),
            _total=total,
        )

    def sample(self, rng: random.Random) -> int:
        """Draw one duplication count in [1, k_max] from the conditioned distribution."""
        u = rng.random() * self._total
        return bisect.bisect_left(self._cum, u) + 1

    def sample_conditioned(self, rng: random.Random, cap: int) -> int:
        """Draw one duplication count conditioned on the integer range [1, cap].

        Conditioning renormalizes the distribution over [1, cap] instead of
        truncating a draw above cap, so the tail mass is redistributed
        proportionally over the feasible range rather than piling onto the
        boundary bucket (R4). Caps at or above k_max sample unconditionally.
        """
        cap = max(1, min(cap, self.k_max))
        if cap >= self.k_max:
            return self.sample(rng)
        u = rng.random() * self._cum[cap - 1]
        return bisect.bisect_left(self._cum, u) + 1

    def expected_count(self) -> float:
        """Expected duplication count E[K] of the conditioned distribution."""
        previous_values = (0.0, *self._cum[:-1])
        return (
            sum(
                k * (cum - previous)
                for k, (cum, previous) in enumerate(
                    zip(self._cum, previous_values, strict=True), start=1
                )
            )
            / self._total
        )

    def bucket_probabilities(self) -> list[float]:
        """Per-bucket P(k) for k in [1, k_max] (for the distribution plot)."""
        previous = 0.0
        probabilities = []
        for cum in self._cum:
            probabilities.append((cum - previous) / self._total)
            previous = cum
        return probabilities


def _truncated_cdf(distribution: str, params: dict[str, float], x: float, k_max: int) -> float:
    """CDF of the scipy truncated distribution on [1, k_max + 1), evaluated at x.

    Built with the modern scipy API: make_distribution wraps the base family
    into a new-style RV, arithmetic shifts/scales it into raw count space, and
    truncate cuts the support at the raw bounds — so the truncation is exact
    at k_max + 1 for every family (the legacy truncexpon standardized-b form
    left ~0.5% of mass beyond k_max + 1, which the sampler had to normalize
    away).
    """
    upper = float(k_max + 1)
    if distribution == "pareto":
        alpha = params.get("alpha", params.get("shape", 1.0))
        pareto_rv = make_distribution(stats.pareto)
        return float(truncate(pareto_rv(b=alpha), lb=1.0, ub=upper).cdf(x))
    if distribution == "exponential":
        rate = params["rate"]
        expon_rv = make_distribution(stats.expon)
        truncated = truncate(expon_rv() * (1.0 / rate) + 1.0, lb=1.0, ub=upper)
        return float(truncated.cdf(x))
    if distribution == "gpd":
        xi = params["shape"]
        sigma = params["scale"]
        mu = params["threshold"]
        lower = _gpd_cdf(xi, sigma, mu, 1.0)
        upper_cdf = _gpd_cdf(xi, sigma, mu, upper)
        raw = _gpd_cdf(xi, sigma, mu, x)
        return (raw - lower) / (upper_cdf - lower)
    raise ValueError(f"unknown distribution: {distribution}")


def _gpd_cdf(xi: float, sigma: float, mu: float, x: float) -> float:
    """Untruncated GPD tail: P(X <= x) = 1 - (1 + xi (x - mu) / sigma)^(-1/xi)."""
    z = 1.0 + xi * (x - mu) / sigma
    if z <= 0:
        return 0.0
    return 1.0 - z ** (-1.0 / xi)


def _bucket_probability(distribution: str, params: dict[str, float], k: int, k_max: int) -> float:
    """P(K = k) for the distribution truncated to [1, k_max + 1), bucket [k, k+1)."""
    if distribution == "uniform":
        return 1.0 / k_max
    if distribution == "zipf":
        return float(k) ** (-params["shape"])
    lower = _truncated_cdf(distribution, params, float(k), k_max)
    upper = _truncated_cdf(distribution, params, float(k + 1), k_max)
    return max(upper - lower, 1e-12)


def default_distribution_curves(k_max: int) -> dict[str, list[float]]:
    """Bucket probabilities of all five distributions with their default parameters."""
    from wikidata_federated_overlap_dataset.config import default_overlap_params

    curves = {}
    for distribution in DISTRIBUTION_ORDER:
        params = default_overlap_params(distribution)
        if distribution == "uniform":
            curves[distribution] = [1.0 / k_max] * k_max
        else:
            raw = [_bucket_probability(distribution, params, k, k_max) for k in range(1, k_max + 1)]
            total = sum(raw)
            curves[distribution] = [p / total for p in raw]
    return curves


DISTRIBUTION_ORDER = ("uniform", "pareto", "exponential", "gpd", "zipf")
