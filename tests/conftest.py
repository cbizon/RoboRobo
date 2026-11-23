"""Shared test setup: an SSH signing key standing in for the scientist's, so
`program approve` can sign and `program check` can verify."""

import subprocess

import pytest


@pytest.fixture(scope="session")
def signing_key(tmp_path_factory):
    folder = tmp_path_factory.mktemp("signing")
    key = folder / "scientist"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "t@example.org",
                    "-f", str(key)], check=True)
    return key


def isolated_gitconfig(monkeypatch, tmp_path, signing_key):
    """No global git config but ours: a fixed identity that signs approvals with
    the test key, which is RoboRobo's only approver for the test."""
    from roborobo import approvals
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    (tmp_path / "gitconfig").write_text(
        "[user]\n\tname = Test Scientist\n\temail = t@example.org\n"
        f"\tsigningkey = {signing_key}.pub\n"
        "[gpg]\n\tformat = ssh\n"
        "[init]\n\tdefaultBranch = main\n")
    public = (signing_key.parent / f"{signing_key.name}.pub").read_text().split()
    approvers = tmp_path / "approvers"
    approvers.write_text(f"t@example.org {public[0]} {public[1]}\n")
    monkeypatch.setattr(approvals, "APPROVERS", approvers)
