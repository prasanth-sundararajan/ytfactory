"""Minimal YouTube Data API v3 client - resumable upload + channel info."""

import logging
import os

import httpx

from app.config import CFG

log = logging.getLogger("youtube")

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"


class YouTubeError(RuntimeError):
    pass


def _token_for(slug):
    """YT_REFRESH_TOKEN_<SLUG> if set, else the shared YT_REFRESH_TOKEN."""
    if slug:
        t = os.environ.get("YT_REFRESH_TOKEN_" + slug.upper().replace("-", "_"), "")
        if t:
            return t
    return CFG.yt_refresh_token


def access_token(slug: str | None = None) -> str:
    refresh = _token_for(slug)
    if not refresh:
        raise YouTubeError("YT_REFRESH_TOKEN missing - run oauth-finish first")
    r = httpx.post(
        TOKEN_URL,
        data={
            "client_id": CFG.yt_client_id,
            "client_secret": CFG.yt_client_secret,
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    d = r.json()
    if "error" in d:
        raise YouTubeError("token refresh failed: %s %s"
                           % (d.get("error"), str(d.get("error_description", ""))[:120]))
    return d["access_token"]


def channel(token: str | None = None) -> dict:
    token = token or access_token()
    r = httpx.get(
        API + "/channels",
        params={"part": "snippet,statistics,status", "mine": "true"},
        headers={"Authorization": "Bearer " + token},
        timeout=30,
    )
    d = r.json()
    if "error" in d:
        raise YouTubeError(str(d["error"])[:200])
    items = d.get("items", [])
    if not items:
        raise YouTubeError("this Google account has no YouTube channel")
    return items[0]


def upload(path: str, title: str, description: str = "", privacy: str = "private",
           tags: list[str] | None = None, category_id: str = "27",
           slug: str | None = None, publish_at: str | None = None) -> dict:
    """Resumable upload. Returns the API response (contains the video id)."""
    if not os.path.isfile(path):
        raise YouTubeError("file not found: " + path)

    token = access_token(slug)
    size = os.path.getsize(path)

    init = httpx.post(
        "https://www.googleapis.com/upload/youtube/v3/videos",
        params={"uploadType": "resumable", "part": "snippet,status"},
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(size),
            "X-Upload-Content-Type": "video/mp4",
        },
        json={
            "snippet": {
                "title": title[:100],
                "description": description[:4900],
                "tags": (tags or [])[:30],
                "categoryId": category_id,
            },
            "status": {
                "privacyStatus": "private" if publish_at else privacy,
                **({"publishAt": publish_at} if publish_at else {}),
                "selfDeclaredMadeForKids": False,
            },
        },
        timeout=60,
    )
    if init.status_code >= 300:
        raise YouTubeError("init failed %s: %s" % (init.status_code, init.text[:300]))

    location = init.headers.get("Location")
    if not location:
        raise YouTubeError("no resumable Location header returned")

    log.info("uploading %s bytes", size)
    with open(path, "rb") as fh:
        put = httpx.put(
            location,
            content=fh.read(),
            headers={"Content-Type": "video/mp4", "Content-Length": str(size)},
            timeout=900,
        )
    if put.status_code >= 300:
        raise YouTubeError("upload failed %s: %s" % (put.status_code, put.text[:300]))
    return put.json()


def set_thumbnail(video_id: str, path: str, slug: str | None = None) -> None:
    """Needs a phone-verified channel; raises YouTubeError otherwise."""
    if not os.path.isfile(path):
        raise YouTubeError("thumbnail file not found")
    r = httpx.post(
        "https://www.googleapis.com/upload/youtube/v3/thumbnails/set",
        params={"videoId": video_id, "uploadType": "media"},
        headers={"Authorization": "Bearer " + access_token(slug),
                 "Content-Type": "image/jpeg"},
        content=open(path, "rb").read(), timeout=60)
    if r.status_code >= 300:
        raise YouTubeError("thumbnail %s: %s" % (r.status_code, r.text[:160]))
