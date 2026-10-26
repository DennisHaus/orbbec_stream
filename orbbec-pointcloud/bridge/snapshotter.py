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

Each snapshot is a depth file (.odp) at the camera's full depth resolution plus,
when the colour camera is used, a .jpg with the same name: the colour image mapped
onto the depth image, so pixel (u, v) of the .jpg is the colour of depth pixel
(u, v). Black pixels are outside the colour camera's view.

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


def color_to_rgb(frame):
    """Decode an Orbbec colour frame (MJPG, RGB, BGR, YUYV, UYVY) to an HxWx3 uint8 array."""
    from pyorbbecsdk import OBFormat
    fmt = frame.get_format()
    w, h = frame.get_width(), frame.get_height()
    data = np.asarray(frame.get_data(), dtype=np.uint8)
    if fmt == OBFormat.MJPG:
        import io
        from PIL import Image
        return np.asarray(Image.open(io.BytesIO(data.tobytes())).convert("RGB"))
    if fmt == OBFormat.RGB:
        return data.reshape(h, w, 3)
    if fmt == OBFormat.BGR:
        return data.reshape(h, w, 3)[:, :, ::-1]
    if fmt in (OBFormat.YUYV, OBFormat.UYVY):
        return yuv422_to_rgb(data.reshape(h, w, 2), uyvy=(fmt == OBFormat.UYVY))
    raise RuntimeError(f"colour format {fmt} is not supported")


def map_color_onto_depth(depth, scale, d_intr, rgb, c_intr, rot, trans):
    """Give every depth pixel the colour the colour camera saw at that 3D point.

    depth: HxW uint16, scale: mm per unit, d_intr/c_intr: (fx, fy, cx, cy) in pixels,
    rot: 3x3 and trans: 3 (mm) taking depth-camera coordinates to colour-camera ones.
    Returns (HxWx3 uint8 image aligned with the depth, fraction of points coloured).
    Pixels with no colour (outside the colour camera's view) stay black.
    """
    h, w = depth.shape
    vs, us = np.nonzero(depth)
    out = np.zeros((h, w, 3), np.uint8)
    if len(us) == 0:
        return out, 0.0
    z = depth[vs, us].astype(np.float32) * scale
    fx, fy, cx, cy = d_intr
    pts = np.stack([(us - cx) * z / fx, (vs - cy) * z / fy, z])          # 3xN, mm, depth camera
    pc = rot.astype(np.float32) @ pts + trans.astype(np.float32)[:, None]  # colour camera
    cfx, cfy, ccx, ccy = c_intr
    with np.errstate(divide="ignore", invalid="ignore"):
        u2 = np.rint(cfx * pc[0] / pc[2] + ccx)
        v2 = np.rint(cfy * pc[1] / pc[2] + ccy)
    ch, cw = rgb.shape[:2]
    ok = (pc[2] > 1) & (u2 >= 0) & (u2 < cw) & (v2 >= 0) & (v2 < ch)
    out[vs[ok], us[ok]] = rgb[v2[ok].astype(np.int32), u2[ok].astype(np.int32)]
    # Keep real black objects distinguishable from "no colour".
    blk = out[vs[ok], us[ok]].sum(axis=1) < 6
    if blk.any():
        out[vs[ok][blk], us[ok][blk]] = (3, 3, 3)
    return out, float(ok.mean())


def yuv422_to_rgb(yuv, uyvy=False):
    yuv = yuv.astype(np.float32)
    if uyvy:
        y = yuv[:, :, 1]
        u = np.repeat(yuv[:, 0::2, 0], 2, axis=1)
        v = np.repeat(yuv[:, 1::2, 0], 2, axis=1)
    else:
        y = yuv[:, :, 0]
        u = np.repeat(yuv[:, 0::2, 1], 2, axis=1)
        v = np.repeat(yuv[:, 1::2, 1], 2, axis=1)
    u, v = u - 128, v - 128
    rgb = np.stack([y + 1.402 * v, y - 0.344 * u - 0.714 * v, y + 1.772 * u], axis=2)
    return np.clip(rgb, 0, 255).astype(np.uint8)


def rgb_to_jpeg(rgb, quality=85):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(rgb)).save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


class _StartError(RuntimeError):
    """The camera refused this combination of modes (try a smaller one)."""


