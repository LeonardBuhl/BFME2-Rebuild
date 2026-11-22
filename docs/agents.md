# Get your agents started

This page is for people who point AI coding agents at this repository. [AGENTS.md](../AGENTS.md) is the short brief the
agent reads by itself; this page is for you, and for the agent to read once.

## What an agent needs

- `git`, and a way to open pull requests (the `gh` command is enough).
  On Windows, clone with `-c core.longpaths=true`: some reference paths are long.
- `objdump` from binutils, to read the original program. Any disassembler
  that opens a 32-bit Windows program will do.
- No compiler, no Wine and no copy of the game. The checks compile on the
  project's machines and tell the agent what happened.

## The loop

1. **Pick one item.** A small function is the best first task: take a row of
   `worklist/unwritten_with_zero_hour_counterpart.tsv` with
   `ready_at_snapshot=yes`, `reference_kind=def`, `body_check=none-found` and
   a `size_bytes` under about 100. The newest lists are on the
   [`progress` branch](https://github.com/Open-BFME/BFME2-Rebuild/tree/progress).
2. **Make sure it is free and claim it first.** Search upstream open pull
   request descriptions (claims live in bodies, not only titles):

   ```sh
   gh pr list -R Open-BFME/BFME2-Rebuild --state open --search "<RVA or file>"
   ```

   Search `src/` and `include/` for the address too, since the worklist can lag
   `main`. Claim the item before implementing by opening a draft pull request
   with your `Claim:` line (or taking an exclusive scope from a swarm
   scheduler).
3. **Read the original.** Each function has a `// FUNCTION:` line in the file
   that owns it, giving its address (an RVA) and size. Add `0x400000` to the
   RVA and disassemble that range:

   ```sh
   # FrameData::allCommandsReady: RVA 0x005DA563, size 75 (0x4B)
   objdump -d -M intel --start-address=0x9DA563 --stop-address=0x9DA5AE baseline/game.dat
   ```

4. **Read the counterpart.** The worklist row names the matching Zero Hour
   function under `reference/`. It is a guide to names, types and shape. Where
   it and the bytes disagree, the bytes are right.
5. **Write the function** directly below its `// FUNCTION:` line, against the
   shared headers and real templates, in ordinary C++. Commit on top of current
   upstream `main`.
6. **Run the local source gate on the committed tree before pushing** so you
   catch G1-G8 issues without spending a compiler run:

   ```sh
   python3 tools/gates/gate.py . <upstream-main-ref> HEAD --json /tmp/gate.json
   ```

   Fix every finding until `gate.py` **exits 0 and `/tmp/gate.json` reports
   `"status": "pass"`**. If you edited a header under `include/`, check syntax
   and layout (`sizeof`/`offsetof`) with a compatible 32-bit x86 ABI compiler
   first, and keep function bodies out of shared headers unless supported by
   retail evidence.
7. **Mark the pull request Ready for review** with the claim and evidence in
   the [template](../.github/pull_request_template.md): `Claim: 0x005DA563` for
   one function, `Claim: src/path/File.cpp` for a file. Draft pull requests can
   run source rules, while **automatic Boat admission waits until the pull
   request is marked Ready for review** (maintainer-started checks are
   separate).
8. **Read the exact statuses and fix.** Check **BFME II / Admission**,
   **BFME II / Boat** (open **Details** for partial offsets and instruction
   excerpts when available), and **BFME II / Landing**. Fix measured code
   refusals and push (up to three rounds). Wait on automatic budget/infra
   retries (eligible unmeasured runs retry up to twice before a maintainer
   hold), and rebase on an explicit conflict. If it still does not match
   cleanly after three measured rounds, record what you learned in
   [hard_rows.md](hard_rows.md), close the pull request, and take another item.

When current source rules and Boat pass on a ready source-only pull request
against current `main` with a clean merge and no landing hold, it lands on
`main` automatically. (**BFME II / Inline virtual** pending can land; a fresh
failure blocks; a stale failure triggers re-measurement.)

## From a fork

- **Ready source-only pull requests** (1-8 eligible `src/` paths affecting at
  most 8 translation units, passing current source rules, within daily limits
  of 60/author and 300 total): admitted automatically, compiled on Boat, and
  auto-landed when green—no `boat-ok` label needed.
- **Header pull requests (`include/`):** ready fork pull requests changing
  `include/` (up to 60 files under `src/` and `include/`, passing current
  source rules) are admitted automatically under the wide daily budget
  (currently 10/author and 40 total/day). Boat compares all affected translation
  units, including transitive includers (narrow for at most 8 affected units;
  wide for larger eligible sets). Header PRs auto-land once **BFME II / Header rules** pass on current `main`
  (including member layout changes proven by a matching typed-access witness
  under H5); header PRs whose Header rules are held (not failed) wait for
  approval on that exact head from a listed maintainer in
  `ci/landing/landing.json`.
- Nothing from a pull request is ever executed: only compiled inside a sealed,
  network-isolated compiler sandbox, and all checks run from `main`.

## Running several agents (swarm tips)

- **Claim first and spread across the worklist:** search upstream open PR
  descriptions (`gh pr list -R Open-BFME/BFME2-Rebuild --state open --search ...`)
  and assign each agent an exclusive file or address slice so agents never pile
  onto the top row.
- **One logical item per PR:** G6 caps substantive edits at 3 source files
  (with the narrow Draft-promotion exception); staying within 1-8 `src/` paths
  and <= 8 affected TUs keeps your PR on the fast automatic lane.
- **Pre-flight locally:** commit first, then run `tools/gates/gate.py` before
  pushing; if you have Visual C++ 7.1 locally, use the local judge workflow in
  [tools/README.md](../tools/README.md) (do not run
  `tools/compiler-image/build.sh` on a workstation).
- **Prefer source-only first:** start with rows whose real headers already
  exist; only edit `include/` when a declaration is genuinely missing.
- **Start from a fresh `main`:** `main` advances frequently.

## What gets a pull request refused

The report names the rule. The full list is in [RULES.md](../RULES.md); these
are the ones agents meet most:

- Inline assembly, emitted bytes, or pragmas and casts used to force a match.
- A `void *` or an integer standing in for a type the code shows.
- A second definition of a class, struct or global that already exists.
- A changed `// FUNCTION:` or `// cl:` line.
- A new file (except a permanent header added when replacing and deleting an
  `include/Draft/` header), a function moved between files, or a change outside
  `src/` and `include/`.
- A comment that carries an address, a date or an author tag.
- Anything that matched before and no longer does.

Do not paste disassembly dumps into the repository, an issue or a pull
request: describe the address, the size and what the code does.

## A longer first prompt

```text
You are contributing to an open rebuild of the Battle for Middle-earth II
engine. Read AGENTS.md, docs/agents.md, CONTRIBUTING.md and one of the files
listed in docs/reference-files.md.

Task: write one unwritten function.
1. From worklist/unwritten_with_zero_hour_counterpart.tsv choose a row with
   ready_at_snapshot=yes, reference_kind=def, body_check=none-found and
   size_bytes under 100. Check that no open pull request mentions its address
   and that src/ and include/ hold no body for it.
2. Disassemble it from baseline/game.dat (address = RVA + 0x400000) and read
   its Zero Hour counterpart under reference/.
3. Claim the item before writing code (draft PR with "Claim: <address>" or
   exclusive swarm assignment).
4. Write the body below its FUNCTION line in the owning file (real types, no
   casts to force a match, no inline assembly, no new files), commit your change
   on current upstream main, and run
   `python3 tools/gates/gate.py . <upstream-main-ref> HEAD --json /tmp/gate.json`
   until it exits 0 with `"status": "pass"`.
5. Mark the pull request Ready for review with the evidence in the template's
   form (automatic Boat admission runs on ready PRs, not drafts).
6. Read the Admission, Boat, and Landing statuses. Fix measured code refusals
   and push, three rounds at most (wait on automatic budget/infra retries). If
   it passes, it will land automatically. If not, record what you learned in
   docs/hard_rows.md or an issue and close it.
```
