"""
PR Analyser Agent
==================
Orchestrates the full PR-analysis pipeline:

  1. Spawns server.py as a subprocess (MCP stdio transport)
  2. Calls get_pull_request, get_pr_files, get_pr_commits, get_pr_reviews,
     get_pr_requested_reviewers, get_pr_checks via MCP
  3. Sends all data to Claude (claude-sonnet-4-5-20250929) for structured analysis
  4. Formats the result as a markdown entry based on the PR action
  5. Appends the entry to the GitHub Wiki PR-Analyse.md via git clone → commit → push

All errors are caught and logged — this script never exits non-zero so that
a failed analysis cannot break the upstream PR event workflow.
"""

import asyncio
import json
import logging
import os
import sys
import time
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path

from anthropic import Anthropic
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[pr-analyser] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ── Environment variables (injected by GitHub Actions) ────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GITHUB_TOKEN      = os.environ.get("GITHUB_TOKEN", "")
REPO_NAME         = os.environ.get("REPO_NAME", "")
PR_NUMBER         = os.environ.get("PR_NUMBER", "")
PR_ACTION         = os.environ.get("PR_ACTION", "")
PR_TITLE          = os.environ.get("PR_TITLE", "")
PR_AUTHOR         = os.environ.get("PR_AUTHOR", "")
PR_BASE_BRANCH    = os.environ.get("PR_BASE_BRANCH", "")
PR_HEAD_BRANCH    = os.environ.get("PR_HEAD_BRANCH", "")

# Derive owner and repo from REPO_NAME
if "/" in REPO_NAME:
    OWNER, REPO = REPO_NAME.split("/", 1)
else:
    OWNER, REPO = "", REPO_NAME

# Claude model
CLAUDE_MODEL = "claude-sonnet-4-5-20250929"

# Badge map — closed action is split by merged field at runtime
BADGES = {
    "opened":   "🟡 OPENED",
    "merged":   "🟢 MERGED",
    "closed":   "🔴 CLOSED",
    "reopened": "🔄 REOPENED",
}


# ─────────────────────────────────────────────────────────────────────────────
# MCP tool call helper
# ─────────────────────────────────────────────────────────────────────────────

async def call_tool(session: ClientSession, tool_name: str, **kwargs) -> str:
    """
    Call an MCP tool and return its text output as a string.
    FastMCP serialises dict/list return values as JSON text inside a text block.
    """
    log.info("Calling MCP tool: %s", tool_name)
    result = await session.call_tool(tool_name, arguments=kwargs)
    parts = [block.text for block in result.content if hasattr(block, "text")]
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Claude analysis
# ─────────────────────────────────────────────────────────────────────────────

