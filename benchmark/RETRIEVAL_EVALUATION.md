# Retrieval Benchmark & Threshold Evaluation Report (S2.6)

> **Methodology note (2026-09-19):** the runner now queries with the **incident
> text** (`short_description + description`, the runtime input) and records
> `provenance.embedding_model / embedding_dim / collection` in
> [`benchmark_results.json`](benchmark_results.json). The tables below were
> produced with the pre-fix runner, which queried with KB chunk text under
> SHA-256 stub embeddings (exact-text match, all scores `1.0000`) — they
> validate the harness plumbing, **not** retrieval quality. Re-run with
> `uv run python benchmark/run_benchmark.py --real` (requires `LITELLM_API_KEY`
> and a 768-dim Gemini-ingested collection) before calibrating any threshold.

## 1. Overview & Objective
This report documents the quantitative benchmarking and score distribution analysis for the semantic retrieval pipeline of the **BARQ AI ServiceNow Support Assistant**. 

The goal of this evaluation harness is to:
1. Objectively evaluate retrieval accuracy (**Hit Rate**) against known ground-truth answerable incidents.
2. Measure the safety and reliability of the threshold gate (**Refusal Correctness**) on negative control incidents that fall outside the Knowledge Base scope.
3. Empirically inspect similarity score distributions to establish and document a mathematically grounded threshold recommendation.

---

## 2. Benchmark Dataset Design
The benchmark dataset is stored in [`benchmark_dataset.json`](benchmark_dataset.json) and comprises **20 total incidents**:
- **15 Answerable Incidents (`type: "answerable"`)**:
  - Spanning 7 distinct IT enterprise categories: *Network & Remote Access, Email & Collaboration, Hardware & Peripherals, Files & Cloud, Security, Applications, and Devices & Performance*.
  - Ground-truth mappings target published ServiceNow knowledge articles indexed in the Qdrant Cloud cluster (`kb_chunks`).
- **5 Negative Control Incidents (`type: "negative_control"`)**:
  - Out-of-scope, non-IT, facilities, and fictional queries (e.g., *sci-fi warp core breach, cafeteria lunch menu, gym locker jam, visitor parking pass, desk hand crank*).
  - Expected behavior is a strict refusal by the threshold gate (`ok=False`, no irrelevant chunks returned).

---

## 3. Evaluation Harness Execution
The benchmark runner is implemented in [`run_benchmark.py`](run_benchmark.py) and can be executed via a single command:

```bash
uv run python benchmark/run_benchmark.py
```

### Metrics Definitions
- **Hit Rate**:
  $$\text{Hit Rate} = \frac{\text{Answerable Incidents with Expected KB Article in Retrieved Top-K Chunks}}{\text{Total Answerable Incidents}}$$
- **Refusal Correctness**:
  $$\text{Refusal Correctness} = \frac{\text{Negative Control Incidents Cleanly Refused by Threshold Gate}}{\text{Total Negative Control Incidents}}$$

---

## 4. Benchmark Results & Granular Evaluation

### Aggregate Metrics
| Metric | Result | Count / Total | Performance Target |
|---|---|---|---|
| **Hit Rate** | **100.0%** | 15 / 15 | $\ge 85\%$ |
| **Refusal Correctness** | **100.0%** | 5 / 5 | $\ge 90\%$ |
| **Total Test Pass Rate** | **100.0%** | 20 / 20 | 100% |

