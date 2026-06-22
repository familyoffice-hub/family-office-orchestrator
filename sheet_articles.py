# -*- coding: utf-8 -*-
"""
SHEET ARTICLES  (Google Sheet -> AI -> menu Artikel website)
============================================================
Membaca daftar JUDUL dari Google Sheet (tab "JUDUL FAMILY OFFICE",
di-"Publish to web" sebagai CSV), lalu untuk tiap judul menulis artikel
panjang (~2.500 kata) dengan Gemini memakai PROMPT_TEMPLATE di bawah,
lalu MENERBITKANNYA ke posts.json repo Website.

Jadwal: GitHub Actions cron jam 08:10 & 16:00 WIB.
Tanpa database & tanpa kredensial Google: cukup link CSV publik.

Cara baca sheet:
- Tidak perlu baris header.
- Kolom A boleh nomor (diabaikan). Kolom B = JUDUL artikel.
- Judul diterbitkan bertahap (MAX_PER_RUN per jalan), urut dari atas.
- Judul yang sudah terbit dicatat di sheet_log.json (anti-dobel),
  di-commit kembali oleh Actions, jadi tidak pernah terbit dua kali.

Untuk MENGUBAH gaya artikel: sunting blok PROMPT_TEMPLATE di bawah.
Pakai {JUDUL} sebagai tempat judul akan disisipkan.
"""

import os
import re
import csv
import json
import time
import html
import base64
import hashlib
from io import StringIO
from datetime import datetime, timezone, timedelta

import sys
import requests
try:
    sys.stdout.reconfigure(line_buffering=True)  # log tampil real-time di Actions
except Exception:
    pass
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ----------------------------- Pengaturan -----------------------------------
SHEET_CSV_URL = os.getenv("SHEET_CSV_URL", "").strip()      # link "Publish to web" -> CSV (tab JUDUL)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
AI_MODEL = os.getenv("AI_MODEL", "gemini-2.5-flash-lite")
AI_ENABLED = bool(GEMINI_API_KEY)

WEB_REPO = os.getenv("WEB_REPO", "").strip()                # mis. "familyoffice-hub/Website"
WEB_POSTS_PATH = os.getenv("WEB_POSTS_PATH", "posts.json")
GH_PUSH_TOKEN = os.getenv("GH_PUSH_TOKEN", "").strip()

LOG_FILE = os.getenv("SHEET_LOG_FILE", "sheet_log.json")    # catatan judul yang sudah terbit
MAX_POSTS = int(os.getenv("MAX_POSTS", "60"))               # batas total artikel di posts.json
MAX_PER_RUN = int(os.getenv("MAX_PER_RUN", "1"))            # artikel per jalan (hemat kuota AI)
MAX_TOKENS = int(os.getenv("ARTICLE_MAX_TOKENS", "8192"))   # cukup untuk ~2.500 kata
AI_PACING_SEC = float(os.getenv("AI_PACING_SEC", "8"))      # jeda antar panggilan AI
AI_TEMPERATURE = float(os.getenv("AI_TEMPERATURE", "0.85")) # lebih "humanized"

JAKARTA = timezone(timedelta(hours=7))
_BLN_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_BLN_ID = ["Januari", "Februari", "Maret", "April", "Mei", "Juni",
           "Juli", "Agustus", "September", "Oktober", "November", "Desember"]

ARTICLE_SYSTEM = ("Anda penulis artikel keuangan profesional di Indonesia. "
                  "Tulisan Anda mengalir, manusiawi, dan tidak terdeteksi sebagai buatan AI.")

# ====================== PROMPT ARTIKEL (boleh disunting) =====================
# {JUDUL}    -> diganti judul dari Google Sheet
# {TANGGAL}  -> diganti tanggal hari ini (mis. "22 Juni 2026")
PROMPT_TEMPLATE = (
    "Anda adalah ahli di industri Family Office dan pakar menulis artikel keuangan "
    "di Indonesia. Buatkan artikel tertanggal hari ini ({TANGGAL}) (tidak perlu tulis "
    "\"tanggal:\"), artikel SEO/GEO yang evergreen, yang humanized, enak dibaca, "
    "miringkan (italic) yang bukan berbahasa Indonesia, dan tidak terdeteksi sebagai "
    "tulisan AI, tidak perlu pakai bar pembatas, tambahkan quotes yang relevan "
    "(jangan dari orang yang sama 2x), di akhir tulisan tulis nama penulis "
    "\"~ David Cornelis Mokalu\" (tidak perlu tulis \"penulis:\"), sesuaikan dengan "
    "aturan terbaru baik di luar negeri ataupun di Indonesia (bila ada, sebutkan aturan "
    "tersebut per kapan), berjumlah sekitar 2.500 kata untuk sebuah artikel yang "
    "berjudul \"{JUDUL}\".\n\n"
    "ATURAN FORMAT KELUARAN (WAJIB DIIKUTI):\n"
    "- Keluarkan HANYA HTML bersih, tanpa pagar kode (tanpa ```), tanpa <h1>, "
    "tanpa <hr>, tanpa <html>/<head>/<body>.\n"
    "- Gunakan <p> untuk paragraf, <h2> atau <h3> untuk subjudul, <em> untuk "
    "kata/istilah berbahasa asing (bukan Indonesia), dan <blockquote> untuk kutipan.\n"
    "- Jangan menulis ulang judul utama di awal (judul sudah tampil terpisah).\n"
    "- Jangan mengarang nama, nomor, atau tanggal regulasi. Sebutkan aturan HANYA bila "
    "Anda cukup yakin; bila ragu, bahas secara umum tanpa menyebut tanggal."
)
# ============================================================================


