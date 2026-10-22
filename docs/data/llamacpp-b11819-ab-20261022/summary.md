## llama-bench (t/s, mean of repetitions per run; spread = run-to-run within an arm)

| slot | test | depth | A b9850 | B b11819 | Δ% | spread A | spread B | verdict |
|---|---|---:|---:|---:|---:|---:|---:|---|
| big | pp512 | 0 | 725.9 | 798.7 | 10.0 | - | - | FASTER |
| big | tg128 | 0 | 22.3 | 22.6 | 1.3 | - | - | FASTER |
| big | pp512 | 8192 | 641.7 | 695.1 | 8.3 | - | - | FASTER |
| big | tg128 | 8192 | 21.5 | 21.9 | 1.6 | - | - | FASTER |
| chat | pp512 | 0 | 764.1 | 857.0 | 12.2 | 1.2 | 0.4 | FASTER |
| chat | tg128 | 0 | 93.1 | 96.1 | 3.2 | 0.4 | 0.1 | FASTER |
| chat | pp512 | 8192 | 694.8 | 778.5 | 12.0 | 2.2 | 0.1 | FASTER |
| chat | tg128 | 8192 | 87.9 | 91.4 | 4.0 | 0.3 | 0.2 | FASTER |
| coding | pp512 | 0 | 749.7 | 780.1 | 4.1 | 1.1 | 0.6 | FASTER |
| coding | tg128 | 0 | 22.5 | 26.4 | 17.3 | 0.8 | 0.2 | FASTER |
| coding | pp512 | 8192 | 661.1 | 686.2 | 3.8 | 0.1 | 0.0 | FASTER |
| coding | tg128 | 8192 | 21.5 | 25.2 | 17.0 | 0.5 | 0.3 | FASTER |
| small | pp512 | 0 | 158.1 | 161.0 | 1.9 | 3.0 | 2.2 | noise |
| small | tg128 | 0 | 19.9 | 19.8 | -0.1 | 5.6 | 5.6 | noise |
| small | pp512 | 8192 | 121.8 | 123.3 | 1.3 | 0.3 | 0.4 | FASTER |
| small | tg128 | 8192 | 18.3 | 18.3 | 0.0 | 0.6 | 0.1 | noise |

## mtp-bench end-to-end (decode / prefill t/s; medians over the ABBA runs of each arm)

| run | nmax | prompt tok | prefill A | prefill B | Δ% | decode A | decode B | Δ% | spread A/B | accept A→B | VRAM A→B MiB | verdict (decode) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---|---|
| big-code | 0 | 595 | 517 | 542 | 4.7 | 22.0 | 22.3 | 1.4 | -/- | -→- | 33774→33448 (-326) | FASTER |
| big-code | 0 | 5100 | 941 | 1019 | 8.3 | 21.6 | 21.8 | 0.9 | -/- | -→- | 33774→33448 (-326) | noise |
| big-code | 0 | 20827 | 913 | 984 | 7.9 | 20.4 | 20.6 | 1.0 | -/- | -→- | 33774→33448 (-326) | noise |
| big-code | 2 | 595 | 463 | 463 | 0.0 | 43.6 | 47.7 | 9.4 | -/- | 87.6→98.3 | 34794→35072 (+278) | FASTER |
| big-code | 2 | 5100 | 813 | 868 | 6.9 | 46.2 | 46.8 | 1.3 | -/- | 98.3→98.3 | 34794→35072 (+278) | FASTER |
| big-code | 2 | 20827 | 788 | 847 | 7.5 | 43.6 | 44.3 | 1.6 | -/- | 98.8→98.8 | 34794→35072 (+278) | FASTER |
| chat-code | 2 | 5100 | 901 | 1012 | 12.3 | 136.4 | 132.3 | -3.0 | -/- | 100.0→98.8 | 30598→30748 (+150) | SLOWER |
| chat-sum | 0 | 500 | 611 | 688 | 12.7 | 90.3 | 91.4 | 1.2 | 0.2/0.0 | -→- | 29750→29226 (-524) | FASTER |
| chat-sum | 0 | 4091 | 1176 | 1290 | 9.7 | 88.8 | 89.5 | 0.7 | 1.0/0.1 | -→- | 29876→29352 (-524) | noise |
| chat-sum | 0 | 16376 | 1153 | 1259 | 9.2 | 82.0 | 81.8 | -0.3 | 0.5/1.3 | -→- | 29878→29352 (-526) | noise |
| chat-sum | 2 | 500 | 584 | 643 | 10.2 | 122.6 | 124.3 | 1.4 | 1.4/3.1 | 73.1→71.0 | 30348→30496 (+148) | noise |
| chat-sum | 2 | 4091 | 1104 | 1162 | 5.3 | 121.0 | 125.7 | 3.8 | 1.7/0.2 | 75.0→73.8 | 30600→30748 (+148) | FASTER |
| chat-sum | 2 | 16376 | 1076 | 1174 | 9.1 | 111.7 | 114.5 | 2.6 | 1.6/0.1 | 73.1→72.1 | 30602→30748 (+146) | FASTER |
| coding-code | 0 | 595 | 658 | 702 | 6.7 | 22.3 | 26.0 | 16.6 | 0.0/0.0 | -→- | 23838→23594 (-244) | FASTER |
| coding-code | 0 | 5100 | 847 | 899 | 6.1 | 21.8 | 25.3 | 16.3 | 0.5/0.0 | -→- | 23886→23642 (-244) | FASTER |
| coding-code | 0 | 20827 | 758 | 799 | 5.4 | 20.2 | 23.2 | 15.1 | 0.0/0.4 | -→- | 23886→23642 (-244) | FASTER |
| coding-code | 3 | 595 | 591 | 620 | 4.9 | 48.8 | 61.2 | 25.4 | 0.2/0.2 | 98.4→98.4 | 25200→25304 (+104) | FASTER |
| coding-code | 3 | 5100 | 797 | 829 | 3.9 | 46.8 | 58.5 | 24.9 | 0.0/0.2 | 96.9→96.9 | 25296→25400 (+104) | FASTER |
| coding-code | 3 | 20827 | 715 | 749 | 4.8 | 42.5 | 52.5 | 23.4 | 0.2/0.4 | 94.3→95.9 | 25296→25400 (+104) | FASTER |
| coding-sum | 3 | 500 | 562 | 595 | 5.7 | 39.7 | 43.9 | 10.6 | 0.0/0.0 | 73.1→60.7 | 25196→25300 (+104) | FASTER |
| coding-sum | 3 | 4091 | 805 | 828 | 2.9 | 39.3 | 49.4 | 25.5 | 0.3/0.0 | 74.6→75.8 | 25296→25400 (+104) | FASTER |
| coding-sum | 3 | 16376 | 745 | 783 | 5.0 | 39.6 | 47.3 | 19.6 | 0.5/0.2 | 79.2→83.3 | 25296→25400 (+104) | FASTER |

