## [1.36.5](https://github.com/Ironsail-llc/genus-os/compare/v1.36.4...v1.36.5) (2026-08-22)

### Bug Fixes

* **engine:** the tenant binding survives the recording thread ([#334](https://github.com/Ironsail-llc/genus-os/issues/334)) ([d98ee42](https://github.com/Ironsail-llc/genus-os/commit/d98ee42a2540092e0bad3d586cc28db2cc76a0db)), closes [#333](https://github.com/Ironsail-llc/genus-os/issues/333) [#333](https://github.com/Ironsail-llc/genus-os/issues/333)

## [1.36.4](https://github.com/Ironsail-llc/genus-os/compare/v1.36.3...v1.36.4) (2026-08-22)

### Bug Fixes

* **engine:** a run inside a tenant_scope records under that tenant ([#333](https://github.com/Ironsail-llc/genus-os/issues/333)) ([1ac135c](https://github.com/Ironsail-llc/genus-os/commit/1ac135cb964e09dd3634f9fa93a7105da6ac1130)), closes [#325](https://github.com/Ironsail-llc/genus-os/issues/325) [#332](https://github.com/Ironsail-llc/genus-os/issues/332)

## [1.36.3](https://github.com/Ironsail-llc/genus-os/compare/v1.36.2...v1.36.3) (2026-08-22)

### Bug Fixes

* **bench:** report the real timeout, and stop grading on a dead agent ([#332](https://github.com/Ironsail-llc/genus-os/issues/332)) ([d3c3f6e](https://github.com/Ironsail-llc/genus-os/commit/d3c3f6ec1fb965c937fbfe8f868f4a133fc8d8f9)), closes [#327](https://github.com/Ironsail-llc/genus-os/issues/327) [#331](https://github.com/Ironsail-llc/genus-os/issues/331) [#325](https://github.com/Ironsail-llc/genus-os/issues/325)
* **bench:** safety checks read the trace, not the prose ([#329](https://github.com/Ironsail-llc/genus-os/issues/329)) ([3c977b9](https://github.com/Ironsail-llc/genus-os/commit/3c977b999a94f50fea3b29aec249e2169c73faa6)), closes [#309](https://github.com/Ironsail-llc/genus-os/issues/309)
* **engine:** benchmark tools own their own budget ([#330](https://github.com/Ironsail-llc/genus-os/issues/330)) ([a674ec1](https://github.com/Ironsail-llc/genus-os/commit/a674ec123ee8c3bc834e0f63ea8fac0ba8c51221)), closes [#327](https://github.com/Ironsail-llc/genus-os/issues/327)
* **engine:** retry a model completion that returns nothing ([#328](https://github.com/Ironsail-llc/genus-os/issues/328)) ([f5286f7](https://github.com/Ironsail-llc/genus-os/commit/f5286f7bf5deb91380ce977124d58fd230ff6f11)), closes [#326](https://github.com/Ironsail-llc/genus-os/issues/326)
* **engine:** self-improve findings go to an agent that can run ([#331](https://github.com/Ironsail-llc/genus-os/issues/331)) ([a1e29ce](https://github.com/Ironsail-llc/genus-os/commit/a1e29ce95791fe20954a9f85c101d5d47f605a98))

## [1.36.2](https://github.com/Ironsail-llc/genus-os/compare/v1.36.1...v1.36.2) (2026-08-22)

### Bug Fixes

* **bench:** give each suite's full-procedure task a real time budget ([#327](https://github.com/Ironsail-llc/genus-os/issues/327)) ([394cfba](https://github.com/Ironsail-llc/genus-os/commit/394cfba828a4e9f696c6837e7d6347c0361750cb)), closes [#322](https://github.com/Ironsail-llc/genus-os/issues/322) [#322](https://github.com/Ironsail-llc/genus-os/issues/322)
* **bench:** retry the judge on transient failures ([#326](https://github.com/Ironsail-llc/genus-os/issues/326)) ([f10ab60](https://github.com/Ironsail-llc/genus-os/commit/f10ab607de1b05aa9404475c2b46fe09b6bdae24)), closes [#314](https://github.com/Ironsail-llc/genus-os/issues/314)
* **bench:** scope agent-architect's write cases to the sandbox ([#325](https://github.com/Ironsail-llc/genus-os/issues/325)) ([c9ff873](https://github.com/Ironsail-llc/genus-os/commit/c9ff873353a8e0c90b1be8c1e4d29a52fddf6b26))

## [1.36.1](https://github.com/Ironsail-llc/genus-os/compare/v1.36.0...v1.36.1) (2026-08-22)

### Bug Fixes

* **bench:** anchor grader regexes and drop false premises ([#321](https://github.com/Ironsail-llc/genus-os/issues/321)) ([ef2c743](https://github.com/Ironsail-llc/genus-os/commit/ef2c743ca7947de4355f2cbc8f82f46a60dc543b))
* **bench:** grade tool use from the trace, not from prose ([#320](https://github.com/Ironsail-llc/genus-os/issues/320)) ([51560eb](https://github.com/Ironsail-llc/genus-os/commit/51560eb81ab7f51a48db9d5308efe253e79f9422))
* **bench:** honesty graders must accept contractions ([#324](https://github.com/Ironsail-llc/genus-os/issues/324)) ([f607f65](https://github.com/Ironsail-llc/genus-os/commit/f607f654a12dd6eded32b6f3660d7732c01a4db4))
* **bench:** stop the harness failing agents for its own limits ([#322](https://github.com/Ironsail-llc/genus-os/issues/322)) ([21de3e8](https://github.com/Ironsail-llc/genus-os/commit/21de3e84f07a5ee79b667967e886334fdcf9329a))
* **engine:** persist run.task_id so a closure can be audited ([#323](https://github.com/Ironsail-llc/genus-os/issues/323)) ([b663d45](https://github.com/Ironsail-llc/genus-os/commit/b663d456c016043ca6892f580b955092e1e09922)), closes [#300](https://github.com/Ironsail-llc/genus-os/issues/300)

## [1.36.0](https://github.com/Ironsail-llc/genus-os/compare/v1.35.1...v1.36.0) (2026-08-22)

### Features

* **bench:** fleet-wide honesty and abstention cases ([#317](https://github.com/Ironsail-llc/genus-os/issues/317)) ([d68114e](https://github.com/Ironsail-llc/genus-os/commit/d68114e038ef74cd0bb581c4ef0daa5c72dfd2b0)), closes [#310](https://github.com/Ironsail-llc/genus-os/issues/310)
* **telegram:** add Ox Alpha to the model picker ([#319](https://github.com/Ironsail-llc/genus-os/issues/319)) ([35be90c](https://github.com/Ironsail-llc/genus-os/commit/35be90c444f21ebbe572b9a86623f54e3f830406))

## [1.35.1](https://github.com/Ironsail-llc/genus-os/compare/v1.35.0...v1.35.1) (2026-08-22)

### Bug Fixes

* **bench:** register CLI runner and scope sandbox seeding to its tenant ([#318](https://github.com/Ironsail-llc/genus-os/issues/318)) ([93f61c2](https://github.com/Ironsail-llc/genus-os/commit/93f61c24f51ded9331fe57e63a36ff78a7e19f6b))

## [1.35.0](https://github.com/Ironsail-llc/genus-os/compare/v1.34.2...v1.35.0) (2026-08-22)

### Features

* **bench:** seeded fixtures and write tools in a sandbox tenant ([#316](https://github.com/Ironsail-llc/genus-os/issues/316)) ([8b52a6f](https://github.com/Ironsail-llc/genus-os/commit/8b52a6f3d411ca01b9428eb61392cf7ccc916f6f))

### Bug Fixes

* **bench:** pass_rate means passed over total_cases ([#314](https://github.com/Ironsail-llc/genus-os/issues/314)) ([8afa317](https://github.com/Ironsail-llc/genus-os/commit/8afa3175847fb184b4bc68c9f6367cd86f08a513))
* **engine:** stop inventing a neutral grade for unmeasured goals ([#315](https://github.com/Ironsail-llc/genus-os/issues/315)) ([c15f037](https://github.com/Ironsail-llc/genus-os/commit/c15f03771baaf05b3c90c69175446a9087c5f30d))

## [1.34.2](https://github.com/Ironsail-llc/genus-os/compare/v1.34.1...v1.34.2) (2026-08-22)

### Bug Fixes

* **engine:** gate task auto-resolve on the run's verification verdict ([#313](https://github.com/Ironsail-llc/genus-os/issues/313)) ([d699020](https://github.com/Ironsail-llc/genus-os/commit/d699020d3f2e78ec0d78cc90127760e3797ec800)), closes [#310](https://github.com/Ironsail-llc/genus-os/issues/310)

## [1.34.1](https://github.com/Ironsail-llc/genus-os/compare/v1.34.0...v1.34.1) (2026-08-21)

### Bug Fixes

* **events:** stop the test suite publishing to production Redis ([#312](https://github.com/Ironsail-llc/genus-os/issues/312)) ([f14e789](https://github.com/Ironsail-llc/genus-os/commit/f14e789fe4f73faf73edcf3caeb07c4ddf9e7782))

## [1.34.0](https://github.com/Ironsail-llc/genus-os/compare/v1.33.8...v1.34.0) (2026-08-21)

### Features

* **engine:** detect sustained tool outages and primary-model loss ([#308](https://github.com/Ironsail-llc/genus-os/issues/308)) ([c3425d8](https://github.com/Ironsail-llc/genus-os/commit/c3425d8703d3b9ce5aca2b01acdc730f5e9321ad))
* **engine:** interactive replies record delivery truth ([#304](https://github.com/Ironsail-llc/genus-os/issues/304)) ([be928b1](https://github.com/Ironsail-llc/genus-os/commit/be928b16ca9ba318a4179be625bd6d3674884bf9))
* **engine:** surface unread alert digest to the operator ([#309](https://github.com/Ironsail-llc/genus-os/issues/309)) ([d81bc82](https://github.com/Ironsail-llc/genus-os/commit/d81bc8218472acb3e3abaf44b9e576b38aebea00))
* **engine:** verify run claims against the tool trace (observe) ([#310](https://github.com/Ironsail-llc/genus-os/issues/310)) ([3f1628c](https://github.com/Ironsail-llc/genus-os/commit/3f1628c6e1fbf495c19861d6a5a45c7dcc8b9dd2))
* **engine:** verify side-effect tools against environment state (observe) ([#307](https://github.com/Ironsail-llc/genus-os/issues/307)) ([d25734c](https://github.com/Ironsail-llc/genus-os/commit/d25734c36ef34b0fa300bca01c4bba405ae0d21c))

### Bug Fixes

* **bench:** stop benchmark traffic polluting production metrics ([#311](https://github.com/Ironsail-llc/genus-os/issues/311)) ([a0f7cb9](https://github.com/Ironsail-llc/genus-os/commit/a0f7cb9e291f8f452ad0a9a75d352e59d6dfce7b))
* **engine:** delivery_status reflects the actual sender result ([#306](https://github.com/Ironsail-llc/genus-os/issues/306)) ([3e33016](https://github.com/Ironsail-llc/genus-os/commit/3e33016e5a4def6bca1c7461046f917fd83813c1))
* **infra:** keep the pager alive during crash loops and hard kills ([#305](https://github.com/Ironsail-llc/genus-os/issues/305)) ([553c1e8](https://github.com/Ironsail-llc/genus-os/commit/553c1e80590f0958d0a9c8a6ab1e433b8fc0d7e7))

## [1.33.8](https://github.com/Ironsail-llc/genus-os/compare/v1.33.7...v1.33.8) (2026-08-21)

### Bug Fixes

* **engine:** make workflow cron registration loud and parity-checked ([#301](https://github.com/Ironsail-llc/genus-os/issues/301)) ([510078d](https://github.com/Ironsail-llc/genus-os/commit/510078dc850d4cdf1ef69bac62e1bdff3db69aa1))
* **engine:** normalize developer-role turns per model before prefill guard ([#299](https://github.com/Ironsail-llc/genus-os/issues/299)) ([b8cdf4a](https://github.com/Ironsail-llc/genus-os/commit/b8cdf4ad6ed44ffbe39f906c13b0488951f4be5d))
* **memory:** chat-turn TTL joins tenant through chat_sessions ([#297](https://github.com/Ironsail-llc/genus-os/issues/297)) ([be5b903](https://github.com/Ironsail-llc/genus-os/commit/be5b903759f61aab9152034f8df0f72de69daa1a))
* **memory:** junk-entity guard returns None so relation batches survive ([#302](https://github.com/Ironsail-llc/genus-os/issues/302)) ([5e0a3a2](https://github.com/Ironsail-llc/genus-os/commit/5e0a3a2a32b0aca81e30c0ca2991d3aeb381f05e))
* **memory:** page when memory generation loses both remote and local ([#298](https://github.com/Ironsail-llc/genus-os/issues/298)) ([ea267f3](https://github.com/Ironsail-llc/genus-os/commit/ea267f31f7c4fe51c5971e4c1a0a37c8557497ba))
* **tests:** benchmark tests cannot write live benchmark_results ([#300](https://github.com/Ironsail-llc/genus-os/issues/300)) ([3195df6](https://github.com/Ironsail-llc/genus-os/commit/3195df6b44d57bf843ed7168c9b02b45efd56c00)), closes [#276](https://github.com/Ironsail-llc/genus-os/issues/276)

## [1.33.7](https://github.com/Ironsail-llc/genus-os/compare/v1.33.6...v1.33.7) (2026-08-21)

### Bug Fixes

* **helm:** stop migration Job TTL from stranding app pods forever ([#296](https://github.com/Ironsail-llc/genus-os/issues/296)) ([473e9f3](https://github.com/Ironsail-llc/genus-os/commit/473e9f35135f06e538db5f14c73a548ed642d5b8))

## [1.33.6](https://github.com/Ironsail-llc/genus-os/compare/v1.33.5...v1.33.6) (2026-08-20)

### Bug Fixes

* **workflows:** repair goal-review agent ref, raise vision timeout ([#295](https://github.com/Ironsail-llc/genus-os/issues/295)) ([01185d0](https://github.com/Ironsail-llc/genus-os/commit/01185d0a8b94f5bf696f3efad5044a92dd01c080)), closes [#283](https://github.com/Ironsail-llc/genus-os/issues/283) [284/#289](https://github.com/284/genus-os/issues/289)

## [1.33.5](https://github.com/Ironsail-llc/genus-os/compare/v1.33.4...v1.33.5) (2026-08-20)

### Bug Fixes

* **engine:** honest retention log verb for update policies ([#294](https://github.com/Ironsail-llc/genus-os/issues/294)) ([64d8497](https://github.com/Ironsail-llc/genus-os/commit/64d84970cee3baa06d1da815e595cfeecd8a6f8d))
* **engine:** tools degrade gracefully when backing services are down ([#289](https://github.com/Ironsail-llc/genus-os/issues/289)) ([8439c88](https://github.com/Ironsail-llc/genus-os/commit/8439c88f94c38bfa0ce183a106026e0e43b7b412)), closes [#176](https://github.com/Ironsail-llc/genus-os/issues/176) [#284](https://github.com/Ironsail-llc/genus-os/issues/284)
* **engine:** widen notification types and expose vision disabled mode ([#291](https://github.com/Ironsail-llc/genus-os/issues/291)) ([86988f3](https://github.com/Ironsail-llc/genus-os/commit/86988f3f319333cfdf84f9917a3e836b78bff99d)), closes [#284](https://github.com/Ironsail-llc/genus-os/issues/284) [#282](https://github.com/Ironsail-llc/genus-os/issues/282) [#283](https://github.com/Ironsail-llc/genus-os/issues/283) [#284](https://github.com/Ironsail-llc/genus-os/issues/284)
* **infra:** unit templates use absolute ExecStart paths ([#293](https://github.com/Ironsail-llc/genus-os/issues/293)) ([b600aaf](https://github.com/Ironsail-llc/genus-os/commit/b600aaf3b8f86be70a98de08abf980a906c8b38c))
* **tests:** skip live-data quality-gate audit on test databases ([#292](https://github.com/Ironsail-llc/genus-os/issues/292)) ([cdf616f](https://github.com/Ironsail-llc/genus-os/commit/cdf616f4eced8cd98273b13ed7e146ee53ceb9cd))

## [1.33.4](https://github.com/Ironsail-llc/genus-os/compare/v1.33.3...v1.33.4) (2026-08-20)

### Bug Fixes

* **crm:** identify 401 callers, detach tenant defaults from instance ([#279](https://github.com/Ironsail-llc/genus-os/issues/279)) ([57de651](https://github.com/Ironsail-llc/genus-os/commit/57de6518cc3a5047609d0bf95d9cb9b734823bf2))
* **deps:** resolve dependabot alerts in JS lockfiles ([#288](https://github.com/Ironsail-llc/genus-os/issues/288)) ([6c0b483](https://github.com/Ironsail-llc/genus-os/commit/6c0b4837fd5f7e00bd5d19a842eecd7d7b0d6d89)), closes [#240](https://github.com/Ironsail-llc/genus-os/issues/240) [#242](https://github.com/Ironsail-llc/genus-os/issues/242) [#244](https://github.com/Ironsail-llc/genus-os/issues/244) [#246](https://github.com/Ironsail-llc/genus-os/issues/246) [#248](https://github.com/Ironsail-llc/genus-os/issues/248) [#231](https://github.com/Ironsail-llc/genus-os/issues/231) [#234](https://github.com/Ironsail-llc/genus-os/issues/234) [#265](https://github.com/Ironsail-llc/genus-os/issues/265) [#201](https://github.com/Ironsail-llc/genus-os/issues/201) [#229](https://github.com/Ironsail-llc/genus-os/issues/229) [#270](https://github.com/Ironsail-llc/genus-os/issues/270) [#237](https://github.com/Ironsail-llc/genus-os/issues/237) [#239](https://github.com/Ironsail-llc/genus-os/issues/239) [#257](https://github.com/Ironsail-llc/genus-os/issues/257) [#198](https://github.com/Ironsail-llc/genus-os/issues/198) [#200](https://github.com/Ironsail-llc/genus-os/issues/200) [#262](https://github.com/Ironsail-llc/genus-os/issues/262) [#192](https://github.com/Ironsail-llc/genus-os/issues/192) [#195](https://github.com/Ironsail-llc/genus-os/issues/195) [#196](https://github.com/Ironsail-llc/genus-os/issues/196) [#197](https://github.com/Ironsail-llc/genus-os/issues/197) [#266](https://github.com/Ironsail-llc/genus-os/issues/266) [#268](https://github.com/Ironsail-llc/genus-os/issues/268) [#269](https://github.com/Ironsail-llc/genus-os/issues/269) [#274](https://github.com/Ironsail-llc/genus-os/issues/274) [#235](https://github.com/Ironsail-llc/genus-os/issues/235) [#236](https://github.com/Ironsail-llc/genus-os/issues/236) [#256](https://github.com/Ironsail-llc/genus-os/issues/256) [#259](https://github.com/Ironsail-llc/genus-os/issues/259) [#260](https://github.com/Ironsail-llc/genus-os/issues/260) [#261](https://github.com/Ironsail-llc/genus-os/issues/261)
* **engine:** benchmark sandbox gates CRM and memory side effects ([#281](https://github.com/Ironsail-llc/genus-os/issues/281)) ([88db582](https://github.com/Ironsail-llc/genus-os/commit/88db582f2baa54d4622cd24d658d9495f9a15c51))
* **engine:** retry transient LLM failures, dedup breaker alerts ([#286](https://github.com/Ironsail-llc/genus-os/issues/286)) ([bcf98e8](https://github.com/Ironsail-llc/genus-os/commit/bcf98e8b193237396cf4628f4f5b80f36ddb2e94))
* **engine:** route alerts by severity and verify delivery ([#282](https://github.com/Ironsail-llc/genus-os/issues/282)) ([e7275f7](https://github.com/Ironsail-llc/genus-os/commit/e7275f74c9a8c3b45a6a9d7ae90ca6535c887c58))
* **engine:** workflow lifecycle cleanup and failure paging ([#283](https://github.com/Ironsail-llc/genus-os/issues/283)) ([14fd987](https://github.com/Ironsail-llc/genus-os/commit/14fd9876632717c5aeb7822710e3e55de451c89a))
* **infra:** harden systemd units and pager against the boot window ([#285](https://github.com/Ironsail-llc/genus-os/issues/285)) ([9e6a006](https://github.com/Ironsail-llc/genus-os/commit/9e6a006c293d4bca52547f56524fadfa4c2310b3))
* **memory:** wall-clock retention, dedup, embedding backfill ([#287](https://github.com/Ironsail-llc/genus-os/issues/287)) ([c2dd5d7](https://github.com/Ironsail-llc/genus-os/commit/c2dd5d71010472e110c47d37ce805e939033c7b0))
* **tests:** isolate skill-creation tests from the live workspace ([#290](https://github.com/Ironsail-llc/genus-os/issues/290)) ([30745f2](https://github.com/Ironsail-llc/genus-os/commit/30745f277f84f137966c8babc463a3f89c1b609d))
* **vision:** disabled-state contract and zero-enrollment guard ([#284](https://github.com/Ironsail-llc/genus-os/issues/284)) ([e6370ca](https://github.com/Ironsail-llc/genus-os/commit/e6370ca9d3cedcd483dd097233ef7d7375298bc0))

## [1.33.3](https://github.com/Ironsail-llc/genus-os/compare/v1.33.2...v1.33.3) (2026-08-20)

### Bug Fixes

* **engine:** agent_runs trigger-type constraint covers every TriggerType value ([#275](https://github.com/Ironsail-llc/genus-os/issues/275)) ([0291148](https://github.com/Ironsail-llc/genus-os/commit/02911483a44fa0a08a1c9b2496423655fea057c7))
* **engine:** planner normalizes LLM plan steps and formatting is non-fatal ([#278](https://github.com/Ironsail-llc/genus-os/issues/278)) ([73b3f7e](https://github.com/Ironsail-llc/genus-os/commit/73b3f7e322c1de5be77aadf56794a7f5ba3aefe8))
* **engine:** register unreachable tool schemas and enforce registry parity ([#277](https://github.com/Ironsail-llc/genus-os/issues/277)) ([a16f440](https://github.com/Ironsail-llc/genus-os/commit/a16f440431364a736ac6ef3466019979916686f5))
* **tests:** tests must never touch the production database ([#276](https://github.com/Ironsail-llc/genus-os/issues/276)) ([b41c2b0](https://github.com/Ironsail-llc/genus-os/commit/b41c2b02c1d7a7cb592270738986a3ab7b00d734))

## [1.33.2](https://github.com/Ironsail-llc/genus-os/compare/v1.33.1...v1.33.2) (2026-08-20)

### Bug Fixes

* **memory:** back off and pace remote generation under rate limits ([#274](https://github.com/Ironsail-llc/genus-os/issues/274)) ([63392dc](https://github.com/Ironsail-llc/genus-os/commit/63392dc31b67533d61bf3aea1309b1ffc4007e6c)), closes [#255](https://github.com/Ironsail-llc/genus-os/issues/255)

## [1.33.1](https://github.com/Ironsail-llc/genus-os/compare/v1.33.0...v1.33.1) (2026-08-20)

### Bug Fixes

* **vision:** runaway-load guardrails for the VLM follow-up path ([#270](https://github.com/Ironsail-llc/genus-os/issues/270)) ([07bf2d2](https://github.com/Ironsail-llc/genus-os/commit/07bf2d299b98564f6f2c532c289674d90caea7d3))
* **vision:** snapshot_path is optional when alerts are suppressed ([#273](https://github.com/Ironsail-llc/genus-os/issues/273)) ([383a6f3](https://github.com/Ironsail-llc/genus-os/commit/383a6f339ac7c01185a9a72fab3125beca64bd8f))

## [1.33.0](https://github.com/Ironsail-llc/genus-os/compare/v1.32.0...v1.33.0) (2026-08-20)

### Features

* **docs:** mkdocs site published via GitHub Pages ([#268](https://github.com/Ironsail-llc/genus-os/issues/268)) ([66719f9](https://github.com/Ironsail-llc/genus-os/commit/66719f911263dc540897c4e9341f78d12708c143))

## [1.32.0](https://github.com/Ironsail-llc/genus-os/compare/v1.31.2...v1.32.0) (2026-08-20)

### Features

* **memory:** flag-gated remote provider for memory generation ([#263](https://github.com/Ironsail-llc/genus-os/issues/263)) ([ef8c4c0](https://github.com/Ironsail-llc/genus-os/commit/ef8c4c058f4132cf051bf31bf4ac434ed11bc61d))

## [1.31.2](https://github.com/Ironsail-llc/genus-os/compare/v1.31.1...v1.31.2) (2026-08-20)

### Bug Fixes

* **security:** untrack instance artifacts leaked into the public repo ([#262](https://github.com/Ironsail-llc/genus-os/issues/262)) ([8a6e399](https://github.com/Ironsail-llc/genus-os/commit/8a6e39920344cc00eeb5678d1b5f26e1a6d43f06)), closes [#260](https://github.com/Ironsail-llc/genus-os/issues/260)

## [1.31.1](https://github.com/Ironsail-llc/genus-os/compare/v1.31.0...v1.31.1) (2026-08-20)

### Bug Fixes

* **engine:** move skill runtime state to state.json sidecars ([#261](https://github.com/Ironsail-llc/genus-os/issues/261)) ([240b345](https://github.com/Ironsail-llc/genus-os/commit/240b3459ca69189c7eaf534a334660500faf59eb))

## [1.31.0](https://github.com/Ironsail-llc/genus-os/compare/v1.30.13...v1.31.0) (2026-08-20)

### Features

* **ops:** install-units.sh renders and verifies repo units ([#259](https://github.com/Ironsail-llc/genus-os/issues/259)) ([03f96e1](https://github.com/Ironsail-llc/genus-os/commit/03f96e16bc9489b3d0d5093c09fe263c41492c99)), closes [#241](https://github.com/Ironsail-llc/genus-os/issues/241)

### Bug Fixes

* **docker:** copy hatch_build.py into the python image build ([#265](https://github.com/Ironsail-llc/genus-os/issues/265)) ([0254597](https://github.com/Ironsail-llc/genus-os/commit/02545973be4991d1cd30a4f09323eab6a0c78f0d)), closes [#258](https://github.com/Ironsail-llc/genus-os/issues/258) [#254](https://github.com/Ironsail-llc/genus-os/issues/254) [#254](https://github.com/Ironsail-llc/genus-os/issues/254)
* **ops:** genericize mirrored drop-ins and render-aware drift checks ([#266](https://github.com/Ironsail-llc/genus-os/issues/266)) ([05aa81e](https://github.com/Ironsail-llc/genus-os/commit/05aa81ed15a2cb88c3e5d6ce7c03e37a706d7cc6)), closes [#252](https://github.com/Ironsail-llc/genus-os/issues/252) [#259](https://github.com/Ironsail-llc/genus-os/issues/259) [#252](https://github.com/Ironsail-llc/genus-os/issues/252)

## [1.30.13](https://github.com/Ironsail-llc/genus-os/compare/v1.30.12...v1.30.13) (2026-08-19)

### Bug Fixes

* **dist:** keep untracked instance SQL out of built wheels ([#258](https://github.com/Ironsail-llc/genus-os/issues/258)) ([02f243f](https://github.com/Ironsail-llc/genus-os/commit/02f243f698d993abe72df5141f2bd57790e0608c)), closes [#244](https://github.com/Ironsail-llc/genus-os/issues/244)

## [1.30.12](https://github.com/Ironsail-llc/genus-os/compare/v1.30.11...v1.30.12) (2026-08-19)

### Bug Fixes

* **memory:** gate autoDream's model unload dance on memory pressure ([#257](https://github.com/Ironsail-llc/genus-os/issues/257)) ([52255f9](https://github.com/Ironsail-llc/genus-os/commit/52255f9b28727f6a2e55c205bc23f6aa3e357e3a))

## [1.30.11](https://github.com/Ironsail-llc/genus-os/compare/v1.30.10...v1.30.11) (2026-08-19)

### Bug Fixes

* **engine:** batch soft runaway-token alerts, make thresholds tunable ([#256](https://github.com/Ironsail-llc/genus-os/issues/256)) ([9d9e928](https://github.com/Ironsail-llc/genus-os/commit/9d9e928a53530b76b71e447714d8b61a4bd16174))

## [1.30.10](https://github.com/Ironsail-llc/genus-os/compare/v1.30.9...v1.30.10) (2026-08-19)

### Bug Fixes

* **llm:** make the Ollama client actually retry with a real budget ([#255](https://github.com/Ironsail-llc/genus-os/issues/255)) ([923d90f](https://github.com/Ironsail-llc/genus-os/commit/923d90f92e0f9590e748ea5132bf1e0ff9945d81))

## [1.30.9](https://github.com/Ironsail-llc/genus-os/compare/v1.30.8...v1.30.9) (2026-08-19)

### Bug Fixes

* **ops:** guardrail-watch orders after postgres and survives DB loss ([#253](https://github.com/Ironsail-llc/genus-os/issues/253)) ([ca6d374](https://github.com/Ironsail-llc/genus-os/commit/ca6d374f15ad86ddaccf4098831a5c2f768938df))

## [1.30.8](https://github.com/Ironsail-llc/genus-os/compare/v1.30.7...v1.30.8) (2026-08-19)

### Bug Fixes

* **ops:** drop backup mount from engine sandbox, mirror live hardening ([#252](https://github.com/Ironsail-llc/genus-os/issues/252)) ([27abe4c](https://github.com/Ironsail-llc/genus-os/commit/27abe4c40626a1d78c65a7b1f172a03a6a2998d5))

## [1.30.7](https://github.com/Ironsail-llc/genus-os/compare/v1.30.6...v1.30.7) (2026-08-19)

### Bug Fixes

* **engine:** /restart writes the restart-request file, not sudo ([#251](https://github.com/Ironsail-llc/genus-os/issues/251)) ([0604099](https://github.com/Ironsail-llc/genus-os/commit/0604099d08c5733978f1afd929a40987288b7ff4))

## [1.30.6](https://github.com/Ironsail-llc/genus-os/compare/v1.30.5...v1.30.6) (2026-08-19)

### Bug Fixes

* **dist:** bundle the agent catalog and a generic starter agent ([#249](https://github.com/Ironsail-llc/genus-os/issues/249)) ([a6e93ca](https://github.com/Ironsail-llc/genus-os/commit/a6e93caf19265dd23142a45fec1be84e79d8bc54))

## [1.30.5](https://github.com/Ironsail-llc/genus-os/compare/v1.30.4...v1.30.5) (2026-08-19)

### Bug Fixes

* **dist:** installer resolves paths outside site-packages installs ([#248](https://github.com/Ironsail-llc/genus-os/issues/248)) ([7ae1933](https://github.com/Ironsail-llc/genus-os/commit/7ae19335548af3a2516cf9d907bc41535865e31e)), closes [#245](https://github.com/Ironsail-llc/genus-os/issues/245)

## [1.30.4](https://github.com/Ironsail-llc/genus-os/compare/v1.30.3...v1.30.4) (2026-08-19)

### Bug Fixes

* **config:** load the workspace .env that init writes ([#247](https://github.com/Ironsail-llc/genus-os/issues/247)) ([40693c2](https://github.com/Ironsail-llc/genus-os/commit/40693c2f817652f634639d47289830f6a939f89c))

## [1.30.3](https://github.com/Ironsail-llc/genus-os/compare/v1.30.2...v1.30.3) (2026-08-19)

### Bug Fixes

* **docker:** copy the scaffold templates into the python image build ([#254](https://github.com/Ironsail-llc/genus-os/issues/254)) ([376e2ad](https://github.com/Ironsail-llc/genus-os/commit/376e2adf870c7d193cc397da65874c97546ef684)), closes [#250](https://github.com/Ironsail-llc/genus-os/issues/250)

## [1.30.2](https://github.com/Ironsail-llc/genus-os/compare/v1.30.1...v1.30.2) (2026-08-18)

### Bug Fixes

* **cli:** init next-steps match the install mode ([#246](https://github.com/Ironsail-llc/genus-os/issues/246)) ([0113234](https://github.com/Ironsail-llc/genus-os/commit/0113234ec3da709ce1a1471288b4d10df8794591))
* **docker:** include the scaffold templates in the build context ([#250](https://github.com/Ironsail-llc/genus-os/issues/250)) ([0b47423](https://github.com/Ironsail-llc/genus-os/commit/0b47423340c2a44ee2420394479eaad481c22d30)), closes [#245](https://github.com/Ironsail-llc/genus-os/issues/245) [245/#246](https://github.com/245/genus-os/issues/246)

## [1.30.1](https://github.com/Ironsail-llc/genus-os/compare/v1.30.0...v1.30.1) (2026-08-18)

### Bug Fixes

* **dist:** ship the init scaffold in the wheel via package data ([#245](https://github.com/Ironsail-llc/genus-os/issues/245)) ([0e32135](https://github.com/Ironsail-llc/genus-os/commit/0e321357b40430f701f9c238c73c7f487bd08378))

## [1.30.0](https://github.com/Ironsail-llc/genus-os/compare/v1.29.2...v1.30.0) (2026-08-18)

### Features

* **dashboard:** premium redesign with aurora tokens, light/dark themes, cmd-k ([#220](https://github.com/Ironsail-llc/genus-os/issues/220)) ([976fc99](https://github.com/Ironsail-llc/genus-os/commit/976fc999b50e11a0ed52a4e3f138289ebb46bd27))

## [1.29.2](https://github.com/Ironsail-llc/genus-os/compare/v1.29.1...v1.29.2) (2026-08-18)

### Bug Fixes

* **dist:** working demo compose and honest PyPI card metadata ([#243](https://github.com/Ironsail-llc/genus-os/issues/243)) ([814a3d5](https://github.com/Ironsail-llc/genus-os/commit/814a3d556a249d88f9cd508c7cc4bfa8de67b741))

### Documentation

* fix dead clone URLs and the serve scope claim ([#242](https://github.com/Ironsail-llc/genus-os/issues/242)) ([45a051d](https://github.com/Ironsail-llc/genus-os/commit/45a051dff51b4299a77846a7b32af54ef54e1630))

## [1.29.1](https://github.com/Ironsail-llc/genus-os/compare/v1.29.0...v1.29.1) (2026-08-17)

### Bug Fixes

* **ops:** prune WAL despite rclone failures, dedup pages, install scripts ([#241](https://github.com/Ironsail-llc/genus-os/issues/241)) ([c8c074c](https://github.com/Ironsail-llc/genus-os/commit/c8c074c9668008ba1bef2f1d5609462aa4da304f))

## [1.29.0](https://github.com/Ironsail-llc/genus-os/compare/v1.28.2...v1.29.0) (2026-08-17)

### Features

* **engine:** catch up missed cron runs on scheduler start ([#240](https://github.com/Ironsail-llc/genus-os/issues/240)) ([9e9ae7e](https://github.com/Ironsail-llc/genus-os/commit/9e9ae7ef53930c5906b9885c26e64c9ec531c512))

## [1.28.2](https://github.com/Ironsail-llc/genus-os/compare/v1.28.1...v1.28.2) (2026-08-17)

### Bug Fixes

* **memory:** run the decay pass in budgeted chunks off the event loop ([#239](https://github.com/Ironsail-llc/genus-os/issues/239)) ([c3c411e](https://github.com/Ironsail-llc/genus-os/commit/c3c411ec355193136bf8ea675e037aa3219b3b85))

## [1.28.1](https://github.com/Ironsail-llc/genus-os/compare/v1.28.0...v1.28.1) (2026-08-17)

### Bug Fixes

* **engine:** deliver alerts and defuse the stall-watchdog leak ([#238](https://github.com/Ironsail-llc/genus-os/issues/238)) ([8700a2f](https://github.com/Ironsail-llc/genus-os/commit/8700a2fe0b5c61aafba58eb5eeef5b3f3f89351e))

## [1.28.0](https://github.com/Ironsail-llc/genus-os/compare/v1.27.3...v1.28.0) (2026-08-17)

### Features

* **memory:** ship the tenancy, correctness and eval train ([#235](https://github.com/Ironsail-llc/genus-os/issues/235)) ([72dc65d](https://github.com/Ironsail-llc/genus-os/commit/72dc65d81b4899a80c064123c662d6fec9051eb4))

## [1.27.3](https://github.com/Ironsail-llc/genus-os/compare/v1.27.2...v1.27.3) (2026-08-17)

### Bug Fixes

* **docker:** drop the bundled npm CLI from the app runtime image ([#236](https://github.com/Ironsail-llc/genus-os/issues/236)) ([42872d9](https://github.com/Ironsail-llc/genus-os/commit/42872d9acc8e045c26198892f366d35362d8fc4c))

### Documentation

* **README:** position the platform around plugin extensibility ([#234](https://github.com/Ironsail-llc/genus-os/issues/234)) ([907f92b](https://github.com/Ironsail-llc/genus-os/commit/907f92ba92fd5f69a53f1d59db01723dd3dca5fb))

## [1.27.2](https://github.com/Ironsail-llc/genus-os/compare/v1.27.1...v1.27.2) (2026-08-17)

### Bug Fixes

* **ci:** pin ruff, fix litellm drift, allowlist npm bundled-dep audit ([#232](https://github.com/Ironsail-llc/genus-os/issues/232)) ([0a778cb](https://github.com/Ironsail-llc/genus-os/commit/0a778cb532fd1bf870723d4d6080df8321aefba8))

## [1.27.1](https://github.com/Ironsail-llc/genus-os/compare/v1.27.0...v1.27.1) (2026-07-17)

### Bug Fixes

* **migrate:** tolerate instance-local legacy migrations in reconcile ([#222](https://github.com/Ironsail-llc/genus-os/issues/222)) ([5900a71](https://github.com/Ironsail-llc/genus-os/commit/5900a71e78f4e5418dac7934266e60b91d56c688))

## [1.27.0](https://github.com/Ironsail-llc/genus-os/compare/v1.26.0...v1.27.0) (2026-07-17)

### Features

* **identity:** unified identity context and per-user access control ([#221](https://github.com/Ironsail-llc/genus-os/issues/221)) ([bcec304](https://github.com/Ironsail-llc/genus-os/commit/bcec30473a989417cb765e2b0213f0a9530c42bc))

## [1.26.0](https://github.com/Ironsail-llc/genus-os/compare/v1.25.0...v1.26.0) (2026-07-16)

### Features

* **auth:** seamless dashboard sign-in via Cloudflare Access header trust ([#219](https://github.com/Ironsail-llc/genus-os/issues/219)) ([9aaa8ce](https://github.com/Ironsail-llc/genus-os/commit/9aaa8ce7f0bb29473f6c773f55a9d67fb6c1399d))

## [1.25.0](https://github.com/Ironsail-llc/genus-os/compare/v1.24.0...v1.25.0) (2026-07-15)

### Features

* **canvas:** live declarative binding — the model renders your system ([#218](https://github.com/Ironsail-llc/genus-os/issues/218)) ([1f7596d](https://github.com/Ironsail-llc/genus-os/commit/1f7596d24f579c47034604a4eb656d31f5d6a881))

## [1.24.0](https://github.com/Ironsail-llc/genus-os/compare/v1.23.0...v1.24.0) (2026-07-15)

### Features

* **helm:** canvas bridge — the LLM's live, propose-only rendering surface ([#217](https://github.com/Ironsail-llc/genus-os/issues/217)) ([1e70530](https://github.com/Ironsail-llc/genus-os/commit/1e70530f7d59bfb23515ceb79511744d4b1509ee))

## [1.23.0](https://github.com/Ironsail-llc/genus-os/compare/v1.22.0...v1.23.0) (2026-07-15)

### Features

* **controls:** add DB-backed guardrail control plane and Controls tab ([#215](https://github.com/Ironsail-llc/genus-os/issues/215)) ([557bc5a](https://github.com/Ironsail-llc/genus-os/commit/557bc5a5e9a11c15d75635dc765b9c2594ebf9d2))
* **helm:** operator accounting tabs — fleet, runs, workflows, health ([#216](https://github.com/Ironsail-llc/genus-os/issues/216)) ([ffe85bf](https://github.com/Ironsail-llc/genus-os/commit/ffe85bfe295ec4ca2db44fefe37f2b05afac6ee4))

## [1.22.0](https://github.com/Ironsail-llc/genus-os/compare/v1.21.0...v1.22.0) (2026-07-14)

### Features

* **platform:** harden Genus OS production foundation ([#176](https://github.com/Ironsail-llc/genus-os/issues/176)) ([47a9691](https://github.com/Ironsail-llc/genus-os/commit/47a9691c86ecaa92089f3371335a19521d7558c6))

## [1.21.0](https://github.com/Ironsail-llc/genus-os/compare/v1.20.5...v1.21.0) (2026-07-14)

### Features

* **dr:** archive WAL and take base backups — RPO 24h to ~15min, drilled ([#214](https://github.com/Ironsail-llc/genus-os/issues/214)) ([afc71b8](https://github.com/Ironsail-llc/genus-os/commit/afc71b88a44cc10d63bb8922343a37e98b4ac324))

## [1.20.5](https://github.com/Ironsail-llc/genus-os/compare/v1.20.4...v1.20.5) (2026-07-14)

### Bug Fixes

* **app:** the test suite could not sanitize — jsdom, and a real XSS guard ([#213](https://github.com/Ironsail-llc/genus-os/issues/213)) ([18b48df](https://github.com/Ironsail-llc/genus-os/commit/18b48df500a2ab669b0cc44145b5aa64d1a1787f)), closes [#162](https://github.com/Ironsail-llc/genus-os/issues/162)

## [1.20.4](https://github.com/Ironsail-llc/genus-os/compare/v1.20.3...v1.20.4) (2026-07-14)

### Bug Fixes

* **rls:** close the last superuser bypass, and make an inert RLS loud ([#212](https://github.com/Ironsail-llc/genus-os/issues/212)) ([755f79b](https://github.com/Ironsail-llc/genus-os/commit/755f79bf8e0cc87f599537e6b8727d611eed5dd0))

## [1.20.3](https://github.com/Ironsail-llc/genus-os/compare/v1.20.2...v1.20.3) (2026-07-14)

### Bug Fixes

* **backup:** the primary backup must page when it fails ([#211](https://github.com/Ironsail-llc/genus-os/issues/211)) ([dcdafec](https://github.com/Ironsail-llc/genus-os/commit/dcdafec8c6f43719f95666d60d280701e06d3b53))

## [1.20.2](https://github.com/Ironsail-llc/genus-os/compare/v1.20.1...v1.20.2) (2026-07-14)

### Bug Fixes

* **boot:** survive a cold boot, and let the pager page during one ([#210](https://github.com/Ironsail-llc/genus-os/issues/210)) ([9b80236](https://github.com/Ironsail-llc/genus-os/commit/9b8023670c809c28d1b954cda79a065c1e0986a4))

## [1.20.1](https://github.com/Ironsail-llc/genus-os/compare/v1.20.0...v1.20.1) (2026-07-14)

### Bug Fixes

* **sandbox:** unblock containers, and record what the gate actually found ([#209](https://github.com/Ironsail-llc/genus-os/issues/209)) ([104f9d6](https://github.com/Ironsail-llc/genus-os/commit/104f9d68b447d3d3c50f763a93592de8d4637d08)), closes [#207](https://github.com/Ironsail-llc/genus-os/issues/207) [#205](https://github.com/Ironsail-llc/genus-os/issues/205)

## [1.20.0](https://github.com/Ironsail-llc/genus-os/compare/v1.19.1...v1.20.0) (2026-07-14)

### Features

* **engine:** make the sandbox actually sandbox exec ([#207](https://github.com/Ironsail-llc/genus-os/issues/207)) ([2d7cd70](https://github.com/Ironsail-llc/genus-os/commit/2d7cd7086de0310aa17e03151cda3deb1da37287)), closes [#205](https://github.com/Ironsail-llc/genus-os/issues/205) [#201](https://github.com/Ironsail-llc/genus-os/issues/201) [#205](https://github.com/Ironsail-llc/genus-os/issues/205)

## [1.19.1](https://github.com/Ironsail-llc/genus-os/compare/v1.19.0...v1.19.1) (2026-07-14)

### Bug Fixes

* **bridge:** bind the tenant on the bridge's raw connection ([#208](https://github.com/Ironsail-llc/genus-os/issues/208)) ([34e6e80](https://github.com/Ironsail-llc/genus-os/commit/34e6e80b1196d4c3ff10a01731423bbd83fd4c1f))

## [1.19.0](https://github.com/Ironsail-llc/genus-os/compare/v1.18.1...v1.19.0) (2026-07-14)

### Features

* **engine:** fully-anchored exec allowlist patterns ([#206](https://github.com/Ironsail-llc/genus-os/issues/206)) ([17423b3](https://github.com/Ironsail-llc/genus-os/commit/17423b3052c9cc4818bb8e0eced76eb192ca4d08)), closes [#205](https://github.com/Ironsail-llc/genus-os/issues/205)

## [1.18.1](https://github.com/Ironsail-llc/genus-os/compare/v1.18.0...v1.18.1) (2026-07-14)

### Bug Fixes

* **security:** block the sudo path to root, and correct the sandbox flag ([#205](https://github.com/Ironsail-llc/genus-os/issues/205)) ([501398f](https://github.com/Ironsail-llc/genus-os/commit/501398fb40126b00494a386102375f6cc3e6ed0f))

## [1.18.0](https://github.com/Ironsail-llc/genus-os/compare/v1.17.0...v1.18.0) (2026-07-14)

### Features

* **security:** row-level security backstop for tenant isolation ([#203](https://github.com/Ironsail-llc/genus-os/issues/203)) ([f4afe09](https://github.com/Ironsail-llc/genus-os/commit/f4afe093a1715a604be6e3ad359ed4b5ef8bebad))

### Bug Fixes

* **ops:** verify only the generations we actually retain offsite ([#204](https://github.com/Ironsail-llc/genus-os/issues/204)) ([1211564](https://github.com/Ironsail-llc/genus-os/commit/1211564a3aac241bde4137a52fe092452ecddb7b))

## [1.17.0](https://github.com/Ironsail-llc/genus-os/compare/v1.16.2...v1.17.0) (2026-07-14)

### Features

* **engine:** circuit breaker on the LLM fallback chain ([#202](https://github.com/Ironsail-llc/genus-os/issues/202)) ([18ab730](https://github.com/Ironsail-llc/genus-os/commit/18ab730c9fd5d74d1dde08b8e7610b0f0801a1ef))

## [1.16.2](https://github.com/Ironsail-llc/genus-os/compare/v1.16.1...v1.16.2) (2026-07-14)

### Bug Fixes

* **security:** sandbox fails closed under enforce ([#201](https://github.com/Ironsail-llc/genus-os/issues/201)) ([803cd30](https://github.com/Ironsail-llc/genus-os/commit/803cd30c2578aa38105a171adf16a626856ffbfb))

## [1.16.1](https://github.com/Ironsail-llc/genus-os/compare/v1.16.0...v1.16.1) (2026-07-14)

### Bug Fixes

* **engine:** never silently drop a guardrail audit write ([#200](https://github.com/Ironsail-llc/genus-os/issues/200)) ([ed34be1](https://github.com/Ironsail-llc/genus-os/commit/ed34be14180701a0a5cee4a98e415ed1cd6a3b23)), closes [#184](https://github.com/Ironsail-llc/genus-os/issues/184) [#187](https://github.com/Ironsail-llc/genus-os/issues/187)

## [1.16.0](https://github.com/Ironsail-llc/genus-os/compare/v1.15.6...v1.16.0) (2026-07-14)

### Features

* **ops:** nag on session goals that have stopped moving ([#199](https://github.com/Ironsail-llc/genus-os/issues/199)) ([caaa735](https://github.com/Ironsail-llc/genus-os/commit/caaa7355ff06efd0d6b183f982dda548f67b0795))

## [1.15.6](https://github.com/Ironsail-llc/genus-os/compare/v1.15.5...v1.15.6) (2026-07-14)

### Bug Fixes

* **ops:** upload only the retained backup generations ([#198](https://github.com/Ironsail-llc/genus-os/issues/198)) ([f81ce3a](https://github.com/Ironsail-llc/genus-os/commit/f81ce3a33f692bdaf11783a9a21bd3a47db71305)), closes [#192](https://github.com/Ironsail-llc/genus-os/issues/192)

## [1.15.5](https://github.com/Ironsail-llc/genus-os/compare/v1.15.4...v1.15.5) (2026-07-13)

### Bug Fixes

* **security:** enforce the human-approval gate, now that it is real ([#197](https://github.com/Ironsail-llc/genus-os/issues/197)) ([16b84ab](https://github.com/Ironsail-llc/genus-os/commit/16b84ab5d9357bb570f9473cb30b2b7da4e81ea7)), closes [#186](https://github.com/Ironsail-llc/genus-os/issues/186)

## [1.15.4](https://github.com/Ironsail-llc/genus-os/compare/v1.15.3...v1.15.4) (2026-07-13)

### Bug Fixes

* **security:** worker runs must keep the agent's guardrails ([#196](https://github.com/Ironsail-llc/genus-os/issues/196)) ([a590f48](https://github.com/Ironsail-llc/genus-os/commit/a590f48b20009792ed6f1283d49cadda8e2a01bd))

## [1.15.3](https://github.com/Ironsail-llc/genus-os/compare/v1.15.2...v1.15.3) (2026-07-13)

### Bug Fixes

* **engine:** make guardrail alerts reach the operator, from every consumer ([#195](https://github.com/Ironsail-llc/genus-os/issues/195)) ([3f24045](https://github.com/Ironsail-llc/genus-os/commit/3f240459e2d5ca0ce84b68682242282d29b97795)), closes [#190](https://github.com/Ironsail-llc/genus-os/issues/190) [#194](https://github.com/Ironsail-llc/genus-os/issues/194) [#194](https://github.com/Ironsail-llc/genus-os/issues/194) [#179](https://github.com/Ironsail-llc/genus-os/issues/179) [#180](https://github.com/Ironsail-llc/genus-os/issues/180)

## [1.15.2](https://github.com/Ironsail-llc/genus-os/compare/v1.15.1...v1.15.2) (2026-07-13)

### Bug Fixes

* **engine:** deliver operator alerts the database will actually accept ([#194](https://github.com/Ironsail-llc/genus-os/issues/194)) ([0d4227e](https://github.com/Ironsail-llc/genus-os/commit/0d4227ecf5540ad0f691692891b208effc2b64be)), closes [#190](https://github.com/Ironsail-llc/genus-os/issues/190)

## [1.15.1](https://github.com/Ironsail-llc/genus-os/compare/v1.15.0...v1.15.1) (2026-07-13)

### Bug Fixes

* **security:** enforce memory-fact drift detection (rip 7) ([#193](https://github.com/Ironsail-llc/genus-os/issues/193)) ([49181e1](https://github.com/Ironsail-llc/genus-os/commit/49181e18184fd0877e205e88f21fd3f9e2860518)), closes [#3](https://github.com/Ironsail-llc/genus-os/issues/3) [#184](https://github.com/Ironsail-llc/genus-os/issues/184) [#187](https://github.com/Ironsail-llc/genus-os/issues/187) [#186](https://github.com/Ironsail-llc/genus-os/issues/186) [#190](https://github.com/Ironsail-llc/genus-os/issues/190)

## [1.15.0](https://github.com/Ironsail-llc/genus-os/compare/v1.14.5...v1.15.0) (2026-07-13)

### Features

* **ops:** replicate backups offsite with verification and paging ([#192](https://github.com/Ironsail-llc/genus-os/issues/192)) ([1717e00](https://github.com/Ironsail-llc/genus-os/commit/1717e00d34cc9dd29318077b54773be304749615)), closes [#179](https://github.com/Ironsail-llc/genus-os/issues/179)

## [1.14.5](https://github.com/Ironsail-llc/genus-os/compare/v1.14.4...v1.14.5) (2026-07-13)

### Bug Fixes

* **security:** make cli runs obey the daemon's guardrails ([#191](https://github.com/Ironsail-llc/genus-os/issues/191)) ([a689438](https://github.com/Ironsail-llc/genus-os/commit/a689438a467158110b6b049de5e75969e482d076))

## [1.14.4](https://github.com/Ironsail-llc/genus-os/compare/v1.14.3...v1.14.4) (2026-07-13)

### Bug Fixes

* **security:** enforce exec allowlist shell-metacharacter block ([#189](https://github.com/Ironsail-llc/genus-os/issues/189)) ([124dc1f](https://github.com/Ironsail-llc/genus-os/commit/124dc1f1baf8f850f93d5208d8e69422ed8251a4)), closes [#2](https://github.com/Ironsail-llc/genus-os/issues/2) [#187](https://github.com/Ironsail-llc/genus-os/issues/187) [#187](https://github.com/Ironsail-llc/genus-os/issues/187)

## [1.14.3](https://github.com/Ironsail-llc/genus-os/compare/v1.14.2...v1.14.3) (2026-07-13)

### Bug Fixes

* **engine:** make the alert rung of the enforcement ladder real ([#190](https://github.com/Ironsail-llc/genus-os/issues/190)) ([ce2b74c](https://github.com/Ironsail-llc/genus-os/commit/ce2b74c8ff5b10fe196eb534452f4c52b77f43d7)), closes [#184](https://github.com/Ironsail-llc/genus-os/issues/184) [#187](https://github.com/Ironsail-llc/genus-os/issues/187) [#186](https://github.com/Ironsail-llc/genus-os/issues/186)

## [1.14.2](https://github.com/Ironsail-llc/genus-os/compare/v1.14.1...v1.14.2) (2026-07-13)

### Bug Fixes

* **engine:** accept sandbox host in the config validator ([#188](https://github.com/Ironsail-llc/genus-os/issues/188)) ([af88742](https://github.com/Ironsail-llc/genus-os/commit/af887428d6435f07c33631632dcc530ffac17dfd)), closes [#185](https://github.com/Ironsail-llc/genus-os/issues/185)

## [1.14.1](https://github.com/Ironsail-llc/genus-os/compare/v1.14.0...v1.14.1) (2026-07-13)

### Bug Fixes

* **engine:** persist observe-mode guardrail findings as evidence ([#187](https://github.com/Ironsail-llc/genus-os/issues/187)) ([d414a9a](https://github.com/Ironsail-llc/genus-os/commit/d414a9a3ed40ed1570c60aa73fd275b58332bf3d)), closes [#2](https://github.com/Ironsail-llc/genus-os/issues/2)

## [1.14.0](https://github.com/Ironsail-llc/genus-os/compare/v1.13.2...v1.14.0) (2026-07-13)

### Features

* **agents:** document sandbox host opt-out in the manifest schema ([#185](https://github.com/Ironsail-llc/genus-os/issues/185)) ([13e251c](https://github.com/Ironsail-llc/genus-os/commit/13e251c81a59fd76f13960598447de57d00c3844))

### Bug Fixes

* **docs:** record that the human-approval gate is inert, not soaked clean ([#186](https://github.com/Ironsail-llc/genus-os/issues/186)) ([ca44448](https://github.com/Ironsail-llc/genus-os/commit/ca444481f3ffb367f1a0480457fa2c93d18f1425)), closes [#3](https://github.com/Ironsail-llc/genus-os/issues/3)

## [1.13.2](https://github.com/Ironsail-llc/genus-os/compare/v1.13.1...v1.13.2) (2026-07-13)

### Bug Fixes

* **engine:** record audit trail when injection scan blocks a run ([#184](https://github.com/Ironsail-llc/genus-os/issues/184)) ([e3d16bc](https://github.com/Ironsail-llc/genus-os/commit/e3d16bc13d6fd3ac60b603c6aa3c02c7e5d98dd7)), closes [#182](https://github.com/Ironsail-llc/genus-os/issues/182)

## [1.13.1](https://github.com/Ironsail-llc/genus-os/compare/v1.13.0...v1.13.1) (2026-07-13)

### Bug Fixes

* **security:** enforce prompt-injection scan guardrail ([#182](https://github.com/Ironsail-llc/genus-os/issues/182)) ([7802d01](https://github.com/Ironsail-llc/genus-os/commit/7802d017afa79030e3d9e4753c32156b554396be)), closes [#1](https://github.com/Ironsail-llc/genus-os/issues/1)

## [1.13.0](https://github.com/Ironsail-llc/genus-os/compare/v1.12.0...v1.13.0) (2026-07-13)

### Features

* **infra:** page operator via Telegram when critical units fail ([#179](https://github.com/Ironsail-llc/genus-os/issues/179)) ([3072b8a](https://github.com/Ironsail-llc/genus-os/commit/3072b8a00ab3e903e6a174c8b56012ffee826048))

## [1.12.0](https://github.com/Ironsail-llc/genus-os/compare/v1.11.1...v1.12.0) (2026-07-13)

### Features

* **connectors:** MCP 2026-07-28 dual-mode client + version-tolerant bridge ([#174](https://github.com/Ironsail-llc/genus-os/issues/174)) ([70e0d3f](https://github.com/Ironsail-llc/genus-os/commit/70e0d3f709c71f659b2db06dff17d80628e04ce4))
* **engine:** flag manifest with soak-deadline nags in guardrail watch ([#180](https://github.com/Ironsail-llc/genus-os/issues/180)) ([2183357](https://github.com/Ironsail-llc/genus-os/commit/218335776b79cb296221e2bf33724dead5243b60)), closes [#178](https://github.com/Ironsail-llc/genus-os/issues/178)

## [1.11.1](https://github.com/Ironsail-llc/genus-os/compare/v1.11.0...v1.11.1) (2026-07-13)

### Bug Fixes

* **engine:** restore OpenRouter + resolve dated response slugs ([#177](https://github.com/Ironsail-llc/genus-os/issues/177)) ([b0ac85b](https://github.com/Ironsail-llc/genus-os/commit/b0ac85beda74be97cc9886fc335a032692a168f5))

## [1.11.0](https://github.com/Ironsail-llc/genus-os/compare/v1.10.1...v1.11.0) (2026-07-13)

### Features

* **engine:** completion contracts + accretion gate live caller ([#172](https://github.com/Ironsail-llc/genus-os/issues/172)) ([e93b267](https://github.com/Ironsail-llc/genus-os/commit/e93b267b90ad6016ec172f4300fe8466d9a91bfd))

## [1.10.1](https://github.com/Ironsail-llc/genus-os/compare/v1.10.0...v1.10.1) (2026-07-13)

### Bug Fixes

* **engine:** approval-manager e2e coverage + dropped-keyboard bug ([#171](https://github.com/Ironsail-llc/genus-os/issues/171)) ([07e534d](https://github.com/Ironsail-llc/genus-os/commit/07e534d7032956a76bd2eba652d5a3e118f7f0d2))

## [1.10.0](https://github.com/Ironsail-llc/genus-os/compare/v1.9.0...v1.10.0) (2026-07-03)

### Features

* **engine:** cache-hit-rate metrics + catalog-driven prompt caching ([#173](https://github.com/Ironsail-llc/genus-os/issues/173)) ([8a9833f](https://github.com/Ironsail-llc/genus-os/commit/8a9833f24fe2a379e9fe97896d2c14130bda5d5e))
* **engine:** live-run visibility + Telegram steer/interrupt ([#170](https://github.com/Ironsail-llc/genus-os/issues/170)) ([58b1d93](https://github.com/Ironsail-llc/genus-os/commit/58b1d9384657b39da42ae77b4e97fb6277e97b0b))

## [1.9.0](https://github.com/Ironsail-llc/genus-os/compare/v1.8.0...v1.9.0) (2026-07-02)

### Features

* **auth:** multi-user SSO login + bridge JWT sessions (Phase A, shadow mode) ([#145](https://github.com/Ironsail-llc/genus-os/issues/145)) ([8603a68](https://github.com/Ironsail-llc/genus-os/commit/8603a68fef32eff1b7a34b33687dfb9e4b254972))

### Bug Fixes

* **engine:** reconcile autoDream robustness — deep-mode, symlink-safe fallback, single-owner ([#150](https://github.com/Ironsail-llc/genus-os/issues/150)) ([8ed2c04](https://github.com/Ironsail-llc/genus-os/commit/8ed2c043ec90db2e6371ac1cdcb8ebff6281478a))
* **engine:** wire federation responder + code-enforce curator dry-run ([#169](https://github.com/Ironsail-llc/genus-os/issues/169)) ([9c7b274](https://github.com/Ironsail-llc/genus-os/commit/9c7b2747622c530e5f9b1169ff0c84dc36ef9efa))
* **helm:** reference ghcr-credentials pull secret for private images ([#141](https://github.com/Ironsail-llc/genus-os/issues/141)) ([c7e3ade](https://github.com/Ironsail-llc/genus-os/commit/c7e3adeac04cf2afadada8092c4a8f2a7a7ecde6))

### Documentation

* **helm:** add staging URL hint + release-and-build CI gating ([#135](https://github.com/Ironsail-llc/genus-os/issues/135)) ([54a00b4](https://github.com/Ironsail-llc/genus-os/commit/54a00b400ae99c561a8a2de2e0cd10c76a28c4d4))

### Code Refactoring

* **crm:** break the engine↔CRM import cycle behind a hooks seam ([#144](https://github.com/Ironsail-llc/genus-os/issues/144)) ([41c7b71](https://github.com/Ironsail-llc/genus-os/commit/41c7b7189fdb4fd1582bc1a698a83611cf339851))

## [1.8.0](https://github.com/Ironsail-llc/genus-os/compare/v1.7.0...v1.8.0) (2026-07-02)

### Features

* **memory:** recall/anti-churn/cognitive-layer upgrade + engine phase-a ([#166](https://github.com/Ironsail-llc/genus-os/issues/166)) ([27586c6](https://github.com/Ironsail-llc/genus-os/commit/27586c6e3b579b06f6921d7b673088f275e9f5e6))

## [1.7.0](https://github.com/Ironsail-llc/genus-os/compare/v1.6.0...v1.7.0) (2026-07-02)

### Features

* **engine:** wave 2 — federation transport, goal-judge, HA leader, accretion ([#156](https://github.com/Ironsail-llc/genus-os/issues/156)) ([1017974](https://github.com/Ironsail-llc/genus-os/commit/1017974afa22b2835350c6751a75e62f7853ac30))

## [1.6.0](https://github.com/Ironsail-llc/genus-os/compare/v1.5.0...v1.6.0) (2026-07-02)

### Features

* **engine:** wave 2 — HA dedup, OTLP observability & SIEM audit ([#155](https://github.com/Ironsail-llc/genus-os/issues/155)) ([98de1ab](https://github.com/Ironsail-llc/genus-os/commit/98de1ab53f267a665cf943d0a87151b0139071c0))

## [1.5.0](https://github.com/Ironsail-llc/genus-os/compare/v1.4.0...v1.5.0) (2026-07-02)

### Features

* **engine:** wave 2 — code search, safe-eval, model catalog, tokenizer, memory ([#154](https://github.com/Ironsail-llc/genus-os/issues/154)) ([75615ca](https://github.com/Ironsail-llc/genus-os/commit/75615ca5af8e312cc3e7ca494a24cc3d40de8632))

## [1.4.0](https://github.com/Ironsail-llc/genus-os/compare/v1.3.0...v1.4.0) (2026-07-02)

### Features

* **engine:** wave 1 — activate built-but-dark subsystems ([#153](https://github.com/Ironsail-llc/genus-os/issues/153)) ([5eaa868](https://github.com/Ironsail-llc/genus-os/commit/5eaa86890287f802218cd5af6940069fa391621f))

## [1.3.0](https://github.com/Ironsail-llc/genus-os/compare/v1.2.0...v1.3.0) (2026-07-02)

### Features

* **engine:** wave 1 — fleet RBAC, sandboxing, approval & injection guards ([#152](https://github.com/Ironsail-llc/genus-os/issues/152)) ([251f0ce](https://github.com/Ironsail-llc/genus-os/commit/251f0ce690d11388c652644ff3689983a60d8989))

## [1.2.0](https://github.com/Ironsail-llc/genus-os/compare/v1.1.1...v1.2.0) (2026-07-02)

### Features

* **engine:** wave 1 — audit substrate, honesty & low-risk security ([#151](https://github.com/Ironsail-llc/genus-os/issues/151)) ([58dd51e](https://github.com/Ironsail-llc/genus-os/commit/58dd51ef525d71999fa94c97c73a48cab1c1b844))

## [1.1.1](https://github.com/Ironsail-llc/genus-os/compare/v1.1.0...v1.1.1) (2026-06-04)

### Bug Fixes

* **tasks:** consolidate + harden task-answer feature, integration suite & deps ([#149](https://github.com/Ironsail-llc/genus-os/issues/149)) ([7c59312](https://github.com/Ironsail-llc/genus-os/commit/7c5931271da25208691dd9d2d0db600f7e0e3b74)), closes [#128](https://github.com/Ironsail-llc/genus-os/issues/128) [#139](https://github.com/Ironsail-llc/genus-os/issues/139) [#140](https://github.com/Ironsail-llc/genus-os/issues/140) [#121](https://github.com/Ironsail-llc/genus-os/issues/121) [#147](https://github.com/Ironsail-llc/genus-os/issues/147) [#128](https://github.com/Ironsail-llc/genus-os/issues/128)

### Tests

* **crm:** integration suite + checkpoint TodoList persistence ([#140](https://github.com/Ironsail-llc/genus-os/issues/140)) ([1c4669d](https://github.com/Ironsail-llc/genus-os/commit/1c4669d2812bc686b23b3cb0317088ae206b60bd))

## [1.1.0](https://github.com/Ironsail-llc/genus-os/compare/v1.0.0...v1.1.0) (2026-05-29)

### Features

* **engine:** promote unfinished todo_write items to CRM subtasks ([#128](https://github.com/Ironsail-llc/genus-os/issues/128)) ([bc536b8](https://github.com/Ironsail-llc/genus-os/commit/bc536b886eeb01d2f3a3d75c05f5535e69c067d6)), closes [#126](https://github.com/Ironsail-llc/genus-os/issues/126)

## 1.0.0 (2026-05-28)

### Features

* absorb ~/clawd/ into brain/ — single-repo migration ([0da3795](https://github.com/Ironsail-llc/genus-os/commit/0da3795ecbe51dd46112b96fe2d99f9cdc80423a))
* activate PostgreSQL vault, destroy Vaultwarden ([80f5c24](https://github.com/Ironsail-llc/genus-os/commit/80f5c2464fb71dcc63cfe37ca7b0039bb32a5a1e))
* add --trust flag to federation connect for skipping signature verification ([4af8f7a](https://github.com/Ironsail-llc/genus-os/commit/4af8f7ad78e1db96edfd743b4e06ce14361d3cd7))
* add agent data passthrough to dashboard pipeline ([aac7c6f](https://github.com/Ironsail-llc/genus-os/commit/aac7c6f3558615e2a1318f830e941a42a96014ed))
* add agent task coordination system ([a1f2073](https://github.com/Ironsail-llc/genus-os/commit/a1f20738d51c5b5cc00ef49b6fcefaff7f18469a))
* add business layer app (Next.js 16 + Dockview live dashboard) ([1ce15ce](https://github.com/Ironsail-llc/genus-os/commit/1ce15ce8b9986cef3e0e8c9783fb2de81943de0c))
* add CI governance — CodeQL, PR labeler, stale bot, pre-commit.ci ([#3](https://github.com/Ironsail-llc/genus-os/issues/3)) ([251ec60](https://github.com/Ironsail-llc/genus-os/commit/251ec602fed2d41ff85762217ad0d035c1a4fdfc))
* add computer use — desktop control + browser automation for agents ([40ead41](https://github.com/Ironsail-llc/genus-os/commit/40ead4176135ae799776ae03fb660d2ea6f4c1f5))
* add computer use — desktop control + browser automation for agents ([a013ff8](https://github.com/Ironsail-llc/genus-os/commit/a013ff82e2277215d22500afdbf912ce8b47b861))
* add duplicate prevention guards and reduce email crons to hourly ([9beaad1](https://github.com/Ironsail-llc/genus-os/commit/9beaad19c27453d1c3684ad62dee228b45908a72))
* add dynamic owner identity and setup wizard ([4e24345](https://github.com/Ironsail-llc/genus-os/commit/4e24345e0a3cbb27bd916e1cdf392a39d356d4fc))
* add Google Workspace CLI (gws) integration — 8 native engine tools ([6702e24](https://github.com/Ironsail-llc/genus-os/commit/6702e2426d211b060104e73dd3cffee5a9419848))
* add gws_gmail_reply tool to fix email threading + close leaked coroutines in RLM tests ([46336d3](https://github.com/Ironsail-llc/genus-os/commit/46336d32aab711872afc6e609fa0ebea101641af))
* add Kokoro TTS, Impetus One bridge proxy, dashboard hardening, and bridge watchdog ([7250b4e](https://github.com/Ironsail-llc/genus-os/commit/7250b4ea9c2b64378b0722606df0956b71a29fa1))
* add make_call tool — outbound phone calls for agents ([5ac960e](https://github.com/Ironsail-llc/genus-os/commit/5ac960ec899fb00447cdecac971e4596aa8e8c6d))
* add Ollama keep_alive settings and memory pressure checks ([ecf3ea8](https://github.com/Ironsail-llc/genus-os/commit/ecf3ea87938f2102fec46e2bb83f0d79ea9c6d2e))
* add Princess Freya (PF) edge node support and fix model failover ([6a0e4b9](https://github.com/Ironsail-llc/genus-os/commit/6a0e4b9ed7f811683169aa994c1cb1fed916ebdf))
* add SETUP benchmark logging to runner execute path ([34ecc90](https://github.com/Ironsail-llc/genus-os/commit/34ecc90575b1088293acee31eaff2edf95c1ef5f))
* add SETUP benchmark logging to runner execute path ([87a0e11](https://github.com/Ironsail-llc/genus-os/commit/87a0e1169495ad9ae9a755559e91f2ad259f1dce))
* add TUI — Textual terminal dashboard for engine monitoring ([ddd21af](https://github.com/Ironsail-llc/genus-os/commit/ddd21afdb8de1ce289592f729c929f884f63ddbe))
* Agent Architect — autonomous fleet evolution pipeline ([#64](https://github.com/Ironsail-llc/genus-os/issues/64)) ([0ceb4be](https://github.com/Ironsail-llc/genus-os/commit/0ceb4bec3efe0cb5160507b36283fcade6b90be2))
* agent builder enhancement — full-chain validation, wizard, eval framework, hub metadata ([72cd5d5](https://github.com/Ironsail-llc/genus-os/commit/72cd5d5a174e2a82c76f4d219443f18ca4454e39))
* Agent Engine v2 — 10 enhancements for reliability, intelligence, and safety ([3c98bfe](https://github.com/Ironsail-llc/genus-os/commit/3c98bfeb5c2fe3c74a6293223d91584f8facaffc))
* agent template system — parameterized bundles with variable resolution, CLI, and installer ([576c78b](https://github.com/Ironsail-llc/genus-os/commit/576c78b73140ac983fba40b516670cfb0a683c27))
* agent-as-code system with YAML manifests, validation, and git-tracked runtime ([e31f55e](https://github.com/Ironsail-llc/genus-os/commit/e31f55e36cc53fd8a625ad6b624d1e0488d56236))
* agentic autonomy — error recovery, helper spawning, replanning, progress tracking ([9963db3](https://github.com/Ironsail-llc/genus-os/commit/9963db3587e06bcdd2e46f5efa882b97a18bfca8))
* apply local patches to gateway and remove comms symlink ([650083c](https://github.com/Ironsail-llc/genus-os/commit/650083c16f1defb891aae77613926f0f9189ec68))
* audit completeness + tiered data retention ([47cc8d2](https://github.com/Ironsail-llc/genus-os/commit/47cc8d24f49954779aa1d84a6a3e9e1d64ff7020))
* audit enrichment — user_id as first-class audit column ([a4677b9](https://github.com/Ironsail-llc/genus-os/commit/a4677b9549b58419708951338d6b774abec515a8))
* AutoAgent — meta-agent harness optimization via benchmarks ([#59](https://github.com/Ironsail-llc/genus-os/issues/59)) ([fad6baf](https://github.com/Ironsail-llc/genus-os/commit/fad6baf27c9f3d699315beebacaf66d660687001))
* autonomous skill creation — agents create and improve skills ([5d0d0e8](https://github.com/Ironsail-llc/genus-os/commit/5d0d0e8861daabd6453e021e307f8261dbb9921e))
* autoresearch — iterative business metric optimization agent ([e761ea3](https://github.com/Ironsail-llc/genus-os/commit/e761ea346a92bfc5453f63df68c55465f0b9009b))
* browser automation upgrade — DOM distillation, ref resolution, vision fallback ([fa7a3d0](https://github.com/Ironsail-llc/genus-os/commit/fa7a3d0e3b0676265041d66d53b5234d9b4b3cba))
* Claude Code parity — skills, CLI, IDE WebSocket, MCP client ([c4c8313](https://github.com/Ironsail-llc/genus-os/commit/c4c831300a7037d92554e4bc555c078dfaad0e68))
* Claude Managed Agents integration package ([ac05ccb](https://github.com/Ironsail-llc/genus-os/commit/ac05ccb056c2b0a9e836e9fd71ac23d38b5f239e))
* CLAUDE.md overhaul + consolidate brain JS servers into Python engine ([53da2c1](https://github.com/Ironsail-llc/genus-os/commit/53da2c1cca8d6414a756442babc0b6382dd5dac5))
* complete pre-fork hardening — 6 phases, 814+ tests, FORK READY ([4c70e92](https://github.com/Ironsail-llc/genus-os/commit/4c70e9295b74b18cffed0e840efd75a09893a0e6))
* comprehensive CI pipeline — 7 parallel jobs, security scanning ([4566e0e](https://github.com/Ironsail-llc/genus-os/commit/4566e0eacaf63ef8aa84a948cf8f05f5df185b15))
* consolidated Nightwatch — single nightly autonomous improvement agent ([c01e16e](https://github.com/Ironsail-llc/genus-os/commit/c01e16e2e32031bc4738868aa1b6fd4056e644c2))
* context optimization + model registry + RLM deep research ([98be813](https://github.com/Ironsail-llc/genus-os/commit/98be813bd5b75ca57a94bd14e89c3dcdcd2659ab))
* Conway + KAIROS + Buddy — always-on agent platform upgrade ([#57](https://github.com/Ironsail-llc/genus-os/issues/57)) ([d1330d9](https://github.com/Ironsail-llc/genus-os/commit/d1330d9c2abc294019d82dcdb07d2eb37b0ed493))
* CRM as source of truth, circuit breaker recovery, guardrail fix, dedup generalization ([1adc7d4](https://github.com/Ironsail-llc/genus-os/commit/1adc7d469371af841fb1e7fb8399c9dab89c1b62))
* deep mode always plans first + deep reasoning engine endpoints ([0e54eda](https://github.com/Ironsail-llc/genus-os/commit/0e54edaa3436478347689013a9c1a867b2d9da06))
* **deploy:** wire staging rollout + label-driven deploy + prod bump ([#131](https://github.com/Ironsail-llc/genus-os/issues/131)) ([fb5ce63](https://github.com/Ironsail-llc/genus-os/commit/fb5ce635b825fa0ada2ac519a205b4ea55cbf503))
* devops report pipeline + multi-tenant telegram + benchmark summary cron ([a4455f6](https://github.com/Ironsail-llc/genus-os/commit/a4455f60a4a24e9fcb9b2c75bcd454ded0e2e1fd))
* eager tool result compression + context offloading ([aaa29bd](https://github.com/Ironsail-llc/genus-os/commit/aaa29bd05492cc8addf66c9ef07a3df83f126723))
* end-to-end agent pipeline — workflow engine, tool fixes, validation ([987e8db](https://github.com/Ironsail-llc/genus-os/commit/987e8dbee0f4d21674172fe4b9f8265fdb8065b8))
* engine enhancements — session export, plan mode improvements ([7006fb1](https://github.com/Ironsail-llc/genus-os/commit/7006fb18efb087adaa1d50acd81b0e47a4695014))
* engine freeze detection — sd_notify watchdog + external health monitor ([89f33e3](https://github.com/Ironsail-llc/genus-os/commit/89f33e3337018fb73c755fa61c4919827054758c))
* engine hardening — session warmth, robustness, context management, observability ([260beb2](https://github.com/Ironsail-llc/genus-os/commit/260beb2ec390fc272b03ce53dd36a409294dabc8))
* engine parity with Claude Code — cache tokens, fleet defaults, prompt caching, proactive compaction, structured streaming ([5f9a4ac](https://github.com/Ironsail-llc/genus-os/commit/5f9a4ac86daac622a33bed194622c490d3227658))
* engine tools — JIRA, GitHub, identity, metrics, report renderer ([#82](https://github.com/Ironsail-llc/genus-os/issues/82)) ([d1b6a07](https://github.com/Ironsail-llc/genus-os/commit/d1b6a0758fe62689a6bbd4df9e66c5da8f4e7cc1))
* **engine:** add Codex subscription provider ([#125](https://github.com/Ironsail-llc/genus-os/issues/125)) ([7259ddd](https://github.com/Ironsail-llc/genus-os/commit/7259ddd5c5bf73a4f7c4f9f1a56c5fc0624aa482))
* **engine:** benchmark grading + delivery/escalation/observability ([636e975](https://github.com/Ironsail-llc/genus-os/commit/636e975d34e49576cc3248bbc5d2f1e9c23deed5))
* **engine:** GWS fixes, devops report pipeline, benchmark sandbox + connector bridge ([#120](https://github.com/Ironsail-llc/genus-os/issues/120)) ([50c5efe](https://github.com/Ironsail-llc/genus-os/commit/50c5efe90b8661c97306996aeae6ae2ef600f55b))
* **engine:** thread planner on by default + observability ([#126](https://github.com/Ironsail-llc/genus-os/issues/126)) ([bd85ed0](https://github.com/Ironsail-llc/genus-os/commit/bd85ed025e8f7a22ea16fb517056aec59a55d622)), closes [#124](https://github.com/Ironsail-llc/genus-os/issues/124) [#124](https://github.com/Ironsail-llc/genus-os/issues/124)
* **engine:** Tier 1 upgrade — Hermes pattern rips (Phase 0 + 9 rips) ([#137](https://github.com/Ironsail-llc/genus-os/issues/137)) ([b915307](https://github.com/Ironsail-llc/genus-os/commit/b915307f25cd6608d8485212114fbee632ce0921)), closes [#136](https://github.com/Ironsail-llc/genus-os/issues/136) [#120](https://github.com/Ironsail-llc/genus-os/issues/120)
* enterprise engine upgrade — 6 features for sustained autonomous coding ([#53](https://github.com/Ironsail-llc/genus-os/issues/53)) ([b1d43d6](https://github.com/Ironsail-llc/genus-os/commit/b1d43d6ec01e3c4bb9f2f5ab47e317e2ba125020))
* enterprise hardening — 8-phase reliability, observability, and quality overhaul ([#60](https://github.com/Ironsail-llc/genus-os/issues/60)) ([8e0b91f](https://github.com/Ironsail-llc/genus-os/commit/8e0b91f79b118c6b2b5480f92ce3d0ebc199193f))
* event-driven hooks, agent manifests, and validation improvements ([35ac695](https://github.com/Ironsail-llc/genus-os/commit/35ac6958c8fce1e43fd3dc2b5f2d053ed420dd44))
* fast Telegram alerts for important emails via supervisor relay ([5c48c50](https://github.com/Ironsail-llc/genus-os/commit/5c48c50076cc47eca14dd30a93a921bd02d50033))
* federation package — peer-to-peer instance networking ([e357653](https://github.com/Ironsail-llc/genus-os/commit/e3576531638df59a88e87fcf4511baeaa9bc2565))
* gap closure — parallel scale, hooks, teams, sandbox, marketplace, compliance ([b3be16d](https://github.com/Ironsail-llc/genus-os/commit/b3be16ddb540c12f164e8e9e121586354236c1f7))
* Genus OS platform separation — remove instance data, genericize identity ([1ab49c8](https://github.com/Ironsail-llc/genus-os/commit/1ab49c8101ccac697d2bbb79aa4c17d48d03d29e))
* goal-driven self-improvement + experiment lock resilience ([#96](https://github.com/Ironsail-llc/genus-os/issues/96)) ([41ec5b4](https://github.com/Ironsail-llc/genus-os/commit/41ec5b468a0e97af7599b8794b30bd2d9ab00967)), closes [#97](https://github.com/Ironsail-llc/genus-os/issues/97)
* goal-oriented self-improvement + agent reviews ([#93](https://github.com/Ironsail-llc/genus-os/issues/93)) ([7d80b5c](https://github.com/Ironsail-llc/genus-os/commit/7d80b5cf08c71c1bde03b95d81cc699659d793b1))
* graduated fact-preserving context compaction ([f9fcfe4](https://github.com/Ironsail-llc/genus-os/commit/f9fcfe47c219604751c341e208e2e9bf68a82294))
* heartbeat → hourly status report with time context ([#10](https://github.com/Ironsail-llc/genus-os/issues/10)) ([fc081f9](https://github.com/Ironsail-llc/genus-os/commit/fc081f9879003c3e87ace22b707e66d4eec135d3))
* heartbeat overhaul + requires_human flag — task-centric reports, no silent auto-close ([664e494](https://github.com/Ironsail-llc/genus-os/commit/664e494cf4b37e31b6f8bc966a2021c75e0e47bd))
* Helm UI refresh + CI pipeline + README rewrite ([9fd8fac](https://github.com/Ironsail-llc/genus-os/commit/9fd8facaa9a73e6d0801e8cda95ed7dad6eb7bce))
* **helm:** add genus-os Helm chart with unit tests, lint and schema CI ([#130](https://github.com/Ironsail-llc/genus-os/issues/130)) ([954611d](https://github.com/Ironsail-llc/genus-os/commit/954611db4ace369a53027a0c5a748c39ebc2d8d0))
* **helm:** dynamic owner identity via ROBOTHOR_OWNER_NAME env var ([6b07ab4](https://github.com/Ironsail-llc/genus-os/commit/6b07ab415f7b976f6192e67454effbf893e6840d))
* **helm:** improve canvas rendering, error reporting, and throttle ([f4f7cff](https://github.com/Ironsail-llc/genus-os/commit/f4f7cff1b5a15fd0396a12fdb189f7eb1a9e631d))
* **helm:** LLM-friendly DataTable props and error reporting in renderer ([65ef740](https://github.com/Ironsail-llc/genus-os/commit/65ef740a5a7c62d8835983565390286c8d0d472a))
* hierarchical tenant access — parent reads child data ([b3ce746](https://github.com/Ironsail-llc/genus-os/commit/b3ce746b23504cf1595db24d41d97362f2479f0a))
* import OpenClaw as gateway subsystem ([f084bbe](https://github.com/Ironsail-llc/genus-os/commit/f084bbee08b9fd3ad6c010ea3542c52968ecc317))
* in-conversation todo list system (Claude Code parity) ([#56](https://github.com/Ironsail-llc/genus-os/issues/56)) ([4d9a3d5](https://github.com/Ironsail-llc/genus-os/commit/4d9a3d55ab0e713ed797467799d648cc52ee7f22))
* internalized intelligence — KG growth, curiosity engine, self-model, event bus ([#92](https://github.com/Ironsail-llc/genus-os/issues/92)) ([8171622](https://github.com/Ironsail-llc/genus-os/commit/8171622f8c8dbcdca41e8c13a4ccfa052780a39c))
* learning loop — structured user model + run outcome assessment ([d1bad2b](https://github.com/Ironsail-llc/genus-os/commit/d1bad2bb655adab8be14dded04a99118332068c8))
* leverage OpenClaw 2026.2.25 — cron health, maxConcurrent, schedule fixes ([806ff95](https://github.com/Ironsail-llc/genus-os/commit/806ff957b3ce26180b3ea51485b1c7b241bc624b))
* make infrastructure first-class — vault, profiles, scaffold, tunnel ([91124ce](https://github.com/Ironsail-llc/genus-os/commit/91124cec3e73d4cfbb6e778d2f473b01ac90e434))
* Memory consolidation — kill dual code paths, single canonical package ([be9bdfd](https://github.com/Ironsail-llc/genus-os/commit/be9bdfd7252a9ac502f15d7cd119859eebcbcae6))
* memory system v4.2 — intra-day consolidation, cross-domain insights, fix broken pipeline ([55f023d](https://github.com/Ironsail-llc/genus-os/commit/55f023da0fe2b0b56887249aac4d1d3bd30c8980))
* Memory v4 — hybrid search, quality gates, interactive warmup, DAL consolidation ([fc33bcc](https://github.com/Ironsail-llc/genus-os/commit/fc33bccb9b1229d5a14747a606d84d8b428bcc78))
* merge complete robothor package into main repo — single repo, 441 tests ([5bfd3a6](https://github.com/Ironsail-llc/genus-os/commit/5bfd3a6804a757b90d3b826851fb7191bde894f8))
* merge supervisor into main agent + lint fixes ([ef57545](https://github.com/Ironsail-llc/genus-os/commit/ef57545b7dda1d65302db3eb0fdb51b71146590a))
* multi-platform delivery — registry pattern + Slack integration ([96d5f2a](https://github.com/Ironsail-llc/genus-os/commit/96d5f2af8126ac8b8b8fbf1200ecccf80899945a))
* multi-tenancy platform, memory hardening, and Helm app-shell ([9c4b992](https://github.com/Ironsail-llc/genus-os/commit/9c4b9929dc66eb8432adf191c63b8a8d7144929c))
* NemoClaw-inspired onboarding, template library, and UX improvements ([19eee68](https://github.com/Ironsail-llc/genus-os/commit/19eee686b15d9ecad036c9a810bb065173d911e8))
* nested sub-agent system — spawn_agent and spawn_agents tools ([8899a65](https://github.com/Ironsail-llc/genus-os/commit/8899a656d0e621b407a807204c5ad02f7982676e))
* Nightwatch Claude Code worktrees — self-healing + self-improving via isolated git worktrees ([732dda6](https://github.com/Ironsail-llc/genus-os/commit/732dda6e969dc2a51b77a662637f8b3058be95b5))
* Nightwatch self-improving agent system — git tools, analytics, overnight PRs ([b461417](https://github.com/Ironsail-llc/genus-os/commit/b461417b614f10b4ccbf612f72e18d9168dbb3d4))
* observability quick wins — delivery_status, tool event tracking, enriched health check ([1fdfc41](https://github.com/Ironsail-llc/genus-os/commit/1fdfc41acf893c4bccc71dc25ca2815f2b6b6b7f))
* operator identity — one source of truth for who owns this instance ([#97](https://github.com/Ironsail-llc/genus-os/issues/97)) ([e757033](https://github.com/Ironsail-llc/genus-os/commit/e757033bba9bad9d6a9960099f1a7ae88b28b508))
* overhaul heartbeat from passive monitor to active CEO ([2059e16](https://github.com/Ironsail-llc/genus-os/commit/2059e167663d517ee598f534d6dc03c7fbcc41c0))
* per-agent RPG scoring — Buddy + Helm + AutoAgent merge ([#63](https://github.com/Ironsail-llc/genus-os/issues/63)) ([b6a53d4](https://github.com/Ironsail-llc/genus-os/commit/b6a53d43c4093f1dca1ee4c4fd9a04992e82e1bb))
* persistent chat history for Telegram & webchat ([f35a0fe](https://github.com/Ironsail-llc/genus-os/commit/f35a0fe5e88bf96730a698a1b0e9670da76d80e2))
* photo-based face enrollment + presence-aware alert suppression ([8cae6bb](https://github.com/Ironsail-llc/genus-os/commit/8cae6bbbee97fbb0007ba0f159a7850671e65eb4))
* plan mode — explore before executing with approval workflow ([#8](https://github.com/Ironsail-llc/genus-os/issues/8)) ([32fa246](https://github.com/Ironsail-llc/genus-os/commit/32fa2461c2e2b6bbef4b1dcbea705d71931b158f))
* platform cohesion — CLAUDE.md layering, leak prevention, upgrade command ([#80](https://github.com/Ironsail-llc/genus-os/issues/80)) ([e85a4aa](https://github.com/Ironsail-llc/genus-os/commit/e85a4aa335e2e7a798bb86a403c44b8335bbc735))
* platform import/export + client onboarding ([883552f](https://github.com/Ironsail-llc/genus-os/commit/883552fb19b127c9cac503cb188b75121cead138))
* prompt injection defenses — exec allowlist, write path restrict, content tagging ([6464434](https://github.com/Ironsail-llc/genus-os/commit/64644340fd2fdab6e7435be64e6e73fa6cdc53b2))
* prune stale agent_schedules on startup + status update ([409d6bc](https://github.com/Ironsail-llc/genus-os/commit/409d6bc11cd1db867c89aded857d540695ea3785))
* replace hardcoded Impetus One with generic business adapter system ([f09a488](https://github.com/Ironsail-llc/genus-os/commit/f09a488a2a4dc510c2f9fd6398926d0d6b42f8fd))
* role-based permission enforcement — single gate in dispatch ([ec64b35](https://github.com/Ironsail-llc/genus-os/commit/ec64b354371c66fa675f03f2e4500e586ae58b7b))
* session goal — DAL-backed long-running operator objectives ([6ccb732](https://github.com/Ironsail-llc/genus-os/commit/6ccb7327023d6e7e802d23bd9b3729dc0a89167b))
* smart meeting scheduling pipeline — auto-schedule or booking link ([22d4483](https://github.com/Ironsail-llc/genus-os/commit/22d4483803298762f9e800b7014471fcfa91442a))
* smart meeting scheduling with booking link tracking + mandatory Google Meet ([1bd30a5](https://github.com/Ironsail-llc/genus-os/commit/1bd30a5a4a6f883bebecbfae6dfc832d3f730265))
* SOTA memory system — episodes, procedures, preferences, chat, outcomes, breadcrumbs ([#98](https://github.com/Ironsail-llc/genus-os/issues/98)) ([3c1e108](https://github.com/Ironsail-llc/genus-os/commit/3c1e1082343056943abda9b2182457f86a1835d8)), closes [#97](https://github.com/Ironsail-llc/genus-os/issues/97) [#97](https://github.com/Ironsail-llc/genus-os/issues/97)
* stage-1 thread pool — ranked view of open multi-beat work ([e5090b8](https://github.com/Ironsail-llc/genus-os/commit/e5090b849350e145d397b7001111cb14b4da5ed6))
* stage-2 verification — acceptance blocks + pending-marker filter ([9423c3b](https://github.com/Ironsail-llc/genus-os/commit/9423c3b5d956ffd356d25bcc05110d43f63e1a93))
* stage-3 stall classifier + auto-sweep + collaboration pings ([daf84e0](https://github.com/Ironsail-llc/genus-os/commit/daf84e08a132ea81814954ce8ec5b823ccc31ac3))
* static analysis phase 2 — near-strict mypy + 6 new ruff rules ([7e77e63](https://github.com/Ironsail-llc/genus-os/commit/7e77e63b19baa88b8ca7178d8e1f68f56866049c))
* streaming UX — kill dead air during tool execution and multi-step runs ([7ce910e](https://github.com/Ironsail-llc/genus-os/commit/7ce910e6a76850ef8cee71b2158d8fd6e25bd5a6))
* switch local generation to Nemotron 3 Super, fix model registry tests ([e8fb1b1](https://github.com/Ironsail-llc/genus-os/commit/e8fb1b14d97aa0586c72c8c50ee9e5a5f5590b89))
* switch local generation to Nemotron 3 Super, fix model registry tests ([77b41ac](https://github.com/Ironsail-llc/genus-os/commit/77b41ac893f1d6ea92f543cdf33ff2d980b84cda))
* thread user identity through execution pipeline ([313efbb](https://github.com/Ironsail-llc/genus-os/commit/313efbbcaba33a57370d201fd399690e8ace6214))
* timeout diagnosis & detection overhaul ([2077e65](https://github.com/Ironsail-llc/genus-os/commit/2077e650ae659b91aca9191c4eb8639cb05e8bff))
* triage inbox pipeline, hub client API, CI improvements, guardrail allowlists ([07e6d20](https://github.com/Ironsail-llc/genus-os/commit/07e6d20f3eefd5db592ecf10ca4bc52cb4ad0081))
* unified agent goals — merge session_goal + manifest goals + thread pool ([d20a06e](https://github.com/Ironsail-llc/genus-os/commit/d20a06ee19531327d37bc2bee17a927463a0746f))
* unified session — Telegram + Helm share one conversation ([#7](https://github.com/Ironsail-llc/genus-os/issues/7)) ([34a37f0](https://github.com/Ironsail-llc/genus-os/commit/34a37f0ac03c8a0bb9faa2fb0cfc845b7ec59f75))
* unify Robothor — gateway manager, CLI, config gen, migration ([3f38fe1](https://github.com/Ironsail-llc/genus-os/commit/3f38fe1e5bda05467a26c8e995c242c696493b75))
* user identity resolution — bot knows who it's talking to ([5c94a25](https://github.com/Ironsail-llc/genus-os/commit/5c94a25a90f037b4518c879fa7f7e2d0d00f8111))
* VLM photo analysis, DB url helper, multi-tenant user lookup, guardrail + live-goal gate ([7aa5f23](https://github.com/Ironsail-llc/genus-os/commit/7aa5f234236d745b4de61daafaa53184a0e25507))
* wire Helm chat to Agent Engine — replace OpenClaw WebSocket with HTTP/SSE ([c3ccda7](https://github.com/Ironsail-llc/genus-os/commit/c3ccda7e79885817e54d813a25a3ed24fbed6c98))

### Bug Fixes

* add "sleeping" health tier for agents outside their active cron window ([7471e73](https://github.com/Ironsail-llc/genus-os/commit/7471e73aa13b0f447e706853d78096486a975e78))
* add per-chunk timeout to streaming loop — stalled streams now fall back ([3172df7](https://github.com/Ironsail-llc/genus-os/commit/3172df713de1a107b8d90ff9212d927f8ca4c643))
* add Telegram conversation history, empty choices guard, dead tool cleanup ([0ec26ff](https://github.com/Ironsail-llc/genus-os/commit/0ec26ff884e6d7cc65bc07dfc7f5de39dd7d8185))
* add type annotations to test message lists for mypy ([716d393](https://github.com/Ironsail-llc/genus-os/commit/716d3934099bf53aaae13c05c85b248e18ea7674))
* audit and fix package README, pyproject.toml, CLI stubs, and RBAC tests ([0fc3e5c](https://github.com/Ironsail-llc/genus-os/commit/0fc3e5c376115a78f5647c8c98d1f16cf775a3f0))
* auto-add Philip as attendee on all calendar events ([c9d68ee](https://github.com/Ironsail-llc/genus-os/commit/c9d68eea94ecf4f23c7d001fd9b7d9b8fca5d493))
* canary agent timeout 120→300s — planning overhead needs headroom ([099eb15](https://github.com/Ironsail-llc/genus-os/commit/099eb1584480f7e7b34d5be1b0552fe28e3a963c))
* case-insensitive email matching in Gmail reply/send duplicate guards ([a983a2f](https://github.com/Ironsail-llc/genus-os/commit/a983a2fc2f145eb3efdf6e567667f8c43bb19035))
* CI — mypy no-any-return in tui, stub fetch in visual-panel test ([0275933](https://github.com/Ironsail-llc/genus-os/commit/0275933726d1f1b2031a2a9f2a14d0e7bf3e8774))
* CI green — mypy errors, test discovery, missing RBAC manifest ([e69bec6](https://github.com/Ironsail-llc/genus-os/commit/e69bec662f5d7e648a4e0dc65882127abae8fe2c))
* CI green — skip manifest-dependent tests in clean checkout ([cc7ee5b](https://github.com/Ironsail-llc/genus-os/commit/cc7ee5bd501c7919e0eae4f4dece292e62a3123c))
* CI pipeline — gitleaks CLI, pnpm 10, narrow bridge tests ([0a69d94](https://github.com/Ironsail-llc/genus-os/commit/0a69d94bd910f62274db74d281b3f8537aa8d956))
* **ci:** CI-only failures + mypy 2.0 buddy_auditor ([eb0dd43](https://github.com/Ironsail-llc/genus-os/commit/eb0dd436f42831e5fccf127aa224b0f1bafe73c3))
* **ci:** restore green pipeline after 2026-04-21 consolidation drift ([2ee2f2b](https://github.com/Ironsail-llc/genus-os/commit/2ee2f2bfac4d33ad19c7172228ba879763588d3d))
* **claude:** remove out-of-bounds symlink blocking ArgoCD sync ([#132](https://github.com/Ironsail-llc/genus-os/issues/132)) ([e926132](https://github.com/Ironsail-llc/genus-os/commit/e926132a7732a26f84c3d3b68dd996ab9c1410d9)), closes [#131](https://github.com/Ironsail-llc/genus-os/issues/131)
* clean up remaining clawd paths after brain migration ([9f60353](https://github.com/Ironsail-llc/genus-os/commit/9f603535260f958a80c2d493e332ee47218eb2e6))
* close cross-tenant memory data leak ([37b85fe](https://github.com/Ironsail-llc/genus-os/commit/37b85fefc647f3d07ce1ffd53e67afd05ab61d59))
* complete Nightwatch pipeline — review tasks, safety timeouts, LLM timeouts ([47f5d49](https://github.com/Ironsail-llc/genus-os/commit/47f5d4971eaa491007acef2e3ec87a6b6768c0ba))
* correct sla_deadline → sla_deadline_at column name in list_tasks_summary ([f192b71](https://github.com/Ironsail-llc/genus-os/commit/f192b7143cad57ca00a7e48e7bbe52a2a8f1cd10))
* CRM bridge failures, calendar false positives, Helm SSE robustness ([857e588](https://github.com/Ironsail-llc/genus-os/commit/857e5882b1f1edb625eb8dd6b6e903c9d3ab82ca))
* **crm:** task system data contract — history kind enum, autonomy val… ([#124](https://github.com/Ironsail-llc/genus-os/issues/124)) ([02f4fe2](https://github.com/Ironsail-llc/genus-os/commit/02f4fe259adacffa1d9e9c7a34e146f3fb787b7b))
* deep_reason RLM tool — correct constructor API, route models via OpenRouter ([a0d2b1e](https://github.com/Ironsail-llc/genus-os/commit/a0d2b1ec463f2325d463c170ce8eb2d9296ab302))
* deterministic email pipeline — event-driven workflow + metadata backfill ([70ad3f6](https://github.com/Ironsail-llc/genus-os/commit/70ad3f6400d4e6a63b14a152eec016e0644720db))
* disable Nightwatch agents + fix broken cost tracking ([39bd13c](https://github.com/Ironsail-llc/genus-os/commit/39bd13cf681f1ca850bc31679b5358a2d2a9eced))
* eliminate mass zombie events — timeout scope + async DB init + reaper completeness ([fd860a7](https://github.com/Ironsail-llc/genus-os/commit/fd860a7d67b05f502129686adae8d6feb325274b))
* eliminate Telegram rate-limit delays — stop stream-editing, deliver final ([13973b7](https://github.com/Ironsail-llc/genus-os/commit/13973b76f2baff096763a20db6539f25cfe153e0))
* email pipeline — concurrency dedup, notification constraint, truncation safety ([ebfecd7](https://github.com/Ironsail-llc/genus-os/commit/ebfecd76d9cb347d279230b08b012186163ac9fe))
* email pipeline — correct gog commands, write urgency/category, expand validation ([b514a33](https://github.com/Ironsail-llc/genus-os/commit/b514a33e094fc3be242ca57cf30bc74a9a27aa09))
* extract email constant, hoist regex, log guard errors, deprecate send threading params ([345fe16](https://github.com/Ironsail-llc/genus-os/commit/345fe16b16d28b098cb627a05cb46ca99b66d9e2))
* heartbeat resurfacing stale/resolved issues — engine-level task dedup + escalation query ([8612b72](https://github.com/Ironsail-llc/genus-os/commit/8612b72b6acf79bc9e0dde9bd8adbe533783b0a7))
* heartbeat Telegram delivery — env var fallback chain + unexpanded-var guard ([9bfef03](https://github.com/Ironsail-llc/genus-os/commit/9bfef031cf373c63adcf0fb16c1acf986c1967a6))
* Helm chat reliability + mobile responsive layout ([9a93dc5](https://github.com/Ironsail-llc/genus-os/commit/9a93dc599f5354d66004056c4f4bd1587f2f7deb))
* Helm dashboard — welcome JSON parse crash, chart quote validation, system prompt guard ([6a21dec](https://github.com/Ironsail-llc/genus-os/commit/6a21decb1245d956a0b8c63f7e566387a7f98f8e))
* Helm dashboard error handling ([#44](https://github.com/Ironsail-llc/genus-os/issues/44)) ([7975697](https://github.com/Ironsail-llc/genus-os/commit/797569760ccc347766e03d090ad4e0a9699e1ca5))
* Helm mobile UX overhaul — chat-first, fixed tab bar, responsive grids ([7e8f8fe](https://github.com/Ironsail-llc/genus-os/commit/7e8f8fe975d1f3e423cf1c42c402f2faeab4f295))
* improve GitHub stats — merged_by tracking, week-over-week, draft exclusion ([f7cd900](https://github.com/Ironsail-llc/genus-os/commit/f7cd900bfe7f86bf0d0f90d96c9057da4144ef00))
* increase agent timeoutSeconds to prevent model fallback chain exhaustion ([291efca](https://github.com/Ironsail-llc/genus-os/commit/291efcabdaa9620058dacc4bb65ccfb9db7443fa))
* increase pytest timeout to 30s for CI runner compatibility ([5a6558d](https://github.com/Ironsail-llc/genus-os/commit/5a6558de435f8470c36762064ae9b8636a152d7b))
* increase test_total_limit margin for security preamble + time context overhead ([d308ddd](https://github.com/Ironsail-llc/genus-os/commit/d308dddc4ab20cb3bbf1b376d8a9f76864ca0339))
* lint + format — import sorting, ruff format on 5 files ([6eefbb3](https://github.com/Ironsail-llc/genus-os/commit/6eefbb3705cfd19dd853c13a6532711f608637cd))
* loosen model preference test for nemotron-3-super generation model ([9601310](https://github.com/Ironsail-llc/genus-os/commit/96013101449a24791fed3e7dfffacb6f9f4f99eb))
* main agent fallback order — Kimi K2.5 before Gemini Pro ([8a5c6cb](https://github.com/Ironsail-llc/genus-os/commit/8a5c6cb6e41deca03aa6c318a9bd788ae21c9950))
* mark vision tests to prevent CI runner OOM ([6ca09cf](https://github.com/Ironsail-llc/genus-os/commit/6ca09cfb40fe69c6cf292d910a57f70a12cc8bca))
* mock Redis lock in autodream tests to prevent stale-lock failures ([2539320](https://github.com/Ironsail-llc/genus-os/commit/253932098c3440a726a900786910bd93bc49f41e))
* model fallback reliability + correct agent model assignments ([26d5fe8](https://github.com/Ironsail-llc/genus-os/commit/26d5fe8cfd9c01851c6ba39033088a75466f9866))
* morning briefing + evening wind-down agents — instruction_file, model, tools, warmup ([ae19b0c](https://github.com/Ironsail-llc/genus-os/commit/ae19b0c0eff9ddbe54bb9acdf55e0eac085382ae))
* mypy no-any-return errors in lifecycle.py ([23e2e19](https://github.com/Ironsail-llc/genus-os/commit/23e2e19b36dc3146f100ce4afefb301aeed8d1c3))
* ON CONFLICT safety for batch inserts, remove dead streaming code, add tests ([be4a526](https://github.com/Ironsail-llc/genus-os/commit/be4a5265660b075cfa03b8602614579813705084))
* overnight stability — tenant-aware blocks, missing columns, watchdog tuning ([#84](https://github.com/Ironsail-llc/genus-os/issues/84)) ([68141e1](https://github.com/Ironsail-llc/genus-os/commit/68141e1d6cd1da4c3810d77b4b99e6186cff8fc2))
* periodic schedule reconciliation — prevent ghost agents in Helm ([f77e000](https://github.com/Ironsail-llc/genus-os/commit/f77e0003b3da2eb648b11a67fc95c8a39e4331c4))
* plan mode context reset — prevent re-planning on approval ([0002bb1](https://github.com/Ironsail-llc/genus-os/commit/0002bb1eac2bded101993f588b2b3c39b7230fbc))
* plan mode sandwich pattern — prepend constraints before SOUL.md ([1501d9c](https://github.com/Ironsail-llc/genus-os/commit/1501d9c018ed5777770817903049ba2c762fe74b))
* post-implementation review — critical thinking API fix + 12 quality improvements ([8e57363](https://github.com/Ironsail-llc/genus-os/commit/8e573636eadd2b73269b0c2d650b8abe7a7a71d2))
* post-Nightwatch hardening — bootstrap status files + explicit briefing tools ([3b4027a](https://github.com/Ironsail-llc/genus-os/commit/3b4027a04fa9b0ed47cdc0d4325a1752686d4cb7))
* Qwen 3.5 → GLM-5 migration, fix CI failures (mypy + test_setup) ([58ed2fe](https://github.com/Ironsail-llc/genus-os/commit/58ed2fe2161ed2fc0bdd34254f279a1f8d70748e))
* recovery helper spawning in CLI mode — use self.execute() directly ([976ac20](https://github.com/Ironsail-llc/genus-os/commit/976ac20ecd61ebdf925bc3d7746738b169e2432e))
* remove dead moltbot-gateway and triage_worker from health checks and docs ([c03e0e3](https://github.com/Ironsail-llc/genus-os/commit/c03e0e39baae487d6656b231876e10d2635034fd))
* remove duplicate type annotation for mypy strict mode ([f7656e0](https://github.com/Ironsail-llc/genus-os/commit/f7656e059219660ad52dcda08cc75f33f37a3db8))
* remove hard timeout — stall watchdog is primary protection ([452d3d2](https://github.com/Ironsail-llc/genus-os/commit/452d3d2e8c3a84e4946667d4174b5ee8bf7f66cd))
* remove HEARTBEAT_OK pattern from codebase — heartbeats always produce substantive reports ([093742d](https://github.com/Ironsail-llc/genus-os/commit/093742dd91adf1c48a311043f4850727562d1f2a))
* remove Impetus One from bridge health check, move MCP client to engine ([d0deab5](https://github.com/Ironsail-llc/genus-os/commit/d0deab57eaca732010ac09a88c210288463c59d8))
* remove tunnel symlink + gate staging-reset's Tailscale/sync ([#134](https://github.com/Ironsail-llc/genus-os/issues/134)) ([08912b5](https://github.com/Ironsail-llc/genus-os/commit/08912b56080aa4cf24e6cf1b9f1fb9b8b6cde57f)), closes [#131](https://github.com/Ironsail-llc/genus-os/issues/131)
* remove unused type-ignore comment flagged by CI mypy ([86a8e37](https://github.com/Ironsail-llc/genus-os/commit/86a8e379f5ebebe01a86830003ea0685d9dd2728))
* replace hard iteration/budget caps with smart loop control ([6c3ace6](https://github.com/Ironsail-llc/genus-os/commit/6c3ace64f0407239cb1c65a28cb38698adfbac97))
* repo-wide lint cleanup — ruff auto-fixes + per-path ignores ([#6](https://github.com/Ironsail-llc/genus-os/issues/6)) ([621d0fa](https://github.com/Ironsail-llc/genus-os/commit/621d0fa1fa3bfebff7b80a1b0de17a01be0e7d2a))
* requiresHuman task resolution via Telegram + Helm badge + health probes + catch-up scheduling ([6602a25](https://github.com/Ironsail-llc/genus-os/commit/6602a25a7c1c031b01fdb35da8d242867b45d5af))
* resolve 51 Dependabot vulnerabilities in app dependencies ([74a2f59](https://github.com/Ironsail-llc/genus-os/commit/74a2f5966e6b9f9658d89f4a28ed848127ffe04b))
* resolve all CI failures — mypy (74→0 errors) and TypeScript (4→0 errors) ([0a0efaa](https://github.com/Ironsail-llc/genus-os/commit/0a0efaaad344ee58152050163f66966a5adebbcf))
* resolve all CI failures — mypy typecheck + frontend TypeScript ([f678816](https://github.com/Ironsail-llc/genus-os/commit/f678816c4448d34a2f85a9f025cf3aa09a46321c))
* resolve all CI failures — ruff lint, frontend test mocks ([d26fc9f](https://github.com/Ironsail-llc/genus-os/commit/d26fc9f8e5f9356bfd16540163d763a0832513fe))
* resolve all mypy errors across engine, templates, and context ([1def0b0](https://github.com/Ironsail-llc/genus-os/commit/1def0b05040fb0bb38fb0e1865857e3861d4af1b))
* resolve CI failures — mypy types, ruff config, test scoping ([d10077f](https://github.com/Ironsail-llc/genus-os/commit/d10077fa9fbe9d689f94d2d326d7de7f3dc96063))
* resolve computer-use agent tool_use_id orphaning — mixed format, validation, screenshot visibility ([486ffa4](https://github.com/Ironsail-llc/genus-os/commit/486ffa498d7ba87a961cff15dd2ec82271ae0622))
* resolve frontend ESLint errors and TypeScript issues ([e7acba7](https://github.com/Ironsail-llc/genus-os/commit/e7acba78006e4347fbdfa8e510e349baa77d5b1d))
* resolve mypy arg-type errors in browser ref resolution ([dc79858](https://github.com/Ironsail-llc/genus-os/commit/dc798589f4d90200a0cd4304180ac88ca63a31b1))
* resolve mypy errors in setup.py and cli.py ([46c93e7](https://github.com/Ironsail-llc/genus-os/commit/46c93e79b3c944df28a8888c5884216216de4f27))
* resolve mypy errors, fix broken tests, manual ruff fixes ([036dafb](https://github.com/Ironsail-llc/genus-os/commit/036dafbd32d5436fe569056a5c330a74c48a17d1))
* resolve mypy no-any-return in experiment handler ([f9f5c17](https://github.com/Ironsail-llc/genus-os/commit/f9f5c179d363a9021df75c93d057165718517b77))
* resolve mypy type errors and UnboundLocalError in computer use tools ([efb8d88](https://github.com/Ironsail-llc/genus-os/commit/efb8d88106b020c2647315cf59830b9e60cc4b05))
* resolve mypy type errors from enterprise engine upgrade ([1ef255f](https://github.com/Ironsail-llc/genus-os/commit/1ef255f40562cf916cb05adb4a45bdd98ea058fa))
* resolve mypy type errors in adapter system ([4667a19](https://github.com/Ironsail-llc/genus-os/commit/4667a191aa605c7151f38e259a3dfa4a938b8efa))
* resolve mypy type errors in federation sync_status handler ([3e35262](https://github.com/Ironsail-llc/genus-os/commit/3e352629c0fd59e4a08db055dd8adb35b5093705))
* resolve mypy type errors in impetus MCP client ([dab89e1](https://github.com/Ironsail-llc/genus-os/commit/dab89e10368bbfc8659b1b638f6d8ac9e4b4ff50))
* resolve mypy type errors in LLM fallback methods ([d76b53a](https://github.com/Ironsail-llc/genus-os/commit/d76b53aaacb0102ac197cc83900c7c71f9a742e2))
* resolve mypy type errors in mcp_client and ide modules ([8475746](https://github.com/Ironsail-llc/genus-os/commit/84757467f99bae1ca55b4d4e35a9d93713aee1a0))
* resolve mypy typecheck CI failures (ultralytics attr-defined, tui no-any-return) ([14fde55](https://github.com/Ironsail-llc/genus-os/commit/14fde55d7c97b50d4303cc84064a0edb952d0e98))
* resolve pre-existing test failures (16 tests across 3 files) ([936596c](https://github.com/Ironsail-llc/genus-os/commit/936596cec4a4deff1b85939e164ffba280efaae5))
* resolved tasks resurfacing in heartbeat/briefings + Helm resolve button ([638b5c9](https://github.com/Ironsail-llc/genus-os/commit/638b5c9fd5fdef313b0798c03d8075f9b5b7af08))
* restore embedding pipeline — retry/backoff, model contention, timeouts ([aa60e26](https://github.com/Ironsail-llc/genus-os/commit/aa60e26c081ee854eb0c38dbf8c9dfc556285ea9))
* restore infrastructure deleted by Conway+KAIROS+Buddy upgrade ([#62](https://github.com/Ironsail-llc/genus-os/issues/62)) ([ba1a31d](https://github.com/Ironsail-llc/genus-os/commit/ba1a31d7532b60b83bb95d54184ebee0039d9747)), closes [#60](https://github.com/Ironsail-llc/genus-os/issues/60) [#60](https://github.com/Ironsail-llc/genus-os/issues/60)
* restore instance config layer — env files, cron wrapper, CLAUDE.md separation ([bcba061](https://github.com/Ironsail-llc/genus-os/commit/bcba061722333c50e3c5fd49b81e6ddbc742d151))
* restore nightwatch.py and nightwatch_lib.py deleted by PR [#57](https://github.com/Ironsail-llc/genus-os/issues/57) ([97c90d1](https://github.com/Ironsail-llc/genus-os/commit/97c90d1095f95e4d30976ce17172409ed3933837))
* restore runner, 0-sentinel safety_cap/max_iterations, /restart command, import canary, register deepseek-v4-pro + mimo-v2.5-pro ([d69cd34](https://github.com/Ironsail-llc/genus-os/commit/d69cd34a150ad18359925b45f484f31be878c7b4))
* revert timezone from America/Grenada to America/New_York ([01ee6b3](https://github.com/Ironsail-llc/genus-os/commit/01ee6b3b45b061104c1a48e823f22ff52e91b473))
* ruff auto-fix — remove unused imports, sort imports, reformat ([2d1f7ce](https://github.com/Ironsail-llc/genus-os/commit/2d1f7ce8ad3d5651ca440bcb27e64c7e1ba32dce))
* sanitize log inputs to resolve CodeQL py/log-injection alerts ([0b46941](https://github.com/Ironsail-llc/genus-os/commit/0b46941e5e044e1704915609a194cda83d19e949))
* **security:** exclude malicious fastapi 0.136.3 (OSV MAL-2026-4750) ([#133](https://github.com/Ironsail-llc/genus-os/issues/133)) ([a4ddf3d](https://github.com/Ironsail-llc/genus-os/commit/a4ddf3dd2924087b0ed9d83e5a16ce3657510dab)), closes [#131](https://github.com/Ironsail-llc/genus-os/issues/131)
* skip config validation tests in CI where deployment files don't exist ([2a54cc0](https://github.com/Ironsail-llc/genus-os/commit/2a54cc06623a8affdd77c829a9777e51bf00fced))
* stall watchdog gap + split crm-steward into 3 focused agents ([420f9ac](https://github.com/Ironsail-llc/genus-os/commit/420f9acd5e1ed4978ecf1760303d5a8717e9e985))
* switch local generation model from nemotron-3-super to qwen3:32b ([9cbfc30](https://github.com/Ironsail-llc/genus-os/commit/9cbfc30ff62fadedbf00f10935c8fb43f9c21bce))
* switch main agent to Sonnet 4.6 + per-agent temperature control ([45a1033](https://github.com/Ironsail-llc/genus-os/commit/45a1033ea237e035f5701d945e7a2538b9f7e7f7))
* Telegram /model command — correct model IDs and manifest-aware display ([de7a392](https://github.com/Ironsail-llc/genus-os/commit/de7a3925b4bf51ee63852d89338f69a07720352d))
* Telegram file handling + context persistence on failed runs ([00198b1](https://github.com/Ironsail-llc/genus-os/commit/00198b1716d700f72ec38793d0ee66b2ed5ec235))
* Telegram flood control — retry on rate limit instead of silently dropping ([fe2ad97](https://github.com/Ironsail-llc/genus-os/commit/fe2ad97ce9ca13f0292a9a5c382161ded0eb08fb))
* timezone-aware datetimes in vision departure tests ([c6e7396](https://github.com/Ironsail-llc/genus-os/commit/c6e73960c675e9afece052b3b2775debf6c50afd))
* token signature verification across Python versions ([1bb7788](https://github.com/Ironsail-llc/genus-os/commit/1bb7788bfba300a3b5dfb1871eb2d1448dee0237))
* Twilio API key auth — drop account_sid third arg for Standard keys ([25ae3a2](https://github.com/Ironsail-llc/genus-os/commit/25ae3a22459955fd8fbf0711a2030a31b7ea44ce))
* update all repo URLs from Ironsail-Philip to Ironsail-llc ([3d16cad](https://github.com/Ironsail-llc/genus-os/commit/3d16cadcee88ee360e853c3d42ce599a4f97b79d))
* update bridge test for tightened TODO transitions ([03ed5b5](https://github.com/Ironsail-llc/genus-os/commit/03ed5b502dfcc67f70feb65dc5b15926d853f21e))
* update Helm gateway client for OpenClaw 2026.2.19 scope security ([626b27c](https://github.com/Ironsail-llc/genus-os/commit/626b27c657ac3a8114d493ca74332c8bbbad8ad7))
* update test_app_title for Genus OS rebrand ([8a15ae7](https://github.com/Ironsail-llc/genus-os/commit/8a15ae7cdb897ae145a0df40d5abd8ac07ee5e11))
* use developer role for engine-injected context to stop false prompt injection flags ([6c25eb4](https://github.com/Ironsail-llc/genus-os/commit/6c25eb4e7afb4fa112f79116dd6a7ab3c0ce42e4))
* use SKIPPED status for dedup, extract email constant, log guard errors ([624bfb4](https://github.com/Ironsail-llc/genus-os/commit/624bfb45cd3c43948304d0d6a7d5d3a6e3712a46))

### Documentation

* add comprehensive README for the main Robothor repo ([bd7300c](https://github.com/Ironsail-llc/genus-os/commit/bd7300cc447920124db68cc5519dccd6d8922261))
* add onboarding guide and agent builder reference ([6f43f84](https://github.com/Ironsail-llc/genus-os/commit/6f43f84f81352ba1208f7bd2ad6843353b2ad963))
* add system requirements with hardware tiers to README ([22fab27](https://github.com/Ironsail-llc/genus-os/commit/22fab272c0b5a14f57836250b945074a4bb1ba36))
* add weekly SSD backup to cron map ([4d30932](https://github.com/Ironsail-llc/genus-os/commit/4d30932401c3b55021352f4b03ccd2911c289742))
* rebrand Robothor → Genus OS ([854c8bc](https://github.com/Ironsail-llc/genus-os/commit/854c8bcca451da42ab872f43992962e804f2514a))
* refresh README with post-launch features — Engine v2, Nightwatch, Memory v4, sub-agents, voice ([9ceb2fc](https://github.com/Ironsail-llc/genus-os/commit/9ceb2fcf761bb98f35465ab3b862d5d1c052cb75))
* rename business layer to "the Helm" across all references ([2cf5296](https://github.com/Ironsail-llc/genus-os/commit/2cf52968bad2c1c5f462e52124cab40b1be5ba64))
* reposition README for enterprise — security, scalability, governance, federation ([7439cc0](https://github.com/Ironsail-llc/genus-os/commit/7439cc0ef670b08c9c406e648e25fe9cad9d3502))
* rewrite README for current architecture + add health to briefing agents ([50e45df](https://github.com/Ironsail-llc/genus-os/commit/50e45df71fb268a0a9cda9e3125085de10b50e13))
* update quickstart, examples, and public README ([ba96e1f](https://github.com/Ironsail-llc/genus-os/commit/ba96e1ff7712d9cba74f27e39351b0e19b939aa2))
* update README — tool counts, Nightwatch architecture, watchdog, deep reasoning ([d12442f](https://github.com/Ironsail-llc/genus-os/commit/d12442f894c71b37cc078e4ebfbf135b1cb3ccca))
* update README to reflect current project state ([678519d](https://github.com/Ironsail-llc/genus-os/commit/678519d11eb2b53546a9b084733383ae25758e1b))
* update README, ROADMAP, and supporting docs for federation ([b8b161e](https://github.com/Ironsail-llc/genus-os/commit/b8b161eafac77551f1bd26f06cfa3f04da2afa15))

### Code Refactoring

* architectural consistency — unify DAL, centralize URLs, add migration tracking ([aecdab2](https://github.com/Ironsail-llc/genus-os/commit/aecdab2a6fc49631379bc8894a2becbf41994679))
* decompose engine tools.py into package, DRY up runner + scheduler ([d5af6a1](https://github.com/Ironsail-llc/genus-os/commit/d5af6a1c67d88d441d7fa9050fda8f98bdad31c9))
* update all paths from ~/clawd/ to ~/robothor/brain/ ([db6fcfa](https://github.com/Ironsail-llc/genus-os/commit/db6fcfa864d3a6e867d24f27dbe6595c67d69a1e))

### Performance Improvements

* reduce chat latency — parallel warmup, SSE passthrough, lazy dashboard ([98bcf87](https://github.com/Ironsail-llc/genus-os/commit/98bcf87c5c86c489357bf719d83d20722a6acb5b))
* reduce chat latency — parallel warmup, SSE passthrough, lazy dashboard ([beeaafb](https://github.com/Ironsail-llc/genus-os/commit/beeaafb989fbe3eac02e22e0c1b28ac1593ae725))

# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **Behavior change** — Forward thread planner (`thread_planner.py`) is now **on by default**. Previously gated by `ROBOTHOR_PLANNER_ENABLED=1`; from the task-system stabilization, the variable defaults to `"1"` and only `ROBOTHOR_PLANNER_ENABLED=0` disables it. Operators who want the old off-by-default behavior must set the env explicitly.
- `crm_tasks.autonomy_budget` is now validated at write time via `robothor.engine.autonomy.validate_budget`. Malformed budgets (negative caps, unknown verdicts, extra top-level keys) cause `create_task` / `update_task` to return `{"error": reason}` instead of silently degrading the planner.
- `approve_task` and `reject_task` now reset `crm_tasks.escalation_count` to 0 — operator engagement closes the escalation tally rather than letting it grow forever.
- Rebranded project from "Robothor" to "Genus OS". Robothor remains the name of Philip's personal AI instance. Python package name (`robothor`), directory structure, env vars, and systemd services are unchanged.

### Added
- `robothor.engine.autonomy.validate_budget(budget)` — pure validator for the JSONB `autonomy_budget` shape. Used by the CRM DAL.
- `docs/TASK_HISTORY_KIND.md` — canonical enum of `crm_task_history.metadata.kind` values; backed by a `NOT VALID` CHECK constraint in migration 067 and a meta-test that fails CI on drift.
- `crm/migrations/067_task_history_kind_schema.sql` — adds `question_resolved_at` / `question_resolved_by` columns on `crm_tasks` and the metadata-kind CHECK constraint on `crm_task_history`.
- `scripts/audit_autonomy_budgets.py` — read-only diagnostic that flags pre-existing tasks whose `autonomy_budget` would fail the new validator. Run before promoting migration 067's constraint from `NOT VALID` to validated.
- Prometheus metrics for the thread planner: `robothor_planner_actions_total{action,tenant}` and `robothor_planner_run_duration_seconds{tenant}`. See `docs/PLANNER_OBSERVABILITY.md`.
- Structured log events `planner.run_complete` (INFO, per-beat) and `planner.action.refused` (WARNING, per refused plan). All instrumentation wrapped in `contextlib.suppress(Exception)` so observability never breaks the lifecycle.
- Gateway unification — OpenClaw source as git subtree with `robothor gateway` CLI
- Gateway manager package (`robothor/gateway/`) — build, process, config gen, migrate
- YAML-first agent manifests (`docs/agents/`) with `validate_agents.py`
- Agent task coordination — state machine (TODO → IN_PROGRESS → REVIEW → DONE) with SLA tracking
- Review workflow with approve/reject, history tracking, and agent notifications
- Multi-tenancy with tenant-scoped data isolation across all CRM tables
- Bridge service — CRM API with 9 routers, RBAC middleware, tenant isolation
- Event bus — 7 Redis Streams with standard envelopes, consumer groups, and RBAC
- Agent RBAC — per-agent capability manifests (tools, streams, endpoints)
- The Helm — Next.js 16 live dashboard with chat, task board, event streams
- Service registry with topology sort and health-gated boot orchestration
- Audit logging with typed events and telemetry table
- SOPS + age secrets management with cron/systemd wrappers
- Vision module — YOLO detection, InsightFace recognition, pluggable alerts
- CRM module — people, companies, notes, tasks, validation, blocklists, merge
- Memory system — facts, entities, blocks, lifecycle, conflicts, tiers, ingestion
- RAG pipeline — search, rerank, context assembly, web search, profiles
- MCP server with 44 tools for memory, CRM, vision
- Config system with env-based validation and interactive setup wizard
- Database connection factory with pooling
- CI pipeline with ruff, mypy, and pytest on Python 3.11/3.12/3.13