# ----------------------------- Util kecil -----------------------------------
def tanggal_posts():
    now = datetime.now(JAKARTA)
    return f"{now.day:02d} {_BLN_EN[now.month - 1]} {now.year}"   # mis. "22 Jun 2026"


def tanggal_id():
    now = datetime.now(JAKARTA)
    return f"{now.day} {_BLN_ID[now.month - 1]} {now.year}"        # mis. "22 Juni 2026"


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def key_judul(judul):
    s = re.sub(r"\s+", " ", (judul or "").strip().lower())
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


# ----------------------------- AI (Gemini) ----------------------------------
def call_ai(system, user, max_tokens=MAX_TOKENS, attempts=3):
    if not AI_ENABLED:
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{AI_MODEL}:generateContent"
    body = {"system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": AI_TEMPERATURE,
                                 "thinkingConfig": {"thinkingBudget": 0}}}
    for attempt in range(attempts):
        try:
            r = requests.post(url, headers={"x-goog-api-key": GEMINI_API_KEY,
                                            "Content-Type": "application/json"},
                              json=body, timeout=180)
            if r.status_code == 200:
                cands = r.json().get("candidates", [])
                if not cands:
                    return None
                parts = cands[0].get("content", {}).get("parts", [])
                txt = "".join(p.get("text", "") for p in parts).strip()
                return txt or None
            if r.status_code == 429:
                w = 15 + attempt * 5
                print(f"[i] Gemini 429, tunggu {w}s... ({attempt+1}/{attempts})")
                time.sleep(w); continue
            if r.status_code in (500, 502, 503, 504):
                w = 8 * (attempt + 1)
                print(f"[i] Gemini sibuk {r.status_code}, tunggu {w}s..."); time.sleep(w); continue
            print("[!] Gemini error", r.status_code, r.text[:200]); return None
        except Exception as e:
            print("[!] Gemini exception", e); time.sleep(6)
    return None


def _clean_ai(text):
    if not text:
        return text
    t = text.strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


def web_safe_html(t):
    """Izinkan tag artikel yang aman; buang sisanya."""
    if not t:
        return ""
    t = _clean_ai(t)
    t = re.sub(r"(?is)<\s*(script|style)[^>]*>.*?<\s*/\s*\1\s*>", "", t)     # buang script/style
    t = re.sub(r"(?i)<\s*/?\s*(html|head|body|h1|hr)\b[^>]*>", "", t)        # buang wrapper/h1/hr
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


# ----------------------------- Baca Google Sheet ----------------------------
def read_titles():
    """Ambil CSV dari tab JUDUL; kembalikan daftar judul (kolom B)."""
    if not SHEET_CSV_URL:
        print("[!] SHEET_CSV_URL belum diset."); return []
    try:
        r = requests.get(SHEET_CSV_URL, timeout=30)
        r.raise_for_status()
        r.encoding = "utf-8"
    except Exception as e:
        print("[!] Gagal ambil Google Sheet:", e); return []
    titles = []
    for row in csv.reader(StringIO(r.text)):
        if not row:
            continue
        # kolom B (indeks 1) = judul; bila cuma 1 kolom, pakai kolom A
        judul = (row[1] if len(row) >= 2 else row[0]).strip()
        if not judul:
            continue
        low = judul.lower()
        if low in ("judul", "title", "judul artikel"):   # lewati baris header bila ada
            continue
        titles.append(judul)
    print(f"[i] {len(titles)} judul terbaca dari Google Sheet.")
    return titles


