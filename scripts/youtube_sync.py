#!/usr/bin/env python3
"""AutoShortSync: back up YouTube uploads and auto-sync them to Instagram.

The script runs locally and inside GitHub Actions. It:

1. refreshes a Google OAuth access token (refresh token stored as a secret),
2. lists the channel's uploads playlist,
3. downloads every new upload with ``yt-dlp``,
4. saves video + thumbnail + metadata + comments + captions into ``backups/``,
5. optionally publishes new Shorts to Instagram as Reels,
6. commits and pushes the backup (and the sync state) to GitHub.

Required environment secrets:
    YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN
Optional:
    BACKUP_TOKEN, BACKUP_REPO, INSTAGRAM_ACCOUNT_ID, INSTAGRAM_ACCESS_TOKEN
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from google_oauth import GoogleOAuthError, refresh_access_token
from github_backup import (
    build_remote_url,
    clone_repo,
    commit_all,
    configure_git,
    current_branch,
    current_repo_slug,
    ensure_branch,
    get_or_create_release,
    list_release_assets,
    push,
    repo_info,
    upload_release_asset,
)
from instagram import (
    DEFAULT_CAPTION_TEMPLATE,
    InstagramError,
    build_caption,
    build_hashtags,
    publish_reel,
    validate_credentials,
)
from youtube_api import (
    YouTubeAPIError,
    get_channel,
    get_uploads_playlist_id,
    get_videos,
    list_comments,
    list_uploads,
    parse_duration,
)

DEFAULT_CONFIG = {
    "youtube": {
        "uploads_max_results": 50,
        # YouTube Shorts can be up to 3 minutes; tighten this if your channel
        # also posts regular vertical videos.
        "shorts_max_seconds": 180,
        "shorts_hashtag": "#shorts",
        "shorts_hashtag_required": False,
        # Back up every upload; Instagram still only receives Shorts.
        "include_regular_videos": True,
        "backup_comments": True,
        "backup_subtitles": False,
        "subtitle_langs": ["en"],
        "download_format": "bv*[height<=1080]+ba/b[height<=1080]/b",
        "download_merge_format": "mp4",
        "download_retries": 3,
        "skip_existing_downloads": True,
    },
    "backup": {
        "directory": "backups",
        "commit": True,
        "git_user_name": "AutoShortSync Bot",
        "git_user_email": "autoshortsync@users.noreply.github.com",
        "repo": "",
        "branch": "",
        "release_enable": True,
        "release_tag": "youtube-shorts",
        "release_name": "YouTube Shorts backup",
    },
    "instagram": {
        "enabled": False,
        "publish": False,
        "account_id": "",
        "access_token": "",
        "video_url_template": "",
        "caption_template": DEFAULT_CAPTION_TEMPLATE,
        "hashtags": [],
        "hashtag_source": "config+video_tags",
        "max_hashtags": 30,
        "credit": "",
        "queue_directory": "instagram_queue",
        "post_all_videos": False,
        "api_version": "v21.0",
    },
}


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def deep_merge(base: dict, override: dict | None) -> dict:
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: str) -> dict:
    user_config = {}
    if path and Path(path).exists():
        with open(path, encoding="utf-8") as handle:
            user_config = json.load(handle)
    return deep_merge(DEFAULT_CONFIG, user_config)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_text(path: Path, text: str, dry_run: bool) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_json(path: Path, payload: dict, dry_run: bool) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def is_short(video: dict, youtube_cfg: dict) -> bool:
    duration = parse_duration(video.get("contentDetails", {}).get("duration", ""))
    limit = int(youtube_cfg["shorts_max_seconds"])
    snippet = video.get("snippet") or {}
    text = " ".join(
        [
            snippet.get("title", ""),
            snippet.get("description", ""),
            " ".join(snippet.get("tags") or []),
        ]
    ).lower()
    has_hashtag = youtube_cfg["shorts_hashtag"].lower() in text
    if youtube_cfg.get("shorts_hashtag_required"):
        return duration <= limit and has_hashtag
    return duration <= limit


def find_video_file(video_dir: Path) -> Path | None:
    for suffix in (".mp4", ".mkv", ".webm"):
        candidate = video_dir / f"video{suffix}"
        if candidate.exists():
            return candidate
    return None


def download_video(video_id: str, video_dir: Path, cfg: dict) -> Path | None:
    existing = find_video_file(video_dir)
    if existing and cfg.get("skip_existing_downloads"):
        log(f"Video already downloaded: {existing.name}")
        return existing

    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--continue",
        "--no-overwrites",
        "-f",
        cfg["download_format"],
        "--merge-output-format",
        cfg["download_merge_format"],
        "--retries",
        str(cfg["download_retries"]),
        "-o",
        str(video_dir / "video.%(ext)s"),
    ]
    if cfg.get("backup_subtitles"):
        command += [
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs",
            ",".join(cfg["subtitle_langs"]),
            "--convert-subs",
            "vtt",
        ]
    command.append(f"https://www.youtube.com/watch?v={video_id}")

    log(f"Downloading https://youtu.be/{video_id} ...")
    try:
        subprocess.run(command, check=True)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "yt-dlp is not installed. Run: pip install yt-dlp"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"yt-dlp failed for {video_id} (exit {exc.returncode})")

    result = find_video_file(video_dir)
    if not result:
        raise RuntimeError(f"yt-dlp finished but produced no video file for {video_id}")
    log(f"Saved {result.name} ({result.stat().st_size / 1e6:.1f} MB)")
    return result


def download_thumbnail(video_id: str, video_dir: Path) -> Path | None:
    destination = video_dir / "thumbnail.jpg"
    if destination.exists():
        return destination
    for quality in ("maxresdefault.jpg", "sddefault.jpg", "hqdefault.jpg"):
        url = f"https://i.ytimg.com/vi/{video_id}/{quality}"
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 (AutoShortSync)"}
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read()
            if len(data) > 1000:
                destination.write_bytes(data)
                return destination
        except Exception:
            continue
    return None


def fetch_comments(video_id: str, cfg: dict, access_token: str):
    if not cfg.get("backup_comments"):
        return None
    try:
        return list_comments(access_token, video_id)
    except YouTubeAPIError as exc:
        if exc.status == 403:
            return {"error": "comments are disabled for this video"}
        raise


def queue_post(
    video: dict,
    caption: str,
    hashtags: list[str],
    video_file: Path | None,
    work_dir: Path,
    cfg: dict,
    dry_run: bool,
) -> Path | None:
    queue_dir = work_dir / cfg["queue_directory"] / video["id"]
    if dry_run:
        return queue_dir
    queue_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "video_id": video["id"],
        "url": f"https://youtube.com/shorts/{video['id']}",
        "title": (video.get("snippet") or {}).get("title", ""),
        "caption": caption,
        "hashtags": hashtags,
        "video_file": str(video_file) if video_file else None,
        "created_at": now_iso(),
    }
    (queue_dir / "post.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (queue_dir / "caption.txt").write_text(caption, encoding="utf-8")
    return queue_dir


def ensure_release_asset(
    repo: str,
    token: str,
    backup_cfg: dict,
    video_file: Path,
    video_id: str,
    dry_run: bool,
) -> str | None:
    if dry_run or not video_file:
        return None
    release = get_or_create_release(
        repo,
        token,
        backup_cfg["release_tag"],
        backup_cfg["release_name"],
        "Automatic YouTube Shorts backup created by AutoShortSync.",
    )
    assets = list_release_assets(repo, token, release["id"])
    asset_name = f"{video_id}{video_file.suffix}"
    for asset in assets:
        if asset.get("name") == asset_name:
            log(f"Release asset already exists: {asset_name}")
            return asset.get("browser_download_url")

    log(f"Uploading release asset {asset_name} ...")
    asset = upload_release_asset(repo, token, release["id"], video_file, asset_name)
    return asset.get("browser_download_url")


def process_video(
    video: dict,
    channel: dict,
    cfg: dict,
    work_dir: Path,
    access_token: str,
    dry_run: bool,
) -> dict:
    video_id = video["id"]
    snippet = video.get("snippet") or {}
    short = is_short(video, cfg["youtube"])
    video_dir = work_dir / cfg["backup"]["directory"] / "videos" / video_id
    if not dry_run:
        video_dir.mkdir(parents=True, exist_ok=True)

    log(f"Processing {video_id} - {snippet.get('title', '')!r} (short={short})")

    video_file = None
    if not dry_run:
        video_file = download_video(video_id, video_dir, cfg["youtube"])
        download_thumbnail(video_id, video_dir)
        comments = fetch_comments(video_id, cfg["youtube"], access_token)
        if comments is not None:
            write_json(video_dir / "comments.json", comments, dry_run)

    metadata = {
        "video_id": video_id,
        "url": f"https://youtube.com/shorts/{video_id}",
        "watch_url": f"https://www.youtube.com/watch?v={video_id}",
        "is_short": short,
        "duration_seconds": parse_duration(
            (video.get("contentDetails") or {}).get("duration", "")
        ),
        "snippet": snippet,
        "contentDetails": video.get("contentDetails"),
        "statistics": video.get("statistics"),
        "status": video.get("status"),
        "topicDetails": video.get("topicDetails"),
        "channel": channel,
        "fetched_at": now_iso(),
    }
    write_json(video_dir / "metadata.json", metadata, dry_run)
    write_text(video_dir / "description.txt", snippet.get("description", "") or "", dry_run)
    write_text(
        video_dir / "caption.txt",
        f"{snippet.get('title', '')}\n\n{snippet.get('description', '') or ''}".strip(),
        dry_run,
    )

    hashtags = build_hashtags(cfg["instagram"], video)
    caption, resolved = build_caption(cfg["instagram"], video, channel, hashtags)
    write_text(video_dir / "hashtags.txt", "\n".join(hashtags), dry_run)

    instagram_dir = video_dir / "instagram"
    write_text(instagram_dir / "caption.txt", caption, dry_run)
    write_text(instagram_dir / "hashtags.txt", "\n".join(hashtags), dry_run)

    instagram_cfg = cfg["instagram"]
    instagram_status = "disabled"
    if instagram_cfg["enabled"] and (short or instagram_cfg.get("post_all_videos")):
        if instagram_cfg.get("publish"):
            instagram_status = "pending"
        else:
            queue_post(video, caption, hashtags, video_file, work_dir, instagram_cfg, dry_run)
            instagram_status = "queued"
            log(f"Queued Instagram post for {video_id} (publish is off)")

    return {
        "video_id": video_id,
        "video_dir": video_dir,
        "video_file": video_file,
        "is_short": short,
        "caption": caption,
        "hashtags": hashtags,
        "instagram_status": instagram_status,
    }


def resolve_public_url(
    video_id: str,
    video_file: Path | None,
    cfg: dict,
    repo: str,
    token: str,
    dry_run: bool,
) -> tuple[str | None, str]:
    """Return ``(url, warning)`` for a publicly fetchable MP4."""
    instagram_cfg = cfg["instagram"]
    template = instagram_cfg.get("video_url_template") or ""
    release_url = None

    if cfg["backup"].get("release_enable") and repo and token and video_file:
        info = repo_info(repo, token)
        if info.get("private"):
            return None, (
                f"Backup repo '{repo}' is private, so Instagram cannot download "
                "release assets. Make it public or set instagram.video_url_template."
            )
        release_url = ensure_release_asset(
            repo, token, cfg["backup"], video_file, video_id, dry_run
        )

    if template:
        formatted = template.format(
            id=video_id,
            file=video_file.name if video_file else f"{video_id}.mp4",
            release_url=release_url or "",
        )
        if not formatted.startswith(("http://", "https://")):
            return None, (
                f"instagram.video_url_template produced a non-URL value: {formatted!r}. "
                "Use a full https:// URL."
            )
        return formatted, ""

    return release_url, "" if release_url else "No public video URL available."


def run_instagram_phase(
    pending: list[dict],
    channel: dict,
    cfg: dict,
    work_dir: Path,
    repo: str,
    token: str,
    dry_run: bool,
) -> None:
    instagram_cfg = cfg["instagram"]
    account_id = (
        os.environ.get("INSTAGRAM_ACCOUNT_ID") or instagram_cfg.get("account_id") or ""
    ).strip()
    access_token = (
        os.environ.get("INSTAGRAM_ACCESS_TOKEN")
        or instagram_cfg.get("access_token")
        or ""
    ).strip()

    if account_id and access_token and not dry_run:
        try:
            info = validate_credentials(
                account_id, access_token, instagram_cfg.get("api_version", "v21.0")
            )
            log(f"Instagram account: @{info.get('username', info.get('id', ''))}")
        except InstagramError as exc:
            log(f"Instagram credentials look invalid: {exc}")

    for item in pending:
        video_id = item["video_id"]
        video_file = item["video_file"]
        state = item["state"]

        if not account_id or not access_token:
            log(f"{video_id}: Instagram credentials missing -> queueing")
            queue_post(
                item["video"],
                item["caption"],
                item["hashtags"],
                video_file,
                work_dir,
                instagram_cfg,
                dry_run,
            )
            state["instagram"] = "queued_no_credentials"
            continue

        video_url, warning = resolve_public_url(
            video_id, video_file, cfg, repo, token, dry_run
        )
        if not video_url:
            log(f"{video_id}: {warning} -> queueing")
            queue_post(
                item["video"],
                item["caption"],
                item["hashtags"],
                video_file,
                work_dir,
                instagram_cfg,
                dry_run,
            )
            state["instagram"] = "queued_no_public_url"
            continue

        if dry_run:
            log(f"[dry-run] Would publish Reel for {video_id} -> {video_url}")
            state["instagram"] = "dry_run"
            continue

        try:
            response = publish_reel(
                account_id,
                access_token,
                video_url,
                item["caption"],
                instagram_cfg.get("api_version", "v21.0"),
            )
            state["instagram"] = "published"
            state["instagram_media_id"] = response.get("id", "")
            state["instagram_permalink"] = response.get("permalink", "")
            log(f"{video_id}: published to Instagram (media {response.get('id')})")
        except InstagramError as exc:
            state["instagram"] = "failed"
            state["instagram_error"] = str(exc)
            log(f"{video_id}: Instagram publish failed: {exc}")


def commit_and_push(
    work_dir: Path,
    cfg: dict,
    repo: str,
    token: str,
    branch: str,
    message: str,
    dry_run: bool,
) -> bool:
    if dry_run or not cfg["backup"].get("commit"):
        return False
    configure_git(work_dir, cfg["backup"]["git_user_name"], cfg["backup"]["git_user_email"])
    if not commit_all(work_dir, message):
        log("Nothing to commit")
        return False
    remote_url = build_remote_url(repo, token) if repo else None
    push(work_dir, remote_url, branch)
    log(f"Pushed backup to {repo or 'origin'} ({branch})")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="AutoShortSync")
    parser.add_argument("--config", default="config.json", help="Path to config JSON")
    parser.add_argument("--dry-run", action="store_true", help="Plan only, write nothing")
    parser.add_argument("--backfill", action="store_true", help="Re-process all uploads")
    parser.add_argument("--list", action="store_true", help="List uploads and exit")
    parser.add_argument("--limit", type=int, default=0, help="Max uploads to process")
    args = parser.parse_args()

    cfg = load_config(args.config)

    client_id = os.environ.get("YOUTUBE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET", "").strip()
    refresh_token = os.environ.get("YOUTUBE_REFRESH_TOKEN", "").strip()
    missing = [
        name
        for name, value in (
            ("YOUTUBE_CLIENT_ID", client_id),
            ("YOUTUBE_CLIENT_SECRET", client_secret),
            ("YOUTUBE_REFRESH_TOKEN", refresh_token),
        )
        if not value
    ]
    if missing:
        log("ERROR: missing required secrets: " + ", ".join(missing))
        log("See README.md section '1. Google OAuth setup'.")
        return 2

    token = os.environ.get("BACKUP_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    repo = (
        os.environ.get("BACKUP_REPO")
        or cfg["backup"].get("repo")
        or current_repo_slug()
    ).strip()

    log("Refreshing Google access token ...")
    try:
        access_token, _ = refresh_access_token(client_id, client_secret, refresh_token)
    except GoogleOAuthError as exc:
        log(f"ERROR: {exc}")
        return 2

    log("Fetching YouTube channel ...")
    try:
        channel = get_channel(access_token)
        uploads_playlist = get_uploads_playlist_id(channel)
    except YouTubeAPIError as exc:
        log(f"ERROR: {exc}")
        return 2

    channel_title = (channel.get("snippet") or {}).get("title", "")
    log(f"Channel: {channel_title} ({channel.get('id')})")

    uploads = list_uploads(access_token, uploads_playlist, cfg["youtube"]["uploads_max_results"])
    log(f"Found {len(uploads)} upload(s) in the uploads playlist")

    if args.list:
        for item in uploads:
            snippet = (item.get("snippet") or {})
            content = item.get("contentDetails") or {}
            print(
                f"{content.get('videoId')}  {snippet.get('publishedAt', '')[:10]}  "
                f"{snippet.get('title', '')[:70]}"
            )
        return 0

    # Resolve where the backup lives.
    work_dir = Path.cwd()
    current_slug = current_repo_slug()
    if repo and repo != current_slug:
        log(f"Cloning backup repo {repo} ...")
        work_dir = clone_repo(repo, token, Path(".autoshortsync") / "backup-repo")

    branch = (
        cfg["backup"].get("branch")
        or os.environ.get("BACKUP_BRANCH")
        or os.environ.get("GITHUB_REF_NAME")
        or current_branch(work_dir)
        or "main"
    )
    ensure_branch(work_dir, branch)

    state_path = work_dir / cfg["backup"]["directory"] / "processed_videos.json"
    state = {}
    if state_path.exists() and not args.dry_run:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log("WARNING: processed_videos.json is corrupt; starting fresh")
            state = {}

    to_process = [
        item
        for item in uploads
        if args.backfill
        or (item.get("contentDetails") or {}).get("videoId") not in state
    ]
    if args.limit:
        to_process = to_process[: args.limit]
    log(f"{len(to_process)} new upload(s) to process")

    videos = []
    if to_process:
        ids = [(item.get("contentDetails") or {}).get("videoId") for item in to_process]
        videos = get_videos(access_token, ids)

    pending_instagram: list[dict] = []
    processed_count = 0

    for video in videos:
        if not video:
            continue
        video_id = video["id"]
        if not is_short(video, cfg["youtube"]) and not cfg["youtube"].get(
            "include_regular_videos"
        ):
            log(f"Skipping non-short video {video_id}")
            continue

        result = process_video(video, channel, cfg, work_dir, access_token, args.dry_run)
        processed_count += 1

        entry = {
            "processed_at": now_iso(),
            "title": (video.get("snippet") or {}).get("title", ""),
            "type": "short" if result["is_short"] else "video",
            "instagram": result["instagram_status"],
        }
        if result["video_file"]:
            entry["video_file"] = str(
                result["video_file"].relative_to(work_dir)
                if result["video_file"].is_relative_to(work_dir)
                else result["video_file"]
            )
        state[video_id] = entry

        if result["instagram_status"] == "pending":
            pending_instagram.append(
                {
                    "video": video,
                    "video_id": video_id,
                    "video_file": result["video_file"],
                    "caption": result["caption"],
                    "hashtags": result["hashtags"],
                    "state": entry,
                }
            )

    if not args.dry_run:
        write_json(state_path, state, dry_run=False)
        if processed_count:
            commit_and_push(
                work_dir,
                cfg,
                repo,
                token,
                branch,
                f"Back up {processed_count} new YouTube upload(s)",
                dry_run=False,
            )
        else:
            log("No new uploads to back up")

    if pending_instagram:
        run_instagram_phase(
            pending_instagram, channel, cfg, work_dir, repo, token, args.dry_run
        )
        if not args.dry_run:
            write_json(state_path, state, dry_run=False)
            commit_and_push(
                work_dir,
                cfg,
                repo,
                token,
                branch,
                "Update Instagram sync state",
                dry_run=False,
            )

    log("Done")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
