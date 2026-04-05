#!/usr/bin/env python3
"""
CIVOPS — Civilian Operations Signal Recon
Backend: scanner daemon + SQLite logger + HTTP API server
Platform: Termux (Android)
Version: 1.0.0
"""

import json
import os
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

DB_PATH = os.path.expanduser("~/.civops/civops.db")
SCAN_INTERVAL = 15  # seconds between scans
SERVER_PORT = int(os.environ.get("CIVOPS_PORT", 8888))


# ─────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS scans (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      INTEGER NOT NULL,
            source  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id     INTEGER NOT NULL REFERENCES scans(id),
            ts          INTEGER NOT NULL,
            source      TEXT NOT NULL,
            identifier  TEXT NOT NULL,
            address     TEXT,
            rssi        INTEGER,
            frequency   INTEGER,
            channel     INTEGER,
            vendor      TEXT,
            flags       TEXT,
            FOREIGN KEY (scan_id) REFERENCES scans(id)
        );

        CREATE TABLE IF NOT EXISTS deltas (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          INTEGER NOT NULL,
            source      TEXT NOT NULL,
            identifier  TEXT NOT NULL,
            address     TEXT,
            status      TEXT NOT NULL,
            rssi        INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_signals_ts  ON signals(ts);
        CREATE INDEX IF NOT EXISTS idx_signals_src ON signals(source);
        CREATE INDEX IF NOT EXISTS idx_deltas_ts   ON deltas(ts);
    """)
    conn.commit()
    conn.close()


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ─────────────────────────────────────────────
# OUI VENDOR LOOKUP (offline partial table)
# ─────────────────────────────────────────────

OUI_TABLE = {
    "00:50:f2": "Microsoft",
    "00:0c:e7": "Apple",
    "ac:de:48": "Apple",
    "3c:22:fb": "Apple",
    "f8:ff:c2": "Apple",
    "00:1a:11": "Google",
    "54:60:09": "Google",
    "48:d7:05": "Google",
    "00:17:f2": "Amazon",
    "fc:65:de": "Amazon",
    "00:26:b9": "Dell",
    "b8:ac:6f": "Dell",
    "00:50:56": "VMware",
    "00:0f:b5": "Netgear",
    "20:e5:2a": "Netgear",
    "c0:ff:d4": "TP-Link",
    "50:c7:bf": "TP-Link",
    "b0:be:76": "Asus",
    "2c:fd:a1": "Asus",
    "00:1d:7e": "Cisco",
    "00:40:96": "Cisco",
    "dc:2c:6e": "Huawei",
    "00:e0:fc": "Huawei",
    "00:16:3e": "Xen/KVM",
    "52:54:00": "QEMU",
    "00:15:5d": "Hyper-V",
    "00:1b:21": "Intel",
    "8c:8d:28": "Intel",
    "68:ec:c5": "Samsung",
    "94:35:0a": "Samsung",
    "b0:72:bf": "OnePlus",
    "94:65:2d": "Xiaomi",
    "28:6c:07": "Xiaomi",
    "14:91:82": "Arris/Comcast",
    "3c:b7:4b": "Comcast/Xfinity",
    "5e:7d:7d": "Xfinity",
    "e0:63:da": "Comcast/Xfinity",
    "e6:63:da": "Comcast/Xfinity",
    "ea:63:da": "Comcast/Xfinity",
    "ee:63:da": "Comcast/Xfinity",
}

# MCC+MNC to carrier name (US focused, offline)
MCC_MNC_TABLE = {
    "311480": "Verizon",
    "311270": "AT&T",
    "310260": "T-Mobile",
    "310410": "AT&T",
    "311490": "T-Mobile",
    "310120": "Sprint",
    "312250": "Dish",
    "310010": "Verizon",
    "311110": "Verizon",
    "311870": "Boost Mobile",
    "311220": "US Cellular",
    "310030": "AT&T",
    "310160": "T-Mobile",
    "311882": "Cricket",
    "310150": "AT&T",
}


def oui_lookup(mac: str) -> str:
    if not mac:
        return "Unknown"
    return OUI_TABLE.get(mac.lower()[:8], "Unknown")


def carrier_lookup(mcc: str, mnc: str) -> str:
    key = f"{mcc}{mnc}"
    return MCC_MNC_TABLE.get(key, f"MCC:{mcc} MNC:{mnc}" if mcc else "Unknown")


# ─────────────────────────────────────────────
# SCANNERS
# ─────────────────────────────────────────────

def run_termux_cmd(cmd: list):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return None


def scan_wifi() -> list[dict]:
    raw = run_termux_cmd(["termux-wifi-scaninfo"])
    if not raw or not isinstance(raw, list):
        return []

    results = []
    for ap in raw:
        ssid    = ap.get("ssid", "").strip() or "<hidden>"
        bssid   = ap.get("bssid", "")
        rssi    = ap.get("rssi", 0)           # Termux field name
        freq    = ap.get("frequency_mhz", 0)  # Termux field name
        channel = freq_to_channel(freq)
        caps    = ap.get("capabilities", "")
        flags   = parse_wifi_flags(caps)
        vendor  = oui_lookup(bssid)

        results.append({
            "source":     "wifi",
            "identifier": ssid,
            "address":    bssid,
            "rssi":       rssi,
            "frequency":  freq,
            "channel":    channel,
            "vendor":     vendor,
            "flags":      flags,
        })
    return results


def scan_cell() -> list[dict]:
    raw = run_termux_cmd(["termux-telephony-cellinfo"])
    if not raw or not isinstance(raw, list):
        return []

    results = []
    for cell in raw:
        ctype      = cell.get("type", "unknown").upper()
        registered = cell.get("registered", False)
        dbm        = cell.get("dbm", None) or cell.get("rssi", None) or 0
        mcc        = str(cell.get("mcc", "")).strip()
        mnc        = str(cell.get("mnc", "")).strip()
        lac        = cell.get("lac", "") or cell.get("tac", "")
        cid        = cell.get("cid", "") or cell.get("ci", "")
        pci        = cell.get("pci", "")
        earfcn     = cell.get("earfcn", "") or cell.get("arfcn", "")

        identifier = f"{ctype}-{cid}" if cid else f"{ctype}-unknown"
        carrier    = carrier_lookup(mcc, mnc)

        flags = []
        if registered:
            flags.append("REGISTERED")
        if pci:
            flags.append(f"PCI:{pci}")
        if lac:
            flags.append(f"LAC:{lac}")

        results.append({
            "source":     "cell",
            "identifier": identifier,
            "address":    f"MCC:{mcc} MNC:{mnc}",
            "rssi":       int(dbm),
            "frequency":  int(earfcn) if earfcn else None,
            "channel":    None,
            "vendor":     carrier,
            "flags":      flags,
        })
    return results


def freq_to_channel(freq: int) -> int:
    if 2412 <= freq <= 2484:
        return (freq - 2407) // 5
    if 5180 <= freq <= 5825:
        return (freq - 5000) // 5
    return 0


def parse_wifi_flags(caps: str) -> list[str]:
    flags = []
    if "WPA2" in caps:
        flags.append("WPA2")
    elif "WPA" in caps:
        flags.append("WPA")
    elif "WEP" in caps:
        flags.append("WEP")
    else:
        flags.append("OPEN")
    if "WPS" in caps:
        flags.append("WPS")
    if "ESS" in caps:
        flags.append("ESS")
    if "IBSS" in caps:
        flags.append("IBSS")
    return flags


# ─────────────────────────────────────────────
# DELTA ENGINE
# ─────────────────────────────────────────────

def compute_deltas(conn, new_signals: list[dict], source: str):
    now    = int(time.time())
    window = now - 120

    prev_ids = {
        row["identifier"] for row in conn.execute(
            "SELECT DISTINCT identifier FROM signals WHERE source=? AND ts>=?",
            (source, window)
        ).fetchall()
    }
    new_ids = {s["identifier"] for s in new_signals}
    deltas  = []

    for s in new_signals:
        if s["identifier"] not in prev_ids:
            ever = conn.execute(
                "SELECT 1 FROM signals WHERE source=? AND identifier=? AND ts<?",
                (source, s["identifier"], window)
            ).fetchone()
            status = "returning" if ever else "new"
            deltas.append((now, source, s["identifier"], s["address"], status, s["rssi"]))

    for ident in prev_ids - new_ids:
        row = conn.execute(
            "SELECT address, rssi FROM signals WHERE source=? AND identifier=? ORDER BY ts DESC LIMIT 1",
            (source, ident)
        ).fetchone()
        deltas.append((now, source, ident,
                       row["address"] if row else None,
                       "lost",
                       row["rssi"] if row else None))

    if deltas:
        conn.executemany(
            "INSERT INTO deltas (ts, source, identifier, address, status, rssi) VALUES (?,?,?,?,?,?)",
            deltas
        )


# ─────────────────────────────────────────────
# SCAN LOOP
# ─────────────────────────────────────────────

scan_lock = threading.Lock()
last_scan_summary = {"ts": 0, "wifi_count": 0, "cell_count": 0}


def do_scan():
    global last_scan_summary
    conn = get_conn()
    now  = int(time.time())

    for source, scanner in [("wifi", scan_wifi), ("cell", scan_cell)]:
        signals = scanner()
        if not signals:
            continue

        compute_deltas(conn, signals, source)

        scan_id = conn.execute(
            "INSERT INTO scans (ts, source) VALUES (?,?)", (now, source)
        ).lastrowid

        conn.executemany(
            """INSERT INTO signals
               (scan_id, ts, source, identifier, address, rssi, frequency, channel, vendor, flags)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [(scan_id, now, s["source"], s["identifier"], s["address"],
              s["rssi"], s["frequency"], s["channel"], s["vendor"],
              json.dumps(s["flags"])) for s in signals]
        )

    conn.commit()

    with scan_lock:
        wifi_now = conn.execute(
            "SELECT COUNT(*) as c FROM signals WHERE source='wifi' AND ts=?", (now,)
        ).fetchone()["c"]
        cell_now = conn.execute(
            "SELECT COUNT(*) as c FROM signals WHERE source='cell' AND ts=?", (now,)
        ).fetchone()["c"]
        last_scan_summary = {"ts": now, "wifi_count": wifi_now, "cell_count": cell_now}

    conn.close()


