# Data requirements

The repository contains analysis code only. It does not contain raw or derived
participant-level data.

## ADNI primary analysis

Source repository: Alzheimer's Disease Neuroimaging Initiative data distributed
through the LONI Image and Data Archive.

- Access and application: <https://adni.loni.usc.edu/data-samples/adni-data/>
- Data Use Agreement: <https://adni.loni.usc.edu/wp-content/themes/adni_2023/documents/ADNI_Data_Use_Agreement.pdf>
- Local destination after download: `csvs/`

Approved investigators must download these exports with their original
filenames:

| Filename | ADNI domain | Variables used by the pipeline |
|---|---|---|
| `DXSUM.csv` | Diagnosis | Participant ID, visit, examination date, diagnosis |
| `PTDEMOG.csv` | Demographics | Sex, education, birth year |
| `GDSCALE.csv` | Clinical assessment | Geriatric Depression Scale total |
| `MMSE.csv` | Global cognition | MMSE total |
| `MOCA.csv` | Global cognition | MoCA total |
| `CDR.csv` | Global staging | CDR Sum of Boxes |
| `FAQ.csv` | Function | Functional Activities Questionnaire total |
| `ADAS.csv` | Cognitive battery | ADAS totals |
| `NEUROBAT.csv` | Cognitive battery | Memory, executive, language, and visuospatial scores |
| `UWNPSYCHSUM.csv` | Composite cognition | ADNI memory, executive, language, and visuospatial composites |
| `UCSFFSX7.csv` | Processed structural MRI | FreeSurfer 7 regional volumes and cortical thicknesses |
| `APOERES.csv` | Genetics | APOE genotype |
| `UCBERKELEY_AMY_6MM.csv` | Processed amyloid PET | Amyloid status and Centiloids |
| `UGOTPTAU181.csv` | Plasma biomarker | Plasma p-tau181 |
| `UPENNBIOMK_MASTER.csv` | CSF biomarkers | A-beta, total tau, and phosphorylated tau |
| `UPENNBIOMK_ROCHE_ELECSYS.csv` | CSF biomarkers | Elecsys A-beta 40/42, total tau, and phosphorylated tau |

The machine-readable column contract is in `input_manifest.json`. Run
`scripts/check_inputs.py` before analysis to identify missing files or columns.
The script reads headers only.

The expected layout is:

```text
adni-phenotyping-depth-progression/
├── csvs/
│   ├── DXSUM.csv
│   ├── PTDEMOG.csv
│   ├── GDSCALE.csv
│   ├── MMSE.csv
│   ├── MOCA.csv
│   ├── CDR.csv
│   ├── FAQ.csv
│   ├── ADAS.csv
│   ├── NEUROBAT.csv
│   ├── UWNPSYCHSUM.csv
│   ├── UCSFFSX7.csv
│   ├── APOERES.csv
│   ├── UCBERKELEY_AMY_6MM.csv
│   ├── UGOTPTAU181.csv
│   ├── UPENNBIOMK_MASTER.csv
│   └── UPENNBIOMK_ROCHE_ELECSYS.csv
├── scripts/
└── run_all.sh
```

## OASIS-2 conceptual replication

Source repository: Open Access Series of Imaging Studies at Washington
University.

- Dataset page: <https://sites.wustl.edu/oasisbrains/home/oasis-2/>
- Input workbook: `oasis_longitudinal_demographics.xlsx`
- Local destination: `external_data/oasis2/`

The `--download` option in `scripts/run_oasis2_replication.py` retrieves the
official longitudinal subject-data workbook. The end-to-end `run_all.sh`
command supplies this option automatically and writes the source URL plus
SHA-256 checksum to `final_report_outputs/oasis2_data_provenance.txt`.

## Commands

From the repository root, run the complete study:

```bash
bash run_all.sh
```

To validate the ADNI inputs without fitting models:

```bash
uv sync --frozen
.venv/bin/python scripts/check_inputs.py
```

To run only one analysis after environment setup:

```bash
.venv/bin/python scripts/run_final_analysis.py
.venv/bin/python scripts/run_oasis2_replication.py --download
.venv/bin/python scripts/build_publication.py
```
