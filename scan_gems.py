"""
Rise of Kingdoms – gem node scanner (Windows)

Captures the game/emulator window and finds gem nodes via OpenCV template matching.
You must supply screenshot crops of gem nodes in ./templates/ (see README).

Usage:
  python scan_gems.py
  python scan_gems.py --window "Rise of Kingdoms"
  python scan_gems.py --window "Rise of Kingdoms" --sweep-cols 3 --sweep-rows 2 --save-debug found.png
  python scan_gems.py --list-windows
  python scan_gems.py --save-debug debug.png
"""

from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import mss
import numpy as np

try:
    import win32gui
except ImportError:
    print("Install dependencies: pip install -r requirements.txt", file=sys.stderr)
    raise

try:
    import pyautogui
except ImportError:
    pyautogui = None

SCRIPT_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = SCRIPT_DIR / "templates"
NEGATIVE_TEMPLATES_DIR = TEMPLATES_DIR / "negatives"
DEFAULT_THRESHOLD = 0.72
DEFAULT_MIN_DISTANCE = 28
DEFAULT_PAN_PIXELS = 620
DEFAULT_LARGE_TEMPLATE_SCALE = 0.72
DEFAULT_ROBUST_CONFIRM_FRAMES = 2
DEFAULT_ROBUST_CONFIRM_DELAY = 0.25
DEFAULT_ROBUST_RED_RATIO = 0.045
DEFAULT_ROBUST_EDGE_DENSITY = 0.045
DEFAULT_ROBUST_GREEN_MAX = 0.22
STOP_KEY_VK_MAP = {
    "ESC": 0x1B,
    "F6": 0x75,
    "F7": 0x76,
    "F8": 0x77,
    "F9": 0x78,
    "F10": 0x79,
}
LARGE_TEMPLATE_SCALES = (0.30, 0.35, 0.40, 0.45, 0.50, 0.60, 0.70, 0.80, 0.90, 1.00)
MEDIUM_TEMPLATE_SCALES = (0.60, 0.70, 0.80, 0.90, 1.00)


def safe_console_text(text: str) -> str:
    encoding = sys.stdout.encoding or "utf-8"
    return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


def resolve_stop_key(stop_key: str) -> tuple[str, int]:
    key = stop_key.strip().upper()
    if key not in STOP_KEY_VK_MAP:
        supported = ", ".join(sorted(STOP_KEY_VK_MAP))
        raise SystemExit(f"Unsupported --stop-key '{stop_key}'. Use one of: {supported}")
    return key, STOP_KEY_VK_MAP[key]


def is_stop_key_pressed(vk_code: int) -> bool:
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk_code) & 0x8000)


def scales_for_template(template: np.ndarray, large_template_scale: float) -> tuple[float, ...]:
    side = max(template.shape[0], template.shape[1])
    if side >= 120 and large_template_scale > 0:
        return (large_template_scale,)
    if side >= 180:
        return LARGE_TEMPLATE_SCALES
    if side >= 120:
        return MEDIUM_TEMPLATE_SCALES
    return (1.0,)


@dataclass
class GemMatch:
    x: int
    y: int
    confidence: float
    template: str


def list_visible_windows() -> list[tuple[str, int]]:
    results: list[tuple[str, int]] = []

    def callback(hwnd: int, _: object) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd).strip()
        if title:
            results.append((title, hwnd))
        return True

    win32gui.EnumWindows(callback, None)
    return sorted(results, key=lambda t: t[0].lower())


def find_window_handle(title_substring: str) -> int:
    needle = title_substring.lower()
    matches = [
        (title, hwnd)
        for title, hwnd in list_visible_windows()
        if needle in title.lower()
    ]
    if not matches:
        raise SystemExit(
            f'No window title containing "{title_substring}". '
            "Run with --list-windows to see options."
        )
    if len(matches) > 1:
        print("Multiple matches; using the first:")
        for title, _ in matches[:5]:
            print(f"  - {safe_console_text(title)}")
    return matches[0][1]


