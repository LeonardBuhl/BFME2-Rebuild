"""Protected matrix preparation, per-runner native comparison and receipt join."""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import sys
import tempfile
from time import monotonic
from types import SimpleNamespace

from common import HEX64, PIN, digest, encoded, read, validate_binding, write
from compare import difference, make_report, native_commands, protected_judge, native_blockers, emitted_row_pairs, unexpected_symbol_pairs, measure_batch, measure_pair, memoized_projection
from emitted_data import record_pairs
from compact_matrix import make_compact, join_compact
from diff_scope import diff_lines
from edited_functions import edited
from finished_index import Owners
from gitrepo import Git
from intake import reconstruct
from managed_matrix import SIDES, assignment, check_native, compiler_receipt, make
from measure import audit, pin
from pool_protocol import safe_diagnostics
from reporting import GATES, STATES, numeric, safe_listing
from wide_plan import identity

RUNNERS = 24
BATCH = 8
MAX_SHARD_TUS = 4 * BATCH  # matches wide_recovery's 4-part-per-shard limit
MAX_RECEIPT = 4 * 1024**2


def admission(path, *, kinds=("wide",)):
    path = Path(path)
    saved = path / "plan.json"
    plan = validate_binding(read(saved))
    decision = read(path / "decision.json", 4096)
    plan_hash = digest(saved.read_bytes())
    if (decision["kind"] not in kinds or decision["plan"] != plan_hash
            or any(decision[key] != plan[key] for key in ("number", "repo", "base", "head", "tree"))
            or plan["base"] != os.environ["GITHUB_SHA"]):
        raise ValueError("The wide input differs from protected admission.")
    return plan, plan_hash


@contextmanager
def comparison_inputs(plan):
    baseline = (Path.cwd() / "baseline/game.dat").resolve(strict=True)
    pin(baseline, PIN["baseline_sha256"])
    tools = audit(PIN)
    with tempfile.TemporaryDirectory(prefix="bfme-managed-wide-") as name:
        work = Path(name)
        git = Git(work / "source.git", initialize=True)
        checkout = Git(Path.cwd() / ".git")
        if checkout.rev("HEAD") != plan["base"]:
            raise ValueError("The wide comparison has an unexpected base checkout.")
        alternates = git.path / "objects/info/alternates"
        alternates.write_text(str((checkout.path / "objects").resolve(strict=True)) + "\n")
        git.run("update-ref", "refs/heads/main", plan["base"])
        tip = reconstruct(git, plan)
        judge = protected_judge(work / "home", tools, baseline)
        judge.BARE = git.path
        yield work, git, tip, judge, tools


