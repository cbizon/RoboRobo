#!/bin/sh
# Starts the RoboRobo service in its pod.
#
# HOME is the volume, laid out like the scientist's Mac
# (~/Projects/experiments/<program>, ~/Projects/experiments/research-programs),
# so paths rendered into program files match. Every setting is required;
# a missing one stops the pod with a message saying which.
#
#   PROGRAMS        space-separated name=git@github.com:owner/repo.git; each is
#                   cloned to ~/Projects/experiments/<name> with the deploy key
#                   /secrets/ssh/github-<name> (write access, for pushing)
#   ROBOROBO_URL    where to clone RoboRobo from (https; it is public)
#   ROBOROBO_REF    the branch of RoboRobo to run
#   GIT_NAME, GIT_EMAIL   the identity commits are made with
#   POLL_MINUTES    how often to look at each program
#   DASHBOARD_PORT  where the dashboard listens in the pod
#   CLAUDE_CODE_OAUTH_TOKEN   from `claude setup-token`, for the agent runs
#   /secrets/ssh/known_hosts  verified host keys for github.com and Hatteras
#   /secrets/ssh/hatteras     the service's own key for Hatteras (optional
#                             until a program lists the host)
set -eu
: "${PROGRAMS:?name=git@github.com:owner/repo.git ...}"
: "${ROBOROBO_URL:?}" "${ROBOROBO_REF:?}" "${GIT_NAME:?}" "${GIT_EMAIL:?}" "${POLL_MINUTES:?}"
: "${DASHBOARD_PORT:?}"
: "${CLAUDE_CODE_OAUTH_TOKEN:?from claude setup-token}"
test -f /secrets/ssh/known_hosts || { echo "missing /secrets/ssh/known_hosts" >&2; exit 1; }

mkdir -p "$HOME/.ssh" "$HOME/.tmp" "$HOME/.roborobo" "$HOME/Projects/experiments"
chmod 700 "$HOME/.ssh"
cp /secrets/ssh/known_hosts "$HOME/.ssh/known_hosts"
config="$HOME/.ssh/config"
: > "$config"
if [ -f /secrets/ssh/hatteras ]; then
  install -m 600 /secrets/ssh/hatteras "$HOME/.ssh/hatteras"
  printf 'Host hatteras\n  HostName ht1.renci.org\n  User bizon\n  IdentityFile ~/.ssh/hatteras\n  IdentitiesOnly yes\n  ControlMaster auto\n  ControlPath ~/.ssh/cm-%%r@%%h:%%p\n  ControlPersist 10m\n\n' >> "$config"
fi

git config --global user.name "$GIT_NAME"
git config --global user.email "$GIT_EMAIL"

experiments="$HOME/Projects/experiments"
roborobo="$experiments/research-programs"
if [ -d "$roborobo/.git" ]; then
  git -C "$roborobo" fetch -q origin
  git -C "$roborobo" checkout -q "$ROBOROBO_REF"
  git -C "$roborobo" merge -q --ff-only "origin/$ROBOROBO_REF"
else
  git clone -q --branch "$ROBOROBO_REF" "$ROBOROBO_URL" "$roborobo"
fi

repos=""
for spec in $PROGRAMS; do
  name="${spec%%=*}"
  url="${spec#*=}"
  key="/secrets/ssh/github-$name"
  test -f "$key" || { echo "missing deploy key $key for $name" >&2; exit 1; }
  install -m 600 "$key" "$HOME/.ssh/github-$name"
  printf 'Host github-%s\n  HostName github.com\n  User git\n  IdentityFile ~/.ssh/github-%s\n  IdentitiesOnly yes\n\n' "$name" "$name" >> "$config"
  repo="$experiments/$name"
  if [ ! -d "$repo/.git" ]; then
    git clone -q "git@github-$name:${url#git@github.com:}" "$repo"
  fi
  repos="$repos --repo $repo"
done

# shellcheck disable=SC2086
exec uv run --project "$roborobo" program serve $repos --state "$HOME/.roborobo" \
  --poll-minutes "$POLL_MINUTES" --dashboard-host 0.0.0.0 --dashboard-port "$DASHBOARD_PORT"