_preferred = None  # (depth w, depth h, with colour) that worked last time


def _video_profiles(profile_list):
    out = []
    try:
        n = profile_list.get_count()
    except Exception:
        return out
    for i in range(n):
        try:
            out.append(profile_list.get_stream_profile_by_index(i).as_video_stream_profile())
        except Exception:
            pass
    return out


def _depth_candidates(pipeline, width=None, height=None):
    """Depth modes from largest to smallest, one per resolution."""
    from pyorbbecsdk import OBFormat, OBSensorType
    plist = pipeline.get_stream_profile_list(OBSensorType.DEPTH_SENSOR)
    profs = _video_profiles(plist)
    if width and height:
        profs = [p for p in profs if p.get_width() == width and p.get_height() == height] or profs
    best = {}
    for p in profs:
        res = (p.get_width(), p.get_height())
        # Prefer plain 16-bit depth, and a moderate frame rate to leave USB bandwidth for colour.
        score = (p.get_format() == OBFormat.Y16, -abs(p.get_fps() - 15))
        if res not in best or score > best[res][0]:
            best[res] = (score, p)
    cands = [v[1] for res, v in sorted(best.items(), key=lambda kv: -(kv[0][0] * kv[0][1]))]
    return cands or [plist.get_default_video_stream_profile()]


def _color_candidates(pipeline):
    """Colour modes from largest to smallest; per size MJPG first (light on USB), then RGB."""
    from pyorbbecsdk import OBFormat, OBSensorType
    try:
        plist = pipeline.get_stream_profile_list(OBSensorType.COLOR_SENSOR)
    except Exception as exc:
        log(f"no colour camera found ({exc}); saving depth only")
        return []
    best = {}
    for p in _video_profiles(plist):
        if p.get_format() not in (OBFormat.MJPG, OBFormat.RGB):
            continue
        k = (p.get_width(), p.get_height(), p.get_format())
        if k not in best or abs(p.get_fps() - 15) < abs(best[k].get_fps() - 15):
            best[k] = p
    order = sorted(best.values(), key=lambda p: (-p.get_width() * p.get_height(), p.get_format() != OBFormat.MJPG))
    if not order:
        try:
            order = [plist.get_default_video_stream_profile()]
        except Exception:
            order = []
    return order


def _intr_tuple(p, w=None, h=None):
    """(fx, fy, cx, cy) from an Orbbec intrinsic, rescaled to w x h if it was for another size."""
    fx, fy, cx, cy = float(p.fx), float(p.fy), float(p.cx), float(p.cy)
    pw, ph = getattr(p, "width", 0) or 0, getattr(p, "height", 0) or 0
    if w and h and pw and ph and (pw, ph) != (w, h):
        sx, sy = w / pw, h / ph
        fx, cx, fy, cy = fx * sx, cx * sx, fy * sy, cy * sy
    return (fx, fy, cx, cy)


def _profile_intrinsics(profile, w, h):
    try:
        p = profile.get_intrinsic()
        if p.fx > 0:
            return _intr_tuple(p, w, h)
    except Exception:
        pass
    return None


def _extrinsic(depth_profile, color_profile, pipeline):
    """Rotation (3x3) and translation (mm) from depth-camera to colour-camera coordinates."""
    ext = None
    try:
        ext = depth_profile.get_extrinsic_to(color_profile)
    except Exception:
        try:
            ext = pipeline.get_camera_param().transform
        except Exception:
            ext = None
    if ext is None:
        return None
    try:
        rot = np.array(ext.rot, dtype=np.float64).reshape(3, 3)
        trans = np.array(ext.transform, dtype=np.float64).reshape(3)
    except Exception:
        return None
    if np.linalg.norm(trans) < 0.5:  # given in metres rather than millimetres
        trans = trans * 1000.0
    return rot, trans


_warned = set()
def _warn_once(msg):
    if msg not in _warned:
        _warned.add(msg)
        log(msg)


