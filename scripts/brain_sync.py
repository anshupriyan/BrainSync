#!/usr/bin/env python3
"""
Brain Sync — Auto-process Claude & ChatGPT exports into Obsidian vault
Drop your export ZIP into the watch folder and this handles the rest.

Features:
- Detects new vs updated conversations (won't skip if messages were added)
- Queues tagging for later if LM Studio is offline
- Never loses a conversation even without LM Studio running
"""

import json
import os
import re
import sys
import time
import zipfile
import hashlib
import requests

# Ensure emoji/unicode prints correctly on Windows terminals
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from datetime import datetime
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ── CONFIG ────────────────────────────────────────────────────────────────────
# All settings are loaded from config.json in the project root.
# Edit config.json and set your LLM server URL and model name.
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

if not _CONFIG_PATH.exists():
    print("❌ config.json not found!")
    print(f"   Expected at: {_CONFIG_PATH}")
    print("   Make sure config.json exists in the project root with your LLM details.")
    sys.exit(1)

_cfg = json.loads(_CONFIG_PATH.read_text())

_PROJECT_ROOT  = _CONFIG_PATH.parent
_base_raw      = _cfg.get("base_dir", ".")
_BASE          = Path(os.path.expanduser(_base_raw))
if not _BASE.is_absolute():
    _BASE = (_PROJECT_ROOT / _BASE).resolve()
WATCH_FOLDER   = _BASE / "incoming"
VAULT_FOLDER   = _BASE / "vault"
STATE_FILE     = _BASE / ".brain_state.json"
PENDING_FILE   = _BASE / ".pending_tagging.json"
LM_STUDIO_URL  = _cfg.get("llm_url", "")
LM_MODEL       = _cfg.get("llm_model", "")
LM_HEALTH_URL  = _cfg.get("llm_health_url", "")

if not LM_STUDIO_URL or not LM_MODEL:
    print("❌ config.json is missing required fields: 'llm_url' and 'llm_model'")
    print("   Open config.json and set your local LLM server URL and model name.")
    sys.exit(1)
# ─────────────────────────────────────────────────────────────────────────────


# ── STATE MANAGEMENT ──────────────────────────────────────────────────────────

def load_state() -> dict:
    """
    State tracks each conversation:
    { conv_id: { "msg_count": int, "file": str, "tagged": bool } }
    """
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def load_pending() -> list:
    """List of dicts with info needed to tag a conversation later."""
    if PENDING_FILE.exists():
        return json.loads(PENDING_FILE.read_text())
    return []


def save_pending(pending: list):
    PENDING_FILE.write_text(json.dumps(pending, indent=2))


# ── LM STUDIO ─────────────────────────────────────────────────────────────────

def lm_studio_online() -> bool:
    # Use the configured health URL, or derive one from the completions URL
    health_url = LM_HEALTH_URL
    if not health_url:
        # Derive from completions URL: strip /chat/completions, use /models
        health_url = re.sub(r"/chat/completions$", "/models", LM_STUDIO_URL)
    try:
        r = requests.get(health_url, timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def ask_llm(prompt: str) -> str:
    try:
        resp = requests.post(LM_STUDIO_URL, json={
            "model": LM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 300,
        }, timeout=60)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"  [LLM] Error: {e}")
        return ""


def sanitize_tag(tag: str) -> str:
    """Make a tag Obsidian-safe: lowercase, spaces→hyphens, strip special chars."""
    tag = tag.lower().strip()
    tag = re.sub(r"[\s_]+", "-", tag)          # spaces/underscores → hyphens
    tag = re.sub(r"[^a-z0-9\-/]", "", tag)     # strip anything not alphanumeric, hyphen, or slash
    tag = re.sub(r"-+", "-", tag).strip("-")   # collapse multiple hyphens
    return tag or "untagged"


