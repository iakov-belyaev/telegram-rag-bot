"""Export OSINT dossiers: JSON, JSONL, CSV and a standalone HTML report.

Usage:
    python export.py "+35799123456" --format html
    python export.py "@username" --format json --out state/exports

Uses query.dossier() for data collection, then renders dependency-free outputs
(HTML uses inline CSS). Files land in config.EXPORT_DIR by default.
"""

from __future__ import annotations

import csv
import html
import json
import sys
import time
from pathlib import Path
from typing import Optional

import config
from logging_config import get_logger

_logger = get_logger("export")


def _safe_name(query: str) -> str:
    keep = "".join(c if (c.isalnum() or c in "-_") else "_" for c in query)
    return (keep or "dossier")[:60]


def _write_messages_csv(dossier: dict, path: Path) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "chat_title", "chat_id", "sender_name", "sender_username", "link", "text"])
        for m in dossier.get("messages", []):
            writer.writerow([
                m.get("date", ""), m.get("chat_title", ""), m.get("chat_id", ""),
                m.get("sender_name", ""), m.get("sender_username", ""),
                m.get("link", ""), str(m.get("text", ""))[:500],
            ])


def _render_html(dossier: dict) -> str:
    esc = html.escape
    e = dossier.get("entity", {})
    lines: list[str] = []
    lines.append("<!doctype html><html><head><meta charset='utf-8'>")
    lines.append("<title>Dossier: " + esc(str(dossier.get("query", ""))) + "</title>")
    lines.append(
        "<style>body{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;max-width:900px;"
        "margin:2rem auto;padding:0 1rem;color:#1a1a1a}h1{font-size:1.4rem}h2{font-size:1.05rem;"
        "margin-top:1.6rem;border-bottom:1px solid #ddd;padding-bottom:.2rem}.tag{background:#eef;"
        "border-radius:4px;padding:1px 6px;font-size:.8rem}table{border-collapse:collapse;width:100%}"
        "td,th{border:1px solid #ddd;padding:4px 6px;text-align:left;font-size:.9rem}"
        ".muted{color:#666}.err{color:#b00}</style></head><body>"
    )
    lines.append(f"<h1>Dossier: {esc(str(dossier.get('query', '')))} "
                 f"<span class='tag'>{esc(str(e.get('type', '')))}</span></h1>")
    lines.append(f"<p class='muted'>generated_at {esc(str(dossier.get('generated_at', '')))} · "
                 f"backend {esc(str(dossier.get('backend', '')))}</p>")
    if dossier.get("error"):
        lines.append(f"<p class='err'>error: {esc(str(dossier['error']))}</p>")

    identity = dossier.get("identity", {})
    profile = identity.get("profile") if isinstance(identity, dict) else None
    lines.append("<h2>Identity</h2>")
    if profile:
        lines.append(f"<p><b>{esc(str(profile.get('display_name', '')))}</b> "
                     f"(@{esc(str(profile.get('username', '')))}) — id {esc(str(profile.get('user_id', '')))}<br>"
                     f"<span class='muted'>bio: {esc(str(profile.get('bio', '')))}</span><br>"
                     f"messages: {esc(str(profile.get('message_count', 0)))} · "
                     f"first: {esc(str(profile.get('first_seen', '')))} · "
                     f"last: {esc(str(profile.get('last_seen', '')))}</p>")
    else:
        lines.append("<p class='muted'>no direct profile match</p>")

    for section, rows in (("Profiles", dossier.get("profiles", [])[:20]),
                          ("Chats", dossier.get("chats", [])[:20])):
        if rows:
            lines.append(f"<h2>{section}</h2><ul>")
            for r in rows:
                label = r.get("username") or r.get("chat_title") or r.get("chat_id") or r
                lines.append(f"<li>{esc(str(label))}</li>")
            lines.append("</ul>")

    messages = dossier.get("messages", [])[:50]
    lines.append("<h2>Messages</h2>")
    if messages:
        lines.append("<table><tr><th>date</th><th>chat</th><th>sender</th><th>text</th></tr>")
        for m in messages:
            sender = str(m.get("sender_name", "")) + (" (@" + str(m.get("sender_username")) + ")" if m.get("sender_username") else "")
            text = esc(str(m.get("text", ""))[:400])
            link = m.get("link")
            if link:
                text += f" <a href='{esc(str(link))}'>[link]</a>"
            lines.append(f"<tr><td>{esc(str(m.get('date', '')))}</td><td>{esc(str(m.get('chat_title', '')))}</td>"
                         f"<td>{esc(sender)}</td><td>{text}</td></tr>")
        lines.append("</table>")
    else:
        lines.append("<p class='muted'>no message matches</p>")

    stats = dossier.get("stats", {})
    lines.append(f"<h2>Stats</h2><p class='muted'>{esc(json.dumps(stats, ensure_ascii=False))}</p>")
    lines.append("</body></html>")
    return "\n".join(lines)


async def export_dossier(query_text: str, fmt: str = "html", out_dir: Optional[str] = None,
                         limit: Optional[int] = None) -> dict:
    """Build a dossier and write it in the requested format. Returns written paths."""
    import query as query_mod

    result = await query_mod.dossier(query_text, limit=limit)
    base = Path(out_dir) if out_dir else Path(config.EXPORT_DIR)
    base.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = _safe_name(query_text)
    written: list[str] = []

    if fmt in ("json", "all"):
        path = base / f"dossier_{safe}_{stamp}.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(str(path))
    if fmt in ("jsonl", "all"):
        path = base / f"dossier_{safe}_{stamp}.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(result, ensure_ascii=False) + "\n")
            for m in result.get("messages", []):
                f.write(json.dumps({"type": "message", **m}, ensure_ascii=False) + "\n")
        written.append(str(path))
    if fmt in ("csv", "all"):
        path = base / f"dossier_{safe}_{stamp}.csv"
        _write_messages_csv(result, path)
        written.append(str(path))
    if fmt in ("html", "all"):
        path = base / f"dossier_{safe}_{stamp}.html"
        path.write_text(_render_html(result), encoding="utf-8")
        written.append(str(path))
    if not written:
        raise ValueError(f"unknown format: {fmt}")
    return {"query": query_text, "format": fmt, "written": written}


def _cli(argv: list[str]) -> int:
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(prog="export.py", description="Export an OSINT dossier.")
    parser.add_argument("query")
    parser.add_argument("--format", default="html", choices=["html", "json", "jsonl", "csv", "all"])
    parser.add_argument("--out", default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(export_dossier(args.query, args.format, args.out, args.limit))
    except Exception as e:  # noqa: BLE001
        _logger.error("export failed: %s", e)
        print(f"export failed: {e}", file=sys.stderr)
        return 1
    for p in result["written"]:
        print(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
