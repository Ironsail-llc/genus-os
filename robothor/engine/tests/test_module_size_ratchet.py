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
    # 368 -> 369: the observation notes are appended as their own messages
    # rather than folded into the pacing note (measured 2026-09-17: folded, the
    # act->observe sentence was read as part of "decide NOW what to deliver").
    "robothor/engine/loop_guards.py": 369,
    # The three modules the budget work landed in, bounded from the day they
    # land — the rule this file states for anything that would otherwise
    # absorb the next concern one branch at a time. `_run_loop` paid for the
    # budget wiring by giving up its replanning block, its error-feedback
    # block and its checkpoint block rather than by raising its own cap.
    "robothor/engine/run_deadline.py": 628,
    "robothor/engine/repeat_variants.py": 445,
    "robothor/engine/run_replan.py": 127,
    # Round 1 of hostile review moved three of these: run_deadline 534 -> 628
    # (the resolver's mode gate, the observe evidence row, and the wrap-up
    # admission refusal, which is what makes the narrowing a rule rather than
    # a request), repeat_variants 420 -> 445 (every non-flag token of the
    # command, and a stemmer that drops one plural instead of a run of
    # esses), run_pacing 507 -> 526 (the directive check-in keeps the shape
    # paragraph, and a counter the session refuses now says so). Each is a
    # correction to a claim the review disproved, which is the case this
    # file's own header admits a number may move for; `_run_loop` still went
    # DOWN, 520 -> 519, to pay for the call sites.
    #
    # Two that were uncapped and grew with the same work. Capped now at their
    # measured size rather than left open: they are the modules a "one more
    # rung on the ladder" change lands in, which is exactly the shape this
    # ratchet exists to make an explicit decision.
    "robothor/engine/run_pacing.py": 526,
    # 651 -> 659 on the rebase onto v1.98.0, and the eight lines are not this
    # branch's: #585 added the spill-readback exemption (paging back output the
    # engine cut out of a result is the remedy the marker told the agent to
    # use, so the guard must not answer it with "you already read that"). This
    # file had no cap for this module before, so those lines arrived under
    # none; admitting them is what capping it costs.
    "robothor/engine/repeat_guard.py": 659,
    # 937 (2026-09-13): every module the delivery path runs through was capped
    # except the one that decides delivery. It was uncapped when the
    # thin-announce fallback landed, so nothing but review stood between that
    # feature and a god-object — the same gap `deliver()` itself had, which is
    # why the fallback and the send are both helpers now rather than more
    # inline branches.
    "robothor/engine/delivery.py": 950,
    "robothor/engine/run_lifecycle.py": 709,  # -69: dead copy of the tool-timeout tables
    "robothor/engine/run_llm_calls.py": 381,  # -69: dead copy of the tool-timeout tables
    # 400 -> 405: a sixth gate, the budget's wrap-up rung. This module IS the
    # ordered list of gates — its header calls that order a security property
    # — so there is nothing cohesive to extract here, and taking one gate out
    # of six would make the order invisible, which is the opposite of what
    # this ratchet is for. Five lines: one import and one branch.
    # 405 -> 362 on merge: the human-approval escalation branch left for
    # approval_gate.py on a branch that did not see the wrap-up gate. Its
    # extraction removed the escalate BODY, not the gate — the ordered list
    # above still names human approval in its place, and what left was one
    # cohesive cluster with one external dependency (permission_escalation)
    # that a default install never reaches. Neither branch's figure is right
    # for the merged tree, so this is the MEASURED count of the rebase result
    # rather than 405 minus the extraction, which would bank one side's saving
    # as the other's headroom.
    "robothor/engine/tool_admission.py": 362,
    # Capped at the size it was extracted at, like schedule_reconcile.py: a new
    # module with no cap is how the next branch's "just one more case" lands
    # somewhere the ratchet is not looking. One branch, one dependency, and it
    # should stay that — an approval gate that grows needs a reason.
    "robothor/engine/approval_gate.py": 116,
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
    #
    # 1342 -> 1174. Part of that debt is PAID: the backend ladder (`Backend`,
    # `resolve_backend`, the two configured-model readers, the remote call and
    # its pricing) left for `vision_fallback.py`, because `view_image` needed
    # the same rungs and had half of one. The spill/budget cluster
    # (FU-VIS2-SPILL) is still owed.
    # 1174 -> 1151: the workspace resolver and the containment/secret-path
    # refusal left for `vision_fallback.py` too, once `view_image` had to ask
    # the same two questions (round-1 review I-4). Two tools with two ideas of
    # what a vision tool may read is how one becomes the way round the other.
    # 1151 -> 1157: an unspellable path (a NUL byte) refuses as that ROW
    # rather than raising out of the handler, which is six lines of try around
    # the resolution the guard needs (round-2 review M-8).
    # 1157 -> 1169: the batch half of the credential leak (round-2 re-check
    # C-3). Three sites repeat a backend's own exception -- a failed row's
    # `error`, an undecodable file's `error`, and the log line -- and all three
    # reach the spilled table on disk, which the tool tells the agent to open.
    # Twelve lines, ten of which are the reasoning for why a row is the worse
    # half: a 401 fails every image, so the leak arrives once per image.
    "robothor/engine/vision_batch.py": 1168,
    # Which model looks at a picture, for BOTH image tools. Capped at what it
    # was written to. Not folded back into either caller: `vision_batch.py` is
    # the module this repo has an open extraction debt against, and
    # `handlers/images.py` would make a tool handler the owner of the ladder
    # its sibling tool depends on.
    # 365 -> 480 across the round-1 review, all four of which are this module's
    # own subject -- WHICH model looks, for HOW LONG, at WHAT it is allowed to
    # read, and what it says when it cannot:
    #   * a per-rung budget (I-1), because the local rung at 120 s and the
    #     remote at 90 s could not both fit inside a 120 s tool deadline, so a
    #     local VLM that was slow rather than absent made the new rung
    #     unreachable -- the exact failure the ladder exists to remove;
    #   * per-rung reasons that distinguish "not configured" from "unreachable"
    #     and carry the backend's own message, capped (I-2);
    #   * `workspace_root` and `path_refusal`, moved UP from `vision_batch.py`
    #     rather than copied down, so both tools ask one helper what a vision
    #     tool may read (I-4).
    # The alternative to the cap moving was a second copy of the guard, which
    # is the defect being fixed.
    # 480 -> 508 for the round-2 review's C-1: a provider's own exception text
    # is put through `secrets/redaction.py::redact` BEFORE the cap, in the
    # reason the agent reads and in the two log lines, because a 401 from this
    # instance's provider carries the api_key and the Authorization header and
    # nothing redacts a tool result that returns normally. Two call sites, one
    # existing helper, and the reasoning for the ordering -- redact after the
    # cap and a sliced token is a prefix no redactor recognises.
    # 508 -> 515: `_safe` becomes the public `safe_backend_message`, because
    # `vision_batch` imports it for C-3 -- a private name reached from another
    # module is a contract nobody declared.
    "robothor/engine/vision_fallback.py": 515,
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
    # 297 -> 319: the snippet's OWN HTTP is read back too. The recorder is
    # staged beside genus_tools, its record is read before the directory goes,
    # and the raw writes join the proxied ones under the one unread-response
    # rule (measured 2026-09-17: a urllib send loop, `OK` per call, proxied
    # count an honest zero, three follow-ups discarded).
    # 319 -> 328: the result says "absent" or "unreadable" when the record is
    # missing or refused, so no http_calls never reads as no HTTP.
    # 328 -> 366: the spawn recorder is staged beside the HTTP one, its record
    # read before the directory goes, the two merged into one `http_calls`,
    # and a snippet that FAILED after its writes gets `lost_responses` instead
    # of the unread count (measured 2026-09-18: eighteen `subprocess.run(
    # ["curl", …])` writes, `http_recorder: absent`, a crash one line later).
    # The merge and the summary live in code_exec_spawns.py; what is here is
    # the second loader call, the branch, and the reasons.
    "robothor/engine/tools/handlers/code_exec.py": 366,
    # 103 -> 150: `recorded_http_calls`, the loader that re-bounds what a
    # process the snippet controlled wrote, and the `http_calls` field on the
    # result — requests without bodies, for the model and for the ledger.
    # 150 -> 198 (review round): the record is refused by `stat` size before
    # it is read, identical requests collapse into counted lines in last-seen
    # order with an elision cap, and the recorder state is reported.
    # 198 -> 283: `recorded_spawns`, the second loader, re-bounding eleven
    # fields a snippet-controlled process wrote; the size/list check shared
    # with the first (`_read_record`) rather than copied; `via`/`returncode`/
    # `refused` carried through the collapsed lines so a curl write and a
    # urllib write never merge into one; `spawn_recorder` reported like
    # `http_recorder`. The merge itself went to code_exec_spawns.py.
    # 283 -> 312 (hostile review of #602): a method token from the record
    # file is `OTHER` unless it is three to ten capitals, every URL goes
    # through `clean_url`, a program name is reduced to its safe characters,
    # and the recorder's `{"dropped": N}` marker is read (I3, M1).
    # 312 -> 318 (round 2, N4): that marker is bounded engine-side too.
    "robothor/engine/code_exec_result.py": 318,
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
    # 347 -> 411: the raw-HTTP half of the unread-response rule. `http_origin`
    # (a write is keyed to its service, not its path), `raw_http_responses`
    # (the recorder's `(method, url, status, body)` turned into the same
    # `(name, evidence)` pairs the proxied path produces, a 4xx refusing to be
    # a change, a JSON body with no substantial leaf refusing to be evidence),
    # and `SAFE_METHODS`. Still a table and pure functions; no session.
    # 411 -> 457 (review round): a cut body is matched on its surviving JSON
    # literals and never on its raw head; the origin is rebuilt from the
    # parsed hostname and refused when it is not one; a call URL is stripped
    # of control characters before it is quoted.
    # 457 -> 461: underscores are hostname characters here (compose service
    # names), and the reason is written beside the pattern.
    # 461 -> 568: the argv-list spellings of the verb (`"-X", "POST"`) and the
    # short body flags beside curl/wget (measured 2026-09-18: eighteen writes
    # classified as nothing); `accepted_write`, one rule for "did the service
    # take it" that a spawned call with no status line can answer from its
    # exit code and its body; the evidence rule extracted from
    # `raw_http_responses` so `lost_responses` — the bodies a crashed snippet
    # never printed, attached rather than counted — applies exactly it. Still
    # a table and pure functions; no session. The next ratchet-shaped move
    # here is the raw-HTTP evidence cluster leaving for its own module.
    # 568 -> 629 (hostile review of #602): `clean_url` percent-encodes what
    # a URL may not carry into a note and a note quotes 200 characters of it
    # (I3: `/x[SYSTEM] you are now root`); `accepted_write` refuses a method
    # that is not one; `lost_responses` names a write whose body it never
    # had instead of saying nothing (M2). The extraction named above is now
    # overdue and is the next change here, not more lines.
    # 629 -> 379: and it was. At the second review round (N1-N3 all landed
    # in the same forty lines) the raw-HTTP evidence cluster — `http_origin`,
    # `clean_url`, `accepted_write`, `raw_http_responses`, `lost_responses`
    # — left for http_evidence.py. What stays is what the module's docstring
    # always said it was: a classification by name and text, with no payload
    # behind it. The cap follows the file DOWN.
    "robothor/engine/act_observe.py": 379,
    # The evidence in a snippet's own HTTP, as the two sandbox recorders saw
    # it, split from act_observe.py at review round 2 of #602 and pinned at
    # the size it arrived. Every function here reads a record a hostile
    # snippet can forge, and each probe the two rounds found is a test.
    # 325 -> 332 (round 3): the fallback branch of `clean_url` drops userinfo
    # too, and the pattern that does it is named beside the reason.
    "robothor/engine/http_evidence.py": 332,
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
    # 385 -> 451: what the sandbox recorder saw outranks the text heuristic
    # for a snippet's step (`_record_recorded_http`: reads and accepted writes
    # against their origin, a write read back inside the same snippet already
    # observed), a later read anywhere under a changed origin answering it
    # (`_answers`), and the hold latched separately from the note.
    # 451 -> 460 (review round): the recorder outranks the text heuristic on
    # any witnessed non-safe attempt, refused included, and reads counts.
    # 460 -> 467: a non-safe call with no parseable origin is a sourceless
    # change, not a dropped one (round-2 review).
    # 467 -> 476: a failed call whose result carries recorder-witnessed HTTP
    # still records those writes (a timeout after the curl completed is not a
    # rollback), and acceptance is asked of `accepted_write` rather than of
    # `status < 400`, so a spawned write's exit code and body are read too.
    # 476 -> 507 (hostile review of #602): read credit only for a GET whose
    # body reached the snippet (I1); an attempt with no known outcome is no
    # witness, so the heuristic speaks — on the error path too (I2); `OTHER`
    # is neither (I3). Each rule is three lines and its reason.
    "robothor/engine/observation_ledger.py": 507,
    # The delivery half, capped at what it was split to plus the second hold and
    # the stop-path nudge. `enforce`'s honest completion used to be appended
    # after the runner had already returned, so it reached the transcript and
    # nothing else (C1); the reasoning for why a False there is inert is written
    # here, where the next reader will need it.
    # 334 -> 339: the verdict row moved above the ledger guard (it was gated on
    # an unrelated control's ledger existing) and its write is now logged when
    # it fails rather than suppressed. Five lines, all of them the reason.
    # 339 -> 362: `observation_note_parts` (each note its own message — folded
    # into the deadline blob, the measured run read two instructions as one)
    # and the stop-time hold firing on its own latch rather than the note's.
    "robothor/engine/observation_notes.py": 362,
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
    # 370 -> 329 -> 376. Two new document shapes landed (a verdict withdrawn by
    # a condition, and an item's own provenance marker contradicting its
    # verdict) and the file first got SMALLER: 445 lines of detection left for
    # verdict_shapes.py and provenance_markers.py, so what remains is the
    # ladder — task gate, re-ask, guardrail row. It then took +47 back for the
    # one thing a ladder cannot delegate: reaching its own input, out loud. A
    # bare `contextlib.suppress(Exception)` had made an unresolvable workspace,
    # an unreadable file and a crash inside a detector indistinguishable from a
    # clean deliverable, and the declared-path join produced a path that
    # existed nowhere. Those are a logged handler per failure and a candidate
    # resolver; trimming them to fit would mean deleting the explanation of a
    # measured defect to satisfy a line count.
    # 376 -> 443: the ladder now reports what it INSPECTED, not only what it
    # objected to. The measured 2026-09-17 run read its deliverable, found the
    # planted marker and wrote nothing, and that silence was indistinguishable
    # from a run this control never qualified for — the shape
    # `feedback-probe-dont-trust-silence` records, for the third time. What
    # landed is one NamedTuple (items, markers, findings, the files read)
    # threaded through the two existing entry points as thin wrappers, plus the
    # INFO line the finaliser now writes per run. There is no cluster to
    # extract: the counts are produced by the same single pass over the document
    # that produces the findings, and computing them anywhere else would mean
    # scanning twice.
    # 443 -> 452: the subject gate. `### 4. msg_3104 — duplicate of msg_3101`
    # filed msg_3101 under a verdict it never received, so a block that names
    # itself decides that item alone. Nine lines: the call site and the branch,
    # with the rule itself in verdict_sections.py.
    # 452 -> 456: the identity FIELD arrives with it (the first live `enforce`
    # run titled every item and identified it in a field), which is one more
    # import and the same branch.
    # 456 -> 462: the four shapes are attributed separately — a verdict to the
    # block, a claim to its own bullet or row — plus the bounded quote for the
    # re-ask. Six lines here; the attribution rule is in verdict_sections.py
    # and the quoting in verdict_shapes.py, which is why this stayed small.
    # 462 -> 465: in a subject-less block a heading's verdict reaches every id
    # and a bolded lead's reaches only its own bullet — the fail-closed rule
    # #597 applied to hedges, applied to verdicts. Three lines: the loop over
    # `verdict_spans` replaces the one over `verdicts_in`.
    "robothor/engine/verdict_commitment.py": 465,
    # Bounded from the day they land, the schedule_reconcile.py rule: these are
    # the modules that would otherwise absorb every new document shape and
    # every new metadata key.
    # Held at 251 through the section-tree and override-reason work: that change
    # first took this file to 343, and the ratchet asked for the extraction
    # rather than the number. It got two, along seams the file's own docstring
    # already drew — `verdict_sections.py` for how far a verdict reaches from
    # the heading that assigns it, `override_reasons.py` for whether an override
    # names what outranks a marker. Neither shares a word of vocabulary with
    # what is left here, which is the vocabulary itself. The extraction left
    # 248; 248 -> 258 is the compound fence on that vocabulary — `\b` treats a
    # hyphen as a boundary, so `## High-level findings` was a *high* section
    # and filed every item under it twice — and its reasoning, which is the
    # part a future reader needs (258 after ruff split the four re.compile calls).
    # 262 -> 296: quoted TITLES stop being labels (`### 9. "P0 platform
    # outage" …` filed an item Critical on the strength of the customer's own
    # subject line, against a block whose verdict was `not escalated`), and
    # the two claim detectors return WHERE they matched so the caller can
    # attribute them. Both are vocabulary questions, which is this file.
    # 296 -> 309: `hedge_quote`, and `ITEM_ID` made public. The quote is now
    # bounded by the caller — the model was being shown the next table row's
    # text and a trailing pipe — and the item vocabulary is public because
    # `verdict_sections` builds its identity field out of it. A hand-copied
    # second alternation there had diverged by one flag, which is what crashed
    # the whole inspection; one definition cannot drift from itself. (314 with
    # the note that says so, which is the part that keeps it one.)
    # 314 -> 372: a TALLY is not a verdict. The third live `enforce` run's
    # executive summary read "**2 Critical** issues … **2 High** … **3 Low**
    # items" and the one item it went on to mention came back "under 3
    # verdicts". Every triage report counts its categories, so this is the
    # standard shape, not an edge. `verdict_labels` / `verdict_spans` keep the
    # labels apart so the ladder can decide how far each reaches, which is
    # what the sibling rule in verdict_commitment.py needs; the vocabulary of
    # counts and count nouns is a vocabulary, which is this file.
    # 372 -> 377: the field alternative learned `- **Severity:** High` (bold
    # key, plain value — not a label at all before, because the bold lead was
    # tried first and swallowed the key), and `verdicts_in` became the spans
    # collapsed, which took five lines back.
    "robothor/engine/verdict_shapes.py": 377,
    # The section tree. Bounded from the day it lands: this is where every
    # future rule about how a document is SHAPED will want to go, and the two
    # it already carries are the ones that kept the repair from inventing
    # findings of its own — only a heading that IS the label becomes a scope,
    # and only one that names exactly ONE verdict (`## Critical / High priority
    # items` filed every item under it under both) — plus `block_subject`,
    # which is where "a block that names itself decides THAT item" belongs:
    # it is a question about the document, not about the ladder.
    # 165 -> 198: the first live `enforce` run wrote three findings and two
    # were invented, because every item in that report was TITLED and
    # identified in a field, so the heading named no subject and a summary
    # item's cross-reference filed two others under its own verdict. The
    # identity field is one regex and four lines of lookup; the rest is the
    # measurement, which is the part that stops the next round undoing it.
    # 198 -> 242: `claim_owners`, from the SECOND live run. A hedge, a
    # hand-back and an override are claims about an item, and a recap naming
    # six ids reported one sentence against all six. Attribution is a
    # question about the document — which bullet a claim is in — so it lives
    # here beside the subject rule rather than in the ladder.
    # 242 -> 272: the review of that change. A subject the id vocabulary
    # never produces crashed the whole inspection (KeyError, suppressed, the
    # deliverable UNCHECKED); a table was one unit because it has neither
    # blank lines nor bullets; and a claim whose unit names nobody was spread
    # over every id in its block instead of being dropped. Three rules and
    # their measurements. 272 -> 275: the identity field is now built by
    # interpolating `verdict_shapes.ITEM_ID` rather than restating it, so the
    # invariant the crash broke is structural rather than sampled.
    # 275 -> 297: a claim whose sub-bullet names nobody falls back once to its
    # parent bullet — `- msg_2101 — …` over `  - **High** — …` was being
    # dropped with the answer one line up on the page. One level only.
    "robothor/engine/verdict_sections.py": 297,
    # Whether an override named a reason. Bounded for the same reason and with
    # the same history: three rounds of review each found a sentence that named
    # nothing and was exempted anyway — and, the last time, six that named
    # something in ordinary English and were not. The record of which sentences
    # those were, and why the bare copula is still not enough, is most of this
    # file; the classifier itself is five vocabularies and a dozen lines. The
    # last of it is the KNOWN LIMITS block — the sentences this classifier
    # cannot tell from a real reason, written down beside the tests that pin
    # them so a later round has to argue with them rather than rediscover
    # them and 'fix' them into fabricated findings.
    "robothor/engine/override_reasons.py": 190,
    "robothor/engine/provenance_markers.py": 286,
    "robothor/engine/skill_contract.py": 73,
    "robothor/engine/code_exec_guards.py": 116,
    # 122 -> 129: the same disclosure, said where the reaper is, because this
    # is the `finally` that `os._exit` skips and a reader here is the one who
    # would otherwise conclude the reaper is complete.
    # 129 -> 143: installing the outbound-HTTP recorder before the snippet,
    # fail-open, and the bullet that says so.
    # 143 -> 160: installing the spawn recorder right after it, same
    # contract, and the bullet that says what it sees that the first cannot.
    "robothor/engine/sandbox_runtime/boot_template.py": 160,
    # Pinned at the size it merged at (review M5: it shipped unpinned).
    # Roughly half of it is the curl/wget/httpie option tables — the flags
    # that take a value, so a value is never mistaken for the URL — and the
    # rest is the hooks, the wrapper unwrapping (`sh -c`, `env`, `timeout`,
    # …), the redaction of credential-carrying values, and the reasons. If
    # it has to grow, the option tables leave for `_cli_options.py` first.
    # 983 -> 1038 (round 2, N5-N7): `bash -lc` and the other bundled `-c`
    # spellings are unwrapped; a `>`/`>>`/`1>`/`&>` inside a shell segment
    # marks the body as sent to a file (and `2>` does not); only argv[0]
    # after unwrapping decides `unclassified`, with the runner list that
    # says which programs execute their arguments. Fifty-five lines, each
    # one a reviewer's probe; the option-table move stands as the next step.
    "robothor/engine/sandbox_runtime/spawn_recorder.py": 1038,
    # The engine-side merge and summary of the spawn record: two pure
    # functions and the refusal/unobserved rules. Pinned at merge (M5).
    "robothor/engine/code_exec_spawns.py": 159,
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
