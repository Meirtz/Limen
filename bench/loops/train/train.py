"""Train on data/train.csv, evaluate on the validation split, report per-item outcomes."""

import argparse
import json
import os

import limen
from trainer import data, model

ap = argparse.ArgumentParser()
ap.add_argument("--lr", type=float, default=0.1)
ap.add_argument("--epochs", type=int, default=30)
ap.add_argument("--seed", type=int, default=0)
ap.add_argument("--config", default="configs/default.json")
ap.add_argument("--train", default="data/train.csv")
ap.add_argument("--out", default="out")
args = ap.parse_args()

with open(args.config) as f:
    cfg = json.load(f)
features = data.feature_set()
limen.param("lr", args.lr)
limen.param("epochs", args.epochs)
limen.param("l2", cfg["l2"])
limen.param("threshold", cfg["threshold"])

train = data.featurize(data.load(args.train), features)
val = data.featurize(data.load(cfg["val_split"]), features)
w = model.fit(train, lr=args.lr, epochs=args.epochs, l2=cfg["l2"], seed=args.seed)
correct = 0
for rid, x, y in val:
    ok = model.predict(w, x, cfg["threshold"]) == y
    correct += ok
    limen.outcome(rid, "pass" if ok else "fail")
acc = correct / len(val)
limen.metric("val_acc", acc)
os.makedirs(args.out, exist_ok=True)
with open(os.path.join(args.out, "metrics.json"), "w") as f:
    json.dump({"val_acc": acc, "features": features}, f)
print(f"val_acc={acc:.4f}")
