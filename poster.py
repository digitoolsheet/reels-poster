"""
poster.py — auto-post Reels multi-akun (FB Page + IG) dari antrean Google Sheet.

Dijalankan GitHub Actions tiap jam. Untuk tiap akun aktif di accounts.json:
  - hitung berapa slot posting yang sudah "jatuh tempo" hari ini (jam WIB <= sekarang)
  - bandingkan dengan jumlah yang sudah terposting hari ini (dari sheet)
  - kalau kurang -> posting 1 video
Jadi kalau jadwal GitHub telat / satu run gagal, run berikutnya otomatis menyusul.

Manual:
  python poster.py                       # semua akun, sesuai jadwal
  python poster.py --account girlsitt --platform fb --force   # paksa 1 posting

Secrets per akun: FB_PAGE_TOKEN_<KEY>   (KEY = nama akun huruf besar, mis. GIRLSITT)
Secret bersama  : GOOGLE_SERVICE_ACCOUNT_JSON
"""
import argparse
import io
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

GV = "v23.0"
BASE = f"https://graph.facebook.com/{GV}"
TZ = ZoneInfo("Asia/Jakarta")
HERE = os.path.dirname(os.path.abspath(__file__))
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Judul kolom sheet queue (sama dengan sync_queue.gs)
C = {
    "drive": "ID File Drive", "link": "Link Affiliate",
    "cap_fb": "Caption FB", "cap_ig": "Caption IG", "ai": "Konten AI",
    "st_fb": "Status FB", "id_fb": "ID Video FB", "t_fb": "Waktu Posting FB",
    "st_ig": "Status IG", "id_ig": "ID Media IG", "t_ig": "Waktu Posting IG",
    "note": "Catatan",
}
KEYS = {"fb": ("st_fb", "id_fb", "t_fb"), "ig": ("st_ig", "id_ig", "t_ig")}


def log(acc, plat, msg):
    print(f"[{acc}/{plat}] {msg}", flush=True)


# ======================= Google =======================

def google_clients():
    info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return (build("sheets", "v4", credentials=creds, cache_discovery=False),
            build("drive", "v3", credentials=creds, cache_discovery=False))


def col_letter(i):
    s, i = "", i + 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


class Queue:
    def __init__(self, sheets, sheet_id, tab):
        self.sheets, self.id, self.tab = sheets, sheet_id, tab
        rows = sheets.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"{tab}!A1:Z").execute().get("values", [])
        if not rows:
            raise RuntimeError("sheet queue kosong / tab tidak ada")
        self.idx = {h.strip(): i for i, h in enumerate(rows[0])}
        miss = [v for v in C.values() if v not in self.idx]
        if miss:
            raise RuntimeError(f"kolom tidak ada: {miss}")
        self.rows = rows[1:]

    def get(self, row, key):
        i = self.idx[C[key]]
        return row[i].strip() if i < len(row) and row[i] else ""

    def posted_today(self, plat):
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        t_key = KEYS[plat][2]
        return sum(1 for r in self.rows if self.get(r, t_key).startswith(today))

    def is_ai(self, row):
        return self.get(row, "ai").lower() in ("ya", "yes", "y")

    def pick(self, plat):
        """FB: baris belum diposting yang punya link + caption FB.
           IG: baris yang sudah 'posted' di FB tapi belum di IG."""
        for n, r in enumerate(self.rows, start=2):
            if not self.get(r, "drive"):
                continue
            if plat == "fb":
                if not self.get(r, "st_fb") and self.get(r, "link") and self.get(r, "cap_fb"):
                    return n, r
            else:
                if (not self.get(r, "st_ig") and self.get(r, "st_fb").lower() == "posted"
                        and self.get(r, "cap_ig")):
                    return n, r
        return None, None

    def write(self, row_no, values):
        data = [{"range": f"{self.tab}!{col_letter(self.idx[C[k]])}{row_no}", "values": [[v]]}
                for k, v in values.items()]
        self.sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=self.id, body={"valueInputOption": "RAW", "data": data}).execute()


