#!/usr/bin/env python3
"""
Busca dados atuais da usina GoodWe no SEMS Portal e gera o arquivo
index.html (snapshot estatico, com JS inline sem chamadas externas).

Versao para rodar em CI (GitHub Actions): le credenciais de variaveis
de ambiente e usa caminhos relativos ao repositorio.

Uso: python3 scripts/generate_dashboard.py
Saida: escreve index.html na raiz do repositorio
"""
import base64
import csv
import json
import os
import re
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

BRAZIL_TZ = timezone(timedelta(hours=-3))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(REPO_ROOT, "data", "monitor.log")
HISTORY_PATH = os.path.join(REPO_ROOT, "data", "history.csv")
OUTPUT_PATH = os.path.join(REPO_ROOT, "index.html")
FONT_PATH = os.path.join(REPO_ROOT, "assets", "inter-variable.woff2")

GITHUB_REPO_SLUG = "IbyangaEdi/UsinaSolar"
GITHUB_WORKFLOW_FILE = "monitor.yml"


def fmt_br(value, decimals=2):
    s = f"{float(value):,.{decimals}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_br_compact(value, decimals=2, threshold=1000):
    value = float(value)
    if abs(value) >= threshold:
        return fmt_br(value / 1000, 1) + "k"
    return fmt_br(value, decimals)


def creds_from_env():
    return {
        "SEMS_ACCOUNT": os.environ["SEMS_ACCOUNT"],
        "SEMS_PASSWORD": os.environ["SEMS_PASSWORD"],
        "PUBLIC_DISPLAY_NAME": os.environ.get("PUBLIC_DISPLAY_NAME", "Usina Solar"),
        "GH_DISPATCH_TOKEN": os.environ.get("GH_DISPATCH_TOKEN", ""),
    }


def call(url, body_obj, token_header=None):
    headers = {"Content-Type": "application/json;charset=UTF-8"}
    if token_header:
        headers["Token"] = token_header
    else:
        headers["Token"] = json.dumps({"version": "v2.1.0", "client": "web", "language": "en"})
    req_body = json.dumps(body_obj).encode()
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, data=req_body, method="POST", headers=headers)
            with urllib.request.urlopen(req, timeout=25) as resp:
                return json.loads(resp.read().decode())
        except Exception:
            if attempt == 1:
                raise
            time.sleep(2)


def sems_login(account, pwd):
    payload = call("https://www.semsportal.com/api/v2/Common/CrossLogin", {
        "account": account, "pwd": pwd, "agreement_agreement": 0, "is_local": True,
    })
    if payload.get("hasError"):
        raise RuntimeError(f"SEMS login failed: {payload.get('msg')}")
    d = payload["data"]
    return json.dumps({
        "uid": d["uid"], "timestamp": d["timestamp"], "token": d["token"],
        "client": d["client"], "version": d["version"], "language": d["language"],
    })


def get_station(token_header):
    payload = call("https://www.semsportal.com/api/PowerStationMonitor/QueryPowerStationMonitor", {}, token_header)
    if payload.get("hasError"):
        raise RuntimeError(f"SEMS query failed: {payload.get('msg')}")
    return payload["data"]["list"][0]


def get_detail(pid, token_header):
    try:
        payload = call("https://www.semsportal.com/api/v2/PowerStation/GetMonitorDetailByPowerstationId",
                        {"powerStationId": pid}, token_header)
    except Exception:
        return {}
    if payload.get("hasError"):
        return {}
    return payload.get("data") or {}


NUM_RE = re.compile(r"-?\d+(\.\d+)?")


def parse_number(text):
    if not text:
        return 0.0
    m = NUM_RE.search(text)
    return float(m.group()) if m else 0.0


def get_inverters(detail, in_window):
    inverters = []
    for e in detail.get("equipment") or []:
        power = parse_number(e.get("powerGeneration"))
        eday = parse_number(e.get("eday"))
        if power > 0:
            state = "good"
        elif in_window:
            state = "critical"
        else:
            state = "neutral"
        name = e.get("title", "Microinversor").replace("Edval", "6").replace("Edenise", "24").strip()
        inverters.append({
            "name": name, "sn": e.get("sn", ""), "power": power, "eday": eday, "state": state,
        })
    return inverters