def _capture_with(pipeline, depth_profile, color_profile, n_frames, warmup):
    from pyorbbecsdk import Config
    config = Config()
    config.enable_stream(depth_profile)
    if color_profile is not None:
        config.enable_stream(color_profile)
        try:
            from pyorbbecsdk import OBFrameAggregateOutputMode
            config.set_frame_aggregate_output_mode(OBFrameAggregateOutputMode.FULL_FRAME_REQUIRE)
        except Exception:
            pass
        try:
            pipeline.enable_frame_sync()
        except Exception:
            pass
    try:
        pipeline.start(config)
    except Exception as exc:
        raise _StartError(str(exc)) from exc
    try:
        dw, dh = depth_profile.get_width(), depth_profile.get_height()
        frames, scale, got = [], 1.0, 0
        last_color = None
        started = time.time()
        while len(frames) < n_frames:
            if time.time() - started > (10 if got == 0 else 40):
                if got == 0:
                    raise _StartError("no frames arrived")
                raise RuntimeError("camera stopped sending frames")
            fs = pipeline.wait_for_frames(500)
            if fs is None:
                continue
            d = fs.get_depth_frame()
            if d is None:
                continue
            c = fs.get_color_frame() if color_profile is not None else None
            if color_profile is not None and c is None:
                continue
            got += 1
            if got <= warmup:  # let exposure settle
                continue
            w, h = d.get_width(), d.get_height()
            frames.append(np.frombuffer(d.get_data(), dtype=np.uint16).reshape(h, w).copy())
            scale = float(d.get_depth_scale())
            if c is not None:
                last_color = c
        depth = median_depth(frames)
        h, w = depth.shape

        # Depth intrinsics for the actual frame size.
        d_intr = _profile_intrinsics(depth_profile, w, h)
        if d_intr is None:
            try:
                d_intr = _intr_tuple(pipeline.get_camera_param().depth_intrinsic, w, h)
            except Exception:
                d_intr = fov_intrinsics(w, h)
                _warn_once("could not read depth calibration; using approximate field of view")

        jpeg = None
        if last_color is not None:
            try:
                rgb = color_to_rgb(last_color)
                ch, cw = rgb.shape[:2]
                c_intr = _profile_intrinsics(color_profile, cw, ch)
                if c_intr is None:
                    try:
                        c_intr = _intr_tuple(pipeline.get_camera_param().rgb_intrinsic, cw, ch)
                    except Exception:
                        c_intr = fov_intrinsics(cw, ch, 86.0, 55.0)
                        _warn_once("could not read colour calibration; colours may be offset")
                ext = _extrinsic(depth_profile, color_profile, pipeline)
                if ext is None:
                    _warn_once("could not read the depth-to-colour offset; colours may be slightly offset")
                    ext = (np.eye(3), np.zeros(3))
                mapped, frac = map_color_onto_depth(depth, scale, d_intr, rgb, c_intr, *ext)
                if frac < 0.2:
                    _warn_once(f"only {frac:.0%} of points fall inside the colour image; the calibration may be off")
                jpeg = rgb_to_jpeg(mapped, quality=90)
                _warn_once(f"colour {cw}x{ch} mapped onto {frac:.0%} of the depth points")
            except Exception as exc:
                log(f"could not add colour ({exc}); saving depth only")
        return depth, scale, d_intr, jpeg
    finally:
        try:
            pipeline.stop()
        except Exception:
            pass


def _key(attempt):
    d, c = attempt
    return (d.get_width(), d.get_height()) + ((c.get_width(), c.get_height(), str(c.get_format())) if c else ())


def capture_orbbec(n_frames, warmup, width=None, height=None, color=True):
    """Capture at the largest depth mode the camera accepts, with colour if possible.

    Returns (depth, scale, intrinsics, jpeg-or-None).
    """
    global _preferred
    from pyorbbecsdk import Pipeline

    pipeline = Pipeline()
    depth_cands = _depth_candidates(pipeline, width, height)
    color_cands = _color_candidates(pipeline) if color else []
    # Largest depth first; for each, try colour from large to small before giving up on that depth size.
    attempts = [(d, c) for d in depth_cands for c in color_cands]
    attempts += [(d, None) for d in depth_cands]  # last resort: depth only
    if _preferred:
        attempts.sort(key=lambda a: _key(a) != _preferred)

    last = None
    for d, c in attempts:
        label = f"{d.get_width()}x{d.get_height()} depth" + (f" + {c.get_width()}x{c.get_height()} colour" if c else "")
        try:
            result = _capture_with(pipeline, d, c, n_frames, warmup)
        except _StartError as exc:
            log(f"camera refused {label} ({exc}); trying a smaller mode")
            last = exc
            continue
        key = _key((d, c))
        if key != _preferred:
            log(f"using {label}")
            _preferred = key
        return result
    raise RuntimeError(f"the camera refused every mode; last error: {last}")


