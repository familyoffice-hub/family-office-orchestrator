# -*- coding: utf-8 -*-
"""
FAMILY OFFICE ORCHESTRATOR
==========================
Membaca "papan tulis bersama" (inbox.json) berisi output dari beberapa agent,
lalu: saring -> skor -> cek risiko/compliance -> rangkum (Gemini) jadi briefing
12 bagian -> kirim ke Telegram -> arsipkan -> tandai sudah diproses.

Jalan terjadwal via GitHub Actions (cron) jam 08:00 WIB. Tanpa database; semua JSON.

GUARDRAIL: baris ber-flag "confidential" TIDAK dikirim ke AI publik (hanya judul yang
tampil + label 🔒), agar data sensitif keluarga tidak bocor ke LLM.
"""

import os
import re
import json
import time
import html
import traceback
from datetime import datetime, timezone, timedelta

import requests
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ----------------------------- Pengaturan -----------------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_IDS = [c.strip() for c in os.getenv("TELEGRAM_CHAT_IDS", "").split(",") if c.strip()]

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
AI_MODEL = os.getenv("AI_MODEL", "gemini-2.5-flash-lite")
AI_ENABLED = bool(GEMINI_API_KEY)

INBOX_FILE = os.getenv("INBOX_FILE", "inbox.json")
ARCHIVE_JSON = os.getenv("ARCHIVE_JSON", "report_archive.json")
REPORT_DIR = os.getenv("REPORT_DIR", "archive")
MAX_ITEMS = int(os.getenv("MAX_ITEMS", "60"))          # batas item yang diproses
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "30"))  # ambil item sejauh ini ke belakang
SEED_DEMO = os.getenv("SEED_DEMO", "0") == "1"          # isi contoh bila inbox kosong

# Penerbitan ke WEBSITE (GitHub Pages). Artikel harian ditulis AI lalu di-push ke repo website.
WEB_REPO = os.getenv("WEB_REPO", "").strip()            # mis. "familyoffice-hub/website"
WEB_POSTS_PATH = os.getenv("WEB_POSTS_PATH", "posts.json")
GH_PUSH_TOKEN = os.getenv("GH_PUSH_TOKEN", "").strip()  # PAT fine-grained, Contents RW ke repo website
MAX_POSTS = int(os.getenv("MAX_POSTS", "60"))

JAKARTA = timezone(timedelta(hours=7))

# Kata kunci untuk skor lokal (pra-urut sebelum dikirim ke AI)
IMPORTANCE = {
    5: ["fraud", "hack", "depeg", "bank failure", "sanction", "tax investigation", "market crash", "collapse", "ppatk"],
    4: ["ojk", "djp", "kemenkeu", "bank indonesia", "regulation", "regulasi", "pajak", "tax", "beneficial ownership", "crs", "fatca"],
    3: ["trust", "estate", "succession", "suksesi", "warisan", "family office", "blackrock", "vanguard", "jp morgan", "goldman", "ubs"],
    2: ["fed", "interest rate", "suku bunga", "inflation", "inflasi", "yield", "usd", "gold", "emas", "bitcoin", "crypto", "portfolio", "allocation"],
}

# ----------------------------- JSON helpers ---------------------------------

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("[!] Gagal simpan", path, e)

def _now():
    return datetime.now(timezone.utc).isoformat()

# ----------------------------- Telegram -------------------------------------

def _split(text, limit=3800):
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            if cur:
                chunks.append(cur)
            cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        chunks.append(cur)
    return chunks

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_IDS:
        print("[!] TELEGRAM belum diisi. Pesan tidak terkirim.\n", message[:500])
        return False
    ok = True
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for chat_id in TELEGRAM_CHAT_IDS:
        for part in _split(message):
            sent = False
            for attempt in range(4):
                try:
                    r = requests.post(url, data={"chat_id": chat_id, "text": part,
                                                 "parse_mode": "HTML",
                                                 "disable_web_page_preview": "true"}, timeout=20)
                    if r.status_code == 200:
                        sent = True; break
                    if r.status_code == 429:
                        w = 5
                        try: w = r.json().get("parameters", {}).get("retry_after", 5)
                        except Exception: pass
                        print(f"[i] Telegram 429, tunggu {w}s..."); time.sleep(int(w) + 1); continue
                    print("[!] Telegram error", r.status_code, r.text[:150]); break
                except Exception as e:
                    print("[!] Telegram exception", e); time.sleep(3)
            if not sent: ok = False
            time.sleep(0.4)
    return ok

