"""Cluster commands for agent runs that hold no keys.

An agent pod (see ``roborobo.kube``) has no SSH key, so ``program submit``,
``program jobs`` and ``program fetch`` can't reach Hatteras from it. With
``ROBOROBO_SERVICE_URL`` set, those commands send their arguments to the
service instead, which runs them in the same repository (the pods share it)
with its keys and returns the output and exit code.

The service accepts a command only:

- with the token made for the run in progress (none when nothing runs);
- for a repository it serves;
- if it is one of ``FORWARDED``: never ``approve``, ``delete``, or anything
  else that isn't the run's to do.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FORWARDED = {"submit", "jobs", "fetch"}
TIMEOUT_SECONDS = 30 * 60


class RunToken:
    """The token of the run in progress, if any."""

    def __init__(self):
        self._value: str | None = None
        self._lock = threading.Lock()

    def set(self, value: str | None) -> None:
        with self._lock:
            self._value = value

    def matches(self, value: str | None) -> bool:
        with self._lock:
            return bool(self._value) and value == self._value


CURRENT = RunToken()


# --- the client, in an agent pod -----------------------------------------------

def forward(argv: list[str]) -> int:
    """Send a program command to the service; print its output; return its
    exit code."""
    args = list(argv)
    repo = Path.cwd()
    if "--repo" in args:
        i = args.index("--repo")
        repo = Path(args[i + 1])
        del args[i:i + 2]
    body = json.dumps({"argv": args, "repo": str(repo.expanduser().resolve()),
                       "token": os.environ.get("ROBOROBO_RUN_TOKEN", "")}).encode()
    url = os.environ["ROBOROBO_SERVICE_URL"].rstrip("/") + "/program"
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS + 60) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as error:
        print(f"the service refused `program {' '.join(args)}`: {error.read().decode()}",
              file=sys.stderr)
        return 2
    print(result["output"], end="")
    return result["code"]


# --- the server, in the service ------------------------------------------------

def run_command(argv: list[str], repo: Path) -> tuple[int, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in ("ROBOROBO_SERVICE_URL", "ROBOROBO_RUN_TOKEN")}
    done = subprocess.run([sys.executable, "-m", "roborobo.cli", *argv, "--repo", str(repo)],
                          cwd=repo, env=env, capture_output=True, text=True,
                          timeout=TIMEOUT_SECONDS)
    return done.returncode, done.stdout + done.stderr


def handler(repos: list[Path], token: RunToken):
    served = {Path(r).expanduser().resolve() for r in repos}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002 (the base class's name)
            pass

        def refuse(self, code: int, why: str) -> None:
            self.send_response(code)
            self.end_headers()
            self.wfile.write(why.encode())

        def do_POST(self):  # noqa: N802 (the base class's name)
            if self.path != "/program":
                return self.refuse(404, "no such endpoint")
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                argv, repo = list(body["argv"]), Path(body["repo"]).resolve()
            except (ValueError, KeyError, TypeError):
                return self.refuse(400, "send {argv, repo, token}")
            if not token.matches(body.get("token")):
                return self.refuse(403, "no run with that token is in progress")
            if repo not in served:
                return self.refuse(403, f"{repo} isn't a program this service runs")
            if not argv or argv[0] not in FORWARDED or "--repo" in argv:
                return self.refuse(403, f"only {', '.join(sorted(FORWARDED))} are run for "
                                        "agent runs")
            code, output = run_command(argv, repo)
            payload = json.dumps({"code": code, "output": output}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def serve(host: str, port: int, repos: list[Path], token: RunToken = CURRENT
          ) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), handler(repos, token))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
