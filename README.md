# AutoShortSync

Automatically backs up **every YouTube upload** and syncs it to **Instagram**.

When you upload a video to YouTube, a GitHub Actions workflow:

1. authenticates to YouTube with **Google OAuth** (refresh token, no manual login),
2. downloads the video with `yt-dlp`,
3. saves the video, thumbnail, metadata, description, tags, comments and subtitles
   into `backups/`,
4. commits everything to GitHub,
5. generates an **Instagram caption with all hashtags**, and either
   **auto-publishes the Reel** or drops a ready-to-post file into
   `instagram_queue/`.

```
YouTube upload
      │
      ▼
GitHub Actions (every 15 min)
      │  Google OAuth refresh token → YouTube Data API
      ▼
yt-dlp download ──► backups/videos/<id>/video.mp4
      │              backups/videos/<id>/thumbnail.jpg
      │              backups/videos/<id>/metadata.json
      │              backups/videos/<id>/comments.json
      │              backups/videos/<id>/instagram/caption.txt
      ▼
git commit + push ──► GitHub (backup repo / release assets)
      │
      ▼
Instagram Reel (auto-publish)  OR  instagram_queue/<id>/caption.txt (manual)
```

---

## Quick start

1. **Google OAuth setup** — section 1 below (creates the YouTube refresh token).
2. **Add the GitHub secrets** — `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`,
   `YOUTUBE_REFRESH_TOKEN` (and optionally `INSTAGRAM_ACCOUNT_ID`,
   `INSTAGRAM_ACCESS_TOKEN`).
3. **Configure Instagram** — edit `config.json` → `instagram` section
   (section 3 has a field-by-field "what to put in" table).
4. Push to GitHub. The workflow in
   [.github/workflows/youtube-shorts-backup.yml](.github/workflows/youtube-shorts-backup.yml)
   runs every 15 minutes and on every manual "Run workflow".

---

## 1. Google OAuth setup (YouTube authentication)

The workflow never asks you to log in. You do the OAuth consent **once on your
own computer**, keep the resulting refresh token as a GitHub secret, and the
workflow exchanges it for short-lived access tokens forever.

### 1.1 Create a Google Cloud project

1. Go to <https://console.cloud.google.com/> and create a project
   (e.g. `autoshortsync`).
2. **APIs & Services → Library → search "YouTube Data API v3" → Enable.**

### 1.2 Configure the OAuth consent screen

1. **APIs & Services → OAuth consent screen**.
2. Choose **External**, fill in the app name and your email.
3. Scopes: click **Add or remove scopes** and select
   `.../auth/youtube.readonly` (read-only YouTube access).
4. **Important:** set the app's **Publishing status** to **In production**
   (bottom of the OAuth consent screen page). If it stays in **Testing** mode,
   Google expires the refresh token after **7 days** and the workflow stops.
   Read-only YouTube access is low risk; if Google asks you to verify the app,
   complete the verification.

### 1.3 Create the OAuth client

1. **APIs & Services → Credentials → Create Credentials → OAuth client ID**.
2. Application type: **Web application**.
3. Under **Authorized redirect URIs**, add:
   ```
   http://localhost:8080/callback            (for get_refresh_token.py)
   http://localhost:8080/google/callback     (for the one-page auth server)
   ```
4. Copy the **Client ID** and **Client secret**.

### 1.4 Mint the refresh token (one time, on your computer)

```bash
pip install yt-dlp
python scripts/get_refresh_token.py \
  --client-id YOUR_CLIENT_ID \
  --client-secret YOUR_CLIENT_SECRET
```

It opens your browser, you pick the channel and click **Allow**. The script then
prints:

```
YOUTUBE_CLIENT_ID      = ...
YOUTUBE_CLIENT_SECRET  = ...
YOUTUBE_REFRESH_TOKEN  = ...
```

**Easiest option — one-page authorization for Google *and* Instagram:**

```bash
python scripts/auth_server.py
```

Open <http://localhost:8080>, paste your Google client ID/secret and Meta app
ID/secret, then click **Authorize Google / YouTube** and **Authorize Instagram**.
The page prints every value to paste into GitHub Secrets — including the
Instagram account ID and the Page access token. The raw authorization links are
shown on the page too (also at `/links`):

