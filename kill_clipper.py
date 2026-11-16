"""
Kill Clipper 2
==============
Finds every kill in a Battlefield 6 gameplay recording by watching for the
red kill "X" around the crosshair, then plays the recording back showing
ONLY those moments - straight from your original file, nothing is cut or
re-saved. You can still save a Premiere/Resolve sequence (XML) of the kills.

How it works:
  1. The middle of the screen is checked 20 times per second for the red X.
  2. Each frame gets a match score (0-100%). Wherever the score is above the
     threshold set by the Fuzziness slider, that's a kill.
  3. Each kill becomes a clip: X seconds before, Y seconds after. Overlapping
     clips are merged.
  4. The built-in player (mpv) plays the original video, and whenever it
     reaches the end of a clip it jumps straight to the start of the next one.
  5. "Save XML sequence..." writes the same clips as a Final Cut Pro 7 XML file
     that Premiere opens as a ready-cut sequence (File > Import).

Run it by double-clicking "2 - Run Kill Clipper.bat" (Windows).
"""

import os
import re
import sys
import queue
import random
import threading
import time
import subprocess
from urllib.parse import quote
from xml.sax.saxutils import escape

APP_NAME = "Kill Clipper"
APP_VERSION = "2.0"
SCAN_PHRASES = (  # shown while scanning; the first one always comes first
    "Finding your kill shots", "Ooh, that's a nice one!", "Ouch! Get good, noob", "Nice shot!",
    "Boom! Headshot!", "Dropping a beacon!", "Heavy armor incoming!", "I'm reloading!", "Great shot!",
    "That one's gonna leave a mark", "Taking the objective!", "Enemy infantry!",
    "I need a fucking medic!", "Need some ammo!", "Securing objective Charlie", "Respawning on squad",
    "Mortaring", "Playing around with loadouts", "1v1 me, noob", "Lowering latency",
)
SCAN_PHRASE_SECONDS = 5  # how long each phrase stays up
VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".flv", ".webm", ".ts", ".m4v")


def _startup_error(message):
    """Shows a problem in a pop-up (there's no console window to print it to)."""
    try:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror(APP_NAME, message)
        r.destroy()
    except Exception:
        print(message, file=sys.stderr or sys.__stderr__)
    sys.exit(1)


try:
    import numpy as np
except ImportError:
    _startup_error("Kill Clipper is missing an add-on it needs (numpy).\n\n"
                   "Double-click \"1 - Setup (run once).bat\" in the Kill Clipper folder, "
                   "then start Kill Clipper again.")


def threshold_for(fuzziness):
    """Fuzziness 0 (strict) .. 100 (loose)  ->  minimum match score 0.90 .. 0.30"""
    return 0.90 - 0.006 * float(fuzziness)


# ---------------------------------------------------------------------------
# FFmpeg helpers (FFmpeg is the tool that reads video files)
# ---------------------------------------------------------------------------
_ffmpeg_path = None


def find_ffmpeg():
    global _ffmpeg_path
    if _ffmpeg_path:
        return _ffmpeg_path
    try:
        import imageio_ffmpeg  # installed by setup.bat; ships its own ffmpeg.exe
        _ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        import shutil
        _ffmpeg_path = shutil.which("ffmpeg")  # or an ffmpeg that's on the PATH
        if not _ffmpeg_path:
            raise RuntimeError("Kill Clipper is missing an add-on it needs (imageio-ffmpeg).\n\n"
                               "Double-click \"1 - Setup (run once).bat\" in the Kill Clipper "
                               "folder, then start Kill Clipper again.")
    return _ffmpeg_path


def _hidden_window():
    # Stops a black console window flashing up every time ffmpeg runs on Windows
    if os.name == "nt":
        return {"creationflags": 0x08000000}  # CREATE_NO_WINDOW
    return {}


_LAYOUT_CHANNELS = {"mono": 1, "stereo": 2, "2.1": 3, "3.0": 3, "quad": 4, "4.0": 4,
                    "5.0": 5, "5.1": 6, "6.1": 7, "7.1": 8}
_GENERIC_NAMES = {"soundhandler", "core media audio", "audiohandler", "isomedia file produced by google",
                  "sound media handler", "gpac isom audio handler", ""}


def _channels_from(desc):
    m = re.search(r"Hz,\s*([^,]+)", desc)
    layout = m.group(1).strip() if m else "stereo"
    m = re.match(r"(\d+)\s*channels", layout)
    if m:
        return int(m.group(1)), layout
    base = layout.split("(")[0].strip()
    return _LAYOUT_CHANNELS.get(base, 2), layout


def probe(path):
    """Reads basic facts about a video: length, frame rate, size and its audio tracks."""
    p = subprocess.run([find_ffmpeg(), "-hide_banner", "-i", path],
                       capture_output=True, text=True, errors="replace",
                       **_hidden_window())
    err = p.stderr
    info = {"duration": None, "fps": None, "width": 1920, "height": 1080,
            "has_video": False, "audio_streams": []}

    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if m:
        h, mi, s = m.groups()
        info["duration"] = int(h) * 3600 + int(mi) * 60 + float(s)

    current = None  # the audio track whose "Metadata:" lines we're reading
    for line in err.splitlines():
        if "Stream #" in line:
            current = None
            if "Video:" in line and not info["has_video"] and "attached pic" not in line:
                info["has_video"] = True
                m = re.search(r",\s*(\d{2,5})x(\d{2,5})", line)
                if m:
                    info["width"], info["height"] = int(m.group(1)), int(m.group(2))
                m = re.search(r"([\d.]+)\s*fps", line) or re.search(r"([\d.]+)\s*tbr", line)
                if m:
                    info["fps"] = float(m.group(1))
            elif "Audio:" in line:
                desc = line.split("Audio:", 1)[1]
                m = re.search(r"Stream #[^(:]*:?\d*(?:\[[^\]]*\])?\(([^)]*)\)", line)
                lang = m.group(1) if m else ""
                m = re.search(r"(\d+)\s*Hz", desc)
                channels, layout = _channels_from(desc)
                current = {"rate": int(m.group(1)) if m else 48000, "channels": channels,
                           "layout": layout, "lang": "" if lang in ("und", "") else lang,
                           "title": ""}
                info["audio_streams"].append(current)
        elif current is not None:
            m = re.match(r"\s*(title|handler_name)\s*:\s*(.*)$", line)
            if m and m.group(2).strip().lower() not in _GENERIC_NAMES:
                if m.group(1) == "title" or not current["title"]:
                    current["title"] = m.group(2).strip()

    if info["duration"] is None:
        raise RuntimeError("Couldn't read this file. Is it a video file?\n\n" + err.strip()[-600:])

    # Double-check the number of audio tracks by asking ffmpeg to "copy" all of
    # them (to nowhere) and counting the tracks it lists.
    n = count_audio_tracks(path)
    if n is not None and n != len(info["audio_streams"]):
        streams = info["audio_streams"][:n]
        while len(streams) < n:
            streams.append({"rate": 48000, "channels": 2, "layout": "stereo", "lang": "", "title": ""})
        info["audio_streams"] = streams
    return info


def count_audio_tracks(path):
    """Number of audio tracks, as ffmpeg sees them, or None if it can't tell."""
    p = subprocess.run([find_ffmpeg(), "-hide_banner", "-nostdin", "-i", path, "-map", "0:a?",
                        "-vn", "-sn", "-dn", "-c", "copy", "-t", "0", "-f", "null", "-"],
                       capture_output=True, text=True, errors="replace", **_hidden_window())
    err = p.stderr
    if "Stream mapping:" not in err:
        return 0 if not re.search(r"Stream #\d+:\d+.*?: Audio:", err) else None
    listing, mapping = err.split("Stream mapping:", 1)
    audio_ids = set(re.findall(r"Stream #(\d+:\d+)[^:]*: Audio:", listing))
    copied = re.findall(r"^\s*Stream #(\d+:\d+)\S*\s*->", mapping, re.M)
    return sum(1 for sid in copied if sid in audio_ids)


def track_labels(info):
    """Names for the dropdown: 'Track 1', 'Track 2', ... (plus the track's name if it has one)."""
    return ["Track %d%s" % (i, " - " + a["title"] if a["title"] else "")
            for i, a in enumerate(info["audio_streams"], start=1)]


# ---------------------------------------------------------------------------
# Visual detection: the red kill "X" around the crosshair (Battlefield 6)
# ---------------------------------------------------------------------------
VIS_FPS = 20     # frames per second to check (the X stays up for several of these)
XC = 240         # we look at a square this size around the screen centre (in 1080p pixels)
X_R_IN, X_R_OUT = 24, 112   # the X's arms sit between these distances from the centre
X_MAXOFF = 18    # the X can be this far off-centre (it follows your aim when moving)
X_NORM = 100.0   # raw score that counts as a 100% match

_yy, _xx = np.mgrid[0:XC, 0:XC]
_dx, _dy = _xx + 0.5 - XC / 2, _yy + 0.5 - XC / 2
_X_RING = (np.hypot(_dx, _dy) >= X_R_IN) & (np.hypot(_dx, _dy) <= X_R_OUT)
_X_NK = 2 * XC - 1
_X_FAMILIES = (  # (which "\" or "/" line each pixel is on, which half of that line it's in)
    ((_yy - _xx) + XC - 1, (_dx + _dy) > 0),   # "\" lines: up-left half vs down-right half
    (_yy + _xx, (_dx - _dy) > 0),              # "/" lines: down-left half vs up-right half
)


