# Qwen3.8-27B V100 benchmark

- Date: Sat Aug 15 01:17:01 AM UTC 2026
- Driver: 580.173.02
- Bench params: -p 512 -n 128 -r 3 -ngl 99 ; depths: 0 8192

### Q6_K  single V100

| model                          |       size |     params | backend    | ngl |     sm |            test |                  t/s |
| ------------------------------ | ---------: | ---------: | ---------- | --: | -----: | --------------: | -------------------: |
| qwen35 27B Q6_K                |  21.30 GiB |    27.32 B | CUDA       |  99 |   none |           pp512 |        754.73 ± 1.22 |
| qwen35 27B Q6_K                |  21.30 GiB |    27.32 B | CUDA       |  99 |   none |           tg128 |         22.76 ± 0.05 |

build: 4f31eedb0 (9850)
| model                          |       size |     params | backend    | ngl |     sm |            test |                  t/s |
| ------------------------------ | ---------: | ---------: | ---------- | --: | -----: | --------------: | -------------------: |
| qwen35 27B Q6_K                |  21.30 GiB |    27.32 B | CUDA       |  99 |   none |   pp512 @ d8192 |        664.70 ± 1.66 |
| qwen35 27B Q6_K                |  21.30 GiB |    27.32 B | CUDA       |  99 |   none |   tg128 @ d8192 |         21.74 ± 0.02 |

build: 4f31eedb0 (9850)

### Q6_K  dual V100 (layer)

| model                          |       size |     params | backend    | ngl |            test |                  t/s |
| ------------------------------ | ---------: | ---------: | ---------- | --: | --------------: | -------------------: |
| qwen35 27B Q6_K                |  21.30 GiB |    27.32 B | CUDA       |  99 |           pp512 |        770.09 ± 1.81 |
| qwen35 27B Q6_K                |  21.30 GiB |    27.32 B | CUDA       |  99 |           tg128 |         25.04 ± 0.07 |

build: 4f31eedb0 (9850)
| model                          |       size |     params | backend    | ngl |            test |                  t/s |
| ------------------------------ | ---------: | ---------: | ---------- | --: | --------------: | -------------------: |
| qwen35 27B Q6_K                |  21.30 GiB |    27.32 B | CUDA       |  99 |   pp512 @ d8192 |        673.56 ± 0.88 |
| qwen35 27B Q6_K                |  21.30 GiB |    27.32 B | CUDA       |  99 |   tg128 @ d8192 |         24.26 ± 0.02 |

build: 4f31eedb0 (9850)

### Q6_K  dual V100 (row)

| model                          |       size |     params | backend    | ngl |     sm |            test |                  t/s |
| ------------------------------ | ---------: | ---------: | ---------- | --: | -----: | --------------: | -------------------: |
| qwen35 27B Q6_K                |  21.30 GiB |    27.32 B | CUDA       |  99 |    row |           pp512 |        201.99 ± 0.16 |
| qwen35 27B Q6_K                |  21.30 GiB |    27.32 B | CUDA       |  99 |    row |           tg128 |         21.41 ± 0.01 |

build: 4f31eedb0 (9850)
| model                          |       size |     params | backend    | ngl |     sm |            test |                  t/s |
| ------------------------------ | ---------: | ---------: | ---------- | --: | -----: | --------------: | -------------------: |
| qwen35 27B Q6_K                |  21.30 GiB |    27.32 B | CUDA       |  99 |    row |   pp512 @ d8192 |        194.21 ± 0.05 |
| qwen35 27B Q6_K                |  21.30 GiB |    27.32 B | CUDA       |  99 |    row |   tg128 @ d8192 |         20.62 ± 0.01 |

build: 4f31eedb0 (9850)

### UD-Q6_K_XL  single V100

| model                          |       size |     params | backend    | ngl |     sm |            test |                  t/s |
| ------------------------------ | ---------: | ---------: | ---------- | --: | -----: | --------------: | -------------------: |
| qwen35 27B Q4_K - Small        |  24.13 GiB |    27.32 B | CUDA       |  99 |   none |           pp512 |        778.31 ± 3.28 |
| qwen35 27B Q4_K - Small        |  24.13 GiB |    27.32 B | CUDA       |  99 |   none |           tg128 |         22.95 ± 0.03 |

build: 4f31eedb0 (9850)
| model                          |       size |     params | backend    | ngl |     sm |            test |                  t/s |
| ------------------------------ | ---------: | ---------: | ---------- | --: | -----: | --------------: | -------------------: |
| qwen35 27B Q4_K - Small        |  24.13 GiB |    27.32 B | CUDA       |  99 |   none |   pp512 @ d8192 |        686.34 ± 1.51 |
| qwen35 27B Q4_K - Small        |  24.13 GiB |    27.32 B | CUDA       |  99 |   none |   tg128 @ d8192 |         22.03 ± 0.01 |

build: 4f31eedb0 (9850)

### UD-Q6_K_XL  dual V100 (layer)

| model                          |       size |     params | backend    | ngl |            test |                  t/s |
| ------------------------------ | ---------: | ---------: | ---------- | --: | --------------: | -------------------: |
| qwen35 27B Q4_K - Small        |  24.13 GiB |    27.32 B | CUDA       |  99 |           pp512 |        801.12 ± 3.74 |
| qwen35 27B Q4_K - Small        |  24.13 GiB |    27.32 B | CUDA       |  99 |           tg128 |         24.31 ± 0.00 |

build: 4f31eedb0 (9850)
| model                          |       size |     params | backend    | ngl |            test |                  t/s |
| ------------------------------ | ---------: | ---------: | ---------- | --: | --------------: | -------------------: |
| qwen35 27B Q4_K - Small        |  24.13 GiB |    27.32 B | CUDA       |  99 |   pp512 @ d8192 |        700.06 ± 2.40 |
| qwen35 27B Q4_K - Small        |  24.13 GiB |    27.32 B | CUDA       |  99 |   tg128 @ d8192 |         23.36 ± 0.01 |

build: 4f31eedb0 (9850)

### UD-Q6_K_XL  dual V100 (row)

| model                          |       size |     params | backend    | ngl |     sm |            test |                  t/s |
| ------------------------------ | ---------: | ---------: | ---------- | --: | -----: | --------------: | -------------------: |
| qwen35 27B Q4_K - Small        |  24.13 GiB |    27.32 B | CUDA       |  99 |    row |           pp512 |        198.08 ± 0.16 |
| qwen35 27B Q4_K - Small        |  24.13 GiB |    27.32 B | CUDA       |  99 |    row |           tg128 |         21.01 ± 0.00 |

build: 4f31eedb0 (9850)
| model                          |       size |     params | backend    | ngl |     sm |            test |                  t/s |
| ------------------------------ | ---------: | ---------: | ---------- | --: | -----: | --------------: | -------------------: |
| qwen35 27B Q4_K - Small        |  24.13 GiB |    27.32 B | CUDA       |  99 |    row |   pp512 @ d8192 |        190.84 ± 0.19 |
| qwen35 27B Q4_K - Small        |  24.13 GiB |    27.32 B | CUDA       |  99 |    row |   tg128 @ d8192 |         20.28 ± 0.01 |

build: 4f31eedb0 (9850)
