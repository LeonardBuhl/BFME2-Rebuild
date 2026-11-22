# Automatic admission

A pull request gets its compiler check without anyone pressing a button when it changes source files only.
`.github/workflows/admission.yml` decides for each new commit, using `admit.py`. It runs this repository's own
code on a GitHub-hosted runner, and reads your pull request through the GitHub API only. Your files are never
checked out or run there; the compiler check compiles them, and that is all.

| Your pull request changes | What happens |
|---|---|
| Only files under `src/`, at most 8, and the source rules passed on this commit | Admitted automatically. `BFME II / Admission` reads `Admitted by rule: …`, and the compiler check starts. |
| Source files, but the source rules failed | Not admitted. Fix what the source rules name and push. |
| More than 8 files, or anything under `include/`, `ci/`, `tools/`, `config/`, `expected/`, `baseline/`, or outside `src/` | Held. A maintainer reads it. |
| Anything under `.github/` | Not admitted here. A maintainer merges workflow changes by hand. |

Every new push is judged again for its new commit, so you never need to ask for the check to run again.

**Daily limits** (repository variables `BFME_ADMISSION_DAILY_TOTAL` and `BFME_ADMISSION_DAILY_PER_AUTHOR`) apply to
contributors outside the organisation. One measurement is one exact commit measured against one exact `main`. When
a limit is reached, the status reads `Held: daily limit, retried automatically`, and an hourly run admits held
commits, oldest first, as room frees up. If the limits are missing or invalid, everything is held; nothing is
admitted without counting. Organisation members and branches of this repository are admitted the same way but are
not counted.

## The ledger (for the compiler check)

`ledger.py` keeps one file, `ledger.json`, on the branch `admission-ledger`, written only by this repository's
workflows with compare-and-set. Each entry reserves one measurement of an exact (pull request, head, main, merged
tree), with its kind (`narrow`, `trusted-narrow`, or `wide`) and the admission run that issued it. The compiler
check calls `claim(…)` with the tree, kind and issuer it inspected, on a hosted runner, before any compiler
machine starts. Only the first matching run attempt gets `claimed`. A rerun, a duplicate delivery, or any other
tree, kind or issuer gets nothing.

`source_approval.py` requires, before any reservation, the exact source-rules receipt of this commit on this
`main`. That means all eight rules plus the total, from one protected run, written by GitHub Actions, with the
same merged tree and policy.

A reservation is used once. When the compiler check that used it finished without measuring anything (the machine
failed, so there is no verdict), the hourly run frees that reservation for another check. It does this at most
four times for one commit on one main without double-charging the daily limits (pausing automatically during a
runner outage), and after the fifth unmeasured run writes
`Held: the compiler check failed to run five times; a maintainer will look`. A commit that has any
verdict for that main is never measured again.

## When main moves

GitHub does not recompute an open pull request's merge preview, or its "mergeable" flag, when `main` moves. Admission
therefore never waits for them after a landing. The hourly run and a landing request take the merged tree from the
protected source-rules receipt for exactly this commit on the current `main` and the current rules. The compiler
check composes the same merge again and refuses a different tree before any machine starts. When there is no
receipt yet (whether or not GitHub has caught up with its preview), admission asks the source rules workflow for one (`bfme-source-refresh`: source rules only, no compiler
machine). It does this at most once an hour for one commit on one `main`, and the status reads
`Held: checking the source rules on main …, retried automatically`. As soon as the source rules have published,
that workflow asks admission to decide again for this pull request, so it is admitted within minutes, not at the
next hourly run. A person's push to `main` starts the same run at once (through `admission-push.yml`), so nothing
waits for the hour after an install. If the source rules find that the commit conflicts with the new `main`, or shares no history with
it, the status says so and nothing more is requested until you push. A receipt for another `main`, another commit,
another pull request or other rules is never used.

Tests: `python3 -m unittest discover -s ci/admission/tests -v` from the repository root.