def choose_runner_count(tu_count, *, max_runners=RUNNERS, target_per_shard=10):
    if not 1 <= max_runners <= 32:
        raise ValueError("Select between 1 and 32 managed wide runners.")
    if not 1 <= target_per_shard <= 32:
        raise ValueError("Select between 1 and 32 translation units per wide shard.")
    # Each shard supports at most 4 recovery groups of 8 TUs (32 TUs).
    min_for_groups = (tu_count + 31) // 32
    if max_runners < min_for_groups:
        raise ValueError(f"Select at least {min_for_groups} wide runners for {tu_count} translation units.")
    desired = max(min_for_groups, (tu_count + target_per_shard - 1) // target_per_shard)
    return max(1, min(max_runners, desired, tu_count))


def tu_weights(git, base, tip, paths):
    """Approximate compile cost by source file byte size so LPT sharding avoids stragglers."""
    weights = {}
    for path in paths:
        size = 0
        for rev in (base, tip):
            res = git.run("cat-file", "-s", f"{rev}:{path}", check=False)
            if res.returncode == 0:
                try:
                    size = int(res.stdout.decode().strip())
                except ValueError:
                    size = 0
                if size > 0:
                    break
        weights[path] = max(1, size)
    return weights


def balanced_chunks(total, max_size=BATCH):
    """Split `total` items into even slices <= `max_size` (e.g. 9 -> [5, 4] instead of [8, 1])."""
    if total <= 0:
        return []
    groups = (total + max_size - 1) // max_size
    base, extra = divmod(total, groups)
    slices, start = [], 0
    for idx in range(groups):
        size = base + (1 if idx < extra else 0)
        slices.append((start, start + size))
        start += size
    return slices


def plan_matrix(source, out):
    plan, _ = admission(source)
    with comparison_inputs(plan) as (_, git, tip, judge, tools):
        commands = {side: native_commands(git, judge, rev, plan["request_paths"], tools)
                    for side, rev in (("base", plan["base"]), ("head", tip))}
        weights = tu_weights(git, plan["base"], tip, plan["request_paths"])
    max_runners = int(os.environ.get("MANAGED_WIDE_SHARDS", RUNNERS))
    target_per_shard = int(os.environ.get("MANAGED_WIDE_TUS_PER_SHARD", "10"))
    runners = choose_runner_count(len(plan["request_paths"]), max_runners=max_runners, target_per_shard=target_per_shard)
    document = make(plan, commands, limit=runners, weights=weights, max_tus_per_shard=MAX_SHARD_TUS)
    out.mkdir(mode=0o700)
    write(out / "matrix.json", document)
    indexes = list(range(len(document["partition"]["shards"])))
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
        stream.write("shards=[" + ",".join(map(str, indexes)) + "]\n")
        stream.write("matrix_sha=" + identity(document) + "\n")
        from wide_recovery import groups
        stream.write("groups=" + encoded([{"shard": shard, "part": part}
                                           for shard, part in groups(document)]).decode())

def load_matrix(path, expected, plan):
    doc = read(Path(path) / "matrix.json", 3 * 1024**2)
    if (not HEX64.fullmatch(expected) or identity(doc) != expected
            or doc["scope_sha256"] != identity(plan)
            or doc["partition"]["binding"] != {key: plan[key] for key in ("base", "head", "tree")}
            or doc["partition"]["compile_rows"] != 2 * len(plan["request_paths"])):
        raise ValueError("The sealed compile matrix differs from the protected plan.")
    return doc


def sanitize_outcome(raw, path, changed, *, judge=None, obj=None, baseline=None, include_symbols=True):
    def gate_key(value):
        if "annotation" in value:
            label = "annotations"
        elif "assembly" in value or " asm " in value:
            label = "assembly"
        else:
            label = next((phrase for phrase in GATES if phrase in value), "native")
        return label + " " + digest(value.encode())

    verdict = {"rows": [{key: row[key] for key in ("rva", "status", "credited", "proof", "pct") if key in row}
                        for row in raw["rows"]],
               "object_verified": bool(raw.get("object_verified")),
               "gates": sorted({gate_key(value) for value in raw["gates"]}),
               "blockers": native_blockers(raw, judge=judge, obj=obj)}
    if include_symbols:
        pairs = unexpected_symbol_pairs(raw, judge=judge, obj=obj, baseline=baseline)
        if pairs:
            verdict["unexpected_symbols"] = pairs
        epairs = emitted_row_pairs(raw, baseline=baseline)
        if epairs:
            verdict["emitted_rows"] = epairs
    if raw.get("error"):
        located = safe_diagnostics(path, raw["error"], tuple(sorted(changed)))
        diagnostics = "\n".join(line for line in located.splitlines() if line.split("(", 1)[0] in changed)
        verdict["error"] = diagnostics or "Compilation failed in an affected translation unit."
    check_native(verdict)
    return verdict


def clean_difference(value, listing):
    return {key: field for key, field in {
        "first": numeric(value.get("first"), 2**31),
        "compiled_size": numeric(value.get("compiled_size"), 2**31),
        "retail_size": numeric(value.get("retail_size"), 2**31),
        "compiled_listing": safe_listing(value.get("compiled_listing", [])) if listing else [],
    }.items() if field is not None}


def compile_shard(source, matrix, expected, index, out):
    plan, _ = admission(source)
    doc = load_matrix(matrix, expected, plan)
    compile_assigned(plan, doc, expected, index, out, label=str(index))


def compile_assigned(plan, doc, expected, index, out, *, label):
    commands_by_side = assignment(doc, index, expected=expected)
    started = monotonic()
    total = {stage: 0.0 for stage in ("compile", "compare")}
    units = len(commands_by_side[SIDES["base"]])
    print(f"Wide shard {label}: {units} translation units / {2 * units} paired sides planned.", flush=True)
    records, reports, details, inline_objects = [], {side: {} for side in SIDES}, {}, {}
    with comparison_inputs(plan) as (work, git, tip, judge, tools):
        owners = Owners(git, plan["base"])
        adapter = SimpleNamespace(ANNOT=judge.ANNOT, land_gate9=SimpleNamespace(diff_lines=diff_lines))
        edited_keys = {(item["owner"], item["address"]) for item in edited(git, adapter, plan["base"], tip, owners)}
        changed = set(plan["changed_paths"])
        base_commands = commands_by_side[SIDES["base"]]
        head_commands = commands_by_side[SIDES["head"]]
        base_paths = [row["tu"] for row in base_commands]
        head_paths = [row["tu"] for row in head_commands]
        if base_paths != head_paths:
            raise ValueError("Shard base and head assignments differ.")
        for side, rev, commands, paths in (("base", plan["base"], base_commands, base_paths),
                                           ("head", tip, head_commands, head_paths)):
            if native_commands(git, judge, rev, paths, tools) != commands:
                raise ValueError("A shard command differs from the protected native compiler settings.")
        job = None
        with memoized_projection():
            for chunk_index, (begin, end) in enumerate(balanced_chunks(len(base_paths), BATCH), start=1):
                b_batch = base_commands[begin:end]
                h_batch = head_commands[begin:end]
                names = [command["tu"] for command in b_batch]
                native = dict(plan, historical_base=plan["base"], historical_head=tip,
                              request_paths=names, compile={"base": b_batch, "head": h_batch})
                b_job = work / f"base-{begin}"
                h_job = work / f"head-{begin}"
                b_spans, h_spans = {}, {}
                try:
                    b_results, h_results = measure_pair(
                        git, judge, native, tools, b_job, h_job,
                        base_timing=b_spans, head_timing=h_spans,
                    )
                finally:
                    batch_compile = b_spans.get("compile", 0.0) + h_spans.get("compile", 0.0)
                    batch_compare = b_spans.get("compare", 0.0) + h_spans.get("compare", 0.0)
                    total["compile"] += batch_compile
                    total["compare"] += batch_compare
                    print(f"Wide shard {label}: paired batch {chunk_index}, {len(names)} files, "
                          f"compiler RPC {batch_compile:.2f} s, native comparison {batch_compare:.2f} s.", flush=True)
                job = h_job
                for side, batch, results, side_job in (("base", b_batch, b_results, b_job),
                                                       ("head", h_batch, h_results, h_job)):
                    if list(results) != names:
                        raise ValueError("The trusted comparator omitted a planned TU.")
                    for command in batch:
                        path = command["tu"]
                        raw = results[path]
                        obj = side_job / "obj" / (path.replace("/", "_") + ".obj")
                        value = {key: command[key] for key in ("tu", "source", "contract")} | {"side": SIDES[side]}
                        base_outcome = reports["base"].get(path) if side == "head" else None
                        reports[side][path] = sanitize_outcome(
                            raw, path, changed, judge=judge, obj=obj if obj.is_file() else None,
                            baseline=base_outcome, include_symbols=(side == "head"),
                        )
                        if obj.is_file():
                            inline_objects[(SIDES[side], path)] = obj
                            local = read(obj.with_suffix(".obj.compile.json"), 4 * 1024**2)
                            if (local["source_sha256"] != command["source"] or local["compiler_contract"] != command["contract"]
                                    or not HEX64.fullmatch(local["object_sha256"])):
                                raise ValueError("The local comparison changed a verified compiler receipt.")
                            dependencies = sorted((str(Path(dep_path).relative_to(side_job)) if Path(dep_path).is_relative_to(side_job)
                                                   else "sealed/" + Path(dep_path).name, checksum)
                                                  for dep_path, checksum in local["headers"].items())
                            value.update(status="compiled", object_sha256=local["object_sha256"], dependencies=dependencies)
                        elif raw.get("error"):
                            value["status"] = "error"
                        else:
                            raise ValueError("The local comparison omitted the compiler outcome.")
                        records.append(value)
                        if side == "head" and obj.is_file():
                            for row in raw["rows"]:
                                if row["status"] != "PARTIAL":
                                    continue
                                listing = (path, int(row["rva"], 16)) in edited_keys
                                record = difference(judge, obj, row, listing)
                                if record:
                                    details[path + "@" + row["rva"]] = clean_difference(record, listing)
        # Both authenticated objects of a pair stay on one runner.
        objects = [(path, inline_objects.get((SIDES["base"], path)), inline_objects.get((SIDES["head"], path)))
                   for path in reports["head"]]
        record_pairs(judge, job, objects, reports["head"])
        compiler = compiler_receipt(doc, index, records)
        atom_started = monotonic()
        try:
            from virtual_atoms import collect
            inline = collect(compiler, find_object=lambda side, tu: inline_objects[(side, tu)])
        except Exception:
            inline = None
            print("Inline virtual advisory evidence is unavailable for this shard.", file=sys.stderr)
        print(f"Wide shard {label}: virtual extraction {monotonic() - atom_started:.2f} s.", flush=True)
    print(f"Wide shard {label}: {units} paired files, compiler RPC {total['compile']:.2f} s, "
          f"native comparison {total['compare']:.2f} s, elapsed {monotonic() - started:.2f} s.", flush=True)
    receipt = make_compact(doc, expected=expected, run=os.environ["GITHUB_RUN_ID"],
                           attempt=os.environ["GITHUB_RUN_ATTEMPT"],
                           compiler=compiler, native=reports, details=details, inline=inline)
    if inline is not None and len(encoded(receipt)) > MAX_RECEIPT:
        receipt["inline"] = None
        print("Inline virtual advisory evidence exceeded its shard budget.", file=sys.stderr)
    out.mkdir(mode=0o700)
    target = out / "shard.json"
    write(target, receipt)
    if target.stat().st_size > MAX_RECEIPT:
        raise ValueError("The protected shard verdict exceeded its size allowance.")


def join_receipts(doc, artifacts, expected, run, attempt):
    expected_dirs = {f"shard-{index}" for index in range(len(doc["partition"]["shards"]))}
    if {item.name for item in artifacts.iterdir()} != expected_dirs:
        raise ValueError("The wide comparison is missing a shard or has an unexpected one.")
    receipts = [read(artifacts / folder / "shard.json", MAX_RECEIPT) for folder in sorted(expected_dirs)]
    tables, details, _inline = join_compact(doc, receipts, expected=expected, run=run, attempt=attempt)
    partial = {path + "@" + row["rva"] for path, outcome in tables["head"].items()
               for row in outcome["rows"] if row["status"] == "PARTIAL"}
    if details.keys() - partial:
        raise ValueError("A shard supplied near-miss details outside its partial results.")
    return tables, details


def combine(source, matrix, expected, staging, out):
    plan, plan_sha = admission(source)
    doc = load_matrix(matrix, expected, plan)
    # Same-run downloads keep the artifact directory, including its attempt-prefix name.
    prefix = "bfme-wide-" + os.environ["GITHUB_RUN_ATTEMPT"] + "-"
    normalized = staging.parent / "shards-for-join"
    normalized.mkdir(mode=0o700)
    for path in staging.iterdir():
        index = path.name.removeprefix(prefix)
        if not path.name.startswith(prefix) or not re.fullmatch(r"(?:0|[1-9][0-9]?)", index) or not path.is_dir():
            raise ValueError("The wide download contains an unexpected artifact.")
        (normalized / ("shard-" + index)).symlink_to(path.resolve(strict=True))
    combine_selected(source, matrix, expected, normalized, out)


def combine_selected(source, matrix, expected, staging, out, *, lease=None, lease_sha=None):
    plan, plan_sha = admission(source)
    doc = load_matrix(matrix, expected, plan)
    tables, offsets = join_receipts(doc, staging, expected, os.environ["GITHUB_RUN_ID"],
                                    os.environ["GITHUB_RUN_ATTEMPT"])

    def lookup(path, row, own):
        result = offsets.get(path + "@" + row["rva"])
        return clean_difference(result, own) if result else None
    with comparison_inputs(plan) as (_, git, tip, judge, _tools):
        report, counts, functions = make_report(git, judge, plan, tip, tables["base"], tables["head"],
            lambda _path: None, lookup_difference=lookup)
    stats = {"runners": len(doc["partition"]["shards"]), "paired_results": 2 * len(tables["base"])}
    if lease:
        from wide_recovery import checked_lease
        assignment = read(lease, 65536)
        checked_lease(doc, expected, assignment, lease_sha)
        stats["recovery"] = {
            "primary_groups": len(assignment["primary"]),
            "reserved_groups": len(assignment["reserve"]),
            "reserve_slots": len(set(assignment["reserve"].values())),
        }
    out.mkdir(mode=0o700)
    write(out / "verdict.json", {key: plan[key] for key in ("version", "repo", "number", "base", "head", "tree")}
          | {"plan": plan_sha, "report": report, "counts": counts, "functions": functions,
             "matrix": stats})


def main(mode, *args):
    os.umask(0o077)
    if mode == "plan":
        plan_matrix(*map(Path, args))
    elif mode == "compile":
        source, matrix, expected, index, out = args
        compile_shard(Path(source), Path(matrix), expected, int(index), Path(out))
    elif mode == "join":
        source, matrix, expected, staging, out = args
        combine(Path(source), Path(matrix), expected, Path(staging), Path(out))
    elif mode == "join-selected":
        source, matrix, expected, staging, out, lease, lease_sha = args
        combine_selected(Path(source), Path(matrix), expected, Path(staging), Path(out),
                         lease=Path(lease), lease_sha=lease_sha)
    else:
        raise ValueError("Unknown protected matrix operation.")


if __name__ == "__main__":
    main(*sys.argv[1:])

