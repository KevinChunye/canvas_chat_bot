"""Shared Canvas plumbing for every agent in this repo.

    env       Canvas host, control line, secrets from the environment
    canvas    the host-pinned, path-allowlisted Canvas client with the control gate
    forum     reading a discussion topic into one flat entry table
    text      HTML, hashing, secret scrubbing and secret-shaped token detection
    log       JSONL log, one file per run, scrubbed
    times     UTC timestamps

Nothing here knows about any one agent's behaviour; agent packages live under agents/.
"""