def x_scores(frames):
    """How strongly each frame shows the red kill X (0..1).

    `frames` is (N, XC, XC, 3) RGB, centred on the screen. Idea: a red X is one
    "\" line and one "/" line, each red on BOTH sides of the centre, crossing
    near the centre. For each frame we count bright-red pixels along every
    possible "\" and "/" line. A real X gives one line in each direction that's
    far redder than the lines right next to it. Blood is red everywhere (no
    single line stands out), and scope crosshairs and the range numbers are
    horizontal/vertical, so they don't line up with the diagonals.
    """
    f = frames.astype(np.int16)
    r, g, b = f[..., 0], f[..., 1], f[..., 2]
    red = (r > 120) & ((r - np.maximum(g, b)) > 60) & _X_RING
    n, y, x = np.nonzero(red)
    N = len(frames)
    idx = np.arange(N)
    strengths, lines = [], []
    for K, half in _X_FAMILIES:
        flat = (n * 2 + half[y, x]) * _X_NK + K[y, x]
        h = np.bincount(flat, minlength=N * 2 * _X_NK).reshape(N, 2, _X_NK).astype(np.float32)
        c = np.cumsum(np.pad(h, ((0, 0), (0, 0), (3, 2))), axis=-1)  # sum each line +-2
        h = c[..., 5:] - c[..., :-5]
        arm = h.min(axis=1)  # weaker of the two halves, so both arms must be there
        lo, hi = XC - 1 - 2 * X_MAXOFF, XC + 2 * X_MAXOFF
        k0 = lo + np.argmax(arm[:, lo:hi], axis=1)
        both = h.max(axis=1)
        nearby = np.maximum(both[idx, np.clip(k0 - 8, 0, _X_NK - 1)],
                            both[idx, np.clip(k0 + 8, 0, _X_NK - 1)])
        strengths.append(np.clip(arm[idx, k0] - nearby, 0, None))
        lines.append(k0)
    kd, ka = lines[0] - (XC - 1), lines[1]
    cx, cy = (ka - kd) / 2 + 0.5 - XC / 2, (ka + kd) / 2 + 0.5 - XC / 2
    near_centre = (np.abs(cx) <= X_MAXOFF) & (np.abs(cy) <= X_MAXOFF)
    return np.clip(np.minimum(*strengths) * near_centre / X_NORM, 0, 1).astype(np.float32)


