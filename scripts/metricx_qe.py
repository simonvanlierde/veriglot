# /// script
# requires-python = ">=3.12"
# dependencies = ["torch", "transformers>=4.57,<5", "sentencepiece", "protobuf"]
# ///
"""Score every benchmark item with MetricX-24 in quality-estimation mode (no reference).

Needs a CUDA GPU and a checkout of google-research/metricx at commit fc4978eb064670f7cc33e93ea4f52d38396b8ae6
(its ``metricx24`` folder holds ``models.py``). Run with uv, which reads the dependencies above and leaves
the project lock alone:

    git clone https://github.com/google-research/metricx
    git -C metricx checkout fc4978eb064670f7cc33e93ea4f52d38396b8ae6
    uv run scripts/metricx_qe.py data/bench/items.jsonl data/bench/runs/metricx24-qe --metricx metricx/metricx24

Writes ``scores.jsonl`` (canary line first) and ``manifest.json`` into the output directory. A lower
score is better; scores are clipped to [0, 25]. The published numbers came from the same ``models.py``
with one change for transformers 5 (a removed module constant read through ``getattr``) and
transformers 4.57.6; this script pins transformers below 5 and has not been run against the unpatched file.
"""

import argparse
import datetime as dt
import hashlib
import json
import sys
import time
from pathlib import Path

import torch
import transformers

MODEL = "google/metricx-24-hybrid-xl-v2p6-bfloat16"
MODEL_REVISION = "1aa765b1705d36c85c9fab40e2f7ce1e0ff69705"  # gitleaks:allow - a Hugging Face commit hash
TOKENIZER = "google/mt5-xl"
TOKENIZER_REVISION = "63fc6450d80515b48e026b69ef2fbbd426433e84"  # gitleaks:allow - a Hugging Face commit hash
MAX_LENGTH = 1536


def main() -> None:
    """Read the items, score each one and write the scores and a manifest."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("items", type=Path, help="items.jsonl from `veriglot bench make`")
    parser.add_argument("out", type=Path, help="output directory")
    parser.add_argument("--metricx", type=Path, required=True, help="the metricx24 folder of the metricx checkout")
    args = parser.parse_args()
    sys.path.insert(0, args.metricx.as_posix())
    import models  # noqa: PLC0415 - found through the path given above

    lines = args.items.read_text(encoding="utf-8").splitlines()
    canary, items = lines[0], [json.loads(line) for line in lines[1:] if line.strip()]
    tokenizer = transformers.AutoTokenizer.from_pretrained(TOKENIZER, revision=TOKENIZER_REVISION)
    model = (
        models.MT5ForRegression.from_pretrained(MODEL, revision=MODEL_REVISION, torch_dtype=torch.bfloat16)
        .cuda()
        .eval()
    )

    def score(source: str, hypothesis: str) -> float:
        encoded = tokenizer(f"source: {source} candidate: {hypothesis}", max_length=MAX_LENGTH, truncation=True)
        # Drop the final EOS as MetricX's predict.py does. One unpadded sequence needs no attention mask.
        ids = torch.tensor([encoded["input_ids"][:-1]]).cuda()
        with torch.no_grad():
            return max(0.0, min(25.0, float(model(input_ids=ids, use_cache=False).predictions[0])))

    args.out.mkdir(parents=True, exist_ok=True)
    started = time.time()
    with (args.out / "scores.jsonl").open("w", encoding="utf-8", newline="\n") as f:
        f.write(canary + "\n")
        for item in items:
            row = {"id": item["id"], "locale": item["locale"], "kind": item["kind"]}
            row["score"] = round(score(item["source"], item["target"]), 4)
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    manifest = {
        "model_repo": MODEL,
        "revision": MODEL_REVISION,
        "tokenizer": TOKENIZER,
        "tokenizer_revision": TOKENIZER_REVISION,
        "code": "google-research/metricx@fc4978eb064670f7cc33e93ea4f52d38396b8ae6 metricx24/models.py",
        "mode": f"QE, input 'source: S candidate: T', max_length {MAX_LENGTH}, final EOS dropped, clipped [0,25]",
        "dtype": "bfloat16",
        "torch": torch.__version__,
        "transformers": transformers.__version__,
        "items_sha256": hashlib.sha256(args.items.read_bytes()).hexdigest(),
        "n": len(items),
        "date": dt.datetime.now(dt.UTC).date().isoformat(),
        "seconds": round(time.time() - started, 1),
        "gpu": torch.cuda.get_device_name(0),
    }
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
