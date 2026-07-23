#!/usr/bin/env python3
"""Conceptual external replication in OASIS-2.

OASIS-2 is smaller and does not contain ADNI's full biomarker battery. The
analysis therefore recreates the same phenotyping-depth question using a
CDR-0.5 index state, CDR>=1 progression within 24 months, and demographics,
MMSE, and derived MRI measures.
"""

from __future__ import annotations

import argparse
import hashlib
import urllib.request
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.model_selection import LeaveOneOut, cross_val_predict
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

SEED = 42
PROJECT = Path(__file__).resolve().parents[1]
DATA = PROJECT / "external_data" / "oasis2" / "oasis_longitudinal_demographics.xlsx"
OUT = PROJECT / "final_report_outputs"
TABLE = OUT / "tables" / "table8_oasis2_external_replication.csv"
FIGURE = OUT / "figures" / "figure7_oasis2_external_replication.png"
SOURCE = ("https://sites.wustl.edu/oasisbrains/files/2024/03/"
          "oasis_longitudinal_demographics-8d83e569fa2e2d30.xlsx")
OFFICIAL_PAGE = "https://sites.wustl.edu/oasisbrains/home/oasis-2/"


def pipeline():
    prep = ColumnTransformer([("num", Pipeline([
        ("impute", SimpleImputer(strategy="median", add_indicator=True)),
        ("scale", StandardScaler()),
    ]), slice(0, None))])
    return Pipeline([("prep", prep), ("model", LogisticRegression(
        solver="liblinear", random_state=SEED,
        max_iter=3000))])


def cohort(raw):
    raw = raw.copy()
    raw["month"] = raw["MR Delay"] / 30.4375
    rows = []
    for sid, g in raw.groupby("Subject ID"):
        g = g.sort_values("month")
        index = g[g.CDR.eq(0.5)]
        if index.empty:
            continue
        idx = index.iloc[0]
        future = g[g.month.gt(idx.month)].copy()
        if future.empty:
            continue
        future["after"] = future.month - idx.month
        converted = ((future.after <= 24) & (future.CDR >= 1)).any()
        if not converted and future.after.max() < 24:
            continue
        rows.append({
            "Subject ID": sid, "y": int(converted), "Age": idx.Age,
            "Female": int(idx["M/F"] == "F"), "EDUC": idx.EDUC, "SES": idx.SES,
            "MMSE": idx.MMSE, "eTIV": idx.eTIV, "nWBV": idx.nWBV, "ASF": idx.ASF,
            "max_followup_months": future.after.max(),
        })
    return pd.DataFrame(rows)


def bootstrap_ci(y, p, n=5000):
    rng = np.random.default_rng(SEED); vals = []
    y = np.asarray(y); p = np.asarray(p)
    for _ in range(n):
        ix = np.concatenate([
            rng.choice(np.flatnonzero(y == value),
                       size=np.sum(y == value), replace=True)
            for value in np.unique(y)
        ])
        vals.append(roc_auc_score(y[ix], p[ix]))
    return np.quantile(vals, [.025, .975])


def bh_adjust(p_values):
    p = np.asarray(p_values, dtype=float)
    out = np.full(len(p), np.nan)
    ok = np.isfinite(p)
    values = p[ok]
    if not len(values):
        return out
    order = np.argsort(values)
    ranked = values[order]
    adjusted = ranked * len(ranked) / np.arange(1, len(ranked) + 1)
    adjusted = np.minimum.accumulate(adjusted[::-1])[::-1]
    restored = np.empty_like(adjusted)
    restored[order] = np.minimum(adjusted, 1)
    out[np.flatnonzero(ok)] = restored
    return out


def permutation_auc_p(y, p, n=10000, seed_offset=0):
    rng = np.random.default_rng(SEED + seed_offset)
    observed = roc_auc_score(y, p)
    null = np.empty(n)
    for i in range(n):
        null[i] = roc_auc_score(rng.permutation(y), p)
    return (1 + np.sum(np.abs(null - 0.5) >= abs(observed - 0.5))) / (n + 1)


