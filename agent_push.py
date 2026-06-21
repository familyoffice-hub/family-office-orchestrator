# -*- coding: utf-8 -*-
"""
AGENT PUSH HELPER
=================
Dipakai oleh agent lain (mis. Tools A / AI Agent) untuk MENULIS 1 baris hasil ke
"papan tulis bersama" (inbox.json) di repo orchestrator, lewat GitHub API.

Butuh environment variables di agent:
  ORCH_REPO    = "username/family-office-orchestrator"   (repo orchestrator Anda)
  GITHUB_TOKEN = token GitHub dengan akses 'contents: write' ke repo itu
                 (di GitHub Actions, pakai ${{ secrets.GITHUB_TOKEN }} bila repo sama,
                  atau Personal Access Token bila repo berbeda)

Contoh pemakaian di dalam agent:
  from agent_push import push
  push("Tax Agent", "DJP perketat lapor aset HNWI",
       "Ringkasan singkat...", source_url="https://...", confidentiality="internal")
"""

import os
import json
import time
import base64
import requests

ORCH_REPO = os.getenv("ORCH_REPO", "").strip()         # "user/repo"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
INBOX_PATH = os.getenv("INBOX_PATH", "inbox.json")
API = "https://api.github.com"

def push(agent_id, title, summary, source_url="", confidentiality="internal",
         category="", raw_output=""):
    if not ORCH_REPO or not GITHUB_TOKEN:
        print("[agent_push] ORCH_REPO/GITHUB_TOKEN belum diset. Lewati.")
        return False
    url = f"{API}/repos/{ORCH_REPO}/contents/{INBOX_PATH}"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}",
               "Accept": "application/vnd.github+json"}
    # ambil isi inbox saat ini + sha
    try:
        r = requests.get(url, headers=headers, timeout=20)
        if r.status_code == 200:
            data = r.json()
            sha = data["sha"]
            content = json.loads(base64.b64decode(data["content"]).decode("utf-8"))
            if not isinstance(content, list):
                content = []
        else:
            sha = None; content = []
    except Exception as e:
        print("[agent_push] gagal baca inbox:", e); return False

    row = {
        "output_id": "o" + format(int(time.time() * 1000) % 10_000_000, "x"),
        "agent_id": agent_id, "title": title, "summary": summary,
        "raw_output": raw_output, "source_url": source_url,
        "category": category, "confidentiality_level": confidentiality,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "is_processed": False,
    }
    content.append(row)
    content = content[-1000:]
    new_b64 = base64.b64encode(json.dumps(content, ensure_ascii=False, indent=2).encode("utf-8")).decode()
    payload = {"message": f"agent push: {agent_id} - {title[:50]}", "content": new_b64}
    if sha:
        payload["sha"] = sha
    try:
        r = requests.put(url, headers=headers, json=payload, timeout=20)
        if r.status_code in (200, 201):
            print("[agent_push] OK:", title[:60]); return True
        print("[agent_push] gagal:", r.status_code, r.text[:150]); return False
    except Exception as e:
        print("[agent_push] exception:", e); return False
