# Drop source videos here

Chat attachments cap out around 25–30 MB, which a phone video passes in about
thirty seconds. Consumer file-sharing hosts (Drive, Dropbox, WeTransfer,
iCloud) are all blocked from the session's network, so the working route into
a session is this repository.

Files in this folder are deliberately **not** gitignored, unlike media
everywhere else in the repo.

## Sending a video

```bash
git clone https://github.com/ksowell7-eng/Video-Editing-.git
cd Video-Editing-
git checkout claude/vertical-sports-video-pipeline-1pl8tp

cp ~/Desktop/my-clip.mp4 uploads/
git add uploads/my-clip.mp4
git commit -m "Add clip for editing"
git push
```

Or use GitHub Desktop: clone the repo, drop the file into `uploads/`, commit,
push. No command line needed.

**Not** the GitHub website's "Add file → Upload files" button — that caps at
25 MB, which is the problem you are working around.

## Limits

| Route | Cap |
|---|---|
| `git push` | **100 MB per file** (a warning over 50 MB, but it still goes) |
| GitHub web upload | 25 MB — too small, avoid |
| Chat attachment | ~25–30 MB — too small, avoid |

A 1–2 minute phone video is normally 30–150 MB. If yours is over 100 MB,
re-encode before pushing:

```bash
ffmpeg -i big.mov -c:v libx264 -crf 20 -preset slow -c:a aac -b:a 160k smaller.mp4
```

CRF 20 is visually near-transparent and typically cuts the size by more than
half. Push the smallest file that still looks right to you — everything after
this gets re-encoded again, so starting from a clean master matters.

## Keeping the repo from bloating

Git keeps every version of a binary forever. One or two clips is fine. If
sending video becomes routine, turn on Git LFS:

```bash
git lfs install
git lfs track "uploads/*.mp4"
git add .gitattributes && git commit -m "Track uploads with LFS"
```

## After pushing

Say so, and the file gets pulled into the session. First things run:

```bash
python -m pipeline inspect uploads/my-clip.mp4     # duration, size, fps, audio
python -m pipeline sheet --video uploads/my-clip.mp4  # stamped frames to point at
```
