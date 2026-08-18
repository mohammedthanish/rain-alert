#!/usr/bin/env python3
"""Rain alerts for two Chennai locations (home + office) by email.

Weather source is Open-Meteo (free, no key). Every value that drives an
alert is cross-checked to keep false positives low, because decisions
(take the car, leave before the rain) are made on these emails:

  * "best_match" forecast supplies 15-minutely precipitation, hourly
    precipitation + probability, and current conditions;
  * three independent models (ICON, GFS, ECMWF IFS) are queried
    separately, and rain-start alerts require at least MODEL_AGREE_MIN
    of them to also predict rain in the same window;
  * "currently raining" requires BOTH measurable precipitation in the
    last 15 minutes AND a rain/drizzle/shower WMO weather code.

Three kinds of email:

  1. Daily outlook (~7:00 IST): will it rain at the OFFICE today.
     The check runs every morning but the email is sent ONLY when the
     verdict is LIKELY (take the car) — no mail on possible/unlikely
     days. The morning check still stamps state.json daily, which
     keeps the repo active (GitHub disables schedules on repos with no
     activity for 60 days) and gives the healthcheck workflow a
     heartbeat. Whether the watcher is alive is monitored separately
     by .github/workflows/healthcheck.yml, not by this email.
  2. Rain starting soon: rain expected to begin within the next
     IMMINENT_WINDOW_MIN minutes (default 120) at home or office.
     One alert per location per SUPPRESS_HOURS.
  3. Rain ongoing at BOTH locations: an email with when the rain is
     expected to end at each (minutes + clock time), from the
     15-minutely forecast.

Alert emails (2 and 3) are muted during QUIET_START-QUIET_END
(default 23:00-05:30 IST); the daily email is not affected.

Privacy note: this file is written to live in a PUBLIC repo (GitHub
Actions minutes are unlimited there, unlike private repos). It
therefore contains no coordinates or place names - those come from
config.env locally and from repo secrets in the cloud (HOME_LAT,
HOME_LON, OFFICE_LAT, OFFICE_LON, optional HOME_NAME/OFFICE_NAME).
Nothing location-identifying is ever printed to the run log.

Stdlib only - no pip installs. Config lives in config.env next to this
script; environment variables override it (GitHub Actions uses repo
secrets).

Usage:
  rain_alert.py                normal check (state-aware, sends emails)
  rain_alert.py --status       print the current assessment, touch nothing
  rain_alert.py --test-email   send a fake alert to verify SMTP, then exit
  rain_alert.py --force-daily  send the daily outlook now, ignore state
                               and the LIKELY-only rule (testing)
"""

import json
import os
import smtplib
import ssl
import sys
import urllib.request
from datetime import datetime, time as dtime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
API = "https://api.open-meteo.com/v1/forecast"

# Independent models for the consensus check. "Seamless" variants let
# Open-Meteo pick the best resolution of each family for the location.
CONSENSUS_MODELS = ["icon_seamless", "gfs_seamless", "ecmwf_ifs025"]

# WMO weather codes that mean liquid precipitation is falling:
# 51-67 drizzle/rain (incl. freezing), 80-82 rain showers,
# 95/96/99 thunderstorms.
RAIN_CODES = set(range(51, 68)) | {80, 81, 82, 95, 96, 99}

STATE_FORMAT = 1
BASE = Path(__file__).resolve().parent
STATE_FILE = BASE / "state.json"
CONFIG_FILE = BASE / "config.env"
LOG_FILE = BASE / "run.log"
LOG_MAX_BYTES = 500_000

# Tunables (all overridable via config.env / env vars).
DEFAULTS = {
    "IMMINENT_WINDOW_MIN": "120",  # look-ahead for "rain starting soon"
    "IMMINENT_SLOT_MM": "0.2",     # min mm per 15-min slot to count as rain starting
    "IMMINENT_PROB_MIN": "60",     # min hourly precipitation probability (%)
    "MODEL_AGREE_MIN": "2",        # of the 3 consensus models
    "WET_SLOT_MM": "0.1",          # a 15-min slot at/above this is "wet" (end-of-rain search)
    "SUPPRESS_HOURS": "3",         # min gap between repeat alerts of the same kind
    "DAILY_AFTER": "06:30",        # earliest IST time for the daily outlook email
    "QUIET_START": "23:00",        # alert emails muted from here...
    "QUIET_END": "05:30",          # ...to here (daily outlook unaffected)
}


