#!/usr/bin/env python3
"""
Monitora a usina GoodWe via SEMS Portal e alerta por e-mail quando a
geracao fica zerada durante o horario em que deveria estar gerando.
Verifica tanto a usina como um todo quanto cada microinversor
individualmente, ja que a usina pode continuar gerando (total > 0)
mesmo com uma ou mais unidades paradas.

Versao para rodar em CI (GitHub Actions): le credenciais de variaveis
de ambiente e usa caminhos relativos ao repositorio (data/).

Uso: python3 scripts/check_goodwe.py
Saida (stdout, ultima linha): OK | ALERT | RECOVERED | ERROR
"""
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
        state = {"consecutive_zero": 0, "alert_active": False}
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

    # --- usina como um todo ---
    if not daylight:
        state["consecutive_zero"] = 0
    elif pac <= 0.0:
        state["consecutive_zero"] = state.get("consecutive_zero", 0) + 1
    else:
        state["consecutive_zero"] = 0
        if state.get("alert_active", False):
            state["alert_active"] = False
            log(f"EVENT RECOVERED pac={pac}kW")
            emails.append((
                f"[GoodWe] {name} voltou a gerar",
                f"A usina '{name}' voltou a gerar energia.\n"
                f"Potencia atual: {pac} kW\n"
                f"Horario: {now.strftime('%d/%m/%Y %H:%M')}\n",
            ))

    if daylight and state.get("consecutive_zero", 0) >= ZERO_THRESHOLD and not state.get("alert_active"):
        state["alert_active"] = True
        log(f"EVENT ALERT pac={pac}kW consecutive_zero={state['consecutive_zero']}")
        emails.append((
            f"[ALERTA] {name} parou de gerar energia",
            f"A usina '{name}' esta com geracao zerada durante o horario "
            f"em que deveria estar produzindo energia.\n\n"
            f"Potencia atual: {pac} kW\n"
            f"Checagens consecutivas zeradas: {state['consecutive_zero']}\n"
            f"Horario: {now.strftime('%d/%m/%Y %H:%M')}\n\n"
            f"Verifique o inversor e o SEMS Portal.",
        ))

    # --- cada microinversor individualmente ---
    # a usina pode continuar gerando (pac > 0) mesmo com uma ou mais
    # unidades paradas, entao isso precisa de checagem separada.
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
                log(f"EVENT INVERTER_ALERT {inv['name']} sn={inv['sn']}")
        else:
            s["consecutive_zero"] = 0
            if s.get("alert_active"):
                s["alert_active"] = False
                newly_recovered.append(inv["name"])
                log(f"EVENT INVERTER_RECOVERED {inv['name']} sn={inv['sn']}")

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

    save_state(state)

    status = "OK"
    for subject, body in emails:
        try:
            send_email(creds, subject, body)
        except Exception as e:
            log(f"ERROR sending email ({subject}): {e}")
        status = "ALERT" if "ALERTA" in subject else status
    print(status)


if __name__ == "__main__":
    main()
