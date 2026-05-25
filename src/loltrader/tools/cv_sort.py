"""Keyboard-driven frame sorter.

Browse the candidate frames in a window. One keypress per image:
    1 = in_game
    2 = studio
    3 = replay
    4 = ads
    s = skip (leave in source, move on)
    d = delete (move to trash dir, can be undone)
    u = undo last action (one level)
    n = next without classifying
    p = previous (re-classify earlier)
    q / Esc = quit

Usage:
    python -m loltrader.tools.cv_sort \\
        --source data/cv_capture_session_1 \\
        --refs   data/cv_references

Spec §6.2 (CV classifier reference frames).
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import cv2

CLASSES = ["in_game", "studio", "replay", "ads"]
KEY_TO_CLASS = {
    ord("1"): "in_game",
    ord("2"): "studio",
    ord("3"): "replay",
    ord("4"): "ads",
}

WINDOW_NAME = "Frame sorter — press 1=in_game 2=studio 3=replay 4=ads s=skip d=delete u=undo q=quit"
MAX_DISPLAY_W = 1280
MAX_DISPLAY_H = 720


def _resize_to_fit(img):
    h, w = img.shape[:2]
    scale = min(MAX_DISPLAY_W / w, MAX_DISPLAY_H / h, 1.0)
    if scale < 1.0:
        new_w, new_h = int(w * scale), int(h * scale)
        return cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
    return img


def _overlay_text(img, lines):
    """Draw a translucent black panel + white text in the top-left."""
    out = img.copy()
    pad = 10
    line_h = 28
    panel_h = pad * 2 + line_h * len(lines)
    panel_w = max(cv2.getTextSize(ln, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)[0][0]
                  for ln in lines) + pad * 2
    overlay = out.copy()
    cv2.rectangle(overlay, (0, 0), (panel_w, panel_h), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.5, out, 0.5, 0, out)
    for i, ln in enumerate(lines):
        y = pad + line_h * (i + 1) - 8
        cv2.putText(out, ln, (pad, y), cv2.FONT_HERSHEY_SIMPLEX,
                    0.7, (255, 255, 255), 2, cv2.LINE_AA)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", required=True,
                   help="Directory containing candidate PNGs to sort")
    p.add_argument("--refs", required=True,
                   help="Reference root (will write into {refs}/{class}/)")
    args = p.parse_args(argv)

    source = Path(args.source)
    refs_root = Path(args.refs)
    trash = source.parent / (source.name + "_trash")

    if not source.exists():
        print(f"ERROR: source not found: {source}", file=sys.stderr)
        return 1
    for cls in CLASSES:
        (refs_root / cls).mkdir(parents=True, exist_ok=True)
    trash.mkdir(parents=True, exist_ok=True)

    pngs = sorted(source.glob("*.png"))
    if not pngs:
        print(f"No PNGs in {source}.")
        return 0

    print(f"{len(pngs)} candidates in {source}")
    print(f"Refs root: {refs_root}")
    print(f"Trash:     {trash}")
    print()
    print("Keys: 1=in_game  2=studio  3=replay  4=ads  s=skip")
    print("      d=delete   u=undo    n=next    p=prev  q=quit")
    print()

    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WINDOW_NAME, MAX_DISPLAY_W, MAX_DISPLAY_H)

    idx = 0
    history: list[tuple[Path, Path]] = []  # (original_path, moved_to_path)
    counts: dict[str, int] = {c: 0 for c in CLASSES}
    counts["trash"] = 0
    counts["skip"] = 0

    while True:
        # Find next existing PNG starting at idx
        while idx < len(pngs) and not pngs[idx].exists():
            idx += 1
        if idx >= len(pngs):
            print()
            print("All frames processed.")
            break

        path = pngs[idx]
        img = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if img is None:
            print(f"  could not read {path.name}; skipping")
            idx += 1
            continue

        display = _resize_to_fit(img)
        lines = [
            f"{idx + 1}/{len(pngs)}   {path.name}",
            "  ".join(f"{c}={counts[c]}" for c in CLASSES),
            f"skip={counts['skip']}  trash={counts['trash']}",
        ]
        cv2.imshow(WINDOW_NAME, _overlay_text(display, lines))

        key = cv2.waitKey(0) & 0xFF

        if key in KEY_TO_CLASS:
            cls = KEY_TO_CLASS[key]
            dest = refs_root / cls / path.name
            shutil.move(str(path), str(dest))
            history.append((path, dest))
            counts[cls] += 1
            idx += 1
        elif key == ord("d"):
            dest = trash / path.name
            shutil.move(str(path), str(dest))
            history.append((path, dest))
            counts["trash"] += 1
            idx += 1
        elif key == ord("s") or key == ord("n"):
            counts["skip"] += 1
            idx += 1
        elif key == ord("p"):
            idx = max(0, idx - 1)
        elif key == ord("u"):
            if history:
                orig, moved = history.pop()
                if moved.exists():
                    shutil.move(str(moved), str(orig))
                # Adjust counts
                for cls in (*CLASSES, "trash"):
                    cls_dir = refs_root / cls if cls != "trash" else trash
                    if str(moved).startswith(str(cls_dir)):
                        counts[cls] -= 1
                        break
                # Roll the index back so we re-show the undone frame
                idx = pngs.index(orig)
            else:
                print("  nothing to undo")
        elif key in (ord("q"), 27):  # q or Esc
            break

    cv2.destroyAllWindows()
    print()
    print("Final counts:")
    for c in CLASSES:
        print(f"  {c}: {counts[c]}")
    print(f"  trash: {counts['trash']}")
    print(f"  skipped (left in source): {counts['skip']}")
    print()
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
