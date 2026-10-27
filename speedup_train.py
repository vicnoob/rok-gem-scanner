"""
Repeat Siege Workshop training in Rise of Kingdoms.

Each cycle: select the workshop, open training, click TRAIN, use speedups
(biggest item first), collect the troops, repeat. A speedup may finish the
timer early only when the unused part is at most --waste-minutes (default 5).

Usage:
  python speedup_train.py --window "Rise of Kingdoms"            # loop until F8
  python speedup_train.py --window "Rise of Kingdoms" --loops 3  # three cycles
  python speedup_train.py --window "Rise of Kingdoms" --dry-run  # show next action only
  python speedup_train.py --window "Rise of Kingdoms" --step workshop|train|start|speedup|use|collect
  python speedup_train.py --self-test
"""

from __future__ import annotations

import argparse
import functools
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import pytesseract
import win32api
import win32con
import win32gui

try:
    import pyautogui
except ImportError:
    pyautogui = None

from scan_gems import (
    capture_region,
    find_window_handle,
    get_window_rect,
    is_stop_key_pressed,
    resolve_stop_key,
)

TESSERACT_CANDIDATES = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)
TIME_RE = re.compile(r"(\d{1,3}):(\d{2}):(\d{2})")
DURATION_RE = re.compile(r"(\d+)\s*-\s*(minute|hour)", re.IGNORECASE)
OWNED_RE = re.compile(r"Owned\s*:\s*(\d+)", re.IGNORECASE)
QUICK_RE = re.compile(r"x\s*(\d+)")
UI_DIR = Path(__file__).resolve().parent / "templates" / "ui"
TRAIN_BUTTON_TEMPLATE = UI_DIR / "siege_train_button.png"
SPEEDUP_BUTTON_TEMPLATE = UI_DIR / "siege_speedup_button.png"
COLLECT_BUBBLE_TEMPLATE = UI_DIR / "siege_collect_bubble.png"
STEPS_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "rok-gem-scanner" / "steps"
TRAIN_BUTTON_SCALES = (0.45, 0.55, 0.7, 0.85, 1.0, 1.15, 1.35)


def configure_tesseract() -> None:
    from pathlib import Path

    for candidate in TESSERACT_CANDIDATES:
        if Path(candidate).exists():
            pytesseract.pytesseract.tesseract_cmd = candidate
            return


configure_tesseract()


@dataclass
class SpeedupItem:
    name: str
    seconds: int
    owned: int
    training: bool = True
    button: tuple[int, int] | None = None


@dataclass
class PlannedUse:
    name: str
    seconds: int
    count: int


@dataclass
class SpeedupPlan:
    uses: list[PlannedUse]
    covered_seconds: int
    overshoot_seconds: int | None


@dataclass
class SpeedupView:
    remaining_seconds: int
    items: list[SpeedupItem]
    timer_region: tuple[int, int, int, int]


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def parse_clock(text: str) -> int | None:
    match = TIME_RE.search(text.replace(" ", ""))
    if not match:
        return None
    hours, minutes, seconds = (int(part) for part in match.groups())
    if minutes >= 60 or seconds >= 60:
        return None
    return hours * 3600 + minutes * 60 + seconds