def analyse_pr_with_claude(
    pr_raw: str,
    files_raw: str,
    commits_raw: str,
    reviews_raw: str,
    reviewers_raw: str,
    checks_raw: str,
) -> dict:
    """
    Send PR data to Claude and return a structured analysis dict with keys:
      summary, technical_impact, review_sentiment, quality
    """
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    system_prompt = (
        "You are a senior software engineer reviewing a GitHub pull request.\n"
        "Analyse the provided PR data and return a JSON object with exactly "
        "these four keys:\n"
        '  "summary"          : 2-3 sentence plain-English description of what '
        "this PR does and why, suitable for a non-technical stakeholder.\n"
        '  "technical_impact" : per-file breakdown for a code reviewer. For EACH '
        "file changed, write a section using this exact structure (use literal \\n "
        "for line breaks inside the JSON string): \"**filename.py**\\n- "
        "FunctionOrClass: what specifically changed — new parameters, altered "
        "logic, removed behaviour, etc.\" Reference exact function names, class "
        "names, and constants. Be precise about what each file does differently "
        "after this PR.\n"
        '  "review_sentiment" : one of: Approved | Changes Requested | Mixed | '
        "No Reviews Yet — followed by an em-dash and one-line summary of the "
        "reviewer feedback (or \"no reviews submitted yet\" if empty).\n"
        '  "quality"          : one-line assessment starting with "Good" or '
        '"Needs improvement", followed by an em-dash and a short reason covering '
        "commit hygiene, PR size, and description quality.\n\n"
        "Return ONLY valid JSON. No markdown fences, no extra text."
    )

    # Build compact user message from raw data
    try:
        pr = json.loads(pr_raw)
        pr_body = (pr.get("body") or "No description provided.")[:800]
    except (json.JSONDecodeError, AttributeError):
        pr_body = "No description provided."

    try:
        files = json.loads(files_raw)[:50]  # cap at 50 files
        files_lines = "\n".join(
            f"{f['filename']} | {f['status']} | +{f['additions']} -{f['deletions']}"
            for f in files
        )
    except (json.JSONDecodeError, TypeError):
        files_lines = "No file data available."

    try:
        commits = json.loads(commits_raw)[:30]  # cap at 30 commits
        commits_lines = "\n".join(
            f"{c['sha']} | {c['message']} | {c['author']}"
            for c in commits
        )
    except (json.JSONDecodeError, TypeError):
        commits_lines = "No commit data available."

    try:
        reviews = json.loads(reviews_raw)
        reviews_lines = "\n".join(
            f"{r['reviewer']} | {r['state']} | {r['body']}"
            for r in reviews
        ) or "No reviews yet."
    except (json.JSONDecodeError, TypeError):
        reviews_lines = "No reviews yet."

    try:
        reviewers = json.loads(reviewers_raw)
        pending = reviewers.get("users", []) + reviewers.get("teams", [])
        reviewers_line = ", ".join(pending) if pending else "None"
    except (json.JSONDecodeError, TypeError):
        reviewers_line = "None"

    try:
        checks = json.loads(checks_raw)
        checks_lines = "\n".join(
            f"{c['name']} | {c['status']} | {c['conclusion']}"
            for c in checks
        ) or "No CI checks found."
    except (json.JSONDecodeError, TypeError):
        checks_lines = "No CI checks found."

    user_message = (
        f"PR #{PR_NUMBER}: {PR_TITLE}\n"
        f"Author: {PR_AUTHOR}  |  {PR_BASE_BRANCH} ← {PR_HEAD_BRANCH}\n"
        f"Action: {PR_ACTION}\n\n"
        f"PR BODY (first 800 chars):\n{pr_body}\n\n"
        f"COMMITS ({len(commits if 'commits' in dir() else [])} shown):\n{commits_lines}\n\n"
        f"FILES CHANGED:\n{files_lines}\n\n"
        f"REVIEWS:\n{reviews_lines}\n\n"
        f"REQUESTED REVIEWERS: {reviewers_line}\n\n"
        f"CI CHECKS:\n{checks_lines}"
    )

    log.info("Sending PR data to Claude (%s)", CLAUDE_MODEL)
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1500,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )

    raw_text = response.content[0].text.strip()

    # Defensively strip markdown fences if Claude includes them
    if raw_text.startswith("```"):
        lines = raw_text.split("\n")
        raw_text = "\n".join(lines[1:-1]).strip()

    return json.loads(raw_text)


# ─────────────────────────────────────────────────────────────────────────────
# Markdown entry formatter
# ─────────────────────────────────────────────────────────────────────────────