def get_window_rect(hwnd: int) -> dict[str, int]:
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    # Client area avoids title bar when possible
    try:
        cl, ct, cr, cb = win32gui.GetClientRect(hwnd)
        origin = win32gui.ClientToScreen(hwnd, (0, 0))
        width = cr - cl
        height = cb - ct
        if width > 100 and height > 100:
            return {
                "left": origin[0],
                "top": origin[1],
                "width": width,
                "height": height,
            }
    except Exception:
        pass
    return {
        "left": left,
        "top": top,
        "width": right - left,
        "height": bottom - top,
    }


def capture_region(region: dict[str, int]) -> np.ndarray:
    with mss.MSS() as sct:
        shot = sct.grab(region)
        frame = np.array(shot)[:, :, :3]  # BGRA -> drop alpha
        return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)


def focus_window(hwnd: int) -> None:
    try:
        win32gui.ShowWindow(hwnd, 5)  # SW_SHOW
        win32gui.SetForegroundWindow(hwnd)
    except Exception as exc:
        raise SystemExit(f"Could not focus game window: {exc}") from exc
    time.sleep(0.2)


def pan_camera(
    region: dict[str, int],
    camera_dx: int,
    camera_dy: int,
    duration: float,
    settle: float,
    stop_vk_code: int,
) -> None:
    if pyautogui is None:
        raise SystemExit(
            "Sweep mode needs pyautogui. Install it with: python -m pip install pyautogui"
        )
    anchor_x = region["left"] + region["width"] // 2
    anchor_y = region["top"] + int(region["height"] * 0.55)
    # To move the camera right/down, drag the map left/up.
    drag_x = anchor_x - camera_dx
    drag_y = anchor_y - camera_dy
    steps = max(1, int(max(abs(camera_dx), abs(camera_dy)) / 120))
    step_duration = max(0.0, duration / steps) if steps else 0.0
    pyautogui.moveTo(anchor_x, anchor_y)
    pyautogui.mouseDown(button="left")
    try:
        for step in range(1, steps + 1):
            if is_stop_key_pressed(stop_vk_code):
                raise KeyboardInterrupt
            next_x = anchor_x + int((drag_x - anchor_x) * step / steps)
            next_y = anchor_y + int((drag_y - anchor_y) * step / steps)
            pyautogui.moveTo(next_x, next_y, duration=step_duration)
    finally:
        pyautogui.mouseUp(button="left")
    if settle > 0:
        end = time.time() + settle
        while time.time() < end:
            if is_stop_key_pressed(stop_vk_code):
                raise KeyboardInterrupt
            time.sleep(0.03)


def sweep_positions(rows: int, cols: int) -> list[tuple[int, int]]:
    order: list[tuple[int, int]] = []
    for row in range(rows):
        if row % 2 == 0:
            col_range = range(cols)
        else:
            col_range = range(cols - 1, -1, -1)
        for col in col_range:
            order.append((row, col))
    return order


def debug_path_for_tile(base_path: str, row: int, col: int) -> str:
    path = Path(base_path)
    suffix = path.suffix or ".png"
    out_dir = path.parent / f"{path.stem}_tiles"
    out_dir.mkdir(parents=True, exist_ok=True)
    return str(out_dir / f"r{row + 1}c{col + 1}{suffix}")


def load_templates(
    templates_dir: Path,
    required: bool = True,
    max_side: int | None = None,
) -> list[tuple[str, np.ndarray]]:
    paths = sorted(templates_dir.glob("*.png")) + sorted(templates_dir.glob("*.jpg"))
    if not paths:
        if not required:
            return []
        raise SystemExit(
            f"No templates in {templates_dir}.\n"
            "Crop 40–80px images of gem nodes from a screenshot and save as templates/gem1.png etc."
        )
    templates: list[tuple[str, np.ndarray]] = []
    for path in paths:
        img = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if img is None:
            print(f"Warning: could not read {path.name}", file=sys.stderr)
            continue
        if max_side is not None and max(img.shape[0], img.shape[1]) > max_side:
            print(
                f"Skipping {path.name}: template is too large ({img.shape[1]}x{img.shape[0]}). "
                f"Use tight crops around the node icon (<= {max_side}px).",
                file=sys.stderr,
            )
            continue
        templates.append((path.name, img))
    if not templates:
        raise SystemExit("No valid template images loaded.")
    return templates