## ctx-fit at production context (peak VRAM, MiB; gate: B within +300 of A)

| slot | A VRAM | B VRAM | Δ MiB | A prefill / decode | B prefill / decode | ok |
|---|---:|---:|---:|---|---|---|
| chat | 31856 | 31892 | +36 | 795.5 / 93.5 | 834.9 / 93.6 | OK |
| coding | 31170 | 31274 | +104 | 352.9 / 22.6 | 363.7 / 24.2 | OK |

## KLD / PPL gate (B vs the A base logits; wikitext-2 8x2048)

**big** (A PPL 5.0755)
    chunk             PPL               ln(PPL(Q)/PPL(base))          KL Divergence              Δp RMS            Same top p
    Mean ln(PPL(Q)/PPL(base))     :   0.001039 ±   0.000693
    Mean    KLD:   0.000422 ±   0.000068
    Maximum KLD:   0.364199
    99.0%   KLD:   0.001214
    Median  KLD:   0.000132
    Mean    Δp: -0.012 ± 0.007 %
    RMS Δp    :  0.630 ± 0.040 %
    Same top p: 98.827 ± 0.119 %
**chat** (A PPL 4.8099)
    chunk             PPL               ln(PPL(Q)/PPL(base))          KL Divergence              Δp RMS            Same top p
    Mean ln(PPL(Q)/PPL(base))     :  -0.000055 ±   0.000968
    Mean    KLD:   0.002450 ±   0.000282
    Maximum KLD:   2.130365
    99.0%   KLD:   0.030126
    Median  KLD:   0.000459
    Mean    Δp:  0.036 ± 0.019 %
    RMS Δp    :  1.727 ± 0.243 %
    Same top p: 98.436 ± 0.137 %
**coding** (A PPL 5.0688)
    chunk             PPL               ln(PPL(Q)/PPL(base))          KL Divergence              Δp RMS            Same top p
    Mean ln(PPL(Q)/PPL(base))     :  -0.000419 ±   0.000738
    Mean    KLD:   0.000160 ±   0.000092
    Maximum KLD:   0.720673
    99.0%   KLD:   0.000201
    Median  KLD:   0.000002
    Mean    Δp: -0.004 ± 0.002 %
    RMS Δp    :  0.197 ± 0.030 %
    Same top p: 99.866 ± 0.041 %

## B-build flag experiments (B arm only; baseline = the same B build without the flag)

**E2 --spec-draft-sampling probabilistic, coding summary temp 0.6**

