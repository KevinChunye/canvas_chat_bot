# Footnote

An autonomous agent for the Canvas **Agent Discussion Forum**. Every three hours it reads the forum, checks the
factual claims other agents make, and replies only when it has something worth adding. Skipping is the default.

The language model never acts. It returns a JSON proposal, and plain Python code decides whether anything is
written to Canvas. Design details and the test behind each safety property: [ARCHITECTURE.md](ARCHITECTURE.md).

## How a cycle works

1. A scheduled trigger starts one cycle. A file lock stops cycles from overlapping, and a halted agent exits
   immediately.
2. It confirms any earlier post whose outcome was unknown, then reads the forum and picks out new or edited
   entries.
3. If nothing is new, it records a no-post run without calling the model. Otherwise it makes one LLM call, which
   returns the claims it found, a verdict and confidence for each, and a decision to post or skip.
4. Code re-checks the proposal: the target, length, output filters, repetition, calibration and every rate limit.
   It re-reads the control line, posts, then re-fetches the entry to confirm it was saved.

## How it meets the requirements

| Requirement | Implementation |
| --- | --- |
| Runs on a schedule, no prompting | Maritime cron trigger every 3 hours (or plain cron); one cycle per run |
| Decides for itself, may do nothing | Skip by default; every no-post run is logged with its reason |
| Persistent memory | SQLite: seen entries, claims, actions with an audit trail, cycles, spend, state |
| Ignores its own posts, no repeats | Own user id skipped; never replies twice to an entry; similarity check against past posts |
| No duplicate effects | Intent saved before each POST; after an unknown outcome it searches by author and content hash before any retry |
| Verifies every post | Re-fetches the entry by id and checks author and content hash |
| Rate limits | At most 1 post per 48 hours (code floor), 3 per hour, 2 per cycle |
| Control line | Re-read before every write; posts only if the first line is exactly `COURSE-TEAM CONTROL: RUNNING` |
| Retries and stopping | Backoff with jitter; 3 failed cycles, a 401/403, or an exhausted budget set a persistent halt |
| Untrusted input | Forum text is passed as delimited data; the model has no tools; code validates its output |
| Small blast radius | Requests allowlisted to this one topic; no edit or delete calls; only canvas.mit.edu and api.openai.com |
| Secrets | Environment variables only; logs scrubbed; output filters block keys, URLs, emails and phone numbers |

## Quick start (local)

Python 3.11+.

```bash
pip install -r requirements-dev.txt
export CANVAS_API_KEY=...  OPENAI_API_KEY=...
python scripts/discover.py "Agent Discussion Forum"   # read-only; writes course and topic ids to config.toml
python -m agent.cli dry-run                           # real reads and a real decision, no writes
python -m pytest -q                                   # offline tests: fake Canvas, fake model
```

The committed `config.toml` already points at the forum (course 40577, topic 448963).

## Deploy (maritime.sh)

Run these on your own machine with the Maritime CLI (`npm install -g maritime-cli`, then `maritime login`):

```bash
maritime create footnote_agent --framework custom \
  --repo https://github.com/KevinChunye/canvas_chat_bot --branch factcheck_agent --idle 900
# add CANVAS_API_KEY and OPENAI_API_KEY as secrets in the agent's Settings (not "Use Maritime LLM")
maritime restart footnote_agent
maritime triggers list footnote_agent      # expect one cron trigger, synced from the agent's /schedules
```

To ship a code change: `maritime deploy footnote_agent --source github --repo <repo> --branch factcheck_agent --wait`.
The database and logs live on the persistent `/data` volume, which survives restarts and redeploys.

Without Maritime, use cron on any Linux host:

```
0 */3 * * * set -a; . /etc/footnote.env; set +a; cd /opt/footnote && python3 -m agent.cli run-cycle
```

## Operating it

These run inside the agent: in its Console tab, or from your machine with
`maritime exec footnote_agent -- env AGENT_DATA_DIR=/data python -m agent.cli <command>`.
The `--` is required; without it the CLI tries to read `-m` as its own option.

| Command | What it does |
| --- | --- |
| `status` | halt flag, failure count, pending posts, spend, recent cycles |
| `report` | markdown summary: every run, no-post reasons, posts with links, fault trail, spend |
| `dry-run` | full cycle with a real decision, no writes |
| `run-cycle` | one live cycle (what the schedule runs) |
| `unhalt` | clear the halt flag after you have fixed the cause |
| `fault drop_ack_once` | one-shot failure test: the next real POST "loses" its reply; the next cycle must confirm the post without duplicating it |

## Configuration (`config.toml`, no secrets)

- Forum ids, agent name, and whether posts are signed (`sign_posts`, currently off).
- Model (`gpt-6-luna`) and per-token prices. Estimates err high, and an unpriced model is refused.
- Budget caps: $5.00 lifetime (hard), $0.50 per day, $0.05 per cycle.
- Limits: `min_hours_between_posts` (floor 48), posts per hour and per cycle, word range 40–160, confidence
  needed to call a claim wrong (0.85).

Caps can be made stricter in config, never looser.

## Good to know

- **Token scope.** A Canvas personal access token can do anything your account can; Canvas doesn't let you scope
  it. The code is the boundary. Give the token an expiry date and revoke it when you are done.
- **Pausing.** The course team can pause the agent by changing the forum's control line. Every write checks it
  first.
- **Fault test timing.** The fault fires only on a real post, so with one post per 48 hours it can take up to
  two days to fire. `status` shows whether it is still armed.
- **Never commit** `.env`, `state/`, `logs/` or `*.db`; `.gitignore` covers them. For a ZIP, use
  `git archive --format=zip -o footnote.zip factcheck_agent`, which includes only committed files.
