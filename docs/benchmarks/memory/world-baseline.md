# Memory eval: answer-and-judge comparison (recorded 2026-11-24, commit 159862d9a)

World seed 1, size full, split dev, 320 cases (at most 40 per stratum), token budget 2048, repeats 1.
Answer model `qwen3:8b` (temperature 0.0), judge model `qwen3:8b`, embedding model `qwen3-embedding:0.6b`.

Judge validation: 100.0% of 900 planted wrong answers marked wrong, 99.6% of 450 planted correct answers marked right.

| System | Stratum | n | Accuracy [95% CI] | Confident-wrong | Abstain P / R | Tokens | Retrieval p50 / p95 ms | Answer p50 ms | Run spread | MDE |
|---|---|---|---|---|---|---|---|---|---|---|
| full_context | abstention | 40 | 100.0% [91%–100%] | 0.0% | 100.0% / 100.0% | 2048 | 18 / 26 | 706 | 0.0% over 1 | 13.7% |
| full_context | cross_tenant | 40 | 0.0% [0%–9%] | 5.0% | 0.0% / – | 2048 | 18 / 28 | 1094 | 0.0% over 1 | 13.7% |
| full_context | entity_confusion | 40 | 0.0% [0%–9%] | 5.0% | 0.0% / – | 2048 | 16 / 22 | 1488 | 0.0% over 1 | 13.7% |
| full_context | knowledge_update | 40 | 0.0% [0%–9%] | 7.5% | 0.0% / – | 2048 | 17 / 24 | 1075 | 0.0% over 1 | 13.7% |
| full_context | long_tenure | 40 | 0.0% [0%–9%] | 5.0% | 0.0% / – | 2048 | 16 / 21 | 1668 | 0.0% over 1 | 13.7% |
| full_context | multi_hop | 40 | 0.0% [0%–9%] | 17.5% | 0.0% / – | 2048 | 16 / 44 | 1519 | 0.0% over 1 | 13.7% |
| full_context | point_in_time | 40 | 0.0% [0%–9%] | 0.0% | 0.0% / – | 2048 | 17 / 23 | 723 | 0.0% over 1 | 13.7% |
| full_context | raw_detail | 40 | 0.0% [0%–9%] | 7.5% | 0.0% / – | 2048 | 17 / 23 | 692 | 0.0% over 1 | 13.7% |
| full_context | hard | 240 | 16.7% [12%–22%] | 6.2% | 17.8% / 100.0% | 2048 | 17 / 25 | 871 | 0.0% over 1 | 9.5% |
| full_context | all | 320 | 12.5% [9%–17%] | 5.9% | 13.3% / 100.0% | 2048 | 17 / 26 | 1065 | 0.0% over 1 | 7.3% |
| keyword_only | abstention | 40 | 100.0% [91%–100%] | 0.0% | 100.0% / 100.0% | 871 | 2 / 2 | 882 | 0.0% over 1 | 13.7% |
| keyword_only | cross_tenant | 40 | 77.5% [62%–88%] | 15.0% | 0.0% / – | 887 | 2 / 2 | 1063 | 0.0% over 1 | 26.2% |
| keyword_only | entity_confusion | 40 | 82.5% [68%–91%] | 5.0% | 0.0% / – | 1056 | 2 / 3 | 1150 | 0.0% over 1 | 23.8% |
| keyword_only | knowledge_update | 40 | 82.5% [68%–91%] | 17.5% | – / – | 1249 | 2 / 3 | 1424 | 0.0% over 1 | 23.8% |
| keyword_only | long_tenure | 40 | 47.5% [33%–63%] | 52.5% | – / – | 829 | 2 / 3 | 1195 | 0.0% over 1 | 31.3% |
| keyword_only | multi_hop | 40 | 5.0% [1%–17%] | 95.0% | – / – | 1208 | 2 / 15 | 1304 | 0.0% over 1 | 13.7% |
| keyword_only | point_in_time | 40 | 22.5% [12%–38%] | 62.5% | 0.0% / – | 1027 | 4 / 5 | 1358 | 0.0% over 1 | 26.2% |
| keyword_only | raw_detail | 40 | 0.0% [0%–9%] | 0.0% | 0.0% / – | 875 | 2 / 2 | 927 | 0.0% over 1 | 13.7% |
| keyword_only | hard | 240 | 48.8% [42%–55%] | 30.0% | 44.0% / 100.0% | 1048 | 2 / 5 | 1224 | 0.0% over 1 | 12.8% |
| keyword_only | all | 320 | 52.2% [47%–58%] | 30.9% | 42.6% / 100.0% | 1000 | 2 / 4 | 1196 | 0.0% over 1 | 11.1% |
| raw_verbatim | abstention | 40 | 100.0% [91%–100%] | 0.0% | 100.0% / 100.0% | 2043 | 50 / 113 | 7625 | 0.0% over 1 | 13.7% |
| raw_verbatim | cross_tenant | 40 | 50.0% [35%–65%] | 50.0% | – / – | 2041 | 93 / 201 | 7223 | 0.0% over 1 | 31.3% |
| raw_verbatim | entity_confusion | 40 | 65.0% [50%–78%] | 32.5% | 0.0% / – | 2042 | 70 / 544 | 8853 | 0.0% over 1 | 29.9% |
| raw_verbatim | knowledge_update | 40 | 60.0% [45%–74%] | 40.0% | – / – | 2041 | 55 / 65 | 5208 | 0.0% over 1 | 30.7% |
| raw_verbatim | long_tenure | 40 | 35.0% [22%–50%] | 65.0% | – / – | 2042 | 122 / 198 | 12297 | 0.0% over 1 | 29.9% |
| raw_verbatim | multi_hop | 40 | 12.5% [5%–26%] | 87.5% | – / – | 2041 | 50 / 659 | 5796 | 0.0% over 1 | 20.7% |
| raw_verbatim | point_in_time | 40 | 45.0% [31%–60%] | 55.0% | – / – | 2041 | 66 / 74 | 7144 | 0.0% over 1 | 31.2% |
| raw_verbatim | raw_detail | 40 | 85.0% [71%–93%] | 5.0% | 0.0% / – | 1981 | 54 / 258 | 10308 | 0.0% over 1 | 22.4% |
| raw_verbatim | hard | 240 | 61.3% [55%–67%] | 36.7% | 88.9% / 100.0% | 2032 | 59 / 507 | 7933 | 0.0% over 1 | 12.5% |
| raw_verbatim | all | 320 | 56.6% [51%–62%] | 41.9% | 88.9% / 100.0% | 2034 | 65 / 465 | 8684 | 0.0% over 1 | 11.0% |
| recall | abstention | 40 | 100.0% [91%–100%] | 0.0% | 100.0% / 100.0% | 266 | 237 / 17648 | 426 | 0.0% over 1 | 13.7% |
| recall | cross_tenant | 40 | 82.5% [68%–91%] | 17.5% | – / – | 256 | 241 / 16494 | 529 | 0.0% over 1 | 23.8% |
| recall | entity_confusion | 40 | 80.0% [65%–90%] | 20.0% | – / – | 269 | 249 / 24085 | 556 | 0.0% over 1 | 25.1% |
| recall | knowledge_update | 40 | 80.0% [65%–90%] | 20.0% | – / – | 263 | 242 / 253 | 630 | 0.0% over 1 | 25.1% |
| recall | long_tenure | 40 | 55.0% [40%–69%] | 40.0% | 0.0% / – | 228 | 239 / 26365 | 544 | 0.0% over 1 | 31.2% |
| recall | multi_hop | 40 | 0.0% [0%–9%] | 97.5% | 0.0% / – | 248 | 244 / 21216 | 559 | 0.0% over 1 | 13.7% |
| recall | point_in_time | 40 | 12.5% [5%–26%] | 37.5% | 0.0% / – | 261 | 247 / 14721 | 494 | 0.0% over 1 | 20.7% |
| recall | raw_detail | 40 | 0.0% [0%–9%] | 0.0% | 0.0% / – | 233 | 232 / 12652 | 396 | 0.0% over 1 | 13.7% |
| recall | hard | 240 | 45.4% [39%–52%] | 29.2% | 39.6% / 100.0% | 257 | 243 / 17536 | 521 | 0.0% over 1 | 12.7% |
| recall | all | 320 | 51.2% [46%–57%] | 29.1% | 38.8% / 100.0% | 253 | 242 / 17536 | 536 | 0.0% over 1 | 11.1% |