def scan_red_x(video_path, info, progress=None, cancel=None):
    """Watches the middle of the screen and returns an X score for each 1/20 s."""
    side = int(round(info["height"] * XC / 1080.0)) // 2 * 2
    vf = ("fps=%d,crop=%d:%d:(iw-%d)/2:(ih-%d)/2,scale=%d:%d:flags=area"
          % (VIS_FPS, side, side, side, side, XC, XC))
    frame_bytes = XC * XC * 3
    expected = (info["duration"] or 0) * VIS_FPS
    last_err = ""
    for hw in (["-hwaccel", "auto"], []):  # try graphics-card decoding first
        cmd = [find_ffmpeg(), "-v", "error"] + hw + ["-i", video_path, "-an", "-vf", vf,
               "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             **_hidden_window())
        err_lines = []
        t = threading.Thread(target=lambda: err_lines.append(p.stderr.read()), daemon=True)
        t.start()
        parts, done = [], 0
        try:
            while True:
                data = p.stdout.read(frame_bytes * 100)  # 100 frames = 5 seconds
                n = len(data) // frame_bytes
                if n:
                    frames = np.frombuffer(data[:n * frame_bytes], np.uint8).reshape(n, XC, XC, 3)
                    parts.append(x_scores(frames))
                    done += n
                    if progress and expected:
                        progress(min(done / expected, 1.0))
                if len(data) < frame_bytes * 100:
                    break
                if cancel and cancel.is_set():
                    p.kill()
                    raise RuntimeError("Cancelled.")
        finally:
            p.wait()
            t.join(timeout=5)
        if done:
            return np.concatenate(parts)
        last_err = (err_lines[0] if err_lines else b"").decode("utf-8", "replace")
    raise RuntimeError("Couldn't read the video frames:\n" + video_path + "\n\n" + last_err.strip()[-600:])


def find_x_hits(curve, threshold):
    """(time, score, preview_time) for each time the X shows up.

    time         = first frame the X is seen (clips are measured from here)
    preview_time = a few frames later, once the X is fully drawn - the
                   clearest frame to look at when checking a kill
    """
    idx = np.flatnonzero(curve >= threshold)
    if len(idx) == 0:
        return []
    gap = int(0.5 * VIS_FPS)  # flickers less than 0.5 s apart count as one X
    runs = np.split(idx, np.flatnonzero(np.diff(idx) > gap) + 1)
    hits = []
    for r in runs:
        start = r[0]
        window = curve[start:start + int(0.3 * VIS_FPS) + 1]  # look up to 0.3 s in
        best = start + int(np.flatnonzero(window >= window.max() * 0.95)[0])
        hits.append((start / VIS_FPS, float(curve[r].max()), best / VIS_FPS))
    return hits


# ---------------------------------------------------------------------------
# Cutting and saving
# ---------------------------------------------------------------------------
def build_segments(hit_times, before, after, total):
    segs = []
    for t in sorted(hit_times):
        s, e = max(0.0, t - before), min(total, t + after)
        if e <= s:
            continue
        if segs and s <= segs[-1][1]:
            segs[-1][1] = max(segs[-1][1], e)
        else:
            segs.append([s, e])
    return segs


# Premiere Pro XML (Final Cut Pro 7 "xmeml" format, which Premiere imports)
def _rate_for(fps):
    for nominal in (24, 30, 60, 120):
        if abs(fps - nominal * 1000 / 1001) < 0.02:
            return nominal, True, nominal * 1000 / 1001
    tb = max(1, int(round(fps)))
    return tb, False, float(tb)


def _path_url(path):
    p = os.path.abspath(path).replace("\\", "/")
    if p.startswith("//"):                       # network share
        return "file:" + quote(p, safe="/")
    m = re.match(r"^([A-Za-z]):(.*)$", p)         # C:/Users/...
    if m:
        return "file://localhost/" + m.group(1) + "%3a" + quote(m.group(2), safe="/")
    return "file://localhost" + quote(p, safe="/")


def write_premiere_xml(out_path, video_path, info, segments, seq_name, audio_track=0):
    """Saves the cut list as Final Cut Pro 7 XML, which Premiere imports.

    audio_track: which of the file's audio tracks to put in the sequence
    (0 = first), or None for no audio.
    """
    tb, ntsc, fps = _rate_for(info["fps"] or 60.0)
    fr = lambda sec: int(round(sec * fps))
    rate = "<rate><timebase>%d</timebase><ntsc>%s</ntsc></rate>" % (tb, "TRUE" if ntsc else "FALSE")
    src_dur = fr(info["duration"])
    fname = escape(os.path.basename(video_path))
    w, h = info["width"], info["height"]
    tc = ("<timecode>%s<string>00:00:00:00</string><frame>0</frame>"
          "<displayformat>NDF</displayformat></timecode>" % rate)

    streams = info["audio_streams"]
    if audio_track is not None and not (0 <= audio_track < len(streams)):
        raise RuntimeError("The video doesn't have an audio track %d." % (audio_track + 1))
    if audio_track is None or not streams:
        chans, first_index, asr = [], 0, 48000
    else:
        sel = streams[audio_track]
        # Premiere numbers audio by channel across ALL tracks of the file: a file with
        # two stereo tracks has channels 1-2 (track 1) and 3-4 (track 2).
        first_index = sum(s["channels"] for s in streams[:audio_track])
        chans = list(range(1, min(sel["channels"], 2) + 1))  # mono -> 1, stereo/surround -> L+R
        asr = sel["rate"]
    total_channels = sum(s["channels"] for s in streams)

    audio_media = ""
    if streams:
        audio_media = ("<audio><samplecharacteristics><depth>16</depth><samplerate>%d</samplerate>"
                       "</samplecharacteristics><channelcount>%d</channelcount></audio>"
                       % (streams[0]["rate"], total_channels))
    file_full = ("<file id=\"file-1\"><name>%s</name><pathurl>%s</pathurl>%s<duration>%d</duration>%s"
                 "<media><video><samplecharacteristics>%s<width>%d</width><height>%d</height>"
                 "<anamorphic>FALSE</anamorphic><pixelaspectratio>square</pixelaspectratio>"
                 "<fielddominance>none</fielddominance></samplecharacteristics></video>%s</media></file>"
                 % (fname, escape(_path_url(video_path)), rate, src_dur, tc, rate, w, h, audio_media))
    file_ref = "<file id=\"file-1\"/>"

    def links(i):
        out = ["<link><linkclipref>clipitem-v%d</linkclipref><mediatype>video</mediatype>"
               "<trackindex>1</trackindex><clipindex>%d</clipindex></link>" % (i, i)]
        for ch in chans:
            out.append("<link><linkclipref>clipitem-a%d-%d</linkclipref><mediatype>audio</mediatype>"
                       "<trackindex>%d</trackindex><clipindex>%d</clipindex><groupindex>1</groupindex>"
                       "</link>" % (ch, i, ch, i))
        return "".join(out)

    video_items, audio_items = [], {ch: [] for ch in chans}
    pos = 0
    for i, (s, e) in enumerate(segments, start=1):
        fin, fout = fr(s), min(fr(e), src_dur)
        if fout <= fin:
            continue
        start, end = pos, pos + (fout - fin)
        pos = end
        common = ("<name>%s</name><enabled>TRUE</enabled><duration>%d</duration>%s"
                  "<start>%d</start><end>%d</end><in>%d</in><out>%d</out>"
                  % (fname, src_dur, rate, start, end, fin, fout))
        video_items.append("<clipitem id=\"clipitem-v%d\">%s%s%s</clipitem>"
                           % (i, common, file_full if i == 1 else file_ref, links(i) if chans else ""))
        for ch in chans:
            audio_items[ch].append(
                "<clipitem id=\"clipitem-a%d-%d\">%s%s<sourcetrack><mediatype>audio</mediatype>"
                "<trackindex>%d</trackindex></sourcetrack>%s</clipitem>"
                % (ch, i, common, file_ref, first_index + ch, links(i)))

    audio_tracks = "".join(
        "<track>%s<enabled>TRUE</enabled><locked>FALSE</locked>"
        "<outputchannelindex>%d</outputchannelindex></track>" % ("".join(audio_items[ch]), ch)
        for ch in chans)

    xml = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<!DOCTYPE xmeml>\n<xmeml version=\"4\">"
           "<sequence id=\"sequence-1\"><name>%s</name><duration>%d</duration>%s%s"
           "<media><video><format><samplecharacteristics>%s<width>%d</width><height>%d</height>"
           "<anamorphic>FALSE</anamorphic><pixelaspectratio>square</pixelaspectratio>"
           "<fielddominance>none</fielddominance></samplecharacteristics></format>"
           "<track>%s<enabled>TRUE</enabled><locked>FALSE</locked></track></video>"
           "<audio><numOutputChannels>2</numOutputChannels><format><samplecharacteristics>"
           "<depth>16</depth><samplerate>%d</samplerate></samplecharacteristics></format>%s</audio>"
           "</media></sequence></xmeml>\n"
           % (escape(seq_name), pos, rate, tc, rate, w, h, "".join(video_items), asr, audio_tracks))

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(xml)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def fmt_time(sec):
    sec = max(0.0, sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%04.1f" % (h, m, s) if h else "%02d:%04.1f" % (m, s)


# ---------------------------------------------------------------------------
# The app icon
# ---------------------------------------------------------------------------
def png_bytes(img):
    """Tiny PNG writer for RGB or RGBA images (used for the icon, and as a
    fallback for showing the preview in older Tk versions)."""
    import zlib
    import struct
    h, w, c = img.shape
    raw = np.hstack([np.zeros((h, 1), np.uint8), img.reshape(h, w * c)]).tobytes()
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    color_type = 6 if c == 4 else 2
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, color_type, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def make_icon(n):
    """The app icon: a red kill X like Battlefield's, as an n x n RGBA image."""
    ss = 4  # draw 4x bigger, then shrink, for smooth edges
    N = n * ss
    yy, xx = np.mgrid[0:N, 0:N]
    x, y = (xx + 0.5) / N - 0.5, (yy + 0.5) / N - 0.5
    u, v = np.abs(x + y) / np.sqrt(2), np.abs(x - y) / np.sqrt(2)  # along / across the arms

    def arms(width, r_in, r_out):
        return (((v <= width / 2) & (u >= r_in) & (u <= r_out)) |
                ((u <= width / 2) & (v >= r_in) & (v <= r_out)))

    core_w = max(0.075, 2.0 / n)   # thin like the game's X, but at least 2 pixels
    edge = max(0.025, 1.0 / n)     # dark edge so it shows on light and dark taskbars
    rgba = np.zeros((N, N, 4), np.float32)
    rgba[arms(core_w + 2 * edge, 0.14 - edge, 0.48 + edge)] = (90, 10, 10, 255)
    rgba[arms(core_w, 0.14, 0.48)] = (235, 55, 45, 255)
    rgba = rgba.reshape(n, ss, n, ss, 4).mean(axis=(1, 3))
    a = rgba[..., 3:4]
    rgb = np.where(a > 0, rgba[..., :3] * 255 / np.maximum(a, 1e-6), 0)
    return np.dstack([rgb, a]).clip(0, 255).astype(np.uint8)

# ---------------------------------------------------------------------------
# Putting it together
# ---------------------------------------------------------------------------
class Analysis:
    """Holds the results of a scan so the slider can update instantly."""

    def __init__(self, video_path, info, x_curve):
        self.video_path = video_path
        self.info = info
        self.x_curve = x_curve

    def hits(self, fuzziness):
        return find_x_hits(self.x_curve, threshold_for(fuzziness))  # [(time, score, preview)]

    def segments(self, hits, before, after):
        return build_segments([h[0] for h in hits], before, after, self.info["duration"])


_cache = {}  # so re-scanning the same video doesn't watch it all again


def analyse(video_path, progress=None, cancel=None):
    try:
        key = (os.path.abspath(video_path), os.path.getmtime(video_path), os.path.getsize(video_path))
    except OSError:
        key = None
    if key is not None and _cache.get("key") == key:
        return Analysis(video_path, _cache["info"], _cache["x"])
    info = probe(video_path)
    if not info["has_video"]:
        raise RuntimeError("This file has no video picture to look at.")
    x_curve = scan_red_x(video_path, info, progress, cancel)
    _cache.clear()
    if key is not None:
        _cache.update(key=key, info=info, x=x_curve)
    return Analysis(video_path, info, x_curve)


# ---------------------------------------------------------------------------
# The dialog window
# ---------------------------------------------------------------------------
def enable_file_drop(widget, on_drop):
    """Lets you drag files from Explorer onto a window (Windows only).

    Tkinter can't do this by itself, so this asks Windows directly: it marks the
    window as accepting dropped files and listens for the "files dropped"
    message. `on_drop` is called with the list of dropped paths.
    Returns True if drag and drop was switched on.

    IMPORTANT: `on_drop` runs inside Windows' message handling, while Tk is busy.
    It must NOT touch Tk/tkinter at all (that crashes the app) - it should only
    hand the paths over, e.g. by putting them on a queue.
    """
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes
    user32, shell32 = ctypes.windll.user32, ctypes.windll.shell32
    WM_DROPFILES, GWLP_WNDPROC = 0x0233, -4
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    set_proc = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
    set_proc.restype = ctypes.c_void_p
    set_proc.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    user32.CallWindowProcW.restype = LRESULT
    user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                                       wintypes.WPARAM, wintypes.LPARAM]
    shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
    shell32.DragQueryFileW.restype = wintypes.UINT
    shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_wchar_p, wintypes.UINT]
    shell32.DragFinish.argtypes = [ctypes.c_void_p]

    widget.update_idletasks()
    hwnd = widget.winfo_id()
    old_proc = [None]

    def window_proc(h, msg, wparam, lparam):
        if msg == WM_DROPFILES:
            paths = []
            try:
                count = shell32.DragQueryFileW(wparam, 0xFFFFFFFF, None, 0)
                for i in range(count):
                    length = shell32.DragQueryFileW(wparam, i, None, 0)
                    buf = ctypes.create_unicode_buffer(length + 1)
                    shell32.DragQueryFileW(wparam, i, buf, length + 1)
                    paths.append(buf.value)
                on_drop(paths)  # just hands them over - see the note above
            except Exception:
                pass
            finally:
                shell32.DragFinish(wparam)
            return 0
        return user32.CallWindowProcW(old_proc[0], h, msg, wparam, lparam)

    callback = WNDPROC(window_proc)
    widget._kc_drop_callback = callback  # must stay alive as long as the window
    old_proc[0] = set_proc(hwnd, GWLP_WNDPROC, ctypes.cast(callback, ctypes.c_void_p))
    if not old_proc[0]:
        return False
    shell32.DragAcceptFiles(hwnd, True)
    return True


# ---------------------------------------------------------------------------
# The video player: mpv (libmpv-2.dll), controlled directly through ctypes
# ---------------------------------------------------------------------------
APP_DIR = os.path.dirname(os.path.abspath(__file__))
MPV_DLL = "libmpv-2.dll"
MPV_SOURCES = ("shinchiro/mpv-winbuild-cmake", "zhongfly/mpv-winbuild")
MPV_MANUAL = ("To install it by hand: download the file named mpv-dev-x86_64-(date).7z from\n"
              "https://github.com/shinchiro/mpv-winbuild-cmake/releases\n"
              "open it (Windows 11 opens .7z files; on Windows 10 use 7-Zip), and copy\n"
              "libmpv-2.dll into the Kill Clipper folder:\n" + APP_DIR)

