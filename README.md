# ROK Gem Node Scanner (Windows)

Finds **gem resource nodes** on the visible Rise of Kingdoms map using screenshot + OpenCV template matching. Works with the PC client or an Android emulator (BlueStacks, LDPlayer, Nox, etc.).

> **Disclaimer:** Automation may violate Lilith / ROK terms of service and risks account action. Use at your own risk; this tool only analyzes your screen—it does not send clicks or gather resources.

## Setup

1. Install [Python 3.10+](https://www.python.org/downloads/) and check **“Add to PATH”**.
2. Open PowerShell in this folder:

```powershell
cd c:\Users\Projects\rok-gem-scanner
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Create templates (required)

Template matching needs small images of what a gem node looks like **on your** resolution and graphics settings.

1. Open ROK on the world map, zoom out so nodes are small but readable.
2. Take a screenshot (Win + Shift + S) of a **gem node icon** only.
3. Crop a tight square (~40–80 px) and save as:
   - `templates/gem1.png`
   - Optionally add `gem2.png`, `gem3.png` (different zoom levels or node art).

Tips:

- Use the same zoom level you will scan at.
- Disable UI overlays that cover the map center if possible.
- If you get false positives, add more specific crops or raise `--threshold` (e.g. `0.8`).
- Keep the crop focused on the gem crystal part, not the shared white node circle.
- Keep template size tight (around 40–80 px) for best speed and fewer false positives.
- Large templates are matched at smaller scales automatically (helps when your live zoom is farther out).

## Reduce false positives (stone/gold)

If stone/gold nodes are detected as gems, add **negative templates**:

1. Create folder: `templates/negatives/`
2. Add tight crops of non-gem resource icons (stone/gold/wood/food) at your scan zoom.
3. Run scanner as usual; negatives are auto-loaded and used to reject near-matches.

Optional tuning:

```powershell
python scan_gems.py --window "Rise of Kingdoms" --negative-margin 0.10
```

Higher `--negative-margin` = stricter filtering against non-gem icons.

Speed tip when your zoom is fixed:

```powershell
python scan_gems.py --window "Rise of Kingdoms" --large-template-scale 0.40
```

This skips auto multi-scale search for large templates and is much faster.

Robust mode (fewer false positives from march lines / overlays):

```powershell
python scan_gems.py --window "Rise of Kingdoms" --robust --save-debug found.png
```

## Run

List windows to find your game window title:

```powershell
python scan_gems.py --list-windows
```

One-shot scan:

```powershell
python scan_gems.py --window "Rise of Kingdoms"
```

Save annotated debug image:

```powershell
python scan_gems.py --window "Rise of Kingdoms" --save-debug found.png
```

Continuous scan every 3 seconds:

```powershell
python scan_gems.py --window "Rise of Kingdoms" --loop 3
```

JSON output (for your own macros):

```powershell
python scan_gems.py --window "Rise of Kingdoms" --json
```

Sweep around map (2 rows x 3 columns):

```powershell
python scan_gems.py --window "Rise of Kingdoms" --sweep-rows 2 --sweep-cols 3 --pan-pixels 620 --save-debug sweep.png
```

This creates debug files in a new folder like `sweep_tiles/r1c1.png`, `sweep_tiles/r1c2.png`, etc.
Only tiles with at least one detected node are saved.
Press `F8` during sweep to stop immediately.

## Tuning

| Flag | Default | Purpose |
|------|---------|---------|
| `--threshold` | 0.72 | Higher = fewer false positives, may miss faint nodes |
| `--min-distance` | 28 | Merge duplicate hits near the same node |
| `--max-template-side` | 0 | Limit template size; `0` means no size limit |
| `--negative-margin` | 0.08 | Reject hits that also look like negative (stone/gold/etc.) templates |
| `--large-template-scale` | 0.72 | Lock large-template matching scale (fast mode). Set `0` for auto multi-scale |
| `--robust` | off | Enable stricter visual + temporal validation (slower, cleaner results) |
| `--robust-confirm-frames` | 2 | Require candidate in N consecutive frames |
| `--robust-confirm-delay` | 0.25 | Delay between robust confirmation frames |
| `--robust-red-ratio` | 0.045 | Minimum red HSV ratio around candidate |
| `--robust-edge-density` | 0.045 | Minimum edge density around candidate |
| `--robust-green-max` | 0.22 | Maximum green HSV ratio around candidate |
| `--sweep-rows` / `--sweep-cols` | 1 / 1 | Scan multiple screens in a serpentine path |
| `--pan-pixels` | 620 | How far each map drag moves between sweep tiles |
| `--pan-duration` | 0.25 | Drag speed per sweep move |
| `--pan-settle` | 0.45 | Wait time after drag before capture |
| `--stop-key` | F8 | Emergency stop key during sweep (`ESC/F6/F7/F8/F9/F10`) |

## Limitations

- Only scans **what is currently on screen**—it does not pan the map. Pan manually or extend the script with drag simulation if you need a wider area.
- Lighting, snow effects, and alliance territory colors can break matching; refresh templates after graphics changes.
- Gem nodes look similar to other small map icons; good templates matter more than algorithm tweaks.

## Sweep notes

- Sweep mode physically drags the map, so keep the game window visible and avoid touching mouse/keyboard during the run.
- `--loop` and sweep mode are mutually exclusive.
- Start with small sweeps (`2x2`) and tune `--pan-pixels` if tiles overlap too much or skip too far.
- Sweep summary prints both raw detections and overlap-merged unique detections.
