"""
Pull NAM + GFS MOS from the TAMU Weather Data Interface and compute
WxChallenge-style guidance (06Z-06Z high, low, max wind, precip).

Usage:
    STATION=KATL python wx_guidance.py            # print only
    STATION=KATL EMAIL_TO=you@x.com SMTP_USER=... SMTP_PASS=... python wx_guidance.py

Requires: pip install requests beautifulsoup4
"""
import os
import re
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import requests
from bs4 import BeautifulSoup

WDI_URL = "https://wdi.geos.tamu.edu/wdi.php"

# Rough QPF midpoints (inches) for MOS Q06 categories. MOS gives categories,
# not amounts, so treat the precip number as a starting point only.
Q06_MID = {0: 0.0, 1: 0.05, 2: 0.17, 3: 0.37, 4: 0.75, 5: 1.5, 6: 2.5}


def fetch_mos_text(station: str) -> str:
    params = {"station": station, "productCategory": "model",
              "dataType": 17, "timePeriod": 0}
    r = requests.get(WDI_URL, params=params, timeout=30,
                     headers={"User-Agent": "personal-wx-guidance-script"})
    r.raise_for_status()
    pre = BeautifulSoup(r.text, "html.parser").find("pre")
    if pre is None:
        raise RuntimeError("No <pre> block found; page layout may have changed.")
    return pre.get_text()


def extract_block(text: str, model: str) -> list[str]:
    """Return lines of e.g. the 'NAM MOS GUIDANCE' block (short-range only)."""
    m = re.search(rf"^[ \t]*\w{{4}}[ \t]+{model} MOS GUIDANCE.*?(?=\n\s*\n|\Z)",
                  text, re.S | re.M)
    if not m:
        raise RuntimeError(f"{model} MOS block not found")
    return m.group(0).splitlines()


def parse_block(lines: list[str]):
    """Parse fixed-width MOS rows into {row_name: {valid_time: value}}."""
    header = lines[0]
    d = re.search(r"(\d+)/(\d+)/(\d{4})", header)
    date = datetime(int(d[3]), int(d[1]), int(d[2]), tzinfo=timezone.utc)

    rows = {ln[:4].strip(): ln for ln in lines[1:] if len(ln) > 4}
    hr_line = rows["HR"]
    ncols = (len(hr_line) - 4) // 3

    def col(line, i):
        return line[4 + 3 * i: 7 + 3 * i].strip()

    # Build valid times; roll the day forward whenever the hour wraps.
    times, prev = [], -1
    for i in range(ncols):
        h = int(col(hr_line, i))
        if h <= prev:
            date += timedelta(days=1)
        prev = h
        times.append(date.replace(hour=h))

    data = {}
    for name in ("X/N", "TMP", "WSP", "Q06"):
        line = rows.get(name, "")
        vals = {}
        for i, t in enumerate(times):
            v = col(line, i)
            if v.lstrip("-").isdigit():
                vals[t] = int(v)
        data[name] = vals
    return data


def window_values(data, start: datetime):
    end = start + timedelta(hours=24)
    in_win = lambda t: start < t <= end

    temps = [v for t, v in data["TMP"].items() if start <= t <= end]
    # MOS aligns daytime max at 00Z (end of day) and nighttime min at 12Z.
    xn_max = data["X/N"].get(start.replace(hour=0) + timedelta(days=1))
    xn_min = data["X/N"].get(start.replace(hour=12))

    high = max(temps + ([xn_max] if xn_max is not None else []))
    low = min(temps + ([xn_min] if xn_min is not None else []))
    wind = max(v for t, v in data["WSP"].items() if in_win(t))
    precip = sum(Q06_MID.get(v, 0) for t, v in data["Q06"].items() if in_win(t))
    return {"high": high, "low": low, "wind": wind, "precip": round(precip, 2)}


def forecast_window_start(now: datetime) -> datetime:
    """Forecasts are due by the next 00Z and verify 06Z-06Z after it."""
    next_00z = (now + timedelta(days=1)).replace(hour=0, minute=0,
                                                 second=0, microsecond=0)
    return next_00z + timedelta(hours=6)


def build_report(station: str, text: str, now: datetime) -> str:
    start = forecast_window_start(now)
    results = {}
    for model in ("NAM", "GFS"):
        try:
            results[model] = window_values(parse_block(extract_block(text, model)), start)
        except Exception as e:  # keep going if one model is missing
            results[model] = f"error: {e}"

    lines = [f"{station} guidance for {start:%a %b %d} 06Z - "
             f"{start + timedelta(days=1):%a %b %d} 06Z", ""]
    good = [r for r in results.values() if isinstance(r, dict)]
    for model, r in results.items():
        if isinstance(r, dict):
            lines.append(f"{model}: High {r['high']}  Low {r['low']}  "
                         f"Wind {r['wind']} kt  Precip {r['precip']:.2f}\"")
        else:
            lines.append(f"{model}: {r}")
    if good:
        avg = lambda k: sum(r[k] for r in good) / len(good)
        lines += ["", f"Consensus: High {round(avg('high'))}  Low {round(avg('low'))}  "
                      f"Wind {round(avg('wind'))} kt  Precip {avg('precip'):.2f}\""]
    lines += ["", "Note: MOS wind is 3-hourly and precip is category-based; adjust as needed.",
              "Submit at https://www.wxchallenge.com/submit_forecast.php before 00Z."]
    return "\n".join(lines)


def send_email(subject: str, body: str):
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, os.environ["SMTP_USER"], os.environ["EMAIL_TO"]
    msg.set_content(body)
    with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST", "smtp.gmail.com"), 465) as s:
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)


if __name__ == "__main__":
    station = os.environ.get("STATION", "KATL").upper()
    report = build_report(station, fetch_mos_text(station), datetime.now(timezone.utc))
    print(report)
    if os.environ.get("EMAIL_TO"):
        send_email(f"WxChallenge guidance: {station}", report)