# ----------------------------- Gemini ---------------------------------------

def call_ai(system, user, max_tokens=2000):
    if not AI_ENABLED:
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{AI_MODEL}:generateContent"
    body = {"system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.4,
                                 "thinkingConfig": {"thinkingBudget": 0}}}
    for attempt in range(4):
        try:
            r = requests.post(url, headers={"x-goog-api-key": GEMINI_API_KEY,
                                            "Content-Type": "application/json"}, json=body, timeout=60)
            if r.status_code == 200:
                cands = r.json().get("candidates", [])
                if not cands: return None
                parts = cands[0].get("content", {}).get("parts", [])
                txt = "".join(p.get("text", "") for p in parts).strip()
                return txt or None
            if r.status_code == 429:
                print("[i] Gemini 429, tunggu 25s..."); time.sleep(25); continue
            if r.status_code in (500, 502, 503, 504):
                w = 8 * (attempt + 1); print(f"[i] Gemini sibuk {r.status_code}, tunggu {w}s..."); time.sleep(w); continue
            print("[!] Gemini error", r.status_code, r.text[:200]); return None
        except Exception as e:
            print("[!] Gemini exception", e); time.sleep(5)
    return None

# ----------------------------- Skor lokal -----------------------------------

def local_importance(row):
    text = (row.get("title", "") + " " + row.get("summary", "")).lower()
    score = 0
    for weight, words in IMPORTANCE.items():
        if any(w in text for w in words):
            score = max(score, weight)
    # baris terbaru sedikit lebih tinggi
    return score

# ----------------------------- Prompt AI ------------------------------------

SUMMARIZER_SYSTEM = (
    "Anda Chief of Staff sebuah Family Office di Indonesia. Rangkum kumpulan output agent "
    "menjadi SATU 'Daily Family Office Intelligence Brief' Bahasa Indonesia: profesional, "
    "ringkas, tajam, actionable untuk founder/principal/CFO/CIO/investment committee.\n"
    "ATURAN: pisahkan FAKTA/ANALISIS/OPINI/REKOMENDASI; jangan mengarang angka; tandai "
    "'(perlu verifikasi)' bila sumber lemah; JANGAN beri nasihat finansial/pajak/hukum "
    "langsung — tambahkan disclaimer; beri prioritas High/Medium/Low. Gunakan HTML Telegram "
    "sederhana (<b>...</b>), JANGAN pakai #, *, atau tabel."
)

def summarizer_user(items_text, tanggal):
    return (
        f"Tanggal: {tanggal}. Berikut output agent (mentah) yang perlu dirangkum:\n\n"
        f"{items_text}\n\n"
        "Susun briefing dengan bagian berjudul tebal persis berikut (lewati bagian yang tak ada datanya):\n"
        "<b>1. Executive Summary</b> (5–7 poin terpenting)\n"
        "<b>2. Top 5 Signals</b> (judul; ringkasan; kenapa penting; dampak; pajak/legal; risiko; peluang; recommended action; sumber)\n"
        "<b>3. Macro & Market</b>\n<b>4. Portfolio & Allocation</b>\n"
        "<b>5. Tax, Legal & Compliance Watch</b> (apa berubah; siapa terdampak; good/neutral/bad; implikasi; perlu advisor?)\n"
        "<b>6. Trust, Estate & Succession</b>\n"
        "<b>7. Risk Alerts</b> (risiko; terdampak; High/Med/Low; mitigasi; eskalasi?)\n"
        "<b>8. Investment Opportunities</b> (thesis; suitability; upside; risiko; likuiditas; horizon; layak IC?)\n"
        "<b>9. Governance & Principal Agenda</b>\n<b>10. Advisory & Content Ideas</b>\n"
        "<b>11. Recommended Actions Today</b> (aksi; siapa; prioritas; deadline; approval?)\n"
        "<b>12. Founder-Level Insight</b> (1–3 insight strategis)\n"
        "Total ringkas (≈500–700 kata). Akhiri dengan disclaimer singkat."
    )

