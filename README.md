# BananaQ 🍌

AI-powered pull-request reviewer and orchestrator.

## Architecture

BananaQ runs in a **hybrid setup**:

| Component | Where it runs |
|-----------|--------------|
| BananaQ server | Local machine |
| Ngrok (webhook tunnel) | Local machine |
| Ollama (LLM inference) | Vast.ai GPU instance (remote) |

Ollama on the vast.ai instance is accessed securely via **SSH port forwarding** — no API keys or Cloudflare tunnels required.

---

## Installation

Install core application dependencies:

```bash
pip install -r requirements.txt
```

Install development and test dependencies:

```bash
pip install -r requirements-dev.txt
```

---

## Configuration

Create a `.env` file in the project root:

```env
# GitHub
GITHUB_TOKEN=ghp_YOUR_PAT_TOKEN         # PAT with repo + contents + metadata scopes
GITHUB_WEBHOOK_SECRET=YOUR_SECRET        # Random string — must match your GitHub webhook setting
GITHUB_REPO=your-username/your-repo

# Ollama (keep this as localhost — traffic is forwarded via SSH tunnel)
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_LLM_MODEL=qwen3.5:9b

# Server
HOST=0.0.0.0
PORT=8000
```

---

## Running the Server

### Step 1: Start the SSH tunnel to your Vast.ai Ollama instance

On your local machine, open a terminal and run:

```bash
ssh -p <SSH_PORT> root@<INSTANCE_IP> -L 11434:localhost:11434
```

Replace `<SSH_PORT>` and `<INSTANCE_IP>` with the values from your instance's connection info on the [Vast.ai Console](https://cloud.vast.ai/).

> **Keep this terminal open.** The SSH tunnel must stay alive for Ollama to be reachable at `localhost:11434`.

When prompted about the host fingerprint on first connection, type `yes`.

#### SSH key setup (first time only)

Vast.ai uses key-based SSH authentication. If you don't have an SSH key yet:

```bash
ssh-keygen -t ed25519
```

Press Enter through all prompts. Then copy the contents of `~/.ssh/id_ed25519.pub` and paste it into your [Vast.ai Account Settings](https://cloud.vast.ai/account/) under **SSH Keys**.

### Step 2: Verify Ollama is reachable

```bash
curl http://localhost:11434/api/version
```

You should get a JSON response. If not, Ollama may not be running on the instance — SSH into it and run `ollama serve`.

### Step 3: Start the BananaQ server

In a separate terminal:

```bash
python main.py
```

### Step 4: Start Ngrok

In another terminal, forward your local port 8000 to a public webhook URL:

```bash
ngrok http 8000
```

Copy the `https://...` URL and set it as your GitHub repository webhook URL (`https://<ngrok-id>.ngrok-free.app/webhook`).

---

## Running Tests

```bash
python -m pytest tests/ -v
```

---

## Vast.ai GPU Instance Setup

### Renting an instance

1. Go to the [Vast.ai Console](https://cloud.vast.ai/) and log in.
2. Select **Docker Image:** [`vastai/ollama`](https://hub.docker.com/r/vastai/ollama/)
3. **Hardware requirements by model:**

   | Model | Min VRAM | Recommended GPU |
   |-------|----------|-----------------|
   | `qwen3.5:9b` | ~8 GB | RTX 4060 Ti 16GB, RTX 3080 |
   | `qwen3.5:35b` | ~24 GB | RTX 3090, RTX 4090, A10G |

4. Allocate enough disk space for the model (at least 10 GB for 9b, 30 GB for 35b).
5. Launch the instance. Ollama starts automatically — no manual `ollama serve` needed.

### Pulling a model (first time)

SSH into the instance and run:

```bash
ollama pull qwen3.5:9b
```

Verify the model is available:

```bash
ollama list
```
