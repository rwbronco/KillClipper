"""
Kill Clipper
============
Finds every kill in a Battlefield 6 gameplay recording by watching for the
red kill "X" around the crosshair, then builds a Premiere Pro sequence
containing only those moments.

How it works:
  1. The middle of the screen is checked 20 times per second for the red X.
  2. Each frame gets a match score (0-100%). Wherever the score is above the
     threshold set by the Fuzziness slider, that's a kill.
  3. Each kill becomes a clip: X seconds before, Y seconds after.
  4. Overlapping clips are merged, and the result is saved as an XML file
     that Premiere opens as a ready-cut sequence (File > Import), with the
     audio track you picked.

Run it by double-clicking "Run Kill Clipper.bat" (Windows).
"""

import os
import re
import sys
import queue
import threading
import subprocess
from urllib.parse import quote
from xml.sax.saxutils import escape

APP_NAME = "Kill Clipper"
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
# Preview frames
# ---------------------------------------------------------------------------
def grab_frame(video_path, info, t):
    """The full-size video frame at `t` seconds, as an (height, width, 3) RGB array."""
    w, h = info["width"], info["height"]
    cmd = [find_ffmpeg(), "-v", "error", "-ss", "%.3f" % max(0.0, t), "-i", video_path,
           "-frames:v", "1", "-an", "-sn", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    p = subprocess.run(cmd, capture_output=True, **_hidden_window())
    if len(p.stdout) < w * h * 3:
        raise RuntimeError("Couldn't read the frame at %s.\n%s"
                           % (fmt_time(t), p.stderr.decode("utf-8", "replace").strip()[-300:]))
    return np.frombuffer(p.stdout[:w * h * 3], np.uint8).reshape(h, w, 3)


def fit_size(src_w, src_h, box_w, box_h):
    """Largest size with the video's shape that fits in the box."""
    scale = min(box_w / src_w, box_h / src_h)
    return max(1, int(src_w * scale)), max(1, int(src_h * scale))


def resize_rgb(img, w, h):
    """Smoothly resizes an RGB image (numpy only, no extra installs)."""
    H, W = img.shape[:2]
    f = int(min(W / w, H / h))
    if f >= 2:  # shrink by a whole number first by averaging blocks of pixels
        img = img[:H // f * f, :W // f * f].reshape(H // f, f, W // f, f, 3).mean(axis=(1, 3))
        H, W = img.shape[:2]
    img = img.astype(np.float32)
    ys = np.clip((np.arange(h) + 0.5) * H / h - 0.5, 0, H - 1)
    xs = np.clip((np.arange(w) + 0.5) * W / w - 0.5, 0, W - 1)
    y0, x0 = ys.astype(int), xs.astype(int)
    y1, x1 = np.minimum(y0 + 1, H - 1), np.minimum(x0 + 1, W - 1)
    wy, wx = (ys - y0)[:, None, None], (xs - x0)[None, :, None]
    top = img[y0][:, x0] * (1 - wx) + img[y0][:, x1] * wx
    bot = img[y1][:, x0] * (1 - wx) + img[y1][:, x1] * wx
    return np.clip(top * (1 - wy) + bot * wy + 0.5, 0, 255).astype(np.uint8)


def ppm_bytes(img):
    h, w = img.shape[:2]
    return b"P6 %d %d 255\n" % (w, h) + np.ascontiguousarray(img).tobytes()


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


def run_gui(start_video=None):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    if os.name == "nt":
        try:  # crisp text on high-DPI screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    CHECKED, UNCHECKED = "☑", "☐"

    if os.name == "nt":
        try:  # gives Kill Clipper its own taskbar button/icon instead of Python's
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("KillClipper.App")
        except Exception:
            pass

    root = tk.Tk()
    root.title(APP_NAME)

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
    root.geometry("1440x680")
    root.minsize(900, 520)

    state = {"analysis": None, "hits": [], "segments": [], "worker": None, "tracks_for": None,
             "cancel": threading.Event(), "q": queue.Queue(),
             "unchecked": [],          # preview times of kills you've unticked
             "row_hit": {},            # list row id -> hit
             "video_info": None,       # probe() of the video shown in the preview
             "frame": None,            # full-size frame currently shown
             "frame_cache": {},        # (video, time) -> full-size frame
             "want": None,             # (video, time) the preview should be showing
             "photo": None, "resize_job": None}

    video_var = tk.StringVar()
    track_var = tk.StringVar()
    track_note_var = tk.StringVar(value="Choose a video to see its audio tracks.")
    before_var = tk.StringVar(value="5")
    after_var = tk.StringVar(value="2")
    fuzz_var = tk.IntVar(value=50)
    below_var = tk.StringVar(value="80")
    status_var = tk.StringVar(value="Pick a gameplay video, then click Scan.")
    caption_var = tk.StringVar(value="")

    # Two panes side by side; drag the divider to resize them
    panes = ttk.PanedWindow(root, orient="horizontal")
    panes.pack(fill="both", expand=True)
    frm = ttk.Frame(panes, padding=14)
    right = ttk.Frame(panes, padding=(4, 14, 14, 14))
    panes.add(frm, weight=0)
    panes.add(right, weight=1)
    frm.columnconfigure(1, weight=1)

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

    ttk.Label(frm, text="Gameplay video:").grid(row=0, column=0, sticky="w", pady=4)
    video_entry = ttk.Entry(frm, textvariable=video_var, width=40)
    video_entry.grid(row=0, column=1, sticky="ew", padx=6)
    ttk.Button(frm, text="Browse…", command=browse_video).grid(row=0, column=2)

    ttk.Label(frm, text="Audio track:").grid(row=1, column=0, sticky="w", pady=4)
    track_box = ttk.Combobox(frm, textvariable=track_var, state="disabled", values=[])
    track_box.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(6, 0))
    ttk.Label(frm, textvariable=track_note_var, foreground="#666").grid(
        row=2, column=1, columnspan=2, sticky="w", padx=6)

    times = ttk.Frame(frm)
    times.grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 4))
    ttk.Label(times, text="Seconds before each kill:").pack(side="left")
    before_entry = ttk.Entry(times, textvariable=before_var, width=6)
    before_entry.pack(side="left", padx=(6, 20))
    ttk.Label(times, text="Seconds after each kill:").pack(side="left")
    after_entry = ttk.Entry(times, textvariable=after_var, width=6)
    after_entry.pack(side="left", padx=6)

    fz = ttk.Frame(frm)
    fz.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 0))
    fz.columnconfigure(1, weight=1)
    ttk.Label(fz, text="Fuzziness:").grid(row=0, column=0, sticky="w")
    scale = tk.Scale(fz, from_=0, to=100, orient="horizontal", variable=fuzz_var,
                     showvalue=True, resolution=1, length=300)
    scale.grid(row=0, column=1, sticky="ew", padx=6)
    hint = ttk.Frame(fz)
    hint.grid(row=1, column=1, sticky="ew", padx=6)
    ttk.Label(hint, text="← strict: only clear X's", foreground="#666").pack(side="left")
    ttk.Label(hint, text="loose: catches faint or tiny X's →", foreground="#666").pack(side="right")

    btns = ttk.Frame(frm)
    btns.grid(row=5, column=0, columnspan=3, sticky="ew", pady=12)
    scan_btn = ttk.Button(btns, text="Scan video")
    scan_btn.pack(side="left")
    save_btn = ttk.Button(btns, text="Save XML sequence…", state="disabled")
    save_btn.pack(side="right")

    bar = ttk.Progressbar(frm, maximum=100)
    bar.grid(row=6, column=0, columnspan=3, sticky="ew")
    ttk.Label(frm, textvariable=status_var, wraplength=520, justify="left").grid(
        row=7, column=0, columnspan=3, sticky="w", pady=(8, 4))

    # The kills list: a checkbox column, then number, time and score
    lst_frame = ttk.Frame(frm)
    lst_frame.grid(row=8, column=0, columnspan=3, sticky="nsew")
    frm.rowconfigure(8, weight=1)
    lst = ttk.Treeview(lst_frame, columns=("use", "num", "time", "score"), show="headings",
                       selectmode="extended", height=12)
    for col, title, width, anchor in (("use", "Use", 50, "center"), ("num", "#", 50, "e"),
                                      ("time", "Time", 110, "center"), ("score", "Red X", 80, "e")):
        lst.heading(col, text=title)
        lst.column(col, width=width, anchor=anchor, stretch=(col == "time"))
    lst.tag_configure("off", foreground="#999")
    sb = ttk.Scrollbar(lst_frame, orient="vertical", command=lst.yview)
    lst.configure(yscrollcommand=sb.set)
    lst.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")

    bulk = ttk.Frame(frm)
    bulk.grid(row=9, column=0, columnspan=3, sticky="ew", pady=(6, 0))
    check_all_btn = ttk.Button(bulk, text="Check all")
    check_all_btn.pack(side="left")
    uncheck_all_btn = ttk.Button(bulk, text="Uncheck all")
    uncheck_all_btn.pack(side="left", padx=6)
    below_btn = ttk.Button(bulk, text="Uncheck below")
    below_btn.pack(side="left", padx=(12, 4))
    ttk.Entry(bulk, textvariable=below_var, width=4).pack(side="left")
    ttk.Label(bulk, text="%").pack(side="left")
    ttk.Label(frm, text="Click a kill to preview it. Click its box (or press Space) to include or "
                        "leave it out.", foreground="#666", wraplength=520).grid(
        row=10, column=0, columnspan=3, sticky="w", pady=(6, 0))

    # The preview
    right.columnconfigure(0, weight=1)
    right.rowconfigure(0, weight=1)
    preview_box = tk.Frame(right, background="#111", width=848, height=480)
    preview_box.grid(row=0, column=0, sticky="nsew")
    preview_box.grid_propagate(False)
    preview_label = tk.Label(preview_box, background="#111", foreground="#888",
                             text="Drag a video onto this window, or click Browse…")
    preview_label.place(relx=0.5, rely=0.5, anchor="center")
    ttk.Label(right, textvariable=caption_var).grid(row=1, column=0, sticky="w", pady=(6, 0))
    link = tk.Label(right, text="github @ rwbronco", foreground="#2a6bd1", cursor="hand2",
                    font=("Segoe UI", 9, "underline"))
    link.grid(row=1, column=0, sticky="e", pady=(6, 0))
    link.bind("<Button-1>", lambda e: __import__("webbrowser").open("https://github.com/rwbronco"))

    # --- preview ------------------------------------------------------------------
    def show_frame():
        """Draws the current frame, scaled to fit the preview area."""
        state["resize_job"] = None
        frame = state["frame"]
        if frame is None:
            return
        box_w, box_h = preview_box.winfo_width(), preview_box.winfo_height()
        if box_w < 10 or box_h < 10:
            return
        w, h = fit_size(frame.shape[1], frame.shape[0], box_w, box_h)
        small = resize_rgb(frame, w, h)
        try:
            photo = tk.PhotoImage(data=ppm_bytes(small), format="PPM")
        except tk.TclError:
            import base64
            photo = tk.PhotoImage(data=base64.b64encode(png_bytes(small)), format="PNG")
        state["photo"] = photo  # keep a reference, or Tk throws the image away
        preview_label.configure(image=photo, text="")

    def on_preview_resize(_event=None):
        if state["resize_job"]:
            root.after_cancel(state["resize_job"])
        state["resize_job"] = root.after(80, show_frame)  # wait until resizing pauses

    preview_box.bind("<Configure>", on_preview_resize)

    def request_frame(video, t, caption):
        """Shows the frame at `t`, loading it in the background if needed."""
        key = (video, round(t, 2))
        state["want"] = key
        caption_var.set(caption)
        if key in state["frame_cache"]:
            state["frame"] = state["frame_cache"][key]
            show_frame()
            return
        info = state["video_info"]
        q = state["q"]

        def work():
            try:
                q.put(("frame", (key, grab_frame(video, info, t))))
            except Exception as e:
                q.put(("frame_error", (key, str(e).splitlines()[0])))

        threading.Thread(target=work, daemon=True).start()

    # --- audio track dropdown ---------------------------------------------------------
    def load_tracks(*_):
        """Fills the dropdown with the audio tracks of the chosen video file,
        and shows its first frame in the preview."""
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
        state["frame_cache"].clear()
        labels = track_labels(info)
        if labels:
            track_box.configure(values=labels, state="readonly")
        else:
            track_box.configure(values=["(no audio in this file)"], state="disabled")
        track_box.current(0)
        track_note_var.set("%d audio track%s in this file." % (len(labels), "" if len(labels) == 1 else "s"))
        if info["has_video"]:
            request_frame(video, 0.0, "First frame of %s" % os.path.basename(video))

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

    def is_unchecked(hit):
        return any(abs(hit[2] - t) < 0.3 for t in state["unchecked"])

    def set_checked(hit, on):
        state["unchecked"] = [t for t in state["unchecked"] if abs(hit[2] - t) >= 0.3]
        if not on:
            state["unchecked"].append(hit[2])

    def update_summary():
        """Recounts the ticked kills, rebuilds the clips and updates the status line."""
        a = state["analysis"]
        if a is None:
            return
        try:
            before = read_seconds(before_var, "Seconds before")
            after = read_seconds(after_var, "Seconds after")
        except ValueError as e:
            status_var.set(str(e))
            save_btn.configure(state="disabled")
            return
        hits = state["hits"]
        chosen = [h for h in hits if not is_unchecked(h)]
        segs = a.segments(chosen, before, after)
        state["segments"] = segs
        total = sum(e - s for s, e in segs)
        best = round(float(a.x_curve.max()) * 100) if len(a.x_curve) else 0
        needed = round(threshold_for(fuzz_var.get()) * 100)
        if hits:
            status_var.set("%d of %d kill%s selected  →  %d clip%s, %s total.   "
                           "(Strongest X anywhere: %d%%. Needed: %d%%.)"
                           % (len(chosen), len(hits), "" if len(hits) == 1 else "s",
                              len(segs), "" if len(segs) == 1 else "s", fmt_time(total), best, needed))
        else:
            status_var.set("No kills found at this fuzziness. Strongest X anywhere: %d%%, and %d%% "
                           "is needed. Try moving the slider right." % (best, needed))
        save_btn.configure(state="normal" if segs else "disabled")

    def refresh(*_):
        """Rebuilds the kills list (after a scan or when the slider moves)."""
        a = state["analysis"]
        if a is None:
            return
        selected_times = [state["row_hit"][i][2] for i in lst.selection() if i in state["row_hit"]]
        hits = a.hits(fuzz_var.get())
        state["hits"] = hits
        lst.delete(*lst.get_children())
        state["row_hit"] = {}
        reselect = []
        for n, hit in enumerate(hits, start=1):
            off = is_unchecked(hit)
            row = lst.insert("", "end", values=(UNCHECKED if off else CHECKED, n, fmt_time(hit[0]),
                                                "%d%%" % round(hit[1] * 100)),
                             tags=("off",) if off else ())
            state["row_hit"][row] = hit
            if any(abs(hit[2] - t) < 0.3 for t in selected_times):
                reselect.append(row)
        if reselect:
            lst.selection_set(reselect)
            lst.see(reselect[0])
        update_summary()

    def redraw_row(row):
        hit = state["row_hit"][row]
        off = is_unchecked(hit)
        vals = list(lst.item(row, "values"))
        vals[0] = UNCHECKED if off else CHECKED
        lst.item(row, values=vals, tags=("off",) if off else ())

    def toggle(rows):
        rows = [r for r in rows if r in state["row_hit"]]
        if not rows:
            return
        # if any of them is ticked, untick them all; otherwise tick them all
        turn_on = all(is_unchecked(state["row_hit"][r]) for r in rows)
        for r in rows:
            set_checked(state["row_hit"][r], turn_on)
            redraw_row(r)
        update_summary()

    def on_click(event):
        if lst.identify_region(event.x, event.y) == "cell" and lst.identify_column(event.x) == "#1":
            row = lst.identify_row(event.y)
            if row:
                toggle([row])
                return "break"  # don't change the selection when only ticking

    def on_select(_event=None):
        sel = lst.selection()
        if not sel or sel[0] not in state["row_hit"] or not state["analysis"]:
            return
        hit = state["row_hit"][sel[0]]
        n = lst.item(sel[0], "values")[1]
        request_frame(state["analysis"].video_path, hit[2],
                      "Kill %s  ·  %s  ·  red X %d%%" % (n, fmt_time(hit[2]), round(hit[1] * 100)))

    def bulk_set(on, below=None):
        for row, hit in state["row_hit"].items():
            if below is None or hit[1] * 100 < below:
                set_checked(hit, on)
                redraw_row(row)
        update_summary()

    def uncheck_below():
        try:
            limit = float(below_var.get().strip().replace("%", ""))
        except ValueError:
            messagebox.showwarning(APP_NAME, "Type a percentage, like 80.")
            return
        bulk_set(False, below=limit)

    lst.bind("<Button-1>", on_click)
    lst.bind("<<TreeviewSelect>>", on_select)
    lst.bind("<space>", lambda e: (toggle(lst.selection()), "break")[1])
    check_all_btn.configure(command=lambda: bulk_set(True))
    uncheck_all_btn.configure(command=lambda: bulk_set(False))
    below_btn.configure(command=uncheck_below)
    scale.configure(command=lambda _v: refresh())
    before_entry.bind("<KeyRelease>", lambda e: update_summary())
    after_entry.bind("<KeyRelease>", lambda e: update_summary())

    # --- background work -------------------------------------------------------------
    def poll():
        try:
            while True:
                kind, payload = state["q"].get_nowait()
                if kind == "progress":
                    bar["value"] = payload * 100
                    status_var.set("Watching for the red X… %d%%" % (payload * 100))
                elif kind == "done":
                    state["analysis"] = payload
                    bar["value"] = 100
                    scan_btn.configure(text="Scan video")
                    state["worker"] = None
                    refresh()
                elif kind == "error":
                    bar["value"] = 0
                    scan_btn.configure(text="Scan video")
                    state["worker"] = None
                    if payload == "Cancelled.":
                        status_var.set("Scan cancelled.")
                    else:
                        status_var.set("Something went wrong. See the message box.")
                        messagebox.showerror(APP_NAME, payload)
                elif kind == "frame":
                    key, frame = payload
                    cache = state["frame_cache"]
                    cache[key] = frame
                    while len(cache) > 12:  # keep memory use down
                        cache.pop(next(iter(cache)))
                    if key == state["want"]:  # ignore frames you've already clicked past
                        state["frame"] = frame
                        show_frame()
                elif kind == "drop":
                    on_drop(payload)
                elif kind == "frame_error":
                    key, msg = payload
                    if key == state["want"]:
                        caption_var.set(msg)
        except queue.Empty:
            pass
        root.after(50, poll)

    def scan():
        if state["worker"]:  # button acts as Cancel while scanning
            state["cancel"].set()
            return
        video = video_var.get().strip()
        if not os.path.isfile(video):
            messagebox.showwarning(APP_NAME, "Please choose the gameplay video first.")
            return
        load_tracks()
        if state["analysis"] is None or state["analysis"].video_path != video:
            state["unchecked"] = []  # a different video: start with everything ticked
        state["analysis"] = None
        state["hits"], state["segments"] = [], []
        lst.delete(*lst.get_children())
        state["row_hit"] = {}
        save_btn.configure(state="disabled")
        bar["value"] = 0
        status_var.set("Reading the video…")
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

    def save():
        a = state["analysis"]
        update_summary()
        segs = state["segments"]
        if not a or not segs:
            return
        media_path = a.video_path  # the sequence links to the file you scanned
        info = a.info
        track = track_box.current() if info["audio_streams"] else None
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
        chosen = sum(1 for h in state["hits"] if not is_unchecked(h))
        messagebox.showinfo(APP_NAME, "Saved!\n\n%s\n\nVideo: %s\nAudio: %s\nKills: %d (%d clips)\n\n"
                                      "Premiere: File > Import, pick the .xml.\n"
                                      "Resolve: File > Import > Timeline, pick the .xml.\n"
                                      "It opens as a sequence named \"%s - kills\"."
                            % (path, os.path.basename(media_path),
                               "Track %d" % (track + 1) if track is not None else "none",
                               chosen, len(segs), base))

    scan_btn.configure(command=scan)
    save_btn.configure(command=save)
    try:
        # Dropped files go on the queue; poll() opens them a moment later, safely.
        if enable_file_drop(root, lambda paths: state["q"].put(("drop", paths))):
            status_var.set("Drag a gameplay video onto this window (or click Browse…), then click Scan.")
    except Exception:
        pass  # drag and drop is a nice extra; Browse… still works without it
    if start_video and os.path.isfile(start_video):  # e.g. a video dropped onto the launcher
        root.after(200, lambda: open_video(start_video))
    root.after(50, poll)
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
    if len(sys.argv) in (6, 7):
        run_cli(sys.argv[1:])
    else:
        try:
            run_gui(sys.argv[1] if len(sys.argv) == 2 else None)
        except Exception as e:
            import traceback
            _startup_error("Kill Clipper couldn't start:\n\n%s\n\n%s" % (e, traceback.format_exc()[-1200:]))