def scan_loop():
    while True:
        try:
            do_scan()
        except Exception as e:
            print(f"[CIVOPS] Scan error: {e}")
        time.sleep(SCAN_INTERVAL)


# ─────────────────────────────────────────────
# API HANDLERS
# ─────────────────────────────────────────────

def api_live(params: dict) -> dict:
    """Current signals — latest scan per source only (no duplicates)."""
    conn   = get_conn()
    now    = int(time.time())
    source = params.get("source", ["all"])[0]

    query = """SELECT s.* FROM signals s
        INNER JOIN (SELECT source, MAX(ts) as max_ts FROM scans GROUP BY source) latest
        ON s.source = latest.source AND s.ts = latest.max_ts"""
    args = []
    if source != "all":
        query += " WHERE s.source=?"
        args.append(source)
    query += " ORDER BY s.rssi DESC"

    rows = conn.execute(query, args).fetchall()
    conn.close()
    return {"ts": now, "signals": [dict(r) for r in rows], "summary": last_scan_summary}


def api_timeline(params: dict) -> dict:
    conn  = get_conn()
    mins  = int(params.get("mins", ["60"])[0])
    since = int(time.time()) - (mins * 60)
    rows  = conn.execute(
        "SELECT ts, source, COUNT(*) as count FROM signals WHERE ts>=? GROUP BY ts, source ORDER BY ts DESC",
        (since,)
    ).fetchall()
    conn.close()
    return {"timeline": [dict(r) for r in rows]}