RISK_SYSTEM = (
    "Anda Risk & Compliance reviewer Family Office. Perbaiki draft laporan agar aman."
)

def risk_user(draft):
    return (
        "Periksa & perbaiki draft berikut: (1) klaim tanpa sumber -> '(perlu verifikasi)'; "
        "(2) nasihat finansial/pajak/hukum langsung -> perhalus + disclaimer; (3) sumber lemah "
        "(akun anonim/rumor/X tanpa sumber) -> beri label; (4) potensi risiko hukum/reputasi/"
        "compliance/konflik kepentingan -> beri flag; (5) pastikan pemisahan FAKTA vs ANALISIS vs OPINI; "
        "(6) topik pajak/legal/investasi/estate -> tambahkan 'disarankan human review'. "
        "Kembalikan HANYA draft versi aman (HTML Telegram sederhana), tanpa komentar tambahan.\n\nDRAFT:\n" + draft
    )

# ----------------------------- Bangun laporan -------------------------------

def esc(s):
    return html.escape(s or "")

def build_items_text(rows):
    """Susun teks untuk AI. Baris confidential TIDAK dikirim isinya (hanya judul + label)."""
    lines = []
    for r in rows:
        conf = (r.get("confidentiality_level", "internal") or "internal").lower()
        agent = r.get("agent_id", "?")
        title = r.get("title", "").strip()
        if conf == "confidential":
            lines.append(f"- [{agent}] 🔒 (CONFIDENTIAL — isi tidak dibagikan ke AI) {title}")
        else:
            summ = (r.get("summary") or r.get("raw_output") or "").strip()
            src = r.get("source_url", "")
            lines.append(f"- [{agent}] {title} :: {summ} {('(sumber: '+src+')') if src else ''}")
    return "\n".join(lines)

def fallback_report(rows, tanggal):
    """Laporan tanpa AI: daftar terstruktur per kategori importance."""
    rows = sorted(rows, key=local_importance, reverse=True)
    out = [f"🗞️ <b>DAILY FAMILY OFFICE INTELLIGENCE BRIEF</b> — {tanggal}",
           "<i>(mode ringkas tanpa AI)</i>", "", "<b>Sorotan utama:</b>"]
    for r in rows[:10]:
        conf = (r.get("confidentiality_level", "internal") or "internal").lower()
        mark = "🔒 " if conf == "confidential" else ""
        body = "" if conf == "confidential" else (" — " + esc((r.get("summary") or "")[:160]))
        out.append(f"• {mark}<b>{esc(r.get('title',''))}</b> [{esc(r.get('agent_id','?'))}]{body}")
    out.append("\n— Disclaimer: bukan nasihat investasi/pajak/hukum. Perlu verifikasi & human review.")
    return "\n".join(out)

def _clean_ai(text):
    """Buang pembungkus ```html / ``` yang kadang ditambahkan AI."""
    if not text:
        return text
    t = text.strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)   # buka pagar di awal
    t = re.sub(r"\s*```$", "", t)            # tutup pagar di akhir
    return t.strip()

