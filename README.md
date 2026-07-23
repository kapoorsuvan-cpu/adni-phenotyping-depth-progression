# Phenotyping Depth in MCI Progression

Reproducible code for the ADNI study **How Much Phenotyping Is Needed to
Predict 24-Month Progression From Mild Cognitive Impairment to Dementia?**

Repository: <https://github.com/kapoorsuvan-cpu/adni-phenotyping-depth-progression>

Participant-level data are not distributed in this repository. ADNI data may
be downloaded only by approved investigators and may not be redistributed.
The pipeline writes derived participant-level and summary CSV files locally;
all CSV, spreadsheet, and generated-output paths are excluded from Git.

## Data sources

### ADNI primary analysis

Request access through the official
[ADNI data page](https://adni.loni.usc.edu/data-samples/adni-data/). After
approval, download the 16 Study Data CSV exports listed below from the LONI
Image and Data Archive. Preserve the filenames and place them in `csvs/`.

| Domain | Required ADNI export |
|---|---|
| Diagnosis and outcome | `DXSUM.csv` |
| Demographics | `PTDEMOG.csv` |
| Basic clinical assessment | `GDSCALE.csv` |
| Global cognition | `MMSE.csv`, `MOCA.csv` |
| Global staging and function | `CDR.csv`, `FAQ.csv` |
| Cognitive battery | `ADAS.csv`, `NEUROBAT.csv`, `UWNPSYCHSUM.csv` |
| Structural MRI | `UCSFFSX7.csv` |
| APOE | `APOERES.csv` |
| Amyloid PET | `UCBERKELEY_AMY_6MM.csv` |
| Plasma biomarker | `UGOTPTAU181.csv` |
| CSF biomarkers | `UPENNBIOMK_MASTER.csv`, `UPENNBIOMK_ROCHE_ELECSYS.csv` |

The exact required columns and source descriptions are recorded in
[`DATA_REQUIREMENTS.md`](DATA_REQUIREMENTS.md) and
[`input_manifest.json`](input_manifest.json).

### OASIS-2 conceptual replication

The replication uses the official OASIS-2 longitudinal subject-data workbook
from [Washington University](https://sites.wustl.edu/oasisbrains/home/oasis-2/).
`run_all.sh` downloads that workbook to
`external_data/oasis2/oasis_longitudinal_demographics.xlsx` and records its
SHA-256 checksum. Users remain responsible for the OASIS data-use terms.

## Run the study

Requirements:

- macOS, Linux, or Windows Subsystem for Linux
- Python 3.12 through 3.14
- [`uv`](https://docs.astral.sh/uv/)
- approved access to the required ADNI exports

From the repository root:

```bash
bash run_all.sh
```

This command:

1. creates `.venv` from the committed `uv.lock`;
2. runs `scripts/check_inputs.py` to verify all 16 ADNI files and columns;
3. runs `scripts/run_final_analysis.py`;
4. downloads and runs the OASIS-2 replication with
   `scripts/run_oasis2_replication.py --download`; and
5. rebuilds the manuscript and supplement with
   `scripts/build_publication.py`.

Outputs are written to `final_report_outputs/` and `publication/`. These
directories are intentionally untracked.

To validate inputs without running the models:

```bash
uv sync --frozen
.venv/bin/python scripts/check_inputs.py
```

## Authoritative files

- `run_all.sh`: single end-to-end entry point
- `scripts/check_inputs.py`: input validation
- `scripts/run_final_analysis.py`: ADNI cohort, models, inference, tables, and figures
- `scripts/run_oasis2_replication.py`: OASIS-2 conceptual replication
- `scripts/build_publication.py`: manuscript and supplement generation
- `input_manifest.json`: machine-readable input contract
- `uv.lock`: resolved software environment

Exploratory notebooks and diagnostic scripts are not part of the publication
pipeline.

## Analysis design

The primary cohort begins at each participant's first MCI visit. Progression is
all-cause dementia within 24 months. Stable controls require at least 24 months
of follow-up without dementia. Visit-level predictors are joined by `RID` and
harmonized visit month. Preprocessing occurs within resampling folds. Classifier
regularization is selected by inner cross-validation. Adjacent model increments
use paired bootstrap inference with Benjamini-Hochberg correction. Minimal
panels are selected using only the training partition before locked-test
evaluation.
