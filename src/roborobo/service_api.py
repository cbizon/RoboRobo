"""Cluster commands for agent runs that hold no keys.

An agent pod (see ``roborobo.kube``) has no SSH key, so ``program submit``,
``program jobs`` and ``program fetch`` can't reach Hatteras from it, and
``program check`` can't fetch the approval tags from the program's remote. With
``ROBOROBO_SERVICE_URL`` set, those commands send their arguments to the
service instead, which runs them in the same repository (the pods share it)
with its keys and returns the output and exit code.

The service accepts a command only:

- with the token of a run in progress, for that run's own folder (its line's
  worktree): a run can't send commands for another line;
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

# check only reads, but it fetches approval tags from the remote, which an
# agent pod can't reach.
# next-id: ids come from the service, so lines running at once never collide.
# wake: a line's timers live in the service's database.
# cancel: scancel needs the service's key; a run cancels only jobs of its program.
# record: it takes an artifact id, which must come from the service's database.
FORWARDED = {"submit", "jobs", "fetch", "check", "next-id", "wake", "cancel", "record"}
TIMEOUT_SECONDS = 30 * 60


class RunTokens:
    """The tokens of the runs in progress, each bound to the folder its run
    works in."""

    def __init__(self):
        self._runs: dict[str, Path] = {}
        self._lock = threading.Lock()

    def add(self, token: str, repo: Path) -> None:
        with self._lock:
            self._runs[token] = Path(repo).expanduser().resolve()

    def remove(self, token: str) -> None:
        with self._lock:
            self._runs.pop(token, None)

    def repo(self, token: str | None) -> Path | None:
        with self._lock:
            return self._runs.get(token) if token else None


ACTIVE = RunTokens()


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


def handler(tokens: RunTokens):

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
            own = tokens.repo(body.get("token"))
            if own is None:
                return self.refuse(403, "no run with that token is in progress")
            if repo != own:
                return self.refuse(403, f"{repo} isn't this run's folder ({own})")
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


def serve(host: str, port: int, tokens: RunTokens = ACTIVE) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), handler(tokens))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
