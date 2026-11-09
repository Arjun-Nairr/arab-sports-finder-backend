# Algorithm review and demo follow-up

The minimum demo is one accurate, fully automated five-slide carousel with a working private preview. The batch target is three different eligible footballers. Keep local Python, the research database, the source adapters and the static showcase. Prefer fewer calls and simpler decision rules when they give comparable results. Propose changes at existing stage boundaries so we can improve the workflow without rebuilding it.

## AR-01 — Review the algorithm on saved evidence

**Ready. Blocked by none.** Start from commit `13df6e6`, the [demo spec](outputs/demo-spec.md), and the [workflow overview](README.md). Trace discovery, eligibility, shortlisting, angle selection, drafting and verification. Use the saved shortlist and source evidence from the November 9 batch when available locally. The repository fixtures cover specific failures and cannot by themselves reconstruct that live shortlist.

- [ ] Compare the current Laya-first shortlist with a deterministic shortlist based on the same eligibility rules and usable source evidence. Use the same input, candidate cap and tie-breaking policy across both. Show which players each would send to Groq, in what order, and why. State if any required saved inputs are unavailable.
- [ ] Explain what each model call contributes and where narrower evidence selection or fewer calls could improve cost or reliability. Use observed failures and call counts where available. Keep estimates separate from observations and leave live model comparisons for a later decision.
- [ ] Recommend the smallest durable change, or recommend keeping the current design. Explain the trade-offs for evidence quality, lesser-known players, and maintenance. Say which of AR-02 through AR-04 still apply. Return a short review with code references before implementation starts.

## AR-02 — Give an advanced angle enough room to draft

**Blocked by AR-01 and agreement on its recommendation.** In the latest batch, earlier angle calls left Younis Mahmoud without a structural revision while the batch had 18 calls left.

- [ ] Reproduce the case with offline fixtures. When an angle advances and the batch has sufficient budget, drafting can make a structural revision and still verify the result.
- [ ] The batch stops at the existing limits of 24 Groq calls, five players tried or three ready drafts. Exhaustion is reported clearly and no failed draft is marked ready.

## AR-03 — Carry Groq pacing and provider failures across stories

**Blocked by AR-01 and agreement on its recommendation.**

- [ ] A fake-clock test shows that the second story respects the first story's remaining request and token window. Keep the existing aggregate call cap and per-story logs.
- [ ] A provider failure during verification preserves the draft and stops the batch with the provider error. A story with insufficient evidence can still advance to a different player.

## AR-04 — Resolve valid excerpt IDs in fact citations

**Blocked by AR-01 and agreement on its recommendation.** A proposal used valid excerpt IDs such as `P8.3` where facts expected a passage ID such as `P8`.

- [ ] An offline proposal using a valid excerpt ID is resolved against the stored text. It advances when the remaining evidence checks pass.
- [ ] Unknown IDs and invalid cross-source facts still fail. Existing passage-ID citations continue to work.

## AR-05 — Review the changes and run one live batch

**Blocked by Codex review of whichever implementation tickets AR-01 retains.** Claude implements and tests the agreed slices. Codex reviews the diff against the tickets before the live run.

- [ ] Run the batch once with existing free-access limits and no manual edits to player selection, angle, facts, copy or visuals. Record each player tried and the outcome.
- [ ] Open every ready private preview and check all five slides, the facts and the source links. Record the preview paths. At least one working preview meets the demo milestone. If zero are ready, record the exact blocker and leave the milestone open.
- [ ] Leave public export to a separate independent review and source-use decision.
