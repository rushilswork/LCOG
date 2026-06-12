"""Stage 2: Historical context extraction from git history.

Uses a single `git log --name-only` subprocess call instead of per-commit
diffs, which is 10–100x faster on large repositories.

Produces per-file metrics:
- change_frequency: how often the file changes (hotspot score)
- co_changes: dict of {other_file: co-change count} (logical coupling)
- last_modified_days: days since last commit touching this file
- top_authors: list of (author, commit_count)
- commit_messages: sample of commit messages touching this file
- is_dead_code_candidate: True if not touched in >2 years
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class FileHistory:
    path: str
    change_frequency: int = 0
    co_changes: dict[str, int] = field(default_factory=dict)
    last_modified_days: Optional[float] = None
    top_authors: list[tuple[str, int]] = field(default_factory=list)
    commit_messages: list[str] = field(default_factory=list)
    is_dead_code_candidate: bool = False


@dataclass
class RepoHistory:
    files: dict[str, FileHistory] = field(default_factory=dict)
    total_commits: int = 0
    active_authors: list[str] = field(default_factory=list)
    major_themes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Git log via subprocess  (much faster than gitpython diff-per-commit)
# ---------------------------------------------------------------------------

DEAD_CODE_THRESHOLD_DAYS = 730   # ~2 years


def _run_git_log(repo_path: Path, max_commits: int) -> Optional[str]:
    """Run one `git log --name-only` command and return raw stdout.

    Uses null-byte (\\x00) as the commit record separator so the output is
    easy to split without worrying about newlines in commit subjects.

    Returns None on any failure (git not found, not a repo, timeout, etc.).
    """
    try:
        result = subprocess.run(
            [
                "git", "log",
                "--name-only",   # list changed files below each commit header
                "--no-renames",  # treat renames as delete+add; simpler to parse
                f"--max-count={max_commits}",
                "--format=%x00%H%x01%aN%x01%s%x01%ct",  # null-delimited header
                "HEAD",
            ],
            capture_output=True,
            cwd=str(repo_path),
            timeout=120,
        )
    except FileNotFoundError:
        return None   # git not installed
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None   # not a git repo, or no HEAD yet
    return result.stdout.decode("utf-8", errors="replace")


def _parse_git_log(log_text: str) -> list[dict]:
    """Parse the output of _run_git_log into a list of commit dicts.

    Each dict has keys: hexsha, author, subject, ts (float), files (set[str]).
    """
    commits: list[dict] = []
    for block in log_text.split("\x00"):
        block = block.strip("\n")
        if not block:
            continue
        lines = block.splitlines()
        if not lines:
            continue
        # First line is the format header; remaining non-blank lines are files
        header = lines[0]
        parts = header.split("\x01", 3)
        if len(parts) < 4:
            continue
        hexsha, author, subject, ts_str = parts[0], parts[1], parts[2], parts[3]
        try:
            ts = float(ts_str.strip())
        except (ValueError, AttributeError):
            ts = 0.0
        changed_files = {ln.strip() for ln in lines[1:] if ln.strip()}
        commits.append({
            "hexsha":  hexsha.strip(),
            "author":  author.strip() or "unknown",
            "subject": subject.strip(),
            "ts":      ts,
            "files":   changed_files,
        })
    return commits


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_git(repo_path: Path, max_commits: int = 2000) -> RepoHistory:
    """Analyse the git history of *repo_path* and return a RepoHistory.

    Returns an empty RepoHistory if the directory is not a git repo, has no
    commits, or git is not installed -- so the pipeline degrades gracefully.
    """
    log_text = _run_git_log(repo_path, max_commits)
    if not log_text:
        return RepoHistory()

    commits = _parse_git_log(log_text)
    if not commits:
        return RepoHistory()

    history = RepoHistory(total_commits=len(commits))
    now_ts = datetime.now(tz=timezone.utc).timestamp()

    author_counter: Counter = Counter()
    all_messages: list[str] = []

    # Per-file accumulators
    file_freq: dict[str, int] = defaultdict(int)
    file_authors: dict[str, Counter] = defaultdict(Counter)
    file_messages: dict[str, list[str]] = defaultdict(list)
    file_last_ts: dict[str, float] = {}
    commit_files: dict[str, set[str]] = {}  # hexsha → set of touched paths

    for commit in commits:
        author  = commit["author"]
        subject = commit["subject"]
        ts      = commit["ts"]
        hexsha  = commit["hexsha"]
        touched = commit["files"]

        author_counter[author] += 1
        all_messages.append(subject)
        commit_files[hexsha] = touched

        for fpath in touched:
            file_freq[fpath] += 1
            file_authors[fpath][author] += 1
            file_messages[fpath].append(subject)
            if fpath not in file_last_ts or ts > file_last_ts[fpath]:
                file_last_ts[fpath] = ts

    # Co-change matrix (files that change together)
    co_change: dict[str, Counter] = defaultdict(Counter)
    for touched in commit_files.values():
        file_list = list(touched)
        for i, fa in enumerate(file_list):
            for fb in file_list[i + 1:]:
                co_change[fa][fb] += 1
                co_change[fb][fa] += 1

    for fpath, freq in file_freq.items():
        last_ts = file_last_ts.get(fpath)
        last_modified_days = (now_ts - last_ts) / 86400 if last_ts else None

        history.files[fpath] = FileHistory(
            path=fpath,
            change_frequency=freq,
            co_changes=dict(co_change[fpath].most_common(10)),
            last_modified_days=last_modified_days,
            top_authors=file_authors[fpath].most_common(3),
            commit_messages=file_messages[fpath][:10],
            is_dead_code_candidate=(
                last_modified_days is not None
                and last_modified_days > DEAD_CODE_THRESHOLD_DAYS
            ),
        )

    history.active_authors = [a for a, _ in author_counter.most_common(10)]
    history.major_themes = _cluster_messages(all_messages)
    return history


# ---------------------------------------------------------------------------
# Commit message clustering
# ---------------------------------------------------------------------------

_STOP_WORDS = {
    "fix", "add", "update", "refactor", "remove", "change", "merge", "bump",
    "the", "a", "an", "in", "of", "to", "for", "and", "or", "is", "was",
    "with", "on", "at", "by", "from", "into", "use", "using",
}


def _cluster_messages(messages: list[str], top_n: int = 10) -> list[str]:
    """Return the top-N recurring noun-like tokens across all commit messages."""
    counter: Counter = Counter()
    for msg in messages:
        tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", msg.lower())
        for tok in tokens:
            if tok not in _STOP_WORDS:
                counter[tok] += 1
    return [word for word, _ in counter.most_common(top_n)]


# ---------------------------------------------------------------------------
# Convenience: hotspot ranking
# ---------------------------------------------------------------------------

def hotspot_ranking(history: RepoHistory, top_n: int = 20) -> list[tuple[str, int]]:
    """Return files sorted by change_frequency descending."""
    ranked = sorted(
        history.files.items(),
        key=lambda kv: kv[1].change_frequency,
        reverse=True,
    )
    return [(k, v.change_frequency) for k, v in ranked[:top_n]]