def non_max_suppression(
    points: list[tuple[int, int, float, str]],
    min_distance: float,
) -> list[GemMatch]:
    if not points:
        return []
    points = sorted(points, key=lambda p: p[2], reverse=True)
    kept: list[GemMatch] = []
    for x, y, conf, name in points:
        if all((x - k.x) ** 2 + (y - k.y) ** 2 >= min_distance**2 for k in kept):
            kept.append(GemMatch(x=x, y=y, confidence=conf, template=name))
    return kept


def match_templates(
    frame: np.ndarray,
    templates: list[tuple[str, np.ndarray]],
    negative_templates: list[tuple[str, np.ndarray]],
    threshold: float,
    min_distance: float,
    negative_margin: float,
    large_template_scale: float,
) -> list[GemMatch]:
    match_frame = frame
    all_points: list[tuple[int, int, float, str]] = []

    negative_prepared = [(name, tpl) for name, tpl in negative_templates]

    def max_negative_score(cx: int, cy: int) -> float:
        best = -1.0
        for _, neg in negative_prepared:
            nh, nw = neg.shape[:2]
            left = cx - nw // 2
            top = cy - nh // 2
            right = left + nw
            bottom = top + nh
            if left < 0 or top < 0 or right > match_frame.shape[1] or bottom > match_frame.shape[0]:
                continue
            roi = match_frame[top:bottom, left:right]
            score = float(cv2.matchTemplate(roi, neg, cv2.TM_CCOEFF_NORMED)[0, 0])
            if score > best:
                best = score
        return best

    for name, template in templates:
        if template.shape[0] > frame.shape[0] or template.shape[1] > frame.shape[1]:
            print(f"Skipping {name}: template larger than capture area", file=sys.stderr)
            continue
        for scale in scales_for_template(template, large_template_scale):
            if scale == 1.0:
                scaled = template
            else:
                sw = max(1, int(template.shape[1] * scale))
                sh = max(1, int(template.shape[0] * scale))
                if sw < 16 or sh < 16:
                    continue
                interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC
                scaled = cv2.resize(template, (sw, sh), interpolation=interp)
            if scaled.shape[0] > frame.shape[0] or scaled.shape[1] > frame.shape[1]:
                continue
            result = cv2.matchTemplate(match_frame, scaled, cv2.TM_CCOEFF_NORMED)
            loc = np.where(result >= threshold)
            h, w = scaled.shape[:2]
            for py, px in zip(*loc):
                cx, cy = px + w // 2, py + h // 2
                conf = float(result[py, px])
                if negative_prepared:
                    neg_conf = max_negative_score(cx, cy)
                    if neg_conf >= conf - negative_margin:
                        continue
                all_points.append((cx, cy, conf, name))

    return non_max_suppression(all_points, min_distance)


def draw_matches(frame: np.ndarray, matches: list[GemMatch]) -> np.ndarray:
    out = frame.copy()
    for m in matches:
        cv2.circle(out, (m.x, m.y), 14, (0, 255, 0), 2)
        label = f"{m.confidence:.2f}"
        cv2.putText(
            out,
            label,
            (m.x - 20, m.y - 18),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            (0, 255, 0),
            1,
            cv2.LINE_AA,
        )
    return out