_MPV_FORMAT_FLAG, _MPV_FORMAT_DOUBLE = 3, 5
_MPV_EVENT_END_FILE, _MPV_EVENT_FILE_LOADED = 7, 8
_MPV_END_FILE_REASON_ERROR = 4


def load_libmpv():
    """Loads libmpv-2.dll from the Kill Clipper folder (or the system on Linux/Mac)."""
    import ctypes
    import ctypes.util
    if os.name == "nt":
        for name in (MPV_DLL, "mpv-2.dll"):
            path = os.path.join(APP_DIR, name)
            if os.path.isfile(path):
                try:
                    os.add_dll_directory(APP_DIR)
                except Exception:
                    pass
                try:
                    return ctypes.CDLL(path)
                except OSError as e:
                    raise RuntimeError("The video player file is there but Windows couldn't load it:\n"
                                       "%s\n\n%s\n\nMake sure you're using 64-bit Python." % (path, e))
        raise RuntimeError("The video player (libmpv-2.dll) isn't installed yet.")
    found = ctypes.util.find_library("mpv")
    if not found:
        raise RuntimeError("The video player (libmpv) isn't installed. Install mpv/libmpv "
                           "with your package manager.")
    return ctypes.CDLL(found)


class MpvPlayer:
    """A small wrapper around libmpv, showing the video inside a window (`wid`)."""

    def __init__(self, wid):
        import ctypes
        self._ct = ctypes
        lib = self.lib = load_libmpv()
        vp, cp, ci = ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int

        class _Event(ctypes.Structure):
            _fields_ = [("event_id", ci), ("error", ci), ("reply_userdata", ctypes.c_uint64),
                        ("data", vp)]
        self._Event = _Event

        lib.mpv_create.restype = vp
        lib.mpv_initialize.argtypes = [vp]
        lib.mpv_set_option_string.argtypes = [vp, cp, cp]
        lib.mpv_set_property_string.argtypes = [vp, cp, cp]
        lib.mpv_command.argtypes = [vp, ctypes.POINTER(cp)]
        lib.mpv_get_property.argtypes = [vp, cp, ci, vp]
        lib.mpv_wait_event.argtypes = [vp, ctypes.c_double]
        lib.mpv_wait_event.restype = ctypes.POINTER(_Event)
        lib.mpv_terminate_destroy.argtypes = [vp]
        lib.mpv_error_string.argtypes = [ci]
        lib.mpv_error_string.restype = cp

        self.h = lib.mpv_create()
        if not self.h:
            raise RuntimeError("The video player couldn't start (mpv_create failed).")
        options = {
            "wid": str(int(wid)),          # draw inside our window
            "keep-open": "yes",            # stay on the last frame instead of closing the file
            "idle": "yes",
            "pause": "yes",                # new videos start paused
            "osc": "no",                   # no mpv on-screen controls; we have our own
            "osd-level": "0",              # no "seeking..." text over the picture
            "input-default-bindings": "no",
            "input-vo-keyboard": "no",
            "input-cursor": "no",
            "cursor-autohide": "no",
            "hr-seek": "yes",              # jump to the exact frame, not the nearest keyframe
            "hwdec": "auto-safe",          # use the graphics card for decoding when possible
            "audio-display": "no",
            "drag-and-drop": "no",         # dropped files go to Kill Clipper, not mpv
        }
        for k, v in options.items():
            lib.mpv_set_option_string(self.h, k.encode(), v.encode())  # unknown options are ignored
        err = lib.mpv_initialize(self.h)
        if err < 0:
            lib.mpv_terminate_destroy(self.h)
            self.h = None
            raise RuntimeError("The video player couldn't start: %s" % self.error_text(err))

    def error_text(self, code):
        try:
            return self.lib.mpv_error_string(code).decode("utf-8", "replace")
        except Exception:
            return "error %d" % code

    def command(self, *args):
        if not self.h:
            return -1
        cp = self._ct.c_char_p
        arr = (cp * (len(args) + 1))(*[str(a).encode("utf-8") for a in args], None)
        return self.lib.mpv_command(self.h, arr)

    def set(self, name, value):
        if self.h:
            self.lib.mpv_set_property_string(self.h, name.encode(), str(value).encode("utf-8"))

    def _get(self, name, fmt, ctype):
        if not self.h:
            return None
        v = ctype()
        if self.lib.mpv_get_property(self.h, name.encode(), fmt, self._ct.byref(v)) < 0:
            return None
        return v.value

    # --- what the playback logic uses -----------------------------------------
    def load(self, path):
        self.set("pause", "yes")
        self.command("loadfile", path, "replace")

    def time_pos(self):
        return self._get("time-pos", _MPV_FORMAT_DOUBLE, self._ct.c_double)

    def is_paused(self):
        v = self._get("pause", _MPV_FORMAT_FLAG, self._ct.c_int)
        return True if v is None else bool(v)

    def set_paused(self, paused):
        self.set("pause", "yes" if paused else "no")

    def seek(self, t):
        self.command("seek", "%.3f" % max(0.0, t), "absolute+exact")

    def events(self):
        """Collects what mpv has reported since last time: [(kind, detail)]."""
        out = []
        while self.h:
            ev = self.lib.mpv_wait_event(self.h, 0).contents
            if ev.event_id == 0:
                break
            if ev.event_id == _MPV_EVENT_FILE_LOADED:
                out.append(("loaded", None))
            elif ev.event_id == _MPV_EVENT_END_FILE and ev.data:
                reason, error = self._ct.cast(ev.data, self._ct.POINTER(self._ct.c_int * 2)).contents
                if reason == _MPV_END_FILE_REASON_ERROR:
                    out.append(("error", self.error_text(error)))
        return out

    def close(self):
        if self.h:
            h, self.h = self.h, None
            self.lib.mpv_terminate_destroy(h)


class NoPlayer:
    """Stands in for the player when libmpv isn't installed, so nothing crashes."""
    def load(self, path): pass
    def time_pos(self): return None
    def is_paused(self): return True
    def set_paused(self, paused): pass
    def seek(self, t): pass
    def set(self, name, value): pass
    def events(self): return []
    def close(self): pass


# ---------------------------------------------------------------------------
# Kill-only playback: decides when to jump to the next kill
# ---------------------------------------------------------------------------
class KillPlayback:
    """Plays only the kill clips of the video by jumping over everything else.

    `segments` are the clips [(start, end), ...] in seconds, sorted and not
    overlapping. While `active` is False (before a scan) the whole video plays
    normally. call tick() many times a second.
    """
    EDGE = 0.04  # seconds of slack around clip starts (seeks can land a hair early)

    def __init__(self, player=None, clock=None):
        import time
        self.player = player or NoPlayer()
        self.clock = clock or time.monotonic
        self.segments = []
        self.active = False
        self.duration = 0.0
        self._pending = None  # (target time, when the seek was asked for)

    def set_segments(self, segments, active):
        self.segments = [(float(s), float(e)) for s, e in segments]
        self.active = bool(active)

    def index_at(self, t):
        """Which clip `t` is inside, or None if it's between clips."""
        for i, (s, e) in enumerate(self.segments):
            if s - self.EDGE <= t < e - 0.01:
                return i
        return None

    def _next_after(self, t):
        for i, (s, _e) in enumerate(self.segments):
            if s > t + self.EDGE:
                return i
        return None

    def position(self):
        """Where the playhead should be drawn (the seek target while a seek is under way)."""
        if self._pending:
            return self._pending[0]
        return self.player.time_pos()

    def current_index(self):
        pos = self.position()
        return None if pos is None else self.index_at(pos)

    def seek(self, t):
        self.player.seek(t)
        self._pending = (t, self.clock())

    def jump_to(self, i):
        """Go to the start of clip i. Doesn't change playing/paused."""
        if 0 <= i < len(self.segments):
            self.seek(self.segments[i][0])

    def next(self):
        pos = self.position()
        if pos is None or not self.segments:
            return False
        i = self._next_after(pos)
        if i is None:
            return False
        self.jump_to(i)
        return True

    def previous(self):
        pos = self.position()
        if pos is None or not self.segments:
            return False
        cur = self.index_at(pos)
        if cur is not None:
            target = max(cur - 1, 0)
        else:  # between clips: the previous kill is the last one that ended before here
            ended = [i for i, (_s, e) in enumerate(self.segments) if e <= pos + self.EDGE]
            target = ended[-1] if ended else 0
        self.jump_to(target)
        return True

    def play(self):
        pos = self.position()
        if self.active:
            if not self.segments:
                return
            if pos is not None and self.index_at(pos) is None:
                i = self._next_after(pos)
                self.jump_to(0 if i is None else i)  # after the last kill: start over
        elif pos is not None and self.duration and pos >= self.duration - 0.2:
            self.seek(0.0)  # at the very end of the video: start over
        self.player.set_paused(False)

    def pause(self):
        self.player.set_paused(True)

    def toggle(self):
        if self.player.is_paused():
            self.play()
        else:
            self.pause()

    def tick(self):
        if self._pending:
            target, started = self._pending
            pos = self.player.time_pos()
            if (pos is not None and abs(pos - target) < 0.25) or self.clock() - started > 1.5:
                self._pending = None
            else:
                return  # still seeking; don't make decisions on an old position
        if not self.active or self.player.is_paused():
            return
        if not self.segments:
            self.player.set_paused(True)
            return
        pos = self.player.time_pos()
        if pos is None or self.index_at(pos) is not None:
            return
        i = self._next_after(pos)
        if i is None:  # finished the last kill
            self.player.set_paused(True)
        else:
            self.jump_to(i)


