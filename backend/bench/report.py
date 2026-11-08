"""Turn benchmark JSON (bench_overhead.py, bench_e2e.py) into a Markdown report.

    .venv/bin/python bench/report.py bench/results/overhead-XXXX.json [e2e-XXXX.json] [-o out.md]

The report ends with the decision rule agreed for a Rust rewrite: only if
the bot's own work is a real share of an answer, or the process runs out of
CPU or memory, at the concurrency the team needs.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

# Thresholds of the decision rule.
OVERHEAD_SHARE = 0.10  # overhead p95 vs. total p50, at 10 conversations
CPU_SATURATION = 80.0  # % of one core, before 50 conversations
RSS_LIMIT_MB = 300.0


def fmt(value, unit: str = "ms") -> str:
    if value is None:
        return "—"
    if unit == "ms" and value >= 1000:
        return f"{value / 1000:.2f} s"
    return f"{value:.1f} {unit}".strip() if isinstance(value, float) else f"{value} {unit}".strip()


def overhead_section(data: dict) -> tuple[list[str], dict]:
    lines = [
        "## Offline: the bot's own work",
        "",
        f"- Date: {data['date']}, Python {data['python']} on {data['event_loop']}, "
        f"{data['cpus']} CPUs, {data['memory_mb']} MB RAM",
        f"- Fake model: first token after {data['model_profile']['ttft_ms']:g} ms, "
        f"{data['model_profile']['tokens_per_second']:g} tokens/s, "
        f"{data['model_profile']['answer_tokens']} tokens per answer; tools take "
        f"{data['tool_latency_ms']:g} ms; at most {data['bot_max_concurrency']} messages handled at once",
        "",
        "| Scenario | Conv. | History | Tools | Turns | Own work p50 | p95 | p99 | Total p50 | First words p50 | CPU / turn | CPU (% core) | Peak RSS |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    by_name = {}
    for s in data["scenarios"]:
        sc = s["scenario"]
        by_name[sc["name"]] = s
        o = s["overhead_ms"]
        lines.append(
            f"| {sc['name']} | {sc['concurrency']} | {sc['history']} | {sc['tools']} | {s['turns']} "
            f"| {fmt(o['p50'])} | {fmt(o['p95'])} | {fmt(o['p99'])} | {fmt(s['total_ms']['p50'])} "
            f"| {fmt(s['ttft_ms']['p50'])} | {fmt(s['cpu_ms_per_turn'])} | {s['cpu_percent_of_core']}% "
            f"| {s['peak_rss_mb']} MB |"
        )
    lines += ["", "Where a turn's time went (p50 per turn, ms):", ""]
    lines += ["| Scenario | Model | Tools | Rocket.Chat | Database | Queue | Own work |", "|---|---|---|---|---|---|---|"]
    for s in data["scenarios"]:
        w = s["waits_ms"]
        lines.append(
            f"| {s['scenario']['name']} | {w['llm']['p50']} | {w['tools']['p50']} | {w['rc']['p50']} "
            f"| {w['db']['p50']} | {w['queue']['p50']} | {s['overhead_ms']['p50']} |"
        )
    if data.get("micro_us"):
        lines += ["", "CPU-bound helpers, per call:", "", "| Helper | µs |", "|---|---|"]
        lines += [f"| {name} | {us} |" for name, us in data["micro_us"].items()]
    return lines, by_name


def e2e_section(data: dict) -> list[str]:
    lines = [
        "## End to end on the running stack",
        "",
        f"- Date: {data['date']}, {data['messages']} messages, {data['concurrency']} at a time, "
        f"model: {data.get('model') or 'as configured'}",
        "",
        "| Step, seen by the user | p50 | p95 | max |",
        "|---|---|---|---|",
    ]
    for key, label in (("ack_ms", "Reaction 👀 appears"), ("first_ms", "First words appear"), ("done_ms", "Answer complete")):
        p = data[key]
        lines.append(f"| {label} | {fmt(p['p50'])} | {fmt(p['p95'])} | {fmt(p['max'])} |")
    if data.get("missing"):
        lines += ["", f"{data['missing']} message(s) got no answer within the timeout."]
    return lines


def decision(by_name: dict) -> list[str]:
    lines = ["## Rust: decision rule", ""]
    verdicts = []
    c10 = by_name.get("c10")
    if c10:
        share = (c10["overhead_ms"]["p95"] or 0) / max(1.0, c10["total_ms"]["p50"] or 1)
        hit = share > OVERHEAD_SHARE
        verdicts.append(hit)
        lines.append(
            f"- Own work p95 at 10 conversations is {share:.1%} of a typical answer "
            f"(limit {OVERHEAD_SHARE:.0%}): **{'exceeded' if hit else 'not exceeded'}**."
        )
    c50 = by_name.get("c50") or by_name.get("c50-h200-tools")
    if c50:
        cpu = c50["cpu_percent_of_core"] or 0
        hit = cpu > CPU_SATURATION
        verdicts.append(hit)
        lines.append(
            f"- CPU at 50 conversations is {cpu}% of one core (limit {CPU_SATURATION:g}%): "
            f"**{'exceeded' if hit else 'not exceeded'}**."
        )
    rss = max((s["peak_rss_mb"] for s in by_name.values()), default=0)
    hit = rss > RSS_LIMIT_MB
    verdicts.append(hit)
    lines.append(f"- Peak memory is {rss} MB (limit {RSS_LIMIT_MB:g} MB): **{'exceeded' if hit else 'not exceeded'}**.")
    lines += [
        "",
        "**Verdict: " + (
            "a Rust module for the measured hot spot is worth considering."
            if any(verdicts) else "stay with Python; a rewrite would not make answers noticeably faster."
        ) + "**",
    ]
    return lines


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("-o", "--out", type=Path)
    args = parser.parse_args()

    lines = ["# Assistant benchmark", ""]
    by_name: dict = {}
    for path in args.files:
        data = json.loads(path.read_text())
        if data["kind"] == "overhead":
            section, by_name = overhead_section(data)
        else:
            section = e2e_section(data)
        lines += section + [""]
    if by_name:
        lines += decision(by_name)
    text = "\n".join(lines) + "\n"
    if args.out:
        args.out.write_text(text)
        print(args.out)
    else:
        print(text)


if __name__ == "__main__":
    main()