def filter_by_visual_robustness(
    frame: np.ndarray,
    matches: list[GemMatch],
    min_red_ratio: float,
    min_edge_density: float,
    max_green_ratio: float,
) -> list[GemMatch]:
    if not matches:
        return []
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    kept: list[GemMatch] = []
    for m in matches:
        radius = 22
        x1 = max(0, m.x - radius)
        y1 = max(0, m.y - radius)
        x2 = min(frame.shape[1], m.x + radius)
        y2 = min(frame.shape[0], m.y + radius)
        if x2 - x1 < 20 or y2 - y1 < 20:
            continue
        roi_hsv = hsv[y1:y2, x1:x2]
        roi_gray = gray[y1:y2, x1:x2]
        red1 = cv2.inRange(roi_hsv, (0, 90, 60), (12, 255, 255))
        red2 = cv2.inRange(roi_hsv, (165, 90, 60), (179, 255, 255))
        red_mask = cv2.bitwise_or(red1, red2)
        green_mask = cv2.inRange(roi_hsv, (35, 60, 45), (90, 255, 255))
        edges = cv2.Canny(roi_gray, 70, 170)
        red_ratio = float(np.count_nonzero(red_mask)) / float(red_mask.size)
        green_ratio = float(np.count_nonzero(green_mask)) / float(green_mask.size)
        edge_density = float(np.count_nonzero(edges)) / float(edges.size)
        if (
            red_ratio >= min_red_ratio
            and edge_density >= min_edge_density
            and green_ratio <= max_green_ratio
        ):
            kept.append(m)
    return kept


def temporal_confirm_matches(
    per_frame_matches: list[list[GemMatch]],
    min_distance: float,
) -> list[GemMatch]:
    if not per_frame_matches:
        return []
    if len(per_frame_matches) == 1:
        return per_frame_matches[0]
    confirmed: list[GemMatch] = []
    first = per_frame_matches[0]
    others = per_frame_matches[1:]
    for candidate in first:
        is_present = True
        for frame_matches in others:
            if not any(
                (candidate.x - m.x) ** 2 + (candidate.y - m.y) ** 2 < min_distance**2
                for m in frame_matches
            ):
                is_present = False
                break
        if is_present:
            confirmed.append(candidate)
    return confirmed