def get_today_curve(pid, token_header, date_str):
    try:
        payload = call("https://www.semsportal.com/api/v2/Charts/GetPlantPowerChart", {
            "id": pid, "date": date_str, "range": 2, "chartIndexId": "8", "isDetailFull": "",
        }, token_header)
    except Exception:
        return []
    lines = payload.get("data", {}).get("lines") or []
    for line in lines:
        if line.get("key") == "PCurve_Power_PV":
            return [{"t": p["x"], "kw": round((p["y"] or 0.0) / 1000.0, 3)} for p in line.get("xy", [])]
    return []


def read_daily_history(days=14):
    if not os.path.exists(HISTORY_PATH):
        return []
    by_day = defaultdict(lambda: {"eday": 0.0, "readings": 0, "zero_in_window": 0})
    with open(HISTORY_PATH) as f:
        for row in csv.DictReader(f):
            ts = row["timestamp"]
            day = ts[:10]
            try:
                eday = float(row["eday_kwh"])
                pac = float(row["pac_kw"])
            except (ValueError, KeyError):
                continue
            entry = by_day[day]
            entry["eday"] = max(entry["eday"], eday)
            entry["readings"] += 1
            hour = int(ts[11:13])
            if 7 <= hour < 17 and pac <= 0.0:
                entry["zero_in_window"] += 1
    cutoff = (datetime.now(BRAZIL_TZ) - timedelta(days=days)).date().isoformat()
    days_list = sorted(d for d in by_day if d >= cutoff)
    return [{"date": d, "kwh": round(by_day[d]["eday"], 2)} for d in days_list]


def read_events(limit=10):
    if not os.path.exists(LOG_PATH):
        return []
    pattern = re.compile(r"^(\S+) EVENT (ALERT|RECOVERED) (.*)$")
    events = []
    with open(LOG_PATH) as f:
        for line in f:
            m = pattern.match(line.strip())
            if m:
                events.append({"time": m.group(1), "type": m.group(2), "detail": m.group(3)})
    return events[-limit:][::-1]


