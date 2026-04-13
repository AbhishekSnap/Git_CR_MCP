"""
Commit Analyser Agent
======================
Orchestrates the full commit-analysis pipeline:

  1. Spawns server.py as a subprocess (MCP stdio transport)
  2. Calls get_commit, get_commit_diff, get_commit_stats via MCP
  3. Sends all data to Claude (claude-sonnet-4-5-20250929) for structured analysis
  4. Formats the result as a markdown Wiki entry
  5. Appends the entry to GitHub Wiki Home.md via git clone → commit → push

All errors are caught and logged — this script never exits non-zero so that
a failed analysis cannot break the upstream commit push.
"""

import asyncio
import json
import logging
import os
import sys
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
    format="[analyser] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ── Environment variables (injected by GitHub Actions) ────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GITHUB_TOKEN      = os.environ.get("GITHUB_TOKEN", "")
COMMIT_SHA        = os.environ.get("COMMIT_SHA", "")
COMMIT_MESSAGE    = os.environ.get("COMMIT_MESSAGE", "")
COMMIT_AUTHOR     = os.environ.get("COMMIT_AUTHOR", "")
COMMIT_TIMESTAMP  = os.environ.get("COMMIT_TIMESTAMP", "")
BRANCH_NAME       = os.environ.get("BRANCH_NAME", "")
REPO_NAME         = os.environ.get("REPO_NAME", "")   # "owner/repo"

# Derive owner and repo from REPO_NAME
if "/" in REPO_NAME:
    OWNER, REPO = REPO_NAME.split("/", 1)
else:
    OWNER, REPO = "", REPO_NAME

# Maximum diff characters to send to Claude (avoids huge token costs)
MAX_DIFF_CHARS = 6_000

# Claude model
CLAUDE_MODEL = "claude-sonnet-4-5-20250929"


# ─────────────────────────────────────────────────────────────────────────────
# MCP tool call helper
# ─────────────────────────────────────────────────────────────────────────────

async def call_tool(session: ClientSession, tool_name: str, **kwargs) -> str:
    """
    Call an MCP tool and return its text output as a string.
    FastMCP serialises dict/str return values as JSON text inside a text block.
    """
    log.info("Calling MCP tool: %s", tool_name)
    result = await session.call_tool(tool_name, arguments=kwargs)
    parts = [block.text for block in result.content if hasattr(block, "text")]
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Claude analysis
# ─────────────────────────────────────────────────────────────────────────────

def analyse_with_claude(commit_raw: str, diff_raw: str, stats_raw: str) -> dict:
    """
    Send commit data to Claude and return a structured analysis dict with keys:
      plain_summary, technical_summary, change_type, quality, risk_level
    """
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    system_prompt = (
        "You are a senior software engineer reviewing a git commit.\n"
        "Analyse the provided commit data and return a JSON object with exactly "
        "these six keys:\n"
        '  "plain_summary"      : 2-3 sentence plain-English description of what '
        "changed (non-technical, suitable for a project manager)\n"
        '  "technical_summary"  : detailed per-file technical breakdown for a code '
        "reviewer who has the diff open. For EACH file changed, produce a section "
        "using this exact structure (use literal \\n for line breaks inside the "
        "JSON string): \"**filename.py**\\n- FunctionOrClass: what specifically "
        "changed — new parameters, altered logic, removed behaviour, etc.\\n- ...\""
        " Reference exact function names, class names, decorators, and constants. "
        "A reviewer should be able to open each file and navigate it using your "
        "description. Do not summarise vaguely — be precise about what each "
        "function does differently after this commit.\n"
        '  "change_type"        : exactly one of: Bug Fix | Feature | Refactor | '
        "Chore | Docs | Tests | Performance | Security\n"
        '  "quality"            : one-line commit quality assessment starting with '
        '"Good" or "Needs improvement", followed by an em-dash and a short reason '
        "(e.g. \"Good \u2014 clear message, focused change, tests included\")\n"
        '  "risk_level"         : one of: Low | Medium | High \u2014 followed by an '
        'em-dash and one-line reason (e.g. "Low \u2014 isolated utility module, '
        'no side effects on existing code")\n'
        '  "suggested_message"  : if the original commit message is clear, '
        "follows conventional commits format (type: description), and accurately "
        "reflects the change, return the original message unchanged. If it is "
        "vague, missing a type prefix, or inaccurate, return an improved message "
        "following the format: type(scope): concise description — where scope is "
        "optional. Examples: \"fix(parser): handle UK date format edge cases\", "
        "\"feat(auth): add JWT refresh token support\"\n\n"
        "Return ONLY valid JSON. No markdown fences, no extra text."
    )

    # Truncate diff to keep cost predictable
    diff_truncated = diff_raw[:MAX_DIFF_CHARS]
    if len(diff_raw) > MAX_DIFF_CHARS:
        diff_truncated += "\n... [diff truncated for brevity]"

    user_message = (
        f"COMMIT SHA: {COMMIT_SHA[:7] if COMMIT_SHA else 'unknown'}\n"
        f"BRANCH: {BRANCH_NAME}\n\n"
        f"COMMIT METADATA:\n{commit_raw[:1000]}\n\n"
        f"DIFF:\n{diff_truncated}\n\n"
        f"FILE STATS:\n{stats_raw}"
    )

    log.info("Sending commit data to Claude (%s)", CLAUDE_MODEL)
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2000,
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