def plan_speedups(
    remaining_seconds: int,
    items: list[SpeedupItem],
    waste_limit_seconds: int,
) -> SpeedupPlan:
    """Choose speedups that finish the queue with at most `waste_limit_seconds` unused.

    If finishing would waste more than that, use only items that fit inside the
    remaining time and leave the rest running.
    """
    if remaining_seconds <= 0 or waste_limit_seconds < 0:
        return SpeedupPlan([], 0, None)

    usable = [item for item in items if item.seconds > 0 and item.owned > 0]
    usable.sort(key=lambda item: (item.training, item.seconds), reverse=True)
    cap = remaining_seconds + waste_limit_seconds
    piece_item: list[int] = []
    piece_count: list[int] = []
    piece_seconds: list[int] = []
    for index, item in enumerate(usable):
        left = min(item.owned, cap // item.seconds)
        bit = 1
        while left > 0:
            take = min(bit, left)
            piece_item.append(index)
            piece_count.append(take)
            piece_seconds.append(item.seconds * take)
            left -= take
            bit *= 2

    inf = 10**9
    best_count = [inf] * (cap + 1)
    parent: list[tuple[int, int] | None] = [None] * (cap + 1)
    best_count[0] = 0
    for piece_index, seconds in enumerate(piece_seconds):
        for total in range(cap, seconds - 1, -1):
            previous = total - seconds
            if best_count[previous] == inf:
                continue
            count = best_count[previous] + piece_count[piece_index]
            if count < best_count[total]:
                best_count[total] = count
                parent[total] = (previous, piece_index)

    finish_candidates = [
        total for total in range(remaining_seconds, cap + 1) if best_count[total] != inf
    ]
    if finish_candidates:
        chosen = min(finish_candidates, key=lambda total: (total - remaining_seconds, best_count[total]))
        overshoot: int | None = chosen - remaining_seconds
    else:
        partials = [total for total in range(0, remaining_seconds + 1) if best_count[total] != inf]
        chosen = max(partials) if partials else 0
        overshoot = None

    counts = [0] * len(usable)
    cursor = chosen
    while cursor > 0 and parent[cursor] is not None:
        previous, piece_index = parent[cursor]
        counts[piece_item[piece_index]] += piece_count[piece_index]
        cursor = previous

    uses = [
        PlannedUse(item.name, item.seconds, count)
        for item, count in zip(usable, counts)
        if count > 0
    ]
    uses.sort(key=lambda use: use.seconds, reverse=True)
    covered = sum(use.seconds * use.count for use in uses)
    return SpeedupPlan(uses, covered, overshoot)


def ocr_text(image: np.ndarray, whitelist: str | None = None, psm: int = 6, otsu: bool = False) -> str:
    if image.size == 0:
        return ""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    enlarged = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    if otsu:
        _, binary = cv2.threshold(enlarged, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    else:
        _, binary = cv2.threshold(enlarged, 165, 255, cv2.THRESH_BINARY)
    config = f"--psm {psm}"
    if whitelist:
        config += f" -c tessedit_char_whitelist={whitelist}"
    return pytesseract.image_to_string(binary, config=config)


def find_use_buttons(frame: np.ndarray) -> list[tuple[int, int, int, int]]:
    blue, green, red = cv2.split(frame)
    mask = ((blue > 200) & (green > 170) & (red < 90)).astype(np.uint8) * 255
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    buttons: list[tuple[int, int, int, int]] = []
    for contour in contours:
        x, y, width, height = cv2.boundingRect(contour)
        if 90 <= width <= 220 and 24 <= height <= 70:
            buttons.append((x, y, width, height))
    buttons.sort(key=lambda box: box[1])
    deduped: list[tuple[int, int, int, int]] = []
    for button in buttons:
        if deduped and abs(button[1] - deduped[-1][1]) < 12:
            continue
        deduped.append(button)
    return deduped


def read_speedup_view(frame: np.ndarray) -> SpeedupView | None:
    buttons = find_use_buttons(frame)
    if len(buttons) < 2:
        return None
    first_x, first_y, first_w, first_h = buttons[0]
    remaining = None
    timer_region = (0, 0, 0, 0)
    for top, bottom, left, right in ((90, 35, 380, 40), (130, 8, 460, first_w + 40)):
        timer_region = (
            max(0, first_y - top),
            max(0, first_y - bottom),
            max(0, first_x - left),
            min(frame.shape[1], first_x + right),
        )
        remaining = read_timer(frame, timer_region)
        if remaining is not None:
            break
    if remaining is None:
        return None

    items: list[SpeedupItem] = []
    for x, y, width, height in buttons:
        row = frame[
            max(0, y - 22) : min(frame.shape[0], y + height + 28),
            max(0, x - 560) : max(0, x - 8),
        ]
        text = ocr_text(row, psm=6)
        duration_match = DURATION_RE.search(text)
        owned_match = OWNED_RE.search(text)
        if not duration_match or not owned_match:
            continue
        amount = int(duration_match.group(1))
        unit = duration_match.group(2).lower()
        seconds = amount * (3600 if unit.startswith("hour") else 60)
        title = re.search(
            rf"{amount}\s*-\s*{duration_match.group(2)}[A-Za-z ]*",
            text,
            re.IGNORECASE,
        )
        name = title.group(0).strip() if title else f"{amount}-{unit.title()}"
        items.append(
            SpeedupItem(
                name=name,
                seconds=seconds,
                owned=int(owned_match.group(1)),
                training="training" in text.lower(),
                button=(x + width // 2, y + height // 2),
            )
        )
    if not items:
        return None
    return SpeedupView(remaining, items, timer_region)


def read_timer(frame: np.ndarray, region: tuple[int, int, int, int]) -> int | None:
    y1, y2, x1, x2 = region
    return parse_clock(ocr_text(frame[y1:y2, x1:x2], whitelist="0123456789:", psm=6))


def find_quick_button(frame: np.ndarray, use_center: tuple[int, int]) -> tuple[tuple[int, int], int] | None:
    """The xN button the game shows left of USE after an item was used once."""
    cx, cy = use_center
    x1, x2 = max(0, cx - 230), max(0, cx - 60)
    y1, y2 = max(0, cy - 35), min(frame.shape[0], cy + 35)
    roi = frame[y1:y2, x1:x2]
    if roi.size == 0:
        return None
    blue, green, red = cv2.split(roi)
    mask = ((blue > 200) & (green > 170) & (red < 90)).astype(np.uint8) * 255
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        x, y, width, height = cv2.boundingRect(contour)
        if not (36 <= width <= 110 and 16 <= height <= 46):
            continue
        count = read_quick_count(roi[y : y + height, x : x + width])
        if count is not None and count > 1:
            return (x1 + x + width // 2, y1 + y + height // 2), count
    return None


def read_quick_count(box: np.ndarray) -> int | None:
    trim = 3
    if box.shape[0] > 2 * trim + 8 and box.shape[1] > 2 * trim + 8:
        box = box[trim:-trim, trim:-trim]
    gray = cv2.cvtColor(box, cv2.COLOR_BGR2GRAY)
    big = cv2.resize(gray, None, fx=5, fy=5, interpolation=cv2.INTER_CUBIC)
    _, binary = cv2.threshold(big, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    # Short labels like "x4" are only read reliably with blank space around them.
    binary = cv2.copyMakeBorder(binary, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=int(binary[0, 0]))
    text = pytesseract.image_to_string(binary, config="--psm 7 -c tessedit_char_whitelist=0123456789x")
    match = QUICK_RE.search(text)
    return int(match.group(1)) if match else None


def describe_plan(view: SpeedupView, plan: SpeedupPlan, waste_limit_seconds: int) -> str:
    lines = [
        f"Remaining: {format_duration(view.remaining_seconds)}",
        f"Waste cap: {format_duration(waste_limit_seconds)}",
    ]
    if not plan.uses:
        lines.append("No speedup will be used. Every owned item would waste more than the cap.")
    else:
        lines.append("Use:")
        for use in plan.uses:
            lines.append(f"  {use.count} x {use.name} ({format_duration(use.seconds)} each)")
        if plan.overshoot_seconds is None:
            left = view.remaining_seconds - plan.covered_seconds
            lines.append(f"Stops with {format_duration(left)} still on the timer.")
        else:
            lines.append(f"Finishes the queue, unused speedup: {format_duration(plan.overshoot_seconds)}.")
    for item in view.items:
        if any(use.name == item.name and use.seconds == item.seconds for use in plan.uses):
            continue
        if item.seconds > view.remaining_seconds:
            waste = item.seconds - view.remaining_seconds
            lines.append(
                f"Skip {item.name}: one item would waste {format_duration(waste)}."
            )
    return "\n".join(lines)


@functools.lru_cache(maxsize=None)
def load_template(path: Path) -> np.ndarray | None:
    if not path.exists():
        return None
    return cv2.imread(str(path), cv2.IMREAD_COLOR)


# Zoom does not change while the loop runs, so after the first good match
# each template is only tried at the scale that worked.
LEARNED_SCALES: dict[str, float] = {}


def match_learned(
    frame: np.ndarray,
    path: Path,
    scales: tuple[float, ...],
    threshold: float,
    key: str | None = None,
) -> tuple[float, tuple[int, int] | None]:
    template = load_template(path)
    if template is None:
        return 0.0, None
    key = key or str(path)
    known = LEARNED_SCALES.get(key)
    score, center, scale = best_match(frame, template, (known,) if known else scales)
    if center is None or score < threshold:
        return score, None
    LEARNED_SCALES[key] = scale
    return score, center


def match_template_scales(
    frame: np.ndarray,
    template: np.ndarray,
    scales: tuple[float, ...],
) -> tuple[float, tuple[int, int] | None]:
    score, center, _ = best_match(frame, template, scales)
    return score, center


def best_match(
    frame: np.ndarray,
    template: np.ndarray,
    scales: tuple[float, ...],
) -> tuple[float, tuple[int, int] | None, float]:
    best_score = 0.0
    best_center: tuple[int, int] | None = None
    best_scale = scales[0] if scales else 1.0
    for scale in scales:
        width = max(8, int(template.shape[1] * scale))
        height = max(8, int(template.shape[0] * scale))
        if height >= frame.shape[0] or width >= frame.shape[1]:
            continue
        scaled = cv2.resize(
            template,
            (width, height),
            interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC,
        )
        result = cv2.matchTemplate(frame, scaled, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(result)
        if score > best_score:
            best_score = float(score)
            best_center = (loc[0] + width // 2, loc[1] + height // 2)
            best_scale = scale
    return best_score, best_center, best_scale


def find_green_buttons(frame: np.ndarray) -> list[tuple[int, int, int, int]]:
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (48, 150, 110), (78, 255, 255))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    buttons: list[tuple[int, int, int, int]] = []
    for contour in contours:
        x, y, width, height = cv2.boundingRect(contour)
        if width < 16 or height < 16 or width > 140 or height > 140:
            continue
        if not 0.55 <= width / height <= 1.8:
            continue
        buttons.append((x + width // 2, y + height // 2, width, height))
    buttons.sort(key=lambda item: item[2] * item[3], reverse=True)
    return buttons


def find_train_menu_button(frame: np.ndarray) -> tuple[int, int] | None:
    if "train_local" not in LEARNED_SCALES:
        _, center = match_learned(frame, TRAIN_BUTTON_TEMPLATE, TRAIN_BUTTON_SCALES, 0.75)
        if center is not None:
            return center
    height, width = frame.shape[:2]
    for cx, cy, box_w, box_h in find_green_buttons(frame):
        x1 = min(width - 1, cx + box_w // 3)
        x2 = min(width, cx + box_w * 5)
        y1 = max(0, cy - box_h * 2)
        y2 = min(height, cy + box_h * 2)
        if x2 - x1 < 20 or y2 - y1 < 20:
            continue
        _, center = match_learned(
            frame[y1:y2, x1:x2],
            TRAIN_BUTTON_TEMPLATE,
            TRAIN_BUTTON_SCALES,
            0.50,
            key="train_local",
        )
        if center is not None:
            return (x1 + center[0], y1 + center[1])
    return None


WORKSHOP_TEMPLATES = tuple(sorted(UI_DIR.glob("siege_workshop*.png")))


def match_workshop(frame: np.ndarray) -> tuple[float, tuple[int, int] | None]:
    best_score = 0.0
    best_center: tuple[int, int] | None = None
    for path in WORKSHOP_TEMPLATES:
        score, center = match_learned(frame, path, (0.6, 0.75, 0.9, 1.0, 1.15, 1.3), 0.62)
        if center is not None and score >= 0.85:
            return score, center
        if score > best_score:
            best_score, best_center = score, center
    return best_score, best_center


def find_workshop_building(frame: np.ndarray) -> tuple[int, int] | None:
    score, center = match_workshop(frame)
    if center is None or score < 0.62:
        return None
    return center


def find_speedup_menu_button(frame: np.ndarray) -> tuple[int, int] | None:
    _, center = match_learned(frame, SPEEDUP_BUTTON_TEMPLATE, (0.7, 0.8, 0.9, 1.0, 1.1, 1.25), 0.75)
    return center


def find_collect_bubble(frame: np.ndarray) -> tuple[int, int] | None:
    _, center = match_learned(frame, COLLECT_BUBBLE_TEMPLATE, (0.8, 0.9, 1.0, 1.1, 1.25), 0.75)
    return center


def color_buttons(
    frame: np.ndarray,
    low: tuple[int, int, int],
    high: tuple[int, int, int],
) -> list[tuple[int, int, int, int]]:
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, low, high)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    height, width = frame.shape[:2]
    boxes = []
    for contour in contours:
        x, y, box_w, box_h = cv2.boundingRect(contour)
        if not (width * 0.06 <= box_w <= width * 0.2 and height * 0.04 <= box_h <= height * 0.1):
            continue
        if not 2.0 <= box_w / box_h <= 4.5:
            continue
        boxes.append((x, y, box_w, box_h))
    return boxes


def find_start_train_button(frame: np.ndarray) -> tuple[tuple[int, int], tuple[int, int, int, int]] | None:
    """Blue TRAIN button, only accepted when the yellow INSTANT (gems) button sits on its left."""
    blues = color_buttons(frame, (85, 120, 150), (105, 255, 255))
    yellows = color_buttons(frame, (15, 150, 150), (30, 255, 255))
    for bx, by, bw, bh in blues:
        for yx, yy, yw, yh in yellows:
            if yx + yw < bx and abs(yy - by) < bh * 0.5 and abs(yh - bh) < bh * 0.4:
                return (bx + bw // 2, by + bh // 2), (bx, by, bw, bh)
    return None


def detect_screen(frame: np.ndarray) -> str:
    if find_start_train_button(frame) is not None:
        return "training_screen"
    if len(find_use_buttons(frame)) >= 2:
        return "speedup_dialog"
    if find_collect_bubble(frame) is not None:
        return "troops_ready"
    if find_train_menu_button(frame) is not None:
        return "menu_idle"
    if find_speedup_menu_button(frame) is not None:
        return "menu_training"
    return "city"


def safe_title(hwnd: int) -> str:
    return win32gui.GetWindowText(hwnd).encode("ascii", "replace").decode("ascii")


def click_client(region: dict[str, int], point: tuple[int, int]) -> None:
    if pyautogui is None:
        raise SystemExit("Clicking needs pyautogui. Install it with: python -m pip install pyautogui")
    pyautogui.FAILSAFE = True
    pyautogui.moveTo(region["left"] + point[0], region["top"] + point[1])
    pyautogui.mouseDown()
    time.sleep(0.08)
    pyautogui.mouseUp()


class Game:
    def __init__(self, hwnd: int, dry_run: bool, stop_vk: int) -> None:
        self.hwnd = hwnd
        self.dry_run = dry_run
        self.stop_vk = stop_vk
        self.last_building: tuple[int, int] | None = None

    def capture(self) -> np.ndarray:
        return capture_region(get_window_rect(self.hwnd))

    def stopped(self) -> bool:
        return is_stop_key_pressed(self.stop_vk)

    def click(self, point: tuple[int, int], what: str) -> bool:
        if self.dry_run:
            print(f"  Dry run: would click {what} at {point}.")
            return False
        self.ensure_focus()
        click_client(get_window_rect(self.hwnd), point)
        return True

    def ensure_focus(self) -> None:
        if win32gui.GetForegroundWindow() == self.hwnd:
            return
        for attempt in range(2):
            try:
                if attempt:
                    # Windows only lets a background process take focus right after a key event.
                    win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
                    win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
                win32gui.ShowWindow(self.hwnd, win32con.SW_SHOW)
                win32gui.SetForegroundWindow(self.hwnd)
            except Exception:
                continue
            time.sleep(0.1)
            if win32gui.GetForegroundWindow() == self.hwnd:
                return
        print(f"  Warning: could not focus the game ({safe_title(win32gui.GetForegroundWindow())} is active). Clicking anyway.")

    def wait_for(self, check, seconds: float = 3.0):
        deadline = time.time() + seconds
        time.sleep(0.15)
        frame = self.capture()
        while True:
            result = check(frame)
            if result is not None or time.time() >= deadline:
                return frame, result
            time.sleep(0.1)
            frame = self.capture()

    def sleep(self, seconds: float) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self.stopped():
                return False
            time.sleep(0.05)
        return True


def save_step(name: str, frame: np.ndarray, *points: tuple[int, int] | None) -> None:
    STEPS_DIR.mkdir(parents=True, exist_ok=True)
    marked = frame.copy()
    for point in points:
        if point is not None:
            cv2.circle(marked, point, 20, (0, 0, 255), 3)
    cv2.imwrite(str(STEPS_DIR / name), marked)


def workshop_menu(frame: np.ndarray):
    return find_train_menu_button(frame) or find_speedup_menu_button(frame)


def select_workshop(game: Game) -> bool:
    frame = game.capture()
    if workshop_menu(frame) is not None:
        print("  Siege Workshop is already selected.")
        return True
    score, building = match_workshop(frame)
    if building is not None and score >= 0.62:
        game.last_building = building
        source = f"image match {score:.2f}"
    elif game.last_building is not None:
        building = game.last_building
        source = f"last known spot (image match only {score:.2f})"
    else:
        save_step("1_workshop_before.png", frame)
        print(f"  Siege Workshop not found (image match {score:.2f}). Is it on screen?")
        return False
    save_step("1_workshop_before.png", frame, building)
    print(f"  Select the Siege Workshop at {building} ({source}).")
    if not game.click(building, "the Siege Workshop"):
        return False
    after, menu = game.wait_for(workshop_menu)
    save_step("2_workshop_after.png", after)
    if menu is None:
        print("  FAILED: the workshop menu did not appear.")
        return False
    print("  OK: workshop menu is open.")
    return True


def open_training(game: Game) -> bool:
    frame = game.capture()
    button = find_train_menu_button(frame)
    save_step("3_train_before.png", frame, button)
    if button is None:
        print("  Train button not visible.")
        return False
    print(f"  Open the training screen ({button}).")
    if not game.click(button, "the train button"):
        return False
    after, opened = game.wait_for(find_start_train_button, 4.0)
    save_step("4_train_after.png", after)
    if opened is None:
        print("  FAILED: the training screen did not open.")
        return False
    print("  OK: training screen is open.")
    return True


def start_training(game: Game) -> bool:
    frame = game.capture()
    found = find_start_train_button(frame)
    save_step("5_start_before.png", frame, found[0] if found else None)
    if found is None:
        print("  Blue TRAIN button not found.")
        return False
    point, (bx, by, bw, bh) = found
    duration = parse_clock(ocr_text(frame[by + bh // 2 : by + bh, bx : bx + bw], whitelist="0123456789:", psm=7))
    print(f"  Start training ({format_duration(duration) if duration else 'time unread'}).")
    if not game.click(point, "TRAIN"):
        return False
    after, closed = game.wait_for(lambda f: True if find_start_train_button(f) is None else None, 4.0)
    save_step("6_start_after.png", after)
    if closed is None:
        print("  FAILED: the training screen is still open (not enough resources?).")
        return False
    print("  OK: training started.")
    return True


def open_speedup(game: Game) -> SpeedupView | None:
    frame = game.capture()
    view = read_speedup_view(frame)
    if view is not None:
        return view
    button = find_speedup_menu_button(frame)
    save_step("7_speedup_before.png", frame, button)
    if button is None:
        print("  Speed up button not visible.")
        return None
    print(f"  Open the speed up list ({button}).")
    if not game.click(button, "speed up"):
        return None
    after, view = game.wait_for(read_speedup_view, 4.0)
    save_step("8_speedup_dialog.png", after)
    if view is None:
        print("  FAILED: the speed up dialog did not open or could not be read.")
    return view


def close_speedup_dialog(game: Game) -> bool:
    frame = game.capture()
    buttons = find_use_buttons(frame)
    if len(buttons) < 2:
        return True
    x, y, width, _ = buttons[0]
    close_x = (x + width + 33, y - 114)
    print("  Close the speed up list.")
    if not game.click(close_x, "the speed up close button"):
        return False
    _, closed = game.wait_for(lambda f: True if len(find_use_buttons(f)) < 2 else None)
    if closed is None:
        pyautogui.press("esc")
        _, closed = game.wait_for(lambda f: True if len(find_use_buttons(f)) < 2 else None)
    return closed is not None


def use_speedups(game: Game, waste_limit: int, max_uses: int = 60) -> str:
    """Click USE, biggest useful item first. Returns finished, leftover, or failed."""
    frame = game.capture()
    view = read_speedup_view(frame)
    if view is None:
        print("  Speed up dialog is not open or could not be read.")
        return "failed"
    remaining = view.remaining_seconds
    last_item: SpeedupItem | None = None
    for used in range(max_uses):
        if game.stopped():
            return "failed"
        plan = plan_speedups(remaining, view.items, waste_limit)
        if not plan.uses:
            print(
                f"  {format_duration(remaining)} left. Every item would waste "
                f"more than {format_duration(waste_limit)}, letting it run."
            )
            return "leftover" if close_speedup_dialog(game) else "failed"
        use = plan.uses[0]
        item = next(i for i in view.items if i.name == use.name and i.seconds == use.seconds)
        point, count = item.button, 1
        quick = find_quick_button(frame, item.button)
        if quick is None and use.count > 1 and item is last_item:
            frame, quick = game.wait_for(lambda f: find_quick_button(f, item.button), 2.0)
            if quick is None:
                save_step("9_quick_missing.png", frame, item.button)
        if quick is not None and quick[1] <= use.count:
            point, count = quick
        steps = ", ".join(f"{u.count} x {format_duration(u.seconds)}" for u in plan.uses)
        print(f"  {format_duration(remaining)} left: use {count} x {item.name} (plan: {steps})")
        before_seconds = remaining
        expected_drop = count * item.seconds
        if not game.click(point, f"USE x{count} {item.name}"):
            return "failed"

        def timer_dropped(current: np.ndarray):
            if len(find_use_buttons(current)) < 2:
                return "closed"
            left = read_timer(current, view.timer_region)
            if left is not None and left <= before_seconds - expected_drop + item.seconds // 2:
                return left
            return None

        frame, result = game.wait_for(timer_dropped, 4.0)
        if result == "closed":
            save_step(f"9_use_{used + 1}.png", frame)
            print("  OK: queue finished.")
            return "finished"
        if result is None:
            save_step(f"9_use_{used + 1}.png", frame)
            print("  FAILED: the timer did not go down after USE.")
            return "failed"
        remaining = result
        item.owned -= count
        last_item = item
    print(f"  Stopped after {max_uses} uses (safety cap).")
    return "failed"


def collect_troops(game: Game) -> bool:
    frame = game.capture()
    bubble = find_collect_bubble(frame)
    save_step("10_collect_before.png", frame, bubble)
    if bubble is None:
        print("  No finished troops to collect.")
        return False
    print(f"  Collect finished troops ({bubble}).")
    if not game.click(bubble, "the finished troops"):
        return False
    after, gone = game.wait_for(lambda f: True if find_collect_bubble(f) is None else None)
    save_step("11_collect_after.png", after)
    if gone is None:
        print("  FAILED: the troops bubble is still there.")
        return False
    print("  OK: troops collected.")
    return True


def run_loop(game: Game, waste_limit: int, loops: int, max_failures: int = 3) -> None:
    collected = 0
    failures = 0
    letting_run = False
    while True:
        if game.stopped():
            print("Stopped.")
            return
        screen = detect_screen(game.capture())
        print(f"[{time.strftime('%H:%M:%S')}] Screen: {screen}")
        if screen == "training_screen":
            ok = start_training(game)
            if ok:
                letting_run = False
        elif screen == "speedup_dialog":
            result = use_speedups(game, waste_limit)
            ok = result != "failed"
            letting_run = result == "leftover"
        elif screen == "troops_ready":
            ok = collect_troops(game)
            if ok:
                collected += 1
                letting_run = False
                print(f"Cycle {collected} done.")
                if loops > 0 and collected >= loops:
                    print(f"Finished {collected} training cycle(s).")
                    return
        elif screen == "menu_idle":
            ok = open_training(game)
        elif screen == "menu_training" and not letting_run:
            ok = open_speedup(game) is not None
        elif letting_run:
            print("  Waiting for the queue to finish on its own.")
            ok = game.sleep(10)
        else:
            ok = select_workshop(game)
        if game.dry_run:
            return
        if ok:
            failures = 0
            continue
        failures += 1
        LEARNED_SCALES.clear()
        if failures >= max_failures:
            print(f"Stopping after {failures} failed steps in a row. Screenshots: {STEPS_DIR}")
            return
        game.sleep(1.0)


STEP_ACTIONS = {
    "workshop": select_workshop,
    "train": open_training,
    "start": start_training,
    "collect": collect_troops,
}


def run_step(game: Game, step: str, waste_limit: int) -> None:
    if step in STEP_ACTIONS:
        STEP_ACTIONS[step](game)
    elif step == "speedup":
        view = open_speedup(game)
        if view is not None:
            print("  OK: speed up dialog is open.")
            print(describe_plan(view, plan_speedups(view.remaining_seconds, view.items, waste_limit), waste_limit))
    elif step == "use":
        use_speedups(game, waste_limit)
    print(f"Screenshots: {STEPS_DIR}")


def self_test() -> None:
    hour = SpeedupItem("60-Minute Training Speedup", 3600, 5)
    five = SpeedupItem("5-Minute Training Speedup", 300, 109)
    one = SpeedupItem("1-Minute Training Speedup", 60, 16)
    three_hours = SpeedupItem("3-Hour Training Speedup", 3 * 3600, 4)

    short = plan_speedups(5 * 60, [hour], 5 * 60)
    assert short.uses == [], short

    almost_hour = plan_speedups(58 * 60, [hour], 5 * 60)
    assert almost_hour.uses == [PlannedUse(hour.name, 3600, 1)], almost_hour
    assert almost_hour.overshoot_seconds == 2 * 60

    video = plan_speedups(1 * 3600 + 39 * 60 + 59, [one, five, hour, three_hours], 5 * 60)
    assert video.uses == [
        PlannedUse(hour.name, 3600, 1),
        PlannedUse(five.name, 300, 8),
    ], video
    assert video.overshoot_seconds == 1

    ten = SpeedupItem("10-Minute Training Speedup", 600, 5)
    partial = plan_speedups(12 * 60, [ten], 5 * 60)
    assert partial.uses == [PlannedUse(ten.name, 600, 1)], partial
    assert partial.overshoot_seconds is None

    exact = plan_speedups(6 * 60, [five, one], 5 * 60)
    assert exact.covered_seconds == 6 * 60
    assert exact.overshoot_seconds == 0

    ten_many = SpeedupItem("10-Minute Training Speedup", 600, 32)
    five_many = SpeedupItem("5-Minute Training Speedup", 300, 228)
    hour_many = SpeedupItem("60-Minute Training Speedup", 3600, 146)
    live = plan_speedups(1 * 3600 + 23 * 60 + 48, [five_many, ten_many, hour_many], 5 * 60)
    assert [use.seconds for use in live.uses] == [3600, 600, 300], live
    assert [use.count for use in live.uses] == [1, 2, 1], live
    print("self-test ok")


def main() -> None:
    parser = argparse.ArgumentParser(description="Repeat ROK siege training and speedups")
    parser.add_argument("--window", default="Rise of Kingdoms", help="Window title substring")
    parser.add_argument("--waste-minutes", type=float, default=5, help="Maximum unused speedup time")
    parser.add_argument("--dry-run", action="store_true", help="Show the next action without clicking")
    parser.add_argument(
        "--loops",
        type=int,
        default=0,
        help="Stop after collecting this many batches (0 = until the stop key)",
    )
    parser.add_argument("--stop-key", default="F8", help="Emergency stop key (ESC/F6/F7/F8/F9/F10)")
    parser.add_argument("--self-test", action="store_true", help="Check the speedup math and exit")
    parser.add_argument(
        "--step",
        choices=["workshop", "train", "start", "speedup", "use", "collect"],
        help=(
            "Run one step only and save screenshots "
            "(workshop = select the Siege Workshop, train = open the training screen, "
            "start = click the blue TRAIN button, speedup = open the speed up list and show the plan, "
            "use = click USE largest item first, collect = collect finished troops)"
        ),
    )
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    if args.waste_minutes < 0:
        raise SystemExit("--waste-minutes must be >= 0")
    if pyautogui is None and not args.dry_run:
        raise SystemExit("Clicking needs pyautogui. Install it with: python -m pip install pyautogui")

    waste_limit = int(args.waste_minutes * 60)
    stop_name, stop_vk = resolve_stop_key(args.stop_key)
    hwnd = find_window_handle(args.window)
    game = Game(hwnd, args.dry_run, stop_vk)
    game.ensure_focus()
    if args.step:
        run_step(game, args.step, waste_limit)
        return
    print(
        "Loop: select Siege Workshop, train, speed up, collect, repeat. "
        f"Waste cap {format_duration(waste_limit)}. Press {stop_name} to stop."
    )
    if not args.dry_run:
        print("Starting in 2 seconds. Move the mouse to a corner to abort.")
        if not game.sleep(2):
            print("Stopped before clicking.")
            return
    try:
        run_loop(game, waste_limit, args.loops)
    except pyautogui.FailSafeException:
        print("Stopped: mouse moved to a screen corner.")


if __name__ == "__main__":
    main()
