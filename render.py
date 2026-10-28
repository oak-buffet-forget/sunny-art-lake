#!/usr/bin/env python3
"""Render an eight-zone static dashboard; Python standard library only.

No HA connection, entity IDs, tokens or live measurements are stored here.
Only an exact numeric/null schema is accepted. Input payloads are never logged.
"""
import argparse
from datetime import datetime, timezone
import html
import json
import math
import os
from pathlib import Path
from string import Template
from zoneinfo import ZoneInfo
import sys

SCHEMA = 2
ZONES = (
    ("outdoor", "Ulkona"),
    ("zone_a", "Vintti 1"),
    ("zone_b", "Makuuhuone 1"),
    ("zone_c", "Makuuhuone 2"),
    ("zone_d", "Kattilahuone"),
    ("zone_e", "Olohuone"),
    ("zone_f", "Keittiö"),
    ("zone_g", "Kellarikomero"),
)
METRICS = {
    f"{zone}_{suffix}": (low, high, precision)
    for zone, _ in ZONES
    for suffix, low, high, precision in (
        ("c", -60, 80, 1),
        ("rh", 0, 100, 0),
    )
}

def validate(payload: dict) -> tuple[datetime, dict]:
    if not isinstance(payload, dict) or set(payload) != {"schema", "recorded_at", "values"}:
        raise ValueError("Invalid payload fields")
    if type(payload["schema"]) is not int or payload["schema"] != SCHEMA:
        raise ValueError("Unsupported schema; update the publisher and renderer together")
    raw = payload["values"]
    if not isinstance(raw, dict) or set(raw) != set(METRICS):
        raise ValueError("Exactly the sixteen approved metric keys are required")
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
            values[key] = round(value, precision)
    try:
        stamp = datetime.fromisoformat(payload["recorded_at"].replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("Invalid snapshot timestamp") from exc
    if stamp.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    stamp = stamp.astimezone(timezone.utc)
    age = (datetime.now(timezone.utc) - stamp).total_seconds()
    if age < -300 or age > 24 * 3600:
        raise ValueError("Snapshot timestamp is outside the allowed time window")
    # All-null snapshots are valid: explicitly show an outage, not old readings.
    return stamp, values

def metric_html(label: str, value: float | None, unit: str, precision: int) -> str:
    # Normalize negative zero and use a Finnish decimal comma.
    number = 0.0 if value == 0 else value
    display = "—" if number is None else f"{number:.{precision}f}".replace(".", ",")
    unit_html = "" if value is None else f'<span class="unit">{html.escape(unit)}</span>'
    return (
        '<div class="metric">'
        f'<div class="metric-label">{html.escape(label)}</div>'
        f'<div class="val">{display}{unit_html}</div>'
        '</div>'
    )

def render(payload: dict, template: str, *, demo: bool = False) -> str:
    stamp, values = validate(payload)
    cards = []
    for zone, label in ZONES:
        temp, humidity = values[f"{zone}_c"], values[f"{zone}_rh"]
        missing = int(temp is None) + int(humidity is None)
        note = ("Mittauksia ei saatavilla" if missing == 2 else
                "Lämpötilatieto puuttuu" if temp is None else
                "Kosteustieto puuttuu" if humidity is None else
                "Lämpötila ja suhteellinen ilmankosteus")
        kind = " missing" if missing else ""
        cards.append(
            f'<article class="card{kind}">'
            f'<h2 class="label">{html.escape(label)}</h2>'
            '<div class="metrics">'
            + metric_html("Lämpötila", temp, "°C", 1)
            + metric_html("Ilmankosteus", humidity, "%", 0)
            + f'</div><div class="card-note">{note}</div></article>'
        )
    available = sum(v is not None for v in values.values())
    summary = (f"{available} / {len(values)} mittausarvoa saatavilla"
               if available else "Mittausarvoja ei saatavilla")
    return Template(template).substitute(
        cards="\n".join(cards),
        timestamp_iso=html.escape(stamp.isoformat().replace("+00:00", "Z"), quote=True),
        timestamp_readable=html.escape(
            stamp.astimezone(ZoneInfo("Europe/Helsinki")).strftime("%d.%m.%Y klo %H.%M")
        ),
        summary=summary,
        available=str(available),
        demo_flag="true" if demo else "false",
        status_initial=("Esimerkkitiedot" if demo else
                        "Mittauksia ei saatavilla" if not available else
                        "Tilannekuva · tarkista koontiaika"),
        demo_notice=(
            '<p class="demo-notice">ESIMERKKITIEDOT: keksittyjä arvoja, eivät oikeita mittauksia.</p>'
            if demo else ""
        ),
    )

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", help="GitHub event JSON file")
    parser.add_argument("--sample", action="store_true", help="Publish visibly marked demo data")
    parser.add_argument("--output", default="site")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    demo = args.sample or event_name == "workflow_dispatch"
    if demo:
        payload = json.loads((root / "sample.json").read_text(encoding="utf-8"))
        payload["recorded_at"] = datetime.now(timezone.utc).isoformat()
    else:
        if not args.event or event_name != "repository_dispatch":
            raise ValueError("Use --sample or a repository_dispatch event")
        event = json.loads(Path(args.event).read_text(encoding="utf-8"))
        if not isinstance(event, dict) or event.get("action") != "github-update":
            raise ValueError("Unexpected dispatch event")
        payload = event.get("client_payload")
    output = render(payload, (root / "template.html").read_text(encoding="utf-8"), demo=demo)
    target = Path(args.output)
    target.mkdir(parents=True, exist_ok=True)
    (target / "index.html").write_text(output, encoding="utf-8")
    (target / ".nojekyll").write_text("", encoding="utf-8")
    print("Static dashboard rendered; input data intentionally not logged.")

if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as exc:
        # Do not dump the payload or environment on failures.
        print(f"Render refused ({type(exc).__name__}); check schema and input validity.", file=sys.stderr)
        sys.exit(1)
