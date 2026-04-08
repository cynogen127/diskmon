                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           <img width="1919" height="1032" alt="image" src="https://github.com/user-attachments/assets/c22c0bdd-0dcd-4663-a856-57ea9ca19fa6" />


# This web panel doesn't have any security either login by password or key and anything else,means use on your local network only and makesure only authorized person only can access it
# 💾 DiskHealth Fleet Monitor

A self-hosted Windows disk health monitoring system. A lightweight Python/Flask server collects SMART data, temperatures, and volume usage from Windows agents across your network and displays everything in a real-time web dashboard.

![Python](https://img.shields.io/badge/Python-3.8+-blue)
![Flask](https://img.shields.io/badge/Flask-2.x-green)
![Platform](https://img.shields.io/badge/Agent-Windows-0078D6?logo=windows)
![License](https://img.shields.io/badge/License-MIT-yellow)

---

## Screenshots

| Dashboard Overview | Heatmap | Trends |
|---|---|---|
| Real-time fleet status | Desktop grid with health colours | Per-disk SMART charts over time |

---

## Features

- **Real-time fleet monitoring** via Server-Sent Events (SSE) — no page refresh needed
- **SMART data collection** — temperature, reallocated sectors, pending sectors, uncorrectable errors, SSD wear, spare capacity, power-on hours
- **Disk health heatmap** — colour-coded grid (green/yellow/red) for instant fleet overview
- **Trend charts** — historical graphs per disk metric using Chart.js
- **Fleet analytics** — reports per day, alerts per day, disk status breakdown
- **Alert system** — automatic critical/warning alerts with dismissal
- **Remote commands** — ping, refresh, update agent, clear log — sent to any agent
- **Script manager** — edit and push PowerShell agent scripts from the browser
- **Bulk commands** — send commands to all online agents at once
- **Export system** — 7 export types (CSV + HTML reports, per-agent and fleet-wide)
- **Settings** — configurable alert thresholds, poll intervals per agent, offline detection
- **Collapsible sidebar** — full-width main view when needed
- **Dark/light theme** toggle
- **Command history** — deletable per-row or clear all

---

## Architecture

```
Windows Desktop (Agent)          Linux Server (Flask)
┌─────────────────────┐          ┌──────────────────────┐
│  DiskHealthAgent.ps1│◄────────►│  panel.py     │
│  - Reads SMART data │  HTTP    │  - SQLite database   │
│  - Polls for cmds   │  REST    │  - SSE push events   │
│  - Reports disks    │          │  - Web dashboard     │
└─────────────────────┘          └──────────────────────┘
                                          │
                                 http://server:8765
                                          │
                                 ┌────────▼────────┐
                                 │  Browser (Any)  │
                                 │  Dashboard UI   │
                                 └─────────────────┘
```

---

## Requirements

### Server
- Python 3.8+
- Flask (`pip install flask`)
- SQLite (included with Python)
- Linux/Windows/macOS

### Agent (Windows machines being monitored)
- Windows 10/11 or Windows Server 2016+
- PowerShell 5.1+
- `smartctl` (from [smartmontools](https://www.smartmontools.org/)) in PATH

---

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/cynogen127/diskmon.git
cd diskmon
```

### 2. Install dependencies

```bash
pip install flask
```

### 3. Download Chart.js (required for Trends/Analytics charts)

```bash
curl -Lo chart.umd.min.js \
  https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js
```

> If your browser cannot reach the internet directly, the server serves Chart.js locally at `/chart.js`. Download the file on the server machine and place it in the same directory as `panel.py`.

### 4. Start the server

```bash
python3 server.py
```

Default port: **8765**. Open `http://your-server-ip:8765` in a browser.

### 5. Install the agent on Windows machines

On each Windows machine you want to monitor, open PowerShell as Administrator:

```powershell
# Replace with your server IP/hostname
$ServerUrl = "http://192.168.0.150:8765"

Invoke-WebRequest -UseBasicParsing `
  -Uri "$ServerUrl/download/installer" `
  -OutFile "install-agent.ps1"

.\install-agent.ps1 -ServerUrl $ServerUrl
```

The installer:
- Downloads the agent script
- Installs it as a scheduled task (runs at startup)
- Starts the agent immediately

---

## Command Line Options

```
python3 panel.py [options]

  --host HOST       Bind address (default: 0.0.0.0)
  --port PORT       Port number (default: 8765)
  --db PATH         Database file path (default: diskhealth.db)
  --daemon          Run as background daemon (Linux)
  --stop            Stop the daemon
  --status          Check daemon status
  --pid FILE        PID file path (default: diskhealth.pid)
  --log FILE        Log file path (default: diskhealth.log)
```

### Run as daemon (Linux)

```bash
# Start
python3 server.py --daemon

# Check status
python3 server.py --status

# Stop
python3 server.py --stop
```

---

## Dashboard Tabs

| Tab | Description |
|---|---|
| **Overview** | Selected agent's system info, active alerts, disk health cards with temperature gauge |
| **Alerts** | Fleet-wide alert list — filter by critical/warning, dismiss individually or all |
| **History** | Per-agent report history (last 50 reports) |
| **Activity Log** | Server-wide event log — registrations, reports, commands, alerts |
| **Commands** | Queue remote commands to agents — ping, refresh, update, clear log |
| **Scripts** | Edit and push PowerShell agent scripts from the browser |
| **Trends** | Historical SMART metric charts per disk |
| **Heatmap** | Colour-coded desktop grid — click any machine to inspect it |
| **Analytics** | Fleet-wide charts — reports/day, alerts/day, disk status donut |
| **All Agents** | Expandable list of every agent with inline disk details |
| **Settings** | Alert thresholds, poll intervals, auto-deregister, exports |

---

## Exports

### Fleet-wide
| Export | Contents |
|---|---|
| Fleet CSV | All agents — hostname, IP, status, disk count, last seen |
| Inventory CSV | Every disk across all machines — model, serial, SMART attributes |
| Fleet Report HTML | Printable/PDF report with all agents and disks |
| Audit CSV | Last 5000 activity log entries |
| Alerts CSV | Active or dismissed alerts |

### Per-agent (select one or more agents)
| Export | Contents |
|---|---|
| Agent CSV (each) | One CSV per selected agent — disks + volumes |
| Agent Report HTML (each) | One printable report per selected agent |
| Combined CSV | All selected agents merged into a single CSV |
| Combined Report HTML | All selected agents in one printable/PDF report |

---

## API Reference

### Agent endpoints (used by PowerShell agent)

| Method | Endpoint | Description |
|---|---|---|
| POST | `/api/register` | Agent registration |
| POST | `/api/report` | Submit disk health report |
| GET | `/api/commands/<agent_id>` | Poll for pending commands |
| POST | `/api/ack` | Acknowledge command result |
| GET | `/agent/agent.ps1` | Download agent script |
| GET | `/agent/tray.ps1` | Download tray script |

### Dashboard endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/agents` | List all agents |
| GET | `/api/agents/<id>` | Single agent detail |
| DELETE | `/api/agents/<id>` | Remove agent and all data |
| GET | `/api/alerts` | Active alerts |
| POST | `/api/alerts/dismiss_all` | Dismiss all alerts |
| GET | `/api/stats` | Fleet statistics |
| POST | `/api/command` | Queue a command |
| GET | `/api/commands-all` | Command history |
| GET | `/api/trends/<agent_id>` | List disks with trend data |
| GET | `/api/trends/<agent_id>/<serial>` | Trend series data |
| GET | `/api/analytics/reports_daily` | Reports per day |
| GET | `/api/analytics/alerts_daily` | Alerts per day |
| GET | `/api/settings` | Get all settings |
| POST | `/api/settings` | Save settings |
| GET | `/api/stream` | SSE event stream |

---

## Alert Thresholds (defaults, configurable in Settings)

| Metric | Warning | Critical |
|---|---|---|
| Temperature | ≥ 45°C | ≥ 60°C |
| Reallocated sectors | ≥ 1 | ≥ 5 |
| Pending sectors | ≥ 1 | ≥ 5 |
| Uncorrectable errors | ≥ 1 | ≥ 1 |
| SSD wear | ≥ 75% | ≥ 90% |
| Spare capacity | ≤ 20% | ≤ 10% |

---

## File Structure

```
diskmon/
├── server.py          # Main server (run this)
├── chart.umd.min.js         # Chart.js (local copy)
├── diskhealth.db            # SQLite database (auto-created)
├── agent_scripts/
│   ├── DiskHealthAgent.ps1  # Windows agent script
│   ├── DiskHealthTray.ps1   # System tray helper
│   └── install-agent.ps1    # One-line installer
└── patch_scripts/           # Incremental patch scripts (dev use)
```

---

## Database

SQLite database auto-created at `diskhealth.db` on first run.

| Table | Description |
|---|---|
| `agents` | Registered agents and their current status |
| `reports` | Historical disk reports (last 100 per agent) |
| `commands` | Command queue and history |
| `alerts` | Active and dismissed alerts |
| `activity_log` | Server event log |
| `disk_trends` | Historical SMART metric data points |
| `settings` | Configurable server settings |
| `agent_poll_intervals` | Per-agent custom poll intervals |

---

## Troubleshooting

**Tabs not clickable / dashboard blank**
- Check browser console (F12) for JavaScript errors
- Hard refresh: `Ctrl + Shift + R`
- Make sure `chart.umd.min.js` exists in the server directory

**Charts blank (no lines)**
- Wait for more data points — charts need at least 2–3 reports to draw a line
- Switch to a wider time window (7d) if agent was recently restarted

**Agent shows offline immediately**
- Default offline threshold is 180 seconds
- Adjustable in Settings → General → Offline threshold

**USB drives appear in Trends**
- Unplug and replug — next agent report clears stale trend data automatically
- USB flash drives typically have no SMART data to trend

**Chart.js not loading**
- Confirm `chart.umd.min.js` is in the same folder as `panel.py`
- Test: `curl http://your-server:8765/chart.js | head -c 30`
- Should show: `!function(t,e){...`

---

## License

MIT License — free to use, modify, and distribute.

---

## Contributing

Pull requests welcome. Please test against a live agent before submitting dashboard changes.