```
Google : https://accounts.google.com/o/oauth2/v2/auth?client_id=...&redirect_uri=http%3A%2F%2Flocalhost%3A8080%2Fgoogle%2Fcallback&scope=...&access_type=offline&prompt=consent
Meta   : https://www.facebook.com/v21.0/dialog/oauth?client_id=...&redirect_uri=http%3A%2F%2Flocalhost%3A8080%2Finstagram%2Fcallback&scope=instagram_basic%2Cinstagram_content_publish%2Cpages_show_list%2Cpages_read_engagement
```

### 1.5 Set up the Meta / Instagram app (for auto-publish)

1. Go to <https://developers.facebook.com/> → **My Apps → Create App**
   (type "Business" or "Other").
2. Add the **Instagram Graph API** product (and **Facebook Login** if you want
   the clickable login flow).
3. In **Facebook Login → Settings → Valid OAuth Redirect URIs**, add:
   ```
   http://localhost:8080/instagram/callback
   ```
4. Make sure your Instagram account is a **Business/Creator** account and is
   linked to a **Facebook Page** (Instagram app → Settings → Account → Linked
   accounts, or Page Settings → Instagram).
5. Add yourself under **App Roles → Testers** (or make yourself admin) so you
   can authorize while the app is in development mode.
6. The token needs these permissions: `instagram_basic`,
   `instagram_content_publish`, `pages_show_list`, `pages_read_engagement`.
   Meta may ask for an app review before other accounts can use it — that does
   not affect your own account.

### 1.6 Add the GitHub secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
| --- | --- |
| `YOUTUBE_CLIENT_ID` | Client ID from 1.3 |
| `YOUTUBE_CLIENT_SECRET` | Client secret from 1.3 |
| `YOUTUBE_REFRESH_TOKEN` | Refresh token printed in 1.4 |
| `INSTAGRAM_ACCOUNT_ID` | Instagram Business/Creator ID from the auth page |
| `INSTAGRAM_ACCESS_TOKEN` | Page access token from the auth page |
| `BACKUP_REPO` | Optional: `you/youtube-backups` |
| `BACKUP_TOKEN` | Optional: GitHub PAT with `repo` scope |

> The refresh token stays valid until you revoke access at
> <https://myaccount.google.com/permissions>. If you ever revoke it, run 1.4
> again and update the secret.

---

## 2. Where the backup is stored

**Option A — this repository (default).** Everything is committed to
`backups/` on the current branch. No extra setup.

**Option B — a separate backup repository** (recommended for large channels):

1. Create an empty GitHub repo with a `README.md` (e.g. `you/youtube-backups`).
   Make it **public** if you want Instagram auto-publishing to work (section 3).
2. Create a PAT with the **repo** scope: GitHub → Settings → Developer settings →
   Personal access tokens → Tokens (classic) → `repo`.
3. Add secrets `BACKUP_REPO` = `you/youtube-backups` and `BACKUP_TOKEN` = the PAT.
4. Set `"repo": "you/youtube-backups"` in `config.json` (or just rely on the
   `BACKUP_REPO` secret).

The backup also creates/updates a GitHub **Release** called `youtube-shorts`
and attaches each MP4 as a release asset. That public asset URL is what
Instagram downloads the video from.

**Large videos?** Consider Git LFS so your repo does not explode:

```bash
git lfs install
git lfs track "backups/videos/**"
git add .gitattributes && git commit -m "Track backup videos with LFS"
```

---

## 3. Instagram auto-sync — what to put in

### 3.1 Prerequisites (only needed for auto-publish)

Instagram only allows automatic posting through the **Instagram Graph API**,
which requires the items below. The easiest way to get the token and account ID
is the one-page auth server from section 1.5:

```bash
python scripts/auth_server.py   # then click "Authorize Instagram"
```

- an **Instagram Professional account** (Business or Creator), not a personal one;
- a **Facebook Page** connected to that Instagram account;
- a **Facebook app** with the **Instagram Graph API** product added;
- a **long-lived Page access token** with the permissions
  `instagram_content_publish`, `instagram_basic`, `pages_read_engagement`;
- the **Instagram Business Account ID** (find it with
  `GET https://graph.facebook.com/v21.0/{page-id}?fields=instagram_business_account`
  — the `id` inside `instagram_business_account`).

If you do not want to set that up, leave `"publish": false`. AutoShortSync will
still generate every caption + hashtag file and put it in `instagram_queue/` for
copy-paste posting.

### 3.2 The Instagram config — field by field

Edit `config.json` → `"instagram"`:

