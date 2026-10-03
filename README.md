# Footnote

An autonomous participant in a Canvas discussion forum where AI agents talk to each other. Every few hours
it reads the forum, checks the factual claims other agents make, and replies only when it has something
worth adding. The voice is dry, deadpan and a little funny. Posts can end with `— Footnote, an agent`
(`sign_posts` in `config.toml`; currently off).

The LLM never acts. It reads new entries and returns a JSON verdict and proposal. Plain Python code
validates that proposal and decides whether anything gets written, enforcing every limit along the way.
See [ARCHITECTURE.md](ARCHITECTURE.md) for how it works and which safety properties are enforced where.

## What it can access

- **The token:** `CANVAS_API_KEY` is a personal access token, and Canvas can't scope those. The token can do
  anything your Canvas account can, in every course you're enrolled in.
- **The code:** every Canvas request is checked against an allowlist before it is sent. The running agent can only:
  - read its own user id (`GET /api/v1/users/self`);
  - read the forum topic (`GET` under `/courses/40577/discussion_topics/448963`);
  - post new entries and replies in that topic.

  Anything else (other topics, other courses, the inbox, files, profile) raises an error without sending a
  request. There are no edit or delete calls at all. The one-time discovery script is the only code that may
  list courses and their topics, read-only. Details and tests: [ARCHITECTURE.md](ARCHITECTURE.md#canvas-access-agentcanvaspy).
- **Where the token lives:** only in the agent's Maritime secrets (and in your local environment if you run it
  there). The LLM never sees it.
- **Limiting the token itself:** give it an expiry date when you create it (Canvas → Account → Settings → New
  Access Token), revoke it there when the project ends, or run the agent from a separate Canvas account that is
  enrolled only in this course, if the course team provides one.

## Setup

Python 3.11+.

```bash
pip install -r requirements-dev.txt     # runtime deps: requests, openai, pandas; dev adds pytest, requests-mock
```

Environment variables (secrets never go in files):

| Variable | Required | Purpose |
| --- | --- | --- |
| `CANVAS_API_KEY` | yes | Canvas token for the agent's account |
| `OPENAI_API_KEY` | yes | your own OpenAI key (calls go straight to api.openai.com) |
| `AGENT_DATA_DIR` | prod | where the SQLite database, lock file and logs live. Defaults to `./state` (gitignored). The Docker image sets `/data` |
| `OPENAI_MODEL` | no | overrides `[openai] model`; only models with a price under `[openai.prices]` can be called |

Everything else lives in `config.toml` (committed, no secrets): forum ids, model, prices, budget caps and post limits.

## Discovery (one time)

```bash
python scripts/discover.py "Agent Discussion Forum"
```

This is read-only. It lists your active courses and their discussion topics, finds the single topic whose title
contains the string you pass, and writes `course_id` and `forum_topic_id` into `config.toml`. The committed config
already holds the result: course `40577`, topic `448963`.

## Running locally

```bash
python -m agent.cli dry-run      # real Canvas reads + one real LLM call (counted in the budget); no writes, no memory changes
python -m agent.cli run-cycle    # one live cycle, then exit
python -m agent.cli status       # halt flag, failure count, pending writes, spend, recent cycles
python -m agent.cli report --out report.md   # markdown evidence report (pandas over SQLite)
python -m pytest -q              # test suite, no live calls
```

`dry-run` prints the full decision as JSON: the claims it found, verdicts, confidence, the proposed post, and
any reasons the code rejected it.

## Deployment (maritime.sh)

Footnote ships as a Maritime **custom container**. The Dockerfile at the repo root follows Maritime's documented
contract: it binds `0.0.0.0:$PORT`, answers `GET /health` and `POST /chat`, and keeps state under `/data`.
Maritime's docs say `/data` "survives restarts, redeploys, and sleep/wake", so the SQLite file and logs persist.

Scheduling: a sleeping VM's own timers never fire, so the agent publishes its schedule at `GET /schedules`
(`0 */3 * * *`, UTC, prompt `run-cycle`). Maritime polls that endpoint while the agent is awake, registers a real
wake trigger, and at each occurrence delivers `{"message": "run-cycle", "source": "scheduled"}` to `POST /chat`.
The server replies at once (Maritime allows 30 seconds) and runs one cycle in a background thread. Any other
chat message does nothing.

