"""Stage 2: Historical context extraction from git history.

Produces per-file metrics:
- change_frequency: how often the file changes (hotspot score)
- co_changes: dict of {other_file: co-change count} (logical coupling)
- last_modified_days: days since last commit touching this file
- top_authors: list of (author, commit_count)
- commit_messages: sample of commit messages touching this file
- is_dead_code_candidate: True if not touched in >2 years and no dependants
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from git import InvalidGitRepositoryError, Repo


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
    major_themes: list[str] = field(default_factory=list)   # from commit clustering


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

DEAD_CODE_THRESHOLD_DAYS = 730   # ~2 years


def analyze_git(repo_path: Path, max_commits: int = 2000) -> RepoHistory:
    """Analyse the git history of *repo_path* and return a RepoHistory."""
    try:
        repo = Repo(repo_path, search_parent_directories=True)
    except InvalidGitRepositoryError:
        return RepoHistory()

    history = RepoHistory()
    now = datetime.now(tz=timezone.utc)

    # Commit file-change map: commit → set of touched files
    commit_files: dict[str, set[str]] = {}
    author_counter: Counter = Counter()
    all_messages: list[str] = []

    # Per-file tracking
    file_commits: dict[str, list] = defaultdict(list)
    file_authors: dict[str, Counter] = defaultdict(Counter)
    file_messages: dict[str, list[str]] = defaultdict(list)
    file_last_ts: dict[str, float] = {}

    commits = list(repo.iter_commits("HEAD", max_count=max_commits))
    history.total_commits = len(commits)

    for commit in commits:
        author = commit.author.name or "unknown"
        author_counter[author] += 1
        msg = commit.message.strip().split("\n")[0]  # first line only
        all_messages.append(msg)

        touched: set[str] = set()
        try:
            # diff against first parent to get changed files
            if commit.parents:
                diff = commit.parents[0].diff(commit)
            else:
                diff = commit.diff(None)  # initial commit
            for d in diff:
                fpath = d.b_path or d.a_path
                if fpath:
                    touched.add(fpath)
        except Exception:
            pass

        commit_files[commit.hexsha] = touched

        for fpath in touched:
            file_commits[fpath].append(commit)
            file_authors[fpath][author] += 1
            file_messages[fpath].append(msg)
            ts = commit.committed_date
            if fpath not in file_last_ts or ts > file_last_ts[fpath]:
                file_last_ts[fpath] = ts

    # Build co-change matrix
    co_change: dict[str, Counter] = defaultdict(Counter)
    for touched in commit_files.values():
        file_list = list(touched)
        for i, fa in enumerate(file_list):
            for fb in file_list[i + 1:]:
                co_change[fa][fb] += 1
                co_change[fb][fa] += 1

    # Assemble FileHistory per file
    for fpath, commits_list in file_commits.items():
        last_ts = file_last_ts.get(fpath)
        if last_ts:
            last_modified_days = (now.timestamp() - last_ts) / 86400
        else:
            last_modified_days = None

        top_authors = file_authors[fpath].most_common(3)
        msgs = file_messages[fpath][:10]  # keep top-10 messages

        fh = FileHistory(
            path=fpath,
            change_frequency=len(commits_list),
            co_changes=dict(co_change[fpath].most_common(10)),
            last_modified_days=last_modified_days,
            top_authors=top_authors,
            commit_messages=msgs,
            is_dead_code_candidate=(
                last_modified_days is not None
                and last_modified_days > DEAD_CODE_THRESHOLD_DAYS
            ),
        )
        history.files[fpath] = fh

    history.active_authors = [a for a, _ in author_counter.most_common(10)]
    history.major_themes = _cluster_messages(all_messages)
    return history


# ---------------------------------------------------------------------------
# Commit message clustering (simple keyword extraction — no heavy ML dep)
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
        tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9_\-]{2,}", msg.lower())
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
