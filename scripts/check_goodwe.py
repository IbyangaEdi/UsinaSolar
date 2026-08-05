#!/usr/bin/env python3
"""
Monitora a usina GoodWe via SEMS Portal e alerta por e-mail quando um
microinversor fica parado (ou volta a funcionar) durante o horario em
que deveria estar gerando. Ao final do dia (disparo de cron dedicado),
envia tambem um relatorio de fechamento com o resumo do dia.

Versao para rodar em CI (GitHub Actions): le credenciais de variaveis
de ambiente e usa caminhos relativos ao repositorio (data/).

Uso: python3 scripts/check_goodwe.py
Saida (stdout, ultima linha): OK | ALERT | RECOVERED | REPORT | ERROR
"""
import csv
import json
import os
import re
import smtplib
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

BRAZIL_TZ = timezone(timedelta(hours=-3))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(REPO_ROOT, "data", "state.json")
LOG_PATH = os.path.join(REPO_ROOT, "data", "monitor.log")
HISTORY_PATH = os.path.join(REPO_ROOT, "data", "history.csv")

DAYLIGHT_START_HOUR = 7
DAYLIGHT_END_HOUR = 17
ZERO_THRESHOLD = 2


def _urlopen_retry(req, timeout=25, attempts=2):
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode()
        except Exception:
            if attempt == attempts - 1:
                raise
            time.sleep(2)


def log(msg):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(f"{datetime.now(BRAZIL_TZ).isoformat(timespec='seconds')} {msg}\n")


def creds_from_env():
    required = ["SEMS_ACCOUNT", "SEMS_PASSWORD", "ZOHO_SMTP_HOST", "ZOHO_SMTP_PORT",
                "ZOHO_USER", "ZOHO_PASSWORD", "ALERT_TO"]
    return {k: os.environ[k] for k in required}


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            state = json.load(f)
    else:
        state = {}
    state.setdefault("inverters", {})
    return state


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)


def sems_login(account, pwd):
    url = "https://www.semsportal.com/api/v2/Common/CrossLogin"
    body = json.dumps({
        "account": account, "pwd": pwd, "agreement_agreement": 0, "is_local": True,
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json;charset=UTF-8",
        "Token": json.dumps({"version": "v2.1.0", "client": "web", "language": "en"}),
    })
    payload = json.loads(_urlopen_retry(req))
    if payload.get("hasError"):
        raise RuntimeError(f"SEMS login failed: {payload.get('msg')}")
    return payload["data"]


def build_token_header(login_data):
    return json.dumps({
        "uid": login_data["uid"], "timestamp": login_data["timestamp"], "token": login_data["token"],
        "client": login_data["client"], "version": login_data["version"], "language": login_data["language"],
    })


def sems_get_station(token_header):
    url = "https://www.semsportal.com/api/PowerStationMonitor/QueryPowerStationMonitor"
    req = urllib.request.Request(url, data=json.dumps({}).encode(), method="POST", headers={
        "Content-Type": "application/json;charset=UTF-8", "Token": token_header,
    })
    payload = json.loads(_urlopen_retry(req))
    if payload.get("hasError"):
        raise RuntimeError(f"SEMS query failed: {payload.get('msg')}")
    stations = payload["data"]["list"]
    if not stations:
        raise RuntimeError("Nenhuma usina encontrada na conta SEMS")
    return stations[0]


def sems_get_detail(pid, token_header):
    if not pid:
        return {}
    url = "https://www.semsportal.com/api/v2/PowerStation/GetMonitorDetailByPowerstationId"
    req = urllib.request.Request(url, data=json.dumps({"powerStationId": pid}).encode(), method="POST", headers={
        "Content-Type": "application/json;charset=UTF-8", "Token": token_header,
    })
    try:
        payload = json.loads(_urlopen_retry(req))
    except Exception:
        return {}
    if payload.get("hasError"):
        return {}
    return payload.get("data") or {}


NUM_RE = re.compile(r"-?\d+(\.\d+)?")


def parse_number(text):
    if not text:
        return 0.0
    m = NUM_RE.search(str(text))
    return float(m.group()) if m else 0.0


def get_inverters(detail):
    inverters = []
    for e in detail.get("equipment") or []:
        name = (e.get("title") or "Microinversor").replace("Edval", "6").replace("Edenise", "24").strip()
        inverters.append({
            "name": name,
            "sn": e.get("sn", ""),
            "power": parse_number(e.get("powerGeneration")),
        })
    return inverters