| Field | What to put in | Example |
| --- | --- | --- |
| `enabled` | `true` to generate Instagram captions for every new Short | `true` |
| `publish` | `true` = post the Reel automatically; `false` = only write files to `instagram_queue/` | `false` |
| `account_id` | Your Instagram Business/Creator account ID. You can also put it in the `INSTAGRAM_ACCOUNT_ID` secret instead | `"17841405793187414"` |
| `access_token` | Long-lived Facebook Page access token. You can also use the `INSTAGRAM_ACCESS_TOKEN` secret | `"EAA..."` |
| `caption_template` | The caption text. Supports `{title}`, `{description}`, `{url}`, `{channel}`, `{date}`, `{hashtags}`, `{credit}` | see below |
| `hashtags` | Your hashtags as a list. `#` is optional; it is added automatically | `["shorts", "comedy", "fyp"]` |
| `hashtag_source` | Where hashtags come from: `config`, `video_tags`, `text`, or combinations joined with `+` | `"config+video_tags"` |
| `max_hashtags` | Maximum hashtags to include (Instagram limit is 30) | `30` |
| `credit` | Optional line appended to the caption (e.g. your handle) | `"🎥 @mychannel"` |
| `video_url_template` | Public MP4 URL template, e.g. `"https://cdn.example.com/{id}.mp4"`. Leave empty to use the GitHub release asset | `""` |
| `queue_directory` | Folder for ready-to-post files when `publish` is `false` | `"instagram_queue"` |
| `post_all_videos` | `true` to also post regular (non-Short) videos | `false` |
| `api_version` | Facebook Graph API version | `"v21.0"` |

### 3.3 Caption template placeholders

| Placeholder | Becomes |
| --- | --- |
| `{title}` | YouTube video title |
| `{description}` | Full YouTube description |
| `{url}` | `https://youtu.be/<videoId>` |
| `{channel}` | Your channel name |
| `{date}` | Upload date (`YYYY-MM-DD`) |
| `{hashtags}` | The generated hashtag block |
| `{credit}` | The `credit` value |

Default template:

```
{title}

{description}

🎬 Full video on YouTube: {url}
{hashtags}
```

If your template has no `{hashtags}`, the hashtags are appended automatically.

### 3.4 Hashtags — what to put in

- Put **5–15 of your own hashtags** in `instagram.hashtags`. They are added to
  every Short.
- `hashtag_source: "config+video_tags"` also pulls the **video's YouTube tags**,
  so each Short gets topical hashtags automatically.
- Add `"text"` too (e.g. `"config+video_tags+text"`) to also pick up `#tags`
  written in your title/description.
- The list is deduplicated (case-insensitive) and capped at `max_hashtags`
  (max 30).
- Good mix: 3–5 niche + 3–5 mid-size + 3–5 broad. Example set:

```json
"hashtags": [
  "shorts", "reels", "viral", "explore", "fyp", "trending",
  "youtube", "ytshorts", "reelsvideo", "shortsvideo",
  "comedy", "funny", "meme", "gaming", "tech", "food", "travel"
]
```

### 3.5 Auto-publish vs. queue

- `publish: false` (default): every new Short gets
  `instagram_queue/<videoId>/caption.txt` (full caption + hashtags) and
  `post.json`. Copy the caption into Instagram and post manually.
- `publish: true`: the Reel is published through the Graph API using the
  GitHub release asset as the video source. Requires the prerequisites in 3.1
  and a **public** backup repo (or `video_url_template`).
- Failures never lose work: if credentials or a public URL are missing, the
  post is queued and the state file records why
  (`queued_no_credentials`, `queued_no_public_url`, `failed`).

### 3.6 Instagram secrets

| Secret | Value |
| --- | --- |
| `INSTAGRAM_ACCOUNT_ID` | Instagram Business/Creator ID (optional; overrides config) |
| `INSTAGRAM_ACCESS_TOKEN` | Long-lived Page access token (optional; overrides config) |

---

## 4. GitHub Actions workflow

`.github/workflows/youtube-shorts-backup.yml`:

- runs **every 15 minutes** (`*/15 * * * *`) and on **workflow_dispatch**;
- checks that the Google secrets exist and fails with a clear message if not;
- installs `yt-dlp`, runs `python scripts/youtube_sync.py`;
- the script commits and pushes the backup itself, so no extra steps are needed;
- `permissions: contents: write` lets the workflow push to `backups/`;
- `concurrency` prevents two runs from racing.

**Manual run:** Actions → "YouTube Shorts Backup & Instagram Sync" →
**Run workflow** → optionally tick **backfill** (re-process every upload) or
**dry-run** (print the plan, download and post nothing).

