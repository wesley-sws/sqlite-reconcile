# Evaluation Artifacts

This directory contains the generated outputs used by the report evaluation
chapter. Results are written under `evaluation/results/`.

Run the full evaluation harness with the settings used for the submitted results:

```sh
venv/bin/python tools/run_evaluation.py all \
  --sizes 10,20,50,75,100 \
  --repeats 3 \
  --micro-rows 10,100,1000,10000,100000 \
  --micro-repeats 10
```

This writes:

```text
evaluation/results/correctness_scenarios.json
evaluation/results/main_benchmark_results.csv
evaluation/results/microbenchmark_results.csv
evaluation/results/git_smoke.json
```

The same parts can also be run individually:

```sh
venv/bin/python tools/run_correctness_scenarios.py
venv/bin/python tools/run_main_benchmark.py --sizes 10,20,50,75,100 --repeats 3
venv/bin/python tools/run_microbenchmark.py --rows 10,100,1000,10000,100000 --repeats 10
venv/bin/python tools/run_git_smoke.py
```

For a faster draft run of the main benchmark:

```sh
venv/bin/python tools/run_main_benchmark.py --sizes 10,20,50,75,100 --repeats 1
```

## Scripts

- `tools/run_correctness_scenarios.py` runs selected pytest cases and writes
  `correctness_scenarios.json`.
- `tools/run_main_benchmark.py` runs the end-to-end synthetic merge benchmark
  and writes `main_benchmark_results.csv`.
- `tools/run_microbenchmark.py` measures smaller internal costs and writes
  `microbenchmark_results.csv`.
- `tools/run_git_smoke.py` creates a temporary Git repository and checks the
  mergetool workflow, writing `git_smoke.json`.
- `tools/run_evaluation.py` is a convenience wrapper that can run all of the
  above.

## Figures

Install the plotting dependencies and regenerate the report figures with:

```sh
venv/bin/pip install -r evaluation/requirements.txt
venv/bin/python evaluation/plot_report_graphs.py
```

The plotting script reads `evaluation/results/main_benchmark_results.csv` and
`evaluation/results/microbenchmark_results.csv`, then writes PNG/PDF figures under
`evaluation/figures/`.
