#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json
import os
import queue
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, Response, jsonify, render_template_string, request, stream_with_context
import csv, io
from datetime import timedelta



_TRENDS_SCHEMA = """
CREATE TABLE IF NOT EXISTS disk_trends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, agent_id TEXT NOT NULL, hostname TEXT,
    disk_serial TEXT NOT NULL, disk_model TEXT,
    metric TEXT NOT NULL, value REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trends_lookup
    ON disk_trends(agent_id, disk_serial, metric, ts DESC);
"""
_TRACKED_METRICS = [
    ("temperature","temp",True),("reallocated","realloc",True),
    ("pending","pending",True),("uncorrectable","uncorr",True),
    ("media_errors","media_err",True),("percentage_used","wear_pct",True),
    ("available_spare","spare_pct",True),("power_on_hours","hours",True),
]

def _record_trend(conn, agent_id, hostname, disks):
    ts = _now()
    current_serials = set()
    for disk in disks:
        serial = disk.get("serial") or disk.get("model","unknown")
        model  = disk.get("model","")
        current_serials.add(serial)
        for field, key, skip_null in _TRACKED_METRICS:
            val = disk.get(field)
            if skip_null and val is None: continue
            try: fval = float(val)
            except (TypeError, ValueError): continue
            conn.execute(
                "INSERT INTO disk_trends(ts,agent_id,hostname,disk_serial,disk_model,metric,value)"
                " VALUES(?,?,?,?,?,?,?)",
                (ts,agent_id,hostname,serial,model,key,fval))
    if current_serials:
        placeholders = ",".join("?" * len(current_serials))
        conn.execute(
            "DELETE FROM disk_trends WHERE agent_id=? AND disk_serial NOT IN (%s)" % placeholders,
            [agent_id] + list(current_serials)
        )
    else:
        conn.execute("DELETE FROM disk_trends WHERE agent_id=?", (agent_id,))
def _cfg_get(c, key, default=None):
    row = c.execute("SELECT value FROM settings WHERE key=?",(key,)).fetchone()
    return row["value"] if row else default

def _agent_poll_interval(agent_id):
    with db() as c:
        row = c.execute("SELECT poll_seconds FROM agent_poll_intervals WHERE agent_id=?",(agent_id,)).fetchone()
        default = int(_cfg_get(c,"default_poll_seconds",30) or 30)
    return row["poll_seconds"] if row else default

def _cfg_get_all():
    with db() as c:
        rows = c.execute("SELECT key,value FROM settings ORDER BY key").fetchall()
    return {r["key"]:r["value"] for r in rows}

def _csv_resp(rows, fields, fname):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader(); w.writerows(rows)
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":"attachment; filename="+fname})

def _load_agents_full():
    cutoff = __import__('datetime').datetime.fromtimestamp(
        __import__('time').time()-180,
        tz=__import__('datetime').timezone.utc).isoformat(timespec="seconds")
    with db() as c:
        rows = c.execute("SELECT * FROM agents ORDER BY hostname").fetchall()
    out = []
    for r in rows:
        try:   disks = __import__('json').loads(r["disk_summary"]) if r["disk_summary"] else []
        except: disks = []
        out.append({**dict(r),"disks":disks,"online":(r["last_seen"] or "")>=cutoff,
                    "worst_status":_worst_status(disks)})
    return out


HOST                  = "0.0.0.0"
PORT                  = 8765
DB_PATH               = "diskhealth.db"
AGENT_DIR             = Path("agent_scripts")
AGENT_OFFLINE_SECONDS = 60

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

_db_lock = threading.Lock()


def _get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db():
    with _db_lock:
        conn = _get_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _existing_columns(c, table):
    rows = c.execute("PRAGMA table_info(%s)" % table).fetchall()
    return {r["name"] for r in rows}


_DEFAULT_AGENT = r"""# DiskHealth Agent v5.1.1 - minimal embedded default
param([string]$ServerUrl,[int]$PollInterval=30,[string]$AgentVersion="2.1.0",[string]$Title="DiskHealth Agent")
# Full script is served from /agent/agent.ps1 after first connection
Write-Host "DiskHealth Agent stub - please use the full installer"
"""

_DEFAULT_TRAY = r"""# DiskHealth Tray v5.1.1 - stub
# Full script served from /agent/tray.ps1
"""

_DEFAULT_INSTALLER = r"""# DiskHealth Installer v2.1
param([Parameter(Mandatory=$true)][string]$ServerUrl,[int]$PollInterval=30)
$InstallDir="$env:ProgramFiles\DiskHealthAgent"
New-Item -ItemType Directory -Force -Path $InstallDir|Out-Null
Invoke-WebRequest -UseBasicParsing -Uri "$ServerUrl/agent/agent.ps1" -OutFile "$InstallDir\DiskHealthAgent.ps1"
Invoke-WebRequest -UseBasicParsing -Uri "$ServerUrl/agent/tray.ps1"  -OutFile "$InstallDir\DiskHealthTray.ps1"
Set-Content -Path "$InstallDir\server_url.txt" -Value $ServerUrl -Encoding UTF8
$action=New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NonInteractive -ExecutionPolicy Bypass -File `"$InstallDir\DiskHealthAgent.ps1`" -ServerUrl `"$ServerUrl`" -PollInterval $PollInterval"
$trigger=New-ScheduledTaskTrigger -AtStartup
Register-ScheduledTask -TaskName "DiskHealthAgent" -Action $action -Trigger $trigger -RunLevel Highest -Force|Out-Null
Start-ScheduledTask -TaskName "DiskHealthAgent"
Write-Host "Installed. Dashboard: $ServerUrl"
"""


def _init_scripts():
    files = {
        "DiskHealthAgent.ps1": _DEFAULT_AGENT,
        "DiskHealthTray.ps1":  _DEFAULT_TRAY,
        "install-agent.ps1":   _DEFAULT_INSTALLER,
    }
    for filename, content in files.items():
        path = AGENT_DIR / filename
        if not path.exists():
            try:
                path.write_bytes(content.encode("utf-8-sig"))
                print("  [init] Wrote default %s" % filename)
            except Exception as e:
                print("  [warn] Could not write %s: %s" % (filename, e))



_SETTINGS_DEFAULTS = {
    "offline_threshold_seconds":"180","auto_deregister_days":"0",
    "default_poll_seconds":"30",
    "thresh_temp_warn":"45","thresh_temp_crit":"60",
    "thresh_realloc_warn":"1","thresh_realloc_crit":"5",
    "thresh_pending_warn":"1","thresh_pending_crit":"5",
    "thresh_uncorr_warn":"1","thresh_uncorr_crit":"1",
    "thresh_wear_warn":"75","thresh_wear_crit":"90",
    "thresh_spare_warn":"20","thresh_spare_crit":"10","thresh_vol_warn":"80","thresh_vol_crit":"90",
}

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS agents (
            agent_id TEXT PRIMARY KEY, hostname TEXT, ip TEXT,
            os_version TEXT, agent_version TEXT, logged_users TEXT,
            welcome_title TEXT, first_seen TEXT, last_seen TEXT,
            last_report TEXT, disk_summary TEXT
        );
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL, received_at TEXT NOT NULL,
            hostname TEXT, ip TEXT, logged_users TEXT,
            disk_data TEXT, command_id TEXT
        );
        CREATE TABLE IF NOT EXISTS commands (
            command_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL,
            action TEXT NOT NULL, queued_at TEXT NOT NULL,
            acked_at TEXT, result TEXT
        );
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL, hostname TEXT,
            severity TEXT NOT NULL, message TEXT NOT NULL,
            disk_serial TEXT, created_at TEXT NOT NULL,
            dismissed INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL, agent_id TEXT, hostname TEXT,
            event_type TEXT NOT NULL, detail TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_reports_agent ON reports(agent_id);
        CREATE INDEX IF NOT EXISTS idx_commands_agent ON commands(agent_id);
        CREATE INDEX IF NOT EXISTS idx_alerts_agent   ON alerts(agent_id);
        CREATE INDEX IF NOT EXISTS idx_activity_ts    ON activity_log(ts DESC);
        """)
        migrations = [
            ("agents","welcome_title","TEXT"),("agents","disk_summary","TEXT"),
            ("agents","last_report","TEXT"),("commands","result","TEXT"),
            ("commands","acked_at","TEXT"),("alerts","dismissed","INTEGER NOT NULL DEFAULT 0"),
            ("alerts","severity","TEXT"),("alerts","message","TEXT"),
            ("alerts","created_at","TEXT"),("alerts","disk_serial","TEXT"),
            ("alerts","hostname","TEXT"),("reports","command_id","TEXT"),
            ("activity_log","agent_id","TEXT"),("activity_log","hostname","TEXT"),
            ("activity_log","detail","TEXT"),
        ]
        for table, col, defn in migrations:
            if col not in _existing_columns(c, table):
                c.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, defn))
        c.executescript(_TRENDS_SCHEMA)
        c.executescript("""
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_poll_intervals (
    agent_id TEXT PRIMARY KEY, poll_seconds INTEGER NOT NULL DEFAULT 30);
""")

        c.executescript("""
        CREATE TABLE IF NOT EXISTS agent_meta (
            agent_id TEXT PRIMARY KEY, display_name TEXT,
            notes TEXT, location TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS agent_uptime (
            agent_id TEXT PRIMARY KEY,
            online_since TEXT,
            last_offline TEXT
        );
        CREATE TABLE IF NOT EXISTS disk_replacements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            disk_serial TEXT NOT NULL,
            disk_model TEXT,
            status TEXT NOT NULL DEFAULT 'scheduled',
            note TEXT,
            scheduled_date TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            UNIQUE(agent_id, disk_serial)
        );
        """)
        for k,v in _SETTINGS_DEFAULTS.items():
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",(k,v))
        try:
            c.executescript("""
                DELETE FROM disk_trends WHERE rowid IN (
                    SELECT dt.rowid FROM disk_trends dt
                    WHERE NOT EXISTS (
                        SELECT 1 FROM agents a
                        WHERE a.agent_id = dt.agent_id
                        AND a.disk_summary LIKE '%' || dt.disk_serial || '%'
                    )
                );
            """)
        except: pass
        try:
            c.executescript("""
                DELETE FROM disk_trends WHERE rowid IN (
                    SELECT dt.rowid FROM disk_trends dt
                    WHERE NOT EXISTS (
                        SELECT 1 FROM agents a
                        WHERE a.agent_id = dt.agent_id
                        AND a.disk_summary LIKE '%' || dt.disk_serial || '%'
                    )
                );
            """)
        except: pass


class _SSEBroker:
    def __init__(self):
        self._clients = []
        self._lock = threading.Lock()
    def subscribe(self):
        q = queue.Queue(maxsize=64)
        with self._lock: self._clients.append(q)
        return q
    def unsubscribe(self, q):
        with self._lock:
            try: self._clients.remove(q)
            except ValueError: pass
    def publish(self, event, data):
        payload = "event: %s\ndata: %s\n\n" % (event, json.dumps(data))
        with self._lock:
            dead = []
            for q in self._clients:
                try: q.put_nowait(payload)
                except queue.Full: dead.append(q)
            for q in dead: self._clients.remove(q)

broker = _SSEBroker()

def _log_activity(c, agent_id, hostname, event_type, detail=""):
    c.execute("INSERT INTO activity_log(ts,agent_id,hostname,event_type,detail) VALUES(?,?,?,?,?)",
              (_now(), agent_id, hostname, event_type, detail))

def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def _worst_status(disks):
    order = {"Critical":0,"Warning":1,"Unknown":2,"Healthy":3}
    if not disks: return "Unknown"
    return min((d.get("smart_status","Unknown") for d in disks), key=lambda s:order.get(s,2))

def _check_alerts(c, agent_id, hostname, disks):
    """Check disk metrics against configurable thresholds and raise alerts."""
    def _thr(key, default):
        try:
            row = c.execute("SELECT value FROM settings WHERE key=?",(key,)).fetchone()
            return float(row["value"]) if row else float(default)
        except: return float(default)

    t_temp_warn  = _thr("thresh_temp_warn",  45)
    t_temp_crit  = _thr("thresh_temp_crit",  60)
    t_real_warn  = _thr("thresh_realloc_warn", 1)
    t_real_crit  = _thr("thresh_realloc_crit", 5)
    t_pend_warn  = _thr("thresh_pending_warn", 1)
    t_pend_crit  = _thr("thresh_pending_crit", 5)
    t_uncr_warn  = _thr("thresh_uncorr_warn",  1)
    t_uncr_crit  = _thr("thresh_uncorr_crit",  1)
    t_wear_warn  = _thr("thresh_wear_warn",   75)
    t_wear_crit  = _thr("thresh_wear_crit",   90)
    t_spar_warn  = _thr("thresh_spare_warn",  20)
    t_spar_crit  = _thr("thresh_spare_crit",  10)

    def _alert(agent_id, hostname, sev, msg, serial):
        existing = c.execute(
            "SELECT id FROM alerts WHERE agent_id=? AND disk_serial=? AND severity=? AND dismissed=0",
            (agent_id, serial, sev)).fetchone()
        if not existing:
            c.execute(
                "INSERT INTO alerts(agent_id,hostname,severity,message,disk_serial,created_at) VALUES(?,?,?,?,?,?)",
                (agent_id, hostname, sev, msg, serial, _now()))
            broker.publish("alert", {"agent_id":agent_id,"hostname":hostname,"severity":sev,"message":msg})
            _log_activity(c, agent_id, hostname, "alert", msg)

    for disk in disks:
        serial = disk.get("serial") or disk.get("model","?")
        model  = disk.get("model","?")
        issues_crit = []
        issues_warn = []

        temp = disk.get("temperature")
        if temp is not None:
            if temp >= t_temp_crit:
                issues_crit.append("temperature %d°C (>= %d°C)" % (temp, int(t_temp_crit)))
            elif temp >= t_temp_warn:
                issues_warn.append("temperature %d°C (>= %d°C)" % (temp, int(t_temp_warn)))

        real = disk.get("reallocated")
        if real is not None:
            if real >= t_real_crit:
                issues_crit.append("%d reallocated sectors" % real)
            elif real >= t_real_warn:
                issues_warn.append("%d reallocated sectors" % real)

        pend = disk.get("pending")
        if pend is not None:
            if pend >= t_pend_crit:
                issues_crit.append("%d pending sectors" % pend)
            elif pend >= t_pend_warn:
                issues_warn.append("%d pending sectors" % pend)

        uncr = disk.get("uncorrectable")
        if uncr is not None:
            if uncr >= t_uncr_crit:
                issues_crit.append("%d uncorrectable errors" % uncr)
            elif uncr >= t_uncr_warn:
                issues_warn.append("%d uncorrectable errors" % uncr)

        wear = disk.get("percentage_used")
        if wear is not None:
            if wear >= t_wear_crit:
                issues_crit.append("SSD wear %d%% (>= %d%%)" % (wear, int(t_wear_crit)))
            elif wear >= t_wear_warn:
                issues_warn.append("SSD wear %d%% (>= %d%%)" % (wear, int(t_wear_warn)))

        spare = disk.get("available_spare")
        if spare is not None:
            if spare <= t_spar_crit:
                issues_crit.append("spare capacity %d%% (<= %d%%)" % (spare, int(t_spar_crit)))
            elif spare <= t_spar_warn:
                issues_warn.append("spare capacity %d%% (<= %d%%)" % (spare, int(t_spar_warn)))

        merr = disk.get("media_errors")
        if merr:
            issues_warn.append("%d media errors" % merr)

        smart_status = disk.get("smart_status","Unknown")

        if issues_crit or smart_status == "Critical":
            detail = ", ".join(issues_crit) if issues_crit else "SMART failure predicted"
            msg = "Disk '%s' (S/N: %s) on %s — CRITICAL: %s" % (model, serial, hostname, detail)
            _alert(agent_id, hostname, "critical", msg, serial)

        elif issues_warn or smart_status == "Warning":
            detail = ", ".join(issues_warn) if issues_warn else "degraded SMART status"
            msg = "Disk '%s' (S/N: %s) on %s — WARNING: %s" % (model, serial, hostname, detail)
            _alert(agent_id, hostname, "warning", msg, serial)

        else:
            if smart_status in ("Healthy", "Unknown"):
                c.execute(
                    "UPDATE alerts SET dismissed=1 WHERE agent_id=? AND disk_serial=? AND dismissed=0",
                    (agent_id, serial))

def _offline_watchdog():
    notified = set()
    while True:
        time.sleep(10)
        try:
            _thr = AGENT_OFFLINE_SECONDS
            try:
                with db() as _c:
                    _row = _c.execute("SELECT value FROM settings WHERE key='offline_threshold_seconds'").fetchone()
                    if _row: _thr = int(_row["value"])
            except: pass
            cutoff_iso = datetime.fromtimestamp(time.time()-_thr,tz=timezone.utc).isoformat(timespec="seconds")
            with db() as c:
                rows = c.execute("SELECT agent_id,hostname,last_seen FROM agents WHERE last_seen<?", (cutoff_iso,)).fetchall()
            for row in rows:
                aid = row["agent_id"]
                if aid not in notified:
                    notified.add(aid)
                    broker.publish("offline",{"agent_id":aid,"hostname":row["hostname"],"last_seen":row["last_seen"]})
                    try:
                        with db() as _uc: _uc.execute("UPDATE agent_uptime SET last_offline=? WHERE agent_id=?",(row["last_seen"],aid))
                    except: pass
            back = notified - {r["agent_id"] for r in rows}
            notified -= back
            for aid in back: broker.publish("online",{"agent_id":aid})
        except Exception: pass

def _auto_deregister_loop():
    while True:
        time.sleep(3600)
        try:
            days = 0
            try:
                with db() as _c:
                    _row = _c.execute(
                        "SELECT value FROM settings WHERE key='auto_deregister_days'"
                    ).fetchone()
                    if _row:
                        days = int(_row["value"])
            except: pass

            if days <= 0:
                continue  
            cutoff = datetime.fromtimestamp(
                time.time() - days * 86400,
                tz=timezone.utc
            ).isoformat(timespec="seconds")

            with db() as c:
                stale = c.execute(
                    "SELECT agent_id, hostname FROM agents WHERE last_seen < ?",
                    (cutoff,)
                ).fetchall()

                for row in stale:
                    aid = row["agent_id"]
                    hn  = row["hostname"] or aid
                    for tbl in ("agents","reports","commands","alerts",
                                "disk_trends","agent_poll_intervals"):
                        try:
                            c.execute("DELETE FROM %s WHERE agent_id=?" % tbl, (aid,))
                        except: pass
                    _log_activity(c, aid, hn, "deregister",
                                  "Auto-deregistered after %d days offline" % days)
                    broker.publish("agent_removed", {"agent_id":aid,"hostname":hn})
                    print("[auto-deregister] Removed stale agent: %s (%s)" % (hn, aid))

        except Exception as e:
            print("[auto-deregister] Error: %s" % e)


def _dead_agent_enforcer():

    while True:
        time.sleep(30)
        try:
            threshold = 120  
            cutoff = datetime.fromtimestamp(
                time.time() - threshold,
                tz=timezone.utc
            ).isoformat(timespec="seconds")

            with db() as c:
                rows = c.execute(
                    "SELECT agent_id, hostname, last_seen FROM agents WHERE last_seen < ?",
                    (cutoff,)
                ).fetchall()

                for row in rows:
                    aid      = row["agent_id"]
                    hostname = row["hostname"] or aid

                    existing = c.execute(
                        "SELECT 1 FROM commands WHERE agent_id=? AND action='restart_agent' AND acked_at IS NULL",
                        (aid,)
                    ).fetchone()

                    if not existing:
                        cmd_id = str(uuid.uuid4())
                        c.execute(
                            "INSERT INTO commands(command_id,agent_id,action,queued_at) VALUES(?,?,?,?)",
                            (cmd_id, aid, "restart_agent", _now())
                        )
                        print("[dead-agent-enforcer] queued restart for %s (%s)" % (hostname, aid))
                        broker.publish("command", {
                            "command_id": cmd_id,
                            "agent_id":   aid,
                            "hostname":   hostname,
                            "action":     "restart_agent"
                        })

        except Exception as e:
            print("[dead-agent-enforcer] error: %s" % e)


def _start_background_threads():
    threading.Thread(target=_auto_deregister_loop, daemon=True, name='auto-deregister').start()
    threading.Thread(target=_offline_watchdog,      daemon=True, name="offline-watchdog").start()
    threading.Thread(target=_dead_agent_enforcer,   daemon=True, name="dead-agent-enforcer").start()



@app.route("/health")
def health():
    return jsonify({"status":"ok","ts":_now()})

@app.route("/api/register", methods=["POST"])
def api_register():
    data = request.get_json(force=True, silent=True) or {}
    agent_id = data.get("agent_id","")
    if not agent_id: return jsonify({"error":"agent_id required"}),400
    hostname = data.get("hostname","unknown")
    now = _now()
    is_new = was_offline = False
    with db() as c:
        existing = c.execute("SELECT agent_id,last_seen FROM agents WHERE agent_id=?", (agent_id,)).fetchone()
        if existing:
            _thr2 = AGENT_OFFLINE_SECONDS
            try:
                _row2 = c.execute("SELECT value FROM settings WHERE key='offline_threshold_seconds'").fetchone()
                if _row2: _thr2 = int(_row2["value"])
            except: pass
            cutoff = datetime.fromtimestamp(time.time()-_thr2,tz=timezone.utc).isoformat(timespec="seconds")
            was_offline = (existing["last_seen"] or "") < cutoff
            c.execute("UPDATE agents SET hostname=?,ip=?,os_version=?,agent_version=?,logged_users=?,welcome_title=?,last_seen=? WHERE agent_id=?",
                      (hostname,data.get("ip",""),data.get("os_version",""),data.get("agent_version",""),
                       data.get("logged_users",""),data.get("welcome_title",""),now,agent_id))
        else:
            is_new = True
            c.execute("INSERT INTO agents(agent_id,hostname,ip,os_version,agent_version,logged_users,welcome_title,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?,?)",
                      (agent_id,hostname,data.get("ip",""),data.get("os_version",""),data.get("agent_version",""),
                       data.get("logged_users",""),data.get("welcome_title",""),now,now))
            _log_activity(c, agent_id, hostname, "register", "New agent from %s" % data.get("ip","?"))
    if is_new:
        broker.publish("register",{"agent_id":agent_id,"hostname":hostname,"ts":now})
        with db() as _uc: _uc.execute("INSERT OR REPLACE INTO agent_uptime(agent_id,online_since,last_offline) VALUES(?,?,NULL)",(agent_id,now))
    elif was_offline:
        broker.publish("online",{"agent_id":agent_id,"hostname":hostname,"ts":now})
        with db() as _uc: _uc.execute(
    "INSERT INTO agent_uptime(agent_id,online_since,last_offline) VALUES(?,?,NULL)"
    " ON CONFLICT(agent_id) DO UPDATE SET online_since=excluded.online_since",
    (agent_id, now))
    return jsonify({"status":"ok","agent_id":agent_id})

@app.route("/api/report", methods=["POST"])
def api_report():
    data = request.get_json(force=True, silent=True) or {}
    agent_id = data.get("agent_id","")
    if not agent_id: return jsonify({"error":"agent_id required"}),400
    hostname = data.get("hostname","unknown")
    disks    = data.get("disks",[])
    now      = _now()
    disk_json = json.dumps(disks)
    worst = _worst_status(disks)
    with db() as c:
        c.execute("INSERT INTO reports(agent_id,received_at,hostname,ip,logged_users,disk_data,command_id) VALUES(?,?,?,?,?,?,?)",
                  (agent_id,now,hostname,data.get("ip",""),data.get("logged_users",""),disk_json,data.get("command_id","")))
        c.execute("UPDATE agents SET last_seen=?,last_report=?,hostname=?,ip=?,logged_users=?,agent_version=?,disk_summary=? WHERE agent_id=?",
                  (now,now,hostname,data.get("ip",""),data.get("logged_users",""),data.get("agent_version",""),disk_json,agent_id))
        c.execute("DELETE FROM reports WHERE agent_id=? AND id NOT IN (SELECT id FROM reports WHERE agent_id=? ORDER BY id DESC LIMIT 100)",(agent_id,agent_id))
        _check_alerts(c, agent_id, hostname, disks)
        _log_activity(c, agent_id, hostname, "report", "%d disk(s) - %s" % (len(disks),worst))
        _record_trend(c, agent_id, hostname, disks)
    broker.publish("report",{"agent_id":agent_id,"hostname":hostname,"disk_count":len(disks),"worst_status":worst,"ts":now})
    return jsonify({"status":"ok"})

@app.route("/api/commands/<agent_id>")
def api_get_commands(agent_id):
    now = _now()
    with db() as c:
        c.execute("""INSERT INTO agents(agent_id,hostname,ip,os_version,agent_version,logged_users,welcome_title,first_seen,last_seen)
                     VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(agent_id) DO UPDATE SET last_seen=excluded.last_seen""",
                  (agent_id,"(pending)","","","","","",now,now))
        rows = c.execute("SELECT command_id,action FROM commands WHERE agent_id=? AND acked_at IS NULL",(agent_id,)).fetchall()
    return jsonify({"commands":[{"command_id":r["command_id"],"action":r["action"]} for r in rows],"poll_interval":_agent_poll_interval(agent_id)})

@app.route("/api/commands-all")
def api_commands_all():
    with db() as c:
        rows = c.execute("SELECT c.command_id,c.agent_id,c.action,c.queued_at,c.acked_at,c.result,a.hostname FROM commands c LEFT JOIN agents a ON a.agent_id=c.agent_id ORDER BY c.queued_at DESC LIMIT 300").fetchall()
    return jsonify({"commands":[dict(r) for r in rows]})

@app.route("/api/cmd/delete/<command_id>", methods=["DELETE"])
def api_delete_command(command_id):
    with db() as c:
        c.execute("DELETE FROM commands WHERE command_id=?", (command_id,))
    return jsonify({"status":"ok"})

@app.route("/api/cmd/clear-all", methods=["DELETE"])
def api_clear_all_commands():
    with db() as c:
        c.execute("DELETE FROM commands")
    return jsonify({"status":"ok"})

@app.route("/api/ack", methods=["POST"])
def api_ack():
    data = request.get_json(force=True, silent=True) or {}
    command_id = data.get("command_id","")
    result     = data.get("result",{})
    if not command_id: return jsonify({"error":"command_id required"}),400
    now = _now()
    with db() as c:
        row = c.execute("SELECT agent_id,action FROM commands WHERE command_id=?",(command_id,)).fetchone()
        if row:
            c.execute("UPDATE commands SET acked_at=?,result=? WHERE command_id=?",(now,json.dumps(result),command_id))
            ar = c.execute("SELECT hostname FROM agents WHERE agent_id=?",(row["agent_id"],)).fetchone()
            hostname = ar["hostname"] if ar else "?"
            _log_activity(c,row["agent_id"],hostname,"ack","'%s' ack'd" % row["action"])
            broker.publish("ack",{"command_id":command_id,"agent_id":row["agent_id"],"hostname":hostname,"action":row["action"],"result":result,"ts":now})
    return jsonify({"status":"ok"})

@app.route("/agent/agent.ps1")
def serve_agent_script():
    f = AGENT_DIR / "DiskHealthAgent.ps1"
    if f.exists(): return Response(f.read_text(encoding="utf-8"), mimetype="text/plain")
    return Response("# not found",mimetype="text/plain"),404

@app.route("/agent/tray.ps1")
def serve_tray_script():
    f = AGENT_DIR / "DiskHealthTray.ps1"
    if f.exists(): return Response(f.read_text(encoding="utf-8"), mimetype="text/plain")
    return Response("# not found",mimetype="text/plain"),404



@app.route("/api/agents")
def api_agents():
    _thr = AGENT_OFFLINE_SECONDS
    try:
        with db() as _c:
            _row = _c.execute("SELECT value FROM settings WHERE key='offline_threshold_seconds'").fetchone()
            if _row: _thr = int(_row["value"])
    except: pass
    cutoff = datetime.fromtimestamp(time.time()-_thr,tz=timezone.utc).isoformat(timespec="seconds")
    try:
        with db() as c:
            rows = c.execute("""SELECT a.agent_id,a.hostname,a.ip,a.os_version,a.agent_version,
                a.logged_users,a.welcome_title,a.first_seen,a.last_seen,a.last_report,a.disk_summary,
                COALESCE((SELECT COUNT(*) FROM alerts WHERE agent_id=a.agent_id AND dismissed=0 AND severity='critical'),0) as crit_alerts,
                COALESCE((SELECT COUNT(*) FROM alerts WHERE agent_id=a.agent_id AND dismissed=0 AND severity='warning'),0) as warn_alerts
                FROM agents a ORDER BY a.last_seen DESC""").fetchall()
        agents = []
        for r in rows:
            try: disks = json.loads(r["disk_summary"]) if r["disk_summary"] else []
            except: disks = []
            agents.append({
                "agent_id":r["agent_id"],"hostname":r["hostname"] or "(unknown)","ip":r["ip"] or "",
                "os_version":r["os_version"] or "","agent_version":r["agent_version"] or "",
                "logged_users":r["logged_users"] or "","welcome_title":r["welcome_title"] or "",
                "first_seen":r["first_seen"],"last_seen":r["last_seen"],"last_report":r["last_report"],
                "online":(r["last_seen"] or "")>=cutoff,"worst_status":_worst_status(disks),
                "disk_count":len(disks),"disks":disks,
                "crit_alerts":r["crit_alerts"] or 0,"warn_alerts":r["warn_alerts"] or 0,
            })
        return jsonify(agents)
    except Exception as ex:
        import traceback; traceback.print_exc()
        return jsonify({"error":str(ex)}),500

@app.route("/api/agents/<agent_id>")
def api_agent_detail(agent_id):
    _thr = AGENT_OFFLINE_SECONDS
    try:
        with db() as _c:
            _row = _c.execute("SELECT value FROM settings WHERE key='offline_threshold_seconds'").fetchone()
            if _row: _thr = int(_row["value"])
    except: pass
    cutoff = datetime.fromtimestamp(time.time()-_thr,tz=timezone.utc).isoformat(timespec="seconds")
    with db() as c:
        r = c.execute("SELECT * FROM agents WHERE agent_id=?",(agent_id,)).fetchone()
        if not r: return jsonify({"error":"not found"}),404
        disks   = json.loads(r["disk_summary"]) if r["disk_summary"] else []
        alerts  = c.execute("SELECT * FROM alerts WHERE agent_id=? AND dismissed=0 ORDER BY created_at DESC",(agent_id,)).fetchall()
        pending = c.execute("SELECT * FROM commands WHERE agent_id=? AND acked_at IS NULL ORDER BY queued_at DESC",(agent_id,)).fetchall()
    return jsonify({
        "agent_id":r["agent_id"],"hostname":r["hostname"],"ip":r["ip"],
        "os_version":r["os_version"],"agent_version":r["agent_version"],
        "logged_users":r["logged_users"],"welcome_title":r["welcome_title"],
        "first_seen":r["first_seen"],"last_seen":r["last_seen"],"last_report":r["last_report"],
        "online":(r["last_seen"] or "")>=cutoff,"worst_status":_worst_status(disks),
        "disks":disks,"alerts":[dict(a) for a in alerts],"pending_commands":[dict(p) for p in pending],
    })

@app.route("/api/agents/<agent_id>/history")
def api_agent_history(agent_id):
    with db() as c:
        rows = c.execute("SELECT id,received_at,hostname,ip,logged_users,disk_data FROM reports WHERE agent_id=? ORDER BY id DESC LIMIT 50",(agent_id,)).fetchall()
    history = []
    for r in rows:
        disks = json.loads(r["disk_data"]) if r["disk_data"] else []
        history.append({"id":r["id"],"received_at":r["received_at"],"hostname":r["hostname"],"ip":r["ip"],
                        "logged_users":r["logged_users"],"worst_status":_worst_status(disks),"disk_count":len(disks)})
    return jsonify(history)

@app.route("/api/agents/<agent_id>/history", methods=["DELETE"])
def api_delete_history(agent_id):
    with db() as c: c.execute("DELETE FROM reports WHERE agent_id=?",(agent_id,))
    return jsonify({"status":"ok"})

@app.route("/api/alerts")
def api_alerts():
    with db() as c:
        rows = c.execute("SELECT * FROM alerts WHERE dismissed=0 ORDER BY created_at DESC LIMIT 100").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/agents/<agent_id>/dismiss", methods=["POST"])
def api_dismiss_alert(agent_id):
    data = request.get_json(force=True,silent=True) or {}
    alert_id = data.get("alert_id")
    with db() as c:
        if alert_id: c.execute("UPDATE alerts SET dismissed=1 WHERE id=? AND agent_id=?",(alert_id,agent_id))
        else:        c.execute("UPDATE alerts SET dismissed=1 WHERE agent_id=?",(agent_id,))
    return jsonify({"status":"ok"})

@app.route("/api/alerts/dismiss_all", methods=["POST"])
def api_dismiss_all_alerts():
    with db() as c: c.execute("UPDATE alerts SET dismissed=1 WHERE dismissed=0")
    return jsonify({"status":"ok"})

@app.route("/api/command", methods=["POST"])
def api_queue_command():
    data = request.get_json(force=True,silent=True) or {}
    agent_id = data.get("agent_id","")
    action   = data.get("action","")
    if not agent_id or not action: return jsonify({"error":"agent_id and action required"}),400
    valid = {"get_disk_health","ping","update_agent","clear_log","restart_agent"}
    if action not in valid: return jsonify({"error":"unknown action"}),400
    command_id = str(uuid.uuid4())
    now = _now()
    with db() as c:
        row = c.execute("SELECT hostname FROM agents WHERE agent_id=?",(agent_id,)).fetchone()
        if not row: return jsonify({"error":"agent not found"}),404
        hostname = row["hostname"]
        c.execute("INSERT INTO commands(command_id,agent_id,action,queued_at) VALUES(?,?,?,?)",(command_id,agent_id,action,now))
        _log_activity(c,agent_id,hostname,"command","'%s' queued" % action)
    broker.publish("command",{"command_id":command_id,"agent_id":agent_id,"hostname":hostname,"action":action,"ts":now})
    return jsonify({"status":"ok","command_id":command_id})

@app.route("/api/agents/<agent_id>", methods=["DELETE"])
def api_delete_agent(agent_id):
    with db() as c:
        row = c.execute("SELECT hostname FROM agents WHERE agent_id=?",(agent_id,)).fetchone()
        hostname = row["hostname"] if row else agent_id
        for tbl in ("agents","reports","commands","alerts"):
            c.execute("DELETE FROM %s WHERE agent_id=?" % tbl,(agent_id,))
    broker.publish("agent_removed",{"agent_id":agent_id,"hostname":hostname})
    return jsonify({"status":"ok"})

@app.route("/api/activity", methods=["DELETE"])
def api_clear_activity():
    with db() as c: c.execute("DELETE FROM activity_log")
    return jsonify({"status":"ok"})

@app.route("/api/stats")
def api_stats():
    _thr = AGENT_OFFLINE_SECONDS
    try:
        with db() as _c:
            _row = _c.execute("SELECT value FROM settings WHERE key='offline_threshold_seconds'").fetchone()
            if _row: _thr = int(_row["value"])
    except: pass
    cutoff  = datetime.fromtimestamp(time.time()-_thr,tz=timezone.utc).isoformat(timespec="seconds")
    day_ago = datetime.fromtimestamp(time.time()-86400,tz=timezone.utc).isoformat(timespec="seconds")
    with db() as c:
        total    = c.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
        online   = c.execute("SELECT COUNT(*) FROM agents WHERE last_seen>=?",(cutoff,)).fetchone()[0]
        crits    = c.execute("SELECT COUNT(*) FROM alerts WHERE dismissed=0 AND severity='critical'").fetchone()[0] or 0
        warns    = c.execute("SELECT COUNT(*) FROM alerts WHERE dismissed=0 AND severity='warning'").fetchone()[0] or 0
        reps24   = c.execute("SELECT COUNT(*) FROM reports WHERE received_at>=?",(day_ago,)).fetchone()[0]
        activity = c.execute("SELECT ts,hostname,event_type,detail FROM activity_log ORDER BY id DESC LIMIT 40").fetchall()
        rows     = c.execute("SELECT disk_summary FROM agents").fetchall()
    sc = {"Healthy":0,"Warning":0,"Critical":0,"Unknown":0}
    total_disks = 0; total_gb = 0
    for r in rows:
        disks = json.loads(r["disk_summary"]) if r["disk_summary"] else []
        total_disks += len(disks)
        total_gb += sum(d.get("size_gb",0) or 0 for d in disks)
        ws = _worst_status(disks); sc[ws] = sc.get(ws,0)+1
    return jsonify({"total_agents":total,"online_agents":online,"offline_agents":total-online,
                    "critical_alerts":crits,"warning_alerts":warns,"reports_24h":reps24,
                    "total_disks":total_disks,"total_tb":round(total_gb/1024,2),"agent_status_breakdown":sc,
                    "activity":[{"ts":a["ts"],"hostname":a["hostname"],"event_type":a["event_type"],"detail":a["detail"]} for a in activity]})

def _read_ps1(filename):
    try: return (AGENT_DIR/filename).read_text(encoding="utf-8")
    except: return ""

def _write_ps1(filename, content):
    (AGENT_DIR/filename).write_bytes(content.encode("utf-8-sig"))

@app.route("/api/scripts", methods=["GET"])
def api_get_scripts():
    return jsonify({"agent":_read_ps1("DiskHealthAgent.ps1"),"installer":_read_ps1("install-agent.ps1"),"tray":_read_ps1("DiskHealthTray.ps1")})

@app.route("/api/scripts", methods=["POST"])
def api_save_scripts():
    data = request.get_json(force=True,silent=True) or {}
    saved = []
    if "agent" in data:     _write_ps1("DiskHealthAgent.ps1",data["agent"]);   saved.append("agent")
    if "installer" in data: _write_ps1("install-agent.ps1",data["installer"]); saved.append("installer")
    if "tray" in data:      _write_ps1("DiskHealthTray.ps1",data["tray"]);     saved.append("tray")
    if not saved: return jsonify({"error":"no scripts provided"}),400
    return jsonify({"status":"saved","saved":saved})

@app.route("/download/agent")
def download_agent():
    f = AGENT_DIR/"DiskHealthAgent.ps1"
    if not f.exists(): return Response("# not found",mimetype="text/plain"),404
    return Response(f.read_bytes(),mimetype="application/octet-stream",headers={"Content-Disposition":"attachment; filename=DiskHealthAgent.ps1"})

@app.route("/download/installer")
def download_installer():
    f = AGENT_DIR/"install-agent.ps1"
    if not f.exists(): return Response("# not found",mimetype="text/plain"),404
    return Response(f.read_bytes(),mimetype="application/octet-stream",headers={"Content-Disposition":"attachment; filename=install-agent.ps1"})

@app.route("/download/tray")
def download_tray():
    f = AGENT_DIR/"DiskHealthTray.ps1"
    if not f.exists(): return Response("# not found",mimetype="text/plain"),404
    return Response(f.read_bytes(),mimetype="application/octet-stream",headers={"Content-Disposition":"attachment; filename=DiskHealthTray.ps1"})

@app.route("/api/stream")
def api_stream():
    q = broker.subscribe()
    @stream_with_context
    def _generate():
        yield "event: connected\ndata: {}\n\n"
        try:
            while True:
                try: msg = q.get(timeout=25); yield msg
                except queue.Empty: yield ": heartbeat\n\n"
        except GeneratorExit: pass
        finally: broker.unsubscribe(q)
    return Response(_generate(), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no","Connection":"keep-alive"})


AGENT_DETAIL_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"/><title>Agent Detail</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}body{background:#1E1E2E;color:#E0E0FF;font-family:'Segoe UI',sans-serif;font-size:14px}
.hdr{background:#27273A;padding:14px 24px;display:flex;align-items:center;gap:12px;border-bottom:1px solid #2D2D45}
.hdr h1{font-size:16px;font-weight:700}.back{background:#313145;border:none;color:#A78BFA;padding:7px 16px;border-radius:7px;cursor:pointer}
.badge{display:inline-block;padding:2px 9px;border-radius:10px;font-size:11px;font-weight:600}
.b-online{background:#14532D;color:#4ADE80}.b-offline{background:#3B1111;color:#F87171}
.b-healthy{background:#14532D;color:#4ADE80}.b-warning{background:#451A03;color:#FBBF24}
.b-critical{background:#3B0A0A;color:#F87171}.b-unknown{background:#1E293B;color:#94A3B8}
.content{padding:20px 24px;max-width:1100px}
.sec{font-size:10px;font-weight:700;color:#7070A0;text-transform:uppercase;letter-spacing:.08em;margin:20px 0 10px;border-bottom:1px solid #2D2D45;padding-bottom:6px}
.info-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px}
.ic{background:#27273A;border-radius:8px;padding:12px 14px}
.ic .l{font-size:10px;color:#7070A0;text-transform:uppercase;margin-bottom:3px}.ic .v{font-size:12px;word-break:break-all}
.disk-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}
.dk{background:#27273A;border-radius:9px;border-left:4px solid #3D3D5C;overflow:hidden}
.dk.healthy{border-left-color:#22C55E}.dk.warning{border-left-color:#F59E0B}.dk.critical{border-left-color:#EF4444}.dk.unknown{border-left-color:#6B7280}
.dk-hdr{padding:12px 14px;display:flex;justify-content:space-between;align-items:flex-start}
.dk-name{font-size:12px;font-weight:700}.dk-sub{font-size:10px;color:#7070A0;margin-top:2px}
.attr-row{display:flex;justify-content:space-between;padding:4px 14px;border-top:1px solid #1E1E2E;font-size:11px}
.attr-row .l{color:#7070A0}.attr-row .v{font-weight:600}
.v.ok{color:#4ADE80}.v.bad{color:#F87171}.v.warn{color:#FBBF24}.v.na{color:#4B4B6B}
.btn{background:#313145;border:1px solid #3D3D5C;color:#E0E0FF;padding:6px 13px;border-radius:6px;cursor:pointer;font-size:11px;font-weight:600}
.btn:hover{background:#3D3D5C}.btn.green{border-color:#0A3A1A;color:#4ADE80}.btn.yellow{border-color:#3A2A05;color:#FBBF24}.btn.danger{border-color:#4A1010;color:#F87171}
.cmd-panel{background:#27273A;border-radius:9px;padding:14px;display:flex;gap:8px;flex-wrap:wrap}
.cmd-out{background:#1E1E2E;border-radius:6px;padding:9px 12px;font-size:11px;font-family:Consolas,monospace;color:#A78BFA;margin-top:8px;display:none}
.alert-item{background:#27273A;border-radius:8px;padding:11px 14px;display:flex;gap:10px;border-left:4px solid #3D3D5C;margin-bottom:7px}
.alert-item.critical{border-left-color:#EF4444;background:#2A1A1A}.alert-item.warning{border-left-color:#F59E0B;background:#2A2010}
.hist-table{width:100%;border-collapse:collapse}
.hist-table th{text-align:left;padding:7px 11px;font-size:10px;color:#7070A0;text-transform:uppercase;border-bottom:1px solid #2D2D45}
.hist-table td{padding:7px 11px;font-size:11px;border-bottom:1px solid #1A1A2E;color:#B0B0D0}
.hist-table tr:hover td{background:#27273A}
</style></head><body>
<div class="hdr">
  <button class="back" onclick="history.back()">&larr; Dashboard</button>
  <h1 id="pageTitle">Agent Detail</h1>
  <span id="onlineBadge" class="badge"></span>
  <span id="statusBadge" class="badge" style="margin-left:6px"></span>
  <span style="flex:1"></span>
  <span style="font-size:11px;color:#4B4B6B">Last seen: <span id="lastSeen">-</span></span>
</div>
<div class="content" id="content"><div style="color:#7070A0;padding:40px;text-align:center">Loading...</div></div>
<script>
const agentId=location.pathname.split('/').pop();
function rel(iso){if(!iso)return'-';const s=Math.floor((Date.now()-new Date(iso))/1000);if(s<5)return'just now';if(s<60)return s+'s ago';if(s<3600)return Math.floor(s/60)+'m ago';if(s<86400)return Math.floor(s/3600)+'h ago';return Math.floor(s/86400)+'d ago';}
function fmt(v,u){return(v===null||v===undefined)?'—':v+(u||'');}
function fmtHours(h){if(h==null)return null;if(h<24)return h+'h';if(h<8760)return Math.round(h/24)+'d';return(h/8760).toFixed(1)+'yr';}
function mc(id,v){if(v===null||v===undefined)return'na';if(['reallocated','pending','media_errors'].includes(id))return v>0?'bad':'ok';if(id==='temperature')return v>=55?'bad':v>=45?'warn':'';if(id==='available_spare')return v<=10?'bad':v<=20?'warn':'ok';if(id==='percentage_used')return v>=90?'bad':v>=75?'warn':'ok';return'';}
function barColor(p){return p>=90?'#EF4444':p>=75?'#F59E0B':'#22C55E';}
function sIcon(sc){return{healthy:'✅',warning:'⚠️',critical:'🔴',unknown:'❓'}[sc]||'❓';}
async function load(){
  const r=await fetch('/api/agents/'+agentId);if(!r.ok)return;
  const a=await r.json();
  document.getElementById('pageTitle').textContent=a.hostname;
  const ob=document.getElementById('onlineBadge');ob.className='badge b-'+(a.online?'online':'offline');ob.textContent=a.online?'Online':'Offline';
  const sb=document.getElementById('statusBadge');sb.className='badge b-'+(a.worst_status||'unknown').toLowerCase();sb.textContent=a.worst_status||'Unknown';
  document.getElementById('lastSeen').textContent=rel(a.last_seen);
  const ic=[['Hostname',a.hostname],['IP',a.ip],['OS',a.os_version],['Agent Version',a.agent_version],['Users',a.logged_users||'—'],['First Seen',a.first_seen?new Date(a.first_seen).toLocaleString():'-'],['Last Report',a.last_report?new Date(a.last_report).toLocaleString():'-'],['Label',a.welcome_title||'—']].map(([l,v])=>'<div class="ic"><div class="l">'+l+'</div><div class="v">'+(v||'—')+'</div></div>').join('');
  const disks=a.disks.map(d=>{
    const sc=(d.smart_status||'Unknown').toLowerCase();
    const attrs=[['Temperature',d.temperature,'temperature','°C'],['Reallocated',d.reallocated,'reallocated',''],['Pending',d.pending,'pending',''],['Uncorrectable',d.uncorrectable,'uncorrectable',''],['Power-On Hrs',d.power_on_hours?fmtHours(d.power_on_hours):null,'',''],['Wear',d.percentage_used!=null?d.percentage_used+'%':null,'percentage_used',''],['Spare',d.available_spare!=null?d.available_spare+'%':null,'available_spare',''],['Media Err',d.media_errors,'media_errors','']].filter(a=>a[1]!==null&&a[1]!==undefined);
    const vols=(d.volumes||[]).map(v=>`<div class="attr-row"><span class="l">${v.drive} ${v.label||''}</span><span class="v">${v.used_pct||0}% used · ${v.free_gb||'?'}/${v.total_gb||'?'} GB free</span></div>`).join('');
    return `<div class="dk ${sc}"><div class="dk-hdr"><div><div class="dk-name">${sIcon(sc)} ${d.model||'Unknown'}</div><div class="dk-sub">S/N: ${d.serial||'—'} · ${d.interface||'?'} · ${d.size_gb!=null?d.size_gb+' GB':'?'}</div></div><span class="badge b-${sc}">${d.smart_status||'?'}</span></div>${attrs.map(([l,v,id])=>'<div class="attr-row"><span class="l">'+l+'</span><span class="v '+mc(id,typeof v==='string'&&v.includes('%')?parseFloat(v):v)+'">'+v+'</span></div>').join('')}${vols}</div>`;
  }).join('');
  const alts=(a.alerts||[]).map(al=>`<div class="alert-item ${al.severity}"><div style="font-size:16px">${al.severity==='critical'?'🔴':'⚠️'}</div><div style="flex:1;font-size:12px">${al.message}</div><div style="display:flex;flex-direction:column;gap:5px;align-items:flex-end"><span style="font-size:10px;color:#7070A0">${rel(al.created_at)}</span><button class="btn" onclick="dis(${al.id})">Dismiss</button></div></div>`).join('');
  document.getElementById('content').innerHTML=`
  <div class="sec">System Info</div><div class="info-grid">${ic}</div>
  ${alts?`<div class="sec">Active Alerts</div>${alts}<button class="btn" onclick="disAll()">Dismiss All</button>`:''}
  <div class="sec">Remote Commands</div>
  <div class="cmd-panel">
    <button class="btn green" onclick="cmd('get_disk_health')">🔄 Refresh</button>
    <button class="btn" onclick="cmd('ping')">Ping</button>
    <button class="btn yellow" onclick="cmd('update_agent')">⬆ Update</button>
    <button class="btn" onclick="cmd('clear_log')">Clear Log</button>
  </div><div id="cmdOut" class="cmd-out"></div>
  <div class="sec">Disks (${a.disks.length})</div>
  <div class="disk-grid">${disks||'<div style="color:#7070A0">No disk data yet.</div>'}</div>
  <div class="sec">History</div>
  <div style="background:#27273A;border-radius:9px;overflow:hidden"><table class="hist-table"><thead><tr><th>Received</th><th>IP</th><th>Users</th><th>Status</th><th>Disks</th></tr></thead><tbody id="histBody"></tbody></table></div>`;
  loadHist();
}
async function loadHist(){const rows=await(await fetch('/api/agents/'+agentId+'/history')).json();const tb=document.getElementById('histBody');if(!tb)return;tb.innerHTML=rows.map(r=>`<tr><td>${new Date(r.received_at).toLocaleString()}</td><td>${r.ip||'-'}</td><td>${r.logged_users||'-'}</td><td><span class="badge b-${(r.worst_status||'unknown').toLowerCase()}">${r.worst_status}</span></td><td>${r.disk_count}</td></tr>`).join('');}
async function cmd(action){const r=await fetch('/api/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({agent_id:agentId,action})});const j=await r.json();const el=document.getElementById('cmdOut');el.style.display='block';el.textContent=r.ok?'[OK] '+action+' queued ('+j.command_id+')':'[ERR] '+j.error;}
async function dis(id){await fetch('/api/agents/'+agentId+'/dismiss',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({alert_id:id})});load();}
async function disAll(){await fetch('/api/agents/'+agentId+'/dismiss',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})});load();}
load();setInterval(load,30000);
const es=new EventSource('/api/stream');
['report','ack','alert'].forEach(ev=>es.addEventListener(ev,e=>{if(JSON.parse(e.data).agent_id===agentId)load();}));
</script></body></html>"""