---

## 5. Full config reference

### `youtube`

| Field | Default | Meaning |
| --- | --- | --- |
| `uploads_max_results` | `50` | How many recent uploads to scan |
| `shorts_max_seconds` | `180` | Max duration counted as a Short (Shorts can be 3 min) |
| `shorts_hashtag` | `"#shorts"` | Hashtag used for detection |
| `shorts_hashtag_required` | `false` | Require the hashtag as well as the duration |
| `include_regular_videos` | `true` | Back up **all** uploads; Instagram still only receives Shorts |
| `backup_comments` | `true` | Save top comments |
| `backup_subtitles` | `false` | Download subtitles/CC (`subtitle_langs`) |
| `download_format` | `bv*[height<=1080]+ba/b[height<=1080]/b` | yt-dlp format selector |
| `download_merge_format` | `mp4` | Container for the merged file |
| `download_retries` | `3` | yt-dlp retries |
| `skip_existing_downloads` | `true` | Reuse the file if it is already backed up |

### `backup`

| Field | Default | Meaning |
| --- | --- | --- |
| `directory` | `"backups"` | Backup folder |
| `commit` | `true` | Commit + push after each run |
| `git_user_name` / `git_user_email` | bot identity | Commit author |
| `repo` | `""` | Separate backup repo (`owner/repo`); empty = this repo |
| `branch` | `""` | Branch to push; empty = current/default branch |
| `release_enable` | `true` | Create/update the release with MP4 assets |
| `release_tag` / `release_name` | `youtube-shorts` | Release to attach videos to |

---

## 6. Backup folder structure

```
backups/
├── processed_videos.json          # state: which uploads were handled
└── videos/
    └── <videoId>/
        ├── video.mp4              # the actual upload
        ├── thumbnail.jpg
        ├── metadata.json          # snippet, stats, status, channel, duration
        ├── description.txt        # raw YouTube description
        ├── caption.txt            # title + description
        ├── hashtags.txt
        ├── comments.json          # top comments (if enabled)
        └── instagram/
            ├── caption.txt        # ready-to-post caption with hashtags
            └── hashtags.txt
instagram_queue/
└── <videoId>/
    ├── post.json                  # caption, hashtags, video path
    └── caption.txt
```

---

## 7. Run locally

```bash
pip install -r requirements.txt
export YOUTUBE_CLIENT_ID=...
export YOUTUBE_CLIENT_SECRET=...
export YOUTUBE_REFRESH_TOKEN=...

python scripts/youtube_sync.py --dry-run     # plan only
python scripts/youtube_sync.py --list        # list uploads
python scripts/youtube_sync.py               # real run
python scripts/youtube_sync.py --backfill    # re-process everything
```

---

## 8. Troubleshooting

| Problem | Fix |
| --- | --- |
| `missing required secrets` | Add the three `YOUTUBE_*` secrets (section 1.6) |
| `Token refresh failed (HTTP 400)` | Refresh token revoked or expired — re-run `get_refresh_token.py` and update the secret. If the consent screen was in Testing mode, the token expires after 7 days; set it to **In production** |
| Redirect URI rejected in Google Cloud | Use application type **Desktop app**, or register `http://localhost:8080/callback` exactly as shown |
| `yt-dlp is not installed` | `pip install -r requirements.txt` |
| `No channel found for this account` | The OAuth account has no channel; pick the right Google account |
| Instagram post queued, not published | Check `INSTAGRAM_*` credentials and that the backup repo is public |
| Shorts longer than 60 s are skipped | Raise `youtube.shorts_max_seconds` (default is 180) |
| Private videos are not downloaded | `yt-dlp` cannot fetch private uploads; public/unlisted work fine |
| Regular vertical videos get posted to Instagram | Set `shorts_hashtag_required: true` and lower `shorts_max_seconds` to 60 |
| Workflow does not run on schedule | Scheduled workflows only run on the **default branch**; also check Actions are enabled |
| `429` from YouTube | Quota exceeded; reduce `uploads_max_results` |

---

## 9. Security notes

- Never commit `config.json` secrets, `.env`, or token files — secrets live in
  GitHub Secrets / environment variables only.
- The YouTube refresh token grants read access to your channel. Store it as a
  secret and treat it like a password.
- Use a dedicated backup repo and a fine-grained PAT with the minimum scope
  (`contents: write` only) if you use a separate repo.
- The workflow only requests the read-only YouTube scope.
