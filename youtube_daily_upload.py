"""Upload only when the owner enables publishing and supplies OAuth credentials."""
import json, os
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
    meta=json.loads((folder/"metadata.json").read_text())
    privacy=os.environ.get("YOUTUBE_PRIVACY","private")
    if privacy not in ("private","unlisted","public"): raise ValueError("Invalid privacy")
    status={"privacyStatus":privacy,"selfDeclaredMadeForKids":False}
    if privacy=="public":
        now=datetime.now(ZoneInfo("Europe/London"))
        target=now.replace(hour=6,minute=0,second=0,microsecond=0)
        if now>=target:
            raise SystemExit("Missed 06:00 UK publication deadline; no late upload attempted")
        status["privacyStatus"]="private"
        status["publishAt"]=target.astimezone(timezone.utc).isoformat().replace("+00:00","Z")
    creds=Credentials(None,refresh_token=os.environ[fields[2]],token_uri="https://oauth2.googleapis.com/token",client_id=os.environ[fields[0]],client_secret=os.environ[fields[1]],scopes=[scope])
    creds.refresh(Request())
    youtube=build("youtube","v3",credentials=creds,cache_discovery=False)
    req=youtube.videos().insert(part="snippet,status",body={"snippet":{"title":meta["title"],"description":meta["description"],"tags":meta["tags"],"categoryId":"22","defaultLanguage":"en"},"status":status},media_body=MediaFileUpload(str(folder/"report.mp4"),mimetype="video/mp4",resumable=True))
    response=None
    while response is None:
        _,response=req.next_chunk(num_retries=2)
    video_id=response["id"]
    print("Uploaded:", "https://youtu.be/"+video_id)
    (folder/"youtube_id.txt").write_text(video_id)
if __name__=="__main__": main()
