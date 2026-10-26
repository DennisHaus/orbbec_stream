#!/usr/bin/env python3
"""
Orbbec Astra 2 -> Cloudflare R2 snapshotter.

Every --interval minutes it captures a depth frame (median of several frames to
reduce noise), uploads it to R2, updates index.json and thins old snapshots:

    younger than 1 day     keep every snapshot
    1 day  to 1 week       keep one per hour
    1 week to 1 month      keep one per day
    older than 1 month     keep one per week

R2 credentials come from environment variables (see run_snapshots.bat):
    R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET

Test without a camera or R2:
    python snapshotter.py --mock --local ./test-bucket --once

Snapshot file format (.odp, little-endian):
  0   4s   magic b"ODP1"
  4   u16  width          6  u16 height
  8   f32  fx  12 f32 fy  16 f32 cx  20 f32 cy   (pixels)
  24  f32  depth scale (millimetres per depth unit)
  28  u8   flags (bit0 = payload is zlib-compressed)
  29  3x   padding
  32  ...  width*height uint16 depth values
"""
import argparse
import datetime as dt
import json
import math
import os
import struct
import sys
import time
import traceback
import warnings
import zlib
from pathlib import Path

import numpy as np

HEADER = struct.Struct("<4sHHfffffB3x")
INDEX_KEY = "index.json"
PREFIX = "snapshots/"

DAY = 86400
# (max age in seconds, bucket size) — bucket None means keep everything.
RETENTION = [
    (1 * DAY, None),
    (7 * DAY, "hour"),
    (30 * DAY, "day"),
    (math.inf, "week"),
]


def log(msg):
    print(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}", flush=True)


# =========================================================================== capture
def fov_intrinsics(w, h, hfov=79.0, vfov=62.0):
    """Fallback intrinsics from the Astra 2's approximate depth field of view."""
    fx = (w / 2) / math.tan(math.radians(hfov / 2))
    fy = (h / 2) / math.tan(math.radians(vfov / 2))
    return (fx, fy, w / 2, h / 2)


def median_depth(frames):
    """Per-pixel median of several depth frames, ignoring 0 (no reading)."""
    stack = np.stack(frames).astype(np.float32)
    stack[stack == 0] = np.nan
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN pixels
        med = np.nanmedian(stack, axis=0)
    return np.nan_to_num(med, nan=0).astype(np.uint16)


def capture_orbbec(n_frames, warmup, width=None, height=None):
    """Open the camera, grab n_frames after warm-up, close it again."""
    from pyorbbecsdk import Config, OBSensorType, Pipeline

    pipeline = Pipeline()
    config = Config()
    profiles = pipeline.get_stream_profile_list(OBSensorType.DEPTH_SENSOR)
    profile = None
    if width and height:
        try:
            from pyorbbecsdk import OBFormat
            profile = profiles.get_video_stream_profile(width, height, OBFormat.Y16, 30)
        except Exception as exc:
            log(f"requested {width}x{height} not available ({exc}); using default")
    if profile is None:
        profile = profiles.get_default_video_stream_profile()
    config.enable_stream(profile)
    pipeline.start(config)
    try:
        try:
            p = pipeline.get_camera_param().depth_intrinsic
            intr = (p.fx, p.fy, p.cx, p.cy)
        except Exception:
            intr = fov_intrinsics(profile.get_width(), profile.get_height())

        frames, scale, got, deadline = [], 1.0, 0, time.time() + 30
        while len(frames) < n_frames:
            if time.time() > deadline:
                raise RuntimeError("camera sent no depth frames for 30 s")
            fs = pipeline.wait_for_frames(500)
            d = fs.get_depth_frame() if fs is not None else None
            if d is None:
                continue
            got += 1
            if got <= warmup:  # let exposure settle
                continue
            w, h = d.get_width(), d.get_height()
            frames.append(np.frombuffer(d.get_data(), dtype=np.uint16).reshape(h, w).copy())
            scale = float(d.get_depth_scale())
        return median_depth(frames), scale, intr
    finally:
        pipeline.stop()


