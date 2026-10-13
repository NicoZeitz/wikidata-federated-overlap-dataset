"""Logging: per-topic markdown logs + global build log in _meta/logs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from wikidata_federated_overlap_dataset.validation import ValidationReport


@dataclass
class TopicLog:
    """Collected log content for one topic."""

    topic_name: str
    phases: dict[str, float] = field(default_factory=dict)
    regions: list[str] = field(default_factory=list)
    table_stats: list[str] = field(default_factory=list)
    widening: list[str] = field(default_factory=list)
    report: ValidationReport = field(default_factory=ValidationReport)

    def render(self) -> str:
        """Render the topic log as markdown."""
        lines = [f"# Topic: {self.topic_name}", ""]
        lines.append("## Phase timings")
        for phase, seconds in self.phases.items():
            lines.append(f"- {phase}: {seconds:.2f}s")
        lines.append("")
        lines.append("## Regions")
        lines.extend(f"- {slug}" for slug in self.regions)
        lines.append("")
        lines.append("## Per-table statistics")
        lines.extend(self.table_stats)
        lines.append("")
        lines.append("## Region widening")
        if self.widening:
            lines.extend(f"- {entry}" for entry in self.widening)
        else:
            lines.append("- none")
        lines.append("")
        lines.append("## Validation")
        lines.extend(f"- ERROR: {e}" for e in self.report.errors)
        lines.extend(f"- WARNING: {w}" for w in self.report.warnings)
        if self.report.ok and not self.report.warnings:
            lines.append("- all checks passed")
        lines.append("")
        return "\n".join(lines)


def write_topic_log(logs_dir: Path, log: TopicLog) -> None:
    """Write one topic log file."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / f"{log.topic_name}.md").write_text(log.render())


def write_build_log(
    logs_dir: Path,
    phase_timings: dict[str, float],
    topic_summaries: list[str],
    warnings: list[str],
    total_seconds: float,
) -> None:
    """Write the global build log."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    lines = ["# Build log", ""]
    lines.append(f"- total: {total_seconds:.2f}s")
    lines.append("")
    lines.append("## Phase timings")
    lines.extend(f"- {phase}: {seconds:.2f}s" for phase, seconds in phase_timings.items())
    lines.append("")
    lines.append("## Topic summary")
    lines.extend(topic_summaries)
    lines.append("")
    lines.append("## Warnings")
    if warnings:
        lines.extend(f"- {w}" for w in warnings)
    else:
        lines.append("- none")
    lines.append("")
    (logs_dir / "build.md").write_text("\n".join(lines))