def log(msg: str) -> None:
    line = f"{datetime.now(IST).isoformat(timespec='seconds')} {msg}"
    print(line)
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            LOG_FILE.rename(LOG_FILE.with_suffix(".log.old"))
        with LOG_FILE.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        for raw in CONFIG_FILE.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            cfg[key.strip()] = value.strip().strip('"').strip("'")
    # Environment variables override the file (GitHub Actions passes
    # repo secrets this way; config.env never leaves this machine).
    for key in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS",
                "EMAIL_FROM", "EMAIL_TO",
                "HOME_LAT", "HOME_LON", "HOME_NAME",
                "OFFICE_LAT", "OFFICE_LON", "OFFICE_NAME",
                *DEFAULTS):
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    return cfg


def num(cfg: dict, key: str) -> float:
    """A typo in one tunable must not take the whole watcher down."""
    try:
        return float(cfg.get(key, DEFAULTS[key]))
    except (TypeError, ValueError):
        log(f"WARNING: {key}={cfg.get(key)!r} is not a number, "
            f"using default {DEFAULTS[key]}")
        return float(DEFAULTS[key])


def parse_hhmm(cfg: dict, key: str) -> dtime:
    raw = cfg.get(key, DEFAULTS[key])
    try:
        hh, _, mm = raw.partition(":")
        return dtime(int(hh), int(mm or 0))
    except (TypeError, ValueError):
        log(f"WARNING: {key}={raw!r} is not HH:MM, using {DEFAULTS[key]}")
        hh, _, mm = DEFAULTS[key].partition(":")
        return dtime(int(hh), int(mm))


def build_locations(cfg: dict) -> list:
    locations = []
    for key, label in (("home", "Home"), ("office", "Office")):
        lat = cfg.get(f"{key.upper()}_LAT")
        lon = cfg.get(f"{key.upper()}_LON")
        if not lat or not lon:
            raise RuntimeError(
                f"{key.upper()}_LAT / {key.upper()}_LON not configured "
                "(config.env locally, repo secrets in the cloud)")
        locations.append({
            "key": key,
            "name": cfg.get(f"{key.upper()}_NAME", label),
            "lat": float(lat),
            "lon": float(lon),
        })
    return locations


def get_json(url: str):
    last_exc = None
    for _ in range(2):  # one retry: Open-Meteo hiccups occasionally
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "rain-alert/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last_exc = exc
    raise last_exc


def ts(value: str) -> datetime:
    """API returns naive local (Asia/Kolkata) times; pin them to IST."""
    return datetime.fromisoformat(value).replace(tzinfo=IST)


def fetch_weather(locations: list) -> list:
    """Return one dict per location with parsed forecast series."""
    lats = ",".join(str(l["lat"]) for l in locations)
    lons = ",".join(str(l["lon"]) for l in locations)
    base = (f"{API}?latitude={lats}&longitude={lons}"
            f"&timezone=Asia%2FKolkata&forecast_days=2")
    main = get_json(
        base
        + "&current=precipitation,rain,showers,weather_code"
        + "&minutely_15=precipitation&forecast_minutely_15=48"
        + "&hourly=precipitation,precipitation_probability"
        + "&daily=precipitation_sum,precipitation_probability_max,precipitation_hours"
    )
    consensus = get_json(
        base + "&hourly=precipitation&models=" + ",".join(CONSENSUS_MODELS)
    )
    if isinstance(main, dict):
        main = [main]
    if isinstance(consensus, dict):
        consensus = [consensus]
    if len(main) != len(locations) or len(consensus) != len(locations):
        raise RuntimeError("API returned an unexpected number of locations")

    out = []
    for loc, m, c in zip(locations, main, consensus):
        hourly = m["hourly"]
        minutely = m["minutely_15"]
        daily = m["daily"]
        models = {}
        times = [ts(t) for t in c["hourly"]["time"]]
        for model in CONSENSUS_MODELS:
            series = c["hourly"].get(f"precipitation_{model}")
            if series is None and len(CONSENSUS_MODELS) == 1:
                series = c["hourly"].get("precipitation")
            if series is not None:
                models[model] = [
                    (t, mm) for t, mm in zip(times, series) if mm is not None
                ]
        out.append({
            **loc,
            "current": m.get("current") or {},
            "minutely": [
                (ts(t), mm)
                for t, mm in zip(minutely["time"], minutely["precipitation"])
                if mm is not None
            ],
            "hourly": [
                (ts(t), pr if pr is not None else 0.0, p)
                for t, pr, p in zip(hourly["time"], hourly["precipitation"],
                                    hourly["precipitation_probability"])
            ],
            "daily": {
                "sum": (daily["precipitation_sum"] or [0])[0] or 0.0,
                "prob_max": (daily["precipitation_probability_max"] or [0])[0] or 0,
                "hours": (daily["precipitation_hours"] or [0])[0] or 0.0,
            },
            "models": models,
        })
    return out