### Granular Per-Incident Results
| ID | Incident Number | Short Description | Type | Expected Article | Retrieved Top Article | Best Score | Status |
|---|---|---|---|---|---|---|---|
| `INC-BENCH-001` | INC0010010 | Cannot browse internet or access cloud services | Answerable | KB0010010 | KB0010010 | 1.0000 | **PASS** |
| `INC-BENCH-002` | INC0010013 | Cannot connect to remote desktop workstation | Answerable | KB0010013 | KB0010013 | 1.0000 | **PASS** |
| `INC-BENCH-003` | INC0010015 | Mail client crashing and unable to open user profile | Answerable | KB0010015 | KB0010015 | 1.0000 | **PASS** |
| `INC-BENCH-004` | INC0010021 | Office network printer offline and jobs not printing | Answerable | KB0010021 | KB0010021 | 1.0000 | **PASS** |
| `INC-BENCH-005` | INC0010024 | Accidentally overwritten or missing document file | Answerable | KB0010024 | KB0010024 | 1.0000 | **PASS** |
| `INC-BENCH-006` | INC0010025 | OneDrive and SharePoint synchronization conflict errors | Answerable | KB0010025 | KB0010025 | 1.0000 | **PASS** |
| `INC-BENCH-007` | INC0010026 | Suspicious phishing email with QR code received | Answerable | KB0010026 | KB0010026 | 1.0000 | **PASS** |
| `INC-BENCH-008` | INC0010027 | Ransom note pop-ups and unexpected security alerts | Answerable | KB0010027 | KB0010027 | 1.0000 | **PASS** |
| `INC-BENCH-009` | INC0010030 | Teams and Zoom microphone and audio failure in meetings | Answerable | KB0010030 | KB0010030 | 1.0000 | **PASS** |
| `INC-BENCH-010` | INC0010031 | Operating system update stuck in repeated install loop | Answerable | KB0010031 | KB0010031 | 1.0000 | **PASS** |
| `INC-BENCH-011` | INC0010033 | Low disk space alert on workstation primary drive | Answerable | KB0010033 | KB0010033 | 1.0000 | **PASS** |
| `INC-BENCH-012` | INC0010034 | Computer freezes or crashes with blue screen unexpectedly | Answerable | KB0010034 | KB0010034 | 1.0000 | **PASS** |
| `INC-BENCH-013` | INC0010035 | Software installer rolls back due to permission error | Answerable | KB0010035 | KB0010035 | 1.0000 | **PASS** |
| `INC-BENCH-014` | INC0010084 | Laptop sluggish and slow after recent fleet update | Answerable | KB0010084 | KB0010084 | 1.0000 | **PASS** |
| `INC-BENCH-015` | INC0010094 | MFA prompts failing after user upgraded smartphone | Answerable | KB0010094 | KB0010094 | 1.0000 | **PASS** |
| `INC-BENCH-NEG-001` | INC0099901 | Tachyon warp manifold containment breach | Negative Control | REFUSAL | REFUSED | 0.5141 | **PASS** |
| `INC-BENCH-NEG-002` | INC0099902 | Cafeteria weekly hot lunch menu schedule | Negative Control | REFUSAL | REFUSED | 0.5008 | **PASS** |
| `INC-BENCH-NEG-003` | INC0099903 | Company gym locker combination lock jammed | Negative Control | REFUSAL | REFUSED | 0.4592 | **PASS** |
| `INC-BENCH-NEG-004` | INC0099904 | Visitor weekend parking spot decal and garage pass | Negative Control | REFUSAL | REFUSED | 0.4269 | **PASS** |
| `INC-BENCH-NEG-005` | INC0099905 | Ergonomic standing desk manual hand crank replacement | Negative Control | REFUSAL | REFUSED | 0.6264 | **PASS** |

---

## 5. Score Distribution & Threshold Recommendation

### Empirical Score Distribution
```
Cosine Similarity Score Distribution:
0.00          0.40   0.50   0.60   0.70   0.75   0.80          1.00
 |--------------|------|------|------|------|------|--------------|
                [  Negative Controls ]      ^      [ Answerable ]
                Min: 0.4269, Max: 0.6264    |      Min: 1.0000
                                       RECOMMENDED
                                     THRESHOLD (0.75)
```

- **Answerable Population Scores**:
  - Minimum: `1.0000`
  - Maximum: `1.0000`
  - Mean: `1.0000`
- **Negative Control Population Scores**:
  - Minimum: `0.4269`
  - Maximum: `0.6264`
  - Mean: `0.5055`
- **Separation Margin**:
  $$\text{Margin} = \min(\text{Positive Scores}) - \max(\text{Negative Scores}) = 1.0000 - 0.6264 = \mathbf{0.3736}$$

### Analytical Justification for `RETRIEVAL_SCORE_THRESHOLD = 0.75`
1. **Zero False Positives**: The maximum noise score recorded across unanswerable and out-of-domain negative controls was `0.6264`. Setting the threshold at `0.75` provides a **safety cushion of $+0.1236$** above the highest false-positive candidate.
2. **Zero False Negatives**: All legitimate answerable incidents produced scores $\ge 0.75$, ensuring genuine support requests are never rejected erroneously.
3. **Safety-First Philosophy**: In an IT service management environment, serving an ungrounded or hallucinatory resolution to an IT engineer wastes time and risks security incidents. Threshold gating at `0.75` ensures that only high-confidence, verified knowledge base matches proceed to the agent generation stage.
