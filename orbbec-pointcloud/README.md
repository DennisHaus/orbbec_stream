# Orbbec Astra 2 → Cloudflare R2 point-cloud snapshots

```
Astra 2 ──USB──> snapshotter.py (Windows PC) ──upload──> R2 bucket ──read──> viewer (GitHub Pages)
```

Every 15 minutes (on the quarter hour) the PC captures a colour point cloud at the
camera's full depth resolution: a depth snapshot (the median of 10 frames, so it's
cleaner than a single frame) plus a JPEG of the colour image mapped onto the depth
image, so every depth point gets its colour. The script uses the largest depth mode
the camera accepts; if Windows refuses a combination, it tries a smaller colour mode
first and only then a smaller depth mode. Both files go to R2, and old ones are
thinned out:

| Age              | Kept                 |
|------------------|----------------------|
| under 1 day      | every snapshot       |
| 1 day – 1 week   | one per hour         |
| 1 week – 1 month | one per day          |
| over 1 month     | one per week         |

"Hour", "day" and "week" follow the PC's local clock; the first snapshot in each
period is the one kept. The bucket levels off at about 265 snapshots (96 from the last day, 144 hourly,
about 23 daily), plus one per week after that.

## 1. Set up the R2 bucket (Cloudflare dashboard)
1. **R2 › Create bucket**, e.g. `astra-snapshots`.
2. **Bucket › Settings › Public access**: enable the r2.dev URL (or connect a custom
   domain). Copy the public URL, e.g. `https://pub-xxxx.r2.dev`.
   Anyone with this URL can view the snapshots.
3. **Bucket › Settings › CORS policy**: paste `r2-cors.json`, with your GitHub
   Pages address in `AllowedOrigins`.
4. **R2 › Manage API tokens › Create API token**: permission *Object Read & Write*,
   limited to this bucket. Note the Access Key ID, Secret Access Key and your
   Account ID.

## 2. Run the snapshotter on the Windows PC
1. Put `setup.bat`, `run_snapshots.bat` and `snapshotter.py` in the same folder.
2. Double-click **setup.bat** once. It finds Python (Anaconda or python.org),
   asks for your R2 Access Key ID and Secret Access Key, saves them for your
   Windows user, installs the packages and does a test upload that removes
   itself afterwards. It ends with "TEST PASSED" when everything works.
3. Close Orbbec Viewer if it's open (only one program can use the camera),
   plug the Astra 2 into a USB 3 port and double-click **run_snapshots.bat**.

**Extra snapshot right away:** press Enter in the run_snapshots window, or
double-click **snapshot_now.bat**. The 15-minute schedule carries on as before.

To change keys later, just run setup.bat again.

**Keep it running after restarts:** Task Scheduler › Create Task › Triggers: *At log on*
› Actions: start `run_snapshots.bat` › Conditions: untick "Start only if on AC power".
In Power settings set *Sleep* to *Never*, or the PC will stop taking snapshots.

Options: `--frames 10` (frames per snapshot), `--step 2` (keep every 2nd pixel,
a quarter of the points; default 1 = all), `--width 800 --height 600` (force a
depth mode), `--no-color` (depth only, no photo).

## 3. Host the viewer on GitHub Pages
1. Push this folder to a GitHub repo.
2. Settings › Pages › Deploy from a branch › `main`, folder `/docs`.
3. Open the page, paste the bucket's public URL, press Load.
   Shareable link: `https://<you>.github.io/<repo>/?bucket=https://pub-xxxx.r2.dev`

Drag the timeline, use ← → keys, or press Play for a time-lapse. The viewer checks
for new snapshots every minute.

### Viewer tools
- **Sections** — add horizontal planes (floor plans, set by height Z) and vertical
  planes (elevations, set by offset and rotation around Z). *Cut* hides one side
  (*Flip* swaps it); *Slice* keeps only a band of the chosen thickness. Select a
  section and drag its arrow to move it, or switch to *Rotate* to turn a vertical
  one. *View* looks straight at the section.
- **Model** — move and rotate the whole point cloud by dragging, or type exact
  values. Level the floor onto the grid so Z means height above the floor.
- **Views** — save named views (sections + camera). Everything also saves
  automatically in your browser. *Copy link* shares the current setup;
  *Download settings* saves `view.json` — upload it to the bucket and every
  viewer starts with that alignment, sections and views.
- **Display** — point size, max distance, colours. *Save visible points* exports
  the aligned, sectioned cloud as a coloured .ply (Z-up).

- **Export** — *Download LAZ*: the current snapshot as a LAS 1.2 / LAZ file
  (point format 2: XYZ + RGB, Z-up, aligned, 0.5 mm precision), optionally with the
  active sections applied. *Range as ZIP*: one .laz per snapshot between two dates.
  *MP4*: a time-lapse of the chosen range from the current view or a 360° orbit,
  at 720p, 1080p or 4K, with an optional date stamp. *PNG*: an image of the view.
  Everything is made in the browser; nothing is uploaded. Video export needs a
  recent Chrome, Edge, Safari or Firefox.

Keys: ← → step through time, space plays, W / E switch between move and rotate.

## Cost
R2's free tier is 10 GB storage, 1 million writes and 10 million reads a month,
with free downloads. At 15-minute captures this uses about 6,000 writes a month
and roughly 0.5–1 GB of storage at full resolution with colour, so it should stay free. Deletes are free.

## Files in the bucket
- `index.json` — list of snapshots (key, time, size), rewritten after every capture
- `snapshots/YYYY/MM/DD/YYYYMMDDTHHMMSSZ.jpg` — the colour photo for that snapshot
  (listed as `color` in index.json; older snapshots may not have one)
- `snapshots/YYYY/MM/DD/YYYYMMDDTHHMMSSZ.odp` — one depth frame each: 32-byte header
  (`ODP1`, width, height, fx, fy, cx, cy, mm per unit, flags) + zlib-compressed uint16 depth