def api_deltas(params: dict) -> dict:
    conn  = get_conn()
    mins  = int(params.get("mins", ["30"])[0])
    since = int(time.time()) - (mins * 60)
    rows  = conn.execute(
        "SELECT * FROM deltas WHERE ts>=? ORDER BY ts DESC LIMIT 200", (since,)
    ).fetchall()
    conn.close()
    return {"deltas": [dict(r) for r in rows]}


def api_signals(params: dict) -> dict:
    conn     = get_conn()
    mins     = int(params.get("mins", ["60"])[0])
    since    = int(time.time()) - (mins * 60)
    source   = params.get("source", ["all"])[0]
    min_rssi = int(params.get("min_rssi", ["-140"])[0])
    pattern  = params.get("pattern", [None])[0]

    query = "SELECT * FROM signals WHERE ts>=? AND rssi>=?"
    args  = [since, min_rssi]
    if source != "all":
        query += " AND source=?"
        args.append(source)
    if pattern:
        query += " AND identifier LIKE ?"
        args.append(f"%{pattern}%")
    query += " ORDER BY ts DESC LIMIT 500"

    rows = conn.execute(query, args).fetchall()
    conn.close()
    return {"signals": [dict(r) for r in rows]}


def api_debrief(params: dict) -> dict:
    conn        = get_conn()
    now         = int(time.time())
    window_mins = int(params.get("mins", ["30"])[0])
    since       = now - (window_mins * 60)

    wifi_count = conn.execute(
        "SELECT COUNT(DISTINCT identifier) as c FROM signals WHERE source='wifi' AND ts>=?", (since,)
    ).fetchone()["c"]
    cell_count = conn.execute(
        "SELECT COUNT(DISTINCT identifier) as c FROM signals WHERE source='cell' AND ts>=?", (since,)
    ).fetchone()["c"]

    top_wifi = conn.execute(
        """SELECT identifier, address, vendor, AVG(rssi) as avg_rssi, COUNT(*) as seen,
           MIN(ts) as first_ts, MAX(ts) as last_ts, GROUP_CONCAT(DISTINCT flags) as all_flags
           FROM signals WHERE source='wifi' AND ts>=?
           GROUP BY identifier ORDER BY seen DESC LIMIT 20""", (since,)
    ).fetchall()

    top_cell = conn.execute(
        """SELECT identifier, address, vendor, AVG(rssi) as avg_rssi, COUNT(*) as seen,
           MIN(ts) as first_ts, MAX(ts) as last_ts, GROUP_CONCAT(DISTINCT flags) as all_flags
           FROM signals WHERE source='cell' AND ts>=?
           GROUP BY identifier ORDER BY seen DESC LIMIT 10""", (since,)
    ).fetchall()

    deltas = conn.execute(
        "SELECT * FROM deltas WHERE ts>=? ORDER BY ts DESC", (since,)
    ).fetchall()
    conn.close()

    debrief = {
        "generated_at":   datetime.utcnow().isoformat() + "Z",
        "window_minutes": window_mins,
        "summary": {
            "unique_wifi_ssids":  wifi_count,
            "unique_cell_towers": cell_count,
            "total_delta_events": len(deltas),
        },
        "wifi_signals": [dict(r) for r in top_wifi],
        "cell_signals": [dict(r) for r in top_cell],
        "deltas":       [dict(r) for r in deltas],
    }
    return {"debrief": debrief, "prompt": build_llm_prompt(debrief)}