def download(drive, file_id, path):
    req = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
    with io.FileIO(path, "wb") as fh:
        dl, done = MediaIoBaseDownload(fh, req, chunksize=8 * 1024 * 1024), False
        while not done:
            _, done = dl.next_chunk()


# ======================= ffmpeg =======================

def has_audio(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                          "-show_entries", "stream=index", "-of", "csv=p=0", path],
                         capture_output=True, text=True)
    return bool(out.stdout.strip())


def differentiate(src, dst, watermark):
    """Speed acak 2–6% + watermark kecil. Tanpa zoom/crop. Gagal -> video asli."""
    if os.environ.get("DIFFERENTIATE", "1") == "0" or not shutil.which("ffmpeg"):
        return src
    speed = round(random.uniform(1.02, 1.06), 3)
    font = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    vf = f"setpts=PTS/{speed}"
    if watermark:
        wm = watermark.replace(":", r"\:").replace("'", "")
        vf += (f",drawtext=fontfile={font}:text='{wm}':fontcolor=white@0.55:fontsize=h/42:"
               f"x=w-tw-24:y=h-th-60:shadowcolor=black@0.4:shadowx=2:shadowy=2")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-vf", vf]
    cmd += (["-af", f"atempo={speed}", "-c:a", "aac", "-b:a", "128k"] if has_audio(src) else ["-an"])
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", dst]
    return dst if subprocess.run(cmd, capture_output=True).returncode == 0 else src


# ======================= Meta API =======================

def _upload(url, token, path):
    with open(path, "rb") as f:
        r = requests.post(url, data=f, timeout=600, headers={
            "Authorization": f"OAuth {token}", "offset": "0",
            "file_size": str(os.path.getsize(path))})
    if not r.ok:
        raise RuntimeError(f"upload gagal: {r.text[:200]}")


def post_fb(page_id, token, path, caption):
    r = requests.post(f"{BASE}/{page_id}/video_reels",
                      data={"upload_phase": "start", "access_token": token}, timeout=60)
    if not r.ok:
        raise RuntimeError(f"FB start gagal: {r.text[:200]}")
    vid, url = r.json()["video_id"], r.json()["upload_url"]
    _upload(url, token, path)
    r = requests.post(f"{BASE}/{page_id}/video_reels", timeout=60, params={
        "access_token": token, "video_id": vid, "upload_phase": "finish",
        "video_state": "PUBLISHED", "description": caption})
    if not r.ok:
        raise RuntimeError(f"FB finish gagal: {r.text[:200]}")
    for _ in range(30):
        st = requests.get(f"{BASE}/{vid}", timeout=30, params={
            "fields": "status", "access_token": token}).json().get("status", {})
        if st.get("video_status") == "ready":
            break
        if st.get("video_status") == "error":
            raise RuntimeError("FB processing error")
        time.sleep(10)
    return vid


def post_ig(ig_id, token, path, caption, is_ai=False):
    data = {"media_type": "REELS", "upload_type": "resumable", "caption": caption,
            "share_to_feed": "true", "access_token": token}
    if is_ai:
        data["is_ai_generated"] = "true"  # label "AI info" (Instagram Content Publishing API)
    r = requests.post(f"{BASE}/{ig_id}/media", timeout=60, data=data)
    if not r.ok:
        raise RuntimeError(f"IG container gagal: {r.text[:200]}")
    cid = r.json()["id"]
    _upload(r.json().get("uri") or f"https://rupload.facebook.com/ig-api-upload/{GV}/{cid}",
            token, path)
    for _ in range(60):
        code = requests.get(f"{BASE}/{cid}", timeout=30, params={
            "fields": "status_code", "access_token": token}).json().get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"IG processing {code}")
        time.sleep(10)
    else:
        raise RuntimeError("IG timeout processing")
    r = requests.post(f"{BASE}/{ig_id}/media_publish", timeout=60,
                      data={"creation_id": cid, "access_token": token})
    if not r.ok:
        raise RuntimeError(f"IG publish gagal: {r.text[:200]}")
    return r.json()["id"]


