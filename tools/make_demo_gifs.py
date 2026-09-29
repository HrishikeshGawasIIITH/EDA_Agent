"""
make_demo_gifs.py — Render the README demo animations.

Two generators, both driven by real captured material:

  terminal_gif()   Animates a real agent transcript (captured verbatim from
                   `python -m eda_agent.cli`) as a scrolling terminal.
  frames_gif()     Animates a sequence of Virtuoso screenshots captured at each
                   step of a schematic build.

Colour emoji have no glyph in the available monospace fonts, so the terminal
renderer substitutes monochrome equivalents (✅ → ✔, 📋 → ▸, …). Only the glyph
changes; the transcript text is otherwise byte-for-byte what the agent printed.

Usage:
    python tools/make_demo_gifs.py --terminal <transcript.txt> --out docs/media/x.gif
    python tools/make_demo_gifs.py --frames "shots/*.png" --out docs/media/y.gif
"""

import argparse
import glob as globmod
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

# Emoji the agent prints -> monochrome glyphs the mono font actually has.
GLYPH_FALLBACK = {
    "🤖": "»", "📋": "▸", "⚙️": "⚙", "⚙": "⚙", "✅": "✔", "❌": "✗",
    "📊": "≡", "🔄": "↻", "📚": "≣", "📐": "◹", "🛑": "■", "💬": "▪",
    "🔍": "◎", "⚠️": "!", "⚠": "!", "✨": "*", "🎯": "◉",
}

BG = (13, 17, 23)
FG = (201, 209, 217)
DIM = (110, 118, 129)
GREEN = (63, 185, 80)
CYAN = (86, 182, 194)
YELLOW = (210, 168, 58)
RED = (248, 81, 73)
MAGENTA = (188, 140, 255)


def _subst(text: str) -> str:
    for k, val in GLYPH_FALLBACK.items():
        text = text.replace(k, val)
    return text


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


def _colour_for(line: str):
    """Pick a colour for a transcript line from its leading marker."""
    s = line.strip()
    if s.startswith(">"):
        return GREEN
    if s.startswith("▸") or s.startswith("Plan:"):
        return CYAN
    if s.startswith("✔"):
        return GREEN
    if s.startswith("✗") or "Error" in s or "Traceback" in s:
        return RED
    if s.startswith("↻") or s.startswith("⚙"):
        return YELLOW
    if s.startswith("≡") or s.startswith("≣") or s.startswith("◹"):
        return MAGENTA
    if s.startswith("=") or s.startswith("-"):
        return DIM
    return FG


def _wrap(line: str, cols: int) -> list:
    """Hard-wrap a line to the terminal width, preserving indentation."""
    if len(line) <= cols:
        return [line]
    indent = len(line) - len(line.lstrip())
    pad = " " * min(indent + 2, cols - 10)
    out, cur = [], line
    first = True
    while len(cur) > cols:
        cut = cur.rfind(" ", 0, cols)
        if cut <= indent:
            cut = cols
        out.append(cur[:cut])
        cur = (pad if not first else pad) + cur[cut:].lstrip()
        first = False
    out.append(cur)
    return out


def _render_screen(lines, cols, rows, font, cw, ch, pad, title=None):
    """Draw one terminal screen (already wrapped and trimmed) to an image."""
    head = 30 if title else 0
    w = cols * cw + pad * 2
    h = rows * ch + pad * 2 + head
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)

    if title:
        d.rectangle([0, 0, w, head], fill=(22, 27, 34))
        for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
            d.ellipse([pad + i * 18, 10, pad + i * 18 + 10, 20], fill=c)
        d.text((pad + 70, 8), title, font=font, fill=DIM)

    y = pad + head
    for ln in lines[-rows:]:
        d.text((pad, y), ln, font=font, fill=_colour_for(ln))
        y += ch
    return img


