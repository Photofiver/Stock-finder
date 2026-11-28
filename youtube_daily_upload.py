"""Upload only when the owner enables publishing and supplies OAuth credentials."""
import json, os, sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
scope="https://www.googleapis.com/auth/youtube.upload"
def main():
    if os.environ.get("YOUTUBE_ENABLED")!="true":
        raise SystemExit("Publishing is disabled")
    fields=["YOUTUBE_CLIENT_ID","YOUTUBE_CLIENT_SECRET","YOUTUBE_REFRESH_TOKEN"]
    if not all(os.environ.get(k) for k in fields):
        raise SystemExit("Missing YouTube OAuth secrets; no upload attempted")
    folder=Path("youtube_output")
    check_only = "--check-existing" in sys.argv
    now = datetime.now(ZoneInfo("Europe/London"))
    base_marker = "BloFin Bot Replay | Episode " + now.strftime("%Y-%m-%d")
    # Owner requested one revised edition of the already published 28 Nov episode.
    # On later dates the regular one-episode-per-day marker is unchanged.
    revision = now.strftime("%Y-%m-%d") == "2026-11-28"
    marker = base_marker + (" | first person edition" if revision else "")
    meta = {} if check_only else json.loads((folder/"metadata.json").read_text())
    if not check_only:
        if not meta["description"].startswith(base_marker+"\n"):
            raise SystemExit("Report date does not match the intended episode")
        if revision:
            meta["description"] = marker + meta["description"][len(base_marker):]
            meta["title"] = (meta["title"][:88] + " | First Person")
    privacy=os.environ.get("YOUTUBE_PRIVACY","private")
    if privacy not in ("private","unlisted","public"): raise ValueError("Invalid privacy")
    status={"privacyStatus":privacy,"selfDeclaredMadeForKids":False}
    if privacy=="public":
        now=datetime.now(ZoneInfo("Europe/London"))
        target=now.replace(hour=6,minute=0,second=0,microsecond=0)
        if now < target:
            status["privacyStatus"]="private"
            status["publishAt"]=target.astimezone(timezone.utc).isoformat().replace("+00:00","Z")
    creds=Credentials(None,refresh_token=os.environ[fields[2]],token_uri="https://oauth2.googleapis.com/token",client_id=os.environ[fields[0]],client_secret=os.environ[fields[1]],scopes=[scope, "https://www.googleapis.com/auth/youtube.readonly"])
    creds.refresh(Request())
    youtube=build("youtube","v3",credentials=creds,cache_discovery=False)
    expected = os.environ.get("YOUTUBE_CHANNEL_ID", "").strip()
    channels = youtube.channels().list(part="id,contentDetails", mine=True).execute()
    if not expected or [item["id"] for item in channels.get("items", [])] != [expected] or channels.get("nextPageToken"):
        raise SystemExit("Authorized channel does not match YOUTUBE_CHANNEL_ID")
    uploads = channels["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    recent = youtube.playlistItems().list(part="snippet,contentDetails", playlistId=uploads, maxResults=50).execute()
    for item in recent.get("items", []):
        if item["snippet"].get("description", "").startswith(marker+"\n") or (not check_only and item["snippet"]["title"] == meta["title"]):
            video_id = item["contentDetails"]["videoId"]
            details = youtube.videos().list(part="status", id=video_id).execute().get("items", [])
            current = details[0]["status"] if details else {}
            visible = current.get("privacyStatus") == "public"
            scheduled = current.get("privacyStatus") == "private" and current.get("publishAt") and datetime.fromisoformat(current["publishAt"].replace("Z", "+00:00")) == now.replace(hour=6,minute=0,second=0,microsecond=0)
            if not (visible or scheduled):
                raise SystemExit("Existing report is not public or scheduled for 06:00: https://youtu.be/" + video_id)
            print("Existing report: https://youtu.be/" + video_id)
            print("YouTube status:", current.get("privacyStatus"), current.get("publishAt", ""))
            folder.mkdir(exist_ok=True)
            (folder / "youtube_id.txt").write_text(video_id)
            if check_only: print("REPORT_EXISTS=true")
            return
    if check_only:
        print("REPORT_EXISTS=false")
        return
    req=youtube.videos().insert(part="snippet,status",body={"snippet":{"title":meta["title"],"description":meta["description"],"tags":meta["tags"],"categoryId":"22","defaultLanguage":"en"},"status":status},media_body=MediaFileUpload(str(folder/"report.mp4"),mimetype="video/mp4",resumable=True))
    response=None
    while response is None:
        _,response=req.next_chunk(num_retries=2)
    video_id=response["id"]
    print("YouTube visibility:", response.get("status", {}).get("privacyStatus"))
    print("Uploaded:", "https://youtu.be/"+video_id)
    (folder/"youtube_id.txt").write_text(video_id)
    thumbnail = folder/"thumbnail.png"
    if thumbnail.exists():
        try:
            youtube.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(str(thumbnail),mimetype="image/png")).execute()
            print("Custom thumbnail uploaded")
        except Exception as error:
            print("Custom thumbnail unavailable:", type(error).__name__)
if __name__=="__main__": main()