# ======================= Konten AI =======================
# FB Page API belum punya parameter label AI, jadi untuk FB:
#   1) pengungkapan tertulis di caption, 2) metadata standar IPTC di file video
#      (sinyal yang dipakai Meta untuk memasang label "AI info" otomatis — best effort).
# IG memakai parameter resmi is_ai_generated=true.

DEFAULT_AI_TEXT = "🤖 AI-generated video"
IPTC_AI = "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"


def add_ai_disclosure(caption, text):
    if not text or text.lower() in caption.lower():
        return caption
    lines = caption.split("\n", 1)  # link tetap di baris pertama
    return lines[0] + "\n" + text + ("\n" + lines[1] if len(lines) > 1 else "")


def tag_ai_metadata(path):
    if not shutil.which("exiftool"):
        return
    subprocess.run(["exiftool", "-overwrite_original", "-q",
                    f"-XMP-iptcExt:DigitalSourceType={IPTC_AI}", path],
                   capture_output=True)


# ======================= Runner =======================

def run_one(acc, cfg, plat, sheets, drive, force=False):
    """Return True kalau OK/tidak perlu posting, False kalau error."""
    if plat == "ig" and not cfg.get("ig_user_id"):
        return True
    token = os.environ.get(f"FB_PAGE_TOKEN_{acc.upper()}")
    if not token:
        log(acc, plat, "secret token tidak ada, dilewati")
        return True

    q = Queue(sheets, cfg["queue_sheet_id"], cfg.get("queue_tab", "queue"))
    if not force:
        now_h = datetime.now(TZ).hour
        due = sum(1 for h in cfg.get(f"{plat}_hours", []) if h <= now_h)
        done = q.posted_today(plat)
        if done >= due:
            return True
        log(acc, plat, f"jatuh tempo {due}, sudah {done} -> posting")

    row_no, row = q.pick(plat)
    if not row_no:
        log(acc, plat, "antrean kosong")
        return True

    st_k, id_k, t_k = KEYS[plat]
    tmp = tempfile.mkdtemp()
    try:
        raw = os.path.join(tmp, "raw.mp4")
        download(drive, q.get(row, "drive"), raw)
        video = differentiate(raw, os.path.join(tmp, "out.mp4"), cfg.get("watermark", ""))
        ai = bool(cfg.get("all_ai")) or q.is_ai(row)
        if ai:
            tag_ai_metadata(video)
        if plat == "fb":
            cap = q.get(row, "cap_fb")
            if ai:
                cap = add_ai_disclosure(cap, cfg.get("ai_disclosure", DEFAULT_AI_TEXT))
            mid = post_fb(cfg["fb_page_id"], token, video, cap)
        else:
            mid = post_ig(cfg["ig_user_id"], token, video, q.get(row, "cap_ig"), ai)
        q.write(row_no, {st_k: "posted", id_k: mid,
                         t_k: datetime.now(TZ).strftime("%Y-%m-%d %H:%M")})
        log(acc, plat, f"posted (baris {row_no})")
        return True
    except Exception as e:
        q.write(row_no, {st_k: "error", "note": f"{plat}: {str(e)[:180]}"})
        log(acc, plat, f"ERROR baris {row_no}: {str(e)[:120]}")
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", help="jalankan 1 akun saja")
    ap.add_argument("--platform", choices=["fb", "ig"], help="fb / ig saja")
    ap.add_argument("--force", action="store_true", help="abaikan jadwal, paksa 1 posting")
    a = ap.parse_args()

    accounts = json.load(open(os.path.join(HERE, "accounts.json"), encoding="utf-8"))
    sheets, drive = google_clients()
    ok = True
    for acc, cfg in accounts.items():
        if a.account and acc != a.account:
            continue
        if not cfg.get("enabled", True) and not a.account:
            continue
        for plat in ([a.platform] if a.platform else ["fb", "ig"]):
            try:
                ok &= run_one(acc, cfg, plat, sheets, drive, a.force)
            except Exception as e:  # error 1 akun tidak menghentikan akun lain
                log(acc, plat, f"ERROR: {str(e)[:120]}")
                ok = False
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
