"""Views and clicks for the walletmcppass.com landing page, read from that site's own sqlite db
(~/projects/walletmcppass-landing/data/walletmcppass.db, mounted read-only into this unit), for the
tailnet dashboard. Returns None if the db can't be read, so the pass stats still load."""
import datetime
import logging
import re
import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.environ.get("WALLET_MCP_SITE_DB",
                              Path.home() / "projects/walletmcppass-landing/data/walletmcppass.db"))
SELF_IP = "149.56.100.54"  # the VPS itself: my own checks
# A vulnerability scanner (2026-09-22, before /api/track allowlisted event names) that sent ~2,700
# junk "clicks" (SQL/path-traversal payloads, "unknown") and 435 page views with a browser user-agent.
SCANNER_IPS = {"188.253.123.2"}
# Real button names are snake_case (track('nav_connect')); anything else was injected, so it's not shown.
EVENT_NAME = re.compile(r"[a-z][a-z0-9_]{1,40}")
# Uptime checkers, crawlers and scripts; matched against the lower-cased user-agent.
BOT_UA = ("bot", "crawl", "spider", "check", "curl", "python", "monitor", "uptime", "headless", "preview")
SETUP_CLICK = "wallet_setup_coinbase"

logger = logging.getLogger(__name__)


def _human(ip: str | None, ua: str | None) -> bool:
    ua = (ua or "").lower()
    return ip != SELF_IP and ip not in SCANNER_IPS and bool(ua) and not any(b in ua for b in BOT_UA)


def _real_event(event: str) -> bool:
    return event != "unknown" and bool(EVENT_NAME.fullmatch(event or ""))


def _count_clicks(rows) -> list[dict]:
    counts: dict[str, int] = {}
    for event, ip, ua in rows:
        if _human(ip, ua) and _real_event(event):
            counts[event] = counts.get(event, 0) + 1
    return sorted(({"event": e, "c": c} for e, c in counts.items()), key=lambda x: -x["c"])


def stats(days: int = 14, tz_min: int = 0) -> dict | None:
    """Days are the browser's local days (tz_min minutes east of UTC)."""
    off = datetime.timedelta(minutes=tz_min)
    local_now = datetime.datetime.now(datetime.timezone.utc) + off
    since = (local_now.replace(hour=0, minute=0, second=0, microsecond=0)
             - datetime.timedelta(days=days - 1) - off).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2)
        try:
            rows = con.execute("SELECT ts, event, ip, ua FROM events WHERE ts >= ?", (since,)).fetchall()
            total = con.execute("SELECT ip, ua FROM events WHERE event = 'pageview'").fetchall()
            all_clicks = con.execute("SELECT event, ip, ua FROM events WHERE event != 'pageview'").fetchall()
            setup_all = con.execute("SELECT count(*), count(DISTINCT ip) FROM events WHERE event = ?",
                                    (SETUP_CLICK,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        logger.warning("site stats unavailable", exc_info=True)
        return None
    by_day: dict[str, dict] = {}
    clicks: dict[str, int] = {}
    for ts, event, ip, ua in rows:
        if not _human(ip, ua):
            continue
        if event == "pageview":
            local = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")) + off
            day = by_day.setdefault(local.strftime("%Y-%m-%d"), {"views": 0, "ips": set()})
            day["views"] += 1
            day["ips"].add(ip)
        elif _real_event(event):
            clicks[event] = clicks.get(event, 0) + 1
    human_total = [(ip, ua) for ip, ua in total if _human(ip, ua)]
    week = sorted(by_day)[-7:]
    return {
        "days": [{"day": d, "views": by_day[d]["views"], "people": len(by_day[d]["ips"])} for d in sorted(by_day)],
        "week_views": sum(by_day[d]["views"] for d in week),
        "week_people": len(set().union(*(by_day[d]["ips"] for d in week))) if week else 0,
        "total_views": len(human_total),
        "total_people": len({ip for ip, _ in human_total}),
        "setup_clicks": setup_all[0],
        "setup_people": setup_all[1],
        "clicks": sorted(({"event": e, "c": c} for e, c in clicks.items()), key=lambda x: -x["c"]),
        "clicks_all": _count_clicks(all_clicks),
    }
