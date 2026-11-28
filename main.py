"""output.mp4 ko YouTube Short ki tarah upload karta hai (GitHub secrets se refresh token)."""
import json
import os
import re
import subprocess
import sys
import time

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

TOKEN_URI = "https://oauth2.googleapis.com/token"


def get_youtube_client():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"].strip(),
        client_id=os.environ["YOUTUBE_CLIENT_ID"].strip(),
        client_secret=os.environ["YOUTUBE_CLIENT_SECRET"].strip(),
        token_uri=TOKEN_URI,
    )
    creds.refresh(Request())
    return build("youtube", "v3", credentials=creds)


def upload_video(youtube, video_path, title, description, tags):
    # UPLOAD_PRIVACY=private/unlisted se bina public kiye test kar sakte ho
    privacy = os.environ.get("UPLOAD_PRIVACY", "public").lower()
    body = {
        "snippet": {"title": title[:100], "description": description[:4900], "tags": tags, "categoryId": "24"},
        "status": {"privacyStatus": privacy if privacy in ("public", "unlisted", "private") else "public", "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(video_path, mimetype="video/mp4", chunksize=16 * 1024 * 1024, resumable=True)
    req = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    print(f"Uploading: {title}")
    response, retries = None, 0
    while response is None:
        try:
            status, response = req.next_chunk()
            if status:
                print(f"  {int(status.progress() * 100)}%")
        except HttpError as e:
            if e.resp.status in (500, 502, 503, 504) and retries < 6:
                retries += 1
                time.sleep(5 * retries)
                continue
            if e.resp.status == 403 and any(k in (e.content or b"") for k in (b"uploadLimitExceeded", b"quotaExceeded")):
                sys.exit("ERROR: YouTube upload/API quota khatam (kal dobara chalega). Video upload nahi hui.")
            raise
        except (ConnectionError, TimeoutError, OSError) as e:      # network toot gaya -> resumable upload wahin se jaari
            if retries >= 6:
                raise
            retries += 1
            print(f"  network error ({e}); retry {retries}/6")
            time.sleep(5 * retries)
    return response["id"]


HISTORY = "history.json"


def quality_gate(path, d):
    """Upload se pehle video ki jaanch: lambai, size 9:16, awaaz maujood aur sunai de, video freeze to nahi. -> (ok, [wajah])"""
    why = []
    try:
        pr = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration", "-of", "json", path],
                            capture_output=True, text=True, timeout=60)
        info = json.loads(pr.stdout or "{}")
        dur = float((info.get("format") or {}).get("duration") or 0)
        st = info.get("streams") or []
        vid = next((x for x in st if x.get("codec_type") == "video"), {})
        has_audio = any(x.get("codec_type") == "audio" for x in st)
    except Exception as e:
        print(f"[gate] ffprobe chal nahi saka ({e}) -> gate skip")
        return True, []
    if not 20 <= dur <= 59.9:
        why.append(f"lambai {dur:.1f}s (20-59.9 chahiye)")
    w, h = int(vid.get("width") or 0), int(vid.get("height") or 0)
    if not h or abs(w / h - 9 / 16) > 0.02:
        why.append(f"size {w}x{h} (9:16 nahi)")
    if not has_audio:
        why.append("awaaz track nahi")
    else:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-i", path, "-vn", "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True, timeout=180)
        m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", r.stderr)
        if m and float(m.group(1)) < -42:
            why.append(f"awaaz bohot dheemi/khali (mean {m.group(1)} dB)")
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", path, "-an", "-vf", "freezedetect=n=-55dB:d=2.5", "-f", "null", "-"], capture_output=True, text=True, timeout=300)
    frozen = sum(float(x) for x in re.findall(r"freeze_duration:\s*([\d.]+)", r.stderr))
    if dur and frozen > 0.4 * dur:
        why.append(f"video {frozen:.0f}s tak freeze")
    if len(d.get("scenes") or []) < 6:
        why.append("6 se kam scenes")
    if len(str(d.get("title", ""))) < 15:
        why.append("title bohot chhota")
    print(f"[gate] {dur:.1f}s {w}x{h} audio={has_audio} frozen={frozen:.1f}s -> {'OK' if not why else 'FAIL: ' + '; '.join(why)}")
    return not why, why


def refresh_stats(youtube):
    """Pichli videos ke views history.json mein likho (story_gen unse seekhta hai). Scope na ho to chupchap skip."""
    try:
        hist = json.load(open(HISTORY, encoding="utf-8"))
    except Exception:
        return
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 45 * 86400))
    ids = [h["video_id"] for h in hist if h.get("video_id") and h.get("date", "") >= cutoff][-50:]
    if not ids:
        return
    try:
        items = youtube.videos().list(part="statistics", id=",".join(ids)).execute().get("items", [])
    except Exception as e:
        print(f"[stats] views nahi mil sakay (shayad token mein youtube.readonly scope nahi): {str(e)[:120]}")
        return
    views = {it["id"]: int(it["statistics"].get("viewCount", 0)) for it in items}
    for h in hist:
        if h.get("video_id") in views:
            h["views"] = views[h["video_id"]]
    json.dump(hist, open(HISTORY, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[stats] {len(views)} videos ke views update")


def record_video_id(title, vid):
    try:
        hist = json.load(open(HISTORY, encoding="utf-8"))
        for h in reversed(hist):
            if h.get("title") == title and not h.get("video_id"):
                h["video_id"] = vid
                break
        json.dump(hist, open(HISTORY, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception as e:
        print(f"[stats] video_id save nahi hua: {e}")


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def with_tag(text, tag="#Shorts"):
    return text if tag.lower() in text.lower() else f"{text}\n\n{tag}"


def summary(lines):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def main():
    if not os.path.exists("output.mp4") or os.path.getsize("output.mp4") < 100_000:
        sys.exit("ERROR: output.mp4 missing/too small - not uploading")
    d = load("script.json")
    ok, why = quality_gate("output.mp4", d)
    if not ok:
        summary(["### Quality gate FAIL", *[f"- {w}" for w in why]])
        if os.environ.get("GATE_FAIL_ACTION", "skip").lower() == "private":
            os.environ["UPLOAD_PRIVACY"] = "private"                 # review ke liye private upload
            print("Gate fail -> video PRIVATE upload hogi (khud dekh kar public karein)")
        else:
            sys.exit("ERROR: quality gate fail: " + "; ".join(why) + " - upload nahi hui (agle ghante dobara koshish hogi)")
    youtube = get_youtube_client()
    refresh_stats(youtube)
    title = d.get("title", "The Real Story #Shorts")
    if "#shorts" not in title.lower():
        title = f"{title[:90]} #Shorts"
    short_id = upload_video(youtube, "output.mp4", title, with_tag(d.get("description", "")), d.get("tags", ["facts", "storytime", "animation"]))
    record_video_id(d.get("title", ""), short_id)
    url = f"https://youtube.com/shorts/{short_id}"
    print(f"Short upload done! {url}")
    summary(["### Uploaded", f"- Short: {url}"])


if __name__ == "__main__":
    main()
