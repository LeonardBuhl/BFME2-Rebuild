# AGENTS.md

Goal: rebuild the Battle for Middle-earth II engine (`game.dat` 1.06) as clean, readable, maintainable C++ that
compiles 1-to-1 with Visual C++ 7.1 (`cl.exe 13.10.3077`) to the original machine code, so the engine can be
patched and extended.

## Where things live

| Path | What it is |
|---|---|
| `src/` | source files; `src/unk/O<addr>.cpp` are files whose original name is not yet known |
| `include/` | shared headers; `include/Draft/` are unverified layouts used only until a real header exists |
| `config/map.json` | the ledger: every function and data item of the original, with address, name, owner file, status |
| `expected/` | generated check records; do not edit |
| `baseline/game.dat` | the original 1.06 binary; disassemble ranges here to verify exact behavior |
| `reference/` | Zero Hour source (predecessor game); a reference for names and structure, never compiled |
| `worklist/` | prioritized work queues regenerated from `main` |
| `docs/` | source-quality rules, reference files, recipes, and recorded hard cases |
| `tools/`, `ci/` | local source gate (`tools/gates/gate.py`), local judge docs (`tools/README.md`), and CI; do not edit in source PRs |

## Work selection and claiming (avoid swarm collisions)

1. Always start from current upstream `main` (`main` moves continuously).
2. **Search upstream open pull requests before choosing** (claims live in PR descriptions, not only titles):
   ```sh
   gh pr list -R Open-BFME/BFME2-Rebuild --state open --search "<RVA or file or header>"
   ```
   Also search `src/` and `include/` for the target address, since the worklist snapshot can lag `main`.
3. Spread picks across the worklist instead of piling onto row 1:
   - **Fastest auto-land lane:** `src/`-only items from `worklist/files.tsv` (few missing functions, real header
     present) or `worklist/unwritten_with_zero_hour_counterpart.tsv` (`ready_at_snapshot=yes`,
     `reference_kind=def`, `body_check=none-found`, small `size_bytes`).
   - **Header / Draft work:** `worklist/draft_headers_in_use.tsv` (add a permanent header under `include/`, move all
     includers, delete the `include/Draft/` file) or adding missing declarations to an existing shared header.
   - **Evidence & hard cases:** open `Evidence:` / `Path:` / `Ledger:` issues, or record a verified blocker in
     `docs/hard_rows.md`.
4. **Claim before implementing:** either open a draft pull request first with `Claim: src/path/File.cpp`,
   `Claim: include/Draft/X.h`, or `Claim: 0x00ABCDEF` (or have your work scheduler assign an exclusive scope) so
   two agents never write the same function.
5. Keep **one logical item per pull request** (G6 ceiling: at most 3 substantive source files, with the narrow
   Draft-promotion exception; automatic narrow admission covers 1-8 eligible `src/` paths affecting at most 8 TUs).

## Fast, zero-waste loop

1. **Read the bytes and write clean C++.**
   - Disassemble the target in `baseline/game.dat` (`VA = RVA + 0x400000`, e.g.
     `objdump -d -M intel --start-address=<VA> --stop-address=<VA+size> baseline/game.dat`).
   - Read the Zero Hour counterpart under `reference/` if one exists. Where Zero Hour and `baseline/game.dat`
     disagree, **follow `baseline/game.dat`**.
   - Write ordinary, typed C++ directly below the owning `// FUNCTION:` line in the file `config/map.json` names,
     against shared headers and real engine/STLport templates.
2. **Commit your change on top of current upstream `main`, then run the local source gate BEFORE pushing:**
   ```sh
   python3 tools/gates/gate.py . <upstream-main-ref> HEAD --json /tmp/gate.json
   ```
   `gate.py` reads committed Git trees (it ignores uncommitted working-tree edits). Fix every G1-G8 finding until
   `gate.py` **exits 0 and `/tmp/gate.json` reports `"status": "pass"`**. Never spend a Boat run on a G1-G8 failure.
   - If you edited a header under `include/`, check syntax and layout (`sizeof`/`offsetof`) with a compatible 32-bit
     x86 ABI compiler first.
   - If you have the Visual C++ 7.1 toolchain locally, follow `tools/README.md` to pre-verify before pushing (do not
     run `tools/compiler-image/build.sh` on a workstation; it is for throwaway machines).
