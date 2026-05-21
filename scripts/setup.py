#!/usr/bin/env python3
"""
Brain Sync Setup — Run this once to get started.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE  = PROJECT_ROOT / "config.json"

# Load base dir from config
if CONFIG_FILE.exists():
    cfg = json.loads(CONFIG_FILE.read_text())
    # Remind user to edit config if placeholders are still there
    if "YOUR_HOST" in cfg.get("llm_url", ""):
        print("  ⚠️  config.json still has placeholder values!")
        print(f"     → Edit {CONFIG_FILE} and set your LLM server URL and model name.\n")
else:
    print(f"  ❌ config.json not found at {CONFIG_FILE}")
    sys.exit(1)

# Resolve base_dir: relative paths are relative to project root
base_raw = cfg.get("base_dir", ".")
base = Path(os.path.expanduser(base_raw))
if not base.is_absolute():
    base = (PROJECT_ROOT / base).resolve()

FOLDERS = [
    base / "incoming",
    base / "vault" / "claude",
    base / "vault" / "chatgpt",
]

print("🧠 Brain Sync Setup\n")

# Create folders
for folder in FOLDERS:
    folder.mkdir(parents=True, exist_ok=True)
    print(f"  ✅ Created: {folder}")

# Install dependencies
print("\n📦 Installing dependencies...")
subprocess.check_call([sys.executable, "-m", "pip", "install", "watchdog", "requests"])

print(f"""
✅ Setup complete!

Next steps:
1. Edit your config file with your LLM server details:
     {CONFIG_FILE}
2. Start your local LLM server (LM Studio, Ollama, etc.)
3. Open Obsidian → Open Folder as Vault → select:
     {base / 'vault'}
4. Run the watcher:
     python scripts/brain_sync.py
5. Export your Claude/ChatGPT data and drop the ZIP into:
     {base / 'incoming'}

That's it — new conversations appear in your vault automatically!

──────────────────────────────────────────
💡 Don't have a local LLM yet?
   Brain Sync still works — conversations are saved as clean Markdown
   and tagged automatically once your LLM comes online.
──────────────────────────────────────────
""")