# ----------------------------- Bangun artikel -------------------------------
def build_entries(titles, sudah_terbit):
    tgl_posts = tanggal_posts()
    tgl_id = tanggal_id()
    # judul yang belum pernah terbit, urut dari atas
    antri = [j for j in titles if key_judul(j) not in sudah_terbit]
    if not antri:
        print("[i] Semua judul sudah pernah terbit -> tidak ada yang baru.")
        return [], []

    entries, new_keys, seen = [], [], set()
    for idx, judul in enumerate(antri):
        if len(entries) >= MAX_PER_RUN:
            break
        k = key_judul(judul)
        if k in seen:
            continue
        seen.add(k)

        body = None
        if AI_ENABLED:
            if entries:
                time.sleep(AI_PACING_SEC)
            prompt = PROMPT_TEMPLATE.replace("{JUDUL}", judul).replace("{TANGGAL}", tgl_id)
            print(f"[i] Menulis artikel (AI, bisa 30-120 detik): {judul[:60]}")
            body = call_ai(ARTICLE_SYSTEM, prompt)
            body = web_safe_html(body) if body else None
        if not body:
            print(f"[!] Lewati '{judul[:40]}...' (AI gagal/menonaktif).")
            continue

        summ = re.sub(r"<[^>]+>", " ", body)
        summ = re.sub(r"\s+", " ", summ).strip()[:200]
        entries.append({
            "id": "sheet-" + k,
            "date": tgl_posts,
            "title": judul,
            "summary": summ,
            "tags": ["Artikel"],
            "source": "",
            "html": body,
        })
        new_keys.append(k)
        print(f"[i] OK: {judul[:60]}")
    print(f"[i] {len(entries)} artikel dibangun.")
    return entries, new_keys


# ----------------------------- Terbit ke website ----------------------------
def publish_to_web(entries, attempts=4):
    if not entries:
        return False
    if not WEB_REPO or not GH_PUSH_TOKEN:
        print("[i] WEB_REPO/GH_PUSH_TOKEN belum diset -> lewati penerbitan."); return False
    url = f"https://api.github.com/repos/{WEB_REPO}/contents/{WEB_POSTS_PATH}"
    headers = {"Authorization": f"Bearer {GH_PUSH_TOKEN}",
               "Accept": "application/vnd.github+json"}
    new_ids = {e["id"] for e in entries}
    for attempt in range(attempts):
        try:
            r = requests.get(url, headers=headers, timeout=20)
            if r.status_code == 200:
                data = r.json(); sha = data["sha"]
                posts = json.loads(base64.b64decode(data["content"]).decode("utf-8"))
                if not isinstance(posts, list):
                    posts = []
            else:
                sha, posts = None, []
            posts = [p for p in posts
                     if p.get("id") not in new_ids
                     and not str(p.get("id", "")).startswith("contoh")
                     and not str(p.get("id", "")).startswith("demo")]
            posts = entries + posts
            posts = posts[:MAX_POSTS]
            new_b64 = base64.b64encode(
                json.dumps(posts, ensure_ascii=False, indent=2).encode()).decode()
            payload = {"message": f"sheet: +{len(entries)} artikel ({tanggal_posts()})",
                       "content": new_b64}
            if sha:
                payload["sha"] = sha
            pr = requests.put(url, headers=headers, json=payload, timeout=25)
            if pr.status_code in (200, 201):
                print(f"[i] {len(entries)} artikel diterbitkan ke website.")
                return True
            if pr.status_code in (409, 422):
                print(f"[i] Bentrok commit ({pr.status_code}), ulangi... ({attempt+1}/{attempts})")
                time.sleep(3); continue
            print("[!] Gagal terbit:", pr.status_code, pr.text[:150]); return False
        except Exception as e:
            print("[!] Exception terbit:", e); time.sleep(4)
    print("[!] Gagal terbit setelah beberapa percobaan."); return False


# ----------------------------- Main -----------------------------------------
def main():
    print("=" * 60)
    print("SHEET ARTICLES |", datetime.now(JAKARTA).strftime("%Y-%m-%d %H:%M WIB"))
    log = load_json(LOG_FILE, [])
    if not isinstance(log, list):
        log = []
    sudah = set(log)

    titles = read_titles()
    if not titles:
        print("Selesai (tidak ada judul)."); return

    entries, new_keys = build_entries(titles, sudah)
    if not entries:
        print("Selesai (tidak ada artikel baru)."); return

    if publish_to_web(entries):
        log.extend(new_keys)
        log = list(dict.fromkeys(log))[-1000:]
        save_json(LOG_FILE, log)
        print(f"[i] Catatan diperbarui: {len(log)} judul tercatat.")
    print("Selesai.")


if __name__ == "__main__":
    main()