def build_llm_prompt(d: dict) -> str:
    lines = [
        "## CIVOPS Signal Environment Debrief",
        f"Generated: {d['generated_at']}",
        f"Window: Last {d['window_minutes']} minutes",
        "",
        "### Summary",
        f"- Unique Wi-Fi SSIDs:  {d['summary']['unique_wifi_ssids']}",
        f"- Unique Cell Towers:  {d['summary']['unique_cell_towers']}",
        f"- Delta Events:        {d['summary']['total_delta_events']}",
        "",
        "### Wi-Fi Signals",
    ]
    for s in d["wifi_signals"]:
        lines.append(
            f"- [{s['identifier']}] BSSID:{s['address']} Vendor:{s['vendor']} "
            f"AvgRSSI:{s['avg_rssi']:.0f}dBm Seen:{s['seen']}x"
        )
    lines += ["", "### Cell Towers"]
    for s in d["cell_signals"]:
        reg = "REGISTERED" in (s.get("all_flags") or "")
        lines.append(
            f"- [{s['identifier']}] Carrier:{s['vendor']} "
            f"AvgRSSI:{s['avg_rssi']:.0f}dBm Seen:{s['seen']}x"
            + (" [SERVING TOWER]" if reg else "")
        )
    lines += ["", "### Delta Events"]
    for ev in d["deltas"]:
        ts = datetime.utcfromtimestamp(ev["ts"]).strftime("%H:%M:%S")
        lines.append(
            f"- {ts} [{ev['status'].upper()}] {ev['source'].upper()} "
            f"{ev['identifier']} RSSI:{ev['rssi']}"
        )
    lines += [
        "", "---",
        "Analyze this signal environment. Consider:",
        "1. What patterns or anomalies do you notice?",
        "2. Are there signals suggesting unusual device density?",
        "3. What can you infer from the vendor and carrier distribution?",
        "4. Are there transient or suspicious signals worth noting?",
        "5. What does the cell tower picture suggest about this location?",
        "6. How has the environment changed during this window?",
    ]
    return "\n".join(lines)


