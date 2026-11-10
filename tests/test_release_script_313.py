"""Release script refuses the wrong commit; uses only local-path remotes."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("git")), reason="bash and git required"
)
SCRIPT = Path(__file__).parent.parent / "scripts" / "release.sh"
ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=ENV, check=True,
                          capture_output=True, text=True).stdout.strip()


def commit(work, version="1.2.3", log="## 1.2.3 - 2026-01-01\n"):
    d = work / "custom_components" / "foxess_charger"
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text('{"version": "%s"}' % version)
    (work / "CHANGELOG.md").write_text("# Changelog\n\n" + log)
    git(work, "add", "-A")
    git(work, "commit", "-qm", "change")


@pytest.fixture
def repo(tmp_path):
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(work))
    git(work, "checkout", "-q", "-b", "main")
    (work / "scripts").mkdir()
    shutil.copy(SCRIPT, work / "scripts" / "release.sh")
    commit(work)
    git(work, "push", "-q", "origin", "main")
    return work


def run(work, *args):
    return subprocess.run(["bash", "scripts/release.sh", *args, "--skip-tests"], cwd=work,
                          env=ENV, capture_output=True, text=True)


def remote_tag(work, tag):
    return git(work, "ls-remote", "--tags", "origin", f"refs/tags/{tag}")


def test_stable_happy_path(repo):
    r = run(repo, "1.2.3")
    assert r.returncode == 0, r.stderr
    assert remote_tag(repo, "v1.2.3")
    assert git(repo, "rev-parse", "v1.2.3^{commit}") == git(repo, "rev-parse", "HEAD")


def test_beta_from_feature_branch(repo):
    git(repo, "checkout", "-q", "-b", "feat")
    commit(repo, "1.3.0-beta.1", "## 1.3.0 - wip\n")
    git(repo, "push", "-q", "origin", "feat")
    r = run(repo, "1.3.0-beta.1")
    assert r.returncode == 0, r.stderr
    assert remote_tag(repo, "v1.3.0-beta.1")
    assert "pre-release" in r.stdout


def test_dry_run_tags_nothing(repo):
    r = run(repo, "1.2.3", "--dry-run")
    assert r.returncode == 0 and "dry run: nothing tagged" in r.stdout
    assert not git(repo, "tag") and not remote_tag(repo, "v1.2.3")


def refused(r, word):
    assert r.returncode != 0 and word in r.stderr


def test_bad_version(repo):
    for v in ("v1.2.3", "1.2", "1.2.3-rc.1", "1.2.3-beta"):
        refused(run(repo, v), "must be X.Y.Z")


def test_dirty_tree(repo):
    (repo / "stray.txt").write_text("x")
    refused(run(repo, "1.2.3"), "dirty")


def test_not_a_repo(tmp_path):
    refused(subprocess.run(["bash", str(SCRIPT), "1.2.3", "--skip-tests"], cwd=tmp_path,
                           env={**ENV, "GIT_CEILING_DIRECTORIES": str(tmp_path.parent)},
                           capture_output=True, text=True), "git work tree")


def test_head_ahead_of_origin_main(repo):
    commit(repo, log="## 1.2.3 - 2026-01-01\n# more\n")  # committed, not pushed
    refused(run(repo, "1.2.3"), "is not origin/main")
    assert not remote_tag(repo, "v1.2.3")


def test_manifest_mismatch(repo):
    refused(run(repo, "1.2.4"), "manifest.json version")


def test_changelog_missing(repo):
    commit(repo, log="## 9.9.9\n")
    git(repo, "push", "-q", "origin", "main")
    refused(run(repo, "1.2.3"), "CHANGELOG.md")


def test_tag_exists(repo):
    git(repo, "tag", "v1.2.3")
    git(repo, "push", "-q", "origin", "v1.2.3")
    git(repo, "tag", "-d", "v1.2.3")
    refused(run(repo, "1.2.3"), "already exists")