def _terminal_frames(transcript: str, cols=98, rows=30, font_size=15,
                     title="EDA Agent", hold_ms=1400):
    """Build the frame/duration lists for a scrolling terminal animation."""
    font = ImageFont.truetype(FONT_REG, font_size)
    bbox = font.getbbox("M")
    cw = int(font.getlength("M"))
    ch = int((bbox[3] - bbox[1]) * 1.9)
    pad = 14

    text = _subst(_strip_ansi(transcript))
    raw_lines = [ln.rstrip() for ln in text.splitlines()]

    wrapped = []
    for ln in raw_lines:
        wrapped.extend(_wrap(ln, cols))

    # Don't leave a slab of dead space under a short transcript.
    rows = min(rows, len(wrapped) + 1)

    frames, durations = [], []
    shown = []
    for ln in wrapped:
        if ln.strip().startswith(">") and len(ln.strip()) > 2:
            # Type the user's prompt out character by character.
            prefix = ln[:ln.index(">") + 2]
            body = ln[len(prefix):]
            step = max(1, len(body) // 18)
            for i in range(0, len(body) + 1, step):
                frames.append(_render_screen(shown + [prefix + body[:i] + "\u2588"],
                                             cols, rows, font, cw, ch, pad, title))
                durations.append(45)
            shown.append(ln)
            frames.append(_render_screen(shown, cols, rows, font, cw, ch, pad, title))
            durations.append(380)
        else:
            shown.append(ln)
            frames.append(_render_screen(shown, cols, rows, font, cw, ch, pad, title))
            durations.append(90 if not ln.strip() else 200)

    if durations:
        durations[-1] = hold_ms
    return frames, durations


def _shared_palette(frames, colors: int):
    """Derive one palette from samples spanning the whole animation.

    Taking the palette from frame 0 alone is wrong for a combined GIF: the
    terminal half is grey-on-navy, so the schematic half's bright red/green
    traces get mapped onto whatever muted colours happen to be in frame 0.
    Tile a handful of evenly spaced frames and quantise that instead.
    """
    n = len(frames)
    idx = sorted({0, n // 4, n // 2, (3 * n) // 4, n - 1} & set(range(n)))
    picks = [frames[i] for i in idx]
    tw = min(f.width for f in picks) // 2 or 1
    th = min(f.height for f in picks) // 2 or 1
    montage = Image.new("RGB", (tw * len(picks), th))
    for i, f in enumerate(picks):
        montage.paste(f.resize((tw, th), Image.NEAREST), (i * tw, 0))
    return montage.convert("P", palette=Image.ADAPTIVE, colors=colors)


def _save_gif(frames, durations, out_path: Path, colors=32):
    """Quantise to a shared palette (big size win) and write the GIF."""
    if not frames:
        return out_path, 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pal = _shared_palette(frames, colors)
    q = [f.quantize(palette=pal, dither=Image.NONE) for f in frames]
    # disposal=2 (restore to background) — without it, a frame that doesn't
    # cover the full canvas lets the previous frame ghost through underneath.
    q[0].save(out_path, save_all=True, append_images=q[1:],
              duration=durations, loop=0, optimize=True, disposal=2)
    return out_path, len(q)


def terminal_gif(transcript: str, out_path: Path, cols=98, rows=30,
                 font_size=15, title="EDA Agent", hold_ms=1400):
    """Animate a captured transcript as a scrolling terminal session."""
    frames, durations = _terminal_frames(transcript, cols, rows, font_size,
                                         title, hold_ms)
    return _save_gif(frames, durations, out_path, colors=16)


def _screenshot_frames(paths, ms=1100, last_ms=2200, max_width=1100,
                       captions=None, scales=None):
    """Build frame/duration lists from a sequence of screenshots.

    `scales` maps a 1-based frame number to a factor < 1, which shrinks that
    capture inside its frame — i.e. shows it "zoomed out" with more empty space
    around it, without re-capturing from Virtuoso.
    """
    font = ImageFont.truetype(FONT_BOLD, 20)
    scales = scales or {}
    imgs = []
    for i, p in enumerate(paths):
        im = Image.open(p).convert("RGB")
        if im.width > max_width:
            im = im.resize((max_width, int(im.height * max_width / im.width)),
                           Image.LANCZOS)
        factor = scales.get(i + 1)
        if factor and 0 < factor < 1:
            small = im.resize((max(1, int(im.width * factor)),
                               max(1, int(im.height * factor))), Image.LANCZOS)
            canvas = Image.new("RGB", im.size, (0, 0, 0))
            canvas.paste(small, ((im.width - small.width) // 2,
                                 (im.height - small.height) // 2))
            im = canvas
        if captions and i < len(captions) and captions[i]:
            bar = 38
            canvas = Image.new("RGB", (im.width, im.height + bar), (22, 27, 34))
            canvas.paste(im, (0, bar))
            d = ImageDraw.Draw(canvas)
            d.text((12, 9), f"{i + 1}/{len(paths)}  {captions[i]}",
                   font=font, fill=(201, 209, 217))
            im = canvas
        imgs.append(im)

    if not imgs:
        return [], []
    durations = [ms] * len(imgs)
    durations[-1] = last_ms
    return imgs, durations


def _unify(frames, bg=BG):
    """Pad every frame onto one common canvas so the GIF doesn't jump."""
    if not frames:
        return frames
    w = max(f.width for f in frames)
    h = max(f.height for f in frames)
    out = []
    for f in frames:
        if f.size == (w, h):
            out.append(f)
            continue
        c = Image.new("RGB", (w, h), bg)
        c.paste(f, ((w - f.width) // 2, (h - f.height) // 2))
        out.append(c)
    return out


def frames_gif(paths, out_path: Path, ms=1100, last_ms=2200, max_width=1100,
               captions=None):
    """Animate a sequence of screenshots, optionally captioned."""
    frames, durations = _screenshot_frames(paths, ms, last_ms, max_width, captions)
    return _save_gif(_unify(frames, (22, 27, 34)), durations, out_path, colors=64)


def combined_gif(transcript: str, paths, out_path: Path, captions=None,
                 cols=98, rows=30, title="EDA Agent", cut_at: str = "",
                 max_width=1100):
    """One animation: the agent session, then the schematic it produced.

    `cut_at` truncates the transcript at the first line starting with that
    string, so the terminal half can stop at the task the screenshots show.
    """
    if cut_at:
        lines = transcript.splitlines()
        for i, ln in enumerate(lines):
            if ln.startswith(cut_at):
                lines = lines[:i]
                break
        transcript = "\n".join(lines).rstrip() + "\n"

    tf, td = _terminal_frames(transcript, cols=cols, rows=rows, title=title,
                              hold_ms=2000)
    sf, sd = _screenshot_frames(paths, captions=captions, max_width=max_width)

    frames = _unify(tf + sf)
    durations = td + sd
    return _save_gif(frames, durations, out_path, colors=64)


def prompt_then_frames(prompt: str, paths, out_path: Path, captions=None,
                       header=(), cols=92, font_size=17,
                       title="EDA Agent", max_width=1100, scales=None):
    """Type one prompt, hold it highlighted, then play the schematic frames.

    The deliberately plain version of the demo: no scrollback, no retrieval
    dump — just the ask, a beat to read it, then the result appearing.

    The screenshot frames are rendered first so the terminal can be drawn on
    exactly the same canvas; otherwise the GIF visibly jumps at the cut.
    """
    sf, sd = _screenshot_frames(paths, captions=captions, max_width=max_width,
                                scales=scales)
    w = max([f.width for f in sf], default=max_width)
    h = max([f.height for f in sf], default=700)

    font = ImageFont.truetype(FONT_REG, font_size)
    bold = ImageFont.truetype(FONT_BOLD, font_size)
    head_font = ImageFont.truetype(FONT_REG, max(13, int(font_size * 0.8)))
    bbox = font.getbbox("M")
    ch = int((bbox[3] - bbox[1]) * 1.95)
    pad = 34
    head_h = max(34, int(font_size * 2.0))

    head = [_subst(x) for x in header]
    # keep the text inside the highlight band, which stops at w - pad
    cols = max(20, (w - pad * 2 - 28) // max(1, int(font.getlength("M"))))

    def screen(typed: int | None, glow: float = 0.0):
        img = Image.new("RGB", (w, h), BG)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, w, head_h], fill=(22, 27, 34))
        for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
            d.ellipse([pad + i * 18, 12, pad + i * 18 + 10, 22], fill=c)
        d.text((pad + 74, 9), title, font=head_font, fill=DIM)

        shown = prompt if typed is None else prompt[:typed]
        lines = _wrap("> " + shown, cols)
        block = (len(head) + 1 + len(lines)) * ch
        y = head_h + max(pad, (h - head_h - block) // 3)

        for line in head:
            d.text((pad, y), line, font=font, fill=_colour_for(line))
            y += ch
        y += ch

        if glow > 0:
            top, bot = y - 8, y + len(lines) * ch + 4
            tint = tuple(int(BG[i] + (GREEN[i] - BG[i]) * 0.20 * glow)
                         for i in range(3))
            d.rectangle([pad - 14, top, w - pad, bot], fill=tint)
            d.rectangle([pad - 14, top, pad - 11, bot], fill=GREEN)

        for i, line in enumerate(lines):
            d.text((pad, y + i * ch), line, font=(bold if glow > 0 else font),
                   fill=GREEN)
        if typed is not None:
            last = lines[-1] if lines else "> "
            d.text((pad + int(font.getlength(last)), y + (len(lines) - 1) * ch),
                   "\u2588", font=font, fill=GREEN)
        return img

    frames, durations = [], []
    frames.append(screen(0)); durations.append(700)
    step = max(1, len(prompt) // 26)
    for i in range(0, len(prompt) + 1, step):
        frames.append(screen(i)); durations.append(55)
    frames.append(screen(None)); durations.append(500)

    # hold on the prompt, pulsing, so it can actually be read
    for glow in (0.35, 0.7, 1.0, 1.0, 1.0, 0.7, 0.35):
        frames.append(screen(None, glow)); durations.append(230)
    frames.append(screen(None)); durations.append(900)

    return _save_gif(_unify(frames + sf), durations + sd, out_path,
                     colors=64)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--terminal", help="path to a captured transcript .txt")
    ap.add_argument("--frames", help="glob of screenshot frames, in order")
    ap.add_argument("--captions", help="'|'-separated captions for --frames")
    ap.add_argument("--title", default="EDA Agent")
    ap.add_argument("--cols", type=int, default=98)
    ap.add_argument("--rows", type=int, default=30)
    ap.add_argument("--prompt",
                    help="simple mode: type this prompt, then play --frames")
    ap.add_argument("--font-size", type=int, default=17,
                    help="terminal font size for --prompt mode")
    ap.add_argument("--scale", action="append", default=[],
                    help="N=FACTOR — shrink screenshot frame N (1-based), e.g. 6=0.7")
    ap.add_argument("--header", default="",
                    help="'|'-separated context lines shown above the prompt")
    ap.add_argument("--combine", action="store_true",
                    help="terminal session followed by the screenshot frames")
    ap.add_argument("--cut-at", default="",
                    help="truncate the transcript at the first line starting with this")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    out = Path(args.out)
    caps = args.captions.split("|") if args.captions else None
    if args.prompt:
        paths = sorted(globmod.glob(args.frames))
        head = tuple(args.header.split("|")) if args.header else ()
        scales = {}
        for spec in args.scale:
            k, _, v = spec.partition("=")
            scales[int(k)] = float(v)
        p, n = prompt_then_frames(args.prompt, paths, out, captions=caps,
                                  header=head, title=args.title,
                                  font_size=args.font_size, scales=scales)
    elif args.combine:
        text = Path(args.terminal).read_text(encoding="utf-8", errors="replace")
        paths = sorted(globmod.glob(args.frames))
        p, n = combined_gif(text, paths, out, captions=caps, cols=args.cols,
                            rows=args.rows, title=args.title, cut_at=args.cut_at)
    elif args.terminal:
        text = Path(args.terminal).read_text(encoding="utf-8", errors="replace")
        p, n = terminal_gif(text, out, cols=args.cols, rows=args.rows,
                            title=args.title)
    elif args.frames:
        paths = sorted(globmod.glob(args.frames))
        p, n = frames_gif(paths, out, captions=caps)
    else:
        ap.error("need --terminal or --frames")

    print(f"wrote {p} ({n} frames, {p.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
