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


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Starting MCP server via stdio transport")
    mcp.run(transport="stdio")