def capture_mock(t, w=640, h=480):
    """Synthetic scene that changes with time, for testing."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    nx, ny = (xx / w - 0.5) * 2, (yy / h - 0.5) * 2
    phase = (t / 3600.0) % (2 * math.pi)
    wall = 2400 + 120 * np.sin(nx * 5 + phase) * np.cos(ny * 3)
    floor = np.where(ny > 0.35, 1500 / np.maximum(ny, 0.36) * 0.4, np.inf)
    bx, by = 0.5 * math.sin(phase), 0.1
    r2 = (nx - bx) ** 2 + (ny - by) ** 2
    box = np.where(r2 < 0.06, 1400 - 1500 * np.sqrt(np.clip(0.06 - r2, 0, None)), np.inf)
    depth = np.minimum(np.minimum(wall, floor), box)
    return depth.astype(np.uint16), 1.0, fov_intrinsics(w, h)


def encode(depth, scale, intr, compress=6):
    h, w = depth.shape
    fx, fy, cx, cy = intr
    payload = zlib.compress(np.ascontiguousarray(depth, dtype="<u2").tobytes(), compress)
    return HEADER.pack(b"ODP1", w, h, fx, fy, cx, cy, scale, 1) + payload


# =========================================================================== retention
def bucket_key(t, size):
    d = dt.datetime.fromtimestamp(t)  # local time, so "one per day" follows your days
    if size == "hour":
        return d.strftime("%Y-%m-%d %H")
    if size == "day":
        return d.strftime("%Y-%m-%d")
    y, wk, _ = d.isocalendar()
    return f"{y}-W{wk:02d}"


def plan_retention(entries, now):
    """Return (keep, delete). Keeps the earliest snapshot in each hour/day/week.

    The earliest snapshot in a week is also the earliest of its day and hour,
    so a snapshot that survives one tier always survives the next.
    """
    earliest = {}
    for e in entries:
        for size in ("hour", "day", "week"):
            k = (size, bucket_key(e["t"], size))
            if k not in earliest or e["t"] < earliest[k]:
                earliest[k] = e["t"]
    keep, delete = [], []
    for e in entries:
        age = now - e["t"]
        size = next(s for limit, s in RETENTION if age < limit)
        if size is None or earliest[(size, bucket_key(e["t"], size))] == e["t"]:
            keep.append(e)
        else:
            delete.append(e)
    return keep, delete


# =========================================================================== storage
class LocalStore:
    """Writes to a folder laid out exactly like the bucket (for testing)."""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, key, data, content_type, cache_control):
        p = self.root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def get(self, key):
        p = self.root / key
        return p.read_bytes() if p.exists() else None

    def delete(self, keys):
        for k in keys:
            (self.root / k).unlink(missing_ok=True)

    def list(self, prefix):
        base = self.root / prefix
        return [str(p.relative_to(self.root)).replace("\\", "/") for p in base.rglob("*.odp")] if base.exists() else []


class R2Store:
    def __init__(self):
        import boto3
        missing = [v for v in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET") if not os.environ.get(v)]
        if missing:
            sys.exit(f"Missing environment variables: {', '.join(missing)} (see run_snapshots.bat)")
        self.bucket = os.environ["R2_BUCKET"]
        self.s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name="auto",
        )

    def put(self, key, data, content_type, cache_control):
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type, CacheControl=cache_control)

    def get(self, key):
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except self.s3.exceptions.NoSuchKey:
            return None

    def delete(self, keys):
        keys = list(keys)
        for i in range(0, len(keys), 1000):
            chunk = [{"Key": k} for k in keys[i:i + 1000]]
            self.s3.delete_objects(Bucket=self.bucket, Delete={"Objects": chunk, "Quiet": True})

    def list(self, prefix):
        out, token = [], None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            r = self.s3.list_objects_v2(**kw)
            out += [o["Key"] for o in r.get("Contents", [])]
            if not r.get("IsTruncated"):
                return out
            token = r["NextContinuationToken"]


def key_for(t):
    d = dt.datetime.fromtimestamp(t, dt.timezone.utc)
    return f"{PREFIX}{d:%Y/%m/%d}/{d:%Y%m%dT%H%M%SZ}.odp"


def t_from_key(key):
    stamp = key.rsplit("/", 1)[-1].removesuffix(".odp")
    return int(dt.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc).timestamp())


def load_index(store):
    raw = store.get(INDEX_KEY)
    if raw:
        return json.loads(raw)["snapshots"]
    # No index yet (or it was deleted): rebuild it from the files in the bucket.
    keys = store.list(PREFIX)
    if keys:
        log(f"rebuilding index from {len(keys)} files in the bucket")
    return sorted(({"key": k, "t": t_from_key(k), "bytes": 0} for k in keys), key=lambda e: e["t"])


def save_index(store, entries, interval_min):
    doc = {
        "updated": int(time.time()),
        "interval_minutes": interval_min,
        "retention": "all for 1 day, hourly for 1 week, daily for 1 month, then weekly",
        "snapshots": entries,
    }
    store.put(INDEX_KEY, json.dumps(doc, separators=(",", ":")).encode(), "application/json", "no-cache")


# =========================================================================== main loop
def take_snapshot(args, store, entries, t=None):
    t = int(t if t is not None else time.time())
    if args.mock:
        depth, scale, intr = capture_mock(t)
    else:
        depth, scale, intr = capture_orbbec(args.frames, args.warmup, args.width, args.height)
    if args.step > 1:
        depth = depth[::args.step, ::args.step]
        intr = tuple(v / args.step for v in intr)
    data = encode(depth, scale, intr)
    key = key_for(t)
    store.put(key, data, "application/octet-stream", "public, max-age=31536000, immutable")
    entries = [e for e in entries if e["key"] != key] + [{"key": key, "t": t, "bytes": len(data)}]
    entries.sort(key=lambda e: e["t"])
    keep, delete = plan_retention(entries, now=t)
    if delete:
        store.delete(e["key"] for e in delete)
    save_index(store, keep, args.interval)
    valid = int(np.count_nonzero(depth))
    log(f"saved {key} ({len(data) / 1024:.0f} KB, {valid:,} points); removed {len(delete)} old; {len(keep)} kept")
    return keep


def sleep_until_next(interval_min):
    """Align captures to the clock, e.g. :00, :15, :30, :45 for 15 minutes."""
    period = interval_min * 60
    wait = period - (time.time() % period)
    time.sleep(wait)


def main():
    ap = argparse.ArgumentParser(description="Save Orbbec depth snapshots to Cloudflare R2 on a schedule.")
    ap.add_argument("--interval", type=float, default=15, help="minutes between snapshots (default 15)")
    ap.add_argument("--frames", type=int, default=10, help="frames to median-filter per snapshot")
    ap.add_argument("--warmup", type=int, default=15, help="frames to skip after opening the camera")
    ap.add_argument("--step", type=int, default=1, help="keep every Nth pixel (2 = quarter size)")
    ap.add_argument("--width", type=int, help="depth width, e.g. 800")
    ap.add_argument("--height", type=int, help="depth height, e.g. 600")
    ap.add_argument("--local", help="save into this folder instead of R2 (for testing)")
    ap.add_argument("--once", action="store_true", help="take one snapshot and exit")
    ap.add_argument("--mock", action="store_true", help="synthetic data instead of a camera")
    args = ap.parse_args()

    store = LocalStore(args.local) if args.local else R2Store()
    entries = load_index(store)
    log(f"{len(entries)} snapshots in index; every {args.interval:g} min; target: {args.local or 'R2 bucket ' + store.bucket}")

    while True:
        try:
            entries = take_snapshot(args, store, entries)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            log(f"snapshot failed: {exc}")
            traceback.print_exc()
        if args.once:
            return
        sleep_until_next(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("stopped")
