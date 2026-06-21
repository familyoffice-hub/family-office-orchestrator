# Family Office Orchestrator

Menyatukan hasil beberapa AI agent → **1 laporan harian** → kirim ke Telegram jam 08:00 WIB → arsip.
Gratis, pola JSON + GitHub Actions (sama seperti Tools A). Tanpa database.

## Cara kerja
```
Agent menulis ke inbox.json  →  Orchestrator (08:00 WIB): saring → skor → cek risiko
   → rangkum (Gemini) jadi briefing 12 bagian → kirim Telegram → arsip → tandai processed
```
"Papan tulis bersama" = file **inbox.json** di repo ini. Semua agent menaruh hasilnya di sini.

## File
- `orchestrator.py` — program utama (jalan sekali tiap hari)
- `inbox.json` — papan tulis bersama (Output Log). Mulai `[]`
- `report_archive.json` — arsip laporan
- `archive/` — salinan tiap laporan (.md)
- `agent_push.py` — helper agar agent lain bisa menulis ke inbox
- `.github/workflows/orchestrator.yml` — jadwal harian 08:00 WIB
- `requirements.txt`, `.env.example`

## Setup (sekali)
1. Buat repo GitHub baru `family-office-orchestrator` (Private/Public). Upload semua file.
2. **Settings ▸ Secrets and variables ▸ Actions** → New repository secret:
   - `TELEGRAM_TOKEN` = token bot (boleh sama dengan tools lain)
   - `TELEGRAM_CHAT_IDS` = chat id tujuan laporan (mis. grup principal)
   - `GEMINI_API_KEY` = kunci Gemini (untuk merangkum). Boleh dikosongkan → mode ringkas tanpa AI.
3. Tab **Actions** → aktifkan workflow.

## Uji pertama (tanpa data agent)
1. Buka `.github/workflows/orchestrator.yml` → ubah `SEED_DEMO: "0"` jadi `"1"` → Commit.
2. Tab **Actions** → **Run workflow**. Cek Telegram: laporan contoh masuk.
3. Setelah yakin, kembalikan `SEED_DEMO` ke `"0"`.

## Cara agent MENULIS ke inbox (3 cara)
**Cara 1 — manual (paling mudah untuk mulai):** buka `inbox.json` di GitHub → Edit (✏️) →
tambahkan 1 baris ke dalam array:
```json
{
  "output_id": "o1",
  "agent_id": "Tax Agent",
  "title": "DJP perketat pelaporan aset HNWI",
  "summary": "Ringkasan singkat...",
  "source_url": "https://...",
  "confidentiality_level": "internal",
  "created_at": "2026-06-22T01:00:00+00:00",
  "is_processed": false
}
```
Field wajib: `agent_id, title, summary, created_at, is_processed`. Untuk data rahasia
keluarga, isi `"confidentiality_level": "confidential"` → isinya TIDAK dikirim ke AI,
hanya judul + label 🔒 yang tampil.

**Cara 2 — dari agent Python (otomatis):** di agent Anda, set env `ORCH_REPO`
(mis. `username/family-office-orchestrator`) dan `GITHUB_TOKEN`, lalu:
```python
from agent_push import push
push("Tax Agent", "Judul", "Ringkasan", source_url="https://...", confidentiality="internal")
```

**Cara 3 — Fase 2 (sambungkan Tools A):** Tools A bisa otomatis push alert prioritas tinggi
ke inbox memakai `agent_push.py`. Minta saja versi Tools A yang sudah disambungkan.

## Penjadwalan
Default 08:00 WIB (`cron: "0 1 * * *"` = 01:00 UTC). Ubah angka jam di workflow bila perlu.
Hanya 1 laporan per hari (anti kirim-ganda). Item yang sudah masuk laporan ditandai
`is_processed=true` agar tidak diulang besok.

## Guardrails (keamanan) yang sudah aktif
- Item `confidential` tidak dikirim ke AI publik (hanya judul + 🔒).
- Laporan selalu memisahkan fakta/analisis/opini dan menambahkan disclaimer.
- Sumber lemah ditandai "(perlu verifikasi)"; topik pajak/legal/investasi diberi "human review".
- Semua laporan diarsipkan; inbox menyimpan jejak.

## Naik level nanti
Saat butuh multi-user, peran (CIO/CFO/tax/legal), dan audit trail → pindahkan inbox/arsip
ke **Airtable** atau **Supabase** (struktur kolom sudah ada di blueprint). Kode pembaca
tinggal diganti sumbernya; alur lain tetap.
EOF
echo "README dibuat"