#!/usr/bin/env python3
"""Reproducible ADNI analysis for 24-month MCI-to-dementia progression.

This script is the authoritative analysis entry point. It replaces the two
exploratory notebooks, whose visit-string joins and model-selection workflow
were not suitable for final reporting.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from scipy import stats
from scipy.optimize import minimize
from scipy.special import expit
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.metrics import (
    average_precision_score, balanced_accuracy_score, confusion_matrix,
    brier_score_loss, f1_score, mean_absolute_error, mean_squared_error,
    r2_score, roc_auc_score, roc_curve,
)
from sklearn.model_selection import (
    GridSearchCV, StratifiedKFold, KFold, cross_val_predict, train_test_split,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

SEED = 42
HORIZON = 24.0
C_GRID = [0.003, 0.01, 0.03, 0.1, 0.3, 1.0]
BURDEN_INCREMENT = {
    "1 Demographics": 0.5,
    "2 + Depressive symptoms": 0.5,
    "3 + MMSE": 1.0,
    "4 + MoCA": 1.0,
    "5 + CDR-SB": 1.0,
    "6 + Broad cognition": 2.0,
    "7 + MRI": 10.0,
    "8 + APOE": 0.75,
    "9 + AD biomarkers": 9.25,
}
PROJECT = Path(__file__).resolve().parents[1]
CSV_DIR = PROJECT / "csvs"
OUT = PROJECT / "final_report_outputs"
TABLES = OUT / "tables"
FIGURES = OUT / "figures"
SUPP = OUT / "supplement"

sns.set_theme(style="whitegrid", context="paper")
plt.rcParams.update({
    "figure.dpi": 140, "savefig.dpi": 300, "font.size": 10,
    "axes.titlesize": 11, "axes.labelsize": 10,
})


def visit_month(value):
    if pd.isna(value):
        return np.nan
    s = str(value).strip().lower()
    if s in {"bl", "sc", "scmri", "init", "baseline", "m0", "m00"}:
        return 0.0
    m = re.fullmatch(r"m(\d+)", s)
    if m:
        return float(m.group(1))
    y = re.fullmatch(r"y(\d+)", s)
    if y:
        return float(y.group(1)) * 12.0
    try:
        return float(s)
    except ValueError:
        return np.nan


def harmonize_dx(value):
    if pd.isna(value):
        return np.nan
    s = str(value).strip().lower()
    numeric = {"1": "CN", "2": "MCI", "3": "DEM", "4": "MCI",
               "5": "DEM", "6": "DEM", "7": "CN", "8": "MCI", "9": "CN"}
    if s in numeric:
        return numeric[s]
    if "mci" in s or "mild cognitive" in s:
        return "MCI"
    if "dementia" in s or "alzheimer" in s or s in {"ad", "dem"}:
        return "DEM"
    if "normal" in s or s in {"cn", "nl"}:
        return "CN"
    return np.nan


def load(name):
    path = CSV_DIR / f"{name}.csv"
    if not path.exists():
        raise FileNotFoundError(path)
    df = pd.read_csv(path, low_memory=False)
    df.columns = [str(c).strip() for c in df.columns]
    return df


def diagnosis_backbone():
    d = load("DXSUM")
    dx_col = next((c for c in ["DIAGNOSIS", "DX", "DXCHANGE"] if c in d), None)
    if dx_col is None:
        raise ValueError("No diagnosis column found in DXSUM.csv")
    vc = "VISCODE2" if "VISCODE2" in d else "VISCODE"
    out = pd.DataFrame({
        "RID": pd.to_numeric(d["RID"], errors="coerce"),
        "visit_code": d[vc],
        "visit_month": d[vc].map(visit_month),
        "exam_date": pd.to_datetime(d.get("EXAMDATE"), errors="coerce"),
        "dx": d[dx_col].map(harmonize_dx),
    }).dropna(subset=["RID", "visit_month", "dx"])
    out["RID"] = out["RID"].astype(int)
    return out.sort_values(["RID", "visit_month", "exam_date"]).reset_index(drop=True)


def build_cohort(dx, min_stable=24.0):
    rows = []
    counts = {"usable_diagnosis": dx.RID.nunique(),
              "with_mci": dx.loc[dx.dx.eq("MCI"), "RID"].nunique(),
              "no_followup": 0, "insufficient": 0}
    for rid, g in dx.groupby("RID", sort=False):
        mci = g[g.dx.eq("MCI")]
        if mci.empty:
            continue
        idx = mci.iloc[0]
        future = g[g.visit_month.gt(idx.visit_month)].copy()
        if future.empty:
            counts["no_followup"] += 1
            continue
        future["months_after"] = future.visit_month - idx.visit_month
        window = future[future.months_after.between(0, HORIZON, inclusive="right")]
        converted = bool(window.dx.eq("DEM").any())
        max_follow = float(future.months_after.max())
        if not converted and max_follow < min_stable:
            counts["insufficient"] += 1
            continue
        conv_rows = window[window.dx.eq("DEM")]
        rows.append({
            "RID": rid, "index_month": float(idx.visit_month),
            "index_visit_code": idx.visit_code,
            "index_exam_date": idx.exam_date,
            "y_convert_2yr": int(converted),
            "conversion_month": (float(conv_rows.months_after.min())
                                 if converted else np.nan),
            "max_followup_months": max_follow,
            "n_followup_visits": len(future),
        })
    cohort = pd.DataFrame(rows).sort_values("RID").reset_index(drop=True)
    counts.update({"eligible_with_followup": len(cohort) + counts["insufficient"],
                   "final": len(cohort),
                   "stable": int(cohort.y_convert_2yr.eq(0).sum()),
                   "converter": int(cohort.y_convert_2yr.eq(1).sum())})
    return cohort, counts


def best_row_per_key(df, keys, value_cols):
    d = df.copy()
    d["_complete"] = d[value_cols].notna().sum(axis=1)
    d = d.sort_values(keys + ["_complete"], ascending=[True] * len(keys) + [False])
    return d.drop_duplicates(keys, keep="first").drop(columns="_complete")


def month_table(name, selected, prefix, cohort):
    d = load(name)
    d["RID"] = pd.to_numeric(d["RID"], errors="coerce")
    vc = "VISCODE2" if "VISCODE2" in d else ("VISCODE" if "VISCODE" in d else None)
    if vc is None:
        raise ValueError(f"{name} has no visit code")
    d["_month"] = d[vc].map(visit_month)
    d = d.dropna(subset=["RID", "_month"])
    d["RID"] = d["RID"].astype(int)
    selected = [c for c in selected if c in d]
    d = best_row_per_key(d, ["RID", "_month"], selected)
    old_keys = d[["RID", vc]].drop_duplicates()
    old_cov = cohort.merge(old_keys, left_on=["RID", "index_visit_code"],
                           right_on=["RID", vc],
                        how="left", indicator=True)["_merge"].eq("both").mean()
    keys = d[["RID", "_month"]].drop_duplicates()
    new_cov = cohort.merge(keys, left_on=["RID", "index_month"],
                           right_on=["RID", "_month"], how="left",
                           indicator=True)["_merge"].eq("both").mean()
    out = d[["RID", "_month"] + selected].rename(
        columns={"_month": "index_month", **{c: f"{prefix}__{c}" for c in selected}})
    return out, {"table": name, "raw_visit_match": old_cov,
                 "harmonized_month_match": new_cov,
                 "gain": new_cov - old_cov}


def rid_table(name, selected, prefix):
    d = load(name)
    d["RID"] = pd.to_numeric(d["RID"], errors="coerce")
    d = d.dropna(subset=["RID"])
    d["RID"] = d["RID"].astype(int)
    selected = [c for c in selected if c in d]
    d = best_row_per_key(d, ["RID"], selected)
    return d[["RID"] + selected].rename(columns={c: f"{prefix}__{c}" for c in selected})


def numeric_frame(df, cols):
    out = df.copy()
    for c in cols:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    return out


MRI_COLS = [
    "ST10CV", "ST29SV", "ST88SV", "ST12SV", "ST71SV",
    "ST24CV", "ST24TA", "ST83CV", "ST83TA",
    "ST44CV", "ST44TA", "ST103CV", "ST103TA",
    "ST32CV", "ST32TA", "ST91CV", "ST91TA",
    "ST40CV", "ST40TA", "ST99CV", "ST99TA",
    "ST37SV", "ST96SV", "ST52TA", "ST111TA", "ST154SV",
]
MRI_LABELS = {
    "ST10CV": "Intracranial volume", "ST29SV": "Left hippocampal volume",
    "ST88SV": "Right hippocampal volume", "ST12SV": "Left amygdala volume",
    "ST71SV": "Right amygdala volume", "ST24CV": "Left entorhinal volume",
    "ST24TA": "Left entorhinal thickness", "ST83CV": "Right entorhinal volume",
    "ST83TA": "Right entorhinal thickness", "ST44CV": "Left parahippocampal volume",
    "ST44TA": "Left parahippocampal thickness", "ST103CV": "Right parahippocampal volume",
    "ST103TA": "Right parahippocampal thickness", "ST32CV": "Left inferior temporal volume",
    "ST32TA": "Left inferior temporal thickness", "ST91CV": "Right inferior temporal volume",
    "ST91TA": "Right inferior temporal thickness", "ST40CV": "Left middle temporal volume",
    "ST40TA": "Left middle temporal thickness", "ST99CV": "Right middle temporal volume",
    "ST99TA": "Right middle temporal thickness", "ST37SV": "Left lateral ventricle volume",
    "ST96SV": "Right lateral ventricle volume", "ST52TA": "Left precuneus thickness",
    "ST111TA": "Right precuneus thickness", "ST154SV": "Total gray matter volume",
}


def build_analysis_data(cohort):
    df = cohort.copy()
    audits = []
    demo = rid_table("PTDEMOG", ["PTGENDER", "PTEDUCAT", "PTDOBYY"], "demo")
    df = df.merge(demo, on="RID", how="left", validate="one_to_one")
    df["age"] = df.index_exam_date.dt.year - pd.to_numeric(df["demo__PTDOBYY"], errors="coerce")
    df["female"] = df["demo__PTGENDER"].astype(str).str.lower().map({"female": 1, "male": 0})
    df["education_years"] = pd.to_numeric(df["demo__PTEDUCAT"], errors="coerce")
    specs = [
        ("GDSCALE", ["GDTOTAL"], "gds"), ("MMSE", ["MMSCORE"], "mmse"),
        ("MOCA", ["MOCA"], "moca"), ("CDR", ["CDRSB"], "cdr"),
        ("FAQ", ["FAQTOTAL"], "faq"), ("ADAS", ["TOTSCORE", "TOTAL13"], "adas"),
        ("NEUROBAT", ["LIMMTOTAL", "LDELTOTAL", "AVTOT1", "AVTOT2", "AVTOT3",
                      "AVTOT4", "AVTOT5", "AVDEL30MIN", "TRABSCOR", "DIGITSCOR",
                      "CATANIMSC", "BNTTOTAL"], "neuro"),
        ("UWNPSYCHSUM", ["ADNI_MEM", "ADNI_EF", "ADNI_LAN", "ADNI_VS"], "comp"),
        ("UCSFFSX7", MRI_COLS, "mri"),
        ("UCBERKELEY_AMY_6MM", ["AMYLOID_STATUS", "CENTILOIDS"], "amyloid_pet"),
        ("UGOTPTAU181", ["PLASMAPTAU181"], "plasma"),
        ("UPENNBIOMK_MASTER", ["ABETA", "TAU", "PTAU"], "csf_legacy"),
        ("UPENNBIOMK_ROCHE_ELECSYS", ["ABETA40", "ABETA42", "TAU", "PTAU"], "csf_roche"),
    ]
    for name, cols, prefix in specs:
        t, audit = month_table(name, cols, prefix, cohort)
        df = df.merge(t, on=["RID", "index_month"], how="left", validate="one_to_one")
        audits.append(audit)
    apoe = rid_table("APOERES", ["GENOTYPE"], "apoe")
    df = df.merge(apoe, on="RID", how="left", validate="one_to_one")
    df["APOE4_count"] = df["apoe__GENOTYPE"].map(
        lambda x: np.nan if pd.isna(x) else str(x).count("4"))
    df["APOE4_carrier"] = df.APOE4_count.map(
        lambda x: np.nan if pd.isna(x) else int(x > 0))
    num = [c for c in df if c.startswith(
        ("gds__", "mmse__", "moca__", "cdr__", "faq__", "adas__", "neuro__",
         "comp__", "mri__", "amyloid_pet__", "plasma__", "csf_"))]
    df = numeric_frame(df, num)
    df["csf_legacy__ABETA_TAU_ratio"] = df["csf_legacy__ABETA"] / df["csf_legacy__TAU"]
    df["csf_legacy__ABETA_PTAU_ratio"] = df["csf_legacy__ABETA"] / df["csf_legacy__PTAU"]
    if "csf_roche__ABETA40" in df:
        df["csf_roche__ABETA42_40_ratio"] = df["csf_roche__ABETA42"] / df["csf_roche__ABETA40"]
    return df, pd.DataFrame(audits)


def feature_sets(df):
    demo = ["age", "female", "education_years"]
    basic = ["gds__GDTOTAL"]
    mmse = ["mmse__MMSCORE"]
    moca = ["moca__MOCA"]
    cdr = ["cdr__CDRSB"]
    broad = [c for c in df if c.startswith(("faq__", "adas__", "neuro__", "comp__"))]
    apoe = ["APOE4_count"]
    mri = [f"mri__{c}" for c in MRI_COLS if f"mri__{c}" in df]
    bio = [c for c in df if c.startswith(("amyloid_pet__", "plasma__", "csf_"))]
    groups = {"demographics": demo, "basic_clinical": basic, "mmse": mmse,
              "moca": moca, "cdr": cdr, "broad_cognition": broad,
              "apoe": apoe, "mri": mri, "biomarkers": bio}
    order = [
        ("1 Demographics", ["demographics"]),
        ("2 + Depressive symptoms", ["demographics", "basic_clinical"]),
        ("3 + MMSE", ["demographics", "basic_clinical", "mmse"]),
        ("4 + MoCA", ["demographics", "basic_clinical", "mmse", "moca"]),
        ("5 + CDR-SB", ["demographics", "basic_clinical", "mmse", "moca", "cdr"]),
        ("6 + Broad cognition", ["demographics", "basic_clinical", "mmse", "moca", "cdr", "broad_cognition"]),
        ("7 + MRI", ["demographics", "basic_clinical", "mmse", "moca", "cdr", "broad_cognition", "mri"]),
        ("8 + APOE", ["demographics", "basic_clinical", "mmse", "moca", "cdr", "broad_cognition", "mri", "apoe"]),
        ("9 + AD biomarkers", list(groups)),
    ]
    steps = {}
    for label, names in order:
        cols = []
        for name in names:
            cols.extend(groups[name])
        steps[label] = list(dict.fromkeys(c for c in cols if c in df and df[c].notna().sum() >= 25))
    return groups, steps


def model_pipe(kind="classification"):
    prep = ColumnTransformer([("num", Pipeline([
        ("impute", SimpleImputer(strategy="median", add_indicator=True,
                                 keep_empty_features=True)),
        ("scale", StandardScaler()),
    ]), slice(0, None))])
    estimator = (LogisticRegression(C=0.03, solver="liblinear",
                                    max_iter=3000,
                                    random_state=SEED)
                 if kind == "classification" else Ridge(alpha=10.0))
    return Pipeline([("prep", prep), ("model", estimator)])


def nested_oof_classification(df, cols):
    y = df.y_convert_2yr.astype(int).to_numpy()
    outer = StratifiedKFold(5, shuffle=True, random_state=SEED)
    probability = np.full(len(df), np.nan)
    selected_c = []
    for fold, (train, test) in enumerate(outer.split(df[cols], y), 1):
        inner = StratifiedKFold(4, shuffle=True, random_state=SEED + fold)
        search = GridSearchCV(
            model_pipe(), {"model__C": C_GRID}, cv=inner,
            scoring="neg_log_loss", n_jobs=-1, refit=True,
        )
        search.fit(df.iloc[train][cols], y[train])
        probability[test] = search.predict_proba(df.iloc[test][cols])[:, 1]
        selected_c.append(float(search.best_params_["model__C"]))
    return probability, selected_c


def stratified_bootstrap_indices(y, rng):
    y = np.asarray(y)
    groups = [np.flatnonzero(y == value) for value in np.unique(y)]
    return np.concatenate([
        rng.choice(group, size=len(group), replace=True) for group in groups
    ])


def bootstrap_auc(y, p, n=3000, seed_offset=0):
    rng = np.random.default_rng(SEED + seed_offset)
    vals = []
    y = np.asarray(y); p = np.asarray(p)
    for _ in range(n):
        idx = stratified_bootstrap_indices(y, rng)
        vals.append(roc_auc_score(y[idx], p[idx]))
    return np.quantile(vals, [0.025, 0.975])


def bh_adjust(p_values):
    p = np.asarray(p_values, dtype=float)
    out = np.full(len(p), np.nan)
    ok = np.isfinite(p)
    if not ok.any():
        return out
    values = p[ok]
    order = np.argsort(values)
    ranked = values[order]
    adjusted = ranked * len(ranked) / np.arange(1, len(ranked) + 1)
    adjusted = np.minimum.accumulate(adjusted[::-1])[::-1]
    restored = np.empty_like(adjusted)
    restored[order] = np.minimum(adjusted, 1.0)
    out[np.flatnonzero(ok)] = restored
    return out


def paired_auc_comparison(y, new_p, old_p, n=3000, seed_offset=0):
    y = np.asarray(y); new_p = np.asarray(new_p); old_p = np.asarray(old_p)
    observed = roc_auc_score(y, new_p) - roc_auc_score(y, old_p)
    rng = np.random.default_rng(SEED + seed_offset)
    values = np.empty(n)
    for i in range(n):
        idx = stratified_bootstrap_indices(y, rng)
        values[i] = (roc_auc_score(y[idx], new_p[idx]) -
                     roc_auc_score(y[idx], old_p[idx]))
    lo, hi = np.quantile(values, [0.025, 0.975])
    centered = values - observed
    p_value = (1 + np.sum(np.abs(centered) >= abs(observed))) / (n + 1)
    return observed, lo, hi, p_value


def paired_r2_comparison(y, new_p, old_p, n=3000, seed_offset=0):
    y = np.asarray(y); new_p = np.asarray(new_p); old_p = np.asarray(old_p)
    observed = r2_score(y, new_p) - r2_score(y, old_p)
    rng = np.random.default_rng(SEED + seed_offset)
    values = np.empty(n)
    for i in range(n):
        idx = rng.integers(0, len(y), len(y))
        values[i] = r2_score(y[idx], new_p[idx]) - r2_score(y[idx], old_p[idx])
    lo, hi = np.quantile(values, [0.025, 0.975])
    centered = values - observed
    p_value = (1 + np.sum(np.abs(centered) >= abs(observed))) / (n + 1)
    return observed, lo, hi, p_value


def calibration_summary(y, p):
    y = np.asarray(y, dtype=float)
    p = np.clip(np.asarray(p, dtype=float), 1e-6, 1 - 1e-6)
    logit = np.log(p / (1 - p))

    def objective(parameters):
        q = np.clip(expit(parameters[0] + parameters[1] * logit),
                    1e-9, 1 - 1e-9)
        return -np.sum(y * np.log(q) + (1 - y) * np.log(1 - q))

    fit = minimize(objective, np.array([0.0, 1.0]), method="BFGS")
    intercept, slope = fit.x if fit.success else (np.nan, np.nan)
    return {
        "brier_score": brier_score_loss(y, p),
        "mean_predicted_risk": p.mean(),
        "observed_event_rate": y.mean(),
        "calibration_intercept": intercept,
        "calibration_slope": slope,
    }


def class_metrics(y, p, threshold=0.5, seed_offset=0):
    pred = (p >= threshold).astype(int)
    tn, fp, fn, tp = confusion_matrix(y, pred, labels=[0, 1]).ravel()
    lo, hi = bootstrap_auc(y, p, seed_offset=seed_offset)
    return {
        "roc_auc": roc_auc_score(y, p), "roc_auc_ci_low": lo, "roc_auc_ci_high": hi,
        "roc_auc_p_vs_chance": stats.mannwhitneyu(
            np.asarray(p)[np.asarray(y) == 1],
            np.asarray(p)[np.asarray(y) == 0],
            alternative="two-sided").pvalue,
        "pr_auc": average_precision_score(y, p),
        "balanced_accuracy": balanced_accuracy_score(y, pred),
        "sensitivity": tp / (tp + fn), "specificity": tn / (tn + fp),
        "f1": f1_score(y, pred), "threshold": threshold,
        "tn": tn, "fp": fp, "fn": fn, "tp": tp,
        **calibration_summary(y, p),
    }


def stepwise_classification(df, steps):
    y = df.y_convert_2yr.astype(int).to_numpy()
    rows, predictions = [], {}
    for i, (step, cols) in enumerate(steps.items(), 1):
        p, selected_c = nested_oof_classification(df, cols)
        m = class_metrics(y, p, seed_offset=i)
        rows.append({
            "step": i, "feature_set": step, "n_features": len(cols),
            "selected_C_by_outer_fold": " | ".join(f"{x:g}" for x in selected_c),
            "median_selected_C": float(np.median(selected_c)), **m,
        })
        predictions[step] = p
    tab = pd.DataFrame(rows)
    tab["delta_auc"] = tab.roc_auc.diff()
    tab["delta_auc_ci_low"] = np.nan
    tab["delta_auc_ci_high"] = np.nan
    tab["delta_auc_p"] = np.nan
    labels = list(steps)
    for i in range(1, len(labels)):
        delta, lo, hi, p_value = paired_auc_comparison(
            y, predictions[labels[i]], predictions[labels[i - 1]],
            seed_offset=100 + i)
        tab.loc[i, ["delta_auc", "delta_auc_ci_low", "delta_auc_ci_high",
                    "delta_auc_p"]] = [delta, lo, hi, p_value]
    tab["delta_auc_q"] = bh_adjust(tab.delta_auc_p)
    tab["delta_auc_fdr_significant"] = tab.delta_auc_q.lt(0.05)
    tab["roc_auc_q_vs_chance"] = bh_adjust(tab.roc_auc_p_vs_chance)
    return tab, predictions


def pretty_feature(name):
    plain = {
        "age": "Age", "female": "Female sex", "education_years": "Education",
        "APOE4_count": "APOE e4 allele count", "mmse__MMSCORE": "MMSE",
        "moca__MOCA": "MoCA", "cdr__CDRSB": "CDR-SB",
        "gds__GDTOTAL": "GDS total", "faq__FAQTOTAL": "FAQ total",
        "adas__TOTSCORE": "ADAS total", "adas__TOTAL13": "ADAS-13",
        "comp__ADNI_MEM": "ADNI memory composite", "comp__ADNI_EF": "ADNI executive composite",
        "comp__ADNI_LAN": "ADNI language composite", "comp__ADNI_VS": "ADNI visuospatial composite",
        "neuro__LDELTOTAL": "Logical Memory delayed recall",
        "neuro__LIMMTOTAL": "Logical Memory immediate recall",
        "amyloid_pet__AMYLOID_STATUS": "Amyloid PET positivity",
        "amyloid_pet__CENTILOIDS": "Amyloid PET Centiloids",
        "plasma__PLASMAPTAU181": "Plasma p-tau181",
    }
    if name in plain:
        return plain[name]
    if name.startswith("mri__"):
        return MRI_LABELS.get(name.split("__", 1)[1], name)
    return name.replace("__", ": ")


def train_cv_auc(df, idx, cols):
    y = df.loc[idx, "y_convert_2yr"].astype(int)
    cv = StratifiedKFold(5, shuffle=True, random_state=SEED)
    p = cross_val_predict(model_pipe(), df.loc[idx, cols], y, cv=cv,
                          method="predict_proba", n_jobs=-1)[:, 1]
    return roc_auc_score(y, p), p


def minimal_panels(df, groups):
    train_idx, test_idx = train_test_split(
        np.arange(len(df)), test_size=0.25, random_state=SEED,
        stratify=df.y_convert_2yr)
    pools = {
        "Clinical": groups["demographics"] + groups["basic_clinical"] + groups["mmse"] +
                    groups["moca"] + groups["cdr"] + groups["broad_cognition"],
        "Clinical + APOE": groups["demographics"] + groups["basic_clinical"] +
                           groups["mmse"] + groups["moca"] + groups["cdr"] +
                           groups["broad_cognition"] + groups["apoe"],
        "Multimodal": sum(groups.values(), []),
    }
    rows = []
    test_predictions = {}
    test_y = df.loc[test_idx, "y_convert_2yr"].astype(int).to_numpy()
    for family, pool in pools.items():
        pool = list(dict.fromkeys(c for c in pool if c in df and df.loc[train_idx, c].notna().sum() >= 50))
        ranked = []
        for c in pool:
            auc, _ = train_cv_auc(df, train_idx, [c])
            ranked.append((auc, c))
        candidates = [c for _, c in sorted(ranked, reverse=True)[:12]]
        selected = []
        for k in range(1, 6):
            scored = []
            for c in candidates:
                if c not in selected:
                    auc, p = train_cv_auc(df, train_idx, selected + [c])
                    scored.append((auc, c, p))
            auc, chosen, train_p = max(scored)
            selected.append(chosen)
            y_train = df.loc[train_idx, "y_convert_2yr"].astype(int).to_numpy()
            thresholds = np.linspace(0.15, 0.85, 141)
            threshold = max(thresholds, key=lambda t: balanced_accuracy_score(y_train, train_p >= t))
            inner = StratifiedKFold(5, shuffle=True, random_state=SEED)
            search = GridSearchCV(
                model_pipe(), {"model__C": C_GRID}, cv=inner,
                scoring="neg_log_loss", n_jobs=-1, refit=True,
            )
            search.fit(df.loc[train_idx, selected], y_train)
            test_p = search.predict_proba(df.loc[test_idx, selected])[:, 1]
            m = class_metrics(test_y, test_p, threshold,
                              seed_offset=300 + len(rows))
            test_predictions[(family, k)] = test_p
            rows.append({"panel_family": family, "panel_size": k,
                         "features": " | ".join(pretty_feature(x) for x in selected),
                         "feature_codes": " | ".join(selected),
                         "training_cv_auc": auc,
                         "selected_C": search.best_params_["model__C"],
                         "test_n": len(test_idx), **m})
    tab = pd.DataFrame(rows)
    tab["delta_auc_vs_clinical"] = np.nan
    tab["delta_auc_ci_low"] = np.nan
    tab["delta_auc_ci_high"] = np.nan
    tab["delta_auc_p"] = np.nan
    for row_index, row in tab.iterrows():
        if row.panel_family == "Clinical":
            continue
        delta, lo, hi, p_value = paired_auc_comparison(
            test_y, test_predictions[(row.panel_family, row.panel_size)],
            test_predictions[("Clinical", row.panel_size)],
            seed_offset=400 + row_index)
        tab.loc[row_index, ["delta_auc_vs_clinical", "delta_auc_ci_low",
                            "delta_auc_ci_high", "delta_auc_p"]] = [
                                delta, lo, hi, p_value]
    tab["delta_auc_q"] = bh_adjust(tab.delta_auc_p)
    tab["delta_auc_fdr_significant"] = tab.delta_auc_q.lt(0.05)
    tab["roc_auc_q_vs_chance"] = bh_adjust(tab.roc_auc_p_vs_chance)
    return tab, train_idx, test_idx


def score_change(cohort, name, score_col, prefix):
    d = load(name)
    vc = "VISCODE2" if "VISCODE2" in d else "VISCODE"
    d["RID"] = pd.to_numeric(d.RID, errors="coerce")
    d["month"] = d[vc].map(visit_month)
    d["score"] = pd.to_numeric(d[score_col], errors="coerce")
    d = d.dropna(subset=["RID", "month", "score"])
    d.RID = d.RID.astype(int)
    rows = []
    for r in cohort.itertuples():
        g = d[d.RID.eq(r.RID)].copy()
        g["after"] = g.month - r.index_month
        base = g[g.after.between(-3, 3)]
        follow = g[g.after.between(18, 30)]
        if base.empty or follow.empty:
            continue
        b = base.iloc[(base.after.abs()).argmin()]
        f = follow.iloc[((follow.after - 24).abs()).argmin()]
        raw = f.score - b.score
        rows.append({"RID": r.RID, f"{prefix}_baseline": b.score,
                     f"{prefix}_followup": f.score, f"{prefix}_months": f.after,
                     f"{prefix}_change": raw,
                     f"{prefix}_annualized_change": raw / (f.after / 12.0)})
    return pd.DataFrame(rows)


def regression_results(df, steps, changes):
    work = df.copy()
    for c in changes:
        work = work.merge(c, on="RID", how="left", validate="one_to_one")
    rows = []
    keep_steps = [list(steps)[0], list(steps)[2], list(steps)[5],
                  list(steps)[6], list(steps)[8]]
    for outcome, baseline in [("MMSE_change", "MMSE_baseline"),
                              ("MOCA_change", "MOCA_baseline"),
                              ("CDRSB_change", "CDRSB_baseline")]:
        sub = work.dropna(subset=[outcome])
        previous_prediction = None
        for i, step in enumerate(keep_steps, 1):
            cols = list(steps[step])
            if i > 1 and baseline in sub:
                cols = [baseline] + cols
            cols = list(dict.fromkeys(c for c in cols if c in sub and sub[c].notna().sum() >= 20))
            cv = KFold(5, shuffle=True, random_state=SEED)
            pred = cross_val_predict(model_pipe("regression"), sub[cols],
                                     sub[outcome], cv=cv, n_jobs=-1)
            row = {"outcome": outcome.replace("_", " "),
                   "feature_set": step, "n": len(sub), "n_features": len(cols),
                   "r2": r2_score(sub[outcome], pred),
                   "rmse": math.sqrt(mean_squared_error(sub[outcome], pred)),
                   "mae": mean_absolute_error(sub[outcome], pred),
                   "delta_r2": np.nan, "delta_r2_ci_low": np.nan,
                   "delta_r2_ci_high": np.nan, "delta_r2_p": np.nan}
            if previous_prediction is not None:
                delta, lo, hi, p_value = paired_r2_comparison(
                    sub[outcome].to_numpy(), pred, previous_prediction,
                    seed_offset=600 + len(rows))
                row.update({"delta_r2": delta, "delta_r2_ci_low": lo,
                            "delta_r2_ci_high": hi, "delta_r2_p": p_value})
            rows.append(row)
            previous_prediction = pred
    tab = pd.DataFrame(rows)
    tab["delta_r2_q"] = bh_adjust(tab.delta_r2_p)
    tab["delta_r2_fdr_significant"] = tab.delta_r2_q.lt(0.05)
    return tab, work


def table1(df):
    rows = []
    variables = [
        ("Age, years", "age", "continuous"), ("Female", "female", "binary"),
        ("Education, years", "education_years", "continuous"),
        ("APOE e4 carrier", "APOE4_carrier", "binary"),
        ("APOE e4 alleles", "APOE4_count", "continuous"),
        ("MMSE", "mmse__MMSCORE", "continuous"),
        ("CDR-SB", "cdr__CDRSB", "continuous"),
        ("Amyloid PET positive", "amyloid_pet__AMYLOID_STATUS", "binary"),
        ("Plasma p-tau181", "plasma__PLASMAPTAU181", "continuous"),
    ]
    for label, col, kind in variables:
        allv = df[col].dropna(); a = df.loc[df.y_convert_2yr.eq(0), col].dropna()
        b = df.loc[df.y_convert_2yr.eq(1), col].dropna()
        if kind == "continuous":
            fmt = lambda s: f"{s.mean():.1f} ({s.std():.1f})"
            p = stats.ttest_ind(a, b, equal_var=False).pvalue
            pooled_sd = math.sqrt((a.var(ddof=1) + b.var(ddof=1)) / 2)
            smd = (b.mean() - a.mean()) / pooled_sd if pooled_sd else np.nan
        else:
            fmt = lambda s: f"{int(s.sum())} ({100*s.mean():.1f}%)"
            contingency = pd.crosstab(df[col], df.y_convert_2yr)
            p = (stats.fisher_exact(contingency)[1] if contingency.shape == (2, 2)
                 else stats.chi2_contingency(contingency)[1])
            pooled = (a.mean() + b.mean()) / 2
            denominator = math.sqrt(pooled * (1 - pooled))
            smd = (b.mean() - a.mean()) / denominator if denominator else np.nan
        rows.append({"Characteristic": label, "Overall": fmt(allv),
                     "Stable MCI": fmt(a), "Converter": fmt(b),
                     "Available n": len(allv), "Standardized difference": smd,
                     "p value": p})
    tab = pd.DataFrame(rows)
    tab["FDR q value"] = bh_adjust(tab["p value"])
    return tab


def feature_coverage(df, groups):
    rows = []
    for group, cols in groups.items():
        if not cols:
            continue
        present = df[cols].notna()
        rows.append({"Feature domain": group.replace("_", " ").title(),
                     "Features n": len(cols),
                     "Participants with any feature n": int(present.any(axis=1).sum()),
                     "Participants with any feature %": present.any(axis=1).mean(),
                     "Median feature availability %": present.mean().median()})
    return pd.DataFrame(rows)


def predictor_dictionary(df, groups):
    """Generate a feature-level analysis dictionary and availability record."""
    rows = []
    for domain, cols in groups.items():
        for code in cols:
            if "__" in code:
                namespace, source_column = code.split("__", 1)
            else:
                namespace, source_column = "derived", code
            available = int(df[code].notna().sum())
            rows.append({
                "Feature domain": domain.replace("_", " ").title(),
                "Reported feature": pretty_feature(code),
                "Analysis feature code": code,
                "Source namespace": namespace,
                "Source column or derived variable": source_column,
                "Available n": available,
                "Available %": available / len(df),
                "Missing %": 1 - available / len(df),
            })
    return pd.DataFrame(rows).sort_values(["Feature domain", "Reported feature"])


def evaluate_feature_set(df, cols, seed_offset=0):
    y = df.y_convert_2yr.astype(int).to_numpy()
    probability, _ = nested_oof_classification(df, cols)
    return class_metrics(y, probability, seed_offset=seed_offset)


def independent_auc_difference(y_a, p_a, y_b, p_b, n=3000, seed_offset=0):
    y_a = np.asarray(y_a); p_a = np.asarray(p_a)
    y_b = np.asarray(y_b); p_b = np.asarray(p_b)
    observed = roc_auc_score(y_b, p_b) - roc_auc_score(y_a, p_a)
    rng = np.random.default_rng(SEED + seed_offset)
    values = np.empty(n)
    for i in range(n):
        idx_a = stratified_bootstrap_indices(y_a, rng)
        idx_b = stratified_bootstrap_indices(y_b, rng)
        values[i] = (roc_auc_score(y_b[idx_b], p_b[idx_b]) -
                     roc_auc_score(y_a[idx_a], p_a[idx_a]))
    lo, hi = np.quantile(values, [0.025, 0.975])
    centered = values - observed
    p_value = (1 + np.sum(np.abs(centered) >= abs(observed))) / (n + 1)
    return observed, lo, hi, p_value


def subgroup_performance(df, probability):
    definitions = {
        "Sex": [
            ("Male", df.female.eq(0)),
            ("Female", df.female.eq(1)),
        ],
        "Age": [
            ("<75 years", df.age.lt(75)),
            (">=75 years", df.age.ge(75)),
        ],
        "Education": [
            ("<16 years", df.education_years.lt(16)),
            (">=16 years", df.education_years.ge(16)),
        ],
        "APOE e4": [
            ("Non-carrier", df.APOE4_carrier.eq(0)),
            ("Carrier", df.APOE4_carrier.eq(1)),
        ],
    }
    y = df.y_convert_2yr.astype(int).to_numpy()
    probability = np.asarray(probability)
    rows = []
    tests = {}
    for domain_index, (domain, groups) in enumerate(definitions.items()):
        valid_groups = []
        for label, mask in groups:
            mask = mask.fillna(False).to_numpy()
            subgroup_y = y[mask]
            subgroup_p = probability[mask]
            if len(subgroup_y) < 20 or np.unique(subgroup_y).size < 2:
                continue
            lo, hi = bootstrap_auc(
                subgroup_y, subgroup_p, seed_offset=800 + len(rows))
            rows.append({
                "Subgroup domain": domain, "Subgroup": label,
                "n": len(subgroup_y), "Converters n": int(subgroup_y.sum()),
                "ROC-AUC": roc_auc_score(subgroup_y, subgroup_p),
                "ROC-AUC CI low": lo, "ROC-AUC CI high": hi,
                "PR-AUC": average_precision_score(subgroup_y, subgroup_p),
                "Brier score": brier_score_loss(subgroup_y, subgroup_p),
            })
            valid_groups.append((label, subgroup_y, subgroup_p))
        if len(valid_groups) == 2:
            difference, lo, hi, p_value = independent_auc_difference(
                valid_groups[0][1], valid_groups[0][2],
                valid_groups[1][1], valid_groups[1][2],
                seed_offset=900 + domain_index)
            tests[domain] = (difference, lo, hi, p_value)
    tab = pd.DataFrame(rows)
    tab["Second-minus-first AUC difference"] = np.nan
    tab["Difference CI low"] = np.nan
    tab["Difference CI high"] = np.nan
    tab["Heterogeneity p"] = np.nan
    for domain, values in tests.items():
        index = tab.index[tab["Subgroup domain"].eq(domain)][-1]
        tab.loc[index, ["Second-minus-first AUC difference", "Difference CI low",
                        "Difference CI high", "Heterogeneity p"]] = values
    tab["Heterogeneity FDR q"] = bh_adjust(tab["Heterogeneity p"])
    return tab


def figures(step, pred, reg, panels, y):
    colors = sns.color_palette("colorblind")
    fig, ax = plt.subplots(figsize=(8.5, 4.8))
    ax.errorbar(step.step, step.roc_auc, yerr=[step.roc_auc-step.roc_auc_ci_low,
                step.roc_auc_ci_high-step.roc_auc], marker="o", capsize=3,
                color=colors[0])
    ax.set_xticks(step.step, [x.replace(" + ", "\n+ ") for x in step.feature_set],
                  rotation=25, ha="right")
    ax.set_ylabel("Cross-validated ROC-AUC (95% bootstrap CI)")
    for row in step.itertuples():
        if bool(row.delta_auc_fdr_significant):
            ax.text(row.step, row.roc_auc_ci_high + 0.012, "*",
                    ha="center", va="bottom", fontsize=13)
    ax.set_ylim(0.44, 0.95)
    ax.set_title("Stepwise 2-year conversion prediction (* FDR q < 0.05)")
    fig.tight_layout(); fig.savefig(FIGURES / "figure2_stepwise_classification.png"); plt.close(fig)

    burden = np.cumsum([BURDEN_INCREMENT[label] for label in step.feature_set])
    fig, ax = plt.subplots(figsize=(7.2, 4.8))
    ax.plot(burden, step.roc_auc, marker="o", color=colors[1])
    for x, yy, label in zip(burden, step.roc_auc, step.feature_set):
        if label in {step.feature_set.iloc[0], step.feature_set.iloc[5],
                     step.feature_set.iloc[6], step.feature_set.iloc[-1]}:
            ax.annotate(label.split(" ", 1)[1], (x, yy), xytext=(5, 5),
                        textcoords="offset points", fontsize=8)
    ax.set(xlabel="Relative testing-burden score", ylabel="Cross-validated ROC-AUC",
           title="Predictive performance versus phenotyping burden")
    fig.tight_layout(); fig.savefig(FIGURES / "figure3_performance_vs_burden.png"); plt.close(fig)

    fig, axes = plt.subplots(1, 3, figsize=(12, 4), sharey=False)
    for ax, (outcome, g) in zip(axes, reg.groupby("outcome")):
        ax.plot(np.arange(len(g)), g.r2, marker="o")
        ax.axhline(0, color="0.4", lw=.8)
        ax.set_xticks(np.arange(len(g)), [x.split(" ", 1)[0] for x in g.feature_set])
        display_outcome = outcome.replace("CDRSB", "CDR-SB").replace("MOCA", "MoCA")
        ax.set_title(display_outcome)
        ax.set_xlabel("Phenotyping step")
        ax.set_ylabel(r"Cross-validated $R^2$")
    fig.suptitle("Prediction of 2-year continuous score change", y=.98)
    fig.tight_layout(rect=[0, 0, 1, .94])
    fig.savefig(FIGURES / "figure4_continuous_outcomes.png"); plt.close(fig)

    fig, ax = plt.subplots(figsize=(7.4, 4.8))
    styles = {
        "Clinical": ("o", "-", colors[0]),
        "Clinical + APOE": ("s", "--", colors[1]),
        "Multimodal": ("^", ":", colors[2]),
    }
    for family, group in panels.groupby("panel_family", sort=False):
        marker, line_style, color = styles[family]
        ax.plot(group.panel_size, group.roc_auc, marker=marker,
                linestyle=line_style, color=color, label=family, linewidth=1.7)
    ax.set(xlabel="Number of markers", ylabel="Locked-test ROC-AUC",
           title="Minimal-marker performance on the untouched test set")
    ax.set_xticks(range(1, 6))
    ax.set_ylim(0.77, 0.87)
    ax.legend(frameon=True, fontsize=8)
    clinical_curve = panels.query("panel_family == 'Clinical'").set_index("panel_size")
    ax.annotate(
        "APOE was not selected;\ncurves coincide",
        xy=(4, clinical_curve.loc[4, "roc_auc"]),
        xytext=(3.15, 0.862),
        textcoords="data",
        ha="center",
        va="top",
        fontsize=8,
        bbox=dict(
            boxstyle="round,pad=0.3",
            facecolor="white",
            edgecolor="0.75",
            alpha=0.95,
        ),
        arrowprops=dict(arrowstyle="->", color="0.35", lw=.8),
    )
    fig.tight_layout()
    fig.savefig(FIGURES / "figure5_minimal_panels.png"); plt.close(fig)

    full = step.iloc[-1]
    broad = step.loc[step.feature_set.eq("6 + Broad cognition")].iloc[0]
    models = [(broad.feature_set, pred[broad.feature_set], colors[0])]
    if full.feature_set != broad.feature_set:
        models.append((full.feature_set, pred[full.feature_set], colors[1]))

    fig, axes = plt.subplots(1, 3, figsize=(13.2, 4.1))
    for label, probability, color in models:
        fpr, tpr, _ = roc_curve(y, probability)
        auc = roc_auc_score(y, probability)
        axes[0].plot(fpr, tpr, color=color,
                     label=f"{label.split(' ', 1)[1]}: {auc:.3f}")
    axes[0].plot([0, 1], [0, 1], "--", color="0.5")
    axes[0].set(xlabel="False-positive rate", ylabel="True-positive rate",
                title="Discrimination")
    axes[0].legend(fontsize=8)

    for label, probability, color in models:
        bins = pd.qcut(probability, q=10, duplicates="drop")
        calibration = pd.DataFrame(
            {"risk": probability, "outcome": y, "bin": bins}
        ).groupby("bin", observed=True).agg(
            predicted=("risk", "mean"), observed=("outcome", "mean"))
        axes[1].plot(calibration.predicted, calibration.observed,
                     marker="o", color=color, label=label.split(" ", 1)[1])
    axes[1].plot([0, 1], [0, 1], "--", color="0.5")
    axes[1].set(xlim=(0, 1), ylim=(0, 1), xlabel="Predicted risk",
                ylabel="Observed proportion", title="Calibration")

    thresholds = np.linspace(0.05, 0.60, 56)
    prevalence = np.mean(y)
    axes[2].plot(thresholds, np.zeros_like(thresholds), color="0.5",
                 linestyle=":", label="Treat none")
    treat_all = prevalence - (1 - prevalence) * thresholds / (1 - thresholds)
    axes[2].plot(thresholds, treat_all, color="0.35", linestyle="--",
                 label="Treat all")
    for label, probability, color in models:
        net_benefit = []
        for threshold in thresholds:
            predicted = probability >= threshold
            tp = np.sum(predicted & (y == 1))
            fp = np.sum(predicted & (y == 0))
            net_benefit.append(
                tp / len(y) - fp / len(y) * threshold / (1 - threshold))
        axes[2].plot(thresholds, net_benefit, color=color,
                     label=label.split(" ", 1)[1])
    axes[2].set(xlabel="Risk threshold", ylabel="Net benefit",
                title="Decision-curve analysis", ylim=(-0.05, 0.28))
    axes[2].legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(FIGURES / "figure6_discrimination_calibration_utility.png")
    plt.close(fig)


def attrition_figure(counts):
    fig, ax = plt.subplots(figsize=(7.5, 6)); ax.axis("off")
    items = [
        (0.90, f"Usable longitudinal diagnosis\nN = {counts['usable_diagnosis']:,}"),
        (0.70, f"At least one MCI visit\nN = {counts['with_mci']:,}"),
        (0.50, f"MCI index plus follow-up\nN = {counts['eligible_with_followup']:,}"),
        (0.30, f"Strict 24-month cohort\nN = {counts['final']:,}"),
    ]
    for y, text in items:
        ax.text(.5, y, text, ha="center", va="center", transform=ax.transAxes,
                bbox=dict(boxstyle="round,pad=.5", fc="#E8F1F8", ec="#345B73"))
    for y1, y2 in zip([.84,.64,.44], [.76,.56,.36]):
        ax.annotate("", (.5,y2), (.5,y1), xycoords=ax.transAxes,
                    arrowprops=dict(arrowstyle="->", color="#345B73"))
    ax.text(.78,.58, f"Excluded: no follow-up\nN = {counts['no_followup']:,}",
            transform=ax.transAxes, ha="center", fontsize=9)
    ax.text(.78,.38, f"Excluded: <24 months, no conversion\nN = {counts['insufficient']:,}",
            transform=ax.transAxes, ha="center", fontsize=9)
    ax.text(.29,.12, f"Stable MCI\nN = {counts['stable']:,}", transform=ax.transAxes,
            ha="center", bbox=dict(boxstyle="round,pad=.4", fc="#EAF4EA"))
    ax.text(.71,.12, f"Converted\nN = {counts['converter']:,}", transform=ax.transAxes,
            ha="center", bbox=dict(boxstyle="round,pad=.4", fc="#F8EBDD"))
    ax.set_title("Cohort construction", pad=12)
    fig.savefig(FIGURES / "figure1_cohort_attrition.png", bbox_inches="tight"); plt.close(fig)


def write_manifest(counts, groups, steps):
    manifest = {
        "analysis_version": "3.0.0", "random_seed": SEED,
        "prediction_horizon_months": HORIZON,
        "stable_followup_requirement_months": 24,
        "classification_C_grid": C_GRID,
        "classification_tuning_metric": "inner-fold negative log loss",
        "classification_outer_validation": "5-fold stratified out-of-fold",
        "cohort": counts,
        "feature_groups": groups, "stepwise_sets": steps,
        "notes": [
            "All visit-level predictors are joined on RID plus harmonized visit month.",
            "Screening, baseline, scmri, init, m0, and m00 are month 0.",
            "Preprocessing is fit independently inside each cross-validation fold.",
            "Minimal-marker selection is performed only in the training partition.",
            "Adjacent model increments use paired stratified bootstrap inference.",
            "Benjamini-Hochberg control is applied within each family of model comparisons.",
            "Calibration and decision-curve analyses use out-of-fold probabilities.",
        ],
    }
    (OUT / "analysis_manifest.json").write_text(json.dumps(manifest, indent=2, default=str))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-output", action="store_true")
    args = parser.parse_args()
    if OUT.exists() and not args.keep_output:
        shutil.rmtree(OUT)
    for d in [TABLES, FIGURES, SUPP]:
        d.mkdir(parents=True, exist_ok=True)
    dx = diagnosis_backbone()
    cohort, counts = build_cohort(dx, 24)
    cohort18, counts18 = build_cohort(dx, 18)
    df, join_audit = build_analysis_data(cohort)
    groups, steps = feature_sets(df)
    step, predictions = stepwise_classification(df, steps)
    df18, _ = build_analysis_data(cohort18)
    groups18, steps18 = feature_sets(df18)
    sensitivity18_metrics = evaluate_feature_set(
        df18, steps18[list(steps18)[-1]], seed_offset=1000)
    panels, train_idx, test_idx = minimal_panels(df, groups)
    changes = [
        score_change(cohort, "MMSE", "MMSCORE", "MMSE"),
        score_change(cohort, "MOCA", "MOCA", "MOCA"),
        score_change(cohort, "CDR", "CDRSB", "CDRSB"),
    ]
    reg, reg_data = regression_results(df, steps, changes)
    t1 = table1(df)
    coverage = feature_coverage(df, groups)
    dictionary = predictor_dictionary(df, groups)
    full_step = step.iloc[-1]
    sensitivity = pd.DataFrame([
        {"Stable follow-up rule": "24 months (primary)",
         "n": counts["final"], "Converters n": counts["converter"],
         "Stable n": counts["stable"], "ROC-AUC": full_step.roc_auc,
         "ROC-AUC CI low": full_step.roc_auc_ci_low,
         "ROC-AUC CI high": full_step.roc_auc_ci_high,
         "PR-AUC": full_step.pr_auc, "Brier score": full_step.brier_score},
        {"Stable follow-up rule": "18 months (sensitivity)",
         "n": counts18["final"], "Converters n": counts18["converter"],
         "Stable n": counts18["stable"],
         "ROC-AUC": sensitivity18_metrics["roc_auc"],
         "ROC-AUC CI low": sensitivity18_metrics["roc_auc_ci_low"],
         "ROC-AUC CI high": sensitivity18_metrics["roc_auc_ci_high"],
         "PR-AUC": sensitivity18_metrics["pr_auc"],
         "Brier score": sensitivity18_metrics["brier_score"]},
    ])
    full_probability = predictions[step.feature_set.iloc[-1]]
    subgroup = subgroup_performance(df, full_probability)
    outcome_desc = []
    for c in ["MMSE_change", "MOCA_change", "CDRSB_change"]:
        s = reg_data[c].dropna()
        outcome_desc.append({"Outcome": c.replace("_", " "), "n": len(s),
                             "Mean": s.mean(), "SD": s.std(), "Median": s.median(),
                             "Q1": s.quantile(.25), "Q3": s.quantile(.75)})
    recurrence = {}
    for x in panels.feature_codes.str.split(" | ").explode():
        recurrence[x] = recurrence.get(x, 0) + 1
    recurrence = pd.DataFrame([{"Feature": pretty_feature(k), "Feature code": k,
                                "Panel recurrence n": v}
                               for k, v in recurrence.items()]).sort_values(
                                   "Panel recurrence n", ascending=False)
    tables = {
        "table1_baseline_characteristics.csv": t1,
        "table2_feature_coverage.csv": coverage,
        "table10_predictor_dictionary.csv": dictionary,
        "table3_stepwise_classification.csv": step,
        "table4_continuous_outcomes.csv": reg,
        "table5_minimal_marker_panels.csv": panels,
        "table6_outcome_descriptives.csv": pd.DataFrame(outcome_desc),
        "table7_sensitivity_cohorts.csv": sensitivity,
        "table9_subgroup_performance.csv": subgroup,
    }
    for name, tab in tables.items():
        tab.to_csv(TABLES / name, index=False)
    join_audit.to_csv(SUPP / "join_audit_raw_vs_harmonized.csv", index=False)
    recurrence.to_csv(SUPP / "minimal_marker_recurrence.csv", index=False)
    cohort.to_csv(SUPP / "analysis_cohort.csv", index=False)
    peak_label = step.loc[step.roc_auc.idxmax(), "feature_set"]
    pd.DataFrame({
        "RID": df.RID, "observed_conversion_24m": df.y_convert_2yr,
        "peak_model": peak_label,
        "peak_model_oof_probability": predictions[peak_label],
        "full_model_oof_probability": full_probability,
    }).to_csv(SUPP / "out_of_fold_predictions.csv", index=False)
    reg_data[["RID"] + [c for c in reg_data if c.endswith(
        ("_change", "_annualized_change", "_months"))]].to_csv(
            SUPP / "continuous_outcomes_patient_level.csv", index=False)
    attrition_figure(counts)
    figures(step, predictions, reg, panels, df.y_convert_2yr.to_numpy())
    write_manifest(counts, groups, steps)
    print(json.dumps({
        "output": str(OUT), "cohort_n": len(df),
        "converters": counts["converter"], "stable": counts["stable"],
        "full_model_auc": float(step.iloc[-1].roc_auc),
        "best_locked_test_auc": float(panels.roc_auc.max()),
    }, indent=2))


if __name__ == "__main__":
    main()