def paired_auc_difference(y, new_p, old_p, n=5000, seed_offset=0):
    y = np.asarray(y); new_p = np.asarray(new_p); old_p = np.asarray(old_p)
    observed = roc_auc_score(y, new_p) - roc_auc_score(y, old_p)
    rng = np.random.default_rng(SEED + seed_offset)
    values = np.empty(n)
    for i in range(n):
        ix = np.concatenate([
            rng.choice(np.flatnonzero(y == value),
                       size=np.sum(y == value), replace=True)
            for value in np.unique(y)
        ])
        values[i] = (roc_auc_score(y[ix], new_p[ix]) -
                     roc_auc_score(y[ix], old_p[ix]))
    lo, hi = np.quantile(values, [.025, .975])
    centered = values - observed
    p_value = (1 + np.sum(np.abs(centered) >= abs(observed))) / (n + 1)
    return observed, lo, hi, p_value


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--download", action="store_true")
    args = ap.parse_args()
    if args.download and not DATA.exists():
        DATA.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(
            SOURCE, headers={"User-Agent": "Mozilla/5.0 OASIS-2 reproducibility pipeline"})
        with urllib.request.urlopen(request, timeout=120) as response:
            DATA.write_bytes(response.read())
    if not DATA.exists():
        raise FileNotFoundError(f"{DATA} not found. Re-run with --download.")
    raw = pd.read_excel(DATA)
    c = cohort(raw)
    steps = {
        "Demographics": ["Age", "Female", "EDUC", "SES"],
        "+ MMSE": ["Age", "Female", "EDUC", "SES", "MMSE"],
        "+ MRI": ["Age", "Female", "EDUC", "SES", "MMSE", "eTIV", "nWBV", "ASF"],
    }
    rows = []
    predictions = {}
    for step_index, (step, cols) in enumerate(steps.items()):
        p = cross_val_predict(pipeline(), c[cols], c.y, cv=LeaveOneOut(),
                              method="predict_proba", n_jobs=-1)[:, 1]
        predictions[step] = p
        lo, hi = bootstrap_ci(c.y, p)
        rows.append({"Dataset": "OASIS-2", "Index state": "CDR 0.5",
                     "Outcome": "CDR >=1 within 24 months",
                     "Feature set": step, "n": len(c), "Converters n": int(c.y.sum()),
                     "Stable n": int(c.y.eq(0).sum()), "ROC-AUC": roc_auc_score(c.y, p),
                     "ROC-AUC CI low": lo, "ROC-AUC CI high": hi,
                     "PR-AUC": average_precision_score(c.y, p),
                     "Brier score": brier_score_loss(c.y, p),
                     "ROC-AUC p vs 0.5": permutation_auc_p(
                         c.y.to_numpy(), p, seed_offset=step_index),
                     "Source URL": SOURCE})
    tab = pd.DataFrame(rows)
    tab["ROC-AUC FDR q vs 0.5"] = bh_adjust(tab["ROC-AUC p vs 0.5"])
    tab["Delta AUC"] = np.nan
    tab["Delta AUC CI low"] = np.nan
    tab["Delta AUC CI high"] = np.nan
    tab["Delta AUC p"] = np.nan
    labels = list(steps)
    for i in range(1, len(labels)):
        values = paired_auc_difference(
            c.y.to_numpy(), predictions[labels[i]], predictions[labels[i - 1]],
            seed_offset=100 + i)
        tab.loc[i, ["Delta AUC", "Delta AUC CI low", "Delta AUC CI high",
                    "Delta AUC p"]] = values
    tab["Delta AUC FDR q"] = bh_adjust(tab["Delta AUC p"])
    TABLE.parent.mkdir(parents=True, exist_ok=True)
    FIGURE.parent.mkdir(parents=True, exist_ok=True)
    tab.to_csv(TABLE, index=False)
    sns.set_theme(style="whitegrid", context="paper")
    fig, ax = plt.subplots(figsize=(6.5, 4.2))
    ax.errorbar(np.arange(3), tab["ROC-AUC"],
                yerr=[tab["ROC-AUC"]-tab["ROC-AUC CI low"],
                      tab["ROC-AUC CI high"]-tab["ROC-AUC"]],
                marker="o", capsize=4)
    ax.set_xticks(np.arange(3), tab["Feature set"])
    ax.set(ylabel="Leave-one-subject-out ROC-AUC (95% bootstrap CI)",
           title="OASIS-2 conceptual external replication", ylim=(0, 1))
    fig.tight_layout(); fig.savefig(FIGURE, dpi=300); plt.close(fig)
    sha = hashlib.sha256(DATA.read_bytes()).hexdigest()
    (OUT / "oasis2_data_provenance.txt").write_text(
        f"Official subject-data workbook: {SOURCE}\nSHA-256: {sha}\n"
        f"Official dataset page: {OFFICIAL_PAGE}\n"
        "OASIS-2 is a conceptual replication, not a direct transport validation.\n")
    print(tab.to_string(index=False))


if __name__ == "__main__":
    main()