def format_pr_entry(
    pr_raw: str,
    files_raw: str,
    commits_raw: str,
    checks_raw: str,
    analysis: dict,
) -> str:
    """
    Build the full markdown block for one PR event.
    """
    try:
        pr = json.loads(pr_raw)
    except json.JSONDecodeError:
        pr = {}

    try:
        files = json.loads(files_raw)[:50]
    except (json.JSONDecodeError, TypeError):
        files = []

    try:
        commits = json.loads(commits_raw)[:30]
    except (json.JSONDecodeError, TypeError):
        commits = []

    try:
        checks = json.loads(checks_raw)
    except (json.JSONDecodeError, TypeError):
        checks = []

    # Determine badge — closed action splits on merged field
    action_key = PR_ACTION
    if PR_ACTION == "closed" and pr.get("merged"):
        action_key = "merged"
    badge = BADGES.get(action_key, PR_ACTION.upper())

    # Timestamp
    event_ts = pr.get("updated_at") or pr.get("created_at") or ""
    try:
        dt = datetime.fromisoformat(event_ts.replace("Z", "+00:00"))
        time_str = dt.astimezone(timezone.utc).strftime("%I:%M %p")
    except Exception:
        time_str = event_ts

    pr_url = pr.get("html_url", f"https://github.com/{OWNER}/{REPO}/pull/{PR_NUMBER}")

    # File stats
    total_add = sum(f.get("additions", 0) for f in files)
    total_del = sum(f.get("deletions", 0) for f in files)
    files_md = "\n".join(
        f"- [{f['status'].capitalize()}] {f['filename']} "
        f"+{f['additions']} / -{f['deletions']}"
        for f in files
    ) or "- No file data available"

    # Commits list
    commits_md = "\n".join(
        f"- `{c['sha']}` {c['message']} ({c['author']})"
        for c in commits
    ) or "- No commit data available"

    # CI checks summary
    if checks:
        n_passed = sum(1 for c in checks if c.get("conclusion") == "success")
        n_total = len(checks)
        failing = [c["name"] for c in checks if c.get("conclusion") not in ("success", "skipped", "neutral", "pending")]
        ci_line = f"{n_passed}/{n_total} checks passed"
        if failing:
            ci_line += " — failed: " + ", ".join(failing)
    else:
        ci_line = "No CI checks found"

    # Action-specific extra line
    extra_line = ""
    if action_key == "merged":
        merged_at = pr.get("merged_at", "")
        try:
            dt_m = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
            merged_str = dt_m.astimezone(timezone.utc).strftime("%Y-%m-%d %I:%M %p UTC")
        except Exception:
            merged_str = merged_at
        extra_line = f"**Merged at:** {merged_str}\n\n"
    elif PR_ACTION == "closed" and not pr.get("merged"):
        extra_line = "**Closed without merging.**\n\n"

    return (
        f"---\n"
        f"[**PR #{PR_NUMBER}**]({pr_url}) — {badge} | {time_str} UTC | "
        f"{PR_AUTHOR} | `{PR_HEAD_BRANCH}` → `{PR_BASE_BRANCH}`\n\n"
        f"**Title:** {PR_TITLE}\n\n"
        f"{extra_line}"
        f"**Summary:** {analysis['summary']}\n\n"
        f"**Technical Impact:**\n{analysis['technical_impact']}\n\n"
        f"**Reviews:** {analysis['review_sentiment']}\n\n"
        f"**Quality:** {analysis['quality']}\n\n"
        f"**CI:** {ci_line}\n\n"
        f"**Files changed ({len(files)} files | +{total_add} / -{total_del}):**\n"
        f"{files_md}\n\n"
        f"**Commits ({len(commits)}):**\n"
        f"{commits_md}\n"
    )


# ─────────────────────────────────────────────────────────────────────────────
# GitHub Wiki update (git-based)
# ─────────────────────────────────────────────────────────────────────────────

