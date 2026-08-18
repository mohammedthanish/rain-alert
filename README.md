# Rain alert watcher

Email alerts for rain at two Chennai locations (home: Kil Ayanambakkam,
office: Perungudi), running free on GitHub Actions every ~15 minutes.
Forecast data comes from [Open-Meteo](https://open-meteo.com) (free, no
API key), cross-checked against three independent weather models (ICON,
GFS, ECMWF IFS) to keep false positives low — these emails drive real
decisions, so precision matters more than volume.

## The three alerts

1. **Daily outlook (~7:00 IST), only when rain is LIKELY** — the check
   runs every morning, but an email goes out only when the office
   verdict is `LIKELY — take the car`. Possible/unlikely days send
   nothing, so every daily email is actionable. It includes max chance,
   expected mm, which hours look wet, and a one-line outlook for home.
   Whether the watcher is alive is monitored separately (see
   "Healthcheck" below), so quiet mornings are safe to trust.
2. **Rain starting soon** — rain expected to begin within the next 2
   hours at home or office, with the expected start time, intensity, and
   when it should ease. Sent at most once per location per 3 hours.
3. **Raining at both locations** — when rain is confirmed falling at both
   home and office, an email saying when it is expected to end at each
   (minutes from now + clock time), from the 15-minutely forecast.

Alerts 2 and 3 are muted 23:00–05:30 IST (quiet hours); the daily
outlook is not.

## How false positives are avoided

A "rain starting soon" alert requires **all three** of:

- the primary (best-match) forecast shows ≥ 0.2 mm in a 15-minute slot
  inside the window;
- the hourly precipitation probability over the window is ≥ 60%;
- at least 2 of the 3 independent models also predict rain in the window.

"Currently raining" requires both measurable precipitation in the last
15 minutes **and** a rain-type WMO weather code — either signal alone
produces false positives. All thresholds are tunable in
`config.example.env`.

## Healthcheck (broken watcher → email)

Since a quiet inbox is now normal, `healthcheck.yml` turns "the watcher
died" into an email instead of silence:

- **On failure**: the moment a rain-check run fails, an email goes out
  with the run link (only the *first* failure of a streak, so a broken
  morning doesn't send 96 emails).
- **Staleness sweep** (every 4 hours, first at 07:15 IST): if there has
  been no *successful* run in the last 2 hours — even without a failing
  run, e.g. the schedule got disabled — it emails.

The one thing it can't catch is GitHub's scheduler being down entirely
(the healthcheck runs there too). The predictable cause — GitHub
disabling schedules after 60 days without repo activity — is prevented
by the watcher committing `state.json` at least daily (the morning
check stamps it even when no email is sent).

## Layout

- `rain_alert.py` — the whole watcher, stdlib-only Python
- `health_check.py` — emails you when the watcher itself is broken
- `.github/workflows/check.yml` — cloud schedule (every 15 min) + state commit
- `.github/workflows/healthcheck.yml` — failure + staleness monitoring
- `config.env` — local credentials, coordinates (git-ignored, never pushed)
- `state.json` — what was last alerted, so nothing is emailed twice
- `setup_github.sh` — one-time repo + secrets setup

## Why the repo is public

GitHub Actions minutes are **unlimited on public repos** but capped at
2,000/month across all private repos — and the two existing private
watchers already track close to that cap. This watcher adds ~96 runs a
day. Nothing personal is in the code: coordinates, place names, and
email credentials live only in `config.env` (git-ignored) and GitHub
repo secrets, and the run log never prints locations. Prefer private
anyway? `VISIBILITY=private bash setup_github.sh`.

## Setup (one time)

```bash
cp config.example.env config.env   # then fill in values (already done here)
bash setup_github.sh               # creates repo, sets secrets, first run
gh workflow run check.yml -f test_email=true   # verify the email path
```

## Day-to-day

```bash
python3 rain_alert.py --status       # current assessment, sends nothing
python3 rain_alert.py --force-daily  # send the daily outlook right now
gh run list -R mohammedthanish/rain-alert   # check cloud runs
```

A green run proves the check executed, not that email works — the email
path only runs on a real trigger. Verify it with the `test_email=true`
dispatch above.

## Notes

- GitHub's cron is best-effort: runs land a few minutes late and slots
  are occasionally skipped under load. The 15-minute cadence leaves
  plenty of margin for the 2-hour look-ahead.
- The daily check is gated on "first run after 06:30 IST", so on LIKELY
  days the email typically lands 06:35–07:10 depending on scheduler
  delay.
- Open-Meteo's 15-minutely data in India is downscaled from hourly
  models, not radar nowcasting — treat start/end times as ±15 min.
- To change locations, edit `config.env`, then re-run the secret lines
  of `setup_github.sh` (or `gh secret set HOME_LAT --body "..."` etc.).