def capture_mock(t, w=640, h=480, color=True):
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
    jpeg = None
    if color:
        # Colour mapped onto the depth image (same size), black where the colour camera can't see.
        cy, cx = np.mgrid[0:h, 0:w].astype(np.float32)
        stripes = ((cx // 20 + cy // 20) % 2) * 40
        rgb = np.stack([150 + stripes + 0 * cy, 120 + 0.2 * cy, 90 + 0.24 * cx], axis=2)
        rnx, rny = (cx / w - 0.5) * 2, (cy / h - 0.5) * 2
        ball = ((rnx - bx) ** 2 + (rny - by) ** 2) < 0.06
        rgb[ball] = [220, 60, 50]
        rgb[rny > 0.35] = [90, 140, 80]
        rgb[:, : w // 16] = 0  # edge outside the colour camera's view
        jpeg = rgb_to_jpeg(np.clip(rgb, 0, 255).astype(np.uint8))
    return depth.astype(np.uint16), 1.0, fov_intrinsics(w, h), jpeg


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
        return [str(p.relative_to(self.root)).replace("\\", "/") for p in base.rglob("*") if p.is_file()] if base.exists() else []


class R2Store:
    def __init__(self):
        import boto3
        missing = [v for v in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET") if not os.environ.get(v)]
        if missing:
            sys.exit(f"Missing environment variables: {', '.join(missing)} (see run_snapshots.bat)")
        env = lambda k: os.environ[k].strip().strip('"')
        self.bucket = env("R2_BUCKET")
        self.s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{env('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com",
            aws_access_key_id=env("R2_ACCESS_KEY_ID"),
            aws_secret_access_key=env("R2_SECRET_ACCESS_KEY"),
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


def files_of(entries):
    """All bucket keys belonging to these snapshots (depth + optional colour)."""
    out = []
    for e in entries:
        out.append(e["key"])
        if e.get("color"):
            out.append(e["color"])
    return out


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
    odp = [k for k in keys if k.endswith(".odp")]
    jpgs = {k for k in keys if k.endswith(".jpg")}
    if odp:
        log(f"rebuilding index from {len(odp)} snapshots in the bucket")
    entries = []
    for k in odp:
        e = {"key": k, "t": t_from_key(k), "bytes": 0}
        if k[:-4] + ".jpg" in jpgs:
            e["color"] = k[:-4] + ".jpg"
        entries.append(e)
    return sorted(entries, key=lambda e: e["t"])


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
    color = not args.no_color
    if args.mock:
        depth, scale, intr, jpeg = capture_mock(t, color=color)
    else:
        depth, scale, intr, jpeg = capture_orbbec(args.frames, args.warmup, args.width, args.height, color)
    step = max(1, args.step)  # 1 = every point (full resolution)
    if step > 1:
        depth = depth[::step, ::step]
        intr = tuple(v / step for v in intr)
    data = encode(depth, scale, intr)
    key = key_for(t)
    immutable = "public, max-age=31536000, immutable"
    store.put(key, data, "application/octet-stream", immutable)
    entry = {"key": key, "t": t, "bytes": len(data)}
    if jpeg:
        ckey = key[:-4] + ".jpg"
        store.put(ckey, jpeg, "image/jpeg", immutable)
        entry["color"] = ckey
        entry["bytes"] += len(jpeg)
    entries = [e for e in entries if e["key"] != key] + [entry]
    entries.sort(key=lambda e: e["t"])
    keep, delete = plan_retention(entries, now=t)
    if delete:
        store.delete(files_of(delete))
    save_index(store, keep, args.interval)
    valid = int(np.count_nonzero(depth))
    extra = " + colour" if jpeg else ""
    log(f"saved {key}{extra} ({entry['bytes'] / 1024:.0f} KB, {valid:,} points); removed {len(delete)} old; {len(keep)} kept")
    return keep


def enter_pressed():
    """True if Enter was pressed in this console window (never blocks)."""
    if not sys.stdin or not sys.stdin.isatty():
        return False
    try:
        import msvcrt  # Windows
        hit = False
        while msvcrt.kbhit():
            if msvcrt.getwch() in ("\r", "\n"):
                hit = True
        return hit
    except ImportError:
        import select
        if select.select([sys.stdin], [], [], 0)[0]:
            sys.stdin.readline()
            return True
        return False


def sleep_until_next(interval_min):
    """Wait for the next quarter hour (for 15 min), or until Enter is pressed.

    Returns True if the wait was cut short by Enter.
    """
    period = interval_min * 60
    end = time.time() + period - (time.time() % period)
    nxt = dt.datetime.fromtimestamp(end)
    log(f"next snapshot at {nxt:%H:%M}. Press Enter to take one now, Ctrl+C to stop.")
    while time.time() < end:
        if enter_pressed():
            log("snapshot requested")
            return True
        time.sleep(0.2)
    return False


def run_test(args):
    """Upload one snapshot, check it can be read back, then undo everything."""
    store = LocalStore(args.local) if args.local else R2Store()
    try:
        before = load_index(store)
        t = int(time.time())
        while any(e["key"] == key_for(t) for e in before):
            t += 1  # never overwrite a real snapshot
        after = take_snapshot(args, store, list(before), t=t)
        new = [e for e in after if e not in before]
        raw = store.get(new[-1]["key"]) if new else None
        ok = raw is not None and raw[:4] == b"ODP1" and store.get(INDEX_KEY) is not None
        if ok and new[-1].get("color"):
            jpg = store.get(new[-1]["color"])
            ok = jpg is not None and jpg[:2] == b"\xff\xd8"
    except Exception as exc:
        log(f"TEST FAILED: {exc}")
        hint = str(exc)
        if "InvalidAccessKeyId" in hint or "SignatureDoesNotMatch" in hint or "Unauthorized" in hint:
            log("-> the Access Key ID or Secret Access Key is wrong. Run setup.bat again.")
        elif "NoSuchBucket" in hint:
            log("-> no bucket with that name. Check R2_BUCKET.")
        elif "AccessDenied" in hint:
            log("-> the API token lacks Object Read & Write on this bucket.")
        elif "Could not connect" in hint or "getaddrinfo" in hint:
            log("-> could not reach Cloudflare. Check the account ID and your internet connection.")
        return 1
    # Clean up: remove the test snapshot and restore the previous index.
    store.delete(files_of(new))
    if before:
        save_index(store, before, args.interval)
    else:
        store.delete([INDEX_KEY])
    if ok:
        log("TEST PASSED: upload, read-back and clean-up all worked.")
        return 0
    log("TEST FAILED: the snapshot could not be read back.")
    return 1


def main():
    ap = argparse.ArgumentParser(description="Save Orbbec depth snapshots to Cloudflare R2 on a schedule.")
    ap.add_argument("--interval", type=float, default=15, help="minutes between snapshots (default 15)")
    ap.add_argument("--frames", type=int, default=10, help="frames to median-filter per snapshot")
    ap.add_argument("--warmup", type=int, default=15, help="frames to skip after opening the camera")
    ap.add_argument("--step", type=int, default=1, help="keep every Nth pixel (default 1 = full resolution)")
    ap.add_argument("--width", type=int, help="depth width, e.g. 800")
    ap.add_argument("--height", type=int, help="depth height, e.g. 600")
    ap.add_argument("--local", help="save into this folder instead of R2 (for testing)")
    ap.add_argument("--once", action="store_true", help="take one snapshot and exit")
    ap.add_argument("--mock", action="store_true", help="synthetic data instead of a camera")
    ap.add_argument("--no-color", action="store_true", help="save depth only, no colour photo")
    ap.add_argument("--test", action="store_true", help="upload one snapshot, read it back, then remove it")
    args = ap.parse_args()
    if args.test:
        sys.exit(run_test(args))

    store = LocalStore(args.local) if args.local else R2Store()
    entries = load_index(store)
    log(f"{len(entries)} snapshots in index; every {args.interval:g} min; target: {args.local or 'R2 bucket ' + store.bucket}")

    while True:
        try:
            # Re-read the index each time, so snapshots taken by snapshot_now.bat
            # (a separate program) are kept in it and thinned like the rest.
            entries = load_index(store)
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
