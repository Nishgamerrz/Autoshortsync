"""Thin YouTube Data API v3 client built on the standard library."""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://www.googleapis.com/youtube/v3"


class YouTubeAPIError(RuntimeError):
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"YouTube API error HTTP {status}: {body[:400]}")


def _request(path: str, params: dict, access_token: str, retries: int = 3) -> dict:
    url = f"{API_BASE}/{path}?{urllib.parse.urlencode(params)}"
    last_error: Exception | None = None

    for attempt in range(retries):
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2**attempt)
                continue
            raise YouTubeAPIError(exc.code, body) from exc
        except urllib.error.URLError as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(2**attempt)
                continue
            raise YouTubeAPIError(0, str(exc)) from exc

    raise YouTubeAPIError(0, f"unreachable: {last_error}")


def get_channel(access_token: str) -> dict:
    """Return the authenticated user's channel resource."""
    data = _request(
        "channels",
        {"part": "snippet,contentDetails,statistics", "mine": "true"},
        access_token,
    )
    items = data.get("items") or []
    if not items:
        raise YouTubeAPIError(404, "No channel found for this account")
    return items[0]


def get_uploads_playlist_id(channel: dict) -> str:
    try:
        return channel["contentDetails"]["relatedPlaylists"]["uploads"]
    except KeyError as exc:
        raise YouTubeAPIError(
            400, f"Channel has no uploads playlist: {json.dumps(channel)[:300]}"
        ) from exc


def list_uploads(
    access_token: str, playlist_id: str, max_results: int = 50
) -> list[dict]:
    """List uploads newest-first, following pagination up to ``max_results``."""
    items: list[dict] = []
    page_token: str | None = None

    while True:
        params = {
            "part": "snippet,contentDetails,status",
            "playlistId": playlist_id,
            "maxResults": 50,
        }
        if page_token:
            params["pageToken"] = page_token

        data = _request("playlistItems", params, access_token)
        items.extend(data.get("items") or [])

        page_token = data.get("nextPageToken")
        if not page_token:
            break
        if max_results and len(items) >= max_results:
            break

    return items[:max_results] if max_results else items


def get_videos(access_token: str, video_ids: list[str]) -> list[dict]:
    """Fetch full video resources (metadata, stats, status) in batches of 50."""
    videos: list[dict] = []
    for start in range(0, len(video_ids), 50):
        chunk = video_ids[start : start + 50]
        data = _request(
            "videos",
            {
                "part": "snippet,contentDetails,status,statistics,localizations,topicDetails",
                "id": ",".join(chunk),
            },
            access_token,
        )
        videos.extend(data.get("items") or [])
    return videos


def list_comments(
    access_token: str, video_id: str, max_results: int = 100
) -> list[dict]:
    """Fetch top comments (relevance order). Comments may be disabled."""
    items: list[dict] = []
    page_token: str | None = None

    while True:
        params = {
            "part": "snippet",
            "videoId": video_id,
            "maxResults": 100,
            "order": "relevance",
        }
        if page_token:
            params["pageToken"] = page_token

        data = _request("commentThreads", params, access_token)
        items.extend(data.get("items") or [])

        page_token = data.get("nextPageToken")
        if not page_token or len(items) >= max_results:
            break

    return items[:max_results]


_ISO_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def parse_duration(duration: str) -> int:
    """Convert an ISO-8601 duration such as ``PT1M30S`` into seconds."""
    match = _ISO_DURATION.match(duration or "")
    if not match:
        return 0
    parts = match.groupdict()
    return (
        int(parts.get("days") or 0) * 86400
        + int(parts.get("hours") or 0) * 3600
        + int(parts.get("minutes") or 0) * 60
        + int(parts.get("seconds") or 0)
    )