HTML_TEMPLATE = """<!doctype html>
<title>GoodWe · Painel da usina</title>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
@font-face {
  font-family: 'Inter';
  font-style: normal;
  font-weight: 100 900;
  font-display: swap;
  src: url(data:font/woff2;base64,__INTER_FONT_BASE64__) format('woff2');
}
:root {
  --bg: #F4F5F7;
  --surface: #FFFFFF;
  --surface-2: #EEF0F3;
  --ink: #101826;
  --ink-2: #4B5768;
  --ink-3: #8592A3;
  --border: #E2E6EC;
  --accent: #2E5FE0;
  --accent-soft: #E4EAFC;
  --gold: #E7A93E;
  --gold-soft: #FBEDD2;
  --good: #1E9E74;
  --good-soft: #DDF3EA;
  --warning: #D97706;
  --warning-soft: #FCEAD4;
  --critical: #DC2626;
  --critical-soft: #FBE1E1;
  --shadow: 0 2px 8px rgba(16, 24, 38, 0.05), 0 20px 40px -22px rgba(16, 24, 38, 0.16);
  --shadow-sm: 0 1px 4px rgba(16, 24, 38, 0.07);
  --header-bg: #12151D;
  --header-ink: #F5F6F8;
  --header-ink-2: #99A2B3;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0B111C;
    --surface: #121A28;
    --surface-2: #182337;
    --ink: #EAF0F8;
    --ink-2: #B7C2D0;
    --ink-3: #7C8A9C;
    --border: #223049;
    --accent: #5B9BFF;
    --accent-soft: #17253F;
    --gold: #F3BE63;
    --gold-soft: #2B2312;
    --good: #34D399;
    --good-soft: #113228;
    --warning: #FBBF24;
    --warning-soft: #33260B;
    --critical: #F87171;
    --critical-soft: #331414;
    --shadow: 0 2px 8px rgba(0,0,0,0.28), 0 22px 44px -22px rgba(0,0,0,0.6);
    --shadow-sm: 0 1px 4px rgba(0,0,0,0.3);
    --header-bg: #060910;
    --header-ink: #EAF0F8;
    --header-ink-2: #6B7A8F;
  }
}
:root[data-theme="dark"] {
  --bg: #0B111C; --surface: #121A28; --surface-2: #182337; --ink: #EAF0F8;
  --ink-2: #B7C2D0; --ink-3: #7C8A9C; --border: #223049; --accent: #5B9BFF;
  --accent-soft: #17253F; --gold: #F3BE63; --gold-soft: #2B2312; --good: #34D399;
  --good-soft: #113228; --warning: #FBBF24; --warning-soft: #33260B; --critical: #F87171;
  --critical-soft: #331414; --shadow: 0 2px 8px rgba(0,0,0,0.28), 0 22px 44px -22px rgba(0,0,0,0.6);
  --shadow-sm: 0 1px 4px rgba(0,0,0,0.3);
  --header-bg: #060910; --header-ink: #EAF0F8; --header-ink-2: #6B7A8F;
}
:root[data-theme="light"] {
  --bg: #F4F5F7; --surface: #FFFFFF; --surface-2: #EEF0F3; --ink: #101826;
  --ink-2: #4B5768; --ink-3: #8592A3; --border: #E2E6EC; --accent: #2E5FE0;
  --accent-soft: #E4EAFC; --gold: #E7A93E; --gold-soft: #FBEDD2; --good: #1E9E74;
  --good-soft: #DDF3EA; --warning: #D97706; --warning-soft: #FCEAD4; --critical: #DC2626;
  --critical-soft: #FBE1E1; --shadow: 0 2px 8px rgba(16, 24, 38, 0.05), 0 20px 40px -22px rgba(16, 24, 38, 0.16);
  --shadow-sm: 0 1px 4px rgba(16, 24, 38, 0.07);
  --header-bg: #12151D; --header-ink: #F5F6F8; --header-ink-2: #99A2B3;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: 'Inter', sans-serif;
  -webkit-font-smoothing: antialiased;
  padding: clamp(16px, 4vw, 40px);
}
.wrap { max-width: 1080px; margin: 0 auto; display: flex; flex-direction: column; gap: 20px; }
.mono { font-family: 'Inter', sans-serif; font-variant-numeric: tabular-nums; }
.eyebrow { text-transform: uppercase; letter-spacing: 0.08em; font-size: 12px; font-weight: 600; color: var(--header-ink-2); }

.topbar {
  display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px;
  background: var(--header-bg); color: var(--header-ink);
  padding: 22px 24px; border-radius: 16px; box-shadow: var(--shadow);
}
.plant-name { color: var(--header-ink); font-size: clamp(22px, 3vw, 30px); font-weight: 700; letter-spacing: -0.01em; text-wrap: balance; }
.plant-meta { color: var(--header-ink-2); font-size: 14px; margin-top: 2px; }
.status-block { display: flex; flex-direction: column; align-items: center; gap: 14px; }
.updated { color: var(--header-ink-2); font-size: 13px; text-align: center; }
.refresh-btn {
  font: inherit; font-size: 13px; font-weight: 600; color: #FFFFFF;
  background: #1E9E74; border: 1px solid transparent;
  border-radius: 999px; padding: 7px 16px; cursor: pointer;
  transition: background 0.15s ease;
}
.refresh-btn:hover:not(:disabled) { background: #178363; }
.refresh-btn:active:not(:disabled) { background: #13744F; }
.refresh-btn:focus-visible { outline: 2px solid #FFFFFF; outline-offset: 2px; }
.refresh-btn:disabled { opacity: 0.7; cursor: default; }

.pill { display: inline-flex; align-items: center; justify-content: center; gap: 6px; padding: 5px 12px; border-radius: 999px; font-size: 13px; font-weight: 600; }
.pill::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.pill-good { background: var(--good-soft); color: var(--good); }
.pill-warning { background: var(--warning-soft); color: var(--warning); }
.pill-critical { background: var(--critical-soft); color: var(--critical); }
.pill-neutral { background: var(--surface-2); color: var(--ink-2); }

.kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; }
.card { background: var(--surface); border-radius: 14px; box-shadow: var(--shadow); }
.kpi { padding: 16px; display: flex; flex-direction: column; gap: 6px; }
.kpi-label { font-size: 12px; color: var(--ink-3); font-weight: 600; }
.kpi-value { font-size: 24px; font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.kpi-value .unit { font-size: 13px; font-weight: 500; color: var(--ink-3); margin-left: 3px; }

.section-title { font-size: 15px; font-weight: 700; }
.section-sub { font-size: 13px; color: var(--ink-3); }

.chart-card { padding: 18px 18px 8px; display: flex; flex-direction: column; gap: 10px; }
.chart-head { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; flex-wrap: wrap; }
svg.chart { width: 100%; height: auto; display: block; overflow: visible; }
.chart-tooltip {
  position: absolute; pointer-events: none; background: var(--ink); color: var(--bg);
  font-size: 12px; padding: 6px 9px; border-radius: 8px; transform: translate(-50%, -115%);
  white-space: nowrap; opacity: 0; transition: opacity 0.1s ease; box-shadow: var(--shadow);
}
.chart-tooltip.show { opacity: 1; }
.chart-wrap { position: relative; }

.two-col { display: grid; grid-template-columns: 1.4fr 1fr; gap: 16px; }
@media (max-width: 720px) { .two-col { grid-template-columns: 1fr; } }

.info-list { display: flex; flex-direction: column; }
.info-row { display: flex; justify-content: space-between; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--border); font-size: 14px; }
.info-row:last-child { border-bottom: none; }
.info-row span:first-child { color: var(--ink-2); }
.info-row span:last-child { font-weight: 600; }

.inverter-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 10px; }
.inverter-card { display: flex; align-items: center; gap: 10px; padding: 12px 14px; border-radius: 12px; background: var(--surface-2); box-shadow: var(--shadow-sm); }
.inverter-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
.inverter-dot.good { background: var(--good); box-shadow: 0 0 0 3px var(--good-soft); }
.inverter-dot.critical { background: var(--critical); box-shadow: 0 0 0 3px var(--critical-soft); }
.inverter-dot.neutral { background: var(--ink-3); box-shadow: 0 0 0 3px var(--surface); }
.inverter-info { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
.inverter-name { font-size: 13px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.inverter-metric { font-size: 12px; color: var(--ink-3); }

.events { display: flex; flex-direction: column; }
.event-row { display: flex; gap: 10px; padding: 10px 0; border-bottom: 1px solid var(--border); font-size: 14px; align-items: flex-start; }
.event-row:last-child { border-bottom: none; }
.event-stripe { width: 3px; align-self: stretch; border-radius: 2px; flex-shrink: 0; }
.event-stripe.alert { background: var(--critical); }
.event-stripe.recovered { background: var(--good); }
.event-body { display: flex; flex-direction: column; gap: 2px; }
.event-title { font-weight: 600; }
.event-time { color: var(--ink-3); font-size: 12px; }
.empty-state { color: var(--ink-3); font-size: 14px; padding: 6px 0; }

footer { color: var(--ink-3); font-size: 12px; text-align: center; padding: 8px 0 0; }
</style>

<div class="wrap">
  <div class="topbar">
    <div>
      <div class="eyebrow">Painel &middot; GoodWe SEMS</div>
      <div class="plant-name">__STATION_NAME__</div>
      <div class="plant-meta">__LOCATION__ &middot; __CAPACITY__ kWp instalados</div>
    </div>
    <div class="status-block">
      __STATUS_PILL__
      <div class="updated mono">Atualizado __UPDATED_AT__</div>
      <button id="refreshBtn" class="refresh-btn">Atualizar agora</button>
    </div>
  </div>

  <div class="kpi-grid">
    <div class="card kpi">
      <div class="kpi-label">Potência agora</div>
      <div class="kpi-value mono">__PAC__<span class="unit">kW</span></div>
    </div>
    <div class="card kpi">
      <div class="kpi-label">Gerado hoje</div>
      <div class="kpi-value mono">__EDAY__<span class="unit">kWh</span></div>
    </div>
    <div class="card kpi">
      <div class="kpi-label">Este mês</div>
      <div class="kpi-value mono">__EMONTH__<span class="unit">kWh</span></div>
    </div>
    <div class="card kpi">
      <div class="kpi-label">Total histórico</div>
      <div class="kpi-value mono">__ETOTAL__<span class="unit">kWh</span></div>
    </div>
    <div class="card kpi">
      <div class="kpi-label">Receita hoje</div>
      <div class="kpi-value mono">R$ __INCOME_DAY__</div>
    </div>
    <div class="card kpi">
      <div class="kpi-label">Receita total</div>
      <div class="kpi-value mono">R$ __INCOME_TOTAL__</div>
    </div>
  </div>

  <div class="card chart-card">
    <div class="chart-head">
      <div>
        <div class="section-title">Curva de geração de hoje</div>
        <div class="section-sub">Potência instantânea (kW), leituras a cada 5 minutos</div>
      </div>
    </div>
    <div class="chart-wrap" id="curveWrap">
      <div class="chart-tooltip" id="curveTooltip"></div>
    </div>
  </div>

  <div class="card" style="padding: 18px;">
    <div class="section-title" style="margin-bottom: 2px;">Microinversores</div>
    <div class="section-sub" style="margin-bottom: 12px;">Status individual de cada unidade</div>
    <div class="inverter-grid">__INVERTERS_HTML__</div>
  </div>

  <div class="two-col">
    <div class="card chart-card">
      <div class="chart-head">
        <div>
          <div class="section-title">Geração diária</div>
          <div class="section-sub">Últimos dias monitorados (kWh)</div>
        </div>
      </div>
      <div class="chart-wrap" id="dailyWrap">
        <div class="chart-tooltip" id="dailyTooltip"></div>
      </div>
    </div>

    <div class="card" style="padding: 18px;">
      <div class="section-title" style="margin-bottom: 10px;">Condições agora</div>
      <div class="info-list">
        <div class="info-row"><span>Clima</span><span>__WEATHER_COND__</span></div>
        <div class="info-row"><span>Temperatura</span><span>__WEATHER_TEMP__ &deg;C</span></div>
        <div class="info-row"><span>Umidade</span><span>__WEATHER_HUM__%</span></div>
        <div class="info-row"><span>Nebulosidade</span><span>__WEATHER_CLOUD__%</span></div>
        <div class="info-row"><span>Vento</span><span>__WEATHER_WIND__</span></div>
      </div>
    </div>
  </div>

  <div class="card" style="padding: 18px;">
    <div class="section-title" style="margin-bottom: 4px;">Eventos recentes</div>
    <div class="section-sub" style="margin-bottom: 8px;">Alertas de falha e recuperação detectados pelo monitor</div>
    <div class="events" id="eventsList">__EVENTS_HTML__</div>
  </div>

  <footer>Painel atualizado automaticamente via GitHub Actions &middot; dados do SEMS Portal (GoodWe)</footer>
</div>

<script>
const curveData = __CURVE_JSON__;
const dailyData = __DAILY_JSON__;
const GH_REPO = '__GH_REPO_SLUG__';
const GH_WORKFLOW = '__GH_WORKFLOW_FILE__';
const GH_TOKEN = atob('__GH_DISPATCH_TOKEN_B64__');

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function fmtBR(n, decimals) {
  return n.toLocaleString('pt-BR', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

function drawAreaChart(containerId, tooltipId, data, opts) {
  const container = document.getElementById(containerId);
  const tooltip = document.getElementById(tooltipId);
  if (!data || data.length === 0) {
    container.innerHTML = '<div class="empty-state">Sem dados suficientes ainda.</div>';
    return;
  }
  const width = 1000, height = 220, padL = 4, padR = 4, padT = 14, padB = 24;
  const values = data.map(d => d.kw);
  const maxV = Math.max(...values, 0.1);
  const n = data.length;
  const x = i => padL + (i / (n - 1 || 1)) * (width - padL - padR);
  const y = v => height - padB - (v / maxV) * (height - padT - padB);

  const gold = cssVar('--gold');
  const goldSoft = cssVar('--gold-soft');
  const border = cssVar('--border');
  const ink3 = cssVar('--ink-3');

  let path = `M ${x(0)} ${y(values[0])}`;
  for (let i = 1; i < n; i++) path += ` L ${x(i)} ${y(values[i])}`;
  const areaPath = `${path} L ${x(n - 1)} ${height - padB} L ${x(0)} ${height - padB} Z`;

  const gridLines = [0.25, 0.5, 0.75, 1].map(f => {
    const yy = height - padB - f * (height - padT - padB);
    return `<line x1="${padL}" x2="${width - padR}" y1="${yy}" y2="${yy}" stroke="${border}" stroke-width="1" />`;
  }).join('');

  const hourTicks = [];
  for (let h = 0; h <= 24; h += 4) {
    const idx = data.findIndex(d => d.t === (h < 10 ? '0' + h : h) + ':00');
    if (idx >= 0) hourTicks.push(`<text x="${x(idx)}" y="${height - 6}" font-size="10" fill="${ink3}" text-anchor="middle">${h}h</text>`);
  }

  const last = data[n - 1];
  const svg = `
  <svg class="chart" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
    ${gridLines}
    <path d="${areaPath}" fill="${goldSoft}" />
    <path d="${path}" fill="none" stroke="${gold}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
    <circle cx="${x(n - 1)}" cy="${y(last.kw)}" r="4" fill="${gold}" stroke="${cssVar('--surface')}" stroke-width="2" />
    ${hourTicks.join('')}
    <rect id="${containerId}_hit" x="0" y="0" width="${width}" height="${height}" fill="transparent" style="cursor: crosshair;" />
  </svg>`;
  container.innerHTML = svg + tooltip.outerHTML;
  const newTooltip = document.getElementById(tooltipId);
  const svgEl = container.querySelector('svg');
  const hit = document.getElementById(containerId + '_hit');

  hit.addEventListener('mousemove', (e) => {
    const rect = svgEl.getBoundingClientRect();
    const relX = (e.clientX - rect.left) / rect.width;
    const idx = Math.min(n - 1, Math.max(0, Math.round(relX * (n - 1))));
    const d = data[idx];
    const px = (x(idx) / width) * rect.width;
    const py = (y(d.kw) / height) * rect.height;
    newTooltip.style.left = px + 'px';
    newTooltip.style.top = py + 'px';
    newTooltip.textContent = `${d.t} — ${fmtBR(d.kw, 2)} kW`;
    newTooltip.classList.add('show');
  });
  hit.addEventListener('mouseleave', () => newTooltip.classList.remove('show'));
}

function drawBarChart(containerId, tooltipId, data) {
  const container = document.getElementById(containerId);
  const tooltip = document.getElementById(tooltipId);
  if (!data || data.length === 0) {
    container.innerHTML = '<div class="empty-state">O histórico crescerá a cada checagem do monitor.</div>';
    return;
  }
  const width = 640, height = 220, padL = 4, padR = 4, padT = 14, padB = 24;
  const n = data.length;
  const maxV = Math.max(...data.map(d => d.kwh), 0.1);
  const gap = 6;
  const barW = (width - padL - padR - gap * (n - 1)) / n;
  const accent = cssVar('--accent');
  const border = cssVar('--border');
  const ink3 = cssVar('--ink-3');

  const gridLines = [0.5, 1].map(f => {
    const yy = height - padB - f * (height - padT - padB);
    return `<line x1="${padL}" x2="${width - padR}" y1="${yy}" y2="${yy}" stroke="${border}" stroke-width="1" />`;
  }).join('');

  let bars = '';
  data.forEach((d, i) => {
    const bx = padL + i * (barW + gap);
    const bh = (d.kwh / maxV) * (height - padT - padB);
    const by = height - padB - bh;
    bars += `<rect data-i="${i}" x="${bx}" y="${by}" width="${barW}" height="${Math.max(bh,1)}" rx="3" fill="${accent}" style="cursor:pointer;" />`;
  });

  const labelStep = Math.ceil(n / 6);
  let labels = '';
  data.forEach((d, i) => {
    if (i % labelStep === 0) {
      const bx = padL + i * (barW + gap) + barW / 2;
      const short = d.date.slice(5).replace('-', '/');
      labels += `<text x="${bx}" y="${height - 6}" font-size="10" fill="${ink3}" text-anchor="middle">${short}</text>`;
    }
  });

  container.innerHTML = `<svg class="chart" viewBox="0 0 ${width} ${height}">${gridLines}${bars}${labels}</svg>` + tooltip.outerHTML;
  const newTooltip = document.getElementById(tooltipId);
  container.querySelectorAll('rect[data-i]').forEach(rect => {
    rect.addEventListener('mousemove', (e) => {
      const i = parseInt(rect.getAttribute('data-i'));
      const d = data[i];
      const svgRect = container.querySelector('svg').getBoundingClientRect();
      const bbox = rect.getBBox();
      newTooltip.style.left = ((bbox.x + bbox.width / 2) / width) * svgRect.width + 'px';
      newTooltip.style.top = (bbox.y / height) * svgRect.height + 'px';
      newTooltip.textContent = `${d.date} — ${fmtBR(d.kwh, 2)} kWh`;
      newTooltip.classList.add('show');
    });
    rect.addEventListener('mouseleave', () => newTooltip.classList.remove('show'));
  });
}

drawAreaChart('curveWrap', 'curveTooltip', curveData);
drawBarChart('dailyWrap', 'dailyTooltip', dailyData);

function matchRefreshBtnWidth() {
  const pill = document.getElementById('statusPill');
  const btn = document.getElementById('refreshBtn');
  if (pill && btn) btn.style.width = pill.offsetWidth + 'px';
}
matchRefreshBtnWidth();
window.addEventListener('resize', matchRefreshBtnWidth);

const refreshBtn = document.getElementById('refreshBtn');
refreshBtn.addEventListener('click', async () => {
  refreshBtn.disabled = true;
  refreshBtn.textContent = 'Atualizando…';
  try {
    await fetch(`https://api.github.com/repos/${GH_REPO}/actions/workflows/${GH_WORKFLOW}/dispatches`, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${GH_TOKEN}`,
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
      },
      body: JSON.stringify({ ref: 'main' }),
    });
  } catch (e) {
    // segue para o reload mesmo assim; se falhar, os dados so ficam desatualizados
  }
  setTimeout(() => {
    location.href = location.pathname + '?t=' + Date.now();
  }, 45000);
});
</script>
"""


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def redact_location(raw):
    """Mantem so cidade/estado, removendo rua/numero/CEP do endereco da usina."""
    m = re.search(r"([\wÀ-ÿ.\s]+?)\s*-\s*([A-Z]{2}),\s*\d{5}-?\d{3}", raw or "")
    if m:
        return f"{m.group(1).strip()} - {m.group(2)}, Brasil"
    parts = [p.strip() for p in (raw or "").split(",")]
    return ", ".join(parts[-2:]) if len(parts) >= 2 else (raw or "-")


