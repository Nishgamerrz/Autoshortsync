"""Instagram caption generation and (optional) Graph API Reels publishing."""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

GRAPH_BASE = "https://graph.facebook.com"
DEFAULT_API_VERSION = "v21.0"
MAX_CAPTION = 2200
MAX_HASHTAGS = 30

DEFAULT_CAPTION_TEMPLATE = (
    "{title}\n\n"
    "{description}\n\n"
    "🎬 Full video on YouTube: {url}\n"
    "{hashtags}"
)


class InstagramError(RuntimeError):
    """Raised when the Instagram Graph API rejects a request."""


def normalize_hashtag(tag: str) -> str:
    """Turn ``"# Travel!"`` into ``Travel`` (letters, numbers, underscore only)."""
    tag = (tag or "").strip().lstrip("#")
    tag = re.sub(r"[^\w]", "", tag, flags=re.UNICODE)
    return tag


def build_hashtags(config: dict, video: dict) -> list[str]:
    """Build a deduplicated, length-capped hashtag list for a video."""
    source = config.get("hashtag_source", "config+video_tags")
    raw: list[str] = []

    if "config" in source:
        raw.extend(config.get("hashtags") or [])

    snippet = video.get("snippet") or {}

    if "video_tags" in source:
        raw.extend(snippet.get("tags") or [])

    if "text" in source:
        text = f"{snippet.get('title', '')}\n{snippet.get('description', '')}"
        raw.extend(re.findall(r"#([0-9A-Za-z_]+)", text))

    limit = int(config.get("max_hashtags", MAX_HASHTAGS))
    seen: set[str] = set()
    hashtags: list[str] = []

    for candidate in raw:
        normalized = normalize_hashtag(candidate)
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        hashtags.append(f"#{normalized}")
        if len(hashtags) >= limit:
            break

    return hashtags


def _safe_format(template: str, values: dict) -> str:
    def replace(match: re.Match) -> str:
        key = match.group(1)
        return str(values.get(key, match.group(0)))

    return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", replace, template)


def build_caption(
    config: dict, video: dict, channel: dict, hashtags: list[str]
) -> tuple[str, dict]:
    """Render the Instagram caption and return it with the resolved values.

    Placeholders: ``{title} {description} {url} {channel} {date} {hashtags}
    {credit}``.
    """
    template = config.get("caption_template") or DEFAULT_CAPTION_TEMPLATE
    snippet = video.get("snippet") or {}

    values = {
        "title": snippet.get("title", ""),
        "description": (snippet.get("description", "") or "").strip(),
        "url": f"https://youtu.be/{video['id']}",
        "channel": (channel.get("snippet") or {}).get("title", ""),
        "date": (snippet.get("publishedAt", "") or "")[:10],
        "hashtags": " ".join(hashtags),
        "credit": config.get("credit", "") or "",
    }

    def render(description: str, tags: list[str]) -> str:
        current = dict(values)
        current["description"] = description
        current["hashtags"] = " ".join(tags)
        text = _safe_format(template, current)
        if "{hashtags}" not in template and tags:
            text = text.rstrip() + "\n\n" + " ".join(tags)
        return text

    description = values["description"]
    caption = render(description, hashtags)

    # Keep the caption inside Instagram's 2200 character limit. Prefer keeping
    # the description and trimming it before dropping whole sections.
    if len(caption) > MAX_CAPTION:
        without_description = render("", hashtags)
        budget = MAX_CAPTION - len(without_description) - 2
        if budget > 30:
            caption = render(description[:budget].rstrip() + "…", hashtags)
        else:
            caption = without_description

    if len(caption) > MAX_CAPTION:
        for keep in range(len(hashtags) - 1, -1, -1):
            caption = render("", hashtags[:keep])
            if len(caption) <= MAX_CAPTION:
                break

    if len(caption) > MAX_CAPTION:
        caption = caption[: MAX_CAPTION - 1].rstrip() + "…"

    values["description"] = description
    values["hashtags"] = " ".join(hashtags)
    return caption, values


def _graph_post(url: str, payload: dict, access_token: str) -> dict:
    body = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    return _graph_send(request)


def _graph_get(url: str, payload: dict, access_token: str) -> dict:
    separator = "&" if "?" in url else "?"
    full = f"{url}{separator}{urllib.parse.urlencode(payload)}"
    request = urllib.request.Request(
        full, headers={"Authorization": f"Bearer {access_token}"}
    )
    return _graph_send(request)


def _graph_send(request: urllib.request.Request) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            error = json.loads(body).get("error", {})
            message = error.get("message", body)
        except json.JSONDecodeError:
            message = body
        raise InstagramError(f"Instagram Graph API error: {message}") from exc


def validate_credentials(account_id: str, access_token: str, api_version: str) -> dict:
    """Fetch basic account info so bad tokens fail fast with a clear message."""
    url = f"{GRAPH_BASE}/{api_version}/{account_id}"
    return _graph_get(
        url,
        {"fields": "id,username,account_type", "access_token": access_token},
        access_token,
    )


def publish_reel(
    account_id: str,
    access_token: str,
    video_url: str,
    caption: str,
    api_version: str = DEFAULT_API_VERSION,
) -> dict:
    """Publish a Reel via the Instagram Graph API.

    ``video_url`` must be a publicly reachable MP4 URL. Returns the API
    response from the publish call (contains ``id`` and ``permalink``).
    """
    base = f"{GRAPH_BASE}/{api_version}/{account_id}"

    create = _graph_post(
        f"{base}/media",
        {
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            "share_to_feed": "true",
            "access_token": access_token,
        },
        access_token,
    )
    creation_id = create.get("id")
    if not creation_id:
        raise InstagramError(f"No creation id returned: {create}")

    status = ""
    for _ in range(60):  # up to 5 minutes
        info = _graph_get(
            f"{GRAPH_BASE}/{api_version}/{creation_id}",
            {"fields": "status_code,status", "access_token": access_token},
            access_token,
        )
        status = info.get("status_code", "")
        if status == "FINISHED":
            break
        if status in ("ERROR", "EXPIRED"):
            raise InstagramError(f"Media container failed: {info}")
        time.sleep(5)
    else:
        raise InstagramError(f"Timed out waiting for media container (status={status})")

    return _graph_post(
        f"{base}/media_publish",
        {"creation_id": creation_id, "access_token": access_token},
        access_token,
    )