def tag_conversation(title: str, snippet: str) -> dict:
    prompt = f"""You are a tagging assistant. Read this AI conversation and return ONLY valid JSON.

Title: {title}
Content snippet: {snippet[:1500]}

Return JSON with this structure:
{{
  "tags": ["topic1", "topic2", "topic3"],
  "summary": "A plain English sentence describing what was discussed.",
  "category": "coding"
}}

Rules:
- "tags": a JSON array of 3-5 topic tags describing what the conversation is about. Tags must be lowercase with hyphens, e.g. "web-dev", "python", "exam-prep", "css-styling", "birthday-card". Do NOT copy these examples, generate tags specific to this conversation.
- "summary": one normal English sentence (NOT hyphenated). Example: "The user asked for help styling a VS Code theme."
- "category": exactly one of: coding, writing, research, planning, learning, personal, work, creative, other

Respond with ONLY the JSON object, nothing else."""

    raw = ask_llm(prompt)
    try:
        raw = re.sub(r"```json|```", "", raw).strip()
        result = json.loads(raw)
        # Handle LLM returning tags as a string instead of a list
        tags = result.get("tags", [])
        if isinstance(tags, str):
            # Split "writing, planning" or "writing planning" into list
            tags = [t.strip() for t in re.split(r"[,\s]+", tags) if t.strip()]
        elif not isinstance(tags, list):
            tags = []
        # Sanitize every tag regardless of what the LLM returns
        result["tags"] = [sanitize_tag(t) for t in tags if t.strip()]
        result["tags"] = [t for t in result["tags"] if t and t != "untagged"]  # remove empties
        return result
    except Exception:
        return {"tags": [], "summary": "", "category": "uncategorized"}


# ── MARKDOWN BUILDER ──────────────────────────────────────────────────────────

def make_markdown(title: str, date: str, source: str, meta: dict, messages: list) -> str:
    clean_tags = [sanitize_tag(t) for t in meta.get("tags", []) if t.strip()]
    clean_tags = [t for t in clean_tags if t and t != "untagged"]
    tags_yaml  = "\n".join(f'  - "{t}"' for t in clean_tags)
    summary    = meta.get("summary", "")
    category   = meta.get("category", "uncategorized")
    tagged     = bool(clean_tags)

    body_parts = []
    for msg in messages:
        role    = msg.get("role", "unknown").capitalize()
        content = msg.get("content", "").strip()
        if content:
            body_parts.append(f"**{role}:** {content}\n")
    body = "\n---\n".join(body_parts)

    pending_notice = "" if tagged else "> ⏳ **Tagging pending** — load LM Studio and re-run to generate tags.\n\n"

    return f"""---
title: "{title}"
date: {date}
source: {source}
category: {category}
tagged: {str(tagged).lower()}
tags:
{tags_yaml if tags_yaml else '  - untagged'}
---

{pending_notice}> {summary}

---

{body}
"""


def slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[\s_-]+", "-", text).strip("-")[:80]


# ── PARSERS ───────────────────────────────────────────────────────────────────

def parse_claude(data: list) -> list:
    conversations = []
    for conv in data:
        conv_id = conv.get("uuid", "")
        title   = conv.get("name", "Untitled")
        created = conv.get("created_at", "")[:10] or datetime.now().strftime("%Y-%m-%d")
        messages = []
        for msg in conv.get("chat_messages", []):
            role    = msg.get("sender", "unknown")
            role    = "user" if role == "human" else "assistant"
            
            # ALWAYS parse structured content blocks first.
            # msg["text"] is a pre-rendered fallback that contains
            # "This block is not supported on your current device yet"
            # for tool_use/tool_result blocks — never use it directly.
            content_blocks = msg.get("content", [])
            if content_blocks:
                parts = []
                for block in content_blocks:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type", "")
                    if btype == "text":
                        text = block.get("text", "").strip()
                        if text:
                            parts.append(text)
                    elif btype == "tool_use":
                        tool_name = block.get("name", "tool")
                        parts.append(f"\n> 🔧 *Used tool: {tool_name}*\n")
                    elif btype == "tool_result":
                        for rb in block.get("content", []):
                            if isinstance(rb, dict) and rb.get("type") == "text":
                                text = rb.get("text", "").strip()
                                if text:
                                    parts.append(f"\n> **Tool output (truncated):**\n> {text[:500]}\n")
                    elif btype in ("image", "document", "file"):
                        parts.append(f"\n> 📎 *{btype} attachment — not shown*\n")
                    # skip any other unknown block types silently
                content = "\n".join(parts)
            else:
                # Fallback to text only if there are no content blocks at all
                content = msg.get("text", "")
            
            if content.strip():
                messages.append({"role": role, "content": content})
        conversations.append({
            "id": conv_id,
            "title": title,
            "date": created,
            "source": "claude",
            "messages": messages,
        })
    return conversations


