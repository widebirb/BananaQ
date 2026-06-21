# BananaQ 🍌

A GitHub pull request reviewer and changelog agent. BananaQ listens for PR webhook events, triages them through an orchestrator, and dispatches specialized agents which posts line-level code review comments and automatically appending changelog entries to `README.md` when PRs are merged.

---

## Table of Contents

- [How It Works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [GitHub Setup](#github-setup)
- [LLM Backend Setup](#llm-backend-setup)
  - [Option A: Local Hardware (Ollama on your machine)](#option-a--local-hardware-ollama-on-your-machine)
  - [Option B: Rented GPU (Vast.ai + Ollama via SSH tunnel)](#option-b--rented-gpu-vastai--ollama-via-ssh-tunnel)
  - [Option C: Managed Cloud API (OpenAI, Anthropic, etc.)](#option-c--managed-cloud-api-openai-anthropic-etc)
- [Configuration Reference](#configuration-reference)
- [Running BananaQ](#running-bananaq)
- [Exposing BananaQ to the Internet](#exposing-bananaq-to-the-internet)
- [Customizing Behavior](#customizing-behavior)
- [Running Tests](#running-tests)

---

## How It Works

BananaQ handles pull request events through a three-agent pipeline:

```
GitHub Webhook (PR event)
    │
    ▼
POST /webhook  (FastAPI: HMAC-SHA256 verified)
    │
    ▼
Orchestrator Dispatcher
    ├─ opened / synchronize / reopened
    │    ├─ Docs/config-only diff → skip (no LLM call)
    │    └─ Code changes → LLM triage
    │           ├─ skip → posts skip notice to PR
    │           └─ review → Reviewer Agent
    │                  → line-level comments + code suggestions posted to PR
    │
    └─ closed + merged (deterministic, no LLM call)
           └─ Changelog Agent
                  → LLM-generated one-line summary
                  → prepended to ## Changelog in README.md on main
```

**Agents:**

| Agent | Input | Output |
|---|---|---|
| **Orchestrator** | PR event + diff | `review` / `skip` / `changelog` routing decision |
| **Reviewer** (`agents/reviewer.py`) | Unified diff | Line-level review comments with code suggestions |
| **Changelog Updater** (`agents/changelog_updater.py`) | PR metadata | One-line summary committed to `README.md` |

**Key properties:**
- Merged PRs always route to the changelog pipeline **no LLM call for routing**
- Docs/config-only diffs are skipped deterministically **no LLM call for triage**
- Code suggestions use [GitHub's suggestion syntax](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/reviewing-changes-in-pull-requests/incorporating-feedback-in-your-pull-request) for one-click commits
- Changelog commits use `[skip ci]` to prevent CI run loops

---

## Requirements

- Python 3.11+
- A GitHub repository with webhook support
- A GitHub Personal Access Token (PAT) with `repo` scope or fine-grained `pull_requests: write` + `contents: write`
- [ngrok](https://ngrok.com/) (or any tunnel) to expose your local server to GitHub webhooks
- One of the LLM backends described below

---

## Installation

```bash
# 1. Clone the repo
git clone https://github.com/your-username/BananaQ.git
cd BananaQ

# 2. Create a virtual environment
python -m venv .venv

# Activate it:
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Copy the example env file and fill it in
cp .env.example .env
```

---

## GitHub Setup

### 1. Create a GitHub Personal Access Token (PAT)

Go to **GitHub → Settings → Developer Settings → Personal Access Tokens**.

**Classic token** - select the `repo` scope (covers everything needed).

**Fine-grained token** - select these permissions on your target repository:
- `Pull requests` - Read and write
- `Contents` - Read and write (required for the changelog agent to commit to `README.md`)
- `Metadata` - Read-only (selected automatically)

Paste the token into `GITHUB_TOKEN` in your `.env` file.

### 2. Create a Webhook

In your GitHub repository: **Settings → Webhooks → Add webhook**

| Field | Value |
|---|---|
| Payload URL | Your ngrok URL + `/webhook`, e.g. `https://abc123.ngrok-free.app/webhook` |
| Content type | `application/json` |
| Secret | A random string then paste the same value into `GITHUB_WEBHOOK_SECRET` in `.env` |
| Events | Select **Let me select individual events** → tick **Pull requests** only |

> You will need to update the Payload URL each time you restart ngrok unless you have a paid ngrok plan with a static domain.

---

## LLM Backend Setup

BananaQ uses the OpenAI-compatible chat completions API (`/v1/chat/completions`). Any backend that speaks this protocol works - Ollama, vLLM, OpenAI, Anthropic via a proxy, etc.

Two `.env` settings control the backend:

```env
OLLAMA_BASE_URL=http://localhost:11434   # Base URL of your LLM server
OLLAMA_LLM_MODEL=qwen2.5-coder:7b       # Model name to pass in the API call
```

Choose the option that matches your setup below.

---

### Option A: Local Hardware (Ollama on your machine)

**Best for:** Developers with a modern GPU (8 GB+ VRAM) who want fully offline operation.

#### Minimum hardware by model

| Model | Min VRAM | Notes |
|---|---|---|
| `qwen2.5-coder:1.5b` | ~2 GB | Fast, basic quality and good for testing |
| `qwen2.5-coder:7b` | ~6 GB | Strong coding quality, recommended baseline |
| `qwen2.5-coder:14b` | ~10 GB | High quality, fits on 12 GB cards with quantization |
| `qwen2.5-coder:32b` | ~20 GB | Best quality, requires 24 GB+ VRAM |
| `deepseek-coder-v2:16b` | ~12 GB | Alternative with strong review quality |

#### Steps

**1. Install Ollama**

Download from [ollama.com](https://ollama.com/) and install. On macOS/Linux it starts as a background service automatically. On Windows, launch **Ollama** from the Start menu.

**2. Pull a model**

```bash
ollama pull qwen2.5-coder:7b
```

Verify it's available:

```bash
ollama list
```

**3. Configure `.env`**

```env
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_LLM_MODEL=qwen2.5-coder:7b
LLM_API_KEY=ollama
```

> `LLM_API_KEY` is required by the OpenAI SDK but ignored by Ollama — set it to anything.

That's it. BananaQ talks to Ollama on `localhost` with no tunneling needed.

---

### Option B: Rented GPU (Vast.ai + Ollama via SSH tunnel)

**Best for:** Developers without a GPU who want to run capable open-source models (14b–35b) on-demand at low cost. You only pay while the instance is running.

#### Steps

**1. Set up your SSH key (first time only)**

```bash
ssh-keygen -t ed25519
```

Press Enter through all prompts. Copy the public key:

```bash
# macOS / Linux
cat ~/.ssh/id_ed25519.pub

# Windows (PowerShell)
Get-Content $env:USERPROFILE\.ssh\id_ed25519.pub
```

Paste it into [Vast.ai Account → SSH Keys](https://cloud.vast.ai/account/).

**2. Rent a GPU instance on Vast.ai**

1. Go to [vast.ai/console](https://cloud.vast.ai/) and log in.
2. Under **Templates**, search for `vastai/ollama` and select it.
3. Filter by GPU. Recommended configurations:

   | Budget | GPU | VRAM | Suitable model |
   |---|---|---|---|
   | ~$0.20–0.40/hr | RTX 3080 | 10 GB | `qwen2.5-coder:7b` |
   | ~$0.30–0.60/hr | RTX 3090 / 4090 | 24 GB | `qwen2.5-coder:14b`, `qwen3:14b` |
   | ~$0.50–1.00/hr | A100 / H100 | 40–80 GB | `qwen2.5-coder:32b` |

4. Allocate at least **30 GB disk** (the model weights need room).
5. Launch the instance. Ollama starts automatically inside the container.

**3. Connect via SSH and pull a model**

Get the SSH command from the instance page on Vast.ai (looks like `ssh root@123.45.67.89 -p 12345`). Run it:

```bash
ssh -p <SSH_PORT> root@<INSTANCE_IP>
```

On first connect, type `yes` to accept the fingerprint. Then pull your model:

```bash
ollama pull qwen2.5-coder:14b
# or
ollama pull qwen3:14b
```

Type `exit` when done.

> If you get `Host key verification failed` on a reconnect, run:
> `ssh-keygen -R "[<INSTANCE_IP>]:<SSH_PORT>"` then retry.

**4. Open an SSH tunnel**

Keep this terminal open while BananaQ is running. It forwards `localhost:11434` on your machine to Ollama inside the Vast.ai instance:

```bash
ssh -p <SSH_PORT> root@<INSTANCE_IP> -L 11434:localhost:11434 -N
```

The `-N` flag keeps the tunnel open without opening a shell.

**5. Verify Ollama is reachable**

In a new terminal:

```bash
curl http://localhost:11434/api/version
```

You should see a JSON response like `{"version":"0.x.x"}`.

**6. Configure `.env`**

```env
OLLAMA_BASE_URL=http://localhost:11434   # Tunnel makes the remote Ollama appear local
OLLAMA_LLM_MODEL=qwen2.5-coder:14b
LLM_API_KEY=ollama
```

> Remember to **stop the Vast.ai instance** when you are done to avoid ongoing charges.

---

### Option C: Managed Cloud API (OpenAI, Anthropic, etc.)

**Best for:** Developers who want zero infrastructure, just an API key and a billing account.

> **Note:** Cloud API providers bill per token. A typical PR review uses 2,000–8,000 tokens depending on diff size.

---

## Configuration Reference

All configuration lives in `.env`. Copy `.env.example` to get started.

```env
# ── GitHub ────────────────────────────────────────────────────────────────────
GITHUB_TOKEN=ghp_...
# PAT with: repo scope (classic) OR pull_requests:write + contents:write (fine-grained)

GITHUB_WEBHOOK_SECRET=some-random-string
# Must match the secret you set in your GitHub repo webhook settings

GITHUB_REPO=owner/repo-name
# The repository BananaQ monitors, e.g. widebirb/BQTR

# ── LLM Backend ───────────────────────────────────────────────────────────────
OLLAMA_BASE_URL=http://localhost:11434
# Base URL of your LLM server (Ollama, OpenAI, Groq, etc.)
# Do NOT include /v1 since BananaQ appends it automatically

OLLAMA_LLM_MODEL=qwen2.5-coder:7b
# Model name to pass in API calls. Must be available on your backend.

LLM_API_KEY=ollama
# API key for your LLM backend. Ollama ignores this, set to any string.
# For OpenAI/Groq/Google: set your actual API key here.

# ── Tuning ────────────────────────────────────────────────────────────────────
MAX_REVIEW_COMMENTS=10
# Maximum number of review comments posted per PR. Prevents overwhelming small PRs.

# ── Server ────────────────────────────────────────────────────────────────────
HOST=0.0.0.0
PORT=8000
```

---

## Running BananaQ

Once your `.env` is configured and your LLM backend is running:

```bash
python main.py
```

You should see:

```
INFO: Starting BananaQ on 0.0.0.0:8000
INFO: Uvicorn running on http://0.0.0.0:8000
```

Health check:

```bash
curl http://localhost:8000/health
# {"status":"ok","service":"BananaQ"}
```

---

## Exposing BananaQ to the Internet

GitHub needs a public URL to send webhook events. The easiest option is [ngrok](https://ngrok.com/).

**Install ngrok**, then in a separate terminal:

```bash
ngrok http 8000
```

You'll see output like:

```
Forwarding  https://abc123def456.ngrok-free.app -> http://localhost:8000
```

Copy the `https://...` URL and set it as your GitHub webhook Payload URL:

```
https://abc123def456.ngrok-free.app/webhook
```

> The free tier of ngrok generates a new URL each time you restart it. Update the GitHub webhook URL accordingly. A paid ngrok plan or [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) provides a stable URL.

---

## Customizing Behavior

### Orchestrator personality (`config/playbook.md`)

The orchestrator's decision-making and personality are controlled entirely by `config/playbook.md`. Edit this file to change:
- When the bot decides to review vs. skip a PR
- The tone and style of review comments
- Severity definitions
- Which file types trigger a review

### Adjusting review depth

| Setting | Effect |
|---|---|
| `MAX_REVIEW_COMMENTS` | Cap the number of comments per PR (default: 10) |
| `OLLAMA_LLM_MODEL` | Use a larger model for deeper reviews |
| `config/playbook.md` | Adjust skip thresholds and review focus areas |

### Skip list (deterministic, no LLM call)

The following file extensions are **always skipped** without an LLM call: `.md`, `.txt`, `.rst`, `.log`, `.csv`, `.json`, `.yaml`, `.yml`, `.toml`, `.ini`, `.lock`, `.png`, `.jpg`, `.gif`, `.svg`, and other binary/asset types. To change this list, edit `_SKIP_EXTENSIONS` in `orchestrator/dispatcher.py`.

---

## Running Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

All 71 tests run without any live API calls, LLM and GitHub endpoints are fully mocked.

| Test file | Coverage |
|---|---|
| `tests/test_webhook.py` | HMAC verification, event routing, health endpoint |
| `tests/test_dispatcher.py` | Orchestrator triage, deterministic skips, LLM fallback |
| `tests/test_reviewer.py` | Reviewer agent, comment capping, LLM retry |
| `tests/test_changelog.py` | Changelog agent, README prepend logic, date handling |