@app.route("/agent/<agent_id>")
def agent_detail_page(agent_id):
    return render_template_string(AGENT_DETAIL_HTML)


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"/><link rel="icon" href="data:,"><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>DiskHealth Dashboard</title>
<script src="/chart.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#1E1E2E;--panel:#27273A;--card:#222236;--card2:#1A1A30;
  --border:#252540;--border2:#303058;
  --text:#E0E0FF;--dim:#6868A0;--dim2:#4A4A78;
  --accent:#7C3AED;--accent2:#A78BFA;--accent3:#5B21B6;
  --green:#22C55E;--green-bg:#0F2A1A;
  --yellow:#F59E0B;--yellow-bg:#2A1F05;
  --red:#EF4444;--red-bg:#2A0A0A;
  --blue:#38BDF8;--r:10px;--rs:6px;
  --sidebar-w:272px;
}
/* ── Light theme ── */
[data-theme="light"]{
  --bg:#F0F2F8;--panel:#FFFFFF;--card:#FFFFFF;--card2:#F5F7FC;
  --border:#DDE1EE;--border2:#C8CEE0;
  --text:#1A1A3A;--dim:#7080A0;--dim2:#A0AABE;
  --accent:#6D28D9;--accent2:#7C3AED;--accent3:#EDE9FE;
  --green:#16A34A;--green-bg:#DCFCE7;
  --yellow:#D97706;--yellow-bg:#FEF9C3;
  --red:#DC2626;--red-bg:#FEE2E2;
  --blue:#0284C7;
}
/* ── Midnight Blue theme ── */
[data-theme="midnight"]{
  --bg:#0F1623;--panel:#151D2E;--card:#1A2235;--card2:#111827;
  --border:#1E2D42;--border2:#243348;
  --text:#C8D8F0;--dim:#5070A0;--dim2:#384E70;
  --accent:#3B82F6;--accent2:#60A5FA;--accent3:#1E3A5F;
  --green:#10B981;--green-bg:#052E1C;
  --yellow:#F59E0B;--yellow-bg:#1F1500;
  --red:#EF4444;--red-bg:#2A0A0A;
  --blue:#38BDF8;
}
/* ── Forest Green theme ── */
[data-theme="forest"]{
  --bg:#0D1F17;--panel:#122619;--card:#162E1E;--card2:#0A1910;
  --border:#1E3A28;--border2:#254832;
  --text:#C8F0D8;--dim:#507060;--dim2:#304838;
  --accent:#22C55E;--accent2:#4ADE80;--accent3:#14532D;
  --green:#4ADE80;--green-bg:#052E16;
  --yellow:#FCD34D;--yellow-bg:#1F1500;
  --red:#F87171;--red-bg:#2A0A0A;
  --blue:#67E8F9;
}
/* ── Crimson theme ── */
[data-theme="crimson"]{
  --bg:#1A0A0F;--panel:#25101A;--card:#2A1220;--card2:#1A0A10;
  --border:#3A1828;--border2:#4A2035;
  --text:#F0D0D8;--dim:#906070;--dim2:#603040;
  --accent:#E11D48;--accent2:#FB7185;--accent3:#881337;
  --green:#22C55E;--green-bg:#052E16;
  --yellow:#F59E0B;--yellow-bg:#1F1500;
  --red:#FB7185;--red-bg:#3B0A14;
  --blue:#38BDF8;
}
.theme-btn{background:var(--card2);border:1px solid var(--border2);color:var(--dim);border-radius:var(--rs);padding:4px 10px;font-size:11px;cursor:pointer;display:flex;align-items:center;gap:5px;transition:background .15s,color .15s}
.theme-btn:hover{background:var(--card);color:var(--text)}
.theme-menu{position:absolute;top:calc(100% + 4px);right:0;background:var(--panel);border:1px solid var(--border2);border-radius:var(--r);box-shadow:0 8px 32px rgba(0,0,0,.5);z-index:9000;min-width:160px;overflow:hidden}
.theme-menu-item{padding:9px 14px;font-size:12px;cursor:pointer;display:flex;align-items:center;gap:9px;color:var(--text);transition:background .1s}
.theme-menu-item:hover{background:var(--card2)}
.theme-menu-item.active{color:var(--accent2);font-weight:700}
.theme-swatch{width:12px;height:12px;border-radius:50%;flex-shrink:0}
html,body{height:100%;overflow:hidden}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;font-size:13px;display:flex;flex-direction:column}
::-webkit-scrollbar{width:5px;height:5px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:var(--border2);border-radius:3px}
.trend-summary-strip{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}
.trend-summary-card{background:var(--card);border:1px solid var(--border);border-radius:var(--rs);padding:9px 14px;min-width:110px;border-left:3px solid var(--border2)}
.trend-summary-card.trend-ok{border-left-color:var(--green)}.trend-summary-card.trend-warn{border-left-color:var(--yellow);background:#2A1F05}.trend-summary-card.trend-crit{border-left-color:var(--red);background:#2A0A0A}
.ts-label{font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:.07em;margin-bottom:3px}
.ts-val{font-size:16px;font-weight:800;color:#F0F0FF}
.trend-summary-card.trend-warn .ts-val{color:var(--yellow)}.trend-summary-card.trend-crit .ts-val{color:var(--red)}
.trend-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}
.trend-card{background:var(--card);border:1px solid var(--border);border-radius:var(--r);overflow:hidden}
.trend-card-hdr{padding:10px 14px 4px;display:flex;align-items:center;gap:8px}
.trend-metric-name{font-size:12px;font-weight:700;color:#F0F0FF;flex:1}
.trend-latest{font-size:16px;font-weight:800;color:#F0F0FF}
.trend-latest.trend-ok{color:var(--green)}.trend-latest.trend-warn{color:var(--yellow)}.trend-latest.trend-crit{color:var(--red)}
.trend-pts{font-size:10px;color:var(--dim2)}
.trend-delta{padding:0 14px 6px;min-height:16px}
.trend-canvas-wrap{display:block;padding:4px 10px 10px;height:160px;min-height:160px;position:relative;overflow:hidden}
.hm-cell{border-radius:10px;padding:12px 10px;cursor:pointer;position:relative;text-align:center;transition:transform .15s,box-shadow .15s;min-height:100px;display:flex;flex-direction:column;align-items:center;justify-content:flex-start;gap:3px}
.hm-cell:hover{transform:translateY(-3px);box-shadow:0 6px 22px rgba(0,0,0,.55);z-index:2}
.hm-cell.hm-selected{outline:3px solid #fff;outline-offset:2px;transform:translateY(-3px)}
.hm-cell-name{font-size:11px;font-weight:800;max-width:90px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-top:4px}
.hm-cell-ip{font-size:9px;opacity:.7;font-family:Consolas,monospace;margin-top:1px}
.hm-cell-meta{font-size:9px;opacity:.8;margin-top:2px;display:flex;gap:5px;justify-content:center;flex-wrap:wrap}
.hm-cell-badge{background:rgba(0,0,0,.25);border-radius:8px;padding:1px 6px;font-size:9px;font-weight:700}
.hm-detail{background:var(--card);border:1px solid var(--border2);border-radius:var(--r);margin-bottom:8px;overflow:hidden;animation:slidedown .18s ease}
.hm-detail-hdr{padding:12px 16px;background:var(--panel);display:flex;align-items:center;gap:10px;flex-wrap:wrap;border-bottom:1px solid var(--border)}
.hm-detail-name{font-size:14px;font-weight:800;color:#F0F0FF}
.hm-detail-disks{padding:12px 16px;display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:9px}
.hm-disk{background:var(--bg);border:1px solid var(--border);border-radius:var(--rs);padding:10px 12px}
.hm-disk.healthy{border-left:3px solid var(--green)}.hm-disk.warning{border-left:3px solid var(--yellow)}
.hm-disk.critical{border-left:3px solid var(--red)}.hm-disk.unknown{border-left:3px solid var(--dim2)}
.hm-disk-name{font-size:12px;font-weight:700;color:#F0F0FF;margin-bottom:4px}
.hm-disk-attrs{display:flex;gap:6px;flex-wrap:wrap}
.hm-disk-attr{font-size:10px;background:var(--card);border-radius:4px;padding:2px 6px;color:var(--dim)}
.hm-disk-attr.bad{color:var(--red);background:var(--red-bg)}.hm-disk-attr.warn{color:var(--yellow);background:var(--yellow-bg)}.hm-disk-attr.ok{color:var(--green);background:var(--green-bg)}
.hm-fleet-bar{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px;padding:12px 14px;background:var(--card);border:1px solid var(--border);border-radius:var(--r)}
.hm-fleet-stat{text-align:center;min-width:60px}
.hm-fleet-stat .n{font-size:20px;font-weight:800;line-height:1}
.hm-fleet-stat .l{font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:.07em;margin-top:2px}
.hm-legend{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px;font-size:11px;align-items:center}
.hm-legend-item{display:flex;align-items:center;gap:5px}
.hm-legend-dot{width:10px;height:10px;border-radius:50%}
.set-row{display:flex;align-items:center;gap:12px;padding:8px 0;border-bottom:1px solid var(--border);flex-wrap:wrap}
.set-row:last-child{border-bottom:none}
.set-label{font-size:11px;font-weight:600;color:var(--text);min-width:170px}
.set-input{background:var(--card2);border:1px solid var(--border2);color:var(--text);border-radius:4px;padding:4px 7px;font-size:11px;outline:none}
@keyframes slidedown{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:translateY(0)}}

.hdr{background:var(--panel);border-bottom:1px solid var(--border);height:52px;display:flex;align-items:center;padding:0 18px;gap:14px;flex-shrink:0}
.logo{font-size:16px;font-weight:800;color:#F0F0FF;display:flex;align-items:center;gap:7px}
.logo-icon{width:26px;height:26px;background:var(--accent);border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:14px}
.live-dot{width:7px;height:7px;border-radius:50%;background:var(--dim);transition:background .3s,box-shadow .3s}
.live-dot.on{background:var(--green);box-shadow:0 0 7px var(--green)}
.live-lbl{font-size:11px;color:var(--dim);margin-left:3px}
.clock{font-size:11px;color:var(--dim2);font-family:Consolas,monospace;margin-left:14px}
.statbar{background:var(--panel);border-bottom:1px solid var(--border);height:54px;display:flex;align-items:center;padding:0 18px;gap:4px;flex-shrink:0;overflow-x:auto}
.statbar::-webkit-scrollbar{height:0}
.stat{display:flex;flex-direction:column;align-items:center;padding:0 12px;min-width:68px}
.stat .n{font-size:20px;font-weight:800;line-height:1;transition:color .3s}
.stat .l{font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:.07em;margin-top:2px;white-space:nowrap}
.stat.white .n{color:#F0F0FF}.stat.green .n{color:var(--green)}.stat.red .n{color:var(--red)}
.stat.yellow .n{color:var(--yellow)}.stat.blue .n{color:var(--blue)}.stat.purple .n{color:var(--accent2)}
.sdiv{width:1px;background:var(--border);align-self:stretch;margin:8px 4px;flex-shrink:0}
.body{display:flex;flex:1;overflow:hidden}

/* ── Sidebar ── */
.sidebar{width:var(--sidebar-w);border-right:1px solid var(--border);display:flex;flex-direction:column;flex-shrink:0;background:var(--panel);transition:width .22s cubic-bezier(.4,0,.2,1),min-width .22s;overflow:hidden}
.sidebar.collapsed{width:0;min-width:0;border-right:none}
.sb-hdr{padding:10px 12px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:7px;flex-shrink:0}
.sb-title{font-size:10px;font-weight:700;color:var(--dim);text-transform:uppercase;letter-spacing:.08em;white-space:nowrap}
.sb-cnt{background:var(--accent3);color:var(--accent2);border-radius:9px;padding:1px 7px;font-size:10px;font-weight:700}
.sb-search{padding:9px 11px;border-bottom:1px solid var(--border);flex-shrink:0}
.sb-search input{width:100%;background:var(--card2);border:1px solid var(--border2);color:var(--text);border-radius:var(--rs);padding:6px 10px;font-size:12px;outline:none;transition:border-color .15s}
.sb-search input:focus{border-color:var(--accent)}
.agent-list{flex:1;overflow-y:auto;padding:7px}
.agent-card{background:var(--card);border-radius:var(--r);padding:11px 12px;margin-bottom:6px;cursor:pointer;border:1px solid transparent;transition:border-color .15s,background .15s}
.agent-card:hover{border-color:var(--accent);background:var(--card2)}
.agent-card.sel{border-color:var(--accent);background:#1A0F38}
.agent-card.off{opacity:.55}
.ac-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:5px}
.ac-host{font-size:13px;font-weight:700;color:#F0F0FF;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:154px}
.ac-mid{display:flex;align-items:center;gap:5px;font-size:11px;color:var(--dim);flex-wrap:wrap}
.ac-ip{font-family:Consolas,monospace;font-size:10px}
.ac-bottom{margin-top:4px;font-size:10px;color:var(--dim2)}
.badge{display:inline-flex;align-items:center;gap:3px;padding:2px 7px;border-radius:9px;font-size:10px;font-weight:700;white-space:nowrap}
.b-online{background:#0F2A1A;color:#4ADE80}.b-offline{background:#2A0A0A;color:#F87171}
.b-healthy{background:#0F2A1A;color:#4ADE80}.b-warning{background:#2A1F05;color:#FBBF24}
.b-critical{background:#2A0A0A;color:#F87171;animation:pulse 1.4s infinite}.b-unknown{background:#1A1A30;color:#94A3B8}
.b-crit-sm{background:#2A0A0A;color:#F87171;border-radius:9px;padding:1px 6px;font-size:10px;font-weight:700}
.b-warn-sm{background:#2A1F05;color:#FBBF24;border-radius:9px;padding:1px 6px;font-size:10px;font-weight:700}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}

/* ── Sidebar toggle button ── */
.sb-toggle{position:absolute;left:0;top:50%;transform:translateY(-50%);z-index:20;background:var(--panel);border:1px solid var(--border2);border-left:none;color:var(--accent2);width:18px;height:48px;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:11px;border-radius:0 6px 6px 0;transition:background .15s,color .15s;flex-shrink:0}
.sb-toggle:hover{background:var(--accent);color:#fff}
.sidebar-wrap{position:relative;display:flex;flex-shrink:0}

.main{flex:1;display:flex;flex-direction:column;overflow:hidden}
.agent-banner{background:var(--card);border-bottom:1px solid var(--border);padding:9px 18px;display:none;align-items:center;gap:10px;flex-wrap:wrap;flex-shrink:0}
.agent-banner.vis{display:flex}
.bn-name{font-size:14px;font-weight:800;color:#F0F0FF}
.bn-meta{display:flex;gap:10px;align-items:center;font-size:11px;color:var(--dim);flex-wrap:wrap}
.bn-spacer{flex:1}
.bn-btns{display:flex;gap:5px;flex-shrink:0}
.btn{background:var(--card2);border:1px solid var(--border2);color:var(--text);padding:5px 12px;border-radius:var(--rs);cursor:pointer;font-size:11px;font-weight:600;transition:background .15s;white-space:nowrap}
.btn:hover{background:var(--border2)}.btn.danger{border-color:#4A1010;color:var(--red)}.btn.danger:hover{background:var(--red-bg)}
.btn.green{border-color:#0A3A1A;color:var(--green)}.btn.green:hover{background:var(--green-bg)}
.btn.accent{background:var(--accent);border-color:var(--accent3);color:#fff}.btn.accent:hover{background:var(--accent3)}
.btn.yellow{border-color:#3A2A05;color:var(--yellow)}.btn.yellow:hover{background:var(--yellow-bg)}
.btn.sm{padding:3px 9px;font-size:10px}
.tabs{display:flex;gap:1px;padding:0 18px;background:var(--panel);border-bottom:1px solid var(--border);flex-shrink:0;overflow-x:auto;position:relative;z-index:5}
.tabs::-webkit-scrollbar{height:0}
.tab{padding:10px 15px;font-size:12px;font-weight:600;color:var(--dim);cursor:pointer;border-bottom:2px solid transparent;transition:color .15s,border-color .15s;white-space:nowrap}
.tab:hover{color:var(--text)}.tab.act{color:var(--accent2);border-bottom-color:var(--accent)}
.tab-badge{background:#3A1060;color:var(--accent2);border-radius:8px;padding:1px 6px;font-size:9px;font-weight:700;margin-left:3px}
.tab-badge.red{background:var(--red-bg);color:var(--red)}
.tc{flex:1;overflow-y:auto;padding:16px 18px;position:relative;z-index:1}
.pane{display:none}.pane.act{display:block}
.sec{font-size:10px;font-weight:700;color:var(--dim);text-transform:uppercase;letter-spacing:.09em;margin-bottom:9px;margin-top:18px;padding-bottom:5px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:7px;position:relative;z-index:2;overflow:visible}
.sec:first-child{margin-top:0}.sec .sec-actions{margin-left:auto;display:flex;gap:5px;position:relative;z-index:3;pointer-events:auto}
.welcome{text-align:center;padding:56px 20px;color:var(--dim)}
.welcome .wi{font-size:44px;margin-bottom:12px}.welcome h2{font-size:15px;font-weight:700;color:var(--text);margin-bottom:7px}.welcome p{font-size:12px;line-height:1.7}
/* Disk cards */
.disk-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}
.dk{background:var(--card);border-radius:var(--r);border:1px solid var(--border);overflow:hidden}
.dk.healthy{border-left:4px solid var(--green)}.dk.warning{border-left:4px solid var(--yellow)}
.dk.critical{border-left:4px solid var(--red)}.dk.unknown{border-left:4px solid var(--dim2)}
.dk-hdr{padding:12px 14px 9px;display:flex;justify-content:space-between;align-items:flex-start;border-bottom:1px solid var(--border)}
.dk-name{font-size:12px;font-weight:700;color:#F0F0FF;margin-bottom:2px}.dk-sub{font-size:10px;color:var(--dim);display:flex;gap:6px;flex-wrap:wrap}
.dk-temp{padding:10px 14px;border-bottom:1px solid var(--border);background:#0F0F1E}
.dk-temp-row{display:flex;justify-content:space-between;align-items:center;margin-bottom:5px}
.dk-temp-label{font-size:10px;font-weight:700;color:var(--dim);text-transform:uppercase}
.dk-temp-val{font-size:20px;font-weight:800}
.dk-temp-val.cool{color:var(--green)}.dk-temp-val.warm{color:var(--yellow)}.dk-temp-val.hot{color:var(--red)}.dk-temp-val.na{color:var(--dim2);font-size:13px}
.temp-track{position:relative;height:5px;border-radius:3px;background:linear-gradient(to right,#1E5AFF 0%,#22C55E 30%,#F59E0B 65%,#EF4444 100%);margin-bottom:2px}
.temp-needle{position:absolute;top:-5px;width:3px;height:15px;background:#fff;border-radius:2px;box-shadow:0 0 5px rgba(255,255,255,.5);transform:translateX(-50%);transition:left .5s}
.temp-scale{display:flex;justify-content:space-between;font-size:9px;color:var(--dim2)}
.dk-body{padding:0 14px}
.attr-row{display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px solid rgba(255,255,255,.03)}
.attr-row:last-child{border-bottom:none}
.attr-label{font-size:11px;color:var(--dim)}.attr-val{font-size:11px;font-weight:600}
.attr-val.ok{color:var(--green)}.attr-val.bad{color:var(--red)}.attr-val.warn{color:var(--yellow)}.attr-val.na{color:var(--dim2);font-weight:400}
.smart-sec{padding:8px 0;border-bottom:1px solid var(--border)}.smart-sec:last-child{border-bottom:none}
.smart-sec-title{font-size:9px;font-weight:700;color:var(--dim);text-transform:uppercase;letter-spacing:.08em;margin-bottom:5px}
.vol-item{margin-bottom:6px}.vol-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:2px}
.vol-drive{font-size:11px;font-weight:700;display:flex;align-items:center;gap:4px}
.vol-bar-wrap{height:5px;background:var(--card2);border-radius:3px;overflow:hidden;margin-bottom:2px}
.vol-bar{height:100%;border-radius:3px;transition:width .5s}
.vol-info{font-size:10px;color:var(--dim)}
/* Alerts */
.filter-btn{background:var(--card2);border:1px solid var(--border2);color:var(--dim);padding:4px 11px;border-radius:18px;cursor:pointer;font-size:11px;font-weight:600;transition:all .15s}
.filter-btn:hover,.filter-btn.act{background:var(--accent);border-color:var(--accent);color:#fff}
.alert-item{background:var(--card);border-radius:var(--r);padding:12px 14px;margin-bottom:7px;border:1px solid var(--border);display:flex;gap:10px;align-items:flex-start}
.alert-item.critical{border-left:4px solid var(--red);background:#2A1A1A}.alert-item.warning{border-left:4px solid var(--yellow);background:#2A2010}
.alert-body{flex:1;min-width:0}.alert-host{font-size:12px;font-weight:700;color:var(--accent2);margin-bottom:2px}
.alert-msg{font-size:12px;line-height:1.5;word-break:break-word}.alert-meta{font-size:10px;color:var(--dim);margin-top:3px}
.empty-state{text-align:center;padding:44px 20px;color:var(--dim)}
.empty-state .ei{font-size:32px;margin-bottom:9px}.empty-state p{font-size:12px;line-height:1.7}
/* History */
.hist-table{width:100%;border-collapse:collapse}
.hist-table th{text-align:left;padding:7px 11px;font-size:10px;color:var(--dim);text-transform:uppercase;border-bottom:1px solid var(--border);background:var(--panel)}
.hist-table td{padding:7px 11px;font-size:11px;border-bottom:1px solid var(--border);color:var(--dim)}
.hist-table tr:hover td{background:var(--card)}.hist-table td:first-child{color:var(--text);font-family:Consolas,monospace;font-size:11px}
/* Activity */
.act-line{display:flex;gap:9px;align-items:flex-start;padding:5px 0;border-bottom:1px solid rgba(255,255,255,.03)}
.act-ts{color:var(--dim2);font-family:Consolas,monospace;font-size:10px;flex-shrink:0;min-width:116px;padding-top:2px}
.act-host{color:var(--accent2);font-weight:700;flex-shrink:0;min-width:96px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px}
.act-type{padding:1px 6px;border-radius:7px;font-size:9px;font-weight:700;flex-shrink:0}
.act-type.register{background:#0A2A40;color:var(--blue)}.act-type.report{background:var(--green-bg);color:var(--green)}
.act-type.command{background:#1A0A30;color:var(--accent2)}.act-type.ack{background:#1A1A05;color:var(--yellow)}
.act-type.alert{background:var(--red-bg);color:var(--red)}.act-type.offline{background:var(--red-bg);color:#F87171}.act-type.online{background:var(--green-bg);color:#4ADE80}
.act-detail{color:var(--dim);flex:1;font-size:11px;word-break:break-word;line-height:1.5}
/* Commands */
.cmd-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:9px;margin-bottom:14px}
.cmd-card{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:14px;cursor:pointer;transition:border-color .15s,background .15s;display:flex;flex-direction:column;gap:5px}
.cmd-card:hover{border-color:var(--accent);background:var(--card2)}
.cmd-card .ci{font-size:22px}.cmd-card .ct{font-size:12px;font-weight:700;color:#F0F0FF}.cmd-card .cd{font-size:11px;color:var(--dim);line-height:1.4}
.cmd-card.green{border-color:var(--green-bg)}.cmd-card.green:hover{border-color:var(--green);background:var(--green-bg)}
.cmd-card.red{border-color:var(--red-bg)}.cmd-card.red:hover{border-color:var(--red);background:var(--red-bg)}
.cmd-card.yellow{border-color:var(--yellow-bg)}.cmd-card.yellow:hover{border-color:var(--yellow);background:var(--yellow-bg)}
.cmd-out{background:var(--panel);border:1px solid var(--border);border-radius:var(--rs);padding:10px 12px;font-size:11px;font-family:Consolas,monospace;color:var(--accent2);margin-top:7px;display:none;line-height:1.6}
.cmd-out.vis{display:block}
.pending-item{background:var(--card);border:1px solid var(--border);border-radius:var(--rs);padding:7px 11px;font-size:11px;color:var(--dim);font-family:Consolas,monospace;display:flex;gap:7px;align-items:center;margin-bottom:5px}
.pending-item .pa{color:var(--accent2);font-weight:700}
/* Command history table */
.cmd-hist-table{width:100%;border-collapse:collapse}
.cmd-hist-table th{text-align:left;padding:7px 11px;font-size:10px;color:var(--dim);text-transform:uppercase;border-bottom:1px solid var(--border);background:var(--panel)}
.cmd-hist-table td{padding:6px 11px;font-size:11px;border-bottom:1px solid var(--border);color:var(--dim);vertical-align:middle}
.cmd-hist-table td:first-child{font-family:Consolas,monospace;font-size:10px;color:var(--text)}
.cmd-hist-table tr:hover td{background:var(--card)}
.cmd-del-btn{background:none;border:1px solid var(--red-bg);color:var(--red);border-radius:4px;padding:2px 7px;cursor:pointer;font-size:10px;transition:background .12s}
.cmd-del-btn:hover{background:var(--red-bg)}
/* Info */
.info-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:9px}
.info-card{background:var(--card);border:1px solid var(--border);border-radius:var(--rs);padding:11px 13px}
.info-card .il{font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:.07em;margin-bottom:3px}
.info-card .iv{font-size:12px;word-break:break-all;line-height:1.4}
/* Scripts */
.script-block{background:var(--card);border:1px solid var(--border);border-radius:var(--r);overflow:hidden;margin-bottom:12px}
.script-block-hdr{padding:11px 14px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:9px;flex-wrap:wrap}

/* ── Script push target selector ── */
.push-target-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:10px 14px;background:var(--card2);border-top:1px solid var(--border)}
.push-target-row .pt-label{font-size:11px;color:var(--dim);font-weight:600;white-space:nowrap}
.push-radio{display:none}
.push-opt{padding:4px 11px;border-radius:14px;border:1px solid var(--border2);font-size:11px;font-weight:600;color:var(--dim);cursor:pointer;transition:all .15s;white-space:nowrap}
.push-opt:hover{color:var(--text)}
.push-radio:checked+.push-opt{background:var(--accent);border-color:var(--accent3);color:#fff}
.agent-check-list{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px;width:100%}
.ac-chk{display:none}
.ac-chk-lbl{padding:3px 10px;border-radius:12px;border:1px solid var(--border2);font-size:10px;font-weight:600;color:var(--dim);cursor:pointer;transition:all .15s}
.ac-chk:checked+.ac-chk-lbl{background:var(--accent3);border-color:var(--accent);color:var(--accent2)}

/* Toasts */
.toast-wrap{position:fixed;bottom:18px;right:18px;display:flex;flex-direction:column;gap:6px;z-index:9999;pointer-events:none}
.toast{background:var(--card2);border:1px solid var(--border2);border-radius:var(--r);padding:10px 14px;min-width:240px;max-width:380px;box-shadow:0 5px 24px rgba(0,0,0,.6);display:flex;gap:9px;align-items:flex-start;pointer-events:all;animation:tin .2s ease;font-size:12px}
.dlg-overlay{position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:19999;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(2px)}
.dlg-box{background:var(--card2);border:1px solid var(--border2);border-radius:12px;padding:28px 28px 22px;min-width:320px;max-width:440px;box-shadow:0 12px 48px rgba(0,0,0,.8);display:flex;flex-direction:column;gap:16px}
.dlg-title{font-size:14px;font-weight:700;color:var(--text);line-height:1.4}
.dlg-msg{font-size:12px;color:var(--dim);line-height:1.6;white-space:pre-wrap}
.dlg-btns{display:flex;gap:8px;justify-content:flex-end;margin-top:4px}
.dlg-btns .btn{min-width:80px;justify-content:center}
.toast.crit{border-left:3px solid var(--red)}.toast.warn{border-left:3px solid var(--yellow)}.toast.info{border-left:3px solid var(--blue)}.toast.ok{border-left:3px solid var(--green)}
.toast .tx{flex:1;line-height:1.5}.toast .tc2{margin-left:auto;background:none;border:none;color:var(--dim);cursor:pointer;font-size:14px;padding:0 1px;line-height:1}
.toast .tc2:hover{color:var(--text)}
@keyframes tin{from{transform:translateY(10px);opacity:0}to{transform:translateY(0);opacity:1}}

/* ══════════ ALL AGENTS TAB ══════════ */
.av-toolbar{display:flex;gap:9px;margin-bottom:14px;align-items:center;flex-wrap:wrap}
.av-search{flex:1;min-width:160px;max-width:260px;background:var(--card2);border:1px solid var(--border2);color:var(--text);border-radius:var(--rs);padding:6px 10px;font-size:12px;outline:none;transition:border-color .15s}
.av-search:focus{border-color:var(--accent)}
.av-pills{display:flex;gap:4px;flex-wrap:wrap}
.av-pill{background:var(--card2);border:1px solid var(--border2);color:var(--dim);padding:4px 10px;border-radius:18px;cursor:pointer;font-size:11px;font-weight:600;transition:all .15s}
.av-pill:hover{color:var(--text)}.av-pill.act{background:var(--accent);border-color:var(--accent3);color:#fff}
.av-agent{background:var(--card);border:1px solid var(--border);border-radius:var(--r);margin-bottom:10px;overflow:hidden}
.av-agent.offline{opacity:.6}
.av-agent-hdr{padding:11px 14px;display:flex;align-items:center;gap:9px;background:var(--panel);flex-wrap:wrap}
.av-hname{font-size:14px;font-weight:800;color:#F0F0FF}
.av-hmeta{flex:1;display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:11px;color:var(--dim);min-width:0}
.av-hmeta .mono{font-family:Consolas,monospace;font-size:10px}
.av-hbtns{display:flex;gap:4px;flex-shrink:0;flex-wrap:wrap}
.av-sections{display:flex;flex-direction:column}
.av-sec-row{border-top:1px solid var(--border)}
.av-sec-hdr{padding:8px 14px;display:flex;align-items:center;gap:7px;cursor:pointer;user-select:none;transition:background .15s}
.av-sec-hdr:hover{background:var(--card2)}
.av-sec-arrow{font-size:10px;color:var(--dim);transition:transform .2s;flex-shrink:0;display:inline-block}
.av-sec-arrow.open{transform:rotate(90deg)}
.av-sec-title{font-size:11px;font-weight:700;color:var(--dim);text-transform:uppercase;letter-spacing:.06em}
.av-sec-summary{flex:1;font-size:11px;color:var(--dim2);margin-left:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.av-sec-body{display:none;padding:12px 14px;background:var(--bg);border-top:1px solid var(--border)}
.av-sec-body.open{display:block}
.av-disk-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:9px}
.av-dk{background:var(--card);border:1px solid var(--border);border-radius:var(--rs);overflow:hidden}
.av-dk.healthy{border-left:3px solid var(--green)}.av-dk.warning{border-left:3px solid var(--yellow)}
.av-dk.critical{border-left:3px solid var(--red)}.av-dk.unknown{border-left:3px solid var(--dim2)}
.av-dk-top{padding:9px 11px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--border)}
.av-dk-name{font-size:12px;font-weight:700;color:#F0F0FF}.av-dk-sub{font-size:10px;color:var(--dim);margin-top:2px}
.av-dk-temp{display:flex;align-items:center;gap:5px;padding:5px 11px;border-bottom:1px solid var(--border);background:#0F0F1E}
.av-tv{font-size:15px;font-weight:800;min-width:40px}
.av-tv.cool{color:var(--green)}.av-tv.warm{color:var(--yellow)}.av-tv.hot{color:var(--red)}.av-tv.na{color:var(--dim2);font-size:11px;font-weight:400}
.av-tbar{flex:1;height:5px;border-radius:3px;background:linear-gradient(to right,#1E5AFF 0%,#22C55E 30%,#F59E0B 65%,#EF4444 100%);position:relative}
.av-tneedle{position:absolute;top:-4px;width:3px;height:13px;background:#fff;border-radius:2px;transform:translateX(-50%);box-shadow:0 0 4px rgba(255,255,255,.4);transition:left .5s}
.av-dk-body{padding:9px 11px;display:grid;grid-template-columns:1fr 1fr;gap:4px}
.av-metric .av-ml{font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:.05em}
.av-metric .av-mv{font-size:11px;font-weight:700}
.av-mv.ok{color:var(--green)}.av-mv.bad{color:var(--red)}.av-mv.warn{color:var(--yellow)}.av-mv.na{color:var(--dim2);font-weight:400;font-size:10px}
.av-vols{padding:7px 11px;border-top:1px solid var(--border)}
.av-vol-row{display:flex;align-items:center;gap:5px;margin-bottom:3px}
.av-vol-row:last-child{margin-bottom:0}
.av-vd{font-size:10px;font-weight:700;min-width:24px}
.av-vbg{flex:1;height:4px;background:var(--card2);border-radius:2px;overflow:hidden}
.av-vb{height:100%;border-radius:2px}.av-vi{font-size:10px;color:var(--dim);white-space:nowrap}
.av-info-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:7px}
.av-ic{background:var(--card);border:1px solid var(--border);border-radius:var(--rs);padding:9px 11px}
.av-il{font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:.07em;margin-bottom:2px}
.av-iv{font-size:11px;word-break:break-all}
.av-alert{background:var(--card);border-radius:var(--rs);padding:9px 11px;display:flex;gap:7px;align-items:flex-start;border-left:3px solid var(--border);margin-bottom:5px}
.av-alert.critical{border-left-color:var(--red);background:#2A1A1A}.av-alert.warning{border-left-color:var(--yellow);background:#2A2010}
.av-alert-msg{flex:1;font-size:11px;line-height:1.5}
.av-hist-table{width:100%;border-collapse:collapse}
.av-hist-table th{text-align:left;padding:5px 9px;font-size:9px;color:var(--dim);text-transform:uppercase;border-bottom:1px solid var(--border);background:var(--panel)}
.av-hist-table td{padding:5px 9px;font-size:11px;border-bottom:1px solid var(--border);color:var(--dim)}
.av-hist-table td:first-child{font-family:Consolas,monospace;font-size:10px;color:var(--text)}
.av-hist-table tr:hover td{background:var(--card)}
</style></head><body>

<div class="hdr">
  <div class="logo"><div class="logo-icon">💾</div>DiskHealth<span style="color:var(--accent2);font-weight:400;margin-left:2px">Dashboard</span></div>
  <div style="flex:1"></div>
  <div style="position:relative" id="themeWrap">
    <button class="theme-btn" onclick="toggleThemeMenu()" title="Change theme">🎨 Theme</button>
    <div class="theme-menu" id="themeMenu" style="display:none">
      <div class="theme-menu-item" data-t="" onclick="setTheme('')"><span class="theme-swatch" style="background:#7C3AED"></span>Dark (default)</div>
      <div class="theme-menu-item" data-t="light" onclick="setTheme('light')"><span class="theme-swatch" style="background:#6D28D9"></span>Light</div>
      <div class="theme-menu-item" data-t="midnight" onclick="setTheme('midnight')"><span class="theme-swatch" style="background:#3B82F6"></span>Midnight Blue</div>
      <div class="theme-menu-item" data-t="forest" onclick="setTheme('forest')"><span class="theme-swatch" style="background:#22C55E"></span>Forest Green</div>
      <div class="theme-menu-item" data-t="crimson" onclick="setTheme('crimson')"><span class="theme-swatch" style="background:#E11D48"></span>Crimson</div>
    </div>
  </div>
  <div class="live-dot" id="sseDot"></div>
  <span class="live-lbl" id="sseLabel">Connecting...</span>
  <span class="clock" id="clk"></span>
</div>

<div class="statbar">
  <div class="stat white"><div class="n" id="s-tot">-</div><div class="l">Total</div></div>
  <div class="stat green"><div class="n" id="s-on">-</div><div class="l">Online</div></div>
  <div class="stat red">  <div class="n" id="s-off">-</div><div class="l">Offline</div></div>
  <div class="sdiv"></div>
  <div class="stat green"><div class="n" id="s-h">-</div><div class="l">Healthy</div></div>
  <div class="stat yellow"><div class="n" id="s-w">-</div><div class="l">Warning</div></div>
  <div class="stat red">  <div class="n" id="s-c">-</div><div class="l">Critical</div></div>
  <div class="sdiv"></div>
  <div class="stat red">   <div class="n" id="s-al">-</div><div class="l">Alerts</div></div>
  <div class="stat purple"><div class="n" id="s-d">-</div><div class="l">Disks</div></div>
  <div class="stat blue">  <div class="n" id="s-r">-</div><div class="l">24h Reports</div></div>
  <div class="stat white"> <div class="n" id="s-tb">-</div><div class="l">Total TB</div></div>
</div>

<div class="body">
  <!-- ── Sidebar wrapper with toggle ── -->
  <div class="sidebar-wrap">
    <div class="sidebar" id="sidebar">
      <div class="sb-hdr"><span class="sb-title">Agents</span><span class="sb-cnt" id="sbCnt">0</span></div>
      <div class="sb-search"><input id="filterInput" placeholder="Filter by name or IP…" oninput="renderSidebar()"/></div>
      <div class="agent-list" id="agentList"></div>
    </div>
    <button class="sb-toggle" id="sbToggle" title="Toggle sidebar" onclick="toggleSidebar()">◀</button>
  </div>

  <div class="main">
    <div class="agent-banner" id="bnr">
      <div><div class="bn-name" id="bnHost"></div><div class="bn-meta" id="bnMeta"></div></div>
      <div class="bn-spacer"></div>
      <div class="bn-btns">
        <button class="btn green"  onclick="sendCmd('get_disk_health')">🔄 Refresh</button>
        <button class="btn yellow" onclick="sendCmd('clear_log')">🗑 Clear Log</button>
        <button class="btn"        onclick="sendCmd('ping')">🏓 Ping</button>
        <button class="btn"        onclick="sendCmd('update_agent')">⬆ Update</button>
        <button class="btn danger" onclick="confirmDeleteAgent()">🗑 Remove</button>
      </div>
    </div>

    <div class="tabs">
      <div class="tab act" data-tab="overview"  onclick="switchTab('overview')">Overview</div>
      <div class="tab"     data-tab="alerts"    onclick="switchTab('alerts')">Alerts<span class="tab-badge red" id="alertsBadge" style="display:none"></span></div>
      <div class="tab"     data-tab="history"   onclick="switchTab('history')">History</div>
      <div class="tab"     data-tab="activity"  onclick="switchTab('activity')">Activity Log</div>
      <div class="tab"     data-tab="commands"  onclick="switchTab('commands')">Commands</div>
      <div class="tab"     data-tab="scripts"   onclick="switchTab('scripts')">📄 Scripts</div>
      <div class="tab"     data-tab="trends"    onclick="switchTab('trends')">Trends</div>
      <div class="tab"     data-tab="heatmap"   onclick="switchTab('heatmap')">Heatmap</div>
      <div class="tab"     data-tab="analytics" onclick="switchTab('analytics')">Analytics</div>
      <div class="tab"     data-tab="allview"   onclick="switchTab('allview')">All Agents</div>
      <div class="tab"     data-tab="settings"  onclick="switchTab('settings')">Settings</div>
    </div>

    <div class="tc" id="tc">

      <!-- OVERVIEW -->
      <div class="pane act" id="pane-overview">
        <div class="welcome" id="welcomeMsg">
          <div class="wi">💾</div><h2>DiskHealth Dashboard Monitor</h2>
          <p>Select an agent from the sidebar to view disk health,<br>SMART data, temperatures, and volumes.</p>
        </div>
        <div id="overviewContent" style="display:none"></div>
      </div>

      <!-- ALERTS -->
      <div class="pane" id="pane-alerts">
        <div class="sec">Dashboard Alerts<div class="sec-actions">
          <button class="btn" onclick="loadAlerts()">🔄 Refresh</button>
          <button class="btn danger" onclick="dismissAllAlerts()">Dismiss All</button>
        </div></div>
        <div style="display:flex;gap:7px;margin-bottom:12px;flex-wrap:wrap">
          <button class="filter-btn act" id="af-all"  onclick="setAlertFilter('all')">All</button>
          <button class="filter-btn"     id="af-crit" onclick="setAlertFilter('critical')">Critical</button>
          <button class="filter-btn"     id="af-warn" onclick="setAlertFilter('warning')">Warning</button>
        </div>
        <div id="alertsContent"></div>
      </div>

      <!-- HISTORY -->
      <div class="pane" id="pane-history">
        <div class="sec" id="histSec">History<div class="sec-actions" id="histActions" style="display:none">
          <button class="btn danger" onclick="confirmDeleteHistory()">🗑 Delete All</button>
        </div></div>
        <div id="historyContent"><div class="empty-state"><div class="ei">📋</div><p>Select an agent to view its report history.</p></div></div>
      </div>

      <!-- ACTIVITY -->
      <div class="pane" id="pane-activity">
        <div class="sec">Activity Log<div class="sec-actions">
          <button class="btn" onclick="loadActivity()">🔄 Refresh</button>
          <button class="btn danger" onclick="confirmClearActivity()">🗑 Clear</button>
        </div></div>
        <div id="actContent"></div>
      </div>

      <!-- COMMANDS -->
      <div class="pane" id="pane-commands">
        <div class="sec">Commands — <span id="cmdAgentLabel" style="color:var(--dim);font-size:11px;font-weight:400;text-transform:none;letter-spacing:0">No agent selected</span></div>
        <div class="cmd-grid">
          <div class="cmd-card green" onclick="sendCmd('get_disk_health')"><div class="ci">🔄</div><div class="ct">Refresh Disks</div><div class="cd">Force an immediate SMART report now.</div></div>
          <div class="cmd-card" onclick="sendCmd('ping')"><div class="ci">🏓</div><div class="ct">Ping Agent</div><div class="cd">Check the agent is alive and responding.</div></div>
          <div class="cmd-card yellow" onclick="sendCmd('update_agent')"><div class="ci">⬆</div><div class="ct">Update Agent</div><div class="cd">Pull latest DiskHealthAgent.ps1 from server.</div></div>
          <div class="cmd-card red" onclick="sendCmd('clear_log')"><div class="ci">🗑</div><div class="ct">Clear Agent Log</div><div class="cd">Delete agent.log on remote machine.</div></div>
        </div>
        <div class="cmd-out" id="cmdOut"></div>
        <div class="sec" style="margin-top:18px">Pending Commands</div>
        <div id="pendingList"><div style="color:var(--dim);font-size:11px">No pending commands.</div></div>
        <div class="sec" style="margin-top:18px">Bulk — All Online Agents</div>
        <div class="cmd-grid">
          <div class="cmd-card" onclick="sendBulk('ping')"><div class="ci">🏓</div><div class="ct">Ping All Online</div><div class="cd">Verify every online agent responds.</div></div>
          <div class="cmd-card green" onclick="sendBulk('get_disk_health')"><div class="ci">🔄</div><div class="ct">Refresh All Disks</div><div class="cd">Force reports from all online agents.</div></div>
          <div class="cmd-card red" onclick="sendBulk('clear_log')"><div class="ci">🗑</div><div class="ct">Clear All Logs</div><div class="cd">Wipe agent.log on every online machine.</div></div>
        </div>
        <div class="cmd-out" id="bulkOut"></div>
        <!-- Command History -->
        <div class="sec" style="margin-top:18px">Command History
          <div class="sec-actions">
            <button class="btn" onclick="loadCmdHistory()">🔄 Refresh</button>
            <button class="btn danger" onclick="confirmClearCmdHistory()">🗑 Clear All</button>
          </div>
        </div>
        <div id="cmdHistContent"><div style="color:var(--dim);font-size:11px">Loading…</div></div>
      </div>

      <!-- SCRIPTS -->
      <div class="pane" id="pane-scripts">
        <div class="sec">Script Manager<div class="sec-actions">
          <a class="btn" href="/download/agent" download>⬇ Agent</a>
          <a class="btn" href="/download/installer" download>⬇ Installer</a>
          <a class="btn" href="/download/tray" download>⬇ Tray</a>
        </div></div>
        <p style="font-size:12px;color:var(--dim);margin-bottom:16px">Scripts saved to disk — <strong style="color:var(--text)">persist across server restarts</strong>. Agents download them via the <code style="background:var(--card2);padding:1px 5px;border-radius:4px;font-size:10px">update_agent</code> command.</p>

        <!-- Agent push target selector (shared, rendered once) -->
        <div style="background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:13px 16px;margin-bottom:14px">
          <div style="font-size:11px;font-weight:700;color:var(--text);margin-bottom:8px">📡 Push Target</div>
          <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:6px">
            <input type="radio" class="push-radio" name="pushTarget" id="pt-all"    value="all"    checked onchange="updatePushTarget()"><label for="pt-all"    class="push-opt">All Online Agents</label>
            <input type="radio" class="push-radio" name="pushTarget" id="pt-select" value="select"         onchange="updatePushTarget()"><label for="pt-select" class="push-opt">Select Agents</label>
          </div>
          <div id="agentCheckboxes" style="display:none" class="agent-check-list"></div>
          <div style="font-size:10px;color:var(--dim);margin-top:6px" id="pushTargetSummary">Will push to all online agents.</div>
        </div>

        <div class="script-block">
          <div class="script-block-hdr">
            <div><div style="font-size:12px;font-weight:700;color:#F0F0FF">DiskHealthAgent.ps1</div><div style="font-size:10px;color:var(--dim);margin-top:1px">Served at /agent/agent.ps1</div></div>
            <div style="margin-left:auto;display:flex;gap:6px;flex-wrap:wrap">
              <label class="btn" style="cursor:pointer">📁 Import<input type="file" accept=".ps1,.txt" style="display:none" onchange="importScript(this,'agent')"></label>
              <button class="btn green" onclick="saveScript('agent')">💾 Save</button>
              <button class="btn accent" onclick="saveAndPush('agent')">⬆ Save &amp; Push</button>
            </div>
          </div>
          <div style="padding:10px"><textarea id="edAgent" style="width:100%;min-height:340px;background:var(--bg);border:1px solid var(--border);border-radius:var(--rs);color:var(--text);font-family:Consolas,monospace;font-size:11px;line-height:1.6;padding:10px;resize:vertical;outline:none" spellcheck="false"></textarea>
          <div id="stAgent" style="font-size:11px;margin-top:4px;color:var(--dim)"></div></div>
        </div>
        <div class="script-block">
          <div class="script-block-hdr">
            <div><div style="font-size:12px;font-weight:700;color:#F0F0FF">install-agent.ps1</div><div style="font-size:10px;color:var(--dim);margin-top:1px">Served at /download/installer</div></div>
            <div style="margin-left:auto"><button class="btn green" onclick="saveScript('installer')">💾 Save</button></div>
          </div>
          <div style="padding:10px"><textarea id="edInstaller" style="width:100%;min-height:180px;background:var(--bg);border:1px solid var(--border);border-radius:var(--rs);color:var(--text);font-family:Consolas,monospace;font-size:11px;line-height:1.6;padding:10px;resize:vertical;outline:none" spellcheck="false"></textarea>
          <div id="stInstaller" style="font-size:11px;margin-top:4px;color:var(--dim)"></div></div>
        </div>
        <div class="script-block">
          <div class="script-block-hdr">
            <div><div style="font-size:12px;font-weight:700;color:#F0F0FF">DiskHealthTray.ps1</div><div style="font-size:10px;color:var(--dim);margin-top:1px">Served at /agent/tray.ps1</div></div>
            <div style="margin-left:auto;display:flex;gap:6px">
              <button class="btn green" onclick="saveScript('tray')">💾 Save</button>
              <button class="btn accent" onclick="saveAndPush('tray')">⬆ Save &amp; Push</button>
            </div>
          </div>
          <div style="padding:10px"><textarea id="edTray" style="width:100%;min-height:180px;background:var(--bg);border:1px solid var(--border);border-radius:var(--rs);color:var(--text);font-family:Consolas,monospace;font-size:11px;line-height:1.6;padding:10px;resize:vertical;outline:none" spellcheck="false"></textarea>
          <div id="stTray" style="font-size:11px;margin-top:4px;color:var(--dim)"></div></div>
        </div>
      </div>

      <!-- ALL AGENTS VIEW -->
      <div class="pane" id="pane-allview">
        <div class="av-toolbar">
          <input class="av-search" id="avSearch" placeholder="Search hostname or IP…" oninput="renderAllView()"/>
          <div class="av-pills">
            <div class="av-pill act" id="avp-all"      onclick="setAvFilter('all')">All</div>
            <div class="av-pill"     id="avp-online"   onclick="setAvFilter('online')">Online</div>
            <div class="av-pill"     id="avp-offline"  onclick="setAvFilter('offline')">Offline</div>
            <div class="av-pill"     id="avp-critical" onclick="setAvFilter('critical')" style="color:var(--red)">Critical</div>
            <div class="av-pill"     id="avp-warning"  onclick="setAvFilter('warning')"  style="color:var(--yellow)">Warning</div>
          </div>
          <div style="flex:1"></div>
          <button class="btn" onclick="loadAgents().then(renderAllView)">🔄 Refresh</button>
        </div>
        <div id="avContent"><div class="empty-state"><div class="ei">🖥</div><p>Loading agents…</p></div></div>
      </div>


      <!-- TRENDS -->
      <div class="pane" id="pane-trends">
        <div style="display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin-bottom:14px">
          <div style="font-size:12px;color:var(--dim)" id="trendsAgentLabel">Select an agent first</div>
          <div style="flex:1"></div>
          <div style="display:flex;gap:4px" id="trendWindowBtns">
            <button class="filter-btn" data-h="1" onclick="setTrendWindow(1)">1h</button>
            <button class="filter-btn act" data-h="24" onclick="setTrendWindow(24)">24h</button>
            <button class="filter-btn" data-h="72" onclick="setTrendWindow(72)">3d</button>
            <button class="filter-btn" data-h="168" onclick="setTrendWindow(168)">7d</button>
          </div>
          <button class="btn" onclick="loadTrendsTab()">Refresh</button>
        </div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px" id="trendDiskTabs"></div>
        <div id="trendsContent"><div class="empty-state"><p>Select an agent to view disk trends.</p></div></div>
      </div>

      <!-- HEATMAP -->
      <div class="pane" id="pane-heatmap">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;flex-wrap:wrap;gap:8px">
          <div style="font-size:11px;font-weight:700;color:var(--text)">Desktop Fleet — click a machine to inspect</div>
          <div style="display:flex;gap:5px">
            <select id="hmSortSel" class="set-input" onchange="renderHeatmap()" style="font-size:11px;padding:4px 8px;background:var(--card2);border:1px solid var(--border2);color:var(--text);border-radius:4px">
              <option value="status">Sort: Status</option>
              <option value="name">Sort: Name</option>
              <option value="temp">Sort: Temp</option>
              <option value="offline">Sort: Offline Last</option>
            </select>
            <button class="btn" onclick="loadAgents().then(renderHeatmap)">Refresh</button>
          </div>
        </div>
        <div style="display:flex;align-items:center;gap:0;margin-bottom:14px;background:var(--card);border:1px solid var(--border);border-radius:var(--r);overflow:hidden;font-size:11px">
          <div style="background:#0F2A1A;color:#4ADE80;padding:8px 14px;font-weight:600;flex:1;text-align:center">1. Spot the sick machine</div>
          <div style="color:var(--dim2);padding:0 4px">&#9658;</div>
          <div style="padding:8px 14px;flex:1;text-align:center">2. Click its square</div>
          <div style="color:var(--dim2);padding:0 4px">&#9658;</div>
          <div style="padding:8px 14px;flex:1;text-align:center">3. Open Overview</div>
          <div style="color:var(--dim2);padding:0 4px">&#9658;</div>
          <div style="background:#2A0A0A;color:#F87171;padding:8px 14px;font-weight:600;flex:1;text-align:center">4. Confirm in Alerts</div>
        </div>
        <div id="heatmapContent"></div>
      </div>

      <!-- ANALYTICS -->
      <div class="pane" id="pane-analytics">
        <div style="display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin-bottom:14px">
          <div style="font-size:12px;color:var(--dim)">Fleet analytics</div>
          <div style="flex:1"></div>
          <div style="display:flex;gap:4px" id="analyticsWindowBtns">
            <button class="filter-btn act" data-d="7"  onclick="setAnalyticsWindow(7)">7d</button>
            <button class="filter-btn" data-d="14" onclick="setAnalyticsWindow(14)">14d</button>
            <button class="filter-btn" data-d="30" onclick="setAnalyticsWindow(30)">30d</button>
          </div>
          <button class="btn" onclick="loadAnalyticsTab()">Refresh</button>
        </div>
        <div id="analyticsContent"></div>
      </div>

      <!-- SETTINGS -->
      <div class="pane" id="pane-settings">
        <div class="sec">Settings<div class="sec-actions">
          <button class="btn green" onclick="saveSettings()">Save All</button>
        </div></div>
        <div id="settingsContent"><div style="color:var(--dim);font-size:11px">Loading...</div></div>
      </div>

    </div><!-- /tc -->
  </div><!-- /main -->
</div><!-- /body -->

<div class="toast-wrap" id="toasts"></div>

<!-- Custom confirm modal — replaces browser confirm() which can be permanently blocked -->
<div class="dlg-overlay" id="dlgOverlay" style="display:none">
  <div class="dlg-box">
    <div class="dlg-title" id="dlgTitle"></div>
    <div class="dlg-msg"   id="dlgMsg"></div>
    <div class="dlg-btns">
      <button class="btn"        id="dlgCancel" onclick="_dlgResolve(false)">Cancel</button>
      <button class="btn danger" id="dlgOk"     onclick="_dlgResolve(true)">Confirm</button>
    </div>
  </div>
</div>

<script>
// ── Custom confirm dialog (never blocked by browser) ──────────────────────────
let _dlgResolve = null;
function dlgConfirm(title, msg, okLabel){
  return new Promise(function(resolve){
    document.getElementById('dlgTitle').textContent  = title;
    document.getElementById('dlgMsg').textContent    = msg || '';
    document.getElementById('dlgMsg').style.display  = msg ? '' : 'none';
    document.getElementById('dlgOk').textContent     = okLabel || 'Confirm';
    document.getElementById('dlgOverlay').style.display = '';
    document.getElementById('dlgOk').focus();
    _dlgResolve = function(val){
      document.getElementById('dlgOverlay').style.display = 'none';
      _dlgResolve = null;
      resolve(val);
    };
  });
}
// Close on overlay click (outside the box)
document.getElementById('dlgOverlay').addEventListener('click', function(e){
  if(e.target === this && _dlgResolve) _dlgResolve(false);
});
// Close on Escape key
document.addEventListener('keydown', function(e){
  if(e.key === 'Escape' && _dlgResolve) _dlgResolve(false);
});
// ── State ─────────────────────────────────────────────────────────────────────
let agents=[], selId=null, alertFilter='all', avFilter='all';
let sidebarOpen=true;
const avOpenSections={};

// ── Theme ─────────────────────────────────────────────────────────────────────
const THEMES=['','light','midnight','forest','crimson'];
function applyTheme(t){
  document.documentElement.setAttribute('data-theme',t||'');
  document.querySelectorAll('.theme-menu-item').forEach(function(el){
    el.classList.toggle('active',el.dataset.t===(t||''));
  });
}
function setTheme(t){
  applyTheme(t);
  try{localStorage.setItem('dh-theme',t);}catch(e){}
  document.getElementById('themeMenu').style.display='none';
}
function toggleThemeMenu(){
  var m=document.getElementById('themeMenu');
  m.style.display=m.style.display==='none'?'':'none';
}
document.addEventListener('click',function(e){
  var wrap=document.getElementById('themeWrap');
  if(wrap&&!wrap.contains(e.target))document.getElementById('themeMenu').style.display='none';
});
// Apply saved theme on load
(function(){try{var t=localStorage.getItem('dh-theme');if(t!==null)applyTheme(t);}catch(e){}})();

// ── Clock ─────────────────────────────────────────────────────────────────────
function tick(){document.getElementById('clk').textContent=new Date().toLocaleTimeString();}
tick();setInterval(tick,1000);

// ── Helpers ───────────────────────────────────────────────────────────────────
function rel(iso){
  if(!iso)return'—';
  const s=Math.floor((Date.now()-new Date(iso))/1000);
  if(s<5)return'just now';if(s<60)return s+'s ago';
  if(s<3600)return Math.floor(s/60)+'m ago';
  if(s<86400)return Math.floor(s/3600)+'h ago';
  return Math.floor(s/86400)+'d ago';
}
function fmtDate(iso){return iso?new Date(iso).toLocaleString():'—';}
function fmtHours(h){if(h==null)return null;if(h<24)return h+'h';if(h<8760)return Math.round(h/24)+'d';return(h/8760).toFixed(1)+'yr';}
function sIcon(sc){return{healthy:'✅',warning:'⚠️',critical:'🔴',unknown:'❓'}[sc]||'❓';}
function barColor(p){return p>=90?'#EF4444':p>=75?'#F59E0B':'#22C55E';}
function mc(id,v){
  if(v===null||v===undefined)return'na';
  if(['reallocated','pending','media_errors','uncorrectable'].includes(id))return v>0?'bad':'ok';
  if(id==='temperature')return v>=55?'bad':v>=45?'warn':'';
  if(id==='available_spare')return v<=10?'bad':v<=20?'warn':'ok';
  if(id==='percentage_used')return v>=90?'bad':v>=75?'warn':'ok';
  return'neutral';
}
function esc(s){const d=document.createElement('div');d.textContent=String(s||'');return d.innerHTML;}

// ── Sidebar toggle ────────────────────────────────────────────────────────────
function toggleSidebar(){
  sidebarOpen=!sidebarOpen;
  const sb=document.getElementById('sidebar');
  const btn=document.getElementById('sbToggle');
  if(sidebarOpen){sb.classList.remove('collapsed');btn.textContent='◀';btn.title='Collapse sidebar';}
  else{sb.classList.add('collapsed');btn.textContent='▶';btn.title='Expand sidebar';}
}

// ── Tab switching ─────────────────────────────────────────────────────────────
function switchTab(name){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('act'));
  document.querySelectorAll('.pane').forEach(p=>p.classList.remove('act'));
  document.querySelector('.tab[data-tab="'+name+'"]').classList.add('act');
  document.getElementById('pane-'+name).classList.add('act');
  if(name==='alerts')   loadAlerts();
  if(name==='history')  loadHistory();
  if(name==='activity') loadActivity();
  if(name==='commands'){refreshPending();loadCmdHistory();}
  if(name==='scripts')  {loadScripts();renderPushTargetCheckboxes();}
  if(name==='allview')  renderAllView();
  if(name==='trends')   {loadTrendsTab();setTimeout(function(){if(trendDiskSel&&selId)loadTrendCharts(trendDiskSel);},600);}
  if(name==='heatmap')  renderHeatmap();
  if(name==='analytics')loadAnalyticsTab();
  if(name==='settings') loadSettingsPane();
}

// ── Toast ─────────────────────────────────────────────────────────────────────
function toast(msg,type,dur){
  dur=dur||5000;
  const el=document.createElement('div');
  el.className='toast '+(type||'info');
  el.innerHTML='<span class="tx">'+msg+'</span><button class="tc2" onclick="this.parentNode.remove()">✕</button>';
  document.getElementById('toasts').prepend(el);
  setTimeout(()=>el.remove(),dur);
}

// ── Stats ─────────────────────────────────────────────────────────────────────
async function loadStats(){
  try{
    const d=await fetch('/api/stats').then(r=>r.json());
    document.getElementById('s-tot').textContent=d.total_agents;
    document.getElementById('s-on' ).textContent=d.online_agents;
    document.getElementById('s-off').textContent=d.offline_agents;
    document.getElementById('s-h'  ).textContent=d.agent_status_breakdown.Healthy||0;
    document.getElementById('s-w'  ).textContent=d.agent_status_breakdown.Warning||0;
    document.getElementById('s-c'  ).textContent=d.agent_status_breakdown.Critical||0;
    document.getElementById('s-r'  ).textContent=d.reports_24h;
    document.getElementById('s-d'  ).textContent=d.total_disks;
    const total=d.critical_alerts+d.warning_alerts;
    document.getElementById('s-al').textContent=total;
    try{fetch('/api/stats').then(r=>r.json()).then(d=>{const e=document.getElementById('s-tb');if(e)e.textContent=(d.total_tb||0)+' TB';});}catch(_){}
    const ab=document.getElementById('alertsBadge');
    if(total>0){ab.textContent=total;ab.style.display='';}else{ab.style.display='none';}
  }catch(e){}
}

// ── Sidebar ───────────────────────────────────────────────────────────────────
async function loadAgents(){
  try{
    const resp=await fetch('/api/agents');
    const data=await resp.json();
    if(!resp.ok){document.getElementById('agentList').innerHTML='<div style="color:var(--red);padding:12px;font-size:11px">⚠ Failed to load agents</div>';return;}
    agents=Array.isArray(data)?data:[];
    document.getElementById('sbCnt').textContent=agents.length;
    renderSidebar();
    if(selId)updateBanner();
  }catch(e){document.getElementById('agentList').innerHTML='<div style="color:var(--red);padding:12px;font-size:11px">Network error: '+e.message+'</div>';}
}
function renderSidebar(){
  const q=(document.getElementById('filterInput').value||'').toLowerCase();
  const list=document.getElementById('agentList');
  const filt=agents.filter(a=>!q||a.hostname.toLowerCase().includes(q)||(a.ip||'').includes(q));
  if(!filt.length){list.innerHTML='<div style="color:var(--dim);padding:18px;text-align:center;font-size:12px">No agents match.</div>';return;}
  list.innerHTML=filt.map(a=>{
    const sc=(a.worst_status||'unknown').toLowerCase();
    const sel=a.agent_id===selId?' sel':'';
    const off=a.online?'':' off';
    const alts=(a.crit_alerts>0?'<span class="b-crit-sm">'+a.crit_alerts+' crit</span>':'')+
               (a.warn_alerts>0?'<span class="b-warn-sm" style="margin-left:2px">'+a.warn_alerts+' warn</span>':'');
    return '<div class="agent-card'+sel+off+'" onclick="selectAgent(\''+a.agent_id+'\')"><div class="ac-top"><span class="ac-host">'+esc(a.hostname)+'</span><span class="badge b-'+sc+'">'+sIcon(sc)+' '+(a.worst_status||'?')+'</span></div><div class="ac-mid"><span class="badge b-'+(a.online?'online':'offline')+'">'+(a.online?'● Online':'○ Offline')+'</span><span class="ac-ip">'+esc(a.ip||'—')+'</span><span>'+a.disk_count+' disk'+(a.disk_count!==1?'s':'')+'</span></div>'+(alts?'<div style="margin-top:4px">'+alts+'</div>':'')+'<div class="ac-bottom">'+rel(a.last_seen)+'</div></div>';
  }).join('');
}

// ── Select agent ──────────────────────────────────────────────────────────────
async function selectAgent(id){
  selId=id;renderSidebar();
  document.getElementById('cmdAgentLabel').textContent=(agents.find(a=>a.agent_id===id)||{}).hostname||id;
  await refreshOverview();updateBanner();
  var _at=document.querySelector('.tab.act');
  if(_at){var _tn=_at.dataset.tab;
    if(_tn==='trends')loadTrendsTab();
    else if(_tn==='history')loadHistory();
    else if(_tn==='commands'){refreshPending();loadCmdHistory();}
  }
}
function updateBanner(){
  if(!selId){document.getElementById('bnr').classList.remove('vis');return;}
  const a=agents.find(x=>x.agent_id===selId);if(!a)return;
  const sc=(a.worst_status||'unknown').toLowerCase();
  document.getElementById('bnHost').innerHTML=esc(a.hostname)+' <span class="badge b-'+(a.online?'online':'offline')+'" style="font-size:10px">'+(a.online?'Online':'Offline')+'</span> <span class="badge b-'+sc+'" style="font-size:10px">'+sIcon(sc)+' '+(a.worst_status||'?')+'</span>';
  document.getElementById('bnMeta').innerHTML='<span>🌐 '+esc(a.ip||'—')+'</span><span>💿 '+a.disk_count+' disk'+(a.disk_count!==1?'s':'')+'</span><span>🕐 '+rel(a.last_report)+'</span><span>v'+esc(a.agent_version||'?')+'</span>';
  document.getElementById('bnr').classList.add('vis');
}

// ── Overview ──────────────────────────────────────────────────────────────────
var _renderGen=0;
async function renderOverview(){
  if(!selId){document.getElementById('welcomeMsg').style.display='';document.getElementById('overviewContent').style.display='none';return null;}
  document.getElementById('welcomeMsg').style.display='none';document.getElementById('overviewContent').style.display='';
  var myGen=++_renderGen;
  const r=await fetch('/api/agents/'+selId);if(!r.ok)return null;
  if(myGen!==_renderGen)return null; // stale — a newer call already ran
  const a=await r.json();
  if(myGen!==_renderGen)return null;
  const el=document.getElementById('overviewContent');
  // Save open-state of any existing ecards before wiping
  const openEcards=new Set();
  el.querySelectorAll('.ecard').forEach(function(ec){
    var body=ec.querySelector('.ecard-body');
    if(body&&body.classList.contains('open'))openEcards.add(ec.dataset.eid||'');
  });
  // Remove injected ecards and mark-repl buttons before innerHTML replace
  el.querySelectorAll('.ecard,.mark-repl-btn').forEach(function(e){e.remove();});
  const ic=[['Hostname',a.hostname],['IP',a.ip],['OS',a.os_version],['Agent Version',a.agent_version],
            ['Users',a.logged_users||'—'],['First Seen',fmtDate(a.first_seen)],['Last Report',fmtDate(a.last_report)],['Label',a.welcome_title||'—']]
    .map(([l,v])=>'<div class="info-card"><div class="il">'+l+'</div><div class="iv">'+esc(v||'—')+'</div></div>').join('');
  const alts=(a.alerts||[]).map(al=>'<div class="alert-item '+al.severity+'"><div style="font-size:16px">'+(al.severity==='critical'?'🔴':'⚠️')+'</div><div class="alert-body"><div class="alert-host">'+esc(al.hostname)+'</div><div class="alert-msg">'+esc(al.message)+'</div><div class="alert-meta">'+rel(al.created_at)+'</div></div><div style="display:flex;flex-direction:column;gap:4px;align-items:flex-end"><span class="alert-meta">'+rel(al.created_at)+'</span><button class="btn" onclick="dismissAlert('+al.id+',\''+selId+'\')">Dismiss</button></div></div>').join('');
  el.innerHTML='<div class="sec">System Info</div><div class="info-grid">'+ic+'</div>'
    +(alts?'<div class="sec" style="margin-top:16px">Active Alerts ('+a.alerts.length+')<div class="sec-actions"><button class="btn danger" onclick="dismissAgentAlerts(\''+selId+'\')">Dismiss All</button></div></div>'+alts:'')
    +'<div class="sec" style="margin-top:16px">Disk Health ('+a.disks.length+')</div><div class="disk-grid">'+(a.disks.length?a.disks.map(buildDiskCard).join(''):'<div class="empty-state"><div class="ei">💿</div><p>No disk data yet.<br>Click Refresh Disks.</p></div>')+'</div>';
  return openEcards;
}
// ── refreshOverview: always re-runs _enhanceOverview after render ─────────
var _ovRefreshing=false;
var _ovPending=false;
async function refreshOverview(silent){
  if(_ovRefreshing){_ovPending=true;return;}
  _ovRefreshing=true;
  try{
    var openEcards=await renderOverview();
    while(_ovPending){
      _ovPending=false;
      var r2=await renderOverview();
      if(r2!==null)openEcards=r2;
    }
    if(openEcards!==null)await _enhanceOverview(openEcards,silent);
  }finally{
    _ovRefreshing=false;
    _ovPending=false;
  }
}


// ── Disk card (overview) ──────────────────────────────────────────────────────
function buildDiskCard(d){
  const sc=(d.smart_status||'Unknown').toLowerCase();
  const t=d.temperature;
  let tempHtml='';
  if(t!=null){
    const pct=Math.min(100,Math.max(0,((t-15)/55)*100));
    const tc=t>=60?'hot':t>=45?'warm':'cool';
    tempHtml='<div class="dk-temp"><div class="dk-temp-row"><span class="dk-temp-label">🌡 Temperature</span><span class="dk-temp-val '+tc+'">'+t+'°C</span></div><div class="temp-track"><div class="temp-needle" style="left:'+pct+'%"></div></div><div class="temp-scale"><span>15°</span><span>35°</span><span>50°</span><span>70°</span></div></div>';
  }else{
    tempHtml='<div class="dk-temp"><div class="dk-temp-row"><span class="dk-temp-label">🌡 Temperature</span><span class="dk-temp-val na">N/A</span></div></div>';
  }
  const rows=[];
  function arow(l,v,id,u){if(v==null)return;const cls=mc(id,v);rows.push('<div class="attr-row"><span class="attr-label">'+l+'</span><span class="attr-val '+cls+'">'+(typeof v==='number'?v.toLocaleString():esc(v))+(u||'')+'</span></div>');}
  arow('Reallocated',d.reallocated,'reallocated');arow('Pending',d.pending,'pending');
  arow('Uncorrectable',d.uncorrectable,'uncorrectable');arow('Temp',d.temperature,'temperature','°C');
  arow('Power-On',d.power_on_hours!=null?fmtHours(d.power_on_hours):null,'');
  arow('Wear',d.percentage_used,'percentage_used','%');arow('Spare',d.available_spare,'available_spare','%');
  arow('Media Err',d.media_errors,'media_errors');arow('Host Reads',d.host_reads_gb!=null?d.host_reads_gb.toFixed(1)+'GB':null,'');
  arow('Host Writes',d.host_writes_gb!=null?d.host_writes_gb.toFixed(1)+'GB':null,'');
  let volHtml='';
  if(d.volumes&&d.volumes.length){
    volHtml='<div class="smart-sec"><div class="smart-sec-title">📁 Volumes</div><div class="vol-list">'
      +d.volumes.map(v=>{const pct=v.used_pct||0;const col=barColor(pct);
        return '<div class="vol-item"><div class="vol-top"><span class="vol-drive">'+esc(v.drive)+(v.label?' <span style="color:var(--dim);font-weight:400">'+esc(v.label)+'</span>':'')+'</span><span style="font-size:11px;font-weight:600;color:'+col+'">'+pct.toFixed(1)+'%</span></div><div class="vol-bar-wrap"><div class="vol-bar" style="width:'+Math.min(100,pct)+'%;background:'+col+'"></div></div><div class="vol-info">'+(v.free_gb||'?')+' / '+(v.total_gb||'?')+' GB free</div></div>';
      }).join('')+'</div></div>';
  }
  return '<div class="dk '+sc+'"><div class="dk-hdr"><div><div class="dk-name">'+sIcon(sc)+' '+esc(d.model||'Unknown')+'</div><div class="dk-sub"><span>S/N: '+esc(d.serial||'—')+'</span><span>'+esc(d.interface||'?')+'</span>'+(d.size_gb!=null?'<span>'+d.size_gb+' GB</span>':'')+'</div></div><span class="badge b-'+sc+'">'+esc(d.smart_status||'?')+'</span></div>'
    +tempHtml+'<div class="dk-body">'+(rows.length?'<div class="smart-sec"><div class="smart-sec-title">🛡 SMART Attributes</div>'+rows.join('')+'</div>':'')
    +volHtml+'</div></div>';
}

// ── Alerts ────────────────────────────────────────────────────────────────────
let allAlerts=[];
function setAlertFilter(f){
  alertFilter=f;
  ['all','crit','warn'].forEach(x=>document.getElementById('af-'+x).classList.remove('act'));
  document.getElementById('af-'+(f==='critical'?'crit':f==='warning'?'warn':'all')).classList.add('act');
  renderAlerts();
}
async function loadAlerts(){try{allAlerts=await fetch('/api/alerts').then(r=>r.json());renderAlerts();}catch(e){}}
function renderAlerts(){
  const filt=alertFilter==='all'?allAlerts:allAlerts.filter(a=>a.severity===alertFilter);
  const el=document.getElementById('alertsContent');
  if(!filt.length){el.innerHTML='<div class="empty-state"><div class="ei">✅</div><p>No active alerts.</p></div>';return;}
  el.innerHTML=filt.map(al=>'<div class="alert-item '+al.severity+'"><div style="font-size:16px">'+(al.severity==='critical'?'🔴':'⚠️')+'</div><div class="alert-body"><div class="alert-host">'+esc(al.hostname)+'</div><div class="alert-msg">'+esc(al.message)+'</div><div class="alert-meta">'+fmtDate(al.created_at)+'</div></div><div style="display:flex;flex-direction:column;gap:4px;align-items:flex-end"><span class="alert-meta">'+rel(al.created_at)+'</span><button class="btn" onclick="dismissAlert('+al.id+',\''+al.agent_id+'\')">Dismiss</button></div></div>').join('');
}
async function dismissAlert(id,agentId){
  await fetch('/api/agents/'+agentId+'/dismiss',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({alert_id:id})});
  loadAlerts();loadStats();if(selId===agentId)refreshOverview();
}
async function dismissAgentAlerts(agentId){
  await fetch('/api/agents/'+agentId+'/dismiss',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})});
  loadAlerts();loadStats();refreshOverview();
}
async function dismissAllAlerts(){
  if(!await dlgConfirm('Dismiss All Alerts','This will dismiss all active alerts across all agents.','Dismiss All'))return;
  await fetch('/api/alerts/dismiss_all',{method:'POST'});
  loadAlerts();loadStats();if(selId)refreshOverview();
  toast('All alerts dismissed','ok');
}

// ── History ───────────────────────────────────────────────────────────────────
async function loadHistory(){
  const el=document.getElementById('historyContent');const ha=document.getElementById('histActions');
  if(!selId){el.innerHTML='<div class="empty-state"><div class="ei">📋</div><p>Select an agent.</p></div>';ha.style.display='none';return;}
  ha.style.display='';
  try{
    const rows=await fetch('/api/agents/'+selId+'/history').then(r=>r.json());
    if(!rows.length){el.innerHTML='<div class="empty-state"><div class="ei">📋</div><p>No reports stored.</p></div>';return;}
    el.innerHTML='<div style="background:var(--card);border-radius:var(--r);overflow:hidden;border:1px solid var(--border)"><table class="hist-table"><thead><tr><th>Received</th><th>IP</th><th>Users</th><th>Status</th><th>Disks</th></tr></thead><tbody>'+rows.map(r=>{const sc=(r.worst_status||'unknown').toLowerCase();return'<tr><td>'+fmtDate(r.received_at)+'</td><td>'+esc(r.ip||'—')+'</td><td>'+esc(r.logged_users||'—')+'</td><td><span class="badge b-'+sc+'">'+sIcon(sc)+' '+(r.worst_status||'?')+'</span></td><td>'+r.disk_count+'</td></tr>';}).join('')+'</tbody></table></div>';
  }catch(e){el.innerHTML='<div class="empty-state"><p>Failed to load history.</p></div>';}
}
async function confirmDeleteHistory(){
  if(!selId){toast('Select an agent first','warn');return;}
  const hn=(agents.find(a=>a.agent_id===selId)||{}).hostname||selId;
  if(!await dlgConfirm('Delete History','Delete ALL report history for '+hn+'? This cannot be undone.','Delete All'))return;
  try{
    const r=await fetch('/api/agents/'+selId+'/history',{method:'DELETE'});
    if(r.ok){toast('History deleted','ok');loadHistory();loadStats();}
    else{toast('Delete failed ('+r.status+')','warn');}
  }catch(e){toast('Delete failed: '+e,'warn');}
}

// ── Activity ──────────────────────────────────────────────────────────────────
async function loadActivity(){
  try{
    const d=await fetch('/api/stats').then(r=>r.json());
    const el=document.getElementById('actContent');
    if(!d.activity||!d.activity.length){el.innerHTML='<div class="empty-state"><div class="ei">📋</div><p>No activity recorded.</p></div>';return;}
    el.innerHTML=d.activity.map(e=>'<div class="act-line"><span class="act-ts">'+fmtDate(e.ts)+'</span><span class="act-host">'+esc(e.hostname||'—')+'</span><span class="act-type '+esc(e.event_type)+'">'+esc(e.event_type)+'</span><span class="act-detail">'+esc(e.detail||'')+'</span></div>').join('');
  }catch(e){}
}
async function confirmClearActivity(){
  if(!await dlgConfirm('Clear Activity Log','This will permanently delete all activity log entries.','Clear'))return;
  try{
    const r=await fetch('/api/activity',{method:'DELETE'});
    if(r.ok){toast('Activity log cleared','ok');loadActivity();}
    else{toast('Clear failed ('+r.status+')','warn');}
  }catch(e){toast('Clear failed: '+e,'warn');}
}

// ── Commands ──────────────────────────────────────────────────────────────────
async function sendCmd(action){
  if(!selId){toast('Select an agent first','warn');return;}
  const hn=(agents.find(a=>a.agent_id===selId)||{}).hostname||selId;
  const r=await fetch('/api/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({agent_id:selId,action})});
  const j=await r.json();
  const el=document.getElementById('cmdOut');el.classList.add('vis');
  if(r.ok){el.textContent='[OK] "'+action+'" queued for '+hn+' ('+j.command_id+')';toast('✅ "'+action+'" queued for '+hn,'ok');}
  else{el.textContent='[ERR] '+j.error;toast('❌ '+j.error,'warn');}
  loadCmdHistory();
}
async function sendBulk(action){
  const online=agents.filter(a=>a.online);if(!online.length){toast('No online agents','warn');return;}
  let ok=0,fail=0;
  for(const a of online){const r=await fetch('/api/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({agent_id:a.agent_id,action})});if(r.ok)ok++;else fail++;}
  const el=document.getElementById('bulkOut');el.classList.add('vis');
  el.textContent='[OK] "'+action+'" queued for '+ok+' agent(s)'+(fail?' ('+fail+' failed)':'');
  toast('✅ Bulk "'+action+'": '+ok+' queued','ok');
  loadCmdHistory();
}
async function refreshPending(){
  if(!selId){document.getElementById('pendingList').innerHTML='<div style="color:var(--dim);font-size:11px">No agent selected.</div>';return;}
  try{
    const a=await fetch('/api/agents/'+selId).then(r=>r.json());
    const el=document.getElementById('pendingList');
    if(!a.pending_commands||!a.pending_commands.length){el.innerHTML='<div style="color:var(--dim);font-size:11px">No pending commands.</div>';return;}
    el.innerHTML=a.pending_commands.map(p=>'<div class="pending-item">⏳ <span class="pa">'+esc(p.action)+'</span><span style="color:var(--dim2)">'+rel(p.queued_at)+'</span></div>').join('');
  }catch(e){}
}
async function confirmDeleteAgent(){
  if(!selId)return;
  const hn=(agents.find(a=>a.agent_id===selId)||{}).hostname||selId;
  if(!await dlgConfirm('Remove Agent — '+hn,'This removes the agent and ALL its data from the server.\n\nThis does NOT uninstall it from the remote machine.','Remove'))return;
  await fetch('/api/agents/'+selId,{method:'DELETE'});
  toast('Agent '+hn+' removed','ok');
  selId=null;document.getElementById('bnr').classList.remove('vis');
  document.getElementById('welcomeMsg').style.display='';document.getElementById('overviewContent').style.display='none';
  loadAgents();loadStats();loadAlerts();
}

// ── Command History ───────────────────────────────────────────────────────────
async function loadCmdHistory(){
  const el=document.getElementById('cmdHistContent');
  if(!el)return;
  try{
    const d=await fetch('/api/commands-all').then(r=>r.json());
    const cmds=d.commands||[];
    if(!cmds.length){el.innerHTML='<div style="color:var(--dim);font-size:11px">No command history.</div>';return;}
    el.innerHTML='<div style="background:var(--card);border-radius:var(--r);overflow:hidden;border:1px solid var(--border);overflow-x:auto">'
      +'<table class="cmd-hist-table"><thead><tr><th>Time</th><th>Host</th><th>Action</th><th>Status</th><th>Result</th><th></th></tr></thead>'
      +'<tbody>'+cmds.map(c=>{
        const acked=!!c.acked_at;
        const st=acked?'<span style="color:var(--green);font-weight:700">✓ Acked</span>':'<span style="color:var(--yellow)">⏳ Pending</span>';
        let res='—';
        if(c.result){try{const rj=JSON.parse(c.result);res=JSON.stringify(rj).substring(0,60);}catch{res=String(c.result).substring(0,60);}}
        return '<tr>'
          +'<td>'+fmtDate(c.queued_at)+'</td>'
          +'<td><span style="color:var(--accent2);font-weight:700">'+esc(c.hostname||c.agent_id)+'</span></td>'
          +'<td><span style="color:var(--text);font-weight:700">'+esc(c.action)+'</span></td>'
          +'<td>'+st+(c.acked_at?'<br><span style="font-size:9px;color:var(--dim)">'+fmtDate(c.acked_at)+'</span>':'')+'</td>'
          +'<td style="font-family:Consolas,monospace;font-size:10px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+esc(c.result||'')+'">'+esc(res)+'</td>'
          +'<td><button class="cmd-del-btn" onclick="deleteCmd(\''+c.command_id+'\')">✕</button></td>'
          +'</tr>';
      }).join('')+'</tbody></table></div>';
  }catch(e){el.innerHTML='<div style="color:var(--red);font-size:11px">Failed to load history.</div>';}
}
async function deleteCmd(cmdId){
  await fetch('/api/cmd/delete/'+cmdId,{method:'DELETE'});
  loadCmdHistory();
}
async function confirmClearCmdHistory(){
  if(!await dlgConfirm('Clear Command History','This will permanently delete all command history records.','Clear All'))return;
  try{
    const r=await fetch('/api/cmd/clear-all',{method:'DELETE'});
    if(r.ok){toast('Command history cleared','ok');loadCmdHistory();}
    else{toast('Clear failed ('+r.status+')','warn');}
  }catch(e){toast('Clear failed: '+e,'warn');}
}

// ── Scripts ───────────────────────────────────────────────────────────────────
async function loadScripts(){
  try{
    const d=await fetch('/api/scripts').then(r=>r.json());
    document.getElementById('edAgent').value=d.agent||'';
    document.getElementById('edInstaller').value=d.installer||'';
    document.getElementById('edTray').value=d.tray||'';
    ['agent','installer','tray'].forEach(k=>{
      const m={agent:'stAgent',installer:'stInstaller',tray:'stTray'};
      const el=document.getElementById(m[k]);
      if(el&&d[k])el.innerHTML='<span style="color:var(--dim)">'+d[k].length.toLocaleString()+' bytes — saved to disk</span>';
    });
  }catch(e){toast('Failed to load scripts','warn');}
}
function importScript(input,which){
  const file=input.files[0];if(!file)return;
  const r=new FileReader();
  r.onload=e=>{document.getElementById({agent:'edAgent',installer:'edInstaller',tray:'edTray'}[which]).value=e.target.result;toast('Script imported — click Save.','info');};
  r.readAsText(file);input.value='';
}
async function saveScript(which){
  const m={agent:'edAgent',installer:'edInstaller',tray:'edTray'};
  const sm={agent:'stAgent',installer:'stInstaller',tray:'stTray'};
  const content=document.getElementById(m[which]).value;
  const st=document.getElementById(sm[which]);
  try{
    const r=await fetch('/api/scripts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({[which]:content})});
    const d=await r.json();
    if(r.ok){st.innerHTML='<span style="color:var(--green)">✓ Saved at '+new Date().toLocaleTimeString()+'</span>';toast(which+' saved.','ok');}
    else{st.innerHTML='<span style="color:var(--red)">✗ '+esc(d.error||'save failed')+'</span>';toast('Save failed.','warn');}
  }catch(e){st.innerHTML='<span style="color:var(--red)">✗ '+e.message+'</span>';toast('Save failed.','warn');}
}

// ── Push target helpers ───────────────────────────────────────────────────────
function renderPushTargetCheckboxes(){
  const wrap=document.getElementById('agentCheckboxes');
  if(!wrap)return;
  wrap.innerHTML=agents.map(a=>'<span><input type="checkbox" class="ac-chk" id="chk-'+a.agent_id+'" value="'+a.agent_id+'" '+(a.online?'checked':'')+' onchange="updatePushTarget()"><label for="chk-'+a.agent_id+'" class="ac-chk-lbl">'+(a.online?'● ':'○ ')+esc(a.hostname)+(a.online?'':' (offline)')+'</label></span>').join('');
  updatePushTarget();
}
function updatePushTarget(){
  const mode=document.querySelector('input[name="pushTarget"]:checked').value;
  const wrap=document.getElementById('agentCheckboxes');
  const summary=document.getElementById('pushTargetSummary');
  if(mode==='all'){
    wrap.style.display='none';
    const cnt=agents.filter(a=>a.online).length;
    summary.textContent='Will push to all '+cnt+' online agent'+(cnt!==1?'s':'')+'.';
  }else{
    wrap.style.display='flex';
    const sel=[...document.querySelectorAll('.ac-chk:checked')].map(el=>el.value);
    summary.textContent=sel.length>0?'Will push to '+sel.length+' selected agent'+(sel.length!==1?'s':'')+'.':'No agents selected.';
  }
}
function getTargetAgents(){
  const mode=document.querySelector('input[name="pushTarget"]:checked').value;
  if(mode==='all')return agents.filter(a=>a.online);
  const sel=new Set([...document.querySelectorAll('.ac-chk:checked')].map(el=>el.value));
  return agents.filter(a=>sel.has(a.agent_id));
}

async function saveAndPush(which){
  await saveScript(which);
  const targets=getTargetAgents();
  if(!targets.length){toast('No target agents selected.','info');return;}
  let ok=0;
  for(const a of targets){
    try{const r=await fetch('/api/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({agent_id:a.agent_id,action:'update_agent'})});if(r.ok)ok++;}catch(e){}
  }
  toast('Saved. Update queued for '+ok+' agent'+(ok!==1?'s':'')+'.','ok',7000);
  loadCmdHistory();
}

// ══════════════════════════════════════════════════════════════════════════════
//  ALL AGENTS VIEW
// ══════════════════════════════════════════════════════════════════════════════

function setAvFilter(f){
  avFilter=f;
  ['all','online','offline','critical','warning'].forEach(x=>{const p=document.getElementById('avp-'+x);if(p)p.classList.toggle('act',x===f);});
  renderAllView();
  /* Update pill counts */
  var critCount=agents.filter(function(a){return (a.worst_status||'').toLowerCase()==='critical'||(a.crit_alerts||0)>0;}).length;
  var warnCount=agents.filter(function(a){return (a.worst_status||'').toLowerCase()==='warning'||(a.warn_alerts||0)>0;}).length;
  var pc=document.getElementById('avp-critical');
  var pw=document.getElementById('avp-warning');
  if(pc)pc.textContent='Critical'+(critCount>0?' ('+critCount+')':'');
  if(pw)pw.textContent='Warning'+(warnCount>0?' ('+warnCount+')':'');
}

function renderAllView(){
  const q=(document.getElementById('avSearch').value||'').toLowerCase();
  const list=agents.filter(a=>{
    if(q&&!a.hostname.toLowerCase().includes(q)&&!(a.ip||'').includes(q))return false;
    if(avFilter==='online')   return a.online;
    if(avFilter==='offline')  return !a.online;
    if(avFilter==='critical') return (a.worst_status||'').toLowerCase()==='critical'||(a.crit_alerts||0)>0;
    if(avFilter==='warning')  return (a.worst_status||'').toLowerCase()==='warning'||(a.warn_alerts||0)>0;
    return true;
  });
  const el=document.getElementById('avContent');
  if(!list.length){el.innerHTML='<div class="empty-state"><div class="ei">🔍</div><p>No agents match the current filter.</p></div>';return;}
  el.innerHTML=list.map(a=>buildAvRow(a)).join('');
}

function buildAvRow(a){
  const id=a.agent_id;
  if(!avOpenSections[id])avOpenSections[id]=new Set();
  const sc=(a.worst_status||'unknown').toLowerCase();
  const diskSummary=a.disks&&a.disks.length
    ?a.disks.map(d=>'<span class="badge b-'+(d.smart_status||'unknown').toLowerCase()+'" style="font-size:9px;padding:1px 5px">'+esc(d.model||'Disk')+'</span>').join(' ')
    :'<span style="color:var(--dim2);font-size:10px">No disk data</span>';
  const alertBadge=(a.crit_alerts>0?'<span class="b-crit-sm" style="margin-left:3px">'+a.crit_alerts+' crit</span>':'')+
                   (a.warn_alerts>0?'<span class="b-warn-sm" style="margin-left:2px">'+a.warn_alerts+' warn</span>':'');
  const sections=[
    {key:'disks',    icon:'💿', title:'Disk Health',    summary:a.disk_count+' disk'+(a.disk_count!==1?'s':'')},
    {key:'sysinfo',  icon:'💻', title:'System Info',    summary:esc(a.ip||'—')},
    {key:'alerts',   icon:'⚠️', title:'Alerts',         summary:(a.crit_alerts+a.warn_alerts)>0?(a.crit_alerts+a.warn_alerts)+' active':'None'},
    {key:'history',  icon:'📋', title:'Report History', summary:'Last: '+rel(a.last_report)},
    {key:'commands', icon:'⌘',  title:'Commands',       summary:'Ping / Refresh / Update / Clear'},
  ];
  const secsHtml=sections.map(s=>{
    const open=avOpenSections[id].has(s.key);
    return '<div class="av-sec-row">'
      +'<div class="av-sec-hdr" onclick="toggleAvSec(\''+id+'\',\''+s.key+'\')">'
      +'<span class="av-sec-arrow'+(open?' open':'')+'">▶</span>'
      +'<span style="font-size:13px">'+s.icon+'</span>'
      +'<span class="av-sec-title">'+s.title+'</span>'
      +'<span class="av-sec-summary">'+s.summary+'</span>'
      +'</div>'
      +'<div class="av-sec-body'+(open?' open':'')+'" id="avb-'+id+'-'+s.key+'"><div style="color:var(--dim);font-size:11px;text-align:center;padding:8px">Loading…</div></div>'
      +'</div>';
  }).join('');
  return '<div class="av-agent'+(a.online?'':' offline')+'" id="av-ag-'+id+'">'
    +'<div class="av-agent-hdr">'
    +'<span class="badge b-'+(a.online?'online':'offline')+'">'+(a.online?'● Online':'○ Offline')+'</span>'
    +'<div><div style="display:flex;align-items:center;gap:5px"><span class="av-hname">'+esc(a.hostname)+'</span>'
    +'<span class="badge b-'+sc+'">'+sIcon(sc)+' '+esc(a.worst_status||'?')+'</span>'
    +alertBadge+'</div>'
    +'<div style="margin-top:2px;display:flex;gap:4px;flex-wrap:wrap;align-items:center">'+diskSummary+'</div></div>'
    +'<div class="av-hmeta">'
    +'<span class="mono">'+esc(a.ip||'—')+'</span>'
    +'<span>v'+esc(a.agent_version||'?')+'</span>'
    +(a.logged_users?'<span>👤 '+esc(a.logged_users)+'</span>':'')
    +'<span>🕐 '+rel(a.last_report)+'</span>'
    +'</div>'
    +'<div class="av-hbtns">'
    +'<button class="btn green" style="font-size:10px;padding:3px 8px" onclick="avQuickCmd(\''+id+'\',\'get_disk_health\')">🔄 Refresh</button>'
    +'<button class="btn" style="font-size:10px;padding:3px 8px" onclick="avQuickCmd(\''+id+'\',\'ping\')">Ping</button>'
    +'<button class="btn" style="font-size:10px;padding:3px 8px" onclick="location.href=\'/agent/'+id+'\'">Detail ↗</button>'
    +'</div>'
    +'</div>'
    +'<div class="av-sections">'+secsHtml+'</div>'
    +'</div>';
}

async function toggleAvSec(agentId,sectionKey){
  if(!avOpenSections[agentId])avOpenSections[agentId]=new Set();
  const isOpen=avOpenSections[agentId].has(sectionKey);
  const bodyEl=document.getElementById('avb-'+agentId+'-'+sectionKey);
  const hdrEl=bodyEl&&bodyEl.previousElementSibling;
  const arrow=hdrEl&&hdrEl.querySelector('.av-sec-arrow');
  if(isOpen){
    avOpenSections[agentId].delete(sectionKey);
    if(bodyEl)bodyEl.classList.remove('open');
    if(arrow)arrow.classList.remove('open');
  }else{
    avOpenSections[agentId].add(sectionKey);
    if(bodyEl)bodyEl.classList.add('open');
    if(arrow)arrow.classList.add('open');
    await fillAvSec(agentId,sectionKey,bodyEl);
  }
}

async function fillAvSec(agentId,sectionKey,bodyEl){
  if(!bodyEl)return;
  const a=agents.find(x=>x.agent_id===agentId);
  if(!a){bodyEl.innerHTML='<div style="color:var(--dim);font-size:11px">Agent not found.</div>';return;}

  if(sectionKey==='disks'){
    if(!a.disks||!a.disks.length){
      bodyEl.innerHTML='<div style="color:var(--dim);font-size:11px;text-align:center;padding:8px">No disk data. <button class="btn" style="font-size:10px;padding:2px 7px" onclick="avQuickCmd(\''+agentId+'\',\'get_disk_health\')">Request now</button></div>';
      return;
    }
    bodyEl.innerHTML='<div class="av-disk-grid">'+a.disks.map(buildAvDisk).join('')+'</div>';
  }
  else if(sectionKey==='sysinfo'){
    bodyEl.innerHTML='<div class="av-info-grid">'
      +[['Hostname',a.hostname],['IP',a.ip],['OS',a.os_version],['Agent',a.agent_version],
        ['Users',a.logged_users||'—'],['Label',a.welcome_title||'—'],
        ['First Seen',fmtDate(a.first_seen)],['Last Seen',fmtDate(a.last_seen)],
        ['Last Report',fmtDate(a.last_report)]]
      .map(([l,v])=>'<div class="av-ic"><div class="av-il">'+l+'</div><div class="av-iv">'+esc(v||'—')+'</div></div>').join('')+'</div>';
  }
  else if(sectionKey==='alerts'){
    try{
      const det=await fetch('/api/agents/'+agentId).then(r=>r.json());
      const alts=det.alerts||[];
      if(!alts.length){bodyEl.innerHTML='<div style="color:var(--green);font-size:11px">✅ No active alerts.</div>';return;}
      bodyEl.innerHTML=alts.map(al=>'<div class="av-alert '+al.severity+'"><div style="font-size:13px">'+(al.severity==='critical'?'🔴':'⚠️')+'</div><div class="av-alert-msg">'+esc(al.message)+'</div><div style="display:flex;flex-direction:column;gap:3px;align-items:flex-end;flex-shrink:0"><span style="font-size:10px;color:var(--dim)">'+rel(al.created_at)+'</span><button class="btn" style="font-size:10px;padding:2px 7px" onclick="avDismissAlert('+al.id+',\''+agentId+'\')">Dismiss</button></div></div>').join('')
        +'<div style="margin-top:5px"><button class="btn" style="font-size:10px;padding:3px 9px" onclick="avDismissAll(\''+agentId+'\')">Dismiss All</button></div>';
    }catch(e){bodyEl.innerHTML='<div style="color:var(--red);font-size:11px">Failed to load alerts.</div>';}
  }
  else if(sectionKey==='history'){
    try{
      const rows=await fetch('/api/agents/'+agentId+'/history').then(r=>r.json());
      if(!rows.length){bodyEl.innerHTML='<div style="color:var(--dim);font-size:11px">No history stored.</div>';return;}
      bodyEl.innerHTML='<div style="overflow-x:auto"><table class="av-hist-table"><thead><tr><th>Received</th><th>IP</th><th>Users</th><th>Status</th><th>Disks</th></tr></thead><tbody>'
        +rows.slice(0,20).map(r=>{const sc=(r.worst_status||'unknown').toLowerCase();return'<tr><td>'+fmtDate(r.received_at)+'</td><td>'+esc(r.ip||'—')+'</td><td>'+esc(r.logged_users||'—')+'</td><td><span class="badge b-'+sc+'">'+sIcon(sc)+' '+(r.worst_status||'?')+'</span></td><td>'+r.disk_count+'</td></tr>';}).join('')
        +'</tbody></table></div>'+(rows.length>20?'<div style="font-size:10px;color:var(--dim);margin-top:5px">Showing 20 of '+rows.length+' records</div>':'');
    }catch(e){bodyEl.innerHTML='<div style="color:var(--red);font-size:11px">Failed to load history.</div>';}
  }
  else if(sectionKey==='commands'){
    bodyEl.innerHTML='<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:7px">'
      +'<button class="btn green" style="font-size:11px" onclick="avCmd(\''+agentId+'\',\'get_disk_health\')">🔄 Refresh Disks</button>'
      +'<button class="btn" style="font-size:11px" onclick="avCmd(\''+agentId+'\',\'ping\')">🏓 Ping</button>'
      +'<button class="btn yellow" style="font-size:11px" onclick="avCmd(\''+agentId+'\',\'update_agent\')">⬆ Update Agent</button>'
      +'<button class="btn" style="font-size:11px" onclick="avCmd(\''+agentId+'\',\'clear_log\')">🗑 Clear Log</button>'
      +'</div>'
      +'<div id="avcout-'+agentId+'" style="font-size:11px;font-family:Consolas,monospace;color:var(--accent2);min-height:16px"></div>';
  }
}

function buildAvDisk(d){
  const sc=(d.smart_status||'Unknown').toLowerCase();
  const t=d.temperature;
  let tempHtml='';
  if(t!=null){
    const pct=Math.min(100,Math.max(0,((t-15)/55)*100));
    const tc=t>=60?'hot':t>=45?'warm':'cool';
    tempHtml='<div class="av-dk-temp"><span class="av-tv '+tc+'">'+t+'°C</span><div class="av-tbar"><div class="av-tneedle" style="left:'+pct+'%"></div></div></div>';
  }else{
    tempHtml='<div class="av-dk-temp"><span class="av-tv na">No temp</span><div class="av-tbar"></div></div>';
  }
  const isNvme=d.interface&&d.interface.toUpperCase().includes('NVME');
  const attrs=[];
  function aa(l,v,type){if(v==null)return;attrs.push({l,v:typeof v==='number'?v.toLocaleString():String(v),cls:type||'na'});}
  if(isNvme){
    aa('Spare',d.available_spare!=null?d.available_spare+'%':null,d.available_spare!=null?(d.available_spare<=10?'bad':d.available_spare<=20?'warn':'ok'):'na');
    aa('Wear', d.percentage_used!=null?d.percentage_used+'%':null,d.percentage_used!=null?(d.percentage_used>=90?'bad':d.percentage_used>=75?'warn':'ok'):'na');
    aa('MediaErr',d.media_errors,d.media_errors!=null?(d.media_errors===0?'ok':'bad'):'na');
    aa('Shutdowns',d.unsafe_shutdowns,'na');
  }else{
    aa('Realloc',d.reallocated,d.reallocated!=null?(d.reallocated===0?'ok':'bad'):'na');
    aa('Pending',d.pending,d.pending!=null?(d.pending===0?'ok':'bad'):'na');
    aa('Uncorr',d.uncorrectable,d.uncorrectable!=null?(d.uncorrectable===0?'ok':'bad'):'na');
    aa('Hours',d.power_on_hours!=null?fmtHours(d.power_on_hours):null,'na');
  }
  const attrsHtml='<div class="av-dk-body">'+attrs.slice(0,4).map(a=>'<div class="av-metric"><div class="av-ml">'+esc(a.l)+'</div><div class="av-mv '+a.cls+'">'+esc(a.v)+'</div></div>').join('')+'</div>';
  let volHtml='';
  if(d.volumes&&d.volumes.length){
    volHtml='<div class="av-vols">'+d.volumes.map(v=>{
      const pct=v.used_pct||0;const col=barColor(pct);
      return '<div class="av-vol-row"><span class="av-vd">'+esc(v.drive)+'</span><div class="av-vbg"><div class="av-vb" style="width:'+Math.min(100,pct)+'%;background:'+col+'"></div></div><span class="av-vi" style="color:'+col+'">'+pct.toFixed(0)+'%</span><span class="av-vi" style="color:var(--dim);margin-left:3px">'+(v.free_gb!=null?v.free_gb.toFixed(1):'?')+'/'+(v.total_gb!=null?v.total_gb.toFixed(1):'?')+'GB</span></div>';
    }).join('')+'</div>';
  }
  return '<div class="av-dk '+sc+'"><div class="av-dk-top"><div><div class="av-dk-name">'+sIcon(sc)+' '+esc(d.model||'Unknown')+'</div><div class="av-dk-sub">'+esc(d.interface||'?')+(d.size_gb!=null?' · '+d.size_gb+'GB':'')+(d.serial?' · '+esc(d.serial.slice(0,14)):'')+'</div></div><span class="badge b-'+sc+'">'+esc(d.smart_status||'?')+'</span></div>'
    +tempHtml+attrsHtml+volHtml+'</div>';
}

async function avQuickCmd(agentId,action){
  const hn=(agents.find(a=>a.agent_id===agentId)||{}).hostname||agentId;
  const r=await fetch('/api/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({agent_id:agentId,action})});
  const j=await r.json();
  if(r.ok)toast('✅ "'+action+'" queued for <b>'+esc(hn)+'</b>','ok');
  else toast('❌ '+j.error,'warn');
}
async function avCmd(agentId,action){
  const hn=(agents.find(a=>a.agent_id===agentId)||{}).hostname||agentId;
  const out=document.getElementById('avcout-'+agentId);
  const r=await fetch('/api/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({agent_id:agentId,action})});
  const j=await r.json();
  if(r.ok){if(out)out.textContent='[OK] "'+action+'" queued ('+j.command_id+')';toast('✅ "'+action+'" → '+esc(hn),'ok');}
  else{if(out)out.textContent='[ERR] '+j.error;toast('❌ '+j.error,'warn');}
}
async function avDismissAlert(alertId,agentId){
  await fetch('/api/agents/'+agentId+'/dismiss',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({alert_id:alertId})});
  toast('Alert dismissed','ok');await loadAgents();loadStats();loadAlerts();
  const b=document.getElementById('avb-'+agentId+'-alerts');if(b&&b.classList.contains('open'))await fillAvSec(agentId,'alerts',b);
}
async function avDismissAll(agentId){
  await fetch('/api/agents/'+agentId+'/dismiss',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})});
  toast('All alerts dismissed','ok');await loadAgents();loadStats();loadAlerts();
  const b=document.getElementById('avb-'+agentId+'-alerts');if(b&&b.classList.contains('open'))await fillAvSec(agentId,'alerts',b);
}

// ── SSE ───────────────────────────────────────────────────────────────────────
function connectSSE(){
  const dot=document.getElementById('sseDot'),lbl=document.getElementById('sseLabel');
  const es=new EventSource('/api/stream');
  es.addEventListener('connected',()=>{dot.classList.add('on');lbl.textContent='Live';});
  function refresh(){loadAgents();loadStats();loadAlerts();}
  es.addEventListener('register',e=>{const d=JSON.parse(e.data);toast('🆕 New agent: <b>'+esc(d.hostname)+'</b>','info',8000);refresh();});
  es.addEventListener('report',e=>{
    const d=JSON.parse(e.data);
    if(d.worst_status==='Critical')toast('🔴 <b>'+esc(d.hostname)+'</b> — CRITICAL!','crit',12000);
    refresh();if(selId===d.agent_id)refreshOverview(true);
    const b=document.getElementById('avb-'+d.agent_id+'-disks');
    if(b&&b.classList.contains('open'))setTimeout(()=>{const a=agents.find(x=>x.agent_id===d.agent_id);if(a)fillAvSec(d.agent_id,'disks',b);},900);
  });
  es.addEventListener('alert',e=>{const d=JSON.parse(e.data);toast((d.severity==='critical'?'🔴':'⚠️')+' <b>'+esc(d.hostname)+'</b>: '+esc(d.message),d.severity==='critical'?'crit':'warn',12000);loadAlerts();loadStats();});
  es.addEventListener('ack',e=>{const d=JSON.parse(e.data);toast('✅ <b>'+esc(d.hostname||d.agent_id)+'</b> ack\'d "'+esc(d.action)+'"','ok');if(selId===d.agent_id){refreshOverview();refreshPending();}});
  es.addEventListener('offline',e=>{const d=JSON.parse(e.data);toast('⚫ <b>'+esc(d.hostname)+'</b> went offline','warn');refresh();});
  es.addEventListener('online', e=>{const d=JSON.parse(e.data);if(d.hostname)toast('🟢 <b>'+esc(d.hostname)+'</b> is back online','ok');refresh();});
  es.addEventListener('alerts_updated',()=>{loadAlerts();loadStats();loadAgents();});
  es.addEventListener('agent_removed',e=>{const d=JSON.parse(e.data);if(selId===d.agent_id)selId=null;refresh();});
  es.onerror=()=>{dot.classList.remove('on');lbl.textContent='Reconnecting…';es.close();setTimeout(connectSSE,3000);};
}

(async function(){
  try{await loadAgents();}catch(e){}
  try{await loadStats();}catch(e){}
  try{loadAlerts();}catch(e){}
  connectSSE();
  setInterval(async()=>{
    try{await loadAgents();}catch(e){}
    try{await loadStats();}catch(e){}
    try{await loadAlerts();}catch(e){}
    if(selId)try{await refreshOverview(true);}catch(e){}
  },30000);
})();

// ---- Trends ----
var trendHours=24,trendDiskSel=null,trendCharts={};
var METRIC_CFG={
  temp:     {label:'Temperature',  unit:'°C',color:'#38BDF8',yMin:0,yMax:null},
  realloc:  {label:'Reallocated',  unit:'',       color:'#EF4444',yMin:0,yMax:null},
  pending:  {label:'Pending',      unit:'',       color:'#F59E0B',yMin:0,yMax:null},
  uncorr:   {label:'Uncorrect.',   unit:'',       color:'#EF4444',yMin:0,yMax:null},
  media_err:{label:'Media Err',    unit:'',       color:'#EF4444',yMin:0,yMax:null},
  wear_pct: {label:'SSD Wear',     unit:'%',      color:'#F59E0B',yMin:0,yMax:100},
  spare_pct:{label:'Spare',        unit:'%',      color:'#22C55E',yMin:0,yMax:100,invertBad:true},
  hours:    {label:'Power-On Hrs', unit:'h',      color:'#A78BFA',yMin:0,yMax:null},
};
var METRIC_ORDER=['temp','realloc','pending','uncorr','media_err','wear_pct','spare_pct','hours'];

function getTrendStatus(metric,val){
  if(val===null||val===undefined)return 'trend-na';
  var thresh={temp:{warn:45,crit:60},realloc:{warn:1,crit:5},pending:{warn:1,crit:5},
    uncorr:{warn:1,crit:1},media_err:{warn:1,crit:5},
    wear_pct:{warn:75,crit:90},spare_pct:{warn:20,crit:10,invertBad:true}};
  var t=thresh[metric];if(!t)return 'trend-ok';
  if(t.invertBad){if(val<=t.crit)return 'trend-crit';if(val<=t.warn)return 'trend-warn';return 'trend-ok';}
  if(val>=t.crit)return 'trend-crit';if(val>=t.warn)return 'trend-warn';return 'trend-ok';
}
function setTrendWindow(h){
  trendHours=h;
  document.querySelectorAll('#trendWindowBtns .filter-btn').forEach(function(b){b.classList.toggle('act',parseInt(b.dataset.h)===h);});
  if(trendDiskSel)loadTrendCharts(trendDiskSel);
}
async function loadTrendsTab(){
  var lbl=document.getElementById('trendsAgentLabel');
  if(!selId){if(lbl)lbl.textContent='Select an agent first';return;}
  var a=agents.find(function(x){return x.agent_id===selId;});
  if(lbl)lbl.textContent=(a?a.hostname:selId)+' disk trends';
  try{
    var disks=await fetch('/api/trends/'+selId).then(function(r){return r.json();});
    if(!disks.length){
      document.getElementById('trendsContent').innerHTML='<div class="empty-state"><p>No trend data yet. Records automatically each report.</p></div>';
      document.getElementById('trendDiskTabs').innerHTML='';return;
    }
    if(!trendDiskSel||!disks.find(function(d){return d.disk_serial===trendDiskSel;}))trendDiskSel=disks[0].disk_serial;
    renderTrendDiskTabs(disks);await loadTrendCharts(trendDiskSel);
  }catch(e){document.getElementById('trendsContent').innerHTML='<div style="color:var(--red);padding:20px">Error: '+esc(e.message)+'</div>';}
}
function renderTrendDiskTabs(disks){
  var el=document.getElementById('trendDiskTabs');if(!el)return;
  el.innerHTML=disks.map(function(d){
    var active=d.disk_serial===trendDiskSel?' act':'';
    var safe=encodeURIComponent(d.disk_serial);
    return '<button class="filter-btn'+active+'" onclick="selectTrendDisk(decodeURIComponent(\''+safe+'\'))">'+esc(d.disk_model||d.disk_serial)+'</button>';
  }).join('');
}
async function selectTrendDisk(serial){
  trendDiskSel=serial;
  try{var disks=await fetch('/api/trends/'+selId).then(function(r){return r.json();});renderTrendDiskTabs(disks);}catch(e){}
  await loadTrendCharts(serial);
}
async function loadTrendCharts(serial){
  var el=document.getElementById('trendsContent');
  el.innerHTML='<div style="color:var(--dim);font-size:12px;padding:20px;text-align:center">Loading...</div>';
  var data;
  try{
    data=await fetch('/api/trends/'+selId+'/'+encodeURIComponent(serial)+'?hours='+trendHours)
      .then(function(r){return r.json();});
  }catch(e){
    el.innerHTML='<div style="color:var(--red);padding:20px">Failed: '+esc(e.message)+'</div>';
    return;
  }
  Object.keys(trendCharts).forEach(function(k){try{trendCharts[k].destroy();}catch(_){}});
  trendCharts={};

  var series=data.series||{};
  var present=METRIC_ORDER.filter(function(m){return series[m]&&series[m].length>0;});
  if(!present.length){
    el.innerHTML='<div class="empty-state"><p>No data in this window. Try a wider range.</p></div>';
    return;
  }

  var strip=present.map(function(m){
    var cfg=METRIC_CFG[m]||{label:m,unit:''};
    var pts=series[m],last=pts[pts.length-1];
    var lv=last?last.v.toFixed(1):'-';
    var sc=getTrendStatus(m,last?last.v:null);
    return '<div class="trend-summary-card '+sc+'"><div class="ts-label">'+cfg.label+'</div><div class="ts-val">'+lv+(cfg.unit||'')+'</div></div>';
  }).join('');

  var cards=present.map(function(m){
    var cfg=METRIC_CFG[m]||{label:m,unit:''};
    var pts=series[m],last=pts[pts.length-1];
    var lv=last?last.v.toFixed(1):'-';
    var sc=getTrendStatus(m,last?last.v:null);
    return '<div class="trend-card">'
      +'<div class="trend-card-hdr">'
      +'<span class="trend-metric-name">'+cfg.label+'</span>'
      +'<span class="trend-latest '+sc+'">'+lv+(cfg.unit||'')+'</span>'
      +'<span class="trend-pts">'+pts.length+' pts</span>'
      +'</div>'
      +'<div class="trend-delta" id="td-'+m+'"></div>'
      +'<div class="trend-canvas-wrap"><canvas id="canvas-'+m+'"></canvas></div>'
      +'</div>';
  }).join('');

  el.innerHTML='<div class="trend-summary-strip">'+strip+'</div>'
    +'<div class="trend-grid">'+cards+'</div>';

  function drawCharts(){
    if(typeof Chart==='undefined'){console.error('Chart.js not loaded');return;}
    present.forEach(function(m){
      var cfg=METRIC_CFG[m]||{label:m,unit:'',color:'#7C3AED'};
      var pts=series[m];
      var canvas=document.getElementById('canvas-'+m);
      if(!canvas)return;
      var wrap=canvas.parentElement;

      /* Force explicit pixel size — CSS grid may not have painted yet */
      var W=wrap?wrap.clientWidth:0;
      var H=wrap?wrap.clientHeight:0;
      if(W<10){
        var card=canvas.closest('.trend-card');
        W=card?card.clientWidth-28:600;
      }
      if(H<10)H=160;
      canvas.width=W;
      canvas.height=H;
      canvas.style.display='block';
      canvas.style.width=W+'px';
      canvas.style.height=H+'px';

      /* Destroy previous instance */
      if(trendCharts['canvas-'+m]){
        try{trendCharts['canvas-'+m].destroy();}catch(_){}
        delete trendCharts['canvas-'+m];
      }

      var vals=pts.map(function(p){return p.v;});
      var sc=getTrendStatus(m,vals[vals.length-1]);
      var lc=sc==='trend-crit'?'#EF4444':sc==='trend-warn'?'#F59E0B':(cfg.color||'#7C3AED');
      var labels=pts.map(function(p){
        try{
          var d=new Date(p.ts),now=new Date();
          return d.toDateString()===now.toDateString()
            ?d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})
            :(d.getMonth()+1)+'/'+d.getDate();
        }catch(_){return p.ts.substring(11,16);}
      });

      try{
        trendCharts['canvas-'+m]=new Chart(canvas,{
          type:'line',
          data:{
            labels:labels,
            datasets:[{
              data:vals,
              borderColor:lc,
              backgroundColor:lc+'22',
              borderWidth:2,
              pointRadius:pts.length>200?0:pts.length>60?1:3,
              pointHoverRadius:5,
              fill:true,
              tension:0.35
            }]
          },
          options:{
            responsive:false,
            animation:{duration:200},
            interaction:{intersect:false,mode:'index'},
            plugins:{
              legend:{display:false},
              tooltip:{
                backgroundColor:'#1E1E2E',
                borderColor:'#303058',
                borderWidth:1,
                titleColor:'#A78BFA',
                bodyColor:'#E0E0FF',
                callbacks:{
                  label:function(i){return ' '+i.raw.toFixed(1)+(cfg.unit||'');}
                }
              }
            },
            scales:{
              x:{ticks:{color:'#4A4A78',font:{size:9},maxTicksLimit:8,maxRotation:0},
                grid:{color:'rgba(255,255,255,0.05)'}},
              y:{min:cfg.yMin,max:cfg.yMax,
                ticks:{color:'#4A4A78',font:{size:9},maxTicksLimit:5},
                grid:{color:'rgba(255,255,255,0.05)'}}
            }
          }
        });
      }catch(err){console.error('Chart error '+m+':',err);}

      /* Delta label */
      var first=vals[0],last2=vals[vals.length-1],delta=last2-first;
      var delEl=document.getElementById('td-'+m);
      if(delEl&&pts.length>=2){
        var isBad=cfg.invertBad?delta<0:delta>0;
        var col=delta===0?'var(--dim)':isBad?'var(--red)':'var(--green)';
        var sign=delta>0?'+':delta<0?'-':'';
        delEl.innerHTML='<span style="color:'+col+';font-size:10px">'+sign+Math.abs(delta).toFixed(1)+(cfg.unit||'')+' over '+trendHours+'h</span>';
      }
    });
  }

  /* Three attempts: 80ms, 350ms, 800ms */
  setTimeout(drawCharts, 80);
  setTimeout(drawCharts, 350);
  setTimeout(drawCharts, 800);
}


// ---- Heatmap ----
var _hmSelected=null;
function _hmSortAgents(list){
  var sv=(document.getElementById('hmSortSel')||{}).value||'status';
  var ord={critical:0,warning:1,unknown:2,healthy:3};var copy=list.slice();
  if(sv==='status')copy.sort(function(a,b){var ao=!a.online?4:(ord[(a.worst_status||'unknown').toLowerCase()]??2);var bo=!b.online?4:(ord[(b.worst_status||'unknown').toLowerCase()]??2);return ao-bo;});
  else if(sv==='name')copy.sort(function(a,b){return(a.hostname||'').localeCompare(b.hostname||'');});
  else if(sv==='temp')copy.sort(function(a,b){var at=Math.max.apply(null,(a.disks||[]).map(function(d){return d.temperature||0;}));var bt=Math.max.apply(null,(b.disks||[]).map(function(d){return d.temperature||0;}));return bt-at;});
  else if(sv==='offline')copy.sort(function(a,b){return(a.online===b.online)?0:a.online?-1:1;});
  return copy;
}
function renderHeatmap(){
  var el=document.getElementById('heatmapContent');if(!el)return;
  if(!agents||!agents.length){el.innerHTML='<div class="empty-state"><p>No agents registered yet.</p></div>';return;}
  var online=agents.filter(function(a){return a.online;}).length;
  var crit=agents.filter(function(a){return(a.worst_status||'').toLowerCase()==='critical'&&a.online;}).length;
  var warn=agents.filter(function(a){return(a.worst_status||'').toLowerCase()==='warning'&&a.online;}).length;
  var disks=agents.reduce(function(s,a){return s+a.disk_count;},0);
  var tgb=agents.reduce(function(s,a){return s+(a.disks||[]).reduce(function(ss,d){return ss+(d.size_gb||0);},0);},0);
  var sc=crit>0?'var(--red)':warn>0?'var(--yellow)':'var(--green)';
  var fb='<div class="hm-fleet-bar">'+_hmStat(agents.length,'Machines','#F0F0FF')+_hmStat(online,'Online','var(--green)')+_hmStat(agents.length-online,'Offline','var(--red)')+'<div style="width:1px;background:var(--border);align-self:stretch;margin:0 4px"></div>'+_hmStat(crit,'Critical','var(--red)')+_hmStat(warn,'Warning','var(--yellow)')+'<div style="width:1px;background:var(--border);align-self:stretch;margin:0 4px"></div>'+_hmStat(disks,'Disks','var(--accent2)')+_hmStat((tgb/1024).toFixed(1)+' TB','Storage','#F0F0FF')+'<div style="flex:1"></div><div style="align-self:center;font-size:11px;color:var(--dim)">Fleet: <strong style="color:'+sc+'">'+(crit>0?crit+' CRITICAL':warn>0?warn+' Warning':'All Clear')+'</strong></div></div>';
  var leg='<div class="hm-legend"><span style="font-size:10px;color:var(--dim);font-weight:600">Status:</span>'+_hmLegend('#22C55E','Healthy')+_hmLegend('#F59E0B','Warning')+_hmLegend('#EF4444','Critical')+_hmLegend('#6B7280','Unknown')+_hmLegend('#3D3060','Offline')+'</div>';
  var sorted=_hmSortAgents(agents);
  var cells=sorted.map(function(a){return _hmBuildCell(a);}).join('');
  el.innerHTML=fb+leg+'<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:8px;margin-bottom:14px">'+cells+'</div><div id="hmDetailSlot"></div>';
  if(_hmSelected){var still=agents.find(function(a){return a.agent_id===_hmSelected;});if(still)setTimeout(function(){hmSelect(_hmSelected);},50);else _hmSelected=null;}
}
function _hmStat(n,label,color){return '<div class="hm-fleet-stat"><div class="n" style="color:'+color+'">'+n+'</div><div class="l">'+label+'</div></div>';}
function _hmLegend(color,label){return '<span class="hm-legend-item"><span class="hm-legend-dot" style="background:'+color+'"></span><span style="font-size:10px;color:var(--dim)">'+label+'</span></span>';}
function _hmBuildCell(a){
  var sc=(a.worst_status||'unknown').toLowerCase(),sk=a.online?sc:'offline';
  var cm={healthy:'#22C55E',warning:'#F59E0B',critical:'#EF4444',unknown:'#6B7280',offline:'#3D3060'};
  var tm={healthy:'#fff',warning:'#1a1a2e',critical:'#fff',unknown:'#fff',offline:'#9090B8'};
  var bg=cm[sk]||'#3B3B5C',fg=tm[sk]||'#fff';
  var temps=(a.disks||[]).map(function(d){return d.temperature||0;}).filter(function(t){return t>0;});
  var maxT=temps.length?Math.max.apply(null,temps):null;
  var tStr=maxT?maxT+'°C':'';
  var tCol=maxT>=60?'#FFAAAA':maxT>=45?'#FFDAA0':'inherit';
  var aB=(a.crit_alerts+a.warn_alerts)>0?'<div style="position:absolute;top:5px;right:5px;background:rgba(0,0,0,.5);color:#fff;border-radius:8px;padding:0 5px;font-size:9px;font-weight:800;line-height:16px">'+(a.crit_alerts+a.warn_alerts)+'!</div>':'';
  var st={healthy:'OK',warning:'WARN',critical:'CRIT',unknown:'?',offline:'OFF'};
  var pulse=sk==='critical'?' hm-pulse':'';
  return '<div class="hm-cell'+pulse+'" id="hmcell-'+a.agent_id+'" style="background:'+bg+';color:'+fg+'" onclick="hmSelect(\''+a.agent_id+'\')" title="'+esc(a.hostname)+' - '+(a.online?a.worst_status:'Offline')+'">'+aB+'<div style="font-size:11px;font-weight:900;letter-spacing:1px">'+(st[sk]||'?')+'</div><div class="hm-cell-name" style="color:'+fg+'">'+esc(a.hostname.substring(0,14))+'</div><div class="hm-cell-ip" style="color:'+fg+'">'+esc(a.ip||'-')+'</div><div class="hm-cell-meta"><span class="hm-cell-badge">'+a.disk_count+' disks</span>'+(tStr?'<span class="hm-cell-badge" style="color:'+tCol+'">'+tStr+'</span>':'')+'</div></div>';
}
function hmSelect(agentId){
  var slot=document.getElementById('hmDetailSlot');if(!slot)return;
  if(_hmSelected===agentId){_hmSelected=null;slot.innerHTML='';document.querySelectorAll('.hm-cell').forEach(function(c){c.classList.remove('hm-selected');});return;}
  _hmSelected=agentId;
  document.querySelectorAll('.hm-cell').forEach(function(c){c.classList.remove('hm-selected');});
  var cellEl=document.getElementById('hmcell-'+agentId);if(cellEl)cellEl.classList.add('hm-selected');
  var a=agents.find(function(x){return x.agent_id===agentId;});if(!a)return;
  var sc=(a.worst_status||'unknown').toLowerCase();
  var onl=a.online?'<span class="badge b-online">Online</span>':'<span class="badge b-offline">Offline</span>';
  var dc=(a.disks||[]).length?(a.disks||[]).map(function(d){
    var ds=(d.smart_status||'Unknown').toLowerCase();
    var t=d.temperature,tc=t>=60?'bad':t>=45?'warn':'ok';
    var at=[t!=null?{l:'Temp',v:t+'°C',c:tc}:null,d.reallocated!=null?{l:'Realloc',v:d.reallocated,c:d.reallocated>0?'bad':'ok'}:null,d.pending!=null?{l:'Pending',v:d.pending,c:d.pending>0?'bad':'ok'}:null,d.percentage_used!=null?{l:'Wear',v:d.percentage_used+'%',c:d.percentage_used>=90?'bad':d.percentage_used>=75?'warn':'ok'}:null,d.power_on_hours!=null?{l:'Hours',v:fmtHours(d.power_on_hours),c:'ok'}:null].filter(Boolean);
    var vs=(d.volumes||[]).map(function(v){var p=v.used_pct||0;return '<span class="hm-disk-attr '+(p>=90?'bad':p>=75?'warn':'ok')+'">'+esc(v.drive)+' '+p.toFixed(0)+'%</span>';}).join('');
    return '<div class="hm-disk '+ds+'"><div class="hm-disk-name"><span class="badge b-'+ds+'" style="font-size:9px;margin-right:5px">'+(d.smart_status||'?')+'</span>'+esc(d.model||'Unknown')+'</div><div style="font-size:10px;color:var(--dim);margin-bottom:5px">'+esc(d.serial||'-')+' - '+esc(d.interface||'?')+(d.size_gb?' - '+d.size_gb+' GB':'')+'</div><div class="hm-disk-attrs">'+at.map(function(x){return '<span class="hm-disk-attr '+x.c+'">'+x.l+': '+esc(String(x.v))+'</span>';}).join('')+vs+'</div></div>';
  }).join(''):'<div style="color:var(--dim);font-size:11px;padding:8px">No disk data - click Refresh.</div>';
  var as2=(a.crit_alerts+a.warn_alerts)>0?'<span style="color:var(--red);font-size:11px;font-weight:700">ALERTS: '+(a.crit_alerts>0?a.crit_alerts+' critical ':'')+( a.warn_alerts>0?a.warn_alerts+' warning':'')+'</span>':'<span style="color:var(--green);font-size:11px">No active alerts</span>';
  slot.innerHTML='<div class="hm-detail"><div class="hm-detail-hdr"><div><div style="display:flex;align-items:center;gap:7px;flex-wrap:wrap"><span class="hm-detail-name">'+esc(a.hostname)+'</span>'+onl+'<span class="badge b-'+sc+'">'+(a.worst_status||'?')+'</span></div><div style="font-size:11px;color:var(--dim);margin-top:3px;display:flex;gap:12px;flex-wrap:wrap"><span>'+esc(a.ip||'-')+'</span><span>'+esc(a.os_version||'?')+'</span><span>v'+esc(a.agent_version||'?')+'</span><span>Last: '+rel(a.last_seen)+'</span></div><div style="margin-top:6px">'+as2+'</div></div><div style="margin-left:auto;display:flex;gap:5px;flex-wrap:wrap;align-items:flex-start"><button class="btn green" onclick="hmGoOverview(\''+agentId+'\')">Overview</button><button class="btn" onclick="hmGoAlerts(\''+agentId+'\')">Alerts</button><button class="btn" onclick="hmCmd(\''+agentId+'\',\'get_disk_health\')">Refresh</button><button class="btn" onclick="hmCmd(\''+agentId+'\',\'ping\')">Ping</button><button class="btn" onclick="document.getElementById(\'hmDetailSlot\').innerHTML=\'\';document.querySelectorAll(\'.hm-cell\').forEach(function(c){c.classList.remove(\'hm-selected\')});_hmSelected=null">X</button></div></div><div class="hm-detail-disks">'+dc+'</div></div>';
  setTimeout(function(){slot.scrollIntoView({behavior:'smooth',block:'nearest'});},100);
}
function hmGoOverview(id){selectAgent(id);switchTab('overview');}
function hmGoAlerts(id){selectAgent(id);switchTab('alerts');}
async function hmCmd(id,action){
  var hn=(agents.find(function(a){return a.agent_id===id;})||{}).hostname||id;
  var r=await fetch('/api/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({agent_id:id,action:action})});
  var j=await r.json();
  if(r.ok)toast('Queued "'+action+'" for '+esc(hn),'ok');else toast('Error: '+j.error,'warn');
}

// ---- Analytics ----
var _analyticsCharts={},_analyticsWindow=7;
function setAnalyticsWindow(d){_analyticsWindow=d;document.querySelectorAll('#analyticsWindowBtns .filter-btn').forEach(function(b){b.classList.toggle('act',parseInt(b.dataset.d)===d);});loadAnalyticsTab();}
async function loadAnalyticsTab(){
  var el=document.getElementById('analyticsContent');if(!el)return;
  el.innerHTML='<div style="color:var(--dim);font-size:12px;padding:20px;text-align:center">Loading...</div>';
  try{
    var stats=await fetch('/api/stats').then(function(r){return r.json();});
    var reps=await fetch('/api/analytics/reports_daily?days='+_analyticsWindow).then(function(r){return r.json();}).catch(function(){return [];});
    var alts=await fetch('/api/analytics/alerts_daily?days='+_analyticsWindow).then(function(r){return r.json();}).catch(function(){return [];});
    Object.values(_analyticsCharts).forEach(function(c){try{c.destroy();}catch(_){}});_analyticsCharts={};
    var sb=stats.agent_status_breakdown||{};
    el.innerHTML='<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px">'+[['total_agents','#F0F0FF','Total'],['online_agents','#22C55E','Online'],['offline_agents','#EF4444','Offline'],['warning_alerts','#F59E0B','Warnings'],['critical_alerts','#EF4444','Critical'],['reports_24h','#38BDF8','24h Reports'],['total_disks','#A78BFA','Disks'],['total_tb','#F0F0FF','TB']].map(function(kv){return '<div style="background:var(--card);border:1px solid var(--border);border-radius:var(--rs);padding:11px 16px;min-width:90px;text-align:center"><div style="font-size:22px;font-weight:800;color:'+kv[1]+'">'+(stats[kv[0]]||0)+'</div><div style="font-size:9px;color:var(--dim);text-transform:uppercase;margin-top:3px">'+kv[2]+'</div></div>';}).join('')+'</div>'
      +'<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:12px">'
      +'<div style="background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:12px 14px"><div style="font-size:11px;font-weight:700;color:#F0F0FF;margin-bottom:8px">Reports per Day</div><div style="height:160px;position:relative;min-height:160px"><canvas id="canvasReports"></canvas></div></div>'
      +'<div style="background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:12px 14px"><div style="font-size:11px;font-weight:700;color:#F0F0FF;margin-bottom:8px">Alerts per Day</div><div style="height:160px;position:relative;min-height:160px"><canvas id="canvasAlerts"></canvas></div></div>'
      +'<div style="background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:12px 14px"><div style="font-size:11px;font-weight:700;color:#F0F0FF;margin-bottom:8px">Online Events / Day</div><div style="height:160px;position:relative;min-height:160px"><canvas id="canvasOnline"></canvas></div></div>'+'<div style="background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:12px 14px"><div style="font-size:11px;font-weight:700;color:#F0F0FF;margin-bottom:8px">Online Events / Day</div><div style="height:160px;position:relative;min-height:160px"><canvas id="canvasOnline"></canvas></div></div>'+'<div style="background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:12px 14px"><div style="font-size:11px;font-weight:700;color:#F0F0FF;margin-bottom:8px">Disk Status</div><div style="height:160px;position:relative;min-height:160px"><canvas id="canvasDonut"></canvas></div><div id="donutLegend" style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px"></div></div>'
      +'</div>';
    var opts={responsive:false,animation:{duration:200},plugins:{legend:{display:false},tooltip:{backgroundColor:'#1E1E2E',borderColor:'#303058',borderWidth:1,titleColor:'#A78BFA',bodyColor:'#E0E0FF'}},scales:{x:{ticks:{color:'#4A4A78',font:{size:9},maxTicksLimit:8},grid:{color:'#252540'}},y:{ticks:{color:'#4A4A78',font:{size:9},maxTicksLimit:5},grid:{color:'#252540'},beginAtZero:true}}};
    setTimeout(function(){
      /* Size canvases before Chart.js renders */
      ['canvasReports','canvasAlerts','canvasOnline','canvasDonut'].forEach(function(cid){
        var cv=document.getElementById(cid);if(!cv)return;
        var p=cv.parentElement;if(!p)return;
        var W=p.clientWidth||640,H=p.clientHeight||160;
        if(W<10)W=640;if(H<10)H=160;
        cv.width=W;cv.height=H;cv.style.width=W+'px';cv.style.height=H+'px';
      });
      var actData=typeof act!=='undefined'?act:[];
      var oc=document.getElementById('canvasOnline');
      if(oc&&typeof Chart!=='undefined'){
        if(_analyticsCharts.canvasOnline){try{_analyticsCharts.canvasOnline.destroy();}catch(_){}}
        _analyticsCharts.canvasOnline=new Chart(oc,{type:'line',data:{labels:actData.map(function(p){return p.date||'';}),datasets:[{data:actData.map(function(p){return p.online_events||0;}),borderColor:'#22C55E',backgroundColor:'#22C55E18',borderWidth:1.5,fill:true,tension:0.3,pointRadius:3}]},options:opts});
      }
      var actData=typeof act!=='undefined'?act:[];
      var oc=document.getElementById('canvasOnline');
      if(oc&&typeof Chart!=='undefined'){
        if(_analyticsCharts.canvasOnline){try{_analyticsCharts.canvasOnline.destroy();}catch(_){}}
        _analyticsCharts.canvasOnline=new Chart(oc,{type:'line',data:{labels:actData.map(function(p){return p.date||'';}),datasets:[{data:actData.map(function(p){return p.online_events||0;}),borderColor:'#22C55E',backgroundColor:'#22C55E18',borderWidth:1.5,fill:true,tension:0.3,pointRadius:3}]},options:opts});
      }
      [['canvasReports',reps,'#38BDF8'],['canvasAlerts',alts,'#EF4444']].forEach(function(kv){
        var c=document.getElementById(kv[0]);if(!c||typeof Chart==='undefined')return;
        _analyticsCharts[kv[0]]=new Chart(c,{type:'bar',data:{labels:kv[1].map(function(p){return p.date||'';}),datasets:[{data:kv[1].map(function(p){return p.count||0;}),backgroundColor:kv[2]+'55',borderColor:kv[2],borderWidth:1.5,borderRadius:3}]},options:opts});
      });
      var dc=document.getElementById('canvasDonut');
      if(dc&&typeof Chart!=='undefined'){
        var dd=[sb.Healthy||0,sb.Warning||0,sb.Critical||0,sb.Unknown||0];
        var dl=['Healthy','Warning','Critical','Unknown'],dco=['#22C55E','#F59E0B','#EF4444','#6B7280'];
        _analyticsCharts.canvasDonut=new Chart(dc,{type:'doughnut',data:{labels:dl,datasets:[{data:dd,backgroundColor:dco,borderColor:'#1E1E2E',borderWidth:2}]},options:{responsive:true,maintainAspectRatio:false,cutout:'65%',plugins:{legend:{display:false},tooltip:{backgroundColor:'#1E1E2E'}}}});
        var leg=document.getElementById('donutLegend');
        if(leg)leg.innerHTML=dl.map(function(l,i){return dd[i]>0?'<span style="font-size:10px;color:var(--dim);display:flex;align-items:center;gap:4px"><span style="width:8px;height:8px;border-radius:50%;background:'+dco[i]+';display:inline-block"></span>'+l+' <b>'+dd[i]+'</b></span>':'';}).join('');
      }
    });
  }catch(e){el.innerHTML='<div style="color:var(--red);padding:20px">Failed: '+e.message+'</div>';}
}

// ---- Settings ----
async function loadSettingsPane(){
  var el=document.getElementById('settingsContent');if(!el)return;
  var s=await fetch('/api/settings').then(function(r){return r.json();});
  var st=s.settings||{};

  /* -- Alert Thresholds -- */
  var tf=[
    ['temp','Temperature','°C','thresh_temp_warn','thresh_temp_crit'],
    ['realloc','Reallocated','sectors','thresh_realloc_warn','thresh_realloc_crit'],
    ['pending','Pending','sectors','thresh_pending_warn','thresh_pending_crit'],
    ['uncorr','Uncorrectable','sectors','thresh_uncorr_warn','thresh_uncorr_crit'],
    ['wear','SSD Wear','%','thresh_wear_warn','thresh_wear_crit'],
    ['spare','Spare %','%','thresh_spare_warn','thresh_spare_crit'],
  ];
  var tr=tf.map(function(f){
    return '<div class="set-row">'
      +'<span class="set-label">'+f[1]+'</span>'
      +'<label style="font-size:11px;color:var(--dim);display:flex;align-items:center;gap:5px">'
      +'Warn <input class="set-input" type="number" id="st-'+f[3]+'" value="'+(st[f[3]]||'')+'" style="width:64px"> '+f[2]+'</label>'
      +'<label style="font-size:11px;color:var(--red);display:flex;align-items:center;gap:5px">'
      +'Crit <input class="set-input" type="number" id="st-'+f[4]+'" value="'+(st[f[4]]||'')+'" style="width:64px"> '+f[2]+'</label>'
      +'</div>';
  }).join('');

  /* -- General -- */
  var gr='<div class="set-row">'
    +'<span class="set-label">Offline threshold</span>'
    +'<label style="font-size:11px;color:var(--dim);display:flex;align-items:center;gap:5px">'
    +'<input class="set-input" type="number" id="st-offline_threshold_seconds" value="'+(st.offline_threshold_seconds||180)+'" style="width:80px"> seconds</label>'
    +'</div>'
    +'<div class="set-row">'
    +'<span class="set-label">Default poll interval</span>'
    +'<label style="font-size:11px;color:var(--dim);display:flex;align-items:center;gap:5px">'
    +'<input class="set-input" type="number" id="st-default_poll_seconds" value="'+(st.default_poll_seconds||30)+'" style="width:80px"> seconds</label>'
    +'</div>'
    +'<div class="set-row">'
    +'<span class="set-label">Auto-deregister stale agents</span>'
    +'<label style="font-size:11px;color:var(--dim);display:flex;align-items:center;gap:5px">'
    +'<input class="set-input" type="number" id="st-auto_deregister_days" value="'+(st.auto_deregister_days||0)+'" style="width:64px"> days (0 = off)</label>'
    +'</div>';

  /* -- Per-Agent Poll Intervals -- */
  var pi=s.poll_intervals||[];
  var ar='';
  if(agents && agents.length){
    ar=agents.map(function(a){
      var pv=pi.find(function(p){return p.agent_id===a.agent_id;});
      var val=pv?pv.poll_seconds:(st.default_poll_seconds||30);
      return '<div class="set-row">'
        +'<span class="set-label" style="display:flex;align-items:center;gap:6px">'
        +'<span class="badge b-'+(a.online?'online':'offline')+'" style="font-size:9px">'+(a.online?'On':'Off')+'</span>'
        +esc(a.hostname)+'</span>'
        +'<label style="font-size:11px;color:var(--dim);display:flex;align-items:center;gap:5px">'
        +'Poll every <input class="set-input" type="number" id="pi-'+a.agent_id+'" value="'+val+'" style="width:64px"> sec</label>'
        +'<button class="btn sm green" onclick="saveAgentPoll(\''+a.agent_id+'\')">Save</button>'
        +'<button class="btn sm danger" onclick="resetAgentPoll(\''+a.agent_id+'\')">Reset</button>'
        +'</div>';
    }).join('');
  }

      /* -- Exports -- */
  var agentChecks=agents.map(function(a){
    var chk=(selId&&a.agent_id===selId)?' checked':'';
    return '<label style="display:flex;align-items:center;gap:5px;padding:4px 8px;'
      +'border-radius:6px;cursor:pointer;font-size:11px;'
      +'background:var(--card2);border:1px solid var(--border2);white-space:nowrap">'
      +'<input type="checkbox" class="exp-agent-chk" value="'+a.agent_id+'"'+chk+' style="accent-color:var(--accent)">'
      +'<span class="badge b-'+(a.online?'online':'offline')+'" style="font-size:9px;padding:1px 5px">'+(a.online?'On':'Off')+'</span>'
      +esc(a.hostname)
      +'</label>';
  }).join('');

  var ex='<div style="margin-bottom:14px">'
    +'<div style="font-size:10px;font-weight:700;color:var(--dim);text-transform:uppercase;letter-spacing:.07em;margin-bottom:7px">Fleet Exports</div>'
    +'<div style="display:flex;gap:6px;flex-wrap:wrap">'
    +'<a class="btn" href="/api/export/fleet.csv" download>Fleet CSV</a>'
    +'<a class="btn" href="/api/export/fleet_inventory.csv" download>Inventory CSV</a>'
    +'<a class="btn" href="/api/export/fleet.html" target="_blank">Fleet Report HTML</a>'
    +'<a class="btn" href="/api/export/audit.csv" download>Audit CSV</a>'
    +'<a class="btn" href="/api/export/alerts.csv" download>Alerts CSV</a>'
    +'</div>'
    +'</div>'
    +(agents.length
      ? '<div>'
        +'<div style="font-size:10px;font-weight:700;color:var(--dim);text-transform:uppercase;letter-spacing:.07em;margin-bottom:7px">Per-Agent Exports</div>'
        +'<div style="font-size:11px;color:var(--dim);margin-bottom:8px">Select one or more agents, then choose export type:</div>'
        /* Agent checkboxes */
        +'<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px" id="expAgentList">'
        +agentChecks
        +'</div>'
        /* Select all / none shortcuts */
        +'<div style="display:flex;gap:6px;margin-bottom:10px;align-items:center">'
        +'<button class="btn sm" onclick="expSelectAll(true)">All</button>'
        +'<button class="btn sm" onclick="expSelectAll(false)">None</button>'
        +'<button class="btn sm" onclick="expSelectOnline()">Online only</button>'
        +'<span id="expSelCount" style="font-size:11px;color:var(--dim);margin-left:4px">0 selected</span>'
        +'</div>'
        /* Export buttons */
        +'<div style="display:flex;gap:6px;flex-wrap:wrap">'
        +'<button class="btn green" onclick="doMultiExportCSV()">Agent CSV (each)</button>'
        +'<button class="btn" onclick="doMultiExportHTML()">Agent Report HTML (each)</button>'
        +'</div>'
        +'<div style="font-size:10px;color:var(--dim);margin-top:2px;margin-bottom:10px">Each selected agent = separate file.</div>'
    +'<div style="font-size:10px;font-weight:700;color:var(--dim);text-transform:uppercase;letter-spacing:.07em;margin-bottom:6px">Or combine all selected into one file:</div>'
    +'<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:4px">'
    +'<button class="btn accent" onclick="doCombinedExportCSV()">Combined CSV</button>'
    +'<button class="btn accent" onclick="doCombinedExportHTML()">Combined Report HTML</button>'
    +'</div>'
    +'<div style="font-size:10px;color:var(--dim);margin-top:2px">Combined = all selected agents merged into a single file.</div>'
        +'</div>'
      : '<div style="font-size:11px;color:var(--dim)">No agents registered yet.</div>');

  el.innerHTML=
    '<div style="background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:14px;margin-bottom:12px">'
    +'<div style="font-size:11px;font-weight:700;color:#F0F0FF;margin-bottom:10px">Alert Thresholds</div>'+tr+'</div>'

    +'<div style="background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:14px;margin-bottom:12px">'
    +'<div style="font-size:11px;font-weight:700;color:#F0F0FF;margin-bottom:10px">General</div>'+gr+'</div>'

    +(ar
      ? '<div style="background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:14px;margin-bottom:12px">'
        +'<div style="font-size:11px;font-weight:700;color:#F0F0FF;margin-bottom:10px">Per-Agent Poll Interval</div>'
        +'<div style="font-size:11px;color:var(--dim);margin-bottom:8px">Override the default poll interval for individual agents. Click Save next to each agent after editing.</div>'
        +ar+'</div>'
      : '')

    +'<div style="background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:14px;margin-bottom:12px">'
    +'<div style="font-size:11px;font-weight:700;color:#F0F0FF;margin-bottom:10px">Exports</div>'+ex+'</div>';
}

async function saveAgentPoll(agentId){
  var e=document.getElementById('pi-'+agentId);if(!e)return;
  var r=await fetch('/api/settings/poll/'+agentId,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({poll_seconds:parseInt(e.value)})});
  if(r.ok)toast('Poll interval saved','ok');else toast('Save failed','warn');
}
async function resetAgentPoll(agentId){
  await fetch('/api/settings/poll/'+agentId,{method:'DELETE'});
  toast('Poll interval reset to default','info');
  loadSettingsPane();
}

async function saveAgentPoll(agentId){
  var e=document.getElementById('pi-'+agentId);if(!e)return;
  var r=await fetch('/api/settings/poll/'+agentId,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({poll_seconds:parseInt(e.value)})});
  if(r.ok)toast('Poll interval saved','ok');else toast('Save failed','warn');
}
async function resetAgentPoll(agentId){
  await fetch('/api/settings/poll/'+agentId,{method:'DELETE'});
  toast('Poll interval reset to default','info');
  loadSettingsPane();
}
async function saveSettings(){
  var keys=['thresh_temp_warn','thresh_temp_crit','thresh_realloc_warn','thresh_realloc_crit',
    'thresh_pending_warn','thresh_pending_crit','thresh_uncorr_warn','thresh_uncorr_crit',
    'thresh_wear_warn','thresh_wear_crit','thresh_spare_warn','thresh_spare_crit',
    'offline_threshold_seconds','default_poll_seconds','auto_deregister_days'];
  var p={};
  for(var i=0;i<keys.length;i++){var e=document.getElementById('st-'+keys[i]);if(e)p[keys[i]]=e.value;}
  var r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});
  if(r.ok){
    toast('Settings saved','ok');
    await loadAgents();
    await loadStats();
    await loadAlerts();
    if(selId)refreshOverview();
  }else{
    toast('Save failed','warn');
  }
}




function _expGetSelected(){
  return [...document.querySelectorAll('.exp-agent-chk:checked')].map(function(c){return c.value;});
}
function _expUpdateCount(){
  var n=_expGetSelected().length;
  var el=document.getElementById('expSelCount');
  if(el)el.textContent=n+' agent'+(n!==1?'s':'')+' selected';
}
function expSelectAll(checked){
  document.querySelectorAll('.exp-agent-chk').forEach(function(c){c.checked=checked;});
  _expUpdateCount();
}
function expSelectOnline(){
  document.querySelectorAll('.exp-agent-chk').forEach(function(c){
    var a=agents.find(function(x){return x.agent_id===c.value;});
    c.checked=a?a.online:false;
  });
  _expUpdateCount();
}
/* Update count whenever a checkbox changes */
document.addEventListener('change',function(e){
  if(e.target&&e.target.classList.contains('exp-agent-chk'))_expUpdateCount();
});
function doMultiExportCSV(){
  var ids=_expGetSelected();
  if(!ids.length){toast('Select at least one agent','warn');return;}
  ids.forEach(function(id,i){
    setTimeout(function(){
      var a=document.createElement('a');
      a.href='/api/export/agent/'+id+'.csv';
      a.download='';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
    },i*400);
  });
  toast('Downloading CSV for '+ids.length+' agent'+(ids.length!==1?'s':''),'ok');
}
function doMultiExportHTML(){
  var ids=_expGetSelected();
  if(!ids.length){toast('Select at least one agent','warn');return;}
  ids.forEach(function(id,i){
    setTimeout(function(){window.open('/api/export/agent/'+id+'.html','_blank');},i*300);
  });
  toast('Opened '+ids.length+' report'+(ids.length!==1?'s':''),'ok');
}

function doCombinedExportCSV(){
  var ids=_expGetSelected();
  if(!ids.length){toast('Select at least one agent','warn');return;}
  var a=document.createElement('a');
  a.href='/api/export/combined.csv?ids='+ids.join(',');
  a.download='';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  toast('Downloading combined CSV for '+ids.length+' agent'+(ids.length!==1?'s':''),'ok');
}
function doCombinedExportHTML(){
  var ids=_expGetSelected();
  if(!ids.length){toast('Select at least one agent','warn');return;}
  window.open('/api/export/combined.html?ids='+ids.join(','),'_blank');
  toast('Opened combined report for '+ids.length+' agent'+(ids.length!==1?'s':''),'ok');
}

function doCombinedExportCSV(){
  var ids=_expGetSelected();
  if(!ids.length){toast('Select at least one agent','warn');return;}
  var a=document.createElement('a');
  a.href='/api/export/combined.csv?ids='+ids.join(',');
  a.download='';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  toast('Downloading combined CSV for '+ids.length+' agent'+(ids.length!==1?'s':''),'ok');
}
function doCombinedExportHTML(){
  var ids=_expGetSelected();
  if(!ids.length){toast('Select at least one agent','warn');return;}
  window.open('/api/export/combined.html?ids='+ids.join(','),'_blank');
  toast('Opened combined report for '+ids.length+' agent'+(ids.length!==1?'s':''),'ok');
}

// ── fmtUptime ─────────────────────────────────────────────────────────────────
function fmtUptime(seconds){
  if(seconds==null)return'';
  var s=Math.floor(seconds);
  if(s<60)return s+'s';
  if(s<3600)return Math.floor(s/60)+'m '+Math.floor(s%60)+'s';
  if(s<86400){var h=Math.floor(s/3600);return h+'h '+Math.floor((s%3600)/60)+'m';}
  var d=Math.floor(s/86400);return d+'d '+Math.floor((s%86400)/3600)+'h';
}

// ── saveAgentMeta ─────────────────────────────────────────────────────────────
async function saveAgentMeta(agentId){
  var dn=document.getElementById('meta-display-name');
  var loc=document.getElementById('meta-location');
  var notes=document.getElementById('meta-notes');
  var payload={};
  if(dn)payload.display_name=dn.value;
  if(loc)payload.location=loc.value;
  if(notes)payload.notes=notes.value;
  var r=await fetch('/api/meta/'+agentId,{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  if(r.ok){toast('Agent info saved','ok');loadAgents();}
  else toast('Save failed','warn');
}

// ── completeReplacement ───────────────────────────────────────────────────────
async function completeReplacement(agentId,serial){
  if(!await dlgConfirm('Mark as Completed','Mark this disk replacement as completed?','Complete'))return;
  var r=await fetch('/api/replacements/'+agentId+'/'+encodeURIComponent(serial)+'/complete',{method:'POST'});
  if(r.ok){toast('Replacement completed','ok');if(selId===agentId)refreshOverview();}
  else toast('Failed','warn');
}

// ── deleteReplacement ─────────────────────────────────────────────────────────
async function deleteReplacement(agentId,serial){
  if(!await dlgConfirm('Remove Replacement Record','Remove this disk replacement record? This cannot be undone.','Remove'))return;
  var r=await fetch('/api/replacements/'+agentId+'/'+encodeURIComponent(serial),{method:'DELETE'});
  if(r.ok){toast('Removed','info');if(selId===agentId)refreshOverview();}
  else toast('Failed','warn');
}

// ── markForReplacement — inline modal (no prompt/confirm) ─────────────────────
function markForReplacement(agentId,serial,model){
  var existing=document.getElementById('_replModal');
  if(existing)existing.remove();

  var overlay=document.createElement('div');
  overlay.id='_replModal';
  overlay.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:9000;display:flex;align-items:center;justify-content:center;';

  var box=document.createElement('div');
  box.style.cssText='background:#27273A;border:1px solid #3D3D5C;border-radius:12px;padding:22px 24px;min-width:340px;max-width:480px;width:90%;box-shadow:0 8px 40px rgba(0,0,0,.7);';

  box.innerHTML=
    '<div style="font-size:14px;font-weight:800;color:#F0F0FF;margin-bottom:4px">Mark for Replacement</div>'
   +'<div style="font-size:11px;color:#6868A0;margin-bottom:16px">'+esc(model||serial)+'</div>'
   +'<div style="margin-bottom:12px">'
     +'<label style="font-size:10px;color:#6868A0;text-transform:uppercase;letter-spacing:.07em;display:block;margin-bottom:6px">Status</label>'
     +'<div style="display:flex;gap:8px">'
       +'<label style="display:flex;align-items:center;gap:6px;cursor:pointer;background:#1A2A40;border:1px solid #38BDF8;border-radius:8px;padding:6px 14px;font-size:12px;color:#38BDF8;font-weight:600">'
         +'<input type="radio" name="_replStatus" value="scheduled" checked> Scheduled</label>'
       +'<label style="display:flex;align-items:center;gap:6px;cursor:pointer;background:#2A0A0A;border:1px solid #EF4444;border-radius:8px;padding:6px 14px;font-size:12px;color:#EF4444;font-weight:600">'
         +'<input type="radio" name="_replStatus" value="urgent"> Urgent</label>'
     +'</div>'
   +'</div>'
   +'<div style="margin-bottom:12px">'
     +'<label style="font-size:10px;color:#6868A0;text-transform:uppercase;letter-spacing:.07em;display:block;margin-bottom:4px">Scheduled Date (optional)</label>'
     +'<input id="_replDate" type="date" style="width:100%;background:#1E1E2E;border:1px solid #3D3D5C;color:#E0E0FF;border-radius:6px;padding:7px 10px;font-size:12px;outline:none;">'
   +'</div>'
   +'<div style="margin-bottom:18px">'
     +'<label style="font-size:10px;color:#6868A0;text-transform:uppercase;letter-spacing:.07em;display:block;margin-bottom:4px">Note (optional)</label>'
     +'<textarea id="_replNote" rows="2" placeholder="e.g. Drive making clicking sounds..." style="width:100%;background:#1E1E2E;border:1px solid #3D3D5C;color:#E0E0FF;border-radius:6px;padding:7px 10px;font-size:12px;outline:none;font-family:inherit;resize:vertical;"></textarea>'
   +'</div>'
   +'<div style="display:flex;gap:8px;justify-content:flex-end">'
     +'<button id="_replCancel" class="btn" style="min-width:80px">Cancel</button>'
     +'<button id="_replConfirm" class="btn accent" style="min-width:140px">Mark for Replacement</button>'
   +'</div>';

  overlay.appendChild(box);
  document.body.appendChild(overlay);
  setTimeout(function(){var n=document.getElementById('_replNote');if(n)n.focus();},50);
  overlay.addEventListener('click',function(e){if(e.target===overlay)overlay.remove();});
  document.getElementById('_replCancel').onclick=function(){overlay.remove();};
  document.getElementById('_replConfirm').onclick=async function(){
    var statusEl=document.querySelector('input[name="_replStatus"]:checked');
    var status=statusEl?statusEl.value:'scheduled';
    var date=(document.getElementById('_replDate').value||'').trim();
    var note=(document.getElementById('_replNote').value||'').trim();
    this.disabled=true;this.textContent='Saving...';
    try{
      var r=await fetch('/api/replacements/'+agentId,{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({disk_serial:serial,disk_model:model,status:status,note:note,scheduled_date:date})
      });
      if(r.ok){overlay.remove();toast('Marked for replacement: '+esc(model||serial),'ok');if(selId===agentId)refreshOverview();}
      else{toast('Save failed','warn');this.disabled=false;this.textContent='Mark for Replacement';}
    }catch(e){toast('Error: '+e.message,'warn');this.disabled=false;this.textContent='Mark for Replacement';}
  };
}

// ── _toggleEnhCard: collapse/expand handler (no inline escaping) ──────────
function _toggleEnhCard(hdr){
  var body=hdr.nextElementSibling;
  var arrow=hdr.querySelector('.eca');
  if(body)body.classList.toggle('open');
  if(arrow)arrow.classList.toggle('open');
}

// ── Inject CSS once ───────────────────────────────────────────────────────
(function(){
  if(document.getElementById('_ecs'))return;
  var s=document.createElement('style');s.id='_ecs';
  s.textContent=
    '.ecard{background:var(--card);border:1px solid var(--border);border-radius:var(--r);margin-top:14px;overflow:hidden}'
   +'.ecard-hdr{padding:10px 14px;display:flex;align-items:center;gap:8px;cursor:pointer;user-select:none;border-bottom:1px solid var(--border);background:var(--panel)}'
   +'.ecard-hdr:hover{background:var(--card2)}'
   +'.ecard-title{font-size:11px;font-weight:700;color:var(--dim);text-transform:uppercase;letter-spacing:.08em;flex:1}'
   +'.eca{font-size:10px;color:var(--dim);transition:transform .2s;display:inline-block}'
   +'.eca.open{transform:rotate(90deg)}'
   +'.ecard-body{display:none;padding:14px}'
   +'.ecard-body.open{display:block}'
   +'.mfield label{font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:.07em;display:block;margin-bottom:3px}'
   +'.mfield input,.mfield textarea{width:100%;background:var(--card2);border:1px solid var(--border2);color:var(--text);border-radius:4px;padding:6px 9px;font-size:12px;outline:none;font-family:inherit;resize:vertical}'
   +'.mfield input:focus,.mfield textarea:focus{border-color:var(--accent)}'
   +'.ri{display:flex;align-items:flex-start;gap:10px;padding:9px 0;border-bottom:1px solid var(--border)}'
   +'.ri:last-child{border-bottom:none}'
   +'.rb{display:inline-block;padding:2px 8px;border-radius:8px;font-size:10px;font-weight:700;margin-right:6px}'
   +'.rb.scheduled{background:#1A2A40;color:#38BDF8}'
   +'.rb.urgent{background:var(--red-bg);color:var(--red)}'
   +'.rb.completed{background:var(--green-bg);color:var(--green)}'
   +'.uptime-pill{background:var(--green-bg);color:var(--green);border-radius:8px;padding:2px 9px;font-size:10px;font-weight:700}';
  document.head.appendChild(s);
})();

var _enhGen=0;
async function _enhanceOverview(openEcards,silent){
  if(!selId)return;
  var oc=document.getElementById('overviewContent');
  if(!oc||oc.style.display==='none')return;

  // Generation token — if another call starts while we await, we abort on resume
  var myGen=++_enhGen;

  // Hard clear injected elements before any await
  oc.querySelectorAll('.ecard').forEach(function(el){el.remove();});
  oc.querySelectorAll('.mark-repl-btn').forEach(function(el){el.remove();});
  var existingPill=document.querySelector('#bnMeta .uptime-pill');
  if(existingPill)existingPill.remove();

  // Fetch all data in parallel
  var meta={},uptime={},replacements=[];
  try{
    var res=await Promise.all([
      fetch('/api/meta/'+selId).then(function(r){return r.json();}),
      fetch('/api/uptime/'+selId).then(function(r){return r.json();}),
      fetch('/api/replacements/'+selId).then(function(r){return r.json();})
    ]);
    meta=res[0]||{};
    uptime=res[1]||{};
    replacements=Array.isArray(res[2])?res[2]:[];
  }catch(e){console.warn('_enhanceOverview fetch error',e);}

  // Abort if a newer call has started while we were awaiting
  if(myGen!==_enhGen)return;

  // ── Uptime pill in banner ───────────────────────────────────────────────
  var a=agents.find(function(x){return x.agent_id===selId;});
  if(a&&a.online&&uptime.uptime_seconds!=null){
    var bnMeta=document.getElementById('bnMeta');
    if(bnMeta){
      var pill=document.createElement('span');
      pill.className='uptime-pill';
      pill.textContent='Up '+fmtUptime(uptime.uptime_seconds);
      bnMeta.appendChild(pill);
    }
  }

  // ── Mark-for-replacement buttons on each disk card ──────────────────────
  // (disk cards are now in DOM because renderOverview() has completed)
  oc.querySelectorAll('.dk-hdr').forEach(function(hdr){
    var sub=hdr.querySelector('.dk-sub');
    if(!sub)return;
    var m=sub.textContent.match(/S\/N:\s*([^\s\u00b7\u22c5\u00d7·]+)/);
    if(!m||!m[1])return;
    var serial=m[1];
    var nameEl=hdr.querySelector('.dk-name');
    var model=nameEl?nameEl.textContent.replace(/^.\s*/,'').trim():'';
    var alreadySched=replacements.find(function(r){
      return r.disk_serial===serial&&r.status!=='completed';
    });
    var btn=document.createElement('button');
    btn.className='btn sm mark-repl-btn'+(alreadySched?' yellow':'');
    btn.style.marginLeft='8px';
    btn.textContent=alreadySched?'⚠ Scheduled':'Mark for replacement';
    btn.dataset.aid=selId;
    btn.dataset.ser=encodeURIComponent(serial);
    btn.dataset.model=model;
    btn.onclick=function(){
      markForReplacement(this.dataset.aid,decodeURIComponent(this.dataset.ser),this.dataset.model);
    };
    var badge=hdr.querySelector('.badge');
    if(badge)badge.after(btn);
  });

  // ── Notes & Labels collapsible card ────────────────────────────────────
  var nc=document.createElement('div');
  nc.className='ecard';
  var nhdr=document.createElement('div');
  nhdr.className='ecard-hdr';
  nhdr.setAttribute('onclick','_toggleEnhCard(this)');
  nhdr.innerHTML='<span style="font-size:14px">📝</span>'
    +'<span class="ecard-title">Machine Notes &amp; Labels</span>'
    +'<span class="eca">▶</span>';
  var nbody=document.createElement('div');
  nbody.className='ecard-body';
  nbody.innerHTML=
    '<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px">'
      +'<div class="mfield"><label>Display Name</label>'
        +'<input id="meta-display-name" value="'+esc(meta.display_name||'')+'" placeholder="e.g. Reception Desk PC"></div>'
      +'<div class="mfield"><label>Location</label>'
        +'<input id="meta-location" value="'+esc(meta.location||'')+'" placeholder="e.g. IT Room, Floor 2"></div>'
    +'</div>'
    +'<div class="mfield" style="margin-bottom:10px"><label>Notes</label>'
      +'<textarea id="meta-notes" rows="3" placeholder="Any notes about this machine...">'+esc(meta.notes||'')+'</textarea>'
    +'</div>'
    +'<button class="btn green" data-sid="'+selId+'" onclick="saveAgentMeta(this.dataset.sid)">Save</button>';
  nc.dataset.eid='notes';
  nc.appendChild(nhdr);nc.appendChild(nbody);
  oc.appendChild(nc);
  if(openEcards&&openEcards.has('notes')){nbody.classList.add('open');nhdr.querySelector('.eca').textContent='▼';}

  // ── Disk Replacement Tracker collapsible card ───────────────────────────
  var rc=document.createElement('div');
  rc.className='ecard';
  rc.dataset.eid='replacements';
  var rhdr=document.createElement('div');
  rhdr.className='ecard-hdr';
  rhdr.setAttribute('onclick','_toggleEnhCard(this)');
  var pending=replacements.filter(function(r){return r.status!=='completed';}).length;
  rhdr.innerHTML='<span style="font-size:14px">🔧</span>'
    +'<span class="ecard-title">Disk Replacement Tracker'
    +(pending?' <span style="background:var(--red-bg);color:var(--red);border-radius:8px;padding:1px 7px;font-size:10px">'+pending+'</span>':'')
    +'</span><span class="eca">▶</span>';
  var rbody=document.createElement('div');
  rbody.className='ecard-body';
  if(!replacements.length){
    rbody.innerHTML='<div style="color:var(--dim);font-size:12px;padding:4px 0">No disks marked for replacement.</div>';
  }else{
    rbody.innerHTML=replacements.map(function(r){
      var sc=r.status||'scheduled';
      var dateStr=r.scheduled_date?'<span style="font-size:10px;color:var(--dim);margin-left:6px">📅 '+esc(r.scheduled_date)+'</span>':'';
      var noteStr=r.note?'<div style="font-size:11px;color:var(--dim);margin-top:3px">'+esc(r.note)+'</div>':'';
      var doneBtnHtml=sc!=='completed'
        ?'<button class="btn sm green" style="white-space:nowrap" data-aid="'+selId+'" data-ser="'+encodeURIComponent(r.disk_serial)+'" onclick="completeReplacement(this.dataset.aid,decodeURIComponent(this.dataset.ser))">Done</button>':'' ;
      var rmBtnHtml='<button class="btn sm danger" style="white-space:nowrap" data-aid="'+selId+'" data-ser="'+encodeURIComponent(r.disk_serial)+'" onclick="deleteReplacement(this.dataset.aid,decodeURIComponent(this.dataset.ser))">Remove</button>';
      return '<div class="ri">'
        +'<div style="flex:1"><span class="rb '+sc+'">'+sc.toUpperCase()+'</span>'
        +'<strong style="font-size:12px">'+esc(r.disk_model||r.disk_serial)+'</strong>'
        +dateStr+noteStr
        +'<div style="font-size:10px;color:var(--dim2);margin-top:2px">S/N: '+esc(r.disk_serial)+'</div></div>'
        +'<div style="display:flex;gap:5px;flex-shrink:0;align-items:center">'+doneBtnHtml+rmBtnHtml+'</div>'
        +'</div>';
    }).join('');
  }
  rc.appendChild(rhdr);rc.appendChild(rbody);
  oc.appendChild(rc);
  if(openEcards&&openEcards.has('replacements')){rbody.classList.add('open');rhdr.querySelector('.eca').textContent='▼';}
}

</script></body></html>"""



@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    data = _cfg_get_all()
    with db() as c:
        pi = c.execute("SELECT agent_id,poll_seconds FROM agent_poll_intervals").fetchall()
    return jsonify({"settings":data,"poll_intervals":[dict(r) for r in pi]})

@app.route("/api/settings", methods=["POST"])
def api_save_settings():
    data = request.get_json(force=True,silent=True) or {}
    with db() as c:
        for k,v in data.items():
            if k in _SETTINGS_DEFAULTS:
                c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",(k,str(v)))
    _reevaluate_alerts()
    broker.publish("settings_changed", {"ts": _now()})
    return jsonify({"status":"ok"})
@app.route("/api/settings/poll/<agent_id>", methods=["POST"])
def api_set_poll(agent_id):
    data = request.get_json(force=True,silent=True) or {}
    secs = max(10,int(data.get("poll_seconds",30)))
    with db() as c:
        c.execute("INSERT OR REPLACE INTO agent_poll_intervals(agent_id,poll_seconds) VALUES(?,?)",(agent_id,secs))
    return jsonify({"status":"ok","poll_seconds":secs})

@app.route("/api/settings/poll/<agent_id>", methods=["DELETE"])
def api_reset_poll(agent_id):
    with db() as c: c.execute("DELETE FROM agent_poll_intervals WHERE agent_id=?",(agent_id,))
    return jsonify({"status":"ok"})

@app.route("/api/trends/<agent_id>/<disk_serial>")
def api_trend_series(agent_id, disk_serial):
    hours  = min(int(request.args.get("hours",24)),168)
    cutoff = (datetime.now(timezone.utc)-timedelta(hours=hours)).isoformat(timespec="seconds")
    mkeys  = {mk for _,mk,_ in _TRACKED_METRICS}
    with db() as c:
        rows = c.execute(
            "SELECT ts,metric,value FROM disk_trends"
            " WHERE agent_id=? AND disk_serial=? AND ts>=? ORDER BY ts ASC",
            (agent_id,disk_serial,cutoff)).fetchall()
        meta = c.execute(
            "SELECT disk_model FROM disk_trends WHERE agent_id=? AND disk_serial=? LIMIT 1",
            (agent_id,disk_serial)).fetchone()
    series = {}
    for row in rows:
        if row["metric"] not in mkeys: continue
        series.setdefault(row["metric"],[]).append({"ts":row["ts"],"v":row["value"]})
    return jsonify({"agent_id":agent_id,"disk_serial":disk_serial,
                    "disk_model":meta["disk_model"] if meta else "","hours":hours,"series":series})

@app.route("/api/trends/<agent_id>")
def api_trend_disks(agent_id):
    with db() as c:
        rows = c.execute(
            "SELECT DISTINCT disk_serial,disk_model,MAX(ts) as last_ts"
            " FROM disk_trends WHERE agent_id=? GROUP BY disk_serial",(agent_id,)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/analytics/reports_daily")
def api_reports_daily():
    days = min(int(request.args.get("days",7)),90)
    with db() as c:
        rows = c.execute(
            "SELECT DATE(received_at) as date,COUNT(*) as count FROM reports"
            " WHERE received_at>=DATE('now','-'||?||' days')"
            " GROUP BY DATE(received_at) ORDER BY date",(days,)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/analytics/alerts_daily")
def api_alerts_daily():
    days = min(int(request.args.get("days",7)),90)
    with db() as c:
        rows = c.execute(
            "SELECT DATE(created_at) as date,COUNT(*) as count FROM alerts"
            " WHERE created_at>=DATE('now','-'||?||' days')"
            " GROUP BY DATE(created_at) ORDER BY date",(days,)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/analytics/activity_daily")
def api_activity_daily():
    days = min(int(request.args.get("days",7)),90)
    with db() as c:
        rows = c.execute(
            "SELECT DATE(ts) as date,"
            "SUM(CASE WHEN event_type IN ('online','register') THEN 1 ELSE 0 END) as online_events"
            " FROM activity_log WHERE ts>=DATE('now','-'||?||' days')"
            " GROUP BY DATE(ts) ORDER BY date",(days,)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/export/fleet.csv")
def export_fleet_csv():
    ags = _load_agents_full()
    rows = [{"hostname":a.get("hostname",""),"ip":a.get("ip",""),
             "online":"Yes" if a.get("online") else "No",
             "worst_status":a.get("worst_status",""),"disk_count":len(a.get("disks",[])),
             "last_seen":a.get("last_seen","")} for a in ags]
    return _csv_resp(rows,["hostname","ip","online","worst_status","disk_count","last_seen"],
                     "fleet.csv")
@app.route("/api/export/fleet_inventory.csv")
def export_fleet_inventory():
    ags = _load_agents_full()
    rows = []
    for a in ags:
        for d in a.get("disks",[]):
            rows.append({
                "hostname":a.get("hostname",""),"ip":a.get("ip",""),
                "model":d.get("model",""),"serial":d.get("serial",""),
                "interface":d.get("interface",""),"size_gb":d.get("size_gb",""),
                "smart_status":d.get("smart_status",""),"temperature":d.get("temperature",""),
                "reallocated":d.get("reallocated",""),"pending":d.get("pending",""),
                "uncorrectable":d.get("uncorrectable",""),
                "wear_pct":d.get("percentage_used",""),"spare_pct":d.get("available_spare",""),
                "power_on_h":d.get("power_on_hours",""),
            })
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return _csv_resp(rows,
        ["hostname","ip","model","serial","interface","size_gb","smart_status",
         "temperature","reallocated","pending","uncorrectable","wear_pct","spare_pct","power_on_h"],
        "inventory_%s.csv" % ts)

@app.route("/api/export/audit.csv")
def export_audit_csv():
    with db() as c:
        rows = c.execute(
            "SELECT ts,agent_id,hostname,event_type,detail FROM activity_log"
            " ORDER BY id DESC LIMIT 5000").fetchall()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return _csv_resp([dict(r) for r in rows],
        ["ts","agent_id","hostname","event_type","detail"],
        "audit_%s.csv" % ts)

@app.route("/api/export/alerts.csv")
def export_alerts_csv():
    dismissed = request.args.get("dismissed","0")
    with db() as c:
        rows = c.execute(
            "SELECT * FROM alerts WHERE dismissed=? ORDER BY created_at DESC",
            (int(dismissed),)).fetchall()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return _csv_resp([dict(r) for r in rows],
        ["id","agent_id","hostname","severity","message","disk_serial","created_at","dismissed"],
        "alerts_%s.csv" % ts)

@app.route("/api/export/agent/<agent_id>.csv")
def export_agent_csv(agent_id):
    ags = _load_agents_full()
    ag  = next((a for a in ags if a["agent_id"] == agent_id), None)
    if not ag: return jsonify({"error":"not found"}),404
    rows = []
    for d in ag.get("disks",[]):
        for v in (d.get("volumes") or [{}]):
            rows.append({
                "hostname":ag["hostname"],
                "model":d.get("model",""),"serial":d.get("serial",""),
                "interface":d.get("interface",""),"size_gb":d.get("size_gb",""),
                "smart_status":d.get("smart_status",""),"temperature":d.get("temperature",""),
                "reallocated":d.get("reallocated",""),"pending":d.get("pending",""),
                "uncorrectable":d.get("uncorrectable",""),
                "wear_pct":d.get("percentage_used",""),"spare_pct":d.get("available_spare",""),
                "power_on_h":d.get("power_on_hours",""),
                "volume":v.get("drive",""),"vol_used_pct":v.get("used_pct",""),
                "vol_free_gb":v.get("free_gb",""),
            })
    hn = ag.get("hostname",agent_id).replace(" ","_")
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return _csv_resp(rows,
        ["hostname","model","serial","interface","size_gb","smart_status","temperature",
         "reallocated","pending","uncorrectable","wear_pct","spare_pct","power_on_h",
         "volume","vol_used_pct","vol_free_gb"],
        "agent_%s_%s.csv" % (hn, ts))

@app.route("/api/export/fleet.html")
def export_fleet_html():
    ags   = _load_agents_full()
    counts = {}
    for a in ags:
        ws = a.get("worst_status","Unknown"); counts[ws] = counts.get(ws,0)+1
    online      = sum(1 for a in ags if a.get("online"))
    total_disks = sum(len(a.get("disks",[])) for a in ags)
    total_tb    = sum(d.get("size_gb",0) or 0 for a in ags for d in a.get("disks",[]))/1024
    css = ("body{font-family:Segoe UI,Arial,sans-serif;background:#fff;color:#1a1a2e;font-size:13px;margin:0}"
        ".hdr{background:#1E1E2E;color:#E0E0FF;padding:18px 32px;display:flex;justify-content:space-between}"
        ".hdr h1{margin:0;font-size:18px;font-weight:800}"
        ".section{padding:18px 32px;border-bottom:1px solid #eee}"
        "h2{font-size:13px;font-weight:700;text-transform:uppercase;color:#6868A0;margin:0 0 10px}"
        "table{width:100%;border-collapse:collapse;font-size:12px}"
        "th{text-align:left;padding:6px 10px;background:#f5f5ff;color:#6868A0;font-size:10px;border-bottom:2px solid #e0e0f0}"
        "td{padding:6px 10px;border-bottom:1px solid #f0f0f8}"
        "tr:nth-child(even) td{background:#fafafa}"
        ".badge{display:inline-block;padding:2px 8px;border-radius:9px;font-size:10px;font-weight:700}"
        ".b-healthy{background:#dcfce7;color:#166534}.b-warning{background:#fef9c3;color:#854d0e}"
        ".b-critical{background:#fee2e2;color:#991b1b}.b-unknown{background:#f1f5f9;color:#475569}"
        ".b-online{background:#dcfce7;color:#166534}.b-offline{background:#fee2e2;color:#991b1b}"
        "@media print{button{display:none}}")
    ts_label = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    summary = ("<div class='section'><h2>Fleet Summary</h2><table><thead>"
        "<tr><th>Total</th><th>Online</th><th>Offline</th><th>Healthy</th>"
        "<th>Warning</th><th>Critical</th><th>Disks</th><th>Storage</th></tr></thead><tbody>"
        "<tr><td>%d</td><td>%d</td><td>%d</td>"
        "<td><span class='badge b-healthy'>%d</span></td>"
        "<td><span class='badge b-warning'>%d</span></td>"
        "<td><span class='badge b-critical'>%d</span></td>"
        "<td>%d</td><td>%.2f TB</td></tr></tbody></table></div>") % (
        len(ags),online,len(ags)-online,
        counts.get("Healthy",0),counts.get("Warning",0),counts.get("Critical",0),
        total_disks,total_tb)
    def agent_rows():
        rows = []
        for a in ags:
            sc = (a.get("worst_status") or "Unknown").lower()
            ol = "online" if a.get("online") else "offline"
            disk_rows = ""
            for d in a.get("disks",[]):
                ds = (d.get("smart_status") or "Unknown").lower()
                disk_rows += ("<tr><td style='padding-left:24px'>%s</td><td>%s</td>"
                    "<td><span class='badge b-%s'>%s</span></td>"
                    "<td>%s</td><td>%s</td><td>%s GB</td>"
                    "<td>%s</td><td>%s</td><td>%s</td></tr>") % (
                    d.get("model","?"),d.get("serial","—"),ds,d.get("smart_status","?"),
                    d.get("interface","?"),
                    (str(d.get("temperature","—"))+"°C") if d.get("temperature") is not None else "—",
                    d.get("size_gb","?"),
                    d.get("reallocated","—"),
                    (str(d.get("percentage_used","—"))+"%") if d.get("percentage_used") is not None else "—",
                    d.get("power_on_hours","—"))
            rows.append(
                "<tr style='background:#f5f5ff'>"
                "<td><strong>%s</strong></td><td>%s</td>"
                "<td><span class='badge b-%s'>%s</span></td>"
                "<td colspan='7'><span class='badge b-%s'>%s</span> — %d disk(s) — Last: %s</td>"
                "</tr>%s" % (
                a.get("hostname","?"),a.get("ip",""),
                sc,a.get("worst_status","?"),
                ol,ol.title(),len(a.get("disks",[])),a.get("last_seen","—"),
                disk_rows))
        return "".join(rows)
    body = (summary +
        "<div class='section'><h2>All Agents &amp; Disks</h2><table><thead>"
        "<tr><th>Host / Model</th><th>IP / Serial</th><th>Status</th>"
        "<th>Interface</th><th>Temp</th><th>Size</th>"
        "<th>Realloc</th><th>Wear</th><th>Hours</th></tr>"
        "</thead><tbody>" + agent_rows() + "</tbody></table></div>")
    html = ("<!DOCTYPE html><html><head><meta charset='UTF-8'>"
        "<title>DiskHealth Fleet Report</title><style>%s</style></head><body>"
        "<div class='hdr'><div><h1>DiskHealth Fleet Report</h1>"
        "<div style='font-size:11px;opacity:.6'>%d agents | %d disks | %.2f TB</div></div>"
        "<div style='font-size:11px;opacity:.6'>%s<br>"
        "<button onclick='window.print()'>Print / Save PDF</button></div></div>"
        "%s</body></html>") % (css, len(ags), total_disks, total_tb, ts_label, body)
    return Response(html, mimetype="text/html")

@app.route("/api/export/agent/<agent_id>.html")
def export_agent_html(agent_id):
    ags = _load_agents_full()
    ag  = next((a for a in ags if a["agent_id"] == agent_id), None)
    if not ag: return jsonify({"error":"not found"}),404
    css = ("body{font-family:Segoe UI,Arial,sans-serif;background:#fff;color:#1a1a2e;font-size:13px;margin:0}"
        ".hdr{background:#1E1E2E;color:#E0E0FF;padding:18px 32px}"
        ".hdr h1{margin:0;font-size:18px;font-weight:800}"
        ".section{padding:18px 32px;border-bottom:1px solid #eee}"
        "h2{font-size:13px;font-weight:700;text-transform:uppercase;color:#6868A0;margin:0 0 10px}"
        "table{width:100%;border-collapse:collapse;font-size:12px}"
        "th{text-align:left;padding:6px 10px;background:#f5f5ff;color:#6868A0;font-size:10px;border-bottom:2px solid #e0e0f0}"
        "td{padding:6px 10px;border-bottom:1px solid #f0f0f8}"
        ".badge{display:inline-block;padding:2px 8px;border-radius:9px;font-size:10px;font-weight:700}"
        ".b-healthy{background:#dcfce7;color:#166534}.b-warning{background:#fef9c3;color:#854d0e}"
        ".b-critical{background:#fee2e2;color:#991b1b}.b-unknown{background:#f1f5f9;color:#475569}"
        ".b-online{background:#dcfce7;color:#166534}.b-offline{background:#fee2e2;color:#991b1b}"
        "@media print{button{display:none}}")
    sc  = (ag.get("worst_status") or "Unknown").lower()
    ol  = "online" if ag.get("online") else "offline"
    ts_label = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    disk_rows = ""
    for d in ag.get("disks",[]):
        ds = (d.get("smart_status") or "Unknown").lower()
        vols = ", ".join(
            "%s %.0f%% (%s GB free)" % (v.get("drive",""),v.get("used_pct",0),v.get("free_gb","?"))
            for v in d.get("volumes",[])
        )
        disk_rows += ("<tr><td>%s</td><td>%s</td><td><span class='badge b-%s'>%s</span></td>"
            "<td>%s</td><td>%s</td><td>%s GB</td>"
            "<td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>") % (
            d.get("model","?"),d.get("serial","—"),ds,d.get("smart_status","?"),
            d.get("interface","?"),
            (str(d.get("temperature",""))+"°C") if d.get("temperature") is not None else "—",
            d.get("size_gb","?"),
            d.get("reallocated","—"),d.get("pending","—"),d.get("uncorrectable","—"),
            (str(d.get("percentage_used",""))+"%") if d.get("percentage_used") is not None else "—",
            vols or "—")
    body = ("<div class='section'><h2>System Info</h2><table>"
        "<tr><th>Hostname</th><th>IP</th><th>OS</th><th>Agent</th><th>Users</th><th>Online</th><th>Status</th><th>Last Seen</th></tr>"
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>v%s</td><td>%s</td>"
        "<td><span class='badge b-%s'>%s</span></td>"
        "<td><span class='badge b-%s'>%s</span></td><td>%s</td></tr>"
        "</table></div>"
        "<div class='section'><h2>Disk Health</h2><table><thead>"
        "<tr><th>Model</th><th>Serial</th><th>Status</th><th>Interface</th>"
        "<th>Temp</th><th>Size</th><th>Realloc</th><th>Pending</th>"
        "<th>Uncorr</th><th>Wear</th><th>Volumes</th></tr>"
        "</thead><tbody>%s</tbody></table></div>") % (
        ag.get("hostname","?"),ag.get("ip",""),ag.get("os_version",""),
        ag.get("agent_version",""),ag.get("logged_users","—"),
        ol,ol.title(),sc,ag.get("worst_status","?"),ag.get("last_seen","—"),
        disk_rows)
    html = ("<!DOCTYPE html><html><head><meta charset='UTF-8'>"
        "<title>DiskHealth — %s</title><style>%s</style></head><body>"
        "<div class='hdr'><div><h1>%s</h1>"
        "<div style='font-size:11px;opacity:.6'>DiskHealth By Agent Report Master Sofa Sdn Bhd</div></div>"
        "<div style='font-size:11px;opacity:.6'>%s<br>"
        "<button onclick='window.print()'>Print / Save PDF</button></div></div>"
        "%s</body></html>") % (
        ag.get("hostname","?"), css,
        ag.get("hostname","?"), ts_label, body)
    return Response(html, mimetype="text/html")




@app.route("/chart.js")
def serve_chartjs():
    f = Path("chart.umd.min.js")
    if not f.exists():
        return Response("// Chart.js not found on server", mimetype="application/javascript"), 404
    return Response(f.read_bytes(), mimetype="application/javascript",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.route("/api/export/combined.html")
def export_combined_html():
    """Combined HTML report for selected agent IDs (passed as ?ids=id1,id2,...)"""
    ids = request.args.get("ids","").split(",")
    ids = [i.strip() for i in ids if i.strip()]
    if not ids:
        return jsonify({"error":"no ids provided"}),400
    ags  = _load_agents_full()
    sel  = [a for a in ags if a["agent_id"] in ids]
    if not sel:
        return jsonify({"error":"no matching agents"}),404

    css = ("body{font-family:Segoe UI,Arial,sans-serif;background:#fff;color:#1a1a2e;font-size:13px;margin:0}"
        ".hdr{background:#1E1E2E;color:#E0E0FF;padding:18px 32px;display:flex;justify-content:space-between;align-items:center}"
        ".hdr h1{margin:0;font-size:18px;font-weight:800}.hdr .sub{font-size:11px;opacity:.6}"
        ".section{padding:18px 32px;border-bottom:1px solid #eee}"
        "h2{font-size:13px;font-weight:700;text-transform:uppercase;color:#6868A0;margin:0 0 10px}"
        "h3{font-size:12px;font-weight:700;color:#1a1a2e;margin:14px 0 6px;padding:8px 10px;background:#f5f5ff;border-radius:6px}"
        "table{width:100%;border-collapse:collapse;font-size:12px;margin-bottom:10px}"
        "th{text-align:left;padding:6px 10px;background:#f5f5ff;color:#6868A0;font-size:10px;border-bottom:2px solid #e0e0f0}"
        "td{padding:6px 10px;border-bottom:1px solid #f0f0f8}"
        "tr:nth-child(even) td{background:#fafafa}"
        ".badge{display:inline-block;padding:2px 8px;border-radius:9px;font-size:10px;font-weight:700}"
        ".b-healthy{background:#dcfce7;color:#166534}.b-warning{background:#fef9c3;color:#854d0e}"
        ".b-critical{background:#fee2e2;color:#991b1b}.b-unknown{background:#f1f5f9;color:#475569}"
        ".b-online{background:#dcfce7;color:#166534}.b-offline{background:#fee2e2;color:#991b1b}"
        ".agent-block{border:1px solid #e0e0f0;border-radius:8px;margin-bottom:20px;overflow:hidden}"
        ".agent-hdr{background:#f5f5ff;padding:10px 14px;display:flex;justify-content:space-between;align-items:center}"
        ".agent-name{font-size:14px;font-weight:800}"
        ".disk-row td{vertical-align:top}"
        ".attr{display:inline-block;background:#f5f5ff;border-radius:4px;padding:2px 7px;font-size:10px;margin:1px}"
        ".attr.bad{background:#fee2e2;color:#991b1b}.attr.warn{background:#fef9c3;color:#854d0e}.attr.ok{background:#dcfce7;color:#166534}"
        "@media print{button{display:none}body{font-size:11px}}")

    ts_label = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    total_disks = sum(len(a.get("disks",[])) for a in sel)
    total_tb    = sum(d.get("size_gb",0) or 0 for a in sel for d in a.get("disks",[]))/1024

    def disk_attrs(d):
        parts = []
        def a(l,v,cls=''):
            if v is None: return
            parts.append('<span class="attr %s">%s: %s</span>' % (cls,l,v))
        t=d.get("temperature")
        a("Temp", (str(t)+"°C") if t is not None else None,
          "bad" if t and t>=60 else "warn" if t and t>=45 else "ok" if t else "")
        r=d.get("reallocated")
        a("Realloc", r, "bad" if r else "ok")
        p=d.get("pending")
        a("Pending", p, "bad" if p else "ok")
        w=d.get("percentage_used")
        a("Wear", (str(w)+"%") if w is not None else None,
          "bad" if w and w>=90 else "warn" if w and w>=75 else "ok" if w is not None else "")
        sp=d.get("available_spare")
        a("Spare", (str(sp)+"%") if sp is not None else None,
          "bad" if sp is not None and sp<=10 else "warn" if sp is not None and sp<=20 else "ok" if sp is not None else "")
        h=d.get("power_on_hours")
        if h: a("Hours", h)
        vols = ", ".join("%s %.0f%%" % (v.get("drive",""),v.get("used_pct",0)) for v in d.get("volumes",[]))
        if vols: a("Volumes", vols)
        return "".join(parts)

    agent_blocks = ""
    for a in sel:
        sc  = (a.get("worst_status") or "Unknown").lower()
        ol  = "online" if a.get("online") else "offline"
        disk_rows = "".join(
            "<tr><td><strong>%s</strong><br><span style='color:#999;font-size:10px'>%s · %s · %s GB</span></td>"
            "<td><span class='badge b-%s'>%s</span></td>"
            "<td>%s</td></tr>" % (
                d.get("model","?"), d.get("serial","—"), d.get("interface","?"), d.get("size_gb","?"),
                (d.get("smart_status") or "Unknown").lower(),
                d.get("smart_status","?"),
                disk_attrs(d)
            ) for d in a.get("disks",[])
        )
        agent_blocks += (
            "<div class='agent-block'>"
            "<div class='agent-hdr'>"
            "<div><span class='agent-name'>%s</span>"
            " <span class='badge b-%s'>%s</span>"
            " <span class='badge b-%s'>%s</span></div>"
            "<div style='font-size:11px;color:#999'>%s · v%s · Last: %s</div>"
            "</div>"
            "<div style='padding:10px 14px'>"
            "<table><thead><tr><th>Disk</th><th>Status</th><th>Attributes</th></tr></thead>"
            "<tbody>%s</tbody></table></div></div>"
        ) % (
            a.get("hostname","?"),
            sc, a.get("worst_status","?"),
            ol, ol.title(),
            a.get("ip",""), a.get("agent_version","?"), a.get("last_seen","—"),
            disk_rows if disk_rows else "<tr><td colspan='3' style='color:#999'>No disk data</td></tr>"
        )

    summary = ("<div class='section'><h2>Summary — %d agent(s) selected</h2>"
        "<table><thead><tr><th>Hostname</th><th>IP</th><th>Status</th><th>Online</th><th>Disks</th><th>Last Seen</th></tr></thead><tbody>"
        "%s</tbody></table></div>") % (
        len(sel),
        "".join("<tr><td>%s</td><td>%s</td><td><span class='badge b-%s'>%s</span></td>"
                "<td><span class='badge b-%s'>%s</span></td><td>%d</td><td>%s</td></tr>" % (
            a.get("hostname","?"), a.get("ip",""),
            (a.get("worst_status") or "Unknown").lower(), a.get("worst_status","?"),
            "online" if a.get("online") else "offline",
            "Online" if a.get("online") else "Offline",
            len(a.get("disks",[])), a.get("last_seen","—")
        ) for a in sel)
    )

    html = ("<!DOCTYPE html><html><head><meta charset='UTF-8'>"
        "<title>DiskHealth — Combined Report</title><style>%s</style></head><body>"
        "<div class='hdr'>"
        "<div><h1>DiskHealth Report Master Sofa Sdn Bhd</h1>"
        "<div class='sub'>%d agents · %d disks · %.2f TB</div></div>"
        "<div class='sub'>%s<br><button onclick='window.print()'>Print / Save PDF</button></div>"
        "</div>"
        "%s"
        "<div class='section'><h2>Disk Details</h2>%s</div>"
        "</body></html>") % (
        css, len(sel), total_disks, total_tb, ts_label,
        summary, agent_blocks)

    return Response(html, mimetype="text/html")


@app.route("/api/export/combined.csv")
def export_combined_csv():
    """Combined CSV for selected agent IDs (passed as ?ids=id1,id2,...)"""
    ids = request.args.get("ids","").split(",")
    ids = [i.strip() for i in ids if i.strip()]
    if not ids:
        return jsonify({"error":"no ids provided"}),400
    ags = _load_agents_full()
    sel = [a for a in ags if a["agent_id"] in ids]
    if not sel:
        return jsonify({"error":"no matching agents"}),404

    rows = []
    for a in sel:
        for d in a.get("disks",[]):
            for v in (d.get("volumes") or [{}]):
                rows.append({
                    "hostname":a.get("hostname",""),
                    "ip":a.get("ip",""),
                    "online":"Yes" if a.get("online") else "No",
                    "model":d.get("model",""),
                    "serial":d.get("serial",""),
                    "interface":d.get("interface",""),
                    "size_gb":d.get("size_gb",""),
                    "smart_status":d.get("smart_status",""),
                    "temperature":d.get("temperature",""),
                    "reallocated":d.get("reallocated",""),
                    "pending":d.get("pending",""),
                    "uncorrectable":d.get("uncorrectable",""),
                    "wear_pct":d.get("percentage_used",""),
                    "spare_pct":d.get("available_spare",""),
                    "power_on_h":d.get("power_on_hours",""),
                    "volume":v.get("drive",""),
                    "vol_used_pct":v.get("used_pct",""),
                    "vol_free_gb":v.get("free_gb",""),
                })
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return _csv_resp(rows,
        ["hostname","ip","online","model","serial","interface","size_gb",
         "smart_status","temperature","reallocated","pending","uncorrectable",
         "wear_pct","spare_pct","power_on_h","volume","vol_used_pct","vol_free_gb"],
        "combined_%s.csv" % ts)




def _reevaluate_alerts():
    """
    Called immediately after settings save.
    - Creates new alerts for disks that now breach the new thresholds
    - Dismisses alerts for disks that no longer breach the thresholds
    Both directions, instant — no need to wait for agent report.
    """
    try:
        with db() as c:
            def _t(k, d):
                row = c.execute("SELECT value FROM settings WHERE key=?",(k,)).fetchone()
                try: return float(row["value"]) if row else float(d)
                except: return float(d)

            t = {
                "temp_warn":  _t("thresh_temp_warn",  45),
                "temp_crit":  _t("thresh_temp_crit",  60),
                "real_warn":  _t("thresh_realloc_warn", 1),
                "real_crit":  _t("thresh_realloc_crit", 5),
                "pend_warn":  _t("thresh_pending_warn", 1),
                "pend_crit":  _t("thresh_pending_crit", 5),
                "uncr_warn":  _t("thresh_uncorr_warn",  1),
                "uncr_crit":  _t("thresh_uncorr_crit",  1),
                "wear_warn":  _t("thresh_wear_warn",   75),
                "wear_crit":  _t("thresh_wear_crit",   90),
                "spar_warn":  _t("thresh_spare_warn",  20),
                "spar_crit":  _t("thresh_spare_crit",  10),
            }

            agents = c.execute("SELECT agent_id, hostname, disk_summary FROM agents").fetchall()

            fired = 0
            dismissed = 0

            for ag in agents:
                agent_id = ag["agent_id"]
                hostname = ag["hostname"] or "?"
                try:
                    disks = json.loads(ag["disk_summary"]) if ag["disk_summary"] else []
                except:
                    disks = []

                for disk in disks:
                    serial = disk.get("serial") or disk.get("model","?")
                    model  = disk.get("model","?")

                    issues_crit = []
                    issues_warn = []

                    temp = disk.get("temperature")
                    if temp is not None:
                        if   temp >= t["temp_crit"]: issues_crit.append("temperature %d°C (crit >= %d)" % (temp, int(t["temp_crit"])))
                        elif temp >= t["temp_warn"]: issues_warn.append("temperature %d°C (warn >= %d)" % (temp, int(t["temp_warn"])))

                    real = disk.get("reallocated")
                    if real is not None:
                        if   real >= t["real_crit"]: issues_crit.append("%d reallocated sectors" % real)
                        elif real >= t["real_warn"]: issues_warn.append("%d reallocated sectors" % real)

                    pend = disk.get("pending")
                    if pend is not None:
                        if   pend >= t["pend_crit"]: issues_crit.append("%d pending sectors" % pend)
                        elif pend >= t["pend_warn"]: issues_warn.append("%d pending sectors" % pend)

                    uncr = disk.get("uncorrectable")
                    if uncr is not None:
                        if   uncr >= t["uncr_crit"]: issues_crit.append("%d uncorrectable errors" % uncr)
                        elif uncr >= t["uncr_warn"]: issues_warn.append("%d uncorrectable errors" % uncr)

                    wear = disk.get("percentage_used")
                    if wear is not None:
                        if   wear >= t["wear_crit"]: issues_crit.append("SSD wear %d%%" % wear)
                        elif wear >= t["wear_warn"]: issues_warn.append("SSD wear %d%%" % wear)

                    spare = disk.get("available_spare")
                    if spare is not None:
                        if   spare <= t["spar_crit"]: issues_crit.append("spare %d%%" % spare)
                        elif spare <= t["spar_warn"]: issues_warn.append("spare %d%%" % spare)

                    merr = disk.get("media_errors")
                    if merr: issues_warn.append("%d media errors" % merr)

                    if issues_crit:
                        target_sev = "critical"
                        detail = ", ".join(issues_crit)
                        msg = "Disk '%s' (S/N: %s) on %s — CRITICAL: %s" % (model, serial, hostname, detail)
                    elif issues_warn:
                        target_sev = "warning"
                        detail = ", ".join(issues_warn)
                        msg = "Disk '%s' (S/N: %s) on %s — WARNING: %s" % (model, serial, hostname, detail)
                    else:
                        target_sev = None

                    existing = c.execute(
                        "SELECT id, severity FROM alerts WHERE agent_id=? AND disk_serial=? AND dismissed=0",
                        (agent_id, serial)).fetchall()

                    if target_sev:
                        correct_exists = any(e["severity"] == target_sev for e in existing)
                        if not correct_exists:
                            for e in existing:
                                c.execute("UPDATE alerts SET dismissed=1 WHERE id=?", (e["id"],))
                                dismissed += 1
                            c.execute(
                                "INSERT INTO alerts(agent_id,hostname,severity,message,disk_serial,created_at)"
                                " VALUES(?,?,?,?,?,?)",
                                (agent_id, hostname, target_sev, msg, serial, _now()))
                            broker.publish("alert", {
                                "agent_id":agent_id, "hostname":hostname,
                                "severity":target_sev, "message":msg})
                            _log_activity(c, agent_id, hostname, "alert",
                                          "[threshold change] " + msg)
                            fired += 1
                    else:
                        for e in existing:
                            c.execute("UPDATE alerts SET dismissed=1 WHERE id=?", (e["id"],))
                            dismissed += 1

            if fired > 0 or dismissed > 0:
                broker.publish("alerts_updated", {"fired": fired, "dismissed": dismissed})
                print("[thresholds] fired=%d dismissed=%d" % (fired, dismissed))

    except Exception as e:
        import traceback; traceback.print_exc()
        print("[reevaluate] error: %s" % e)


@app.route("/api/meta/<agent_id>", methods=["GET"])
def api_get_meta(agent_id):
    with db() as c:
        row = c.execute("SELECT * FROM agent_meta WHERE agent_id=?",(agent_id,)).fetchone()
    return jsonify(dict(row) if row else {})

@app.route("/api/meta/<agent_id>", methods=["POST"])
def api_set_meta(agent_id):
    data = request.get_json(force=True,silent=True) or {}
    now  = _now()
    with db() as c:
        ex = c.execute("SELECT agent_id FROM agent_meta WHERE agent_id=?",(agent_id,)).fetchone()
        if ex:
            fields,vals=[],[]
            for f in ("display_name","notes","location"):
                if f in data: fields.append(f+"=?"); vals.append(data[f])
            if fields:
                fields.append("updated_at=?"); vals.append(now); vals.append(agent_id)
                c.execute("UPDATE agent_meta SET "+",".join(fields)+" WHERE agent_id=?",vals)
        else:
            c.execute("INSERT INTO agent_meta(agent_id,display_name,notes,location,updated_at) VALUES(?,?,?,?,?)",
                      (agent_id,data.get("display_name"),data.get("notes"),data.get("location"),now))
    return jsonify({"status":"ok"})

@app.route("/api/uptime/<agent_id>")
def api_get_uptime(agent_id):
    with db() as c:
        row = c.execute("SELECT * FROM agent_uptime WHERE agent_id=?",(agent_id,)).fetchone()
    if not row:
        return jsonify({"online_since":None,"uptime_seconds":None})
    online_since = row["online_since"]
    if online_since:
        try:
            from datetime import datetime as _dt
            ts = _dt.fromisoformat(online_since)
            now_ts = _dt.now(timezone.utc)
            if ts.tzinfo is None:
                from datetime import timezone as _tz
                ts = ts.replace(tzinfo=_tz.utc)
            uptime_sec = int((now_ts - ts).total_seconds())
        except:
            uptime_sec = None
    else:
        uptime_sec = None
    return jsonify({"online_since":online_since,"uptime_seconds":uptime_sec,
                    "last_offline":row["last_offline"]})

@app.route("/api/replacements/<agent_id>", methods=["GET"])
def api_get_replacements(agent_id):
    with db() as c:
        rows = c.execute("SELECT * FROM disk_replacements WHERE agent_id=? ORDER BY created_at DESC",(agent_id,)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/replacements/<agent_id>", methods=["POST"])
def api_set_replacement(agent_id):
    data = request.get_json(force=True,silent=True) or {}
    serial = data.get("disk_serial","")
    if not serial: return jsonify({"error":"disk_serial required"}),400
    now = _now()
    with db() as c:
        c.execute("""INSERT INTO disk_replacements(agent_id,disk_serial,disk_model,status,note,scheduled_date,created_at)
                     VALUES(?,?,?,?,?,?,?)
                     ON CONFLICT(agent_id,disk_serial) DO UPDATE SET
                     status=excluded.status,note=excluded.note,
                     scheduled_date=excluded.scheduled_date""",
                  (agent_id,serial,data.get("disk_model",""),
                   data.get("status","scheduled"),data.get("note",""),
                   data.get("scheduled_date",""),now))
    return jsonify({"status":"ok"})

@app.route("/api/replacements/<agent_id>/<disk_serial>", methods=["DELETE"])
def api_delete_replacement(agent_id, disk_serial):
    with db() as c:
        c.execute("DELETE FROM disk_replacements WHERE agent_id=? AND disk_serial=?",(agent_id,disk_serial))
    return jsonify({"status":"ok"})

@app.route("/api/replacements/<agent_id>/<disk_serial>/complete", methods=["POST"])
def api_complete_replacement(agent_id, disk_serial):
    now = _now()
    with db() as c:
        c.execute("UPDATE disk_replacements SET status='completed',completed_at=? WHERE agent_id=? AND disk_serial=?",
                  (now,agent_id,disk_serial))
    return jsonify({"status":"ok"})


@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)

def _print_banner(port):
    sep = "=" * 60
    print(sep)
    print("  DiskHealth Agent Server  v2.3")
    print(sep)
    print("  Dashboard  ->  http://localhost:%d/" % port)
    print("  Health     ->  http://localhost:%d/health" % port)
    print("  Scripts    ->  %s/" % AGENT_DIR.resolve())
    print("  Database   ->  %s" % DB_PATH)
    print("  Offline threshold: %ds" % AGENT_OFFLINE_SECONDS)
    print(sep)

def _daemonise(pid_file, log_file):
    import resource
    try:
        if os.fork() > 0: os._exit(0)
    except OSError as e: sys.exit("[daemon] fork #1 failed: %s" % e)
    os.chdir("/"); os.setsid(); os.umask(0)
    try:
        if os.fork() > 0: os._exit(0)
    except OSError as e: sys.exit("[daemon] fork #2 failed: %s" % e)
    maxfd = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    if maxfd == resource.RLIM_INFINITY: maxfd = 1024
    for fd in range(3, maxfd):
        try: os.close(fd)
        except OSError: pass
    sys.stdout.flush(); sys.stderr.flush()
    with open(os.devnull,"rb") as f: os.dup2(f.fileno(), sys.stdin.fileno())
    lf = open(log_file, "ab+", buffering=0)
    os.dup2(lf.fileno(), sys.stdout.fileno())
    os.dup2(lf.fileno(), sys.stderr.fileno())
    with open(pid_file,"w") as pf: pf.write(str(os.getpid())+"\n")

def _stop_daemon(pid_file):
    import signal as _signal
    if not os.path.exists(pid_file): print("PID file not found: %s" % pid_file); sys.exit(1)
    with open(pid_file) as f: pid = int(f.read().strip())
    try: os.kill(pid, _signal.SIGTERM); os.remove(pid_file); print("[OK] Sent SIGTERM to PID %d" % pid)
    except ProcessLookupError: print("Stale PID, removing."); os.remove(pid_file); sys.exit(1)
    except PermissionError: sys.exit("Permission denied for PID %d" % pid)

def _status_daemon(pid_file):
    if not os.path.exists(pid_file): print("NOT running."); sys.exit(1)
    with open(pid_file) as f: pid = int(f.read().strip())
    try: os.kill(pid, 0); print("Running (PID %d)" % pid)
    except ProcessLookupError: print("NOT running (stale PID %d)." % pid); sys.exit(1)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="DiskHealth Agent Server v2.4")
    parser.add_argument("--host",   default=HOST)
    parser.add_argument("--port",   default=PORT, type=int)
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--stop",   action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--pid",    default="diskhealth.pid")
    parser.add_argument("--log",    default="diskhealth.log")
    parser.add_argument("--db",     default=DB_PATH)
    args = parser.parse_args()
    if args.db != DB_PATH: DB_PATH = args.db
    if args.stop:   _stop_daemon(args.pid);   sys.exit(0)
    if args.status: _status_daemon(args.pid); sys.exit(0)
    init_db()
    AGENT_DIR.mkdir(exist_ok=True)
    _init_scripts()
    if args.daemon:
        _print_banner(args.port)
        print("  Starting daemon…")
        _daemonise(args.pid, args.log)
        _start_background_threads()
        app.run(host=args.host, port=args.port, threaded=True, debug=False)
    else:
        _print_banner(args.port)
        _start_background_threads()
        app.run(host=args.host, port=args.port, threaded=True, debug=False)