def _tg_safe_html(text):
    """Telegram hanya mendukung <b><i><u><s><a><code><pre>. Ubah/buang tag lain
    (mis. <ul><li><h1><p><br>) agar tidak kena error 'can't parse entities'."""
    if not text:
        return text
    t = text
    t = re.sub(r"(?i)<\s*li\s*>", "• ", t)          # <li> -> bullet
    t = re.sub(r"(?i)<\s*/\s*li\s*>", "\n", t)       # </li> -> newline
    t = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", t)      # <br> -> newline
    t = re.sub(r"(?i)</?\s*(ul|ol|p|h[1-6]|div|span|strong|em|table|tr|td|th|thead|tbody)\b[^>]*>",
               "", t)                                 # buang tag tak didukung
    # <strong>/<em> sudah dibuang di atas; pulihkan jadi bold/italic bila perlu:
    # (kita biarkan teks polos agar aman)
    t = re.sub(r"\n{3,}", "\n\n", t)                 # rapikan baris kosong berlebih
    return t.strip()

def build_report(rows, tanggal):
    has_conf = any((r.get("confidentiality_level","internal") or "").lower()=="confidential" for r in rows)
    if AI_ENABLED:
        items_text = build_items_text(rows)
        draft = call_ai(SUMMARIZER_SYSTEM, summarizer_user(items_text, tanggal), max_tokens=2200)
        if draft:
            safe = call_ai(RISK_SYSTEM, risk_user(draft), max_tokens=2200) or draft
            safe = _tg_safe_html(_clean_ai(safe))
            header = f"🗞️ <b>DAILY FAMILY OFFICE INTELLIGENCE BRIEF</b> — {tanggal}\n"
            if has_conf:
                header += "🔒 <b>Mengandung item CONFIDENTIAL — internal only</b>\n"
            return header + "\n" + safe
    # fallback
    return fallback_report(rows, tanggal)

# ----------------------------- Demo seed ------------------------------------

DEMO_ROWS = [
    {"agent_id": "Tax Agent", "title": "DJP perketat pelaporan aset HNWI",
     "summary": "Kemenkeu/DJP menambah kewajiban pelaporan aset bagi wajib pajak kaya; berdampak ke tax planning & beneficial ownership.",
     "source_url": "https://example.com/djp", "confidentiality_level": "internal", "category": "Tax Planning"},
    {"agent_id": "Benjamin Agent", "title": "The Fed sinyal tahan suku bunga lebih lama",
     "summary": "Yield US Treasury naik; tekanan ke USD/IDR dan obligasi.",
     "source_url": "https://example.com/fed", "confidentiality_level": "internal", "category": "Macro"},
    {"agent_id": "Risk Agent", "title": "Peringatan penipuan deepfake menargetkan keluarga kaya",
     "summary": "Modus deepfake/AI meniru tokoh untuk menipu investor; perlu protokol verifikasi.",
     "source_url": "https://example.com/risk", "confidentiality_level": "internal", "category": "Risk"},
    {"agent_id": "Charlie Agent", "title": "Catatan rebalancing portfolio keluarga Q3",
     "summary": "Detail alokasi internal keluarga.",
     "source_url": "", "confidentiality_level": "confidential", "category": "Portfolio"},
]

# ----------------------------- Penerbit Website -----------------------------

ARTICLE_SYSTEM = ("Anda jurnalis keuangan untuk audiens ritel Indonesia. Tulis ringkas, jelas, "
                  "netral, dan informatif dalam Bahasa Indonesia. Bukan nasihat investasi.")

def _article_user(items_text, tanggal):
    return (
        f"Tulis SATU artikel ringkasan pasar harian (sekitar 400-550 kata) untuk tanggal {tanggal}, "
        f"berdasarkan poin-poin di bawah. Gunakan HANYA tag HTML: <p>, <h3>, <ul>, <li>, <b>. "
        f"Mulai dengan 1 paragraf pembuka, lalu 2-4 subjudul <h3> (mis. Makro & Pasar, Saham, "
        f"Crypto & DeFi, Yang Perlu Diperhatikan) masing-masing dengan paragraf singkat. "
        f"JANGAN mengarang angka/kutipan yang tidak ada. JANGAN beri nasihat investasi (ini informasi, "
        f"bukan rekomendasi). JANGAN tulis judul utama (h1) atau disclaimer — itu ditambahkan otomatis.\n\n"
        f"POIN:\n{items_text}"
    )

