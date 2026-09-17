"""God-object regrowth ratchet.

The 2026-08-24 architecture audit scored engine architecture 3/5 for one
dominant reason: runner.py was a 4,660-line god-object (execute ~1,100 lines,
_run_loop ~1,230) that every cross-cutting concern threads through, with
telegram.py (3,792) as the same disease in the delivery layer. Phase 1 of the
decomposition extracted the run-finalization cluster (~740 lines whose only
external dependency was self.config) into run_finalizer.py.

This ratchet makes the remaining sizes a one-way door: a change that grows a
capped module past its high-water mark fails CI with instructions, the same
drift-gate pattern as the guardrail-list and alert-name tests. When you
EXTRACT code and a module shrinks, lower its cap to the new size — that is
the point of a ratchet.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# module -> (cap_lines, rationale)
CAPS = {
    # Lowered 4000 -> 3200 after phase 2 (LLM-call + lifecycle clusters out).
    # Lowered 3200 -> 2900 after phase 4 (tool-call admission gates out).
    # 2900 -> 2950 held while the run-budget cluster left (deadline warning,
    # compaction trigger, runaway token caps -> run_budget.py).
    # +4 for bounding the cancellation path's _finish_run — the one
    # finalization outside the outer asyncio.timeout, and the stretch
    # where a 1200s run reached 3110s. Further trimming would have meant
    # deleting the explanation to satisfy a line count.
    # +2: the deadline note now also takes the task text, so it can name the
    # deliverable that is missing. The composition itself was extracted to
    # deliverables.py rather than grown here.
    # +1 for the cancel-classification call site. The classification itself
    # went to cancel_outcome.py, and the two lazy run_budget imports it
    # arrived with were hoisted to the module header (run_budget was already
    # imported there, so they bought nothing) — which paid back four of the
    # five lines this would otherwise have cost.
    # 2545 -> 2539: a concurrent session lifted injection screening and journal
    # resume out of execute(); the deliverable guard's call site added the rest.
    # Its 25 lines of logic went to loop_guards.py, so what remains here is a
    # comment, a lazy import and a two-line branch. (These four lines sat above
    # telegram.py's entry until 2026-09-17, where they read as a chain ending at
    # 2545 against a cap of 2000 — they were always about this file.)
    # 2515 -> 2522: Stage 5 propagates the CRM task id onto the run at INSERT
    # time, so sub-agent runs stop landing with task_id NULL (0 of 44,611 rows
    # had one). Seven lines: a four-line note and a two-line branch inside
    # execute(). There is no cohesive cluster to extract here — the write has
    # to happen where the run row is being assembled.
    # 2522 -> 2520: the post-stall autoDream spawn left for run_lifecycle.py.
    # 2520 -> 2514 on merge: that extraction and the host-state work landed
    # independently, so the ratchet takes the merged actual rather than either
    # branch's number -- a cap carried over from one side would bank the other
    # side's savings as headroom.
    # 2514 -> 2512 with the step-efficiency work. The repeat guard's drain, the
    # pace-note call site and the refusal-is-not-progress branch cost less than
    # the inlined deadline and check-in blocks returned when they left for
    # run_pacing.py, so the cap follows the file DOWN rather than banking the
    # difference as headroom — the whole point of a ratchet, and this one had
    # quietly consumed its last line before the cap was re-read.
    # 2512 -> 2268: the tool-call block — admission, execution and recording
    # for one assistant message — left for tool_turn.py. The ratchet asked for
    # an extraction rather than a bigger number when parallel execution and
    # the tool proxy needed somewhere to live, and got one; the cap follows
    # the file DOWN rather than banking 244 lines of headroom.
    # 2268 -> 2224: the per-tool wall-clock tables and the rule that reads
    # them left for tool_timeouts.py, taking three DEAD verbatim copies in
    # run_llm_calls / run_lifecycle / run_finalizer with them (three of the
    # four had already drifted -- `ask_user` was in this one alone). That is
    # what paid for teaching the resolver about self-timed tools, instead of
    # raising this number for it.
    "robothor/engine/runner.py": 2224,
    # 3850 -> 3150 after the plan-mode cluster left (phase 3), then again after
    # phase 3b (_setup_handlers closures -> methods), and 3150 -> 2000 as the
    # handler and attachment clusters left for telegram_handlers.py and
    # telegram_attachments.py. The cap follows the file down.
    "robothor/engine/telegram.py": 2000,
    # 1300 -> 1293: the `ask:` callback body and the `handle_text` ask
    # interception moved to channels/telegram_ask.py, beside the binding rules
    # they apply. The ratchet only ever goes down, so the new actual is the new
    # cap — leaving it at 1300 would bank headroom this file did not earn.
    # 1293 -> 1123: the attachment intake (recognising, downloading, saving,
    # describing and album-grouping an inbound file) left for
    # telegram_attachments.py. That cluster was what the attachment work would
    # otherwise have added ~220 lines to — it paid for itself and then some.
    # 1123 -> 1082: bounding the inbound download (hostile review I5) cost this
    # file seven lines, so `handle_voice` went to telegram_attachments.py where
    # the rest of the intake lives — it calls media_ref, _keep_attachment and
    # the same ceiling, so it was always that cluster. The ratchet asked for an
    # extraction rather than a bigger number, and got one.
    "robothor/engine/telegram_handlers.py": 1082,
    "robothor/engine/telegram_plan_mode.py": 900,
    # 1100 -> 1048: the deliverable-contract cluster (the ladder, the guardrail
    # event, the operator alert and the honest failure) left for
    # deliverable_verdict.py. The shape contract would otherwise have added ~55
    # lines here; it paid for itself by taking the ~100 that were already there.
    # The ratchet asked for an extraction rather than a bigger number, by this
    # file's own header, and got one.
    # 992 -> 1001: one more irreducible call site, the `chat.py` case. What a
    # run never finished READING is a different question from whether its
    # artefact is right — a run can write exactly the named file in exactly the
    # named shape over a strictly smaller set of facts than the task gave it —
    # and the whole ladder for it lives in observation_ledger.py. Nine lines is
    # the comment, the import and the call; there is nothing here to extract.
    "robothor/engine/run_finalizer.py": 1001,
    # The deliverable-contract cluster, capped at the size it was split to.
    # Hostile review 2026-09-16 (I6): `deliverable_contract.py` had reached
    # 1,354 lines and none of the three new modules was listed here, so "the
    # ratchet passed" said nothing about the code this branch had added. The
    # seam was already drawn by the file's own section comments — extraction
    # and checking share nothing but the item definitions — so it was cut
    # there, and every piece is now something the next change has to argue
    # with.
    # 469 -> 486: a contract stated only inside a loaded SKILL was invisible
    # here, because this accessor reads the prompt. Measured — one benchmark
    # task's output path is in its skill body, so prompt-only extraction
    # returned an empty contract for exactly the run that had been handed one.
    # The remembering went to skill_contract.py; what is here is the accessor
    # split into `_prompt_text` plus a two-line join, so all three consumers
    # keep reading one source and cannot disagree about what the task asked.
    "robothor/engine/deliverable_contract.py": 486,
    # 552 -> 594: CodeQL's five `py/polynomial-redos` findings. Every added
    # line is the reasoning for a regex, not another extractor — the rule that
    # keeps this module linear on hostile input is worth more written down
    # than rediscovered, and splitting a file to hide a comment would be the
    # ratchet working against its own purpose.
    "robothor/engine/deliverable_extract.py": 594,
    # "Is this a requirement at all?" is a different question from "what
    # shape does it require", with one entry point and no dependency on the
    # rest of the extractor — so the ratchet took it out rather than take a
    # bigger number when the suppressor grew.
    "robothor/engine/deliverable_suppress.py": 107,
    "robothor/engine/deliverable_check.py": 549,
    "robothor/engine/deliverable_items.py": 256,
    "robothor/engine/deliverable_verdict.py": 249,
    # 325 -> 368: the third and fourth questions asked of a run that wants to
    # stop — did it finish reading what it was shown, and where the task asked
    # for a decision per item, did its artefact contain one — plus the fifth,
    # the act->observe nudge, which has no other delivery path on a run short
    # enough to cross no check-in. Every body is in its own module
    # (observation_notes.py, verdict_commitment.py); what this file gains is the
    # chaining and its `contextlib.suppress`, which is the job this file exists
    # for. A guard that raises here takes the runner's main loop with it.
    "robothor/engine/loop_guards.py": 368,
    # 937 (2026-09-13): every module the delivery path runs through was capped
    # except the one that decides delivery. It was uncapped when the
    # thin-announce fallback landed, so nothing but review stood between that
    # feature and a god-object — the same gap `deliver()` itself had, which is
    # why the fallback and the send are both helpers now rather than more
    # inline branches.
    "robothor/engine/delivery.py": 950,
    "robothor/engine/run_lifecycle.py": 709,  # -69: dead copy of the tool-timeout tables
    "robothor/engine/run_llm_calls.py": 381,  # -69: dead copy of the tool-timeout tables
    "robothor/engine/tool_admission.py": 400,
    # One assistant message's tool calls, from admission to the ledger.
    # Bounded from the day it lands, like schedule_reconcile.py: this is the
    # module that would otherwise absorb every per-turn concern the runner
    # used to, one branch at a time — which is exactly how runner.py became
    # the 4,660-line god-object this file's header blames.
    "robothor/engine/tool_turn.py": 549,  # +21: the snippet approval budget, read beside the call cap
    # The code-sandbox cluster, each piece capped at the size it was written
    # to. They are deliberately four small modules rather than one: the
    # POLICY (which calls may share a batch) is a table of names with no
    # runtime behind it, the PROXY is admission plus the ledger, the SERVER
    # is a wire format, and the HANDLER is a subprocess. A single
    # `code_execution.py` holding all four would be untestable in exactly
    # the place it matters most.
    "robothor/engine/parallel_tools.py": 173,
    # Capped on the way IN, not after it regrew. `choices` and `reason` landed
    # in a module that had reached 1,479 lines, so the reply contract — what a
    # model may answer and how the answer is checked — left for
    # vision_contract.py before the cap was written. The two are different
    # subjects: one fans calls out, bounds them and budgets the result; the
    # other is a matcher whose whole safety property ("equality, never a
    # prefix") has to be readable in one screen. A cap set at the post-
    # extraction size is the only one that makes the extraction a one-way door.
    # 1276 -> 1275: dropping the sample top-up loop (review I3). The cap
    # follows the file DOWN, or the next change banks a line it did not earn.
    # 1275 -> 1342: review M4 and M5. A row that times out or blows up mid
    # re-ask now reports the tokens and money the FIRST call already spent
    # (`_paid_for`) instead of dropping them, and the spill note is told which
    # key the rows actually carry so it stops promising "answer" to a batch
    # whose rows say "choice". Both are corrections to what this module
    # REPORTS, which is its own subject; there is no cohesive cluster to lift
    # out of a 20-line ledger helper and a threaded argument.
    #
    # THIS CAP CARRIES A DEBT: **FU-VIS2-SPILL** — extract the spill/budget
    # cluster (`_spill`, `_spill_sentence`, `_spill_path`, `_fit`,
    # `_pick_sample`, `_sample_row`, `prune_spill_files` and the budget
    # constants) into a module of its own. Filed in the P4-VIS2 report's
    # follow-up list; the next change that needs room in this file pays it
    # rather than raising this number again. An intention is not a commitment,
    # so it is named here where the raise has to be argued for.
    "robothor/engine/vision_batch.py": 1342,
    # 248 -> 351: the reply parser. Review finding I1 measured three ordinary
    # model formatting habits — both markers on one line, a JSON object, a
    # parenthetical gloss — each turning a whole batch into `error` rows at
    # twice the cost, because the matcher was tolerant and the PARSER was not.
    # The fix is unwrapping (fence, <think>), a JSON-object reader and an
    # inline-marker split, all of which is this module's own subject: what a
    # model may answer and how the answer is read. This is the trade
    # `code_exec_rpc.py`'s entry above already names — correcting a cap set
    # hours earlier in the same PR for the thing that makes the module correct
    # is not the same as bumping a long-standing one to dodge a refactor.
    # 351 -> 385: re-check O1 and F1, same PR, same argument. The JSON reader
    # the I1 fix added was eating a free-text transcription — an object with an
    # `answer` key came back as that one field, silently — so it is now gated
    # on SHAPE, and the inline-marker pattern learned that `_` is a word
    # character so `\b` never fired inside `__WHY:__`. Both are this module's
    # own subject, and both are the parser being made correct rather than a
    # feature being added to it. This module is ONE readable subject at 385
    # lines; splitting it would produce two files neither of which can be read
    # alone, which is the opposite of what the ratchet is for.
    # 385 -> 397: R2-1. The three marker patterns each carried a hand-written
    # character class and the backtick was missing from all three, so a model
    # writing `ANSWER:` as inline code had every row rejected and re-asked.
    # The class is now ONE named constant the three share — which is why this
    # costs twelve lines instead of three characters, and why the next
    # character somebody's model likes cannot go missing from two of them.
    "robothor/engine/vision_contract.py": 397,
    # 277 -> 314: the approval budget. A proxied call passes the same approval
    # gate a turn's call does, which is right and which is also how a loop
    # could queue two hundred prompts at the operator. The check belongs where
    # the reach is decided, not in the socket.
    # 314 -> 322: the per-call response ledger. A proxied call's response is
    # EVIDENCE, not a receipt — the measured run printed each send's `status`
    # and threw away the three follow-up messages that rode back in those same
    # responses. Eight lines, and the evidence extraction itself is in
    # act_observe.py rather than here.
    "robothor/engine/tool_proxy.py": 322,
    # 209 -> 283: the peer-session check. Not a feature, a control: the token
    # alone could not tell one run's snippet from another's, and a probe drove
    # a second run's proxy with a stolen token. Correcting a cap set hours
    # earlier in the same PR for the thing that makes the module correct is
    # not the same as bumping a long-standing one to dodge a refactor.
    # 283 -> 316: aclose cancels its handlers instead of waiting them out.
    "robothor/engine/code_exec_rpc.py": 316,
    # 523 -> 388: spawning a snippet, reading its pipes without deadlocking
    # it, detecting its exit and killing its descendants is one subject and
    # the handler's admission/staging/shaping is another. The split is what
    # paid for the cancellation fix rather than a bigger cap, and it is where
    # the descendant walk lands.
    # 351 -> 281: the result shaping and the stdout spill left for
    # code_exec_result.py. "Given what the process produced, what goes into the
    # context and what goes to disk" is its own question, and it is the one the
    # escape-disclosure wording lives in — so it belongs where a reader looking
    # for that sentence would go. The ratchet caught this file three lines over
    # after a style commit; the answer is the extraction, not the number.
    # 395 -> 351: the pre-spawn refusals and the process hardening left for
    # code_exec_guards.py — "may this run at all, and is the engine ready" is a
    # different question from staging, spawning and shaping, and it is what
    # paid for teaching this tool to harden its own process rather than trust
    # that some entry point remembered.
    # 413 -> 395: the in-sandbox boot source left for
    # sandbox_runtime/boot_template.py, beside the client it loads. Ninety
    # lines of code that runs somewhere else was the shape `genus_tools.py`
    # was deliberately not written in; the reaper it grew made that obvious.
    # 281 -> 297: the count of proxied responses this snippet never printed.
    # The computation is in act_observe.py; this is the bracket that says which
    # of the turn's proxied calls belong to THIS snippet, plus the call.
    "robothor/engine/tools/handlers/code_exec.py": 297,
    "robothor/engine/code_exec_result.py": 103,
    # The observation cluster (2026-09-16), each piece capped at the size it was
    # written to and each one a separate question, for the reason the code-
    # sandbox cluster above is four modules: what the model SEES of a command's
    # output and where the rest goes (`exec_spill`); how a call is CLASSIFIED as
    # a read or a change (`act_observe`, a table of names and two regexes, no
    # runtime behind it); what one RUN has not finished reading
    # (`observation_ledger`); and whether a classification artefact actually
    # classified (`verdict_commitment`, the only one touching judgement, which
    # is why it is reachable and testable without a session). One
    # `observations.py` holding all four would be untestable in the place it
    # matters most — and `exec_spill` in particular has to be importable by the
    # repeat guard and the no-progress detector, neither of which may drag a
    # ladder or a session in with it.
    # 275 -> 427 across one review round, in three steps that are one subject:
    # what this module PROMISES about the file it writes.
    #   * the read-back check resolves a real path instead of matching a
    #     substring — appending `# .robothor/exec/a__b` to a command used to
    #     switch off the repeat-call guard AND the no-progress detector for the
    #     rest of the run (I3), and qualifying now takes a token outside a
    #     comment, the filename this module writes, the right directory, and a
    #     file that exists;
    #   * a size ceiling and a free-space check — a 50 MB command wrote a
    #     50,000,000-byte file under the workspace (I6), and before this branch
    #     nothing from a command reached the disk at all, so the ceiling is new
    #     surface this change is responsible for;
    #   * `<stream>_shown_chars`, so the ledger and the marker cannot report two
    #     different numbers for one cut (I8).
    "robothor/engine/exec_spill.py": 427,
    # 232 -> 347: classification by tool KIND and TARGET. The first cut asked
    # only "does this call name something with a slash in it", so every
    # `write_file` to an absolute path — which is every WildClaw deliverable —
    # read as a remote state change and the note fired on essentially every run
    # (hostile review I1). The added lines are four tables and the reasoning for
    # each, including the split between a strong HTTP write VERB and the weak
    # `--data` flag: requiring a literal host made `requests.post(url, json=m)`
    # classify as `neither`, blind to the shape the control exists for. A
    # target-only rule would miss every CRM write, which names no host at all.
    "robothor/engine/act_observe.py": 347,
    # 469 -> 505 -> 285 -> 385 across one review round. The middle number is
    # the one that matters: at 505 the DELIVERY half left for
    # observation_notes.py, because "what happened" and "what the run is told
    # about it, and when" are different questions and only the second is allowed
    # to change a run — which is exactly where both of that round's substantive
    # findings landed. The ratchet asked for an extraction rather than a bigger
    # number when the act->observe stop path needed somewhere to live, and got
    # one. What grew back to 385 is resolution that answers the question it
    # claims to: asked on every call rather than only inside the READ branch (a
    # `cat` of the spill classifies as `neither`), the read-back a resolved
    # path, a re-run compared on the TARGET with the query stripped, a call that
    # observed nothing no longer counting, entries keyed by `(step, stream)`,
    # and the run able to say which workspaces its spills went to. I4, I5, M2.
    "robothor/engine/observation_ledger.py": 385,
    # The delivery half, capped at what it was split to plus the second hold and
    # the stop-path nudge. `enforce`'s honest completion used to be appended
    # after the runner had already returned, so it reached the transcript and
    # nothing else (C1); the reasoning for why a False there is inert is written
    # here, where the next reader will need it.
    "robothor/engine/observation_notes.py": 334,
    # 265 -> 322: the guardrail row this control writes at finalization on
    # `observe` as well as `enforce`. Without it `flags/evidence.py` would
    # report the one ladder whose promotion depends on watching the evidence as
    # permanently inert, which is the failure this repo has recorded twice.
    # 322 -> 370: three false positives the hostile review found, and the
    # reasoning for each. The bare word `test` in a label read as a verdict,
    # `per \w+` opened the gate on "as per the runbook", and a hand-back about
    # the WORK read as a refusal to decide. This is the one control here that
    # touches judgement; a false positive teaches an agent to stop stating its
    # doubts, which is worse than the defect, so the vocabulary is phrases and
    # the reasoning for each narrowing is written down rather than rediscovered.
    "robothor/engine/verdict_commitment.py": 370,
    "robothor/engine/skill_contract.py": 73,
    "robothor/engine/code_exec_guards.py": 116,
    # 122 -> 129: the same disclosure, said where the reaper is, because this
    # is the `finally` that `os._exit` skips and a reader here is the one who
    # would otherwise conclude the reaper is complete.
    "robothor/engine/sandbox_runtime/boot_template.py": 129,
    # 211 -> 341: the descendant census. `killpg` alone reached neither a
    # `setsid` child nor a double-forked daemon, and a probe left 16 of 16
    # running after the call returned. This is the module that owns "nothing
    # survives", so the census belongs here and nowhere else.
    # 350 -> 355. Nine of those lines were MAX_TIMEOUT_SECONDS joining the
    # other two clocks; the last five are the disclosure the re-check
    # required — that a snippet can FORCE the escape (a `setsid` child, then
    # `os._exit` to skip its own reaper) rather than merely win a race. That
    # is the `deliverable_extract.py` case: splitting a file to hide a comment
    # would be the ratchet working against its own purpose, and a limit
    # rediscovered by the next reviewer costs more than five lines.
    "robothor/engine/code_exec_process.py": 355,
    # The per-tool wall-clock rule, with one definition instead of four.
    "robothor/engine/tool_timeouts.py": 144,
    "robothor/engine/tools/read_only.py": 87,
    "robothor/engine/run_budget.py": 120,
    # Live host state for the warmup preamble (2026-09-13). Bounded from the
    # day it lands, like schedule_reconcile.py: this is the module that would
    # otherwise absorb every "what is this box doing right now?" probe an agent
    # ever wants, one system call at a time, inside a code path that runs on
    # every cron beat and every interactive turn. Anything bigger than these
    # three facts belongs in a tool the agent calls deliberately, not in warmup.
    # 384 -> 498 in review round 1, then 498 -> 592 in round 2, and every line
    # of both is a correction rather than a feature: the systemd probe reads and
    # now JUDGES ActiveState (a nonexistent unit exits 0 with empty output; a
    # failed unit keeps its last start's timestamp) and retries without
    # --timestamp=utc for systemd < 247; the reach sentence stopped calling
    # whatever model was busiest "the primary", and then learned to tell "no
    # primary is configured" from "nobody told me which one". The section still
    # renders the same three facts it started with. Each round's growth is
    # distinguishing a case that was previously answered with a plausible guess —
    # which is the whole defect class this module exists to close, so the cap
    # moving for that reason is the ratchet working, not being dodged.
    "robothor/engine/host_state.py": 592,
    # Tempo-scaled watchdog budgets (2026-08-27): extracted here rather than
    # growing run_budget past its cap, same as the finalization cluster.
    # Raised 110 -> 125 the same day to admit max_wallclock_ceiling(), which the
    # reaper needs. Adjusting a cap set hours earlier for a cohesive addition is
    # not the same as bumping a long-standing one to dodge a refactor; this
    # module is four related functions, not a god-object. Do not raise again
    # without extracting.
    "robothor/engine/watchdog_budgets.py": 125,
    # Cancel classification (2026-08-27): extracted from the runner, which is
    # the god-object this ratchet exists to shrink.
    # 80 -> 86: terminal_run was untyped, which the mypy gate rejects as an
    # untyped call in a typed context. The signature costs six lines and the
    # TYPE_CHECKING import four; trimming to fit would have meant deleting
    # the docstring that says why watchdog_fired overrides the status.
    # 86 -> 83: learning a THIRD kind of evidence (a workflow budget expiring
    # mid-call is a cancellation, not a timeout) was paid for by moving
    # `propagates_to_caller` beside the exception that gave that rule its
    # second member, and by deleting history the module docstring already told.
    "robothor/engine/cancel_outcome.py": 83,
    # Fleet admission (2026-08-27): extracted from the scheduler rather than
    # growing it past its cap — the pool it drives had no production caller for
    # its whole existence, and the wiring is a cohesive unit of its own.
    "robothor/engine/admission.py": 105,  # +23: observe/enforce ladder; the evidence writer was extracted to admission_evidence.py
    # The finalization cluster (what a run may spend AFTER its loop ends)
    # was extracted here rather than growing run_budget past its cap.
    "robothor/engine/finalization_budget.py": 160,
    "robothor/engine/chat.py": 1600,
    # 1600 -> 1607 (2026-08-27): fleet admission finally has a caller here.
    # The 75-line implementation went to admission.py; what remains is five
    # irreducible call sites (admit / register / complete / import). The
    # ratchet did its job — it forced the extraction and made this residual
    # an explicit decision rather than drift. Do not raise again without
    # extracting something real.
    # 1626 -> 1600: the wanted-job-set derivation left for
    # schedule_reconcile.py, and start()'s three hand-written registration
    # blocks collapsed into one loop over it (266 -> 117 lines). That is what
    # paid for reconcile learning to add and replace, rather than raising this.
    # 1600 -> 1610: review round 1 added the row-refresh branch (a trigger can
    # be unchanged while agent_schedules is not). Its bookkeeping went to
    # schedule_reconcile.RowLedger; what is left here is the call site. This
    # corrects a cap set hours earlier in the same PR, not a long-standing one,
    # and is still 16 lines below where this module started.
    "robothor/engine/scheduler.py": 1610,
    # The job-set derivation reconcile and start() now share. Bounded from the
    # day it lands: this is the module that would otherwise absorb every
    # scheduling concern that does not fit in scheduler.py's cap.
    # 300 -> 340: JobSpec.row() and RowLedger — the agent_schedules half of a
    # spec, which decides whether the ROW needs rewriting where fingerprint()
    # decides whether the JOB does. Conflating the two left a model-only edit
    # reconciling to a no-op with a stale row; separating them is what kept the
    # scheduler a call site rather than growing this logic there.
    "robothor/engine/schedule_reconcile.py": 340,
    # The bridge's agent builder. Not under robothor/, and that is the point:
    # CAPS was a robothor/-only list, so the largest new file in the manifest-API
    # change was unbounded while every engine file it touched was pinned to
    # within 0.6%. The paths here are resolved against REPO_ROOT, so a crm/ entry
    # costs nothing but the line — and one ratchet is right, because two copies
    # of a guard drift and the drift is what nobody sees.
    #
    # 1,172 -> 1,049: the verdicts moved to _manifest_validation.py along a real
    # seam (nothing there touches the filesystem, the engine or a request, which
    # is what lets it take its inputs as parameters instead of importing the
    # router's resolvers back).
    "crm/bridge/routers/agent_manifests.py": 1060,
    # 250 -> 260: `introduced` now carries the record of getting this wrong
    # twice in opposite directions — keyed too loosely it missed an added
    # fault, keyed too tightly it refused a partial repair — plus the per-fault
    # loop that fixed it. Trimming to fit would mean deleting the explanation to
    # satisfy a line count, which is the trade this repo has explicitly refused
    # before (see runner.py's entry). The fix itself went UPSTREAM into
    # CheckResult.faults rather than growing this file.
    "crm/bridge/routers/_manifest_validation.py": 260,
}


def test_capped_modules_do_not_regrow():
    over = []
    for rel, cap in CAPS.items():
        lines = len((REPO_ROOT / rel).read_text().splitlines())
        if lines > cap:
            over.append(f"{rel}: {lines} lines > cap {cap}")
    assert not over, (
        "module(s) grew past the decomposition ratchet — extract a cohesive "
        "cluster into its own module instead of adding to a god-object "
        f"(see run_finalizer.py's header for the pattern): {over}"
    )


def test_ratchet_caps_are_tight():
    """A cap far above the actual size is a dead gate. Keep each within 10%
    of reality so the ratchet actually ratchets — when you extract code,
    lower the cap."""
    loose = []
    for rel, cap in CAPS.items():
        lines = len((REPO_ROOT / rel).read_text().splitlines())
        if cap > lines * 1.10:
            loose.append(f"{rel}: cap {cap} vs actual {lines}")
    assert not loose, f"tighten these caps to (actual x 1.10) or less: {loose}"
