# BananaQ 🍌

AI-powered pull-request reviewer and orchestrator.

## Installation

To install the core application dependencies:

```bash
pip install -r requirements.txt
```

To install development and test dependencies:

```bash
pip install -r requirements-dev.txt
```

## Running the Server

Start the server using:

```bash
python main.py
```

## Running Tests

Run the test suite using pytest:

```bash
python -m pytest tests/ -v
```

---

## Rented GPU Setup (Vast.ai via Web Terminal/Jupyter)

If you are renting a GPU on [Vast.ai](https://vast.ai/) and want to run the entire system (BananaQ, Ollama, and Ngrok) directly inside the rented container using their web terminal or Jupyter workspace, follow these steps.

### Step 1: Rent a GPU Instance on Vast.ai
1. Go to the [Vast.ai Console](https://console.vast.ai/) and log in.
2. In the template search or custom Docker configuration, specify:
   - **Docker Image:** [vastai/ollama](https://hub.docker.com/r/vastai/ollama/)
3. **Hardware Selection:**
   - **VRAM Requirements:** The `qwen3.5:27b` model needs a GPU with at least **24 GB VRAM** (e.g., NVIDIA RTX 3090, RTX 4090, or A10G) for quantized runs, or **48 GB+ VRAM** (e.g., RTX 6000 Ada, A40, A100) for full precision.
   - **Disk Space:** Allocate at least **35-40 GB** of disk space to ensure room for the `qwen3.5:27b` model.
4. Launch the instance and open the **Web Terminal** or **Jupyter Lab Terminal** once it is running.

---

### Step 2: System Setup & Python Dependencies
Inside the web terminal (as the `root` user), run the following commands to install system packages and dependencies:

```bash
# 1. Update package manager and install Python, git, pip, and utilities
apt update && apt install -y python3 python3-pip python3-venv git curl wget jq

# 2. Clone your BananaQ repository (or navigate to your uploaded workspace folder)
# git clone <YOUR_REPOS_URL> && cd BananaQ
# If your files are already mounted in the workspace, just cd into the folder:
cd /workspace/BananaQ

# 3. Create a python virtual environment and activate it
python3 -m venv .venv
source .venv/bin/activate

# 4. Install BananaQ dependencies
pip install -r requirements.txt
```

---

### Step 3: Run Ollama & Pull the Model
Ollama is pre-installed in the Docker image. Start the service and download the model:

```bash
# 1. Start the Ollama server in the background (output redirected to log)
ollama serve > ollama.log 2>&1 &

# 2. Wait a few seconds for Ollama to initialize, then verify it is running
sleep 3
ollama list

# 3. Pull the qwen3.5:27b model
ollama pull qwen3.5:27b
```

---

### Step 4: Install & Run Ngrok
Install the Ngrok agent, authenticate it, and start the tunnel in the background:

```bash
# 1. Install Ngrok via the official Debian repository
curl -sSL https://ngrok-agent.s3.amazonaws.com/ngrok.asc | tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null \
&& echo "deb https://ngrok-agent.s3.amazonaws.com bookworm main" | tee /etc/apt/sources.list.d/ngrok.list \
&& apt update \
&& apt install -y ngrok

# 2. Add your Ngrok authtoken (retrieve from https://dashboard.ngrok.com/)
ngrok config add-authtoken <YOUR_NGROK_AUTHTOKEN>

# 3. Start Ngrok in the background forwarding port 8000
ngrok http 8000 > ngrok.log 2>&1 &

# 4. Wait 2 seconds and fetch your public HTTPS webhook payload URL
sleep 2
curl -s http://localhost:4040/api/tunnels | jq -r '.tunnels[0].public_url'
```
*Note: Copy the `https://...` URL printed by the last command. You will paste this into your GitHub Repository Webhook settings as `https://<ngrok-id>.ngrok-free.app/webhook`.*

---

### Step 5: Configure Environment & Run BananaQ
1. Create your `.env` file in `/workspace/BananaQ/`:
   ```bash
   nano .env
   ```
2. Paste and configure the following variables (replacing placeholder values):
   ```env
   GITHUB_TOKEN=ghp_YOUR_PAT_TOKEN
   GITHUB_WEBHOOK_SECRET=YOUR_WEBHOOK_HMAC_SECRET
   GITHUB_REPO=your-username/your-repo

   OLLAMA_BASE_URL=http://localhost:11434
   OLLAMA_LLM_MODEL=qwen3.5:27b

   HOST=0.0.0.0
   PORT=8000
   ```
3. Run the BananaQ server:
   ```bash
   python main.py
   ```
   *(If you want to run BananaQ in the background so you can close the terminal, run `python main.py > server.log 2>&1 &` instead).*