def format_entry(commit_raw: str, stats_raw: str, analysis: dict) -> str:
    """
    Build the full markdown block for one commit, matching the required format:

    ---
    **a3f92c1** | 09:32 AM | Abhishek Kumar | `feature/branch`
    ...
    """
    commit = json.loads(commit_raw)
    stats  = json.loads(stats_raw)

    sha_short = commit.get("sha", COMMIT_SHA)[:7]
    author    = commit.get("commit", {}).get("author", {}).get("name", COMMIT_AUTHOR)
    iso_ts    = commit.get("commit", {}).get("author", {}).get("date", COMMIT_TIMESTAMP)

    # Format timestamp as 12-hour clock in UTC
    try:
        dt       = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        time_str = dt.astimezone(timezone.utc).strftime("%I:%M %p")
    except Exception:
        time_str = iso_ts

    # First line of the commit message only
    first_line = (commit.get("commit", {}).get("message", COMMIT_MESSAGE) or "").split("\n")[0]

    # Per-file lines
    files_md = "\n".join(
        f"- [{f['status'].capitalize()}] {f['filename']} "
        f"+{f['additions']} / -{f['deletions']}"
        for f in stats.get("files", [])
    ) or "- No file data available"

    total_add  = stats.get("stats", {}).get("additions", 0)
    total_del  = stats.get("stats", {}).get("deletions", 0)
    file_count = len(stats.get("files", []))

    commit_url = f"https://github.com/{OWNER}/{REPO}/commit/{commit['sha']}"

    # Only surface the suggested message if it differs from what was written
    suggested = analysis.get("suggested_message", "").strip()
    suggestion_line = (
        f"**Suggested message:** `{suggested}`\n\n"
        if suggested and suggested != first_line
        else ""
    )

    return (
        f"---\n"
        f"[**{sha_short}**]({commit_url}) | {time_str} | {author} | `{BRANCH_NAME}`\n\n"
        f"**Message:** {first_line}\n\n"
        f"{suggestion_line}"
        f"**What changed:** {analysis['plain_summary']}\n\n"
        f"**Technical Breakdown:**\n{analysis['technical_summary']}\n\n"
        f"**Type:** {analysis['change_type']} | "
        f"**Quality:** {analysis['quality']} | "
        f"**Risk:** {analysis['risk_level']}\n\n"
        f"**Files changed:**\n{files_md}\n\n"
        f"**Stats:** {file_count} files | +{total_add} lines | -{total_del} lines\n"
    )


# ─────────────────────────────────────────────────────────────────────────────
# GitHub Wiki update (git-based — the Contents API is not supported for wikis)
# ─────────────────────────────────────────────────────────────────────────────