# Timeline (progress bar) maths, kept separate so it can be tested
TL_PAD = 8


def timeline_x(t, duration, width):
    if not duration:
        return TL_PAD
    return TL_PAD + (width - 2 * TL_PAD) * min(max(t / duration, 0.0), 1.0)


def segment_at_x(x, segments, duration, width, tolerance=4):
    """Which kill section is under the mouse at x (with a little slack, since
    kills can be just a few pixels wide on a long video), or None."""
    best, best_d = None, None
    for i, (s, e) in enumerate(segments):
        x0, x1 = timeline_x(s, duration, width), timeline_x(e, duration, width)
        x1 = max(x1, x0 + 3)
        d = 0 if x0 <= x <= x1 else min(abs(x - x0), abs(x - x1))
        if d <= tolerance and (best_d is None or d < best_d):
            best, best_d = i, d
    return best


# ---------------------------------------------------------------------------
# Installing the player (used by "1 - Setup (run once).bat", and the app)
# ---------------------------------------------------------------------------
def pick_mpv_asset(assets):
    """From a GitHub release's files, the 64-bit libmpv package (not the 'v3' one,
    which only runs on newer CPUs)."""
    for a in assets:
        n = a.get("name", "")
        if n.startswith("mpv-dev-x86_64-") and "-v3-" not in n and n.endswith(".7z"):
            return a
    return None