def _web_safe_html(t):
    """Izinkan tag aman untuk web; buang sisanya. Input dari AI kita sendiri."""
    if not t:
        return ""
    t = _clean_ai(t)
    t = re.sub(r"(?is)<\s*(script|style)[^>]*>.*?<\s*/\s*\1\s*>", "", t)   # buang script/style
    t = re.sub(r"(?i)<\s*/?\s*(html|head|body|h1|h2)\b[^>]*>", "", t)       # buang wrapper & h1/h2
    return t.strip()

def build_article(rows, tanggal, today_key):
    """Buat 1 artikel dari item NON-confidential. Return entry dict atau None."""
    public = [r for r in rows if (r.get("confidentiality_level","internal") or "").lower() != "confidential"]
    if not public:
        print("[i] Tidak ada item publik (semua confidential) -> tidak menerbitkan artikel.")
        return None
    items_text = build_items_text(public)
    body = None
    if AI_ENABLED:
        body = call_ai(ARTICLE_SYSTEM, _article_user(items_text, tanggal), max_tokens=1800)
        body = _web_safe_html(body) if body else None
    if not body:
        # fallback tanpa AI: daftar ringkas
        lis = "".join(f"<li><b>{html.escape(r.get('title',''))}</b> — {html.escape((r.get('summary') or '')[:160])}</li>"
                      for r in public[:10])
        body = f"<p>Ringkasan pasar untuk {html.escape(tanggal)} berdasarkan pemantauan otomatis.</p><ul>{lis}</ul>"
    # tags dari kategori
    tags, seen = [], set()
    for r in public:
        c = (r.get("category") or "").strip()
        if c and c.lower() not in seen:
            seen.add(c.lower()); tags.append(c)
    tags = tags[:4] or ["Pasar"]
    top = public[0].get("title", "Catatan Pasar")
    summary = f"{len(public)} sorotan hari ini, termasuk: {top}."
    return {"id": today_key, "date": tanggal, "title": f"Catatan Pasar Harian — {tanggal}",
            "summary": summary[:200], "tags": tags, "html": body}

def publish_to_web(entry):
    """Tambahkan artikel ke posts.json di repo website via GitHub API (1 commit)."""
    if not WEB_REPO or not GH_PUSH_TOKEN:
        print("[i] WEB_REPO/GH_PUSH_TOKEN belum diset -> lewati penerbitan ke website.")
        return False
    import base64
    url = f"https://api.github.com/repos/{WEB_REPO}/contents/{WEB_POSTS_PATH}"
    headers = {"Authorization": f"Bearer {GH_PUSH_TOKEN}", "Accept": "application/vnd.github+json"}
    try:
        r = requests.get(url, headers=headers, timeout=20)
        if r.status_code == 200:
            data = r.json(); sha = data["sha"]
            posts = json.loads(base64.b64decode(data["content"]).decode("utf-8"))
            if not isinstance(posts, list):
                posts = []
        else:
            sha = None; posts = []
        # buang contoh & artikel dengan id sama, taruh yang baru paling depan
        posts = [p for p in posts if p.get("id") not in (entry["id"], "contoh-2026-06-22")
                 and not str(p.get("id","")).startswith("contoh")]
        posts.insert(0, entry)
        posts = posts[:MAX_POSTS]
        new_b64 = base64.b64encode(json.dumps(posts, ensure_ascii=False, indent=2).encode()).decode()
        payload = {"message": f"artikel: {entry['title']}", "content": new_b64}
        if sha:
            payload["sha"] = sha
        pr = requests.put(url, headers=headers, json=payload, timeout=20)
        if pr.status_code in (200, 201):
            print(f"[i] Artikel diterbitkan ke website: {entry['title']}")
            return True
        print("[!] Gagal terbit ke website:", pr.status_code, pr.text[:150]); return False
    except Exception as e:
        print("[!] Exception terbit ke website:", e); return False