Create the agent as a **custom** agent built from this repo. Don't redeploy an agent made from a template:
`maritime deploy --source github` onto an OpenClaw agent rebuilt from the repo but kept the OpenClaw framework,
and no schedule was registered. Run these on your own machine (the `maritime` CLI: `npm install -g maritime-cli`,
then `maritime login`):

```bash
maritime create footnote_agent --framework custom \
  --repo https://github.com/KevinChunye/canvas_chat_bot --branch factcheck_agent --idle 900
# then add CANVAS_API_KEY and OPENAI_API_KEY as secrets under the agent's Settings in the dashboard
maritime restart footnote_agent
maritime info footnote_agent             # Framework: custom
maritime triggers list footnote_agent    # a cron trigger synced from /schedules ("byo_sync")
```

Setting the keys in the dashboard keeps them out of your shell history. Do not press "Use Maritime LLM": it
would replace `OPENAI_API_KEY` with a proxy token, and the client is pinned to api.openai.com. If the repo is
private, Maritime needs its GitHub App installed on it.

`--idle 900` keeps the VM awake 15 minutes after each wake so the background cycle finishes before auto-sleep.
If the VM does sleep mid-cycle, it resumes from a snapshot, and the idempotent write path covers any
interrupted POST.

The `agent.cli` commands below run **inside the agent**, not on your machine: the code and the database live
there. Either type them in the agent's **Console** tab in the dashboard, or send them with `maritime exec`. With
`exec`, put `--` before the command. Without it the CLI tries to read `-m` as one of its own options and fails
with "unknown option". `AGENT_DATA_DIR` is spelled out so the command always uses the `/data` database:

```bash
maritime exec footnote_agent -- env AGENT_DATA_DIR=/data python -m agent.cli status
maritime exec footnote_agent -- env AGENT_DATA_DIR=/data python -m agent.cli report
maritime exec footnote_agent -- env AGENT_DATA_DIR=/data python -m agent.cli dry-run   # no posts, about $0.001
```

### Fallback: cron on any Linux box

```bash
git clone -b factcheck_agent https://github.com/KevinChunye/canvas_chat_bot /opt/footnote
pip install -r /opt/footnote/requirements.txt
printf 'CANVAS_API_KEY=...\nOPENAI_API_KEY=...\nAGENT_DATA_DIR=/var/lib/footnote\n' > /etc/footnote.env && chmod 600 /etc/footnote.env
# crontab -e
0 */3 * * * set -a; . /etc/footnote.env; set +a; cd /opt/footnote && python3 -m agent.cli run-cycle >> /var/lib/footnote/cron.log 2>&1
```

## Stopping and unhalting

The agent halts itself, persistently, on any of the following:

- 3 consecutive failed cycles
- a 401/403 from Canvas
- the Canvas token suddenly belonging to a different user
- the $5.00 lifetime OpenAI budget running out

A halted agent does nothing until someone clears the flag:

```bash
python -m agent.cli status
python -m agent.cli unhalt
# on Maritime:
maritime exec footnote_agent -- env AGENT_DATA_DIR=/data python -m agent.cli unhalt
```

The course team can also pause it without touching the agent. Any first line of the topic description other than
exactly `COURSE-TEAM CONTROL: RUNNING` blocks every write.

## Fault test: lost acknowledgement

After a few scheduled cycles have run normally, arm the one-shot fault:

```bash
python -m agent.cli fault drop_ack_once
# on Maritime:
maritime exec footnote_agent -- env AGENT_DATA_DIR=/data python -m agent.cli fault drop_ack_once
```

The next real POST reaches Canvas, but the client discards the response and raises, as if the reply had been lost.
That cycle ends as an error with the write still pending, and the flag clears itself. On the next cycle, the
reconcile step finds the entry by author and content hash, marks the intent confirmed, and posts nothing new.
The JSONL log shows each step (`fault_drop_ack_fired`, `action_ack_lost_fault`, `action_reconcile_start`,
`action_reconcile_found`, `action_confirmed`). `python -m agent.cli report` prints the same trail under
"Fault-injection recovery trail".

Note: the fault only fires on a cycle that actually decides to post, so it may stay armed for a few cycles.
`status` shows whether it is still armed.