# --- Assessments -------------------------------------------------------

def is_raining(loc: dict) -> bool:
    """Strict on purpose: measurable precipitation AND a rain code.

    Either signal alone produces false "it's raining" calls (a trace
    0.0 with a drizzle code, or residual precip with a cleared sky
    code); requiring both keeps precision high.
    """
    cur = loc["current"]
    mm = (cur.get("precipitation") or 0) + (cur.get("showers") or 0)
    return mm >= 0.1 and cur.get("weather_code") in RAIN_CODES


def model_agreement(loc: dict, t0: datetime, t1: datetime, need_mm: float) -> tuple:
    """(#models predicting >= need_mm total in [t0, t1), #models with data)."""
    agree = have = 0
    for series in loc["models"].values():
        rows = [mm for t, mm in series
                if t + timedelta(hours=1) > t0 and t < t1]
        if not rows:
            continue
        have += 1
        if sum(rows) >= need_mm:
            agree += 1
    return agree, have


def imminent_rain(loc: dict, now: datetime, cfg: dict):
    """Rain starting within the window? None, or details for the email.

    Three independent conditions, all required:
      1. best_match 15-minutely shows a slot >= IMMINENT_SLOT_MM,
      2. hourly precipitation probability over the window >= IMMINENT_PROB_MIN,
      3. >= MODEL_AGREE_MIN consensus models predict rain in the window.
    """
    window_end = now + timedelta(minutes=num(cfg, "IMMINENT_WINDOW_MIN"))
    slot_mm = num(cfg, "IMMINENT_SLOT_MM")
    wet = [(t, mm) for t, mm in loc["minutely"]
           if now < t <= window_end and mm >= slot_mm]
    if not wet:
        return None
    prob = max((p for t, _, p in loc["hourly"]
                if p is not None and t + timedelta(hours=1) > now and t < window_end),
               default=0)
    if prob < num(cfg, "IMMINENT_PROB_MIN"):
        return None
    agree, have = model_agreement(loc, now, window_end, slot_mm)
    # If the consensus feed is down, alert anyway (recall over silence)
    # but say so in the email rather than suppressing a real warning.
    if have and agree < num(cfg, "MODEL_AGREE_MIN"):
        return None
    start = wet[0][0]
    return {
        "start": start,
        "minutes": max(0, int((start - now).total_seconds() // 60)),
        "peak_mm": max(mm for _, mm in wet),
        "prob": prob,
        "agree": agree,
        "have": have,
        "end": rain_end_after(loc, start, cfg),
    }


def rain_end_after(loc: dict, start: datetime, cfg: dict):
    """First sustained dry spell at/after `start`: (dt, minutes_after_start).

    "Sustained" = the next hour of 15-min slots (as far as available,
    at least two slots) all below WET_SLOT_MM, so a single dry slot in
    the middle of a shower doesn't call the end early.
    """
    wet_mm = num(cfg, "WET_SLOT_MM")
    slots = [(t, mm) for t, mm in loc["minutely"]
             if t + timedelta(minutes=15) > start]
    for i, (t, _) in enumerate(slots):
        ahead = slots[i:i + 4]
        if len(ahead) >= 2 and all(mm < wet_mm for _, mm in ahead):
            return t, max(0, int((t - start).total_seconds() // 60))
    return None


def day_verdict(loc: dict, now: datetime, cfg: dict) -> dict:
    """Will it rain (rest of) today at this location?"""
    day_end = now.replace(hour=23, minute=59, second=59)
    rest = [(t, pr, p) for t, pr, p in loc["hourly"]
            if t.date() == now.date() and t + timedelta(hours=1) > now]
    total = sum(pr for _, pr, _ in rest)
    prob = max((p for _, _, p in rest if p is not None), default=0)
    agree, have = model_agreement(loc, now, day_end, 0.5)
    wet_hours = [(t, pr, p) for t, pr, p in rest
                 if pr >= 0.1 and (p or 0) >= 40]
    if prob >= 60 and (total >= 1.0 or agree >= num(cfg, "MODEL_AGREE_MIN")):
        tier = "likely"
    elif prob >= 40 and (total >= 0.2 or agree >= 1):
        tier = "possible"
    else:
        tier = "unlikely"
    return {"tier": tier, "prob": prob, "total": total,
            "agree": agree, "have": have, "wet_hours": wet_hours}


def in_quiet_hours(now: datetime, cfg: dict) -> bool:
    start, end = parse_hhmm(cfg, "QUIET_START"), parse_hhmm(cfg, "QUIET_END")
    t = now.time()
    if start <= end:
        return start <= t < end
    return t >= start or t < end


# --- Emails ------------------------------------------------------------

def send_email(cfg: dict, subject: str, lines: list) -> bool:
    required = ["SMTP_USER", "SMTP_PASS", "EMAIL_TO"]
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        log(f"ERROR: email not sent, missing config: {', '.join(missing)}")
        return False

    host = cfg.get("SMTP_HOST", "smtp.gmail.com")
    port = int(cfg.get("SMTP_PORT", "465"))
    sender = cfg.get("EMAIL_FROM", cfg["SMTP_USER"])

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = cfg["EMAIL_TO"]
    body = list(lines) + ["", "(Automated alert from the rain-alert watcher; "
                          "forecast data by open-meteo.com)"]
    msg.set_content("\n".join(body))

    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=60) as server:
            server.login(cfg["SMTP_USER"], cfg["SMTP_PASS"])
            server.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=60) as server:
            server.starttls(context=context)
            server.login(cfg["SMTP_USER"], cfg["SMTP_PASS"])
            server.send_message(msg)
    log(f"Email sent: {subject}")
    return True


def fmt_hour(t: datetime) -> str:
    return t.strftime("%H:%M")


def consensus_note(agree: int, have: int) -> str:
    if not have:
        return "consensus models unavailable this run"
    return f"{agree}/{have} independent models agree"


def daily_email(data: list, now: datetime, cfg: dict) -> tuple:
    office = next(l for l in data if l["key"] == "office")
    home = next(l for l in data if l["key"] == "home")
    ov = day_verdict(office, now, cfg)
    hv = day_verdict(home, now, cfg)
    advice = {
        "likely": "take the car",
        "possible": "consider the car",
        "unlikely": "no rain expected",
    }[ov["tier"]]
    subject = f"Rain today at office: {ov['tier'].upper()} - {advice}"

    def block(loc, v):
        lines = [f"{loc['name']}: {v['tier'].upper()} "
                 f"(max chance {v['prob']}%, expected {v['total']:.1f} mm, "
                 f"{consensus_note(v['agree'], v['have'])})"]
        if v["wet_hours"]:
            spans = ", ".join(f"{fmt_hour(t)} ({pr:.1f} mm/h, {p or 0}%)"
                              for t, pr, p in v["wet_hours"][:8])
            lines.append(f"  Wet spells: {spans}")
        return lines

    lines = [f"Outlook for {now.strftime('%A %d %b %Y')}:", ""]
    lines += block(office, ov)
    lines += [""]
    lines += block(home, hv)
    return subject, lines, ov["tier"]


def imminent_email(hits: list, now: datetime, cfg: dict) -> tuple:
    window = int(num(cfg, "IMMINENT_WINDOW_MIN"))
    if len(hits) == 1:
        loc, info = hits[0]
        subject = (f"Rain starting ~{fmt_hour(info['start'])} at {loc['name']} "
                   f"(in ~{info['minutes']} min)")
    else:
        subject = f"Rain starting within {window} min at home & office"
    lines = []
    for loc, info in hits:
        lines.append(f"{loc['name']}: rain expected from ~{fmt_hour(info['start'])} "
                     f"(in ~{info['minutes']} min)")
        lines.append(f"  Intensity up to {info['peak_mm']:.1f} mm/15min, "
                     f"chance {info['prob']}%, {consensus_note(info['agree'], info['have'])}")
        if info["end"]:
            end_t, dur = info["end"]
            lines.append(f"  Expected to ease by ~{fmt_hour(end_t)} (~{dur} min of rain)")
        lines.append("")
    return subject, [l for l in lines if l is not None]


def both_raining_email(data: list, now: datetime, cfg: dict) -> tuple:
    parts, lines = [], ["Rain is falling at both locations right now.", ""]
    for loc in data:
        end = rain_end_after(loc, now, cfg)
        if end:
            end_t, mins = end
            parts.append(f"{loc['key']} ~{mins} min")
            lines.append(f"{loc['name']}: expected to end around "
                         f"{fmt_hour(end_t)} (~{mins} min from now)")
        else:
            horizon = max((t for t, _ in loc["minutely"]), default=now)
            hrs = max(1, int((horizon - now).total_seconds() // 3600))
            parts.append(f"{loc['key']} {hrs}h+")
            lines.append(f"{loc['name']}: no sustained dry spell in the next "
                         f"~{hrs}h of forecast")
    subject = "Raining at home & office - ends: " + ", ".join(parts)
    return subject, lines


# --- State -------------------------------------------------------------

def load_state() -> dict:
    empty = {"daily_sent": None, "alerts": {}}
    if not STATE_FILE.exists():
        return empty
    try:
        state = json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        log("WARNING: state.json unreadable, starting fresh")
        return empty
    if state.get("format") != STATE_FORMAT:
        log("State format changed, starting fresh")
        return empty
    state.setdefault("daily_sent", None)
    state.setdefault("alerts", {})
    return state


def save_state(state: dict, previous: dict) -> None:
    """Write state.json only when something moved (cloud mode commits it)."""
    if (state.get("daily_sent") == previous.get("daily_sent")
            and state.get("alerts") == previous.get("alerts")):
        return
    STATE_FILE.write_text(json.dumps({
        "format": STATE_FORMAT,
        "updated": datetime.now(IST).isoformat(timespec="seconds"),
        "daily_sent": state.get("daily_sent"),
        "alerts": state.get("alerts", {}),
    }, indent=2) + "\n")


def parse_ts(value):
    try:
        stamp = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=IST)


def suppressed(alerts: dict, key: str, now: datetime, cfg: dict) -> bool:
    last = parse_ts(alerts.get(key))
    return (last is not None
            and now - last < timedelta(hours=num(cfg, "SUPPRESS_HOURS")))


# --- Modes -------------------------------------------------------------

def print_status(data: list, now: datetime, cfg: dict) -> None:
    print(f"As of {now.isoformat(timespec='minutes')}")
    for loc in data:
        raining = is_raining(loc)
        print(f"\n{loc['name']}:")
        cur = loc["current"]
        print(f"  now: {'RAINING' if raining else 'dry'} "
              f"(precip {cur.get('precipitation', '?')} mm/15min, "
              f"code {cur.get('weather_code', '?')})")
        if raining:
            end = rain_end_after(loc, now, cfg)
            if end:
                print(f"  ends: ~{fmt_hour(end[0])} (~{end[1]} min)")
            else:
                print("  ends: beyond forecast window")
        info = imminent_rain(loc, now, cfg)
        if info:
            print(f"  imminent: from ~{fmt_hour(info['start'])} "
                  f"(in ~{info['minutes']} min, up to {info['peak_mm']:.1f} mm/15min, "
                  f"{info['prob']}%, {consensus_note(info['agree'], info['have'])})")
        else:
            print(f"  imminent: nothing in the next "
                  f"{int(num(cfg, 'IMMINENT_WINDOW_MIN'))} min")
        v = day_verdict(loc, now, cfg)
        print(f"  today: {v['tier']} (max {v['prob']}%, {v['total']:.1f} mm, "
              f"{consensus_note(v['agree'], v['have'])})")


def test_email(cfg: dict) -> int:
    try:
        ok = send_email(cfg, "Rain alert test (not real)",
                        ["This is a test of the rain-alert watcher's email path.",
                         "If you can read this, SMTP settings are working."])
    except Exception as exc:
        log(f"ERROR: test email failed: {exc}")
        return 1
    return 0 if ok else 1


def main(argv: list) -> int:
    cfg = load_config()
    if "--test-email" in argv:
        return test_email(cfg)

    try:
        locations = build_locations(cfg)
        data = fetch_weather(locations)
    except Exception as exc:
        log(f"ERROR: fetch failed: {exc}")
        return 1

    now = datetime.now(IST).replace(microsecond=0)

    if "--status" in argv:
        print_status(data, now, cfg)
        return 0

    if "--force-daily" in argv:
        subject, lines, _ = daily_email(data, now, cfg)
        try:
            return 0 if send_email(cfg, subject, lines) else 1
        except Exception as exc:
            log(f"ERROR: email failed: {exc}")
            return 1

    state = load_state()
    previous = {"daily_sent": state.get("daily_sent"),
                "alerts": dict(state.get("alerts", {}))}
    alerts = state.setdefault("alerts", {})
    today = now.date().isoformat()
    quiet = in_quiet_hours(now, cfg)

    # Summarize the run (never includes coordinates: public logs).
    raining = {loc["key"]: is_raining(loc) for loc in data}
    log("status: " + ", ".join(
        f"{k}={'raining' if v else 'dry'}" for k, v in raining.items())
        + (" (quiet hours)" if quiet else ""))

    failures = 0

    # 1. Daily outlook, evaluated once per day after DAILY_AFTER, but
    #    emailed ONLY when the office verdict is LIKELY. Dry-day checks
    #    still stamp daily_sent so state.json moves (and is committed)
    #    every day - that is the watcher's heartbeat.
    if state.get("daily_sent") != today and now.time() >= parse_hhmm(cfg, "DAILY_AFTER"):
        subject, lines, tier = daily_email(data, now, cfg)
        if tier != "likely":
            log(f"daily outlook: {tier}, no email (LIKELY-only)")
            state["daily_sent"] = today
        else:
            try:
                sent = send_email(cfg, subject, lines)
            except Exception as exc:
                log(f"ERROR: daily email failed: {exc}")
                sent = False
            if sent:
                state["daily_sent"] = today
            else:
                failures += 1

    # 2. Rain starting soon (per location, suppressed per SUPPRESS_HOURS,
    #    muted in quiet hours, skipped where it is already raining).
    if not quiet:
        hits = []
        for loc in data:
            if raining[loc["key"]]:
                continue
            if suppressed(alerts, f"imminent_{loc['key']}", now, cfg):
                continue
            info = imminent_rain(loc, now, cfg)
            if info:
                hits.append((loc, info))
        if hits:
            subject, lines = imminent_email(hits, now, cfg)
            log("ALERTING imminent: " + ", ".join(loc["key"] for loc, _ in hits))
            try:
                sent = send_email(cfg, subject, lines)
            except Exception as exc:
                log(f"ERROR: imminent email failed: {exc}")
                sent = False
            if sent:
                for loc, _ in hits:
                    alerts[f"imminent_{loc['key']}"] = now.isoformat(timespec="seconds")
            else:
                failures += 1

    # 3. Raining at both locations: when does it end?
    if not quiet and all(raining.values()) and not suppressed(alerts, "both_end", now, cfg):
        subject, lines = both_raining_email(data, now, cfg)
        log("ALERTING both raining")
        try:
            sent = send_email(cfg, subject, lines)
        except Exception as exc:
            log(f"ERROR: both-raining email failed: {exc}")
            sent = False
        if sent:
            alerts["both_end"] = now.isoformat(timespec="seconds")
        else:
            failures += 1

    save_state(state, previous)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