def _extract_dll(archive, out_dir, log):
    """Gets libmpv-2.dll out of the .7z, trying several tools. Returns its path or None."""
    def found():
        for d, _dirs, files in os.walk(out_dir):
            for f in files:
                if f.lower() == MPV_DLL:
                    return os.path.join(d, f)
        return None

    try:  # 1) py7zr (installed by setup)
        import py7zr
        with py7zr.SevenZipFile(archive, "r") as z:
            names = [n for n in z.getnames() if os.path.basename(n).lower() == MPV_DLL]
            z.extract(path=out_dir, targets=names)
        if found():
            return found()
    except Exception as e:
        log("  (py7zr couldn't unpack it: %s)" % str(e).splitlines()[0][:120])
    try:  # 2) tar, built into Windows 10/11
        subprocess.run(["tar", "-xf", archive, "-C", out_dir], capture_output=True, timeout=300,
                       **_hidden_window())
        if found():
            return found()
    except Exception:
        pass
    for base in (os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")):
        exe = os.path.join(base, "7-Zip", "7z.exe")  # 3) 7-Zip, if you have it
        if base and os.path.isfile(exe):
            try:
                subprocess.run([exe, "e", archive, "-o" + out_dir, MPV_DLL, "-r", "-y"],
                               capture_output=True, timeout=300, **_hidden_window())
                if found():
                    return found()
            except Exception:
                pass
    return None


def install_player(log=print, progress=None, force=False):
    """Downloads libmpv-2.dll into the Kill Clipper folder. Returns its path."""
    import json
    import shutil
    import struct
    import tempfile
    import urllib.request
    dest = os.path.join(APP_DIR, MPV_DLL)
    if os.path.isfile(dest) and not force:
        log("The video player is already installed (%s)." % MPV_DLL)
        return dest
    if os.name != "nt":
        raise RuntimeError("Automatic install is for Windows. Install mpv/libmpv with your package manager.")
    if struct.calcsize("P") != 8:
        raise RuntimeError("Your Python is 32-bit, but the video player needs 64-bit Python.\n"
                           "Install the 64-bit version from https://www.python.org/downloads/")
    headers = {"User-Agent": "KillClipper", "Accept": "application/vnd.github+json"}
    last_err = "no download found"
    for repo in MPV_SOURCES:
        try:
            log("Looking for the latest video player from github.com/%s ..." % repo)
            req = urllib.request.Request("https://api.github.com/repos/%s/releases/latest" % repo,
                                         headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                release = json.load(r)
            asset = pick_mpv_asset(release.get("assets", []))
            if not asset:
                last_err = "no 64-bit libmpv file in the latest release of " + repo
                continue
            tmp = tempfile.mkdtemp(prefix="killclipper_mpv_")
            archive = os.path.join(tmp, asset["name"])
            log("Downloading %s ..." % asset["name"])
            req = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": "KillClipper"})
            with urllib.request.urlopen(req, timeout=60) as r, open(archive, "wb") as f:
                total = int(r.headers.get("Content-Length") or asset.get("size") or 0)
                done = 0
                while True:
                    chunk = r.read(1 << 18)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if progress and total:
                        progress(done / total)
            log("Unpacking ...")
            dll = _extract_dll(archive, os.path.join(tmp, "x"), log)
            if not dll:
                kept = os.path.join(APP_DIR, asset["name"])
                shutil.move(archive, kept)
                shutil.rmtree(tmp, ignore_errors=True)
                raise RuntimeError("Downloaded the video player but couldn't unpack it automatically.\n"
                                   "The download was saved as:\n%s\n\nOpen that file (Windows 11 opens "
                                   ".7z files; on Windows 10 use 7-Zip) and copy libmpv-2.dll into:\n%s"
                                   % (kept, APP_DIR))
            shutil.copyfile(dll, dest)
            shutil.rmtree(tmp, ignore_errors=True)
            log("Video player installed: %s" % dest)
            return dest
        except RuntimeError:
            raise
        except Exception as e:
            last_err = "%s: %s" % (repo, e)
            log("  That didn't work (%s)." % str(e).splitlines()[0][:150])
    raise RuntimeError("Couldn't download the video player (%s).\n\n%s" % (last_err, MPV_MANUAL))


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------
def _icon_shapes(c, kind, s, fg, level=100, muted=False):
    """Draws a player button icon on canvas `c` (size s x s)."""
    P = lambda *pts: [v * s for v in pts]
    if kind == "play":
        c.create_polygon(*P(0.37, 0.25, 0.37, 0.75, 0.77, 0.5), fill=fg, outline="")
    elif kind == "pause":
        c.create_rectangle(*P(0.31, 0.26, 0.44, 0.74), fill=fg, outline="")
        c.create_rectangle(*P(0.56, 0.26, 0.69, 0.74), fill=fg, outline="")
    elif kind == "prev":
        c.create_rectangle(*P(0.25, 0.27, 0.33, 0.73), fill=fg, outline="")
        c.create_polygon(*P(0.75, 0.27, 0.75, 0.73, 0.35, 0.5), fill=fg, outline="")
    elif kind == "next":
        c.create_rectangle(*P(0.67, 0.27, 0.75, 0.73), fill=fg, outline="")
        c.create_polygon(*P(0.25, 0.27, 0.25, 0.73, 0.65, 0.5), fill=fg, outline="")
    elif kind == "speaker":
        c.create_polygon(*P(0.16, 0.39, 0.30, 0.39, 0.48, 0.23, 0.48, 0.77, 0.30, 0.61, 0.16, 0.61),
                         fill=fg, outline="")
        w = max(1, int(s * 0.06))
        if muted:
            c.create_line(*P(0.60, 0.37, 0.84, 0.63), fill=fg, width=w)
            c.create_line(*P(0.60, 0.63, 0.84, 0.37), fill=fg, width=w)
        else:
            for r, need in ((0.13, 1), (0.25, 50)):
                if level >= need:
                    c.create_arc(*P(0.50 - r, 0.5 - r, 0.50 + r, 0.5 + r), start=-50, extent=100,
                                 style="arc", outline=fg, width=w)


def run_gui(start_video=None):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    if os.name == "nt":
        try:  # crisp text on high-DPI screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        try:  # gives Kill Clipper its own taskbar button/icon instead of Python's
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("KillClipper.App")
        except Exception:
            pass

    root = tk.Tk()
    root.title("%s %s" % (APP_NAME, APP_VERSION))

    def report_error(exc_type, exc, tb):
        # Unexpected errors pop up instead of disappearing (there's no console window)
        import traceback
        details = "".join(traceback.format_exception(exc_type, exc, tb))
        messagebox.showerror(APP_NAME, "Something unexpected went wrong:\n\n%s\n\n"
                                       "Details:\n%s" % (exc, details[-1200:]))
    root.report_callback_exception = report_error

    try:  # red X icon in the title bar and taskbar
        import base64
        icons = [tk.PhotoImage(data=base64.b64encode(png_bytes(make_icon(n))), format="PNG")
                 for n in (256, 64, 48, 32, 16)]
        root.iconphoto(True, *icons)
        root._kc_icons = icons  # keep them alive
    except Exception:
        pass
    root.geometry("1280x940")
    root.minsize(820, 640)
    try:
        ui = max(1.0, float(root.winfo_fpixels("1i")) / 96.0)  # bigger buttons on high-DPI screens
    except Exception:
        ui = 1.0
    try:
        bg = ttk.Style().lookup("TFrame", "background") or "#f0f0f0"
    except Exception:
        bg = "#f0f0f0"

    playback = KillPlayback()
    state = {"analysis": None, "hits": [], "segments": [], "worker": None, "tracks_for": None,
             "cancel": threading.Event(), "q": queue.Queue(),
             "video_info": None,      # probe() of the video in the player
             "player": None,          # MpvPlayer once it's running
             "player_error": None,    # why the player couldn't start
             "loaded": None,          # the video loaded in the player
             "installing": False, "muted": False,
             "hover": None, "shown_index": "x", "shown_paused": None, "shown_label": None,
             "head_x": None}

    video_var = tk.StringVar()
    track_var = tk.StringVar()
    track_note_var = tk.StringVar(value="Choose a video to see its audio tracks.")
    before_var = tk.StringVar(value="5")
    after_var = tk.StringVar(value="2")
    fuzz_var = tk.IntVar(value=50)
    status_var = tk.StringVar(value="Pick a gameplay video, then click Scan.")
    clip_var = tk.StringVar(value="")
    vol_var = tk.DoubleVar(value=100)
    vol_text = tk.StringVar(value="100%")

    frm = ttk.Frame(root, padding=14)
    frm.pack(fill="both", expand=True)
    frm.columnconfigure(1, weight=1)

    def unfocus(fn):
        """Wraps a button command so the keyboard shortcuts keep working afterwards."""
        def run(*a):
            fn(*a)
            root.after_idle(root.focus_set)
        return run

    video_types = [("Video files", " ".join("*" + e for e in VIDEO_EXTS)), ("All files", "*.*")]

    def browse_video():
        path = filedialog.askopenfilename(title="Choose the gameplay recording", filetypes=video_types)
        if path:
            open_video(path)

    def open_video(path):
        video_var.set(os.path.normpath(path))
        load_tracks()

    def on_drop(paths):
        files = [p for p in paths if os.path.isfile(p)]
        if not files:
            messagebox.showwarning(APP_NAME, "Drop a video file (not a folder) onto the window.")
            return
        videos = [p for p in files if os.path.splitext(p)[1].lower() in VIDEO_EXTS]
        open_video((videos or files)[0])
        if len(files) > 1:
            status_var.set("Several files were dropped. Using %s." % os.path.basename((videos or files)[0]))

    # --- settings (top) ---------------------------------------------------------------
    ttk.Label(frm, text="Gameplay video:").grid(row=0, column=0, sticky="w", pady=4)
    video_entry = ttk.Entry(frm, textvariable=video_var, width=40)
    video_entry.grid(row=0, column=1, sticky="ew", padx=6)
    ttk.Button(frm, text="Browse…", command=unfocus(browse_video)).grid(row=0, column=2)

    ttk.Label(frm, text="Audio track:").grid(row=1, column=0, sticky="w", pady=4)
    trk = ttk.Frame(frm)
    trk.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(6, 0))
    track_box = ttk.Combobox(trk, textvariable=track_var, state="disabled", values=[], width=40)
    track_box.pack(side="left")
    ttk.Label(trk, textvariable=track_note_var, foreground="#666").pack(side="left", padx=10)

    opts = ttk.Frame(frm)
    opts.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 0))
    opts.columnconfigure(5, weight=1)
    ttk.Label(opts, text="Seconds before each kill:").grid(row=0, column=0, sticky="w")
    before_entry = ttk.Entry(opts, textvariable=before_var, width=6)
    before_entry.grid(row=0, column=1, padx=(6, 18))
    ttk.Label(opts, text="Seconds after each kill:").grid(row=0, column=2, sticky="w")
    after_entry = ttk.Entry(opts, textvariable=after_var, width=6)
    after_entry.grid(row=0, column=3, padx=(6, 30))
    ttk.Label(opts, text="Fuzziness:").grid(row=0, column=4, sticky="w")
    scale = tk.Scale(opts, from_=0, to=100, orient="horizontal", variable=fuzz_var,
                     showvalue=True, resolution=1, length=260, takefocus=0)
    scale.grid(row=0, column=5, sticky="ew", padx=6)
    hint = ttk.Frame(opts)
    hint.grid(row=1, column=5, sticky="ew", padx=6)
    ttk.Label(hint, text="← strict: only clear X's", foreground="#666").pack(side="left")
    ttk.Label(hint, text="loose: catches faint or tiny X's →", foreground="#666").pack(side="right")

    acts = ttk.Frame(frm)
    acts.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(10, 0))
    acts.columnconfigure(1, weight=1)
    scan_btn = ttk.Button(acts, text="Scan video")
    scan_btn.grid(row=0, column=0)
    bar = ttk.Progressbar(acts, maximum=100)
    bar.grid(row=0, column=1, sticky="ew", padx=10)
    save_btn = ttk.Button(acts, text="Save XML sequence…", state="disabled")
    save_btn.grid(row=0, column=2)
    status_label = ttk.Label(frm, textvariable=status_var, justify="left")
    status_label.grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 2))
    frm.bind("<Configure>", lambda e: status_label.configure(wraplength=max(300, e.width - 40)))

    # --- the video ----------------------------------------------------------------------
    video_box = tk.Frame(frm, background="#000", height=360)
    video_box.grid(row=5, column=0, columnspan=3, sticky="nsew", pady=(6, 0))
    frm.rowconfigure(5, weight=1)
    screen = tk.Frame(video_box, background="#000")   # mpv draws in here
    screen.place(relx=0, rely=0, relwidth=1, relheight=1)
    video_msg = tk.Label(video_box, background="#000", foreground="#999", justify="center",
                         text="Drag a video onto this window, or click Browse…", wraplength=640)
    install_btn = ttk.Button(video_box, text="Install video player")

    def show_message(text, with_install=False):
        video_msg.configure(text=text)
        video_msg.place(relx=0.5, rely=0.45, anchor="center")
        video_msg.lift()
        if with_install:
            install_btn.place(relx=0.5, rely=0.62, anchor="center")
            install_btn.lift()
        else:
            install_btn.place_forget()

    def hide_message():
        video_msg.place_forget()
        install_btn.place_forget()

    show_message("Drag a video onto this window, or click Browse…")

    # --- the timeline (kill sections highlighted) ---------------------------------------
    TL_H = int(44 * ui)
    timeline = tk.Canvas(frm, height=TL_H, background=bg, highlightthickness=0, bd=0)
    timeline.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(8, 0))
    TL_TRACK, TL_KILL, TL_NOW, TL_HOVER, TL_HEAD = "#3a3a3a", "#c8372d", "#ff4d3f", "#ff9a8f", "#ffffff"

    def draw_timeline(*_):
        c = timeline
        c.delete("all")
        w, h = c.winfo_width(), TL_H
        if w < 30:
            return
        top, bot = int(10 * ui), h - int(10 * ui)
        c.create_rectangle(TL_PAD, top, w - TL_PAD, bot, fill=TL_TRACK, outline="")
        dur = playback.duration
        cur = playback.current_index()
        for i, (s, e) in enumerate(playback.segments):
            x0, x1 = timeline_x(s, dur, w), timeline_x(e, dur, w)
            x1 = max(x1, x0 + 3)
            color = TL_HOVER if i == state["hover"] else TL_NOW if i == cur else TL_KILL
            c.create_rectangle(x0, top, x1, bot, fill=color, outline="")
        state["head_x"] = None
        move_playhead()

    def move_playhead():
        pos = playback.position()
        w = timeline.winfo_width()
        if pos is None or not playback.duration or w < 30:
            timeline.delete("head")
            state["head_x"] = None
            return
        x = round(timeline_x(pos, playback.duration, w))
        if x == state["head_x"]:
            return
        state["head_x"] = x
        timeline.delete("head")
        t = max(2, int(5 * ui))
        timeline.create_line(x, int(4 * ui), x, TL_H - int(4 * ui), fill=TL_HEAD,
                             width=max(2, int(2 * ui)), tags="head")
        timeline.create_polygon(x - t, 0, x + t, 0, x, t + 2, fill=TL_HEAD, outline="#555", tags="head")

    def timeline_hit(x):
        return segment_at_x(x, playback.segments, playback.duration, timeline.winfo_width(),
                            tolerance=int(4 * ui))

    def timeline_motion(event):
        i = timeline_hit(event.x)
        timeline.configure(cursor="hand2" if i is not None else "")
        if i != state["hover"]:
            state["hover"] = i
            draw_timeline()

    def timeline_leave(_event):
        if state["hover"] is not None:
            state["hover"] = None
            draw_timeline()

    def timeline_click(event):
        root.focus_set()
        i = timeline_hit(event.x)
        if i is not None:          # clicks between kills do nothing
            playback.jump_to(i)
            update_transport()

    timeline.bind("<Configure>", draw_timeline)
    timeline.bind("<Motion>", timeline_motion)
    timeline.bind("<Leave>", timeline_leave)
    timeline.bind("<Button-1>", timeline_click)

    # --- player buttons -------------------------------------------------------------------
    class IconButton(tk.Canvas):
        def __init__(self, master, kind, command, size=36):
            size = int(size * ui)
            super().__init__(master, width=size, height=size, background=bg, highlightthickness=0,
                             bd=0, cursor="hand2", takefocus=0)
            self.kind, self.command, self.size = kind, command, size
            self.enabled, self.hover, self.extra = True, False, {}
            self.bind("<Enter>", lambda e: self._hover(True))
            self.bind("<Leave>", lambda e: self._hover(False))
            self.bind("<Button-1>", self._click)
            self.redraw()

        def _hover(self, on):
            self.hover = on
            self.redraw()

        def _click(self, _e):
            root.focus_set()
            if self.enabled:
                self.command()

        def set(self, kind=None, enabled=None, **extra):
            changed = False
            if kind is not None and kind != self.kind:
                self.kind, changed = kind, True
            if enabled is not None and enabled != self.enabled:
                self.enabled, changed = enabled, True
                self.configure(cursor="hand2" if enabled else "")
            if extra and extra != self.extra:
                self.extra, changed = extra, True
            if changed:
                self.redraw()

        def redraw(self):
            self.delete("all")
            s = self.size
            if self.hover and self.enabled:
                self.create_oval(1, 1, s - 1, s - 1, fill="#dcdcdc", outline="")
            _icon_shapes(self, self.kind, s, "#222" if self.enabled else "#aaa", **self.extra)

    ctl = ttk.Frame(frm)
    ctl.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(4, 0))
    prev_btn = IconButton(ctl, "prev", lambda: (playback.previous(), update_transport()))
    play_btn = IconButton(ctl, "play", lambda: (playback.toggle(), update_transport()), size=44)
    next_btn = IconButton(ctl, "next", lambda: (playback.next(), update_transport()))
    prev_btn.pack(side="left")
    play_btn.pack(side="left", padx=4)
    next_btn.pack(side="left")
    ttk.Label(ctl, textvariable=clip_var, font=("Segoe UI", 10)).pack(side="left", padx=14)

    link = tk.Label(ctl, text="github @ rwbronco", foreground="#2a6bd1", cursor="hand2",
                    font=("Segoe UI", 9, "underline"), background=bg)
    link.pack(side="right", padx=(16, 0))
    link.bind("<Button-1>", lambda e: __import__("webbrowser").open("https://github.com/rwbronco"))
    ttk.Label(ctl, textvariable=vol_text, width=5).pack(side="right")
    vol_scale = ttk.Scale(ctl, from_=0, to=100, orient="horizontal", variable=vol_var,
                          length=int(130 * ui), takefocus=0)
    vol_scale.pack(side="right", padx=4)
    speaker_btn = IconButton(ctl, "speaker", lambda: toggle_mute())
    speaker_btn.pack(side="right")

    def player():
        return state["player"] or NoPlayer()

    def apply_volume(*_):
        v = int(round(vol_var.get()))
        vol_text.set("%d%%" % v)
        if state["muted"] and v > 0:  # moving the slider un-mutes
            state["muted"] = False
            player().set("mute", "no")
        player().set("volume", v)
        speaker_btn.set(level=v, muted=state["muted"] or v == 0)

    def toggle_mute():
        state["muted"] = not state["muted"]
        player().set("mute", "yes" if state["muted"] else "no")
        v = int(round(vol_var.get()))
        speaker_btn.set(level=v, muted=state["muted"] or v == 0)

    vol_scale.configure(command=apply_volume)
    vol_scale.bind("<ButtonRelease-1>", lambda e: root.after_idle(root.focus_set))

    def apply_audio(*_):
        info = state["video_info"]
        if not info or not info["audio_streams"]:
            player().set("aid", "no")
        else:
            player().set("aid", max(0, track_box.current()) + 1)  # mpv counts audio tracks from 1

    track_box.bind("<<ComboboxSelected>>", lambda e: (apply_audio(), root.after_idle(root.focus_set)))

    def update_transport():
        """Refreshes the buttons, the time readout and the playhead."""
        p = state["player"]
        has_video = p is not None and state["loaded"] is not None
        clip_mode = playback.active
        segs = playback.segments
        paused = player().is_paused()
        play_btn.set(kind="play" if paused else "pause",
                     enabled=has_video and (not clip_mode or bool(segs)))
        prev_btn.set(enabled=has_video and clip_mode and bool(segs))
        next_btn.set(enabled=has_video and clip_mode and bool(segs))
        pos = playback.position()
        idx = playback.current_index()
        if not has_video or pos is None:
            label = ""
        elif clip_mode and segs:
            where = "Clip %d of %d" % (idx + 1, len(segs)) if idx is not None else "Between clips"
            label = "%s   ·   %s / %s" % (where, fmt_time(pos), fmt_time(playback.duration))
        else:
            label = "%s / %s" % (fmt_time(pos), fmt_time(playback.duration))
        if label != state["shown_label"]:
            state["shown_label"] = label
            clip_var.set(label)
        if idx != state["shown_index"]:   # highlight the clip that's playing
            state["shown_index"] = idx
            draw_timeline()
        else:
            move_playhead()

    # --- starting the player ----------------------------------------------------------------
    def ensure_player():
        if state["player"] or state["player_error"]:
            return state["player"]
        try:
            screen.update_idletasks()
            state["player"] = MpvPlayer(screen.winfo_id())
            playback.player = state["player"]
            apply_volume()
        except Exception as e:
            state["player_error"] = str(e)
            show_message("%s\n\nScanning and saving XML still work without it.%s"
                         % (state["player_error"],
                            "\nClick below to download it (about 30 MB), or run "
                            "\"1 - Setup (run once).bat\" again." if os.name == "nt" else ""),
                         with_install=(os.name == "nt"))
        return state["player"]

    def load_into_player(video):
        p = ensure_player()
        if not p:
            return
        hide_message()
        p.load(video)
        state["loaded"] = video
        apply_audio()
        update_transport()

    def install_clicked():
        if state["installing"]:
            return
        state["installing"] = True
        install_btn.configure(state="disabled")
        show_message("Downloading the video player…", with_install=True)
        q = state["q"]

        def work():
            try:
                install_player(log=lambda m: q.put(("install_msg", m)),
                               progress=lambda f: q.put(("install_msg", "Downloading the video player… %d%%"
                                                         % (f * 100))), force=True)
                q.put(("installed", None))
            except Exception as e:
                q.put(("install_error", str(e)))

        threading.Thread(target=work, daemon=True).start()

    install_btn.configure(command=install_clicked)

    # --- audio track dropdown ---------------------------------------------------------
    def load_tracks(*_):
        """Reads the chosen video's audio tracks and opens it in the player."""
        video = video_var.get().strip()
        if not os.path.isfile(video):
            return
        if state["tracks_for"] == video:
            return
        try:
            info = probe(video)
        except Exception as e:
            track_box.configure(values=[], state="disabled")
            track_var.set("")
            track_note_var.set("Couldn't read the audio tracks: %s" % str(e).splitlines()[0])
            state["tracks_for"] = None
            return
        state["tracks_for"] = video
        state["video_info"] = info
        labels = track_labels(info)
        if labels:
            track_box.configure(values=labels, state="readonly")
        else:
            track_box.configure(values=["(no audio in this file)"], state="disabled")
        track_box.current(0)
        track_note_var.set("%d audio track%s in this file." % (len(labels), "" if len(labels) == 1 else "s"))
        # a different video: forget the old scan
        if state["analysis"] is not None and state["analysis"].video_path != video:
            state["analysis"], state["hits"], state["segments"] = None, [], []
            save_btn.configure(state="disabled")
            bar["value"] = 0
            status_var.set("Click Scan to find the kills. Until then the whole video plays.")
        playback.duration = info["duration"] or 0.0
        apply_segments()
        if info["has_video"]:
            load_into_player(video)

    video_entry.bind("<FocusOut>", load_tracks)
    video_entry.bind("<Return>", load_tracks)

    # --- results ------------------------------------------------------------------
    def read_seconds(var, name):
        try:
            v = float(var.get().strip().replace(",", "."))
            if v < 0:
                raise ValueError
            return v
        except ValueError:
            raise ValueError("'%s' needs to be a number of seconds, like 5 or 2.5" % name)

    def apply_segments():
        """Hands the current clips to the player and redraws the timeline."""
        playback.set_segments(state["segments"], active=state["analysis"] is not None)
        state["hover"] = None
        draw_timeline()
        update_transport()

    def update_summary():
        """Rebuilds the clips from the kills and the before/after seconds."""
        a = state["analysis"]
        if a is None:
            return
        try:
            before = read_seconds(before_var, "Seconds before")
            after = read_seconds(after_var, "Seconds after")
        except ValueError as e:
            status_var.set(str(e))
            return  # keep playing the last good clips until the number is fixed
        hits = state["hits"]
        segs = a.segments(hits, before, after)
        state["segments"] = segs
        total = sum(e - s for s, e in segs)
        best = round(float(a.x_curve.max()) * 100) if len(a.x_curve) else 0
        needed = round(threshold_for(fuzz_var.get()) * 100)
        if hits:
            status_var.set("%d kill%s  →  %d clip%s, %s total.   (Strongest X anywhere: %d%%. Needed: %d%%.)"
                           % (len(hits), "" if len(hits) == 1 else "s",
                              len(segs), "" if len(segs) == 1 else "s", fmt_time(total), best, needed))
        else:
            status_var.set("No kills found at this fuzziness. Strongest X anywhere: %d%%, and %d%% "
                           "is needed. Try moving the slider right." % (best, needed))
        save_btn.configure(state="normal" if segs else "disabled")
        apply_segments()

    def refresh(*_):
        """Finds the kills again (after a scan or when the slider moves)."""
        a = state["analysis"]
        if a is None:
            return
        state["hits"] = a.hits(fuzz_var.get())
        update_summary()

    scale.configure(command=lambda _v: refresh())
    scale.bind("<ButtonRelease-1>", lambda e: root.after_idle(root.focus_set))
    before_entry.bind("<KeyRelease>", lambda e: update_summary())
    after_entry.bind("<KeyRelease>", lambda e: update_summary())

    # --- keyboard shortcuts ------------------------------------------------------------
    def shortcut(fn):
        def handler(event):
            try:
                cls = event.widget.winfo_class()
            except Exception:
                return None
            if cls in ("Entry", "TEntry", "TCombobox", "Scale", "TScale", "Listbox"):
                return None  # typing in a box: leave the keys alone
            fn()
            update_transport()
            return "break"
        return handler

    root.bind_all("<space>", shortcut(lambda: play_btn.enabled and playback.toggle()))
    root.bind_all("<Left>", shortcut(lambda: prev_btn.enabled and playback.previous()))
    root.bind_all("<Right>", shortcut(lambda: next_btn.enabled and playback.next()))
    root.bind_all("<m>", shortcut(toggle_mute))

    # --- background work and the player clock ---------------------------------------------------
    def poll():
        try:
            while True:
                kind, payload = state["q"].get_nowait()
                if kind == "progress":
                    bar["value"] = payload * 100
                    state["scan_pct"] = int(payload * 100)
                    show_scan_text()
                elif kind == "done":
                    state["analysis"] = payload
                    bar["value"] = 100
                    scan_btn.configure(text="Scan video")
                    state["worker"] = None
                    refresh()
                    if playback.segments:
                        playback.jump_to(0)
                        update_transport()
                elif kind == "error":
                    bar["value"] = 0
                    scan_btn.configure(text="Scan video")
                    state["worker"] = None
                    if payload == "Cancelled.":
                        status_var.set("Scan cancelled.")
                    else:
                        status_var.set("Something went wrong. See the message box.")
                        messagebox.showerror(APP_NAME, payload)
                elif kind == "drop":
                    on_drop(payload)
                elif kind == "install_msg":
                    show_message(payload, with_install=True)
                elif kind == "installed":
                    state["installing"], state["player_error"] = False, None
                    install_btn.configure(state="normal")
                    hide_message()
                    if state["tracks_for"]:
                        load_into_player(state["tracks_for"])
                    elif ensure_player():
                        show_message("Video player installed. Drag a video onto this window, or click Browse…")
                elif kind == "install_error":
                    state["installing"] = False
                    install_btn.configure(state="normal")
                    show_message("The video player couldn't be installed.\n\n" + payload, with_install=True)
        except queue.Empty:
            pass
        root.after(50, poll)

    def clock():
        """Runs ~70 times a second: lets the playback jump over non-kill parts."""
        p = state["player"]
        if p is not None:
            for kind, detail in p.events():
                if kind == "loaded":
                    apply_audio()
                elif kind == "error":
                    status_var.set("The player couldn't open this video: %s" % detail)
            playback.tick()
            update_transport()
        root.after(14, clock)

    # --- fun messages while scanning (like The Sims' "Reticulating splines") ----------------
    def show_scan_text():
        status_var.set("%d%% %s%s" % (state["scan_pct"], state["scan_phrase"], "." * state["scan_dots"]))

    def scan_ticker(job):
        """Adds a dot every 0.4 s and switches to a new random phrase every 5 s."""
        if state["worker"] is None or job != state["scan_job"]:
            return  # the scan finished, failed or was cancelled
        state["scan_dots"] = state["scan_dots"] % 3 + 1
        if time.monotonic() - state["scan_phrase_since"] >= SCAN_PHRASE_SECONDS:
            state["scan_phrase_since"] = time.monotonic()
            state["scan_phrase"] = random.choice([p for p in SCAN_PHRASES if p != state["scan_phrase"]])
        show_scan_text()
        root.after(400, scan_ticker, job)

    def scan():
        if state["worker"]:  # button acts as Cancel while scanning
            state["cancel"].set()
            return
        video = video_var.get().strip()
        if not os.path.isfile(video):
            messagebox.showwarning(APP_NAME, "Please choose the gameplay video first.")
            return
        load_tracks()
        save_btn.configure(state="disabled")
        bar["value"] = 0
        scan_btn.configure(text="Cancel")
        state["cancel"] = threading.Event()
        q, cancel = state["q"], state["cancel"]

        def work():
            try:
                q.put(("done", analyse(video, lambda f: q.put(("progress", f)), cancel)))
            except Exception as e:
                q.put(("error", str(e)))

        state["worker"] = threading.Thread(target=work, daemon=True)
        state["worker"].start()
        state.update(scan_pct=0, scan_phrase=SCAN_PHRASES[0], scan_dots=1, scan_phrase_since=time.monotonic(),
                     scan_job=state.get("scan_job", 0) + 1)
        show_scan_text()
        root.after(400, scan_ticker, state["scan_job"])

    def save():
        a = state["analysis"]
        update_summary()
        segs = state["segments"]
        if not a or not segs:
            return
        media_path = a.video_path  # the sequence links to the file you scanned
        info = a.info
        track = max(0, track_box.current()) if info["audio_streams"] else None
        base = os.path.splitext(os.path.basename(a.video_path))[0]
        path = filedialog.asksaveasfilename(
            title="Save XML sequence", defaultextension=".xml",
            initialdir=os.path.dirname(a.video_path), initialfile=base + " - kills.xml",
            filetypes=[("Final Cut Pro 7 XML (Premiere, Resolve…)", "*.xml")])
        if not path:
            return
        try:
            write_premiere_xml(path, media_path, info, segs, base + " - kills", track)
        except Exception as e:
            messagebox.showerror(APP_NAME, "Couldn't save the file:\n\n%s" % e)
            return
        messagebox.showinfo(APP_NAME, "Saved!\n\n%s\n\nVideo: %s\nAudio: %s\nKills: %d (%d clips)\n\n"
                                      "Premiere: File > Import, pick the .xml.\n"
                                      "Resolve: File > Import > Timeline, pick the .xml.\n"
                                      "It opens as a sequence named \"%s - kills\"."
                            % (path, os.path.basename(media_path),
                               "Track %d" % (track + 1) if track is not None else "none",
                               len(state["hits"]), len(segs), base))

    def on_close():
        state["cancel"].set()
        try:
            if state["player"]:
                state["player"].close()
        finally:
            root.destroy()

    scan_btn.configure(command=unfocus(scan))
    save_btn.configure(command=unfocus(save))
    root.protocol("WM_DELETE_WINDOW", on_close)
    try:
        # Dropped files go on the queue; poll() opens them a moment later, safely.
        if enable_file_drop(root, lambda paths: state["q"].put(("drop", paths))):
            status_var.set("Drag a gameplay video onto this window (or click Browse…), then click Scan.")
    except Exception:
        pass  # drag and drop is a nice extra; Browse… still works without it
    if start_video and os.path.isfile(start_video):  # e.g. a video dropped onto the launcher
        root.after(200, lambda: open_video(start_video))
    update_transport()
    root.after(50, poll)
    root.after(100, clock)
    root.mainloop()


