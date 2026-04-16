# image-organizer

A small Python CLI that uses [NVIDIA NIM](https://build.nvidia.com)'s free
vision API to **categorize, rename, and organize a folder of images** into
clean per-category subfolders. It is well-suited for receipts, pantry
inventories, photo backlogs, and any other unsorted image dump.

The tool:

- Sends each image to a vision model and asks it for `{category, item_name,
  description, confidence}` in strict JSON.
- Moves or copies the image into `output/<category>/<item_name>_NNNN.ext`.
- Routes low-confidence results to an `_review/` folder so you can sort
  them by hand.
- Persists a `manifest.json` so the run is fully **resumable** if you
  interrupt it (or the network drops).

## Prerequisites

- Python **3.10+**
- An NVIDIA NIM API key. Get a free one at
  [build.nvidia.com](https://build.nvidia.com) — sign in, pick any vision
  model (e.g. `meta/llama-3.2-90b-vision-instruct`) and copy the
  `nvapi-...` key from the "Get API Key" panel.

## Install

```bash
git clone <this repo>
cd image-organizer
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env       # then edit .env and paste your key
```

Your `.env` should look like:

```
NVIDIA_API_KEY=nvapi-xxxxxxxxxxxxxxxxxxxxxxxx
```

## Usage

The wrapper script `run.py` is the easiest entry point.

```bash
# Show every flag
python run.py --help

# Safe first run: copy from ./unsorted_images into ./organized_images
python run.py

# Try a run without touching any files
python run.py --dry-run

# Move (don't copy) and use a stricter review threshold
python run.py --mode move --threshold 0.85

# Point at a custom source / output folder
python run.py --source ~/Pictures/pantry --output ~/Pictures/pantry_sorted

# Use a different vision model
python run.py --model meta/llama-3.2-11b-vision-instruct

# Wipe the manifest and start over (asks before deleting)
python run.py --reset
```

### All flags

| Flag           | Default               | Meaning                                                 |
| -------------- | --------------------- | ------------------------------------------------------- |
| `--source`     | `./unsorted_images`   | Directory to scan for images.                           |
| `--output`     | `./organized_images`  | Where sorted folders are created.                       |
| `--mode`       | `copy`                | `copy` (safer) or `move`.                               |
| `--dry-run`    | off                   | Print planned actions without changing files.           |
| `--threshold`  | `0.7`                 | Below this confidence, route to `_review/`.             |
| `--model`      | NVIDIA Llama 3.2 90B  | Override the model name.                                |
| `--reset`      | off                   | Delete the manifest after a confirmation prompt.        |

## Customizing categories

The default categories are tuned for groceries / pantry photos:

```
meat, dairy, groceries, produce, hygiene, beverages,
snacks, frozen, bakery, household, other
```

To use your own list, drop a `categories.txt` file next to where you
run the tool — one category per line, lowercase. Lines starting with
`#` are treated as comments. `other` is always added as a fallback
bucket if you forget to include it.

```text
# categories.txt
electronics
clothing
books
art_supplies
other
```

## How it works

1. The tool scans `--source` for files with a known image extension
   (`.jpg`, `.jpeg`, `.png`, `.webp`, `.gif`).
2. Each image is base64-encoded and POSTed (via the OpenAI-compatible
   SDK) to NVIDIA's NIM endpoint with a strict-JSON prompt.
3. The response is validated: bad categories fall back to `other`,
   `item_name` is forced to snake_case with at most 4 words.
4. The image is moved or copied to `output/<category>/<item_name>_NNNN.ext`.
   If the model's confidence is below `--threshold`, it goes to
   `output/_review/` instead.
5. Each result is written to `output/manifest.json` so a re-run picks
   up where the last one left off.

## The `_review/` folder

Anything under the confidence threshold is dropped here untouched. The
model wasn't sure what it was looking at — sort these by hand or
re-run with a higher-quality model. They are still recorded in the
manifest, so they will not be re-analyzed on resume.

## Troubleshooting

**`HTTP 429: Too Many Requests` / rate-limit errors**
NVIDIA's free tier is roughly 40 requests/minute. The tool already
sleeps `1.6s` between calls and retries with exponential backoff
(2s → 4s → 8s). If you still see 429s, lower your concurrency by
running fewer copies in parallel, or upgrade your NVIDIA tier.

**`VisionError: Vision response was not valid JSON`**
The model occasionally returns prose alongside its JSON. The tool
already strips ```` ``` ```` markdown fences. If a particular image
keeps failing, check the logs — the raw model output is logged at
`DEBUG` level. Smaller / less capable models are more prone to this;
try `--model meta/llama-3.2-90b-vision-instruct`.

**`NVIDIA_API_KEY is not set`**
Either your `.env` file is missing, in the wrong directory, or the
variable name is wrong. Re-copy `.env.example` and confirm you ran
the CLI from the same directory.

**Re-run picked up nothing**
Every successful (or failed-after-retry) image is recorded in
`output/manifest.json`. If you want to start over, run
`python run.py --reset`.

## Running the tests

```bash
python -m pytest tests/ -v
```

The included tests cover the manifest's add/save/load/resume/stats
behavior with a `tmp_path` fixture — no API key required.