def update_pr_wiki(new_entry: str) -> None:
    """
    Write new_entry to PR-Analyse.md in the wiki git repo.

    Page structure:
      # PR Analysis Log      ← H1 title (created once on first ever PR event)
      ## YYYY-MM-DD          ← H2 per day (created on first PR event of that day)
      {newest entry}
      {older entries}

    New entries are inserted at the top of today's section (newest first).
    """
    import subprocess
    import tempfile

    today_date   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    target_file  = "PR-Analyse.md"
    wiki_git_url = (
        f"https://x-access-token:{GITHUB_TOKEN}@github.com/{OWNER}/{REPO}.wiki.git"
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        # ── Clone ─────────────────────────────────────────────────────────────
        log.info("Cloning wiki repo: github.com/%s/%s.wiki", OWNER, REPO)
        subprocess.run(
            ["git", "clone", "--depth", "1", wiki_git_url, tmpdir],
            check=True, capture_output=True, text=True,
        )

        page_path = os.path.join(tmpdir, target_file)

        # ── Read or initialise ─────────────────────────────────────────────────
        if os.path.exists(page_path):
            with open(page_path, "r", encoding="utf-8") as f:
                current_content = f.read()
            log.info("Appending to existing wiki page: %s", target_file)
        else:
            current_content = "# PR Analysis Log\n"
            log.info("Creating new wiki page: %s", target_file)

        today_heading = f"## {today_date}"

        if today_heading in current_content:
            # Insert new entry right after today's heading line
            idx = current_content.index(today_heading)
            end_of_heading = current_content.index("\n", idx) + 1
            updated_content = (
                current_content[:end_of_heading]
                + "\n"
                + new_entry
                + current_content[end_of_heading:]
            )
        else:
            # Prepend today's heading after the H1 title line
            if current_content.startswith("# "):
                first_newline = current_content.index("\n")
                updated_content = (
                    current_content[: first_newline + 1]
                    + "\n"
                    + today_heading
                    + "\n\n"
                    + new_entry
                    + current_content[first_newline + 1:]
                )
            else:
                updated_content = today_heading + "\n\n" + new_entry + current_content

        # ── Write ──────────────────────────────────────────────────────────────
        with open(page_path, "w", encoding="utf-8") as f:
            f.write(updated_content)

        # ── Commit and push ────────────────────────────────────────────────────
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        subprocess.run(
            ["git", "config", "user.email", "bot@github-actions"],
            cwd=tmpdir, check=True, capture_output=True, env=env,
        )
        subprocess.run(
            ["git", "config", "user.name", "Commit Analyser Bot"],
            cwd=tmpdir, check=True, capture_output=True, env=env,
        )
        subprocess.run(
            ["git", "add", target_file],
            cwd=tmpdir, check=True, capture_output=True, env=env,
        )
        subprocess.run(
            ["git", "commit", "-m", f"docs: add PR #{PR_NUMBER} analysis ({PR_ACTION})"],
            cwd=tmpdir, check=True, capture_output=True, env=env,
        )
        subprocess.run(
            ["git", "push"],
            cwd=tmpdir, check=True, capture_output=True, env=env,
        )
        log.info("Wiki updated successfully via git push")


# ─────────────────────────────────────────────────────────────────────────────
# Main async pipeline
# ─────────────────────────────────────────────────────────────────────────────

async def run_analysis() -> None:
    """Spawn the MCP server, collect PR data, analyse, and update wiki."""
    server_script = Path(__file__).parent / "server.py"
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[str(server_script)],
        env=dict(os.environ),
    )

    async with AsyncExitStack() as stack:
        transport = await stack.enter_async_context(stdio_client(server_params))
        read_stream, write_stream = transport

        session = await stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await session.initialize()
        log.info("MCP session initialised — server ready")

        pr_num = int(PR_NUMBER)

        # Brief pause to allow GitHub API to reflect the latest PR state
        log.info("Waiting 10 seconds for GitHub API to settle...")
        time.sleep(10)

        # ── Step 1: Collect data via MCP tools ────────────────────────────────
        log.info("Fetching PR metadata (#%s)", PR_NUMBER)
        pr_raw = await call_tool(
            session, "get_pull_request", owner=OWNER, repo=REPO, pr_number=pr_num
        )

        # Extract head SHA for CI checks
        try:
            head_sha = json.loads(pr_raw)["head"]["sha"]
        except (json.JSONDecodeError, KeyError):
            head_sha = ""

        log.info("Fetching PR files")
        files_raw = await call_tool(
            session, "get_pr_files", owner=OWNER, repo=REPO, pr_number=pr_num
        )

        log.info("Fetching PR commits")
        commits_raw = await call_tool(
            session, "get_pr_commits", owner=OWNER, repo=REPO, pr_number=pr_num
        )

        log.info("Fetching PR reviews")
        reviews_raw = await call_tool(
            session, "get_pr_reviews", owner=OWNER, repo=REPO, pr_number=pr_num
        )

        log.info("Fetching PR requested reviewers")
        reviewers_raw = await call_tool(
            session, "get_pr_requested_reviewers", owner=OWNER, repo=REPO, pr_number=pr_num
        )

        log.info("Fetching CI checks for head SHA %s", head_sha[:7] if head_sha else "unknown")
        checks_raw = await call_tool(
            session, "get_pr_checks", owner=OWNER, repo=REPO, head_sha=head_sha
        ) if head_sha else "[]"

    # MCP session closed — server subprocess has exited

    # ── Step 2: Claude analysis ───────────────────────────────────────────────
    analysis = analyse_pr_with_claude(
        pr_raw, files_raw, commits_raw, reviews_raw, reviewers_raw, checks_raw
    )
    log.info("Claude analysis: sentiment=%s, quality=%s",
             analysis.get("review_sentiment", "")[:30],
             analysis.get("quality", "")[:30])

    # ── Step 3: Format markdown entry ─────────────────────────────────────────
    entry = format_pr_entry(pr_raw, files_raw, commits_raw, checks_raw, analysis)
    log.info("Formatted PR wiki entry (%d chars)", len(entry))

    # ── Step 4: Update wiki ───────────────────────────────────────────────────
    update_pr_wiki(entry)
    log.info("Wiki updated successfully for PR #%s (%s)", PR_NUMBER, PR_ACTION)


async def main() -> None:
    """Entry point — wraps run_analysis() so errors never break the workflow."""
    try:
        if not PR_NUMBER:
            log.error("PR_NUMBER not set — skipping analysis")
            return
        if not ANTHROPIC_API_KEY:
            log.error("ANTHROPIC_API_KEY not set — skipping analysis")
            return
        if not GITHUB_TOKEN:
            log.error("GITHUB_TOKEN not set — skipping analysis")
            return
        if not OWNER or not REPO:
            log.error("REPO_NAME must be in 'owner/repo' format — got: %s", REPO_NAME)
            return

        await run_analysis()

    except Exception as exc:
        log.error("PR analysis failed (non-blocking): %s", exc, exc_info=True)
        sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
