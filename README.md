# commit-analyser-mcp

Automatic AI-powered commit analysis for every push — delivered straight to your GitHub Wiki.

## What it does

Every time a developer pushes a commit, this tool fetches the commit details from GitHub, sends them to Claude AI for analysis, and appends a structured, human-readable entry to the repository's Wiki. The result is a running development log that gives both technical and non-technical stakeholders a clear picture of what changed and why — with zero manual effort from the team.

---

## How it works

```
Push to GitHub
      │
      ▼
GitHub Actions (your repo's commit-analyser.yml)
      │
      │  calls (workflow_call)
      ▼
commit-analyser-mcp / .github/workflows/main.yml
      │
      │  python .mcp/analyser.py
      ▼
analyser.py ──spawns subprocess──► server.py  (MCP Server, stdio transport)
      │                                │
      │   session.call_tool()          │  requests → api.github.com
      │   • get_commit                 │    GET /repos/{owner}/{repo}/commits/{sha}
      │   • get_commit_diff            │    GET /repos/{owner}/{repo}/commits/{sha}
      │   • get_commit_stats           │    (Accept: application/vnd.github.diff)
      │◄───────────────────────────────┘
      │
      │  Anthropic SDK → claude-sonnet-4-5
      │    Plain English summary
      │    Technical summary
      │    Type of change
      │    Commit quality
      ▼
analyser.py
      │
      │  requests → api.github.com
      │    GET  /repos/{owner}/{repo}.wiki/contents/Home.md
      │    PUT  /repos/{owner}/{repo}.wiki/contents/Home.md
      ▼
GitHub Wiki (Home.md updated with new entry)
```

---

## Setup — 4 steps

### Step 1 — Enable GitHub Wiki on your repository

Go to your repository's **Settings → Features** and tick the **Wikis** checkbox.

Then click **Wiki** in the top navigation and create the first page manually (title "Home", any content). This initialises the wiki's git repository so the bot can write to it on subsequent runs.

### Step 2 — Add your Anthropic API key as a secret

Go to **Settings → Secrets and variables → Actions → New repository secret**.

| Name | Value |
|------|-------|
| `ANTHROPIC_API_KEY` | Your key from [console.anthropic.com](https://console.anthropic.com) |

### Step 3 — Copy the consumer workflow into your repository

Copy [`examples/consumer-workflow.yml`](examples/consumer-workflow.yml) into your repository at:

```
.github/workflows/commit-analyser.yml
```

Open the file and replace `YOUR_ORG` with the GitHub organisation or username where this `commit-analyser-mcp` repository lives:

```yaml
jobs:
  analyse:
    uses: YOUR_ORG/commit-analyser-mcp/.github/workflows/main.yml@main
```

Also ensure your Actions workflow permissions allow write access:
**Settings → Actions → General → Workflow permissions → Read and write**.

### Step 4 — Push a commit and check the Wiki

Push any commit to your repository. Within ~30 seconds you should see a new entry appear on your Wiki's **Home** page.

---

## Wiki entry format

Each commit produces one entry like this:

```
---
**a3f92c1** | 09:32 AM | Abhishek Kumar | `feature/contract-extraction`

**Message:** fix: incorrect date parsing for UK format contracts

**What changed:** Fixed a bug where dates in DD/MM/YYYY format were being read
incorrectly, causing wrong contract expiry dates.

**Technical:** Updated regex pattern in extractor.py and added dateutil fallback
parser. Added 3 new test cases covering edge cases.

**Type:** Bug Fix | **Quality:** Good — clear message, focused change, tests included

**Files changed:**
- [Modified] src/extractor.py +24 / -8
- [Modified] tests/test_extractor.py +31 / -0

**Stats:** 2 files | +55 lines | -8 lines
```

Entries are grouped under date headings (`## YYYY-MM-DD`). New entries are inserted at the top of each day's section so the most recent work is always easy to find.

---

## Cost estimate

| Item | Estimate |
|------|----------|
| Input tokens per commit | ~1,500 – 3,000 |
| Output tokens per commit | ~200 – 400 |
| Cost per commit (Claude Sonnet 4.5) | ~$0.006 – $0.015 |
| Cost for 50 commits/day | ~$0.30 – $0.75/day |

Analysis costs fractions of a penny per commit. For most teams the monthly spend is under $5.

---

## Repository structure

```
commit-analyser-mcp/
├── .github/
│   └── workflows/
│       └── main.yml              ← reusable workflow (called by consumer repos)
├── .mcp/
│   ├── server.py                 ← MCP server — exposes GitHub tools over stdio
│   ├── analyser.py               ← agent — orchestrates MCP + Claude + Wiki update
│   └── requirements.txt          ← anthropic, requests, mcp
├── examples/
│   └── consumer-workflow.yml     ← template for teams to copy into their repo
└── README.md
```

---

## Troubleshooting

**Wiki not updating?**
- Check that Wikis are enabled and the first page was created manually.
- Verify that workflow permissions are set to "Read and write".
- Check the Actions log — the analyser logs to stderr and always exits 0, so look for `[analyser] ERROR` lines.

**`ANTHROPIC_API_KEY` errors?**
- Confirm the secret is set in **Settings → Secrets → Actions** (not environment variables).
- For `workflow_call`, secrets must be explicitly passed — the consumer workflow already does this.

**`404` on Wiki GET?**
- The wiki git repo doesn't exist yet. Go to the Wiki tab and create the first page manually.

**`409` on Wiki PUT?**
- Two commits pushed in rapid succession. The analyser retries once automatically. A second 409 is logged but does not fail the workflow.