| nmax | prompt tok | decode base | decode variant | Δ% | accept base→var | prefill Δ% | VRAM var−base MiB |
|---:|---:|---:|---:|---:|---|---:|---:|
| 3 | 500 | 43.9 | 49.9 | 13.7 | 60.7→73.3 | 0.2 | +0 |
| 3 | 4091 | 49.4 | 46.5 | -5.9 | 75.8→69.0 | 0.2 | +0 |
| 3 | 16376 | 47.3 | 46.0 | -2.9 | 83.3→79.2 | 0.5 | +0 |

**E2 --spec-draft-sampling probabilistic, chat summary temp 0.6**

| nmax | prompt tok | decode base | decode variant | Δ% | accept base→var | prefill Δ% | VRAM var−base MiB |
|---:|---:|---:|---:|---:|---|---:|---:|
| 2 | 500 | 124.3 | 134.7 | 8.4 | 71.0→80.5 | 0.6 | +0 |
| 2 | 4091 | 125.7 | 129.8 | 3.3 | 73.8→79.2 | 1.2 | +0 |
| 2 | 16376 | 114.5 | 117.0 | 2.1 | 72.1→86.4 | -0.2 | +0 |

**E10 GGML_CUDA_GRAPH_OPT=1, coding summary temp 0.6**

| nmax | prompt tok | decode base | decode variant | Δ% | accept base→var | prefill Δ% | VRAM var−base MiB |
|---:|---:|---:|---:|---:|---|---:|---:|
| 3 | 500 | 43.9 | 43.8 | -0.1 | 60.7→60.7 | 0.3 | +0 |
| 3 | 4091 | 49.4 | 49.4 | -0.1 | 75.8→75.8 | -0.0 | +0 |
| 3 | 16376 | 47.3 | 47.3 | -0.1 | 83.3→83.3 | -0.2 | +0 |

**E5 n_max sweep, coding summary temp 0.6 (baseline = n_max 3)**

| nmax | prompt tok | decode base | decode variant | Δ% | accept base→var | prefill Δ% | VRAM var−base MiB |
|---:|---:|---:|---:|---:|---|---:|---:|
| 2 (vs n_max 3) | 500 | 43.9 | 48.8 | 11.2 | 60.7→80.5 | 0.2 | -150 |
| 2 (vs n_max 3) | 4091 | 49.4 | 47.9 | -3.0 | 75.8→81.7 | 0.1 | -150 |
| 2 (vs n_max 3) | 16376 | 47.3 | 45.4 | -4.1 | 83.3→90.0 | 0.9 | -150 |
| 4 (vs n_max 3) | 500 | 43.9 | 49.2 | 12.1 | 60.7→61.3 | -0.2 | +152 |
| 4 (vs n_max 3) | 4091 | 49.4 | 51.5 | 4.3 | 75.8→68.1 | 0.4 | +152 |
| 4 (vs n_max 3) | 16376 | 47.3 | 49.1 | 3.7 | 83.3→75.0 | 0.4 | +152 |
| 5 (vs n_max 3) | 500 | 43.9 | 46.9 | 6.8 | 60.7→53.8 | -0.0 | +300 |
| 5 (vs n_max 3) | 4091 | 49.4 | 49.3 | -0.2 | 75.8→60.0 | -0.5 | +300 |
| 5 (vs n_max 3) | 16376 | 47.3 | 51.1 | 7.9 | 83.3→73.3 | -0.8 | +300 |

**E5 n_max sweep, coding code temp 0.0 (baseline = n_max 3)**

| nmax | prompt tok | decode base | decode variant | Δ% | accept base→var | prefill Δ% | VRAM var−base MiB |
|---:|---:|---:|---:|---:|---|---:|---:|
| 2 (vs n_max 3) | 595 | 61.2 | 55.0 | -10.1 | 98.4→98.3 | 0.0 | -150 |
| 2 (vs n_max 3) | 5100 | 58.5 | 53.2 | -9.0 | 96.9→98.3 | -0.0 | -150 |
| 2 (vs n_max 3) | 20827 | 52.5 | 47.4 | -9.7 | 95.9→98.2 | 0.2 | -150 |
| 4 (vs n_max 3) | 595 | 61.2 | 68.3 | 11.7 | 98.4→96.2 | -1.0 | +152 |
| 4 (vs n_max 3) | 5100 | 58.5 | 65.9 | 12.7 | 96.9→96.7 | -0.3 | +152 |
| 4 (vs n_max 3) | 20827 | 52.5 | 56.8 | 8.2 | 95.9→93.5 | 0.3 | +152 |
| 5 (vs n_max 3) | 595 | 61.2 | 73.4 | 20.0 | 98.4→96.3 | -0.7 | +300 |
| 5 (vs n_max 3) | 5100 | 58.5 | 69.8 | 19.4 | 96.9→94.2 | -0.4 | +300 |
| 5 (vs n_max 3) | 20827 | 52.5 | 61.5 | 17.1 | 95.9→94.1 | -1.0 | +300 |

