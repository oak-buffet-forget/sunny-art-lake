#!/usr/bin/env python3
"""Finnish static room dashboard + bounded, allowlisted 48-hour history.

Python 3.10+ standard library only. No HA credentials or entity IDs. Dispatch
schema stays at 2. Manual workflow runs rebuild existing observations.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import html
import json
import math
import os
from pathlib import Path
from string import Template
import sys
from zoneinfo import ZoneInfo

UTC = timezone.utc
SCHEMA = 2
HISTORY_VERSION = 1
WINDOW_SECONDS = 48 * 3600
GAP_SECONDS = 75 * 60
MAX_BYTES = 4 * 1024 * 1024
MAX_SAMPLES = 6000
EVENT_TYPE = "github-update"
ZONES = (
    ("outdoor", "Ulkona", "leaf"),
    ("zone_a", "Vintti 1", "roof"),
    ("zone_b", "Makuuhuone 1", "bed"),
    ("zone_c", "Makuuhuone 2", "bed"),
    ("zone_e", "Olohuone", "sofa"),
    ("zone_f", "Keittiö", "kitchen"),
    ("zone_d", "Kattilahuone", "heat"),
    ("zone_g", "Kellarikomero", "shelf"),
)
METRICS = {
    f"{zone}_{suffix}": (low, high, precision)
    for zone, _, _ in ZONES
    for suffix, low, high, precision in (("c", -60, 80, 1), ("rh", 0, 100, 0))
}
ICONS = {
    "leaf": '<path d="M20 4C10 3 4 7 5 14c1 6 11 7 14-2 1-3 1-5 1-8Z"/><path d="m4 21 10-11"/>',
    "roof": '<path d="m3 11 9-8 9 8M5 10v10h14V10M9 20v-7h6v7"/>',
    "bed": '<path d="M3 18V9m18 9V9M3 15h18M5 9V5h14v4M3 15v-4a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v4M7 9V7m10 2V7"/>',
    "sofa": '<path d="M6 11V6a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v5M6 14h12M5 20v-2m14 2v-2M4 11h2v3h12v-3h2a2 2 0 0 1 2 2v5H2v-5a2 2 0 0 1 2-2ZM12 5v8"/>',
    "kitchen": '<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M5 10h14M8 6v1m0 6v3"/>',
    "heat": '<rect x="3" y="8" width="18" height="12" rx="2"/><path d="M7 11v6m5-6v6m5-6v6M7 4v1m5-2v2m5-1v1"/>',
    "shelf": '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M4 10h16M4 16h16M8 6h3m3 7h3M8 19h4"/>',
}


def loads_json(raw: str | bytes) -> object:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("JSON contains a duplicate field")
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValueError("JSON contains a non-finite number")

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("Invalid JSON syntax or text encoding") from exc


def read_json(path: Path) -> object:
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("JSON file exceeds the size limit")
    return loads_json(raw)


def parse_stamp(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Invalid snapshot timestamp")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("Timezone required")
        return stamp.astimezone(UTC).replace(microsecond=0)
    except (ValueError, OverflowError) as exc:
        raise ValueError("Timestamp must be an ISO date with a timezone") from exc


def iso(stamp: datetime) -> str:
    return stamp.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def validate_values(raw: object) -> dict:
    if not isinstance(raw, dict) or set(raw) != set(METRICS):
        raise ValueError("Exactly the sixteen approved metric keys are required (schema 2)")
    values = {}
    for key, (low, high, precision) in METRICS.items():
        value = raw[key]
        if value is None:
            values[key] = None
        elif type(value) not in (int, float):
            raise ValueError("Metrics must be JSON numbers or null")
        elif not low <= value <= high or not math.isfinite(value):
            raise ValueError("Metric outside the allowed range")
        else:
            rounded = round(value, precision)
            values[key] = (0 if rounded == 0 else rounded)
    return values


def validate(payload: object, *, now: datetime | None = None) -> dict:
    if not isinstance(payload, dict) or set(payload) != {"schema", "recorded_at", "values"}:
        raise ValueError("Invalid payload fields; expected schema, recorded_at, values")
    if type(payload["schema"]) is not int or payload["schema"] != SCHEMA:
        raise ValueError("Unsupported schema; the Home Assistant payload must use schema 2")
    stamp = parse_stamp(payload["recorded_at"])
    age = ((now or datetime.now(UTC)) - stamp).total_seconds()
    if age < -300 or age > 24 * 3600:
        raise ValueError("Snapshot timestamp is outside the allowed window; send a fresh update")
    return {"recorded_at": iso(stamp), "values": validate_values(payload["values"])}


def empty_history() -> dict:
    return {"version": HISTORY_VERSION, "kind": "observed", "samples": []}


def validate_history(history: object) -> dict:
    if not isinstance(history, dict) or set(history) != {"version", "kind", "samples"}:
        raise ValueError("Invalid history fields; refusing to discard existing history")
    if type(history["version"]) is not int or history["version"] != HISTORY_VERSION:
        raise ValueError("Unsupported history version")
    if history["kind"] != "observed":
        raise ValueError("Only observed history can be restored")
    raw = history["samples"]
    if not isinstance(raw, list) or len(raw) > MAX_SAMPLES:
        raise ValueError("Invalid history sample count")
    samples = {}
    future_limit = datetime.now(UTC) + timedelta(minutes=5)
    for sample in raw:
        if not isinstance(sample, dict) or set(sample) != {"recorded_at", "values"}:
            raise ValueError("Invalid history sample fields")
        stamp = parse_stamp(sample["recorded_at"])
        if stamp > future_limit:
            raise ValueError("History contains a timestamp in the future")
        key = iso(stamp)
        samples[key] = {"recorded_at": key, "values": validate_values(sample["values"])}
    return {"version": HISTORY_VERSION, "kind": "observed", "samples": [samples[t] for t in sorted(samples)]}


def merge_history(history: dict, snapshot: dict) -> dict:
    history = validate_history(history)
    by_time = {sample["recorded_at"]: sample for sample in history["samples"]}
    by_time[snapshot["recorded_at"]] = snapshot
    newest = max(parse_stamp(t) for t in by_time)
    cutoff = newest - timedelta(seconds=WINDOW_SECONDS)
    samples = [by_time[t] for t in sorted(by_time) if parse_stamp(t) >= cutoff]
    if len(samples) > MAX_SAMPLES:
        raise ValueError("Too many snapshots in the 48-hour window")
    return {"version": HISTORY_VERSION, "kind": "observed", "samples": samples}


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n", encoding="utf-8")
    temporary.replace(path)


def number(value: float | None, precision: int) -> str:
    if value is None:
        return "—"
    return f"{(0 if value == 0 else value):.{precision}f}".replace(".", ",")


def icon(name: str) -> str:
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + ICONS[name] + '</svg>'


def segments_for(samples: list[dict], key: str) -> list[list[tuple[float, float]]]:
    segments, segment = [], []
    for sample in samples:
        timestamp = parse_stamp(sample["recorded_at"]).timestamp()
        value = sample["values"][key]
        if value is None:
            if segment:
                segments.append(segment)
                segment = []
        else:
            if segment and timestamp - segment[-1][0] > GAP_SECONDS:
                segments.append(segment)
                segment = []
            segment.append((timestamp, float(value)))
    if segment:
        segments.append(segment)
    return segments


def chart_html(samples: list[dict], key: str, label: str, room: str, end: datetime) -> str:
    precision = METRICS[key][2]
    unit = "°C" if key.endswith("_c") else "%"
    segments = segments_for(samples, key)
    valid = [value for segment in segments for _, value in segment]
    start_epoch, end_epoch = end.timestamp() - WINDOW_SECONDS, end.timestamp()
    width, height, padding = 320, 92, 5
    paths = []
    if valid:
        lowest, highest = min(valid), max(valid)
        minimum_span = 2.0 if unit == "°C" else 10.0
        span = max(highest - lowest, minimum_span)
        middle = (lowest + highest) / 2
        low = max(METRICS[key][0], middle - span * .62)
        high = min(METRICS[key][1], middle + span * .62)
        y_span = max(high - low, .1)
        for segment in segments:
            points = [(
                padding + (t - start_epoch) / WINDOW_SECONDS * (width - padding * 2),
                height - padding - (v - low) / y_span * (height - padding * 2)
            ) for t, v in segment]
            line = "M " + " L ".join(f"{x:.2f} {y:.2f}" for x, y in points)
            if len(points) > 1:
                fill = line + f" L {points[-1][0]:.2f} {height} L {points[0][0]:.2f} {height} Z"
                paths.append(f'<path class="chart-fill" d="{fill}"/>')
                paths.append(f'<path class="chart-line" d="{line}"/>')
            else:
                x, y = points[0]
                paths.append(f'<circle class="chart-point" cx="{x:.2f}" cy="{y:.2f}" r="2.6"/>')
        # The end marker is only added when this metric has a real latest value.
        if samples[-1]["values"][key] is not None and len(segments[-1]) > 1:
            t, v = segments[-1][-1]
            x = padding + (t - start_epoch) / WINDOW_SECONDS * (width - padding * 2)
            y = height - padding - (v - low) / y_span * (height - padding * 2)
            paths.append(f'<circle class="chart-point" cx="{x:.2f}" cy="{y:.2f}" r="2.6"/>')
        detail = f'Pienin <strong>{number(lowest, precision)} {unit}</strong><span> / </span>Suurin <strong>{number(highest, precision)} {unit}</strong>'
        if len(valid) == 1:
            detail = 'Yksi mittaus · historiaa kertyy seuraavilla päivityksillä'
    else:
        paths.append('<text x="160" y="49" text-anchor="middle" class="chart-empty">Ei historiatietoja</text>')
        detail = 'Ei mittaustietoja tällä aikavälillä'
    data = [[int(parse_stamp(s["recorded_at"]).timestamp()), s["values"][key]] for s in samples]
    encoded = html.escape(json.dumps(data, separators=(",", ":"), allow_nan=False), quote=True)
    title = html.escape(f"{room}: {label.lower()}, 48 tuntia. Nuolinäppäimillä voi selata mittauksia.")
    return (
        f'<div class="chart" tabindex="0" role="group" aria-label="{title}" '
        f'data-series="{encoded}" data-end="{int(end_epoch)}" data-unit="{unit}" data-precision="{precision}">'
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" role="img" aria-label="{title}">'
        f'<path class="chart-guide" d="M 0 91 H {width}"/>' + ''.join(paths) + '</svg>'
        '<div class="chart-cursor" hidden></div><output class="chart-tooltip" hidden></output></div>'
        '<div class="chart-times" aria-hidden="true"><span>−48 h</span><span>−24 h</span><span>Koonti</span></div>'
        f'<div class="chart-range">{detail}</div>'
    )


def metric_html(samples: list[dict], key: str, label: str, room: str, end: datetime) -> str:
    value = samples[-1]["values"][key]
    humidity = key.endswith("_rh")
    kind, unit, precision = ("humidity", "%", 0) if humidity else ("temperature", "°C", 1)
    shown_unit = f'<span class="unit">{unit}</span>' if value is not None else ''
    return (
        f'<section class="metric {kind}" aria-label="{html.escape(label)}">'
        f'<div class="metric-heading"><span class="metric-label"><i></i>{label}</span>'
        + ('<span class="missing-label">Ei mittausta</span>' if value is None else '') + '</div>'
        f'<div class="value">{number(value, precision)}{shown_unit}</div>'
        + chart_html(samples, key, label, room, end) + '</section>'
    )


def render(history: dict, template: str) -> str:
    samples = history["samples"]
    if not samples:
        raise ValueError("No stored observations; send an update from Home Assistant first")
    end = parse_stamp(samples[-1]["recorded_at"])
    start = end - timedelta(seconds=WINDOW_SECONDS)
    samples = [sample for sample in samples if parse_stamp(sample["recorded_at"]) >= start]
    values = samples[-1]["values"]
    cards = []
    for key, label, icon_name in ZONES:
        missing = sum(values[f"{key}_{suffix}"] is None for suffix in ("c", "rh"))
        cards.append(
            '<article class="card' + (' missing' if missing else '') + '">'
            f'<header class="card-heading"><span class="room-icon">{icon(icon_name)}</span><h2>{html.escape(label)}</h2>'
            + ('<span class="room-warning" title="Yksi tai useampi nykyarvo puuttuu">!</span>' if missing else '')
            + '</header>'
            + metric_html(samples, f"{key}_c", "Lämpötila", label, end)
            + metric_html(samples, f"{key}_rh", "Ilmankosteus", label, end)
            + '</article>'
        )
    available = sum(value is not None for value in values.values())
    span_h = (end - parse_stamp(samples[0]["recorded_at"])).total_seconds() / 3600
    if len(samples) == 1:
        history_note = "Ensimmäinen tilannekuva tallennettu. Käyrät alkavat muodostua seuraavilla päivityksillä."
    elif span_h < 47.5:
        history_note = f"Historiaa kertynyt {number(span_h, 1)} h. Tyhjä osa täyttyy tulevilla päivityksillä."
    else:
        history_note = "Viimeiset 48 tuntia. Puuttuvat arvot ja pitkät lähetyskatkot näkyvät käyrissä aukkoina."
    fi_zone = ZoneInfo("Europe/Helsinki")
    def date_text(t):
        return t.astimezone(fi_zone).strftime("%d.%m. klo %H.%M")
    return Template(template).substitute(
        cards="\n".join(cards),
        timestamp_iso=iso(end), timestamp_readable=date_text(end),
        range_start=date_text(start), range_end=date_text(end),
        summary=f"{available} / 16 mittausarvoa saatavilla",
        history_count=f"{len(samples)} tilannekuvaa", history_note=history_note,
        available=str(available),
        status_initial="Tilannekuva · tarkista koontiaika",
    )



def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", help="GitHub repository_dispatch event JSON")
    parser.add_argument("--rebuild", action="store_true", help="Rebuild using the last real observations")
    parser.add_argument("--history", default="state/history.json")
    parser.add_argument("--history-output", help="Path to validated history for the next run")
    parser.add_argument("--output", default="site")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    history_path = Path(args.history)
    history = validate_history(read_json(history_path)) if history_path.exists() else empty_history()
    if args.rebuild or event_name == "workflow_dispatch":
        if not history["samples"]:
            raise ValueError("No stored history yet; run the publish action in Home Assistant first")
    else:
        if not args.event or event_name != "repository_dispatch":
            raise ValueError("Use a repository_dispatch event or --rebuild")
        event = read_json(Path(args.event))
        if not isinstance(event, dict) or event.get("action") != EVENT_TYPE:
            raise ValueError("Unexpected dispatch event; expected github-update")
        snapshot = validate(event.get("client_payload"))
        history = merge_history(history, snapshot)
    document = render(history, (root / "template.html").read_text(encoding="utf-8"))
    target = Path(args.output)
    target.mkdir(parents=True, exist_ok=True)
    (target / "index.html").write_text(document, encoding="utf-8")
    (target / ".nojekyll").write_text("", encoding="utf-8")
    if args.history_output:
        write_json(Path(args.history_output), history)
    print(f"Rendered 8 rooms; {len(history['samples'])} snapshots. Input measurements are not logged.")


if __name__ == "__main__":
    try:
        main()
    except ValueError as exc:
        print(f"Render refused: {exc}", file=sys.stderr)
        sys.exit(1)
    except (OSError, KeyError, OverflowError) as exc:
        print(f"Render failed ({type(exc).__name__}); check files and configuration.", file=sys.stderr)
        sys.exit(1)