def parse_chatgpt(data: list) -> list:
    conversations = []
    for conv in data:
        conv_id = conv.get("id", "")
        title   = conv.get("title", "Untitled")
        created = datetime.fromtimestamp(
            conv.get("create_time", 0)
        ).strftime("%Y-%m-%d") if conv.get("create_time") else datetime.now().strftime("%Y-%m-%d")
        messages = []
        mapping  = conv.get("mapping", {})
        for node in mapping.values():
            msg = node.get("message")
            if not msg:
                continue
            role  = msg.get("author", {}).get("role", "")
            if role not in ("user", "assistant"):
                continue
            parts = msg.get("content", {}).get("parts", [])
            text_parts = []
            for p in parts:
                if isinstance(p, str):
                    text_parts.append(p)
                elif isinstance(p, dict):
                    # Non-text parts (images, files, etc.) — show a placeholder
                    ptype = p.get("content_type", p.get("type", "attachment"))
                    text_parts.append(f"*[{ptype} attachment — not shown]*")
            content = " ".join(text_parts).strip()
            if content:
                messages.append({"role": role, "content": content})
        conversations.append({
            "id": conv_id,
            "title": title,
            "date": created,
            "source": "chatgpt",
            "messages": messages,
        })
    return conversations


# ── PROCESS A SINGLE CONVERSATION ─────────────────────────────────────────────

def process_conversation(conv: dict, state: dict, pending: list, online: bool, output_dir: Path, force_retag: bool = False):
    conv_id   = conv["id"] or hashlib.md5(conv["title"].encode()).hexdigest()[:12]
    title     = conv["title"] or "Untitled"
    messages  = conv["messages"]
    msg_count = len(messages)

    if not messages:
        return "skip"

    existing = state.get(conv_id)

    # Already processed, same count, already tagged → skip (unless forcing retag)
    if existing and existing["msg_count"] == msg_count and existing["tagged"] and not force_retag:
        return "skip"

    # Force retag: treat as retag action even if already tagged
    if force_retag and existing:
        state[conv_id]["tagged"] = False  # temporarily mark untagged to trigger retag path

    # Already processed, same count, but not yet tagged → try to tag now
    if existing and existing["msg_count"] == msg_count and not existing["tagged"]:
        if not online:
            return "skip"  # Still offline, leave in pending
        action = "retag"
    elif existing and existing["msg_count"] != msg_count:
        action = "update"  # New messages added to existing conversation
    else:
        action = "new"     # Brand new conversation

    snippet = " ".join(m["content"] for m in messages[:6] if m.get("content"))

    if online:
        print(f"  🤖 {'Tagging' if action == 'retag' else 'Processing'}: {title[:55]} ({msg_count} msgs)")
        meta   = tag_conversation(title, snippet)
        tagged = True
        # Remove from pending if it was there
        pending[:] = [p for p in pending if p["id"] != conv_id]
    else:
        print(f"  💾 Saving (untagged): {title[:55]}")
        meta   = {"tags": [], "summary": "", "category": "uncategorized"}
        tagged = False
        # Add to pending if not already there
        if not any(p["id"] == conv_id for p in pending):
            pending.append({
                "id":      conv_id,
                "title":   title,
                "snippet": snippet[:500],
            })

    md_content = make_markdown(
        title    = title,
        date     = conv["date"],
        source   = conv["source"],
        meta     = meta,
        messages = messages,
    )

    # Use existing file path or create new one
    if existing and existing.get("file"):
        out_path = Path(existing["file"])
    else:
        filename = f"{conv['date']}-{slugify(title)}.md"
        out_path = output_dir / filename
        counter  = 1
        while out_path.exists() and not existing:
            out_path = output_dir / f"{conv['date']}-{slugify(title)}-{counter}.md"
            counter += 1

    out_path.write_text(md_content, encoding="utf-8")

    state[conv_id] = {
        "msg_count": msg_count,
        "file":      str(out_path),
        "tagged":    tagged,
    }

    return action


# ── TAG PENDING CONVERSATIONS ──────────────────────────────────────────────────