3. **Mark the pull request Ready for review when you want automatic Boat compilation.**
   - **Draft PRs:** **BFME II / source rules** can run, but **automatic Boat admission waits until the PR is marked
     Ready for review** (maintainer-started checks are separate).
   - **Ready `src/`-only fork PRs** (1-8 eligible `src/` paths, <= 8 affected TUs, passing source rules, within the
     daily limits—currently 60/author and 300 total/day): automatically admitted, compiled on Boat, and
     squash-merged to `main` once **BFME II / source rules** pass on current `main` and **BFME II / Boat** passed
     on current `main` (or a recent disjoint `src/Code/`-only ancestor) with a clean merge and no landing hold. (**BFME II / Inline virtual** pending can land; a fresh failure blocks;
     a stale failure triggers re-measurement.)
   - **Header PRs (`include/`):** ready fork PRs changing `include/` (with or without `src/`, up to 60 files,
     passing current source rules) are admitted automatically under the wide daily budget (currently 10/author and
     40 total/day). Boat compares all affected TUs including transitive includers (narrow for <= 8 affected TUs,
     wide for larger eligible sets). Header PRs auto-land once **BFME II / Header rules** pass on current `main`
     (including member layout changes proven by a matching typed-access witness under H5); header PRs whose Header
     rules are held (not failed) wait for approval on that exact head from a listed maintainer in
     `ci/landing/landing.json`.
4. **Read the exact status before acting (at most 3 measured code-fix rounds).**
   - **`BFME II / Admission`:** tells you whether the commit was admitted, held for daily budget (retried
     automatically), held for a rebase/conflict (update your branch and push), or waiting on a maintainer.
   - **`BFME II / Boat`:** open **Details** for per-function verdicts (`MATCH / proven`, `PARTIAL` with first
     differing offset and compiled instructions when available, `COMPILE_ERROR`, or refusing rule). Fix code
     refusals and push. Unmeasured infrastructure faults retry automatically without double-charging your daily budget (up to four free retries before a maintainer hold), and pause automatically during a runner outage;
     do not force-push blindly on a transient retry.
   - **`BFME II / Landing`:** reports why a ready PR has not merged yet (`measuring`, `check-failure`,
     `merge-conflict`, `header-approval`, etc.).
   - Count the 3-round budget against **measured code refusals**. If a function still does not match cleanly after 3
     rounds, record the concrete blocker in an issue or `docs/hard_rows.md`, close the PR, and move on.

## Hard rules for clean, 1-to-1 C++

- **One definition:** Never duplicate a class, struct, enum, or global that exists anywhere under `include/` or
  `src/`. Replacing a Draft header adds the permanent header under `include/`, moves every includer, and deletes the
  `include/Draft/` file in the same PR.
- **Header discipline:** Keep function bodies out of shared headers unless supported by retail evidence. Unowned
  inline definitions or constructors in shared headers emit unexpected code in every includer.
- **Real types & real templates:** No `void *` or integer standing in for a known type, no caller casts to recover a
  type, and reuse real STLport/engine templates rather than hand-written copies.
- **Evidence-backed names:** Every new or changed name must come from `baseline/game.dat` (string, export, class
  vtable) or a corresponding Zero Hour symbol. Unknown names stay placeholders.
- **Inline and inherited virtuals:** Any added or changed inline or inherited virtual behavior must match the target
  class's virtual table slot in `baseline/game.dat` and cite that slot and observed behavior in a comment.
- **Path and annotation boundaries (G1/G2):** Edit only existing `.c`/`.cpp` under `src/` and eligible headers under
  `include/` (adding permanent headers or deleting `include/Draft/` headers is allowed). No new `.cpp` files, no
  moving functions between files, and **never alter `// FUNCTION:` or `// cl:` lines**.
- **No hacks (G3):** No inline `__asm`, emitted bytes, `#pragma optimize`, or casts/dead code used to steer codegen.
- **Clean comments & commit trailers (G5):** New comments explain *why* (no addresses/sizes repeated from
  `// FUNCTION:`, no dates, no agent/tool tags). Add a `Removed-Declaration: Class::member` trailer to the tip commit
  message for every header declaration removed or renamed. Never paste disassembly or binary dumps into PRs, issues,
  or code.
- **Zero regressions:** Nothing that previously matched, verified, or proved its references may regress.

## Done

A function is finished when it matches 1-to-1 (`MATCH / proven`), passes G1-G8, lives in its original named
translation unit, uses real shared headers and types, and reads like the files in `docs/reference-files.md`. Full
guide: `CONTRIBUTING.md`, `docs/agents.md`, `RULES.md`, and `REVIEW.md`.