def send_email(creds, subject, body_text):
    msg = MIMEText(body_text, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = creds["ZOHO_USER"]
    msg["To"] = creds["ALERT_TO"]
    with smtplib.SMTP(creds["ZOHO_SMTP_HOST"], int(creds["ZOHO_SMTP_PORT"]), timeout=20) as server:
        server.starttls()
        server.login(creds["ZOHO_USER"], creds["ZOHO_PASSWORD"])
        server.send_message(msg)


def in_daylight_window(now):
    return DAYLIGHT_START_HOUR <= now.hour < DAYLIGHT_END_HOUR


def read_today_history(today_str):
    if not os.path.exists(HISTORY_PATH):
        return []
    rows = []
    with open(HISTORY_PATH) as f:
        for row in csv.DictReader(f):
            if row["timestamp"].startswith(today_str):
                rows.append(row)
    return rows


def read_today_events(today_str):
    if not os.path.exists(LOG_PATH):
        return []
    pattern = re.compile(r"^(\S+) EVENT (ALERT|RECOVERED) (.*)$")
    events = []
    with open(LOG_PATH) as f:
        for line in f:
            if not line.startswith(today_str):
                continue
            m = pattern.match(line.strip())
            if m:
                events.append({"time": m.group(1), "type": m.group(2), "detail": m.group(3)})
    return events


def build_daily_report(name, now, today_str):
    rows = read_today_history(today_str)
    events = read_today_events(today_str)

    if rows:
        eday_final = max(parse_number(r["eday_kwh"]) for r in rows)
        peak_row = max(rows, key=lambda r: parse_number(r["pac_kw"]))
        peak_pac = parse_number(peak_row["pac_kw"])
        peak_time = peak_row["timestamp"][11:16]
    else:
        eday_final = 0.0
        peak_pac = 0.0
        peak_time = "-"

    lines = [
        f"Relatorio de fechamento - {name}",
        f"Data: {now.strftime('%d/%m/%Y')}",
        "",
        f"Geracao total do dia: {eday_final:.1f} kWh",
        f"Pico de potencia: {peak_pac:.2f} kW as {peak_time}",
        "",
    ]
    if events:
        lines.append(f"Eventos do dia ({len(events)}):")
        for ev in events:
            title = "parou de gerar" if ev["type"] == "ALERT" else "voltou a gerar"
            lines.append(f"- {ev['time'][11:16]} {title}: {ev['detail']}")
    else:
        lines.append("Nenhum evento de falha registrado hoje - tudo funcionou normalmente.")
    lines.append("")

    subject = f"[GoodWe] Fechamento do dia - {name} - {now.strftime('%d/%m/%Y')}"
    return subject, "\n".join(lines)


def record_history(now, station):
    new_file = not os.path.exists(HISTORY_PATH)
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    with open(HISTORY_PATH, "a") as f:
        if new_file:
            f.write("timestamp,pac_kw,eday_kwh,emonth_kwh,etotal_kwh,status\n")
        f.write(
            f"{now.isoformat(timespec='minutes')},{station.get('pac', 0.0)},"
            f"{station.get('eday', 0.0)},{station.get('emonth', 0.0)},"
            f"{station.get('etotal', 0.0)},{station.get('status')}\n"
        )


def main():
    creds = creds_from_env()
    now = datetime.now(BRAZIL_TZ)
    state = load_state()

    try:
        login_data = sems_login(creds["SEMS_ACCOUNT"], creds["SEMS_PASSWORD"])
        token_header = build_token_header(login_data)
        station = sems_get_station(token_header)
        detail = sems_get_detail(station.get("powerstation_id"), token_header)
    except Exception as e:
        log(f"ERROR checking SEMS: {e}")
        print("ERROR")
        return

    pac = station.get("pac", 0.0)
    name = station.get("stationname", "usina")
    daylight = in_daylight_window(now)
    log(f"pac={pac}kW status={station.get('status')} daylight={daylight}")
    record_history(now, station)

    emails = []

    # --- cada microinversor individualmente ---
    # a usina pode continuar gerando (pac > 0) mesmo com uma ou mais
    # unidades paradas, entao o alerta e sempre por unidade, nao pelo total.
    inv_state = state["inverters"]
    newly_down, newly_recovered = [], []
    for inv in get_inverters(detail):
        key = inv["sn"] or inv["name"]
        s = inv_state.setdefault(key, {"consecutive_zero": 0, "alert_active": False})
        if not daylight:
            s["consecutive_zero"] = 0
            continue
        if inv["power"] <= 0.0:
            s["consecutive_zero"] = s.get("consecutive_zero", 0) + 1
            if s["consecutive_zero"] >= ZERO_THRESHOLD and not s.get("alert_active"):
                s["alert_active"] = True
                newly_down.append(inv["name"])
                log(f"EVENT ALERT microinversor={inv['name']} sn={inv['sn']}")
        else:
            s["consecutive_zero"] = 0
            if s.get("alert_active"):
                s["alert_active"] = False
                newly_recovered.append(inv["name"])
                log(f"EVENT RECOVERED microinversor={inv['name']} sn={inv['sn']}")

    if newly_down:
        emails.append((
            f"[ALERTA] {len(newly_down)} microinversor(es) parado(s) em {name}",
            "Os seguintes microinversores estao com geracao zerada durante o "
            "horario de sol, mesmo com a usina no total ainda gerando:\n\n"
            + "\n".join(f"- {n}" for n in newly_down)
            + f"\n\nHorario: {now.strftime('%d/%m/%Y %H:%M')}\n\n"
            "Verifique cada unidade no SEMS Portal.",
        ))
    if newly_recovered:
        emails.append((
            f"[GoodWe] {len(newly_recovered)} microinversor(es) voltou/voltaram a gerar em {name}",
            "Os seguintes microinversores voltaram a gerar normalmente:\n\n"
            + "\n".join(f"- {n}" for n in newly_recovered)
            + f"\n\nHorario: {now.strftime('%d/%m/%Y %H:%M')}\n",
        ))

    # --- relatorio de fechamento do dia ---
    # disparado apenas pelo cron dedicado (30 22 * * * UTC = 19:30 BR),
    # identificado pelo workflow via IS_DAILY_REPORT.
    is_daily_report = os.environ.get("IS_DAILY_REPORT") == "true"
    if is_daily_report:
        today_str = now.date().isoformat()
        emails.append(build_daily_report(name, now, today_str))

    save_state(state)

    status = "OK"
    for subject, body in emails:
        try:
            send_email(creds, subject, body)
        except Exception as e:
            log(f"ERROR sending email ({subject}): {e}")
        if "ALERTA" in subject:
            status = "ALERT"
        elif "Fechamento" in subject and status == "OK":
            status = "REPORT"
    print(status)


if __name__ == "__main__":
    main()