def process_pending():
    pending = load_pending()
    if not pending:
        return
    if not lm_studio_online():
        print(f"  ⏳ {len(pending)} conversations waiting to be tagged (LM Studio offline)")
        return

    print(f"\n🔁 Tagging {len(pending)} previously saved conversations...")
    state         = load_state()
    still_pending = []

    for item in pending:
        conv_id = item["id"]
        entry   = state.get(conv_id)
        if not entry or not Path(entry["file"]).exists():
            continue

        meta = tag_conversation(item["title"], item["snippet"])
        if not meta.get("tags"):
            still_pending.append(item)
            continue

        tags_yaml    = "\n".join(f'  - "{t}"' for t in meta["tags"])
        existing_md  = Path(entry["file"]).read_text(encoding="utf-8")
        existing_md  = re.sub(r'tagged: false', 'tagged: true', existing_md)
        existing_md  = re.sub(r'category: uncategorized', f'category: {meta["category"]}', existing_md)
        existing_md  = re.sub(r'tags:\n  - untagged', f'tags:\n{tags_yaml}', existing_md)
        existing_md  = existing_md.replace(
            '> ⏳ **Tagging pending** — load LM Studio and re-run to generate tags.\n\n', ''
        )
        existing_md  = re.sub(r'> \n', f'> {meta["summary"]}\n', existing_md)

        Path(entry["file"]).write_text(existing_md, encoding="utf-8")
        state[conv_id]["tagged"] = True
        print(f"  ✅ Tagged: {item['title'][:55]}")

    save_pending(still_pending)
    save_state(state)
    if still_pending:
        print(f"  ⏳ {len(still_pending)} still pending (LLM returned empty response)")


# ── CROSSLINKS ────────────────────────────────────────────────────────────────

CROSSLINKS_START = "<!-- crosslinks-start -->"
CROSSLINKS_END   = "<!-- crosslinks-end -->"

def _tag_words(tag: str) -> set:
    """Split a hyphenated tag into root words for fuzzy matching."""
    return set(tag.replace("-", " ").split())


def build_crosslinks():
    """Scan all vault notes, find related ones, and inject cross-link sections."""
    state = load_state()
    if not state:
        return

    # Build index: collect tags + category for each note
    note_info = {}  # filepath -> {"tags": set, "category": str, "title": str}
    for conv_id, entry in state.items():
        fpath = Path(entry.get("file", ""))
        if not fpath.exists():
            continue
        raw = fpath.read_text(encoding="utf-8")
        tags = set(re.findall(r'  - "(.+?)"', raw[:1000]))
        title_match = re.search(r'^title:\s*"?(.+?)"?\s*$', raw, re.MULTILINE)
        title = title_match.group(1) if title_match else fpath.stem
        cat_match = re.search(r'^category:\s*(\S+)', raw, re.MULTILINE)
        category = cat_match.group(1) if cat_match else ""
        note_info[str(fpath)] = {"tags": tags, "category": category, "title": title}

    # Score relatedness between each pair of notes
    updated = 0
    for fpath, info in note_info.items():
        scores = {}  # other_path -> (score, reasons)
        my_tags = info["tags"]
        my_words = set()
        for t in my_tags:
            my_words |= _tag_words(t)

        for other_path, other_info in note_info.items():
            if other_path == fpath:
                continue

            score = 0
            reasons = []

            # Exact tag matches (strongest signal)
            shared_exact = my_tags & other_info["tags"]
            if shared_exact:
                score += len(shared_exact) * 3
                reasons.extend(shared_exact)

            # Tag-word overlap (e.g. "exam-prep" ↔ "viva-preparation" share "exam"/"prep")
            other_words = set()
            for t in other_info["tags"]:
                other_words |= _tag_words(t)
            shared_words = my_words & other_words
            # Exclude very common/short words
            shared_words -= {"a", "an", "the", "of", "to", "in", "for", "and", "or", "app", "web",
                             "vs", "new", "best", "how", "use", "get", "set", "my", "no", "do",
                             "file", "files", "tools", "tool", "guide", "subject"}
            if shared_words and not shared_exact:  # don't double-count
                score += len(shared_words)
                reasons.append(f"shared: {', '.join(sorted(shared_words))}")

            # Same category (weaker signal but still useful)
            if info["category"] and info["category"] == other_info["category"]:
                score += 1
                if not reasons:
                    reasons.append(info["category"])

            if score > 0:
                scores[other_path] = (score, reasons)

        # Sort by score, take top 5
        ranked = sorted(scores.items(), key=lambda x: -x[1][0])[:5]

        # Build crosslinks section
        if ranked:
            links = []
            for rel_path, (score, reasons) in ranked:
                rel_name = Path(rel_path).stem
                rel_title = note_info[rel_path]["title"]
                reason_str = ", ".join(f"`{r}`" for r in reasons[:3])
                links.append(f"- [[{rel_name}|{rel_title}]] — {reason_str}")
            crosslinks_block = f"\n{CROSSLINKS_START}\n## 🔗 Related Conversations\n\n" + "\n".join(links) + f"\n{CROSSLINKS_END}\n"
        else:
            crosslinks_block = ""

        # Read file, strip old crosslinks, append new ones
        raw = Path(fpath).read_text(encoding="utf-8")
        raw = re.sub(
            rf"\n?{re.escape(CROSSLINKS_START)}.*?{re.escape(CROSSLINKS_END)}\n?",
            "",
            raw,
            flags=re.DOTALL,
        )
        raw = raw.rstrip() + "\n"

        if crosslinks_block:
            raw += crosslinks_block

        Path(fpath).write_text(raw, encoding="utf-8")
        if ranked:
            updated += 1

    if updated:
        print(f"  🔗 Cross-linked {updated} notes with related conversations")


