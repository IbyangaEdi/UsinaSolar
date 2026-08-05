#!/usr/bin/env python3
"""
Monitora a usina GoodWe via SEMS Portal e alerta por e-mail quando a
geracao fica zerada durante o horario em que deveria estar gerando.

Versao para rodar em CI (GitHub Actions): le credenciais de variaveis
de ambiente e usa caminhos relativos ao repositorio (data/).

Uso: python3 scripts/check_goodwe.py
Saida (stdout, ultima linha): OK | ALERT | RECOVERED | ERROR
"""
import json
import os
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
            return json.load(f)
    return {"consecutive_zero": 0, "alert_active": False}


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


def sems_get_station(login_data):
    token_header = json.dumps({
        "uid": login_data["uid"], "timestamp": login_data["timestamp"], "token": login_data["token"],
        "client": login_data["client"], "version": login_data["version"], "language": login_data["language"],
    })
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
        station = sems_get_station(login_data)
    except Exception as e:
        log(f"ERROR checking SEMS: {e}")
        print("ERROR")
        return

    pac = station.get("pac", 0.0)
    name = station.get("stationname", "usina")
    log(f"pac={pac}kW status={station.get('status')} daylight={in_daylight_window(now)}")
    record_history(now, station)

    if not in_daylight_window(now):
        state["consecutive_zero"] = 0
        save_state(state)
        print("OK")
        return

    if pac <= 0.0:
        state["consecutive_zero"] = state.get("consecutive_zero", 0) + 1
    else:
        was_active = state.get("alert_active", False)
        state["consecutive_zero"] = 0
        if was_active:
            state["alert_active"] = False
            save_state(state)
            log(f"EVENT RECOVERED pac={pac}kW")
            subject = f"[GoodWe] {name} voltou a gerar"
            body = (
                f"A usina '{name}' voltou a gerar energia.\n"
                f"Potencia atual: {pac} kW\n"
                f"Horario: {now.strftime('%d/%m/%Y %H:%M')}\n"
            )
            try:
                send_email(creds, subject, body)
            except Exception as e:
                log(f"ERROR sending recovery email: {e}")
            print("RECOVERED")
            return
        save_state(state)
        print("OK")
        return

    if state["consecutive_zero"] >= ZERO_THRESHOLD and not state.get("alert_active"):
        state["alert_active"] = True
        save_state(state)
        log(f"EVENT ALERT pac={pac}kW consecutive_zero={state['consecutive_zero']}")
        subject = f"[ALERTA] {name} parou de gerar energia"
        body = (
            f"A usina '{name}' esta com geracao zerada durante o horario "
            f"em que deveria estar produzindo energia.\n\n"
            f"Potencia atual: {pac} kW\n"
            f"Checagens consecutivas zeradas: {state['consecutive_zero']}\n"
            f"Horario: {now.strftime('%d/%m/%Y %H:%M')}\n\n"
            f"Verifique o inversor e o SEMS Portal."
        )
        try:
            send_email(creds, subject, body)
        except Exception as e:
            log(f"ERROR sending alert email: {e}")
        print("ALERT")
        return

    save_state(state)
    print("OK")


if __name__ == "__main__":
    main()
