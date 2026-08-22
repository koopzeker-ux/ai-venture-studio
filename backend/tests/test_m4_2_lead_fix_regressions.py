"""LEAD fix-round regression tests (2026-08-22), following REVIEWER's
CRITICAL finding (commit caa30d7): `_git_changed_files` used plain
`git diff --name-only <base_ref>`, which by design never reports untracked
files -- so any new out-of-scope file a worker's Write tool created bypassed
the layer-2 allowed_resources scope check entirely.

Fix: `_git_changed_files` now unions `_git_diff_against_base` (tracked
changes vs. base_ref) with `_git_status_changed_files` (`git status
--porcelain=v1 -z --untracked-files=all`, which sees modified/added/
deleted/renamed AND untracked files). Both raise (fail closed) on a
non-zero git exit rather than silently returning an empty/partial list.

REVIEWER's own test_m4_2_reviewer_adversarial.py already covers: modified/
deleted tracked files, staged renames, the allowed/file.py.evil prefix
trick, the "backend" vs. "backend_evil/" directory-prefix trick, Windows
separator normalization, multiple-changes-one-violation, and empty-allowlist
fail-closed -- at the `_check_scope_violation` unit level. This file adds
the real-git, end-to-end coverage specifically for the untracked-file gap
the fix closes: an exactly-allowed new file must NOT become a false-positive
violation now that new files are visible at all, and combined
new-file-plus-tracked-change scenarios must be caught correctly.
"""
from __future__ import annotations

import subprocess

import pytest

from app.orchestration.run_task import _check_scope_violation, _git_changed_files


def _run_git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(["init", "-q", "-b", "main"], repo)
    _run_git(["config", "user.email", "lead@test.local"], repo)
    _run_git(["config", "user.name", "lead"], repo)
    (repo / "backend").mkdir()
    (repo / "backend" / "tracked.py").write_text("original\n")
    (repo / "backend" / "second.py").write_text("second\n")
    _run_git(["add", "."], repo)
    _run_git(["commit", "-q", "-m", "init"], repo)
    return repo


# 1. exact toegestaan nieuw bestand -> toegestaan
def test_new_file_exactly_in_allowed_resources_is_not_a_violation(git_repo):
    (git_repo / "backend" / "new_allowed.py").write_text("print('fine')\n")
    changed = _git_changed_files(git_repo, git_repo, base_ref="main")
    assert "backend/new_allowed.py" in changed
    violations = _check_scope_violation(changed, allowed_resources=["backend/new_allowed.py"])
    assert violations == []


# 1b. new file inside an allowed directory prefix -> also allowed
def test_new_file_inside_an_allowed_directory_prefix_is_not_a_violation(git_repo):
    (git_repo / "backend" / "scratch").mkdir()
    (git_repo / "backend" / "scratch" / "ping.py").write_text("def ping(): return 'pong'\n")
    changed = _git_changed_files(git_repo, git_repo, base_ref="main")
    assert "backend/scratch/ping.py" in changed
    violations = _check_scope_violation(changed, allowed_resources=["backend/scratch"])
    assert violations == []


# 2. nieuw out-of-scope bestand -> violation (dedicated end-to-end check,
# distinct from REVIEWER's inverted CRITICAL test -- a second file this time)
def test_new_out_of_scope_file_is_flagged(git_repo):
    (git_repo / "backend" / "unexpected.py").write_text("print('scope creep')\n")
    changed = _git_changed_files(git_repo, git_repo, base_ref="main")
    violations = _check_scope_violation(changed, allowed_resources=["backend/tracked.py"])
    assert violations == ["backend/unexpected.py"]


# 3. lege allowlist + nieuw (untracked) bestand -> violation, via the real git path
def test_empty_allowlist_plus_new_untracked_file_is_a_violation_via_real_git(git_repo):
    (git_repo / "backend" / "anything_new.py").write_text("x = 1\n")
    changed = _git_changed_files(git_repo, git_repo, base_ref="main")
    violations = _check_scope_violation(changed, allowed_resources=[])
    assert violations == ["backend/anything_new.py"]