def update_wiki(new_entry: str) -> None:
    """
    Write new_entry to a per-day wiki page: YYYY-MM-DD.md

    Strategy: clone the wiki git repo, create or append to today's page, push.
    Each calendar day (UTC) gets its own wiki page — the sidebar lists them
    as separate pages ordered by date.

    Page structure:
      # YYYY-MM-DD          ← H1 title (created once on first commit of the day)
      {entry}               ← newest entry at the top, each separated by ---
      {older entries}
    """
    import subprocess
    import tempfile

    today_date   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_file   = f"{today_date}.md"
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

        page_path = os.path.join(tmpdir, today_file)

        # ── Read or initialise today's page ───────────────────────────────────
        if os.path.exists(page_path):
            with open(page_path, "r", encoding="utf-8") as f:
                current_content = f.read()
            log.info("Appending to existing wiki page: %s", today_file)
        else:
            # First commit of the day — create the page with an H1 title
            current_content = f"# {today_date}\n"
            log.info("Creating new wiki page: %s", today_file)

        # ── Insert new entry at the top (after H1), newest commits first ───────
        if current_content.startswith("# "):
            first_newline   = current_content.index("\n")
            updated_content = (
                current_content[: first_newline + 1]
                + "\n"
                + new_entry
                + current_content[first_newline + 1 :]
            )
        else:
            updated_content = new_entry + current_content

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
            ["git", "add", today_file],
            cwd=tmpdir, check=True, capture_output=True, env=env,
        )
        subprocess.run(
            ["git", "commit", "-m", f"docs: add commit analysis for {COMMIT_SHA[:7]}"],
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
    """Spawn the MCP server, collect commit data, analyse, and update wiki."""
    server_script = Path(__file__).parent / "server.py"
    server_params = StdioServerParameters(
        command=sys.executable,       # same Python interpreter running this script
        args=[str(server_script)],
        env=dict(os.environ),         # forward full env so GITHUB_TOKEN reaches server
    )

    async with AsyncExitStack() as stack:
        # stdio_client returns (read_stream, write_stream)
        transport = await stack.enter_async_context(stdio_client(server_params))
        read_stream, write_stream = transport

        # ClientSession manages the JSON-RPC lifecycle
        session = await stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await session.initialize()
        log.info("MCP session initialised — server ready")

        # ── Step 1: Collect data via MCP tools ────────────────────────────────
        log.info("Fetching commit metadata (%s)", COMMIT_SHA[:7])
        commit_raw = await call_tool(
            session, "get_commit", owner=OWNER, repo=REPO, sha=COMMIT_SHA
        )

        log.info("Fetching commit diff")
        diff_raw = await call_tool(
            session, "get_commit_diff", owner=OWNER, repo=REPO, sha=COMMIT_SHA
        )

        log.info("Fetching commit stats")
        stats_raw = await call_tool(
            session, "get_commit_stats", owner=OWNER, repo=REPO, sha=COMMIT_SHA
        )

    # MCP session closed — server subprocess has exited

    # ── Step 2: Claude analysis ───────────────────────────────────────────────
    analysis = analyse_with_claude(commit_raw, diff_raw, stats_raw)
    log.info("Claude analysis: type=%s, quality=%s",
             analysis.get("change_type"), analysis.get("quality", "")[:30])

    # ── Step 3: Format markdown entry ─────────────────────────────────────────
    entry = format_entry(commit_raw, stats_raw, analysis)
    log.info("Formatted wiki entry (%d chars)", len(entry))

    # ── Step 4: Update wiki ───────────────────────────────────────────────────
    update_wiki(entry)
    log.info("Wiki updated successfully for commit %s", COMMIT_SHA[:7])


async def main() -> None:
    """Entry point — wraps run_analysis() so errors never break the push."""
    try:
        # Basic sanity checks
        if not COMMIT_SHA:
            log.error("COMMIT_SHA not set — skipping analysis")
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
        # Log full traceback but exit 0 so the workflow step stays green
        log.error("Commit analysis failed (non-blocking): %s", exc, exc_info=True)
        sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