# ----------------------------- Main -----------------------------------------

def run_once():
    print("=" * 60)
    now = datetime.now(JAKARTA)
    tanggal = now.strftime("%d %b %Y")
    print("Orchestrator jalan:", now.strftime("%Y-%m-%d %H:%M WIB"))

    inbox = load_json(INBOX_FILE, [])
    if not isinstance(inbox, list):
        inbox = []
    if not inbox and SEED_DEMO:
        print("[i] Inbox kosong -> isi contoh (SEED_DEMO).")
        for r in DEMO_ROWS:
            r = dict(r); r["output_id"] = "o" + format(int(time.time()*1000) % 10_000_000, "x")
            r["created_at"] = _now(); r["is_processed"] = False
            inbox.append(r); time.sleep(0.01)
        save_json(INBOX_FILE, inbox)

    # anti kirim ganda: 1 laporan per hari
    archive = load_json(ARCHIVE_JSON, [])
    if not isinstance(archive, list):   # jaga-jaga bila file berisi {} atau tipe lain
        archive = []
    today_key = now.strftime("%Y-%m-%d")
    if any(a.get("report_date") == today_key for a in archive):
        print("[i] Laporan hari ini sudah dibuat. Berhenti.")
        return

    # ambil item belum diproses & masih dalam rentang waktu
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    def fresh(r):
        if r.get("is_processed"):
            return False
        try:
            return datetime.fromisoformat(r.get("created_at")) >= cutoff
        except Exception:
            return True
    pending = [r for r in inbox if fresh(r)]
    # dedup judul mirip
    seen_titles, uniq = set(), []
    for r in sorted(pending, key=local_importance, reverse=True):
        t = (r.get("title", "") or "").strip().lower()
        if t and t in seen_titles:
            continue
        seen_titles.add(t); uniq.append(r)
    uniq = uniq[:MAX_ITEMS]

    if not uniq:
        print("[i] Tidak ada item baru untuk dilaporkan.")
        # tetap catat agar tidak dicek berulang? Tidak; cukup berhenti.
        return

    print(f"[i] {len(uniq)} item diproses (AI={'on' if AI_ENABLED else 'off'}).")
    report = build_report(uniq, tanggal)

    ok = send_telegram(report)

    # Terbitkan artikel harian ke website (hanya item non-confidential).
    if WEB_REPO and GH_PUSH_TOKEN:
        try:
            entry = build_article(uniq, tanggal, today_key)
            if entry:
                publish_to_web(entry)
        except Exception as e:
            print("[!] Penerbitan website gagal:", e)

    # arsip + tandai processed
    rid = "r" + now.strftime("%Y%m%d")
    os.makedirs(REPORT_DIR, exist_ok=True)
    try:
        with open(os.path.join(REPORT_DIR, today_key + ".md"), "w", encoding="utf-8") as f:
            f.write(re.sub("<[^>]+>", "", report))
    except Exception as e:
        print("[!] Gagal tulis arsip md:", e)

    archive.append({"report_id": rid, "report_date": today_key, "report_title": f"Daily Brief {tanggal}",
                    "delivery_channel": "telegram", "delivery_status": "sent" if ok else "failed",
                    "sent_at": _now(), "item_count": len(uniq),
                    "confidentiality_level": "confidential" if any(
                        (r.get("confidentiality_level","internal") or "").lower()=="confidential" for r in uniq) else "internal"})
    archive = archive[-180:]
    save_json(ARCHIVE_JSON, archive)

    processed_ids = {r.get("output_id") for r in uniq}
    for r in inbox:
        if r.get("output_id") in processed_ids:
            r["is_processed"] = True
            r["report_id"] = rid
    inbox = inbox[-1000:]
    save_json(INBOX_FILE, inbox)
    print(f"[i] Selesai. Terkirim={ok}, diarsip={rid}.")

def main():
    try:
        run_once()
    except Exception:
        print("[!] Error orchestrator:")
        traceback.print_exc()

if __name__ == "__main__":
    main()
