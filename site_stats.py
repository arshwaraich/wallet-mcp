"""Views and clicks for the walletmcppass.com landing page, read from that site's own sqlite db
(~/projects/walletmcppass-landing/data/walletmcppass.db, mounted read-only into this unit), for the
tailnet dashboard. Returns None if the db can't be read, so the pass stats still load."""
import logging
import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.environ.get("WALLET_MCP_SITE_DB",
                              Path.home() / "projects/walletmcppass-landing/data/walletmcppass.db"))
SELF_IP = "149.56.100.54"  # the VPS itself: my own checks
# Uptime checkers, crawlers and scripts; matched against the lower-cased user-agent.
BOT_UA = ("bot", "crawl", "spider", "check", "curl", "python", "monitor", "uptime", "headless", "preview")
SETUP_CLICK = "wallet_setup_coinbase"

logger = logging.getLogger(__name__)


def _human(ip: str | None, ua: str | None) -> bool:
    ua = (ua or "").lower()
    return ip != SELF_IP and bool(ua) and not any(b in ua for b in BOT_UA)


def stats(days: int = 14) -> dict | None:
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2)
        try:
            rows = con.execute("SELECT ts, event, ip, ua FROM events WHERE ts >= date('now', ?)",
                               (f"-{days - 1} day",)).fetchall()
            total = con.execute("SELECT ip, ua FROM events WHERE event = 'pageview'").fetchall()
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
            day = by_day.setdefault(ts[:10], {"views": 0, "ips": set()})
            day["views"] += 1
            day["ips"].add(ip)
        else:
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
    }