# ── PROCESS ZIP ───────────────────────────────────────────────────────────────

def process_zip(zip_path: Path):
    print(f"\n📦 Processing: {zip_path.name}")
    state   = load_state()
    pending = load_pending()

    # Clean up state entries whose files were deleted or are outside current vault
    vault_resolved = str(VAULT_FOLDER.resolve())
    stale = [
        cid for cid, entry in state.items()
        if not Path(entry.get("file", "")).exists()
        or not str(Path(entry.get("file", "")).resolve()).startswith(vault_resolved)
    ]
    if stale:
        print(f"  🧹 {len(stale)} notes missing or outside current vault — clearing from state")
        for cid in stale:
            del state[cid]
        save_state(state)

    online  = lm_studio_online()

    if online:
        print("  🟢 LM Studio online — tagging enabled")
        process_pending()
        state   = load_state()
        pending = load_pending()
    else:
        print("  🔴 LM Studio offline — saving without tags (will tag next time it's online)")

    counts = {"new": 0, "update": 0, "retag": 0, "skip": 0}

    with zipfile.ZipFile(zip_path, "r") as zf:
        json_files = [n for n in zf.namelist() if n.endswith(".json") and "conversation" in n.lower()]

        for json_file in json_files:
            print(f"  📄 Reading {json_file}")
            try:
                data = json.loads(zf.read(json_file))
            except Exception as e:
                print(f"  ❌ Could not parse {json_file}: {e}")
                continue

            if isinstance(data, list) and data and "chat_messages" in data[0]:
                conversations = parse_claude(data)
                source_label  = "claude"
            elif isinstance(data, list) and data and "mapping" in data[0]:
                conversations = parse_chatgpt(data)
                source_label  = "chatgpt"
            else:
                print(f"  ⚠️  Unknown format in {json_file}, skipping")
                continue

            output_dir = VAULT_FOLDER / source_label
            output_dir.mkdir(parents=True, exist_ok=True)

            for conv in conversations:
                result = process_conversation(conv, state, pending, online, output_dir)
                counts[result] = counts.get(result, 0) + 1

    save_state(state)
    save_pending(pending)
    build_crosslinks()

    print(f"""
✅ Done!
   🆕 New:      {counts['new']}
   🔄 Updated:  {counts['update']}
   🏷️  Retagged: {counts['retag']}
   ⏭️  Skipped:  {counts['skip']}
   ⏳ Pending tagging: {len(pending)}
   📚 Vault: {VAULT_FOLDER}
""")


# ── RETAG ALL ─────────────────────────────────────────────────────────────────

