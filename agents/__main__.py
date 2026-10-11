"""Container entry point: python -m agents serves the agent named by $AGENT (default: factcheck)."""

import os
import sys

from agents.factcheck import server as factcheck_server
from agents.ghost_fleet import server as ghost_fleet_server

SERVERS = {"factcheck": factcheck_server.main, "ghost_fleet": ghost_fleet_server.main}


def main() -> None:
    name = os.environ.get("AGENT", "factcheck")
    if name not in SERVERS:
        sys.exit(f"unknown AGENT {name!r}; expected one of {', '.join(sorted(SERVERS))}")
    SERVERS[name]()


if __name__ == "__main__":
    main()
