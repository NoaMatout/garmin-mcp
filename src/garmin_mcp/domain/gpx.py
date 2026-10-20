"""Writing GPX 1.1.

Export only. Nothing in this project reads a GPX back, which is deliberate:
parsing a file a user hands over is a trust boundary of its own and belongs to
the import feature that has not been built (see docs/garmin-courses.md).

A track becomes one `<trk>` with one `<trkseg>` per uninterrupted segment. That
is the GPX way of saying "do not draw a line across this gap", and it is what
lets a consumer see an interruption instead of a phantom straight stretch.
"""

from __future__ import annotations

from collections.abc import Sequence
from xml.sax.saxutils import escape

from garmin_mcp.domain.geo import TrackPoint

MIME_TYPE = "application/gpx+xml"

_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<gpx version="1.1" creator="garmin-mcp" '
    'xmlns="http://www.topografix.com/GPX/1/1" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xsi:schemaLocation="http://www.topografix.com/GPX/1/1 '
    'http://www.topografix.com/GPX/1/1/gpx.xsd">\n'
)


def build_gpx(
    name: str,
    segments: Sequence[Sequence[TrackPoint]],
    *,
    description: str | None = None,
) -> str:
    """Serialise segments of valid fixes.

    Every point that is given is written, in order. `<ele>` and `<time>` appear
    only where the point carries them — an absent altitude is left out, never
    replaced by zero or by a neighbour's value.
    """
    lines = [_HEADER, f"  <metadata><name>{escape(name)}</name></metadata>\n"]
    lines.append("  <trk>\n")
    lines.append(f"    <name>{escape(name)}</name>\n")
    if description:
        lines.append(f"    <desc>{escape(description)}</desc>\n")

    for segment in segments:
        if not segment:
            continue
        lines.append("    <trkseg>\n")
        for p in segment:
            lines.append(f'      <trkpt lat="{p.lat:.7f}" lon="{p.lon:.7f}">')
            if p.altitude_m is not None:
                lines.append(f"<ele>{p.altitude_m:.1f}</ele>")
            if p.time_iso is not None:
                lines.append(f"<time>{p.time_iso}</time>")
            lines.append("</trkpt>\n")
        lines.append("    </trkseg>\n")

    lines.append("  </trk>\n</gpx>\n")
    return "".join(lines)