def retag_all():
    """Re-tag every conversation in the vault using current LM Studio model."""
    if not lm_studio_online():
        print("❌ LM Studio is offline — cannot retag. Start LM Studio and try again.")
        return

    state = load_state()
    if not state:
        print("⚠️  No conversations in state yet. Run normally first to import ZIPs.")
        return

    print(f"\n🏷️  Retagging {len(state)} conversations in vault...\n")
    ok = fail = skip = 0

    for conv_id, entry in state.items():
        file_path = Path(entry.get("file", ""))
        if not file_path.exists():
            print(f"  ⚠️  File missing, skipping: {file_path.name}")
            skip += 1
            continue

        # Extract title + snippet from existing markdown body
        raw_md = file_path.read_text(encoding="utf-8")

        # Pull title from frontmatter
        title_match = re.search(r'^title:\s*"?(.+?)"?\s*$', raw_md, re.MULTILINE)
        title = title_match.group(1) if title_match else file_path.stem

        # Snippet from body (after second ---)
        body_match = re.split(r'^---\s*$', raw_md, maxsplit=2, flags=re.MULTILINE)
        snippet = body_match[-1][:1500] if len(body_match) >= 3 else raw_md[:1500]

        print(f"  🤖 Retagging: {title[:55]}")
        meta = tag_conversation(title, snippet)

        if not meta.get("tags"):
            print(f"     ⚠️  LLM returned no tags, skipping")
            fail += 1
            continue

        # Rewrite frontmatter in-place
        tags_yaml   = "\n".join(f'  - "{t}"' for t in meta["tags"])
        updated_md  = re.sub(r'tagged:\s*\w+', 'tagged: true', raw_md)
        updated_md  = re.sub(r'category:\s*\S+', f'category: {meta["category"]}', updated_md)
        updated_md  = re.sub(r'tags:\n(?:  - .+\n?)+', f'tags:\n{tags_yaml}\n', updated_md)
        # Remove pending notice if present
        updated_md  = updated_md.replace(
            '> ⏳ **Tagging pending** — load LM Studio and re-run to generate tags.\n\n', ''
        )
        # Replace summary line
        updated_md  = re.sub(r'^> .*$', f'> {meta["summary"]}', updated_md, count=1, flags=re.MULTILINE)
        # Replace wikilinks line (first non-empty line after the blockquote)
        wikilinks   = " ".join(f"[[{t}]]" for t in meta["tags"])
        updated_md  = re.sub(
            r'^(\[\[.*?\]\][ ]*)+$',
            wikilinks,
            updated_md,
            count=1,
            flags=re.MULTILINE
        )

        file_path.write_text(updated_md, encoding="utf-8")
        state[conv_id]["tagged"] = True
        ok += 1

    save_state(state)
    print(f"""
✅ Retag complete!
   ✅ Retagged: {ok}
   ⚠️  Failed:   {fail}
   ⏭️  Skipped:  {skip}
""")


# ── FOLDER WATCHER ────────────────────────────────────────────────────────────

class ZipDropHandler(FileSystemEventHandler):
    def _handle(self, path: str):
        if not path.endswith(".zip"):
            return
        time.sleep(1)
        process_zip(Path(path))

    def on_created(self, event):
        if not event.is_directory:
            self._handle(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._handle(event.dest_path)


def watch():
    WATCH_FOLDER.mkdir(parents=True, exist_ok=True)
    VAULT_FOLDER.mkdir(parents=True, exist_ok=True)

    online = lm_studio_online()
    status = "🟢 Online" if online else "🔴 Offline (will save without tags)"

    print(f"""
🧠 Brain Sync Running
─────────────────────────────
📥 Drop ZIPs here:  {WATCH_FOLDER}
📚 Vault:           {VAULT_FOLDER}
🤖 LM Studio:       {status}
─────────────────────────────
Waiting for exports...
""")

    if online:
        process_pending()

    # Process any ZIPs already sitting in the folder at startup
    existing_zips = sorted(WATCH_FOLDER.glob("*.zip"))
    if existing_zips:
        print(f"📂 Found {len(existing_zips)} existing ZIP(s) in incoming — processing now...")
        for zip_path in existing_zips:
            process_zip(zip_path)

    observer = Observer()
    observer.schedule(ZipDropHandler(), str(WATCH_FOLDER), recursive=False)
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Brain Sync — AI export processor")
    parser.add_argument("--retag",      action="store_true", help="Re-tag all existing vault notes and exit")
    parser.add_argument("--reprocess",  action="store_true", help="Wipe state and reprocess all ZIPs in incoming from scratch")
    args = parser.parse_args()

    if args.retag:
        retag_all()
    elif args.reprocess:
        print("🗑️  Wiping state and reprocessing all ZIPs...")
        STATE_FILE.unlink(missing_ok=True)
        PENDING_FILE.unlink(missing_ok=True)
        for zip_path in sorted(WATCH_FOLDER.glob("*.zip")):
            process_zip(zip_path)
    else:
        watch()
