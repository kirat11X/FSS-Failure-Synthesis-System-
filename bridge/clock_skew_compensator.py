"""Conservative cross-source timestamp caveats for telemetry investigations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from fss_py.model import DecodedEvent, EventRecord


@dataclass
class ClockSkewReport:
    """Report source coverage without inferring causality from event-name patterns."""

    skew_detected: bool
    estimated_offset_bounds_ns: dict[tuple[int, int], tuple[int, int]] = field(default_factory=dict)
    ambiguous_pairs: list[dict[str, Any]] = field(default_factory=list)
    lamport_order_notes: list[str] = field(default_factory=list)
    clock_caveat: str = ""
    sources: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "skew_detected": self.skew_detected,
            "estimated_offset_bounds_ns": {
                f"{first}->{second}": list(bounds)
                for (first, second), bounds in self.estimated_offset_bounds_ns.items()
            },
            "ambiguous_pairs": list(self.ambiguous_pairs),
            "lamport_order_notes": list(self.lamport_order_notes),
            "clock_caveat": self.clock_caveat,
            "sources": list(self.sources),
        }


def _source_id(event: dict[str, Any] | DecodedEvent | EventRecord) -> int:
    if isinstance(event, dict):
        source = event.get("source_id", event.get("source", 0))
    else:
        source = event.source_id
    try:
        return int(source)
    except (TypeError, ValueError):
        return 0


def analyze_clock_skew(
    events: Sequence[dict[str, Any] | DecodedEvent | EventRecord],
) -> ClockSkewReport:
    """Flag cross-source timestamp ordering as unverified, without causal heuristics."""
    sources = sorted({_source_id(event) for event in events})
    caveat = ""
    if len(sources) > 1:
        caveat = (
            "Cross-source clock synchronization is unverified; timestamps alone do not "
            "establish event order or causality."
        )
    return ClockSkewReport(
        skew_detected=False,
        clock_caveat=caveat,
        sources=sources,
    )