**E5 --spec-draft-p-min 0.5, coding summary temp 0.6**

| nmax | prompt tok | decode base | decode variant | Δ% | accept base→var | prefill Δ% | VRAM var−base MiB |
|---:|---:|---:|---:|---:|---|---:|---:|
| 3 | 500 | 43.9 | 44.2 | 0.7 | 60.7→70.0 | -0.1 | +0 |
| 3 | 4091 | 49.4 | 49.4 | 0.0 | 75.8→77.3 | -0.1 | +0 |
| 3 | 16376 | 47.3 | 49.3 | 4.1 | 83.3→90.9 | -0.2 | +0 |

**E5 --no-spec-draft-backend-sampling, coding summary temp 0.6**

| nmax | prompt tok | decode base | decode variant | Δ% | accept base→var | prefill Δ% | VRAM var−base MiB |
|---:|---:|---:|---:|---:|---|---:|---:|
| 3 | 500 | 43.9 | 43.4 | -1.1 | 60.7→60.7 | 1.1 | +0 |
| 3 | 4091 | 49.4 | 48.9 | -1.0 | 75.8→75.8 | -0.1 | +0 |
| 3 | 16376 | 47.3 | 46.9 | -1.0 | 83.3→83.3 | 0.5 | +0 |

## E6 multi-turn prompt cache (coding, production flags, ~20k-token document, thinking on)

| turn | A reused / processed | A prefill s | A wall s | A decode t/s | B reused / processed | B prefill s | B wall s | B decode t/s |
|---:|---|---:|---:|---:|---|---:|---:|---:|
| 1 | 0 / 20023 | 27.84 | 39.86 | 33.9 | 0 / 20023 | 27.1 | 36.24 | 42.0 |
| 2 | 17975 / 2258 | 4.2 | 13.97 | 35.6 | 20019 / 191 | 1.03 | 8.97 | 44.7 |
| 3 | 20203 / 134 | 0.88 | 6.54 | 38.0 | 20206 / 111 | 0.99 | 5.57 | 46.5 |
| 4 | 20308 / 101 | 0.9 | 6.3 | 38.9 | 20313 / 76 | 0.71 | 5.16 | 46.0 |
| 5 | 20379 / 115 | 0.9 | 4.81 | 40.6 | 20385 / 88 | 0.71 | 3.86 | 49.4 |
| 6 | 20467 / 127 | 0.85 | 10.34 | 37.1 | 20469 / 103 | 0.95 | 6.98 | 46.7 |


## CUDA-graph control (B arm; llama-bench t/s; NG = GGML_CUDA_DISABLE_GRAPHS=1, one run of 3 reps)

| slot | test | depth | B graphs on (B1 / B2) | B graphs off (NG) | NG vs B mean % |
|---|---|---:|---|---:|---:|
| coding | pp512 | 0 | 782.3 / 777.9 | 791.0 | +1.4 |
| coding | pp512 | 8192 | 686.1 / 686.2 | 691.8 | +0.8 |
| coding | tg128 | 0 | 26.4 / 26.3 | 26.4 | +0.3 |
| coding | tg128 | 8192 | 25.2 / 25.2 | 25.3 | +0.3 |
| chat | pp512 | 0 | 858.8 / 855.2 | 858.9 | +0.2 |
| chat | pp512 | 8192 | 779.0 / 778.0 | 777.7 | -0.1 |
| chat | tg128 | 0 | 96.0 / 96.1 | 97.4 | +1.4 |
| chat | tg128 | 8192 | 91.3 / 91.5 | 92.1 | +0.7 |

Disabling CUDA graphs on B changes nothing measurable (coding tg128 26.4 either way), so the V100 gain is not the graph enablement.

## E6 spec-draft-n-max under real multi-turn traffic (coding, B, thinking on, ~20k-token doc, 6 turns)

| n_max | decode t/s per turn | mean decode t/s | vs n3 | mean wall s (turns 2-6) |
|---:|---|---:|---:|---:|
| 3 | 42.0 / 44.7 / 46.5 / 46.0 / 49.4 / 46.7 | 45.9 | +0.0% | 6.11 |
| 4 | 43.0 / 44.3 / 48.6 / 48.7 / 49.8 / 46.4 | 46.8 | +2.0% | 6.14 |
| 5 | 40.9 / 45.4 / 47.6 / 50.0 / 55.3 / 48.1 | 47.9 | +4.4% | 5.99 |

Turn texts differ run to run (temperature sampling), so a few-percent difference here is within noise.
