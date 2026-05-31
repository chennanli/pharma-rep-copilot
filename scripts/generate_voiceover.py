"""
Generate the demo video's voiceover from a plain-text script using Edge-TTS.

Why Edge-TTS:
  - Free, no API key, runs locally via Microsoft Edge's free Azure neural voices.
  - Voice quality is genuinely good (Aria, Guy, Jenny — natural, professional).
  - One pip install, no GPU.

This script reads a Markdown-style scene file and emits one .mp3 per scene plus
a concatenated full-length .mp3 for the whole video.

Install Edge-TTS once:
    pip install edge-tts

Run:
    python scripts/generate_voiceover.py \
        --script docs/deliverables/voiceover-script.md \
        --out-dir data/voiceover \
        --voice en-US-GuyNeural

Script file format: top-level `## scene-<slug>` headings; everything until the next
`## ` heading is the narration text for that scene. Lines starting with `>` are
treated as stage directions (skipped). Blank lines and inline markdown are stripped.

To concatenate scene MP3s into one file you'll need ffmpeg:
    brew install ffmpeg
The script will run `ffmpeg -f concat -i list.txt -c copy voiceover_full.mp3` for you.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import shutil
import subprocess
import sys
from pathlib import Path


def parse_script(text: str) -> list[tuple[str, str]]:
    """Returns list of (slug, narration_text) tuples."""
    scenes = []
    current_slug = None
    current_lines: list[str] = []

    for raw in text.splitlines():
        line = raw.rstrip()
        m = re.match(r"^##\s+scene-([\w\-]+)", line)
        if m:
            if current_slug and current_lines:
                scenes.append((current_slug, _clean("\n".join(current_lines))))
            current_slug = m.group(1)
            current_lines = []
            continue
        if current_slug is None:
            continue
        if line.startswith(">"):
            continue  # stage directions
        current_lines.append(line)

    if current_slug and current_lines:
        scenes.append((current_slug, _clean("\n".join(current_lines))))

    return scenes


def _clean(text: str) -> str:
    # Strip markdown emphasis, links, headers
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Collapse whitespace
    text = re.sub(r"\n+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


async def synth_scene(text: str, voice: str, out_path: Path):
    import edge_tts  # imported lazily so the dep is optional
    communicate = edge_tts.Communicate(text=text, voice=voice, rate="+0%", volume="+0%")
    await communicate.save(str(out_path))


def concat_with_ffmpeg(parts: list[Path], out_path: Path):
    if not shutil.which("ffmpeg"):
        print("⚠ ffmpeg not found; skipping concatenation. Individual scene MP3s are still produced.")
        print("  brew install ffmpeg  # then re-run to concat.")
        return
    list_file = out_path.parent / "_concat_list.txt"
    list_file.write_text("\n".join(f"file '{p.resolve()}'" for p in parts))
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
         "-c", "copy", str(out_path)],
        check=True,
    )
    list_file.unlink()
    print(f"✓ Concatenated full voiceover: {out_path}")


async def main_async(script_path: Path, out_dir: Path, voice: str):
    text = script_path.read_text()
    scenes = parse_script(text)
    if not scenes:
        print(f"✗ No scenes parsed from {script_path}. Each scene needs a heading like '## scene-intro'.")
        sys.exit(1)

    out_dir.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    for i, (slug, narration) in enumerate(scenes, 1):
        outp = out_dir / f"voiceover_{i:02d}_{slug}.mp3"
        print(f"  ▶ scene {i}: {slug}  ({len(narration)} chars)")
        await synth_scene(narration, voice, outp)
        parts.append(outp)

    full = out_dir / "voiceover_full.mp3"
    concat_with_ffmpeg(parts, full)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--script", default="docs/deliverables/voiceover-script.md")
    p.add_argument("--out-dir", default="data/voiceover")
    p.add_argument("--voice", default="en-US-GuyNeural",
                   help="Other good choices: en-US-AriaNeural, en-US-JennyNeural, "
                        "en-US-AndrewNeural, en-US-EmmaNeural")
    args = p.parse_args()

    try:
        import edge_tts  # noqa: F401
    except ImportError:
        print("✗ edge-tts not installed. Run:  pip install edge-tts")
        sys.exit(2)

    asyncio.run(main_async(Path(args.script), Path(args.out_dir), args.voice))


if __name__ == "__main__":
    main()