def scan_once(
    hwnd: int,
    templates: list[tuple[str, np.ndarray]],
    negative_templates: list[tuple[str, np.ndarray]],
    threshold: float,
    min_distance: float,
    negative_margin: float,
    large_template_scale: float,
) -> tuple[np.ndarray, list[GemMatch], dict[str, int]]:
    region = get_window_rect(hwnd)
    frame = capture_region(region)
    matches = match_templates(
        frame,
        templates,
        negative_templates,
        threshold,
        min_distance,
        negative_margin,
        large_template_scale,
    )
    return frame, matches, region


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan ROK map for gem nodes (template matching)")
    parser.add_argument(
        "--window",
        default="",
        help='Substring of game window title (e.g. "MASS", "Gamota", "Rise of Kingdoms")',
    )
    parser.add_argument("--list-windows", action="store_true", help="Print visible window titles")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="Match confidence 0–1")
    parser.add_argument("--min-distance", type=int, default=DEFAULT_MIN_DISTANCE, help="NMS pixel radius")
    parser.add_argument(
        "--max-template-side",
        type=int,
        default=0,
        help="Max allowed template side in pixels (0 disables size limit)",
    )
    parser.add_argument("--loop", type=float, default=0, help="Rescan every N seconds (0 = once)")
    parser.add_argument("--save-debug", type=str, help="Save annotated screenshot to this path")
    parser.add_argument("--json", action="store_true", help="Print matches as JSON")
    parser.add_argument(
        "--negative-margin",
        type=float,
        default=0.08,
        help="Reject hit when negative template score is too close to positive score",
    )
    parser.add_argument(
        "--large-template-scale",
        type=float,
        default=DEFAULT_LARGE_TEMPLATE_SCALE,
        help="Fixed scale for large templates (>=120px). Set 0 for auto multi-scale",
    )
    parser.add_argument(
        "--robust",
        action="store_true",
        help="Enable stricter visual + temporal validation (slower, fewer false positives)",
    )
    parser.add_argument(
        "--robust-confirm-frames",
        type=int,
        default=DEFAULT_ROBUST_CONFIRM_FRAMES,
        help="Frames required for temporal confirmation in robust mode",
    )
    parser.add_argument(
        "--robust-confirm-delay",
        type=float,
        default=DEFAULT_ROBUST_CONFIRM_DELAY,
        help="Delay between confirmation frames in robust mode",
    )
    parser.add_argument(
        "--robust-red-ratio",
        type=float,
        default=DEFAULT_ROBUST_RED_RATIO,
        help="Minimum red HSV ratio in candidate patch (robust mode)",
    )
    parser.add_argument(
        "--robust-edge-density",
        type=float,
        default=DEFAULT_ROBUST_EDGE_DENSITY,
        help="Minimum Canny edge density in candidate patch (robust mode)",
    )
    parser.add_argument(
        "--robust-green-max",
        type=float,
        default=DEFAULT_ROBUST_GREEN_MAX,
        help="Maximum green HSV ratio in candidate patch (robust mode)",
    )
    parser.add_argument("--sweep-cols", type=int, default=1, help="Map sweep width in screens")
    parser.add_argument("--sweep-rows", type=int, default=1, help="Map sweep height in screens")
    parser.add_argument(
        "--pan-pixels",
        type=int,
        default=DEFAULT_PAN_PIXELS,
        help="Drag distance per sweep step (larger = less tile overlap)",
    )
    parser.add_argument("--pan-duration", type=float, default=0.25, help="Seconds spent dragging each step")
    parser.add_argument("--pan-settle", type=float, default=0.45, help="Wait after each drag before scanning")
    parser.add_argument(
        "--stop-key",
        type=str,
        default="F8",
        help="Emergency key to stop sweep instantly (ESC/F6/F7/F8/F9/F10)",
    )
    args = parser.parse_args()

    if args.list_windows:
        for title, hwnd in list_visible_windows():
            print(f"{hwnd:8}  {safe_console_text(title)}")
        return

    window_query = args.window.strip()
    if not window_query:
        # Common client / emulator titles
        for guess in (
            "MASS",
            "Gamota",
            "ROK",
            "Rise of Kingdoms",
            "BlueStacks",
            "LDPlayer",
            "Nox",
            "MUMU",
        ):
            try:
                hwnd = find_window_handle(guess)
                window_query = guess
                print(f"Using window matching: {guess}")
                break
            except SystemExit:
                continue
        else:
            raise SystemExit(
                "Could not auto-detect game window. Use --window TITLE or --list-windows."
            )
    else:
        hwnd = find_window_handle(window_query)

    max_template_side = args.max_template_side if args.max_template_side > 0 else None
    templates = load_templates(
        TEMPLATES_DIR,
        required=True,
        max_side=max_template_side,
    )
    negative_templates = load_templates(
        NEGATIVE_TEMPLATES_DIR,
        required=False,
        max_side=max_template_side,
    )
    if negative_templates:
        print(f"Loaded {len(negative_templates)} negative template(s) from {NEGATIVE_TEMPLATES_DIR}")
    total_tiles = args.sweep_cols * args.sweep_rows
    if args.sweep_cols < 1 or args.sweep_rows < 1:
        raise SystemExit("--sweep-cols and --sweep-rows must be >= 1")
    if args.loop > 0 and total_tiles > 1:
        raise SystemExit("Use either --loop or sweep mode, not both at once.")
    if args.robust_confirm_frames < 1:
        raise SystemExit("--robust-confirm-frames must be >= 1")

    def run_scan(label: str | None = None, debug_path: str | None = None) -> list[GemMatch]:
        frame, matches, region = scan_once(
            hwnd,
            templates,
            negative_templates,
            args.threshold,
            args.min_distance,
            args.negative_margin,
            args.large_template_scale,
        )
        if args.robust:
            per_frame = [
                filter_by_visual_robustness(
                    frame,
                    matches,
                    args.robust_red_ratio,
                    args.robust_edge_density,
                    args.robust_green_max,
                )
            ]
            for _ in range(args.robust_confirm_frames - 1):
                if args.robust_confirm_delay > 0:
                    time.sleep(args.robust_confirm_delay)
                next_frame, next_matches, _ = scan_once(
                    hwnd,
                    templates,
                    negative_templates,
                    args.threshold,
                    args.min_distance,
                    args.negative_margin,
                    args.large_template_scale,
                )
                per_frame.append(
                    filter_by_visual_robustness(
                        next_frame,
                        next_matches,
                        args.robust_red_ratio,
                        args.robust_edge_density,
                        args.robust_green_max,
                    )
                )
            matches = temporal_confirm_matches(per_frame, args.min_distance)
        if debug_path and matches:
            debug = draw_matches(frame, matches)
            cv2.imwrite(debug_path, debug)
            print(f"Debug image saved: {debug_path}")

        if args.json:
            payload = {
                "window": window_query,
                "label": label,
                "region": region,
                "count": len(matches),
                "matches": [asdict(m) for m in matches],
            }
            print(json.dumps(payload, indent=2))
        else:
            prefix = f"[{label}] " if label else ""
            print(f"{prefix}Found {len(matches)} gem node(s) on current map view:")
            for i, m in enumerate(matches, 1):
                print(
                    f"  {i}. screen=({region['left'] + m.x}, {region['top'] + m.y}) "
                    f"client=({m.x}, {m.y}) conf={m.confidence:.3f} tpl={m.template}"
                )
        return matches

    if total_tiles > 1:
        focus_window(hwnd)
        if pyautogui is None:
            raise SystemExit(
                "Sweep mode needs pyautogui. Install it with: python -m pip install pyautogui"
            )
        stop_key_name, stop_vk_code = resolve_stop_key(args.stop_key)
        path_order = sweep_positions(args.sweep_rows, args.sweep_cols)
        print(
            f"Sweep mode: {args.sweep_rows}x{args.sweep_cols} ({len(path_order)} screens), "
            f"pan={args.pan_pixels}px."
        )
        print(f"Keep mouse away from game window while sweeping. Press {stop_key_name} to stop.")
        total_found = 0
        offset_x = 0
        offset_y = 0
        merged_points: list[tuple[int, int, float, str]] = []
        try:
            for idx, (row, col) in enumerate(path_order):
                if is_stop_key_pressed(stop_vk_code):
                    raise KeyboardInterrupt
                if idx > 0:
                    prev_row, prev_col = path_order[idx - 1]
                    step_dx = (col - prev_col) * args.pan_pixels
                    step_dy = (row - prev_row) * args.pan_pixels
                    pan_camera(
                        get_window_rect(hwnd),
                        camera_dx=step_dx,
                        camera_dy=step_dy,
                        duration=args.pan_duration,
                        settle=args.pan_settle,
                        stop_vk_code=stop_vk_code,
                    )
                    offset_x += step_dx
                    offset_y += step_dy
                label = f"tile {idx + 1}/{len(path_order)} r{row + 1}c{col + 1}"
                debug_path = (
                    debug_path_for_tile(args.save_debug, row, col)
                    if args.save_debug
                    else None
                )
                tile_matches = run_scan(label=label, debug_path=debug_path)
                total_found += len(tile_matches)
                for m in tile_matches:
                    merged_points.append(
                        (m.x + offset_x, m.y + offset_y, m.confidence, m.template)
                    )
        except KeyboardInterrupt:
            print("\nSweep interrupted by user.")
        unique_found = len(non_max_suppression(merged_points, args.min_distance))
        print(
            f"Sweep complete. Raw detections: {total_found}. "
            f"Unique after overlap merge: {unique_found}"
        )
        return

    if args.loop <= 0:
        run_scan(debug_path=args.save_debug)
        return

    print(f"Scanning every {args.loop}s (Ctrl+C to stop). Keep the map visible and zoomed out.")
    try:
        while True:
            run_scan(debug_path=args.save_debug)
            print("-" * 40)
            time.sleep(args.loop)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
