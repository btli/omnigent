"""Validate candidate migration metadata without executing candidate Python."""

import pytest
import stage as stage_mod
from test_stage import MIGRATIONS_DIR, Env, git


@pytest.mark.parametrize("ring", [stage_mod.STAGING, stage_mod.PRODUCTION])
@pytest.mark.parametrize("hourly", [False, True])
def test_missing_parent_never_publishes(tmp_path, ring, hourly):
    env = Env(tmp_path)
    (env.seed / MIGRATIONS_DIR).mkdir(parents=True)
    pr = env.add_pr(
        1, f"{MIGRATIONS_DIR}/merge.py", "revision = 'merge'\ndown_revision = 'missing'\n"
    )
    before = git(env.work, "ls-remote", str(env.fork)).stdout
    with pytest.raises(stage_mod.StageError, match=r"missing.*missing"):
        env.run([pr], ring=ring, staging_only=hourly)
    assert git(env.work, "ls-remote", str(env.fork)).stdout == before


@pytest.mark.parametrize(
    ("source", "error"),
    [
        ("revision = 'b'\ndown_revision = None\n", "heads"),
        ("revision = 'a'\ndown_revision = None\n", "duplicate"),
        ("revision = 'b'\ndown_revision = 'b'\n", "cycle"),
        ("revision = 'b'\ndown_revision = 'a'\ndepends_on = 'gone'\n", "missing"),
        ("raise RuntimeError('do not execute')\n", "revision"),
    ],
)
def test_invalid_graph_rejected(tmp_path, source, error):
    env = Env(tmp_path)
    (env.seed / MIGRATIONS_DIR).mkdir(parents=True)
    base = env.add_pr(1, f"{MIGRATIONS_DIR}/a.py", "revision = 'a'\ndown_revision = None\n")
    other = env.add_pr(2, f"{MIGRATIONS_DIR}/b.py", source)
    with pytest.raises(stage_mod.StageError, match=error):
        env.run([base, other])


def test_staging_cannot_drop_published_migration(tmp_path):
    env = Env(tmp_path)
    (env.seed / MIGRATIONS_DIR).mkdir(parents=True)
    pr = env.add_pr(1, f"{MIGRATIONS_DIR}/a.py", "revision = 'a'\ndown_revision = None\n")
    published = env.run([pr], staging_only=True)
    with pytest.raises(stage_mod.StageError, match="published migration history changed"):
        env.run([], staging_only=True)
    assert env.fork_ref("refs/heads/staging") == published["staging_sha"]