Deciding comparison (pooled hard strata): `recall` vs `full_context`: 69 improved, 0 regressed of 240; promote: True (delta +0.287, p=0.000, no stratum regression). Non-inferior at a 5% margin: True (difference +28.7%, 95% CI +23.0% to +34.5%).

Time: seeding 196.747 s, answering 1637.278 s, total 2025.997 s.

## Run-to-run spread (3 repeats)

Same parameters, with `--limit-per-stratum 10 --repeats 3`: 80 cases × 4
systems × 3 runs. Repeat 0 was reused from the main run's progress file. No
errors. Time 1,129 s (seeding 76 s, answering 863 s). The embedding cache
served all 12,339 lookups.

Range of per-run accuracy (max − min) across the three runs:

| System | Strata with any spread | Pooled hard | Pooled all |
|--------|------------------------|-------------|------------|
| recall | none (0 in every stratum) | 0.0 | 0.0 |
| keyword_only | none | 0.0 | 0.0 |
| raw_verbatim | raw_detail 80–90% (10 points, one case of 10) | 1.7 points | 1.2 points |
| full_context | none | 0.0 | 0.0 |

At temperature 0 the answer model is almost deterministic. Over these runs
the noise floor is at most one case per 10-case stratum, and only for the
system that carries the longest contexts.
