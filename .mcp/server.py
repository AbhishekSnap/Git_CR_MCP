"""
MCP Server: github-commit-analyser
===================================
Exposes three tools over stdio transport (JSON-RPC 2.0):
  - get_commit       : commit metadata (SHA, message, author, timestamp)
  - get_commit_diff  : raw unified diff for a commit
  - get_commit_stats : per-file stats (additions, deletions, status)

All GitHub API calls use requests + GITHUB_TOKEN from env.
stdout is reserved for JSON-RPC messages — all logging goes to stderr.
"""

import logging
import os
import sys

import requests
from mcp.server.fastmcp import FastMCP

# ── Logging (stderr only — stdout is owned by the MCP transport) ─────────────
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[server] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ── GitHub API config ─────────────────────────────────────────────────────────
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
API_BASE = "https://api.github.com"

HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# ── FastMCP server ────────────────────────────────────────────────────────────
mcp = FastMCP("github-commit-analyser")


def gh_get(url: str) -> dict:
    """GET a GitHub API endpoint and return parsed JSON. Raises on HTTP error."""
    log.info("GET %s", url)
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.json()


# ── Tool 1: get_commit ────────────────────────────────────────────────────────
@mcp.tool()
def get_commit(owner: str, repo: str, sha: str) -> dict:
    """
    Fetch commit metadata from GitHub.

    Returns the full commit object including:
    - sha, html_url
    - commit.message, commit.author.name, commit.author.date
    - parents list
    """
    return gh_get(f"{API_BASE}/repos/{owner}/{repo}/commits/{sha}")


# ── Tool 2: get_commit_diff ───────────────────────────────────────────────────
@mcp.tool()
def get_commit_diff(owner: str, repo: str, sha: str) -> str:
    """
    Fetch the raw unified diff for a commit.

    Uses Accept: application/vnd.github.diff on the commits endpoint.
    Returns the diff as a plain string (not JSON).
    """
    url = f"{API_BASE}/repos/{owner}/{repo}/commits/{sha}"
    log.info("GET diff %s", url)
    resp = requests.get(
        url,
        headers={**HEADERS, "Accept": "application/vnd.github.diff"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.text


# ── Tool 3: get_commit_stats ──────────────────────────────────────────────────
@mcp.tool()
def get_commit_stats(owner: str, repo: str, sha: str) -> dict:
    """
    Fetch per-file change stats for a commit.

    Returns:
    {
      "stats": {"total": N, "additions": N, "deletions": N},
      "files": [
        {"filename": "...", "status": "modified|added|removed|renamed",
         "additions": N, "deletions": N, "changes": N},
        ...
      ]
    }
    """
    data = gh_get(f"{API_BASE}/repos/{owner}/{repo}/commits/{sha}")
    return {
        "stats": data.get("stats", {"total": 0, "additions": 0, "deletions": 0}),
        "files": [
            {
                "filename": f["filename"],
                "status": f["status"],
                "additions": f.get("additions", 0),
                "deletions": f.get("deletions", 0),
                "changes": f.get("changes", 0),
            }
            for f in data.get("files", [])
        ],
    }


# ── PR Tools ──────────────────────────────────────────────────────────────────

@mcp.tool()
def get_pull_request(owner: str, repo: str, pr_number: int) -> dict:
    """
    Fetch full PR metadata: title, body, state, merged, author, base/head branches,
    labels, milestone, created_at, merged_at, closed_at, additions, deletions,
    changed_files, review_comments, commits count.
    """
    return gh_get(f"{API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}")


@mcp.tool()
def get_pr_files(owner: str, repo: str, pr_number: int) -> dict:
    """
    List all files changed in the PR, including the patch diff for each file.
    Returns: {"files": [{"filename", "status", "additions", "deletions", "changes", "patch"}, ...]}
    patch is capped at 1500 chars per file to keep token usage predictable.
    """
    data = gh_get(f"{API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}/files")
    return {
        "files": [
            {
                "filename": f["filename"],
                "status": f["status"],
                "additions": f.get("additions", 0),
                "deletions": f.get("deletions", 0),
                "changes": f.get("changes", 0),
                "patch": (f.get("patch") or "")[:1500],
            }
            for f in data
        ]
    }


@mcp.tool()
def get_pr_commits(owner: str, repo: str, pr_number: int) -> dict:
    """
    List all commits in the PR (up to 250 via GitHub default pagination).
    Returns: {"commits": [{"sha", "message", "author", "date"}, ...]}
    """
    data = gh_get(f"{API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}/commits")
    return {
        "commits": [
            {
                "sha": c["sha"][:7],
                "message": c["commit"]["message"].split("\n")[0],
                "author": c["commit"]["author"]["name"],
                "date": c["commit"]["author"]["date"],
            }
            for c in data
        ]
    }


@mcp.tool()
def get_pr_reviews(owner: str, repo: str, pr_number: int) -> dict:
    """
    List all submitted reviews on the PR.
    Returns: {"reviews": [{"reviewer", "state", "submitted_at", "body"}, ...]}
    State is one of: APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED.
    """
    data = gh_get(f"{API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}/reviews")
    return {
        "reviews": [
            {
                "reviewer": r["user"]["login"],
                "state": r["state"],
                "submitted_at": r["submitted_at"],
                "body": (r.get("body") or "")[:200],
            }
            for r in data
        ]
    }


@mcp.tool()
def get_pr_requested_reviewers(owner: str, repo: str, pr_number: int) -> dict:
    """
    List users and teams requested as reviewers but who have not yet reviewed.
    Returns: {"users": [...], "teams": [...]}
    """
    data = gh_get(f"{API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}/requested_reviewers")
    return {
        "users": [u["login"] for u in data.get("users", [])],
        "teams": [t["name"] for t in data.get("teams", [])],
    }


@mcp.tool()
def get_pr_checks(owner: str, repo: str, head_sha: str) -> dict:
    """
    List CI check runs for the PR's head commit.
    Deduplicates by check name — only the latest run per name is returned,
    so repeated workflow runs on the same commit do not inflate the count.
    Returns: {"checks": [{"name", "status", "conclusion", "started_at", "completed_at"}, ...]}
    """
    data = gh_get(f"{API_BASE}/repos/{owner}/{repo}/commits/{head_sha}/check-runs")
    # Keep only the latest run per check name (runs are returned newest-first by GitHub)
    seen: dict[str, dict] = {}
    for r in data.get("check_runs", []):
        name = r["name"]
        if name not in seen:
            seen[name] = {
                "name": name,
                "status": r["status"],
                "conclusion": r.get("conclusion", "pending"),
                "started_at": r.get("started_at", ""),
                "completed_at": r.get("completed_at", ""),
            }
    return {"checks": list(seen.values())}


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Starting MCP server via stdio transport")
    mcp.run(transport="stdio")
