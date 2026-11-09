"""Leakage check for AgentPal-Bench v2 items.

    python benchmarks/agentpal-bench-v2/leakage_check.py [--report out.json]

Compares every v2 item (dev cases and episode steps, and any other bench dir passed with
--bench-dir) against:
  * the synthetic dataset: data/{train,validation,test}.jsonl when present; otherwise the
    split regenerated in memory with agentpal.dataset.generate(DEFAULT_SEED). The retired
    test split is only READ here for overlap; nothing is tuned on it;
  * the generator's own template pools in agentpal/dataset.py;
  * the v1 seed benchmark;
  * injection strings written by the rule author in tests/test_od_decision_9_adversarial.py.

Findings (any one fails the check, exit code 1):
  1. ID       a v2 request_id or agent_id equal to one in a reference source;
  2. DOMAIN   a v2 merchant domain that is NOT on the owner's approved list but appears in a
              reference source (approved domains are operator configuration and are shared
              by design);
  3. EXACT    a v2 purpose / business_justification / evidence description whose normalized
              text equals any reference text;
  4. NGRAM    a v2 text and a reference text sharing >= MIN_SHARED word trigrams AND at least
              CONTAINMENT_THRESHOLD of the smaller text's trigrams.

Normalization: NFKC, zero-width/format characters removed, case-folded, every run of
non-alphanumeric characters replaced by one space. Texts with fewer than 3 tokens are only
checked for exact matches (they have no trigram). CJK text without spaces is one token, so
it can only match exactly; that is acceptable because the references are English-only.

Threshold rationale (also in benchmarks/agentpal-bench-v2/README.md): one shared trigram is common
in ordinary English ("for the team"), so a single shared trigram is never a finding. Two or
more shared trigrams covering half of the shorter text means one text substantially
reproduces the other (for example a template copied with a suffix), which is what this
check must catch.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACKAGE_ROOT = HERE.parent.parent                 # .../agentpal (project root)
NGRAM = 3
MIN_SHARED = 2
CONTAINMENT_THRESHOLD = 0.5
TEXT_FIELDS = ("purpose", "business_justification")

_FORMAT = re.compile(r"[­͏؜ᅟᅠ឴឵᠋-᠏"
                     r"​-‏‪-‮⁠-⁯ㅤ︀-️﻿]")
_NON_ALNUM = re.compile(r"[\W_]+", re.UNICODE)


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "")
    t = _FORMAT.sub("", t).casefold()
    return " ".join(_NON_ALNUM.sub(" ", t).split())


def grams(text: str, n: int = NGRAM) -> set[tuple[str, ...]]:
    toks = normalize(text).split()
    return {tuple(toks[i:i + n]) for i in range(len(toks) - n + 1)}


# ------------------------------------------------------------------ item extraction

def _requests(item: dict) -> list[tuple[str, dict]]:
    if "request" in item:
        return [(item["id"], item["request"])]
    return [(f"{item['id']}#{i}", s["request"]) for i, s in enumerate(item.get("steps", []))]


def item_texts(item: dict) -> list[tuple[str, str, str]]:
    """(unit_id, field, text) for purpose, justification and evidence descriptions."""
    out = []
    for uid, req in _requests(item):
        for f in TEXT_FIELDS:
            if req.get(f):
                out.append((uid, f, req[f]))
        for i, e in enumerate(req.get("evidence") or []):
            if e.get("description"):
                out.append((uid, f"evidence[{i}].description", e["description"]))
    return out


def item_ids(item: dict) -> list[tuple[str, str, str]]:
    out = []
    for uid, req in _requests(item):
        out.append((uid, "request_id", req["request_id"]))
        out.append((uid, "agent_id", req["agent_id"]))
    return out


def item_domains(item: dict) -> list[tuple[str, str]]:
    out = []
    for uid, req in _requests(item):
        if req.get("merchant_domain"):
            out.append((uid, req["merchant_domain"].lower()))
        for e in req.get("evidence") or []:
            if e.get("merchant_domain"):
                out.append((uid, e["merchant_domain"].lower()))
    return out


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def load_items(bench_dir: Path) -> list[dict]:
    return read_jsonl(bench_dir / "cases.jsonl") + read_jsonl(bench_dir / "episodes.jsonl")


# ------------------------------------------------------------------ reference corpus

class Reference:
    def __init__(self) -> None:
        self.texts: dict[str, set[str]] = defaultdict(set)    # normalized text -> sources
        self.raw: dict[str, str] = {}
        self.ids: dict[str, set[str]] = defaultdict(set)
        self.domains: dict[str, set[str]] = defaultdict(set)
        self.sources: dict[str, dict] = {}

    def add_text(self, text: str | None, source: str) -> None:
        if text and normalize(text):
            n = normalize(text)
            self.texts[n].add(source)
            self.raw.setdefault(n, text)

    def add_id(self, value: str | None, source: str) -> None:
        if value:
            self.ids[value].add(source)

    def add_domain(self, value: str | None, source: str) -> None:
        if value:
            self.domains[value.lower()].add(source)

    def add_request(self, req: dict, source: str) -> None:
        for f in ("purpose", "business_justification"):
            self.add_text(req.get(f), source)
        self.add_id(req.get("request_id"), source)
        self.add_id(req.get("agent_id"), source)
        self.add_domain(req.get("merchant_domain"), source)
        for e in req.get("evidence") or []:
            self.add_text(e.get("description"), source)
            self.add_domain(e.get("merchant_domain"), source)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_reference(project_root: Path = PACKAGE_ROOT, include_v1: bool = True,
                    include_data: bool = True) -> Reference:
    sys.path.insert(0, str(project_root))
    ref = Reference()
    # 1. synthetic dataset splits (read-only)
    if include_data:
        data_dir = project_root / "data"
        present = [s for s in ("train", "validation", "test")
                   if (data_dir / f"{s}.jsonl").exists()]
        if present:
            rows = []
            for s in present:
                path = data_dir / f"{s}.jsonl"
                split_rows = read_jsonl(path)
                rows += [(s, r) for r in split_rows]
                ref.sources[f"data/{s}.jsonl"] = {"rows": len(split_rows), "sha256": _sha(path)}
        else:  # data/ is git-ignored: regenerate the same seed in memory
            from agentpal.dataset import DEFAULT_SEED, generate
            rows = [(ex.split, ex.__dict__) for ex in generate(DEFAULT_SEED)]
            ref.sources["data (regenerated in memory, seed "
                        f"{DEFAULT_SEED})"] = {"rows": len(rows)}
        for split, r in rows:
            src = f"data/{split}"
            ref.add_request(r["request"], src)
            for h in r.get("context", {}).get("history", []):
                ref.add_id(h.get("request_id"), src)
                ref.add_id(h.get("agent_id"), src)
                ref.add_domain(h.get("merchant_domain"), src)
    # 2. generator template pools
    from agentpal import dataset as ds
    src = "agentpal/dataset.py templates"
    pools = [p for c in ds.PURPOSES.values() for p, _ in c]
    pools += [j.format(project=p) for j in ds.JUSTIFICATIONS for p in ds.PROJECTS]
    pools += ds.INJECTIONS_OBVIOUS + ds.INJECTIONS_SUBTLE + ds.PRESSURE
    pools += [p for p, _ in ds.MISMATCH_PURPOSES]
    for t in pools:
        ref.add_text(t, src)
    for _name, dom, _ in ds.UNAPPROVED_LEGIT:
        ref.add_domain(dom, src)
    ref.sources[src] = {"texts": len(pools), "sha256": _sha(Path(ds.__file__))}
    # 3. v1 seed benchmark
    if include_v1:
        v1 = project_root / "benchmarks" / "agentpal-bench-v1"
        for item in load_items(v1):
            for _, req in _requests(item):
                ref.add_request(req, "agentpal-bench-v1")
        ref.sources["agentpal-bench-v1"] = {
            f: _sha(v1 / f) for f in ("cases.jsonl", "episodes.jsonl") if (v1 / f).exists()}
    # 4. injection strings written by the rule author in the adversarial tests
    test_file = project_root / "tests" / "test_od_decision_9_adversarial.py"
    if test_file.exists():
        tree = ast.parse(test_file.read_text(encoding="utf-8"))
        n = 0
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id in ("KNOWN_INJECTIONS", "UNDETECTED_INJECTIONS")):
                for elt in node.value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        ref.add_text(elt.value, "tests/test_od_decision_9_adversarial.py")
                        n += 1
        ref.sources["tests/test_od_decision_9_adversarial.py"] = {"texts": n}
    return ref


def approved_domains(project_root: Path = PACKAGE_ROOT) -> set[str]:
    policy = json.loads((project_root / "config" / "risk_policy.json").read_text("utf-8"))
    return {m["domain"].lower() for m in policy["approved_merchants"]}


# ------------------------------------------------------------------ the check

def check(items: list[dict], ref: Reference, approved: set[str],
          label: str = "v2") -> dict:
    findings: list[dict] = []
    # 1 identifiers
    for item in items:
        for uid, kind, value in item_ids(item):
            if value in ref.ids:
                findings.append({"type": "ID", "unit": uid, "field": kind, "value": value,
                                 "sources": sorted(ref.ids[value])})
    # 2 non-approved domains
    domain_overlap_approved = set()
    for item in items:
        for uid, dom in item_domains(item):
            if dom in ref.domains:
                if dom in approved:
                    domain_overlap_approved.add(dom)
                else:
                    findings.append({"type": "DOMAIN", "unit": uid, "value": dom,
                                     "sources": sorted(ref.domains[dom])})
    # 3 + 4 texts
    index: dict[tuple[str, ...], set[str]] = defaultdict(set)
    ref_grams = {}
    for norm in ref.texts:
        g = grams(norm)
        ref_grams[norm] = g
        for x in g:
            index[x].add(norm)
    all_ref_grams = set(index)
    v2_texts = [t for item in items for t in item_texts(item)]
    max_containment, shared_any, novel, total = [], 0, 0, 0
    for uid, field, text in v2_texts:
        norm = normalize(text)
        if norm in ref.texts:
            findings.append({"type": "EXACT", "unit": uid, "field": field, "text": text,
                             "sources": sorted(ref.texts[norm])})
            max_containment.append(1.0)
            continue
        g = grams(text)
        total += len(g)
        novel += len(g - all_ref_grams)
        counts: dict[str, int] = defaultdict(int)
        for x in g:
            for r in index.get(x, ()):
                counts[r] += 1
        best = 0.0
        for r, shared in counts.items():
            denom = min(len(g), len(ref_grams[r])) or 1
            c = shared / denom
            best = max(best, c)
            if shared >= MIN_SHARED and c >= CONTAINMENT_THRESHOLD:
                findings.append({"type": "NGRAM", "unit": uid, "field": field, "text": text,
                                 "reference": ref.raw[r], "shared_trigrams": shared,
                                 "containment": round(c, 3), "sources": sorted(ref.texts[r])})
        if counts:
            shared_any += 1
        max_containment.append(best)
    mc = sorted(max_containment)
    stats = {
        "items": len(items),
        "units": sum(len(_requests(i)) for i in items),
        "texts_checked": len(v2_texts),
        "texts_with_any_shared_trigram": shared_any,
        "max_containment_p50": mc[len(mc) // 2] if mc else None,
        "max_containment_p95": mc[min(len(mc) - 1, int(len(mc) * 0.95))] if mc else None,
        "max_containment_max": mc[-1] if mc else None,
        "trigrams_total": total,
        "trigrams_also_in_reference": total - novel,
        "trigram_overlap_fraction": round((total - novel) / total, 4) if total else None,
        "approved_domains_shared_by_design": sorted(domain_overlap_approved),
    }
    by_type: dict[str, int] = defaultdict(int)
    for f in findings:
        by_type[f["type"]] += 1
    return {"checked": label, "passed": not findings, "findings_by_type": dict(by_type),
            "findings": findings, "stats": stats}


def locked_split_guard(benchmarks_root: Path) -> list[dict]:
    """Locked items must never live in the engineering repository: only LOCK.json and
    aggregate reports may be committed (protocol section 6)."""
    problems = []
    for path in sorted(benchmarks_root.rglob("*.jsonl")):
        for n, row in enumerate(read_jsonl(path)):
            if isinstance(row, dict) and row.get("split") == "locked":
                problems.append({"type": "LOCKED_ITEM_IN_REPO", "file": str(path),
                                 "row": n, "id": row.get("id")})
    gi = benchmarks_root / "agentpal-bench-v2" / ".gitignore"
    if not gi.exists() or "locked/" not in gi.read_text(encoding="utf-8"):
        problems.append({"type": "GITIGNORE_MISSING_LOCKED_RULE", "file": str(gi)})
    return problems


def run(bench_dirs: list[Path], project_root: Path = PACKAGE_ROOT) -> dict:
    ref = build_reference(project_root)
    approved = approved_domains(project_root)
    report = {
        "method": {"normalization": "NFKC, format chars removed, casefold, non-alnum -> space",
                   "ngram": NGRAM, "min_shared": MIN_SHARED,
                   "containment_threshold": CONTAINMENT_THRESHOLD,
                   "fields": ["purpose", "business_justification", "evidence.description"]},
        "reference_sources": ref.sources,
        "reference_distinct_texts": len(ref.texts),
        "results": [check(load_items(d), ref, approved, label=str(d.name))
                    for d in bench_dirs],
        "locked_split_guard": locked_split_guard(project_root / "benchmarks"),
    }
    # v1 is reported against the generator/data only (it cannot be checked against itself)
    v1 = project_root / "benchmarks" / "agentpal-bench-v1"
    if v1.exists():
        ref_no_v1 = build_reference(project_root, include_v1=False)
        report["v1_seed_for_information"] = check(load_items(v1), ref_no_v1, approved,
                                                  label="agentpal-bench-v1 (informational)")
    report["passed"] = (all(r["passed"] for r in report["results"])
                        and not report["locked_split_guard"])
    return report


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="AgentPal-Bench v2 leakage check")
    p.add_argument("--bench-dir", action="append", default=None)
    p.add_argument("--report", default=None)
    args = p.parse_args(argv)
    dirs = [Path(d) for d in (args.bench_dir or [str(HERE)])]
    report = run(dirs)
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                                     encoding="utf-8")
    for r in report["results"] + ([report["v1_seed_for_information"]]
                                  if "v1_seed_for_information" in report else []):
        s = r["stats"]
        print(f"[{'PASS' if r['passed'] else 'FAIL'}] {r['checked']}: units={s['units']} "
              f"texts={s['texts_checked']} findings={r['findings_by_type'] or 0} "
              f"trigram_overlap={s['trigram_overlap_fraction']} "
              f"max_containment p50/p95/max={s['max_containment_p50']}/"
              f"{s['max_containment_p95']}/{s['max_containment_max']}")
    print(f"locked-split guard problems: {len(report['locked_split_guard'])}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
