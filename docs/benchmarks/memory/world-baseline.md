# Memory eval: answer-and-judge comparison (recorded 2026-11-24, commit b85d9a2c7)

World seed 1, size full, split dev, 320 cases (at most 40 per stratum), token budget 2048, repeats 1.
Answer model `qwen3:8b` (temperature 0.0), judge model `qwen3:8b`, embedding model `qwen3-embedding:0.6b`.

Judge validation: 100.0% of 1050 planted wrong answers marked wrong, 99.6% of 450 planted correct answers marked right.

| System | Stratum | n | Accuracy [95% CI] | Confident-wrong | Abstain P / R | Tokens | Retrieval p50 / p95 ms | Answer p50 ms | Run spread | MDE |
|---|---|---|---|---|---|---|---|---|---|---|
| full_context | abstention | 40 | 100.0% [91%–100%] | 0.0% | 100.0% / 100.0% | 2048 | 14 / 18 | 732 | 0.0% over 1 | 13.7% |
| full_context | cross_tenant | 40 | 0.0% [0%–9%] | 5.0% | 0.0% / – | 2048 | 13 / 14 | 369 | 0.0% over 1 | 13.7% |
| full_context | entity_confusion | 40 | 0.0% [0%–9%] | 5.0% | 0.0% / – | 2048 | 13 / 16 | 706 | 0.0% over 1 | 13.7% |
| full_context | knowledge_update | 40 | 0.0% [0%–9%] | 7.5% | 0.0% / – | 2048 | 14 / 19 | 384 | 0.0% over 1 | 13.7% |
| full_context | long_tenure | 40 | 0.0% [0%–9%] | 5.0% | 0.0% / – | 2048 | 13 / 17 | 697 | 0.0% over 1 | 13.7% |
| full_context | multi_hop | 40 | 0.0% [0%–9%] | 17.5% | 0.0% / – | 2048 | 14 / 26 | 1481 | 0.0% over 1 | 13.7% |
| full_context | point_in_time | 40 | 0.0% [0%–9%] | 0.0% | 0.0% / – | 2048 | 12 / 16 | 369 | 0.0% over 1 | 13.7% |
| full_context | raw_detail | 40 | 0.0% [0%–9%] | 7.5% | 0.0% / – | 2048 | 14 / 16 | 656 | 0.0% over 1 | 13.7% |
| full_context | hard | 240 | 16.7% [12%–22%] | 6.2% | 17.8% / 100.0% | 2048 | 14 / 18 | 607 | 0.0% over 1 | 9.5% |
| full_context | all | 320 | 12.5% [9%–17%] | 5.9% | 13.3% / 100.0% | 2048 | 13 / 17 | 580 | 0.0% over 1 | 7.3% |
| keyword_only | abstention | 40 | 100.0% [91%–100%] | 0.0% | 100.0% / 100.0% | 871 | 2 / 3 | 976 | 0.0% over 1 | 13.7% |
| keyword_only | cross_tenant | 40 | 77.5% [62%–88%] | 15.0% | 0.0% / – | 887 | 1 / 1 | 1121 | 0.0% over 1 | 26.2% |
| keyword_only | entity_confusion | 40 | 82.5% [68%–91%] | 5.0% | 0.0% / – | 1056 | 1 / 2 | 1179 | 0.0% over 1 | 23.8% |
| keyword_only | knowledge_update | 40 | 82.5% [68%–91%] | 17.5% | – / – | 1249 | 3 / 4 | 1481 | 0.0% over 1 | 23.8% |
| keyword_only | long_tenure | 40 | 47.5% [33%–63%] | 52.5% | – / – | 829 | 2 / 3 | 1314 | 0.0% over 1 | 31.3% |
| keyword_only | multi_hop | 40 | 5.0% [1%–17%] | 95.0% | – / – | 1208 | 2 / 14 | 1329 | 0.0% over 1 | 13.7% |
| keyword_only | point_in_time | 40 | 22.5% [12%–38%] | 62.5% | 0.0% / – | 1027 | 4 / 5 | 1400 | 0.0% over 1 | 26.2% |
| keyword_only | raw_detail | 40 | 0.0% [0%–9%] | 0.0% | 0.0% / – | 875 | 1 / 2 | 1012 | 0.0% over 1 | 13.7% |
| keyword_only | hard | 240 | 48.8% [42%–55%] | 30.0% | 44.0% / 100.0% | 1048 | 2 / 5 | 1274 | 0.0% over 1 | 12.8% |
| keyword_only | all | 320 | 52.2% [47%–58%] | 30.9% | 42.6% / 100.0% | 1000 | 2 / 4 | 1261 | 0.0% over 1 | 11.1% |
| raw_verbatim | abstention | 40 | 100.0% [91%–100%] | 0.0% | 100.0% / 100.0% | 2042 | 33 / 37 | 8333 | 0.0% over 1 | 13.7% |
| raw_verbatim | cross_tenant | 40 | 47.5% [33%–63%] | 52.5% | – / – | 2041 | 32 / 36 | 9909 | 0.0% over 1 | 31.3% |
| raw_verbatim | entity_confusion | 40 | 65.0% [50%–78%] | 32.5% | 0.0% / – | 2042 | 34 / 39 | 8546 | 0.0% over 1 | 29.9% |
| raw_verbatim | knowledge_update | 40 | 60.0% [45%–74%] | 40.0% | – / – | 2041 | 33 / 37 | 7498 | 0.0% over 1 | 30.7% |
| raw_verbatim | long_tenure | 40 | 35.0% [22%–50%] | 65.0% | – / – | 2042 | 33 / 35 | 10106 | 0.0% over 1 | 29.9% |
| raw_verbatim | multi_hop | 40 | 12.5% [5%–26%] | 87.5% | – / – | 2041 | 38 / 46 | 9476 | 0.0% over 1 | 20.7% |
| raw_verbatim | point_in_time | 40 | 45.0% [31%–60%] | 55.0% | – / – | 2041 | 36 / 39 | 10518 | 0.0% over 1 | 31.2% |
| raw_verbatim | raw_detail | 40 | 85.0% [71%–93%] | 5.0% | 0.0% / – | 1982 | 32 / 35 | 5768 | 0.0% over 1 | 22.4% |
| raw_verbatim | hard | 240 | 61.3% [55%–67%] | 36.7% | 88.9% / 100.0% | 2032 | 34 / 40 | 8582 | 0.0% over 1 | 12.5% |
| raw_verbatim | all | 320 | 56.2% [51%–62%] | 42.2% | 88.9% / 100.0% | 2034 | 33 / 40 | 8919 | 0.0% over 1 | 11.0% |
| recall | abstention | 40 | 100.0% [91%–100%] | 0.0% | 100.0% / 100.0% | 266 | 421 / 5914 | 470 | 0.0% over 1 | 13.7% |
| recall | cross_tenant | 40 | 82.5% [68%–91%] | 17.5% | – / – | 256 | 508 / 18772 | 594 | 0.0% over 1 | 23.8% |
| recall | entity_confusion | 40 | 80.0% [65%–90%] | 20.0% | – / – | 269 | 802 / 25535 | 626 | 0.0% over 1 | 25.1% |
| recall | knowledge_update | 40 | 80.0% [65%–90%] | 20.0% | – / – | 263 | 454 / 17902 | 659 | 0.0% over 1 | 25.1% |
| recall | long_tenure | 40 | 55.0% [40%–69%] | 40.0% | 0.0% / – | 228 | 427 / 18561 | 611 | 0.0% over 1 | 31.2% |
| recall | multi_hop | 40 | 0.0% [0%–9%] | 97.5% | 0.0% / – | 248 | 785 / 19073 | 633 | 0.0% over 1 | 13.7% |
| recall | point_in_time | 40 | 12.5% [5%–26%] | 37.5% | 0.0% / – | 261 | 346 / 12826 | 549 | 0.0% over 1 | 20.7% |
| recall | raw_detail | 40 | 0.0% [0%–9%] | 0.0% | 0.0% / – | 233 | 426 / 16081 | 442 | 0.0% over 1 | 13.7% |
| recall | hard | 240 | 45.4% [39%–52%] | 29.2% | 39.6% / 100.0% | 257 | 431 / 16863 | 583 | 0.0% over 1 | 12.7% |
| recall | all | 320 | 51.2% [46%–57%] | 29.1% | 38.8% / 100.0% | 253 | 439 / 18050 | 590 | 0.0% over 1 | 11.1% |
| recall_budget | abstention | 40 | 100.0% [91%–100%] | 0.0% | 100.0% / 100.0% | 1038 | 659 / 34424 | 1118 | 0.0% over 1 | 13.7% |
| recall_budget | cross_tenant | 40 | 82.5% [68%–91%] | 15.0% | 0.0% / – | 982 | 462 / 22206 | 1231 | 0.0% over 1 | 23.8% |
| recall_budget | entity_confusion | 40 | 85.0% [71%–93%] | 12.5% | 0.0% / – | 1086 | 1976 / 40554 | 1208 | 0.0% over 1 | 22.4% |
| recall_budget | knowledge_update | 40 | 80.0% [65%–90%] | 20.0% | – / – | 1022 | 696 / 26034 | 1337 | 0.0% over 1 | 25.1% |
| recall_budget | long_tenure | 40 | 60.0% [45%–74%] | 37.5% | 0.0% / – | 858 | 659 / 34107 | 1218 | 0.0% over 1 | 30.7% |
| recall_budget | multi_hop | 40 | 12.5% [5%–26%] | 87.5% | – / – | 969 | 999 / 50126 | 1215 | 0.0% over 1 | 20.7% |
| recall_budget | point_in_time | 40 | 22.5% [12%–38%] | 57.5% | 0.0% / – | 986 | 708 / 39525 | 1231 | 0.0% over 1 | 26.2% |
| recall_budget | raw_detail | 40 | 0.0% [0%–9%] | 0.0% | 0.0% / – | 940 | 659 / 38388 | 1027 | 0.0% over 1 | 13.7% |
| recall_budget | hard | 240 | 50.0% [44%–56%] | 29.6% | 44.9% / 100.0% | 1007 | 714 / 39525 | 1211 | 0.0% over 1 | 12.8% |
| recall_budget | all | 320 | 55.3% [50%–61%] | 28.7% | 44.0% / 100.0% | 985 | 689 / 38311 | 1214 | 0.0% over 1 | 11.0% |

Deciding comparison (pooled hard strata): `recall` vs `keyword_only`: 7 improved, 15 regressed of 240; promote: False (delta -0.033 is not positive). Non-inferior at a 5% margin: False (difference -3.3%, 95% CI -7.1% to +0.5%).

Time: seeding 75.354 s, answering 2324.135 s, total 2603.218 s.