def main():
    creds = creds_from_env()
    token_header = sems_login(creds["SEMS_ACCOUNT"], creds["SEMS_PASSWORD"])
    station = get_station(token_header)
    pid = station["powerstation_id"]
    detail = get_detail(pid, token_header)
    station["kpi"] = detail.get("kpi") or {}
    now = datetime.now(BRAZIL_TZ)
    today = now.date().isoformat()
    curve = get_today_curve(pid, token_header, today)
    daily = read_daily_history()
    events = read_events()

    pac = station.get("pac", 0.0)
    in_window = 7 <= now.hour < 17
    inverters = get_inverters(detail, in_window)

    if pac > 0.0:
        status_pill = '<span id="statusPill" class="pill pill-good">Gerando normalmente</span>'
    elif in_window:
        status_pill = '<span id="statusPill" class="pill pill-critical">Sem geração no horário esperado</span>'
    else:
        status_pill = '<span id="statusPill" class="pill pill-neutral">Fora do horário de geração</span>'

    weather = (station.get("weather") or {}).get("HeWeather6", [{}])[0].get("now", {})

    if events:
        rows = []
        for ev in events:
            cls = "alert" if ev["type"] == "ALERT" else "recovered"
            title = "Sistema parou de gerar" if ev["type"] == "ALERT" else "Sistema voltou a gerar"
            rows.append(
                f'<div class="event-row"><div class="event-stripe {cls}"></div>'
                f'<div class="event-body"><div class="event-title">{esc(title)}</div>'
                f'<div class="event-time mono">{esc(ev["time"])} &middot; {esc(ev["detail"])}</div></div></div>'
            )
        events_html = "".join(rows)
    else:
        events_html = '<div class="empty-state">Nenhum evento registrado ainda — bom sinal.</div>'

    if inverters:
        cards = []
        for inv in inverters:
            metric = f"{fmt_br(inv['power'], 2)} kW agora · {fmt_br(inv['eday'], 1)} kWh hoje"
            cards.append(
                f'<div class="inverter-card"><div class="inverter-dot {inv["state"]}"></div>'
                f'<div class="inverter-info"><div class="inverter-name">{esc(inv["name"])}</div>'
                f'<div class="inverter-metric mono">{esc(metric)}</div></div></div>'
            )
        inverters_html = "".join(cards)
    else:
        inverters_html = '<div class="empty-state">Sem dados de microinversores no momento.</div>'

    html = HTML_TEMPLATE
    replacements = {
        "__STATION_NAME__": esc(creds.get("PUBLIC_DISPLAY_NAME") or station.get("stationname", "Usina")),
        "__LOCATION__": esc(redact_location(station.get("location", ""))),
        "__CAPACITY__": fmt_br(station.get("capacity", 0.0), 1),
        "__STATUS_PILL__": status_pill,
        "__UPDATED_AT__": now.strftime("%d/%m/%Y %H:%M"),
        "__PAC__": fmt_br_compact(pac, 2),
        "__EDAY__": fmt_br_compact(station.get('eday', 0.0), 1),
        "__EMONTH__": fmt_br_compact(station.get('emonth', 0.0), 1),
        "__ETOTAL__": fmt_br_compact(station.get('etotal', 0.0), 1),
        "__INCOME_DAY__": fmt_br_compact(station.get('eday_income', 0.0), 2),
        "__INCOME_TOTAL__": fmt_br_compact((station.get('kpi') or {}).get('total_income', 0.0), 2) if station.get("kpi") else "-",
        "__WEATHER_COND__": esc(weather.get("cond_txt", "-")),
        "__WEATHER_TEMP__": esc(weather.get("tmp", "-")),
        "__WEATHER_HUM__": esc(weather.get("hum", "-")),
        "__WEATHER_CLOUD__": esc(weather.get("cloud", "-")),
        "__WEATHER_WIND__": esc(f"{weather.get('wind_dir', '')} {weather.get('wind_spd', '-')} km/h".strip()),
        "__EVENTS_HTML__": events_html,
        "__INVERTERS_HTML__": inverters_html,
        "__INTER_FONT_BASE64__": base64.b64encode(open(FONT_PATH, "rb").read()).decode(),
        "__CURVE_JSON__": json.dumps(curve, ensure_ascii=False),
        "__DAILY_JSON__": json.dumps(daily, ensure_ascii=False),
        "__GH_REPO_SLUG__": GITHUB_REPO_SLUG,
        "__GH_WORKFLOW_FILE__": GITHUB_WORKFLOW_FILE,
        "__GH_DISPATCH_TOKEN_B64__": base64.b64encode(creds["GH_DISPATCH_TOKEN"].encode()).decode(),
    }
    for k, v in replacements.items():
        html = html.replace(k, str(v))

    with open(OUTPUT_PATH, "w") as f:
        f.write(html)
    print(f"Dashboard written to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