# ---------------------------------------------------------------------------
# Command-line mode (optional):
#   python kill_clipper.py VIDEO BEFORE AFTER FUZZINESS OUTPUT.xml [AUDIO_TRACK]
#   (AUDIO_TRACK counts from 1; default 1)
# ---------------------------------------------------------------------------
def run_cli(args):
    video, before, after, fuzz, out = args[:5]
    track = int(args[5]) - 1 if len(args) > 5 else 0
    a = analyse(video)
    hits = a.hits(float(fuzz))
    segs = a.segments(hits, float(before), float(after))
    for t, sc, _ in hits:
        print("%s  red X %d%%" % (fmt_time(t), round(sc * 100)))
    print("%d kills -> %d clips" % (len(hits), len(segs)))
    media, info = video, a.info
    for line in track_labels(info):
        print("   ", line)
    base = os.path.splitext(os.path.basename(video))[0]
    write_premiere_xml(out, media, info, segs, base + " - kills",
                       track if info["audio_streams"] else None)
    print("Sequence uses", media, "with audio track", track + 1)
    print("Saved", out)


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--install-player":
        # used by "1 - Setup (run once).bat"
        def _bar(f):
            print("\r  %3d%%" % (f * 100), end="", flush=True)
            if f >= 1:
                print()
        try:
            install_player(progress=_bar)
        except Exception as e:
            print()
            print(e)
            sys.exit(1)
    elif len(sys.argv) in (6, 7):
        run_cli(sys.argv[1:])
    else:
        try:
            run_gui(sys.argv[1] if len(sys.argv) == 2 else None)
        except Exception as e:
            import traceback
            _startup_error("Kill Clipper couldn't start:\n\n%s\n\n%s" % (e, traceback.format_exc()[-1200:]))
