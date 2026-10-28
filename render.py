#!/usr/bin/env python3
"""Render a static dashboard from a tiny, numeric-only, allowlisted payload.

Uses only the Python standard library. It does not access Home Assistant.
Unexpected keys are rejected, not copied into the generated HTML.
"""
import argparse
from datetime import datetime, timezone
import html
import json
import math
import os
from pathlib import Path
from string import Template


# Edit this list and the corresponding HA whitelist together.
# Fields outside this list are rejected rather than exported.
METRICS = (
    ("outdoor_c", "Outdoor temperature", "°C", -55, 55, 1),
    ("indoor_c", "Indoor temperature", "°C", -30, 55, 1),
    ("cellar_c", "Lower-level temperature", "°C", -30, 55, 1),
    ("cellar_rh", "Relative humidity", "%", 0, 100, 0),
)
ALLOWED = {field[0] for field in METRICS}


def validate(payload: dict) -> tuple[datetime, dict]:
    if not isinstance(payload, dict) or set(payload) != {"schema", "recorded_at", "values"}:
        raise ValueError("Invalid payload fields; refusing to deploy")
    if payload["schema"] != 1:
        raise ValueError("Unsupported payload schema")

    raw = payload["values"]
    if not isinstance(raw, dict) or not raw or set(raw) - ALLOWED:
        raise ValueError("Unexpected metric fields; refusing to deploy")

    result = {}
    for name, _, _, low, high, decimals in METRICS:
        value = raw.get(name)
        if value is None:
            result[name] = None
            continue
        if isinstance(value, bool) or not isinstance(value, (float, int)):
            raise ValueError("Metric must be a JSON number or null")
        if not math.isfinite(value) or not (low <= value <= high):
            raise ValueError("Metric out of allowed range")
        result[name] = round(value, decimals)

    if all(v is None for v in result.values()):
        raise ValueError("All sensors unavailable; keeping previous published page")

    try:
        recorded_at = datetime.fromisoformat(payload["recorded_at"].replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("Invalid timestamp") from exc
    if recorded_at.tzinfo is None:
        raise ValueError("Timestamp requires timezone")
    recorded_at = recorded_at.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    if abs((now - recorded_at).total_seconds()) > 24 * 3600:
        # Test payloads use current time; old or grossly future-dated HA data is rejected.
        raise ValueError("Snapshot timestamp is outside allowed 24-hour window")
    return recorded_at, result


def render(payload, template: str) -> str:
    stamp, values = validate(payload)
    pieces = []
    for key, name, unit, _, _, precision in METRICS:
        value = values[key]
        if value is None:
            val_text = "—"
            unit_text = ""
        else:
            val_text = f"{value:.{precision}f}"
            unit_text = f'<span class="unit">{html.escape(unit)}</span>'
        pieces.append(
            '<article class="card">'
            f'<div class="label">{html.escape(name)}</div>'
            f'<div class="val">{html.escape(val_text)}{unit_text}</div>'
            '</article>'
        )
    return Template(template).safe_substitute(
        cards="\n".join(pieces),
        timestamp_iso=html.escape(stamp.isoformat().replace("+00:00", "Z"), quote=True),
        timestamp_readable=html.escape(stamp.strftime("%Y-%m-%d %H:%M UTC")),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", help="Path to GITHUB_EVENT_PATH JSON")
    parser.add_argument("--sample", action="store_true", help="Use safe demo values and current time")
    parser.add_argument("--output", default="site", help="Target directory")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    if args.sample or os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        payload = json.loads((root / "sample.json").read_text(encoding="utf-8"))
        payload["recorded_at"] = datetime.now(timezone.utc).isoformat()
    else:
        if not args.event or os.environ.get("GITHUB_EVENT_NAME") != "repository_dispatch":
            raise ValueError("Use --sample or trigger a repository_dispatch event")
        event = json.loads(Path(args.event).read_text(encoding="utf-8"))
        if event.get("action") != "ambient-update":
            raise ValueError("Unexpected GitHub dispatch type")
        payload = event.get("client_payload")

    template = (root / "template.html").read_text(encoding="utf-8")
    content = render(payload, template)
    target = Path(args.output)
    target.mkdir(parents=True, exist_ok=True)
    (target / "index.html").write_text(content, encoding="utf-8")
    (target / ".nojekyll").write_text("", encoding="utf-8")
    print("Static page generated from approved numeric fields")


if __name__ == "__main__":
    main()