# 4. tracked modified buiten scope -> violation, via the fixed function
def test_tracked_modification_out_of_scope_is_flagged_via_fixed_function(git_repo):
    (git_repo / "backend" / "second.py").write_text("modified content\n")
    changed = _git_changed_files(git_repo, git_repo, base_ref="main")
    violations = _check_scope_violation(changed, allowed_resources=["backend/tracked.py"])
    assert violations == ["backend/second.py"]


# 5. deleted buiten scope -> violation, via the fixed function
def test_tracked_deletion_out_of_scope_is_flagged_via_fixed_function(git_repo):
    (git_repo / "backend" / "second.py").unlink()
    changed = _git_changed_files(git_repo, git_repo, base_ref="main")
    violations = _check_scope_violation(changed, allowed_resources=["backend/tracked.py"])
    assert violations == ["backend/second.py"]


# 6. renamed buiten scope -> violation, via the fixed function (unstaged rename:
# git sees this as an untracked new path + a tracked deletion until staged --
# both must still surface through the union)
def test_unstaged_rename_out_of_scope_is_flagged_via_fixed_function(git_repo):
    (git_repo / "backend" / "tracked.py").rename(git_repo / "backend" / "renamed.py")
    changed = _git_changed_files(git_repo, git_repo, base_ref="main")
    assert "backend/renamed.py" in changed
    violations = _check_scope_violation(changed, allowed_resources=["backend/tracked.py"])
    assert "backend/renamed.py" in violations


# 7. meerdere wijzigingen waarvan één buiten scope -> violation, mixing a new
# untracked file with an in-scope tracked modification
def test_mixed_in_scope_and_out_of_scope_changes_flags_only_the_violation(git_repo):
    (git_repo / "backend" / "tracked.py").write_text("legit change\n")  # in scope
    (git_repo / "backend" / "sneaky.py").write_text("not in scope\n")  # new, untracked, out of scope
    changed = _git_changed_files(git_repo, git_repo, base_ref="main")
    violations = _check_scope_violation(changed, allowed_resources=["backend/tracked.py"])
    assert violations == ["backend/sneaky.py"]


# 8. prefix-truc blijft geblokkeerd, nu via een echt nieuw (untracked) bestand
def test_prefix_trick_still_blocked_for_a_real_new_file(git_repo):
    (git_repo / "backend" / "tracked.py.evil").write_text("x\n")
    changed = _git_changed_files(git_repo, git_repo, base_ref="main")
    violations = _check_scope_violation(changed, allowed_resources=["backend/tracked.py"])
    assert violations == ["backend/tracked.py.evil"]


# 9. traversal blijft geblokkeerd (known LOW/defense-in-depth gap, documented
# by REVIEWER as not concretely reachable via real git output -- confirmed
# here that real git never actually emits a traversal-shaped path for an
# in-repo file, so the gap stays theoretical after this fix too)
def test_git_never_emits_a_traversal_shaped_path_for_an_in_repo_file(git_repo):
    (git_repo / "backend" / "normal.py").write_text("x\n")
    changed = _git_changed_files(git_repo, git_repo, base_ref="main")
    assert all(".." not in path for path in changed)


# 10. Windows/POSIX separators blijven correct behandeld door de echte,
# gefixte functie (niet alleen de losstaande _check_scope_violation-unit-tests)
def test_windows_and_posix_separators_both_correctly_scoped_via_fixed_function(git_repo):
    (git_repo / "backend" / "tracked.py").write_text("modified\n")
    changed = _git_changed_files(git_repo, git_repo, base_ref="main")
    # git itself always reports POSIX-style paths; confirm the allowlist
    # comparison still matches when the allowlist entry uses backslashes.
    violations = _check_scope_violation(changed, allowed_resources=["backend\\tracked.py"])
    assert violations == []


def test_git_changed_files_fails_closed_on_git_error(tmp_path):
    """Neither a real repo nor a git worktree at all -- both the diff and the
    status calls must raise, never silently return []."""
    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()
    with pytest.raises(RuntimeError):
        _git_changed_files(not_a_repo, not_a_repo, base_ref="main")