def api_status(params: dict) -> dict:
    with scan_lock:
        return {
            "status":        "running",
            "scan_interval": SCAN_INTERVAL,
            "last_scan":     last_scan_summary,
            "db_path":       DB_PATH,
            "port":          SERVER_PORT,
        }


ROUTES = {
    "/api/live":     api_live,
    "/api/timeline": api_timeline,
    "/api/deltas":   api_deltas,
    "/api/signals":  api_signals,
    "/api/debrief":  api_debrief,
    "/api/status":   api_status,
}

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
MIME = {
    ".html": "text/html",
    ".js":   "application/javascript",
    ".css":  "text/css",
    ".json": "application/json",
    ".ico":  "image/x-icon",
}


class CivopsHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, data: dict, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path
        params = parse_qs(parsed.query)

        if path in ROUTES:
            try:
                self.send_json(ROUTES[path](params))
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
            return

        if path in ("/", ""):
            path = "/index.html"

        file_path = os.path.join(FRONTEND_DIR, path.lstrip("/"))
        if os.path.isfile(file_path):
            ext  = os.path.splitext(file_path)[1]
            mime = MIME.get(ext, "application/octet-stream")
            with open(file_path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_json({"error": "not found"}, 404)


if __name__ == "__main__":
    print("[CIVOPS] Initializing database...")
    init_db()
    print("[CIVOPS] Starting scanner daemon...")
    threading.Thread(target=scan_loop, daemon=True).start()
    print(f"[CIVOPS] Server running on http://127.0.0.1:{SERVER_PORT}")
    print(f"[CIVOPS] DB: {DB_PATH}")
    print(f"[CIVOPS] Scan interval: {SCAN_INTERVAL}s")
    try:
        HTTPServer(("127.0.0.1", SERVER_PORT), CivopsHandler).serve_forever()
    except KeyboardInterrupt:
        print("\n[CIVOPS] Shutting down.")
