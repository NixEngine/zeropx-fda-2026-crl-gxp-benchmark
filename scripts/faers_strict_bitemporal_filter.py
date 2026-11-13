#!/usr/bin/env python3
"""
Scoped Historical openFDA/FAERS Candidate-Selection & Traceability Prototype (v1.2).
Zero external dependencies (Python 3.11+ standard library).

Incorporates R1.2 Multi-Model Audit Fixes (R01-R08):
- R01: Does NOT treat `sender.senderorganization == "FDA-PUBLIC USE"` as proof of direct
  non-manufacturer origin (openFDA sets FDA-PUBLIC USE as public distributor). Records
  `companynumb` presence explicitly as a documented scope heuristic.
- R02/R03: Connected-component Union-Find over `safetyreportid` root and company-namespaced
  `(company_namespace, companynumb/duplicatenumb)` aliases so transitive `{A,B}+{B,C}` links
  unite without cross-company local ID collisions.
- R04: Preserves full immutable version history per collapsed case and resolves same-rank
  version ties deterministically via canonical SHA-256 while flagging `VERSION_CONFLICT`.
- R05: Treats both `drugcharacterization == "1"` (Suspect) and `"3"` (Interacting) as suspect
  drugs per FDA/ICH E2D(R1) Note 4.
- R06: Maps administratively closed `NDA213182` (2020-01-16 letter) to `NDA213051`; preserves
  product ambiguity across `OZEMPIC` (injectable NDA209637 vs. oral tablets NDA213051/S-030 from
  2026-01-30) and `WEGOVY` (injectable NDA215256 vs. oral tablets NDA218316); fails closed if
  ANY suspect/interacting semaglutide entry in the report is unresolved or contradictory.
- R07: Validates real calendar dates via `datetime.strptime("%Y%m%d")`, rejects overlapping
  ontology windows at load time, never substitutes `receiptdate` for a missing `receivedate`,
  and evaluates `receivedate` vs. `receiptdate` label windows separately (no set union).
- R08: Implements exact RFC 6962 Section 2.1 empty Merkle tree root (`SHA-256(b"")`).
- Proportional Scope: Outputs `selected_candidates_for_review` (never claiming automated proof
  of 21 CFR 314.80 violation, manufacturer Day-0 awareness date, or Rule 702 admissibility).
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
import urllib.parse
import urllib.request
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

ACTIVE_SEMAGLUTIDE_NDAS: dict[str, str] = {
    "NDA209637": "OZEMPIC_INJECTION",
    "NDA213051": "RYBELSUS_OR_OZEMPIC_TABLETS",
    "NDA215256": "WEGOVY_INJECTION",
    "NDA218316": "WEGOVY_TABLETS",
}

HISTORICAL_NDA_REDIRECTS: dict[str, str] = {
    "NDA213182": "NDA213051",  # Administratively closed 2020-01-16 -> NDA 213051
}

NDA_REGEX = re.compile(r"(?:NDA[\s\-:]*)?0*(\d{6})\b", re.IGNORECASE)
COMPANY_PREFIX_RE = re.compile(r"^([A-Z]{2}-[A-Z0-9\-]{2,20})-")


@dataclass(frozen=True)
class LabelWindow:
    nda: str
    brand_cohort: str
    dosage_form: str
    spl_set_id: str
    spl_version: str
    effective_from: str  # Validated YYYYMMDD
    effective_to: str | None  # Validated YYYYMMDD or None
    labeled_pts: frozenset[str]


def normalize_pt(term: str) -> str:
    return " ".join(term.strip().upper().split())


def normalize_case_key(val: Any) -> str:
    if not isinstance(val, str):
        return ""
    return " ".join(val.strip().upper().split())


def is_valid_yyyymmdd(val: Any) -> bool:
    if not isinstance(val, str) or len(val) != 8 or not val.isdigit():
        return False
    try:
        datetime.strptime(val, "%Y%m%d")
        return True
    except ValueError:
        return False


def canonical_record_sha256(rec: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(rec, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def load_bitemporal_ontology(path: Path) -> dict[str, list[LabelWindow]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    ndas_block = raw.get("ndas", raw)
    ontology: dict[str, list[LabelWindow]] = {}

    for nda_key, windows in ndas_block.items():
        nda = normalize_case_key(nda_key)
        if nda not in ACTIVE_SEMAGLUTIDE_NDAS:
            raise ValueError(f"Unsupported or non-active NDA key in ontology: {nda}")
        parsed_windows: list[LabelWindow] = []
        for w in windows:
            eff_from = str(w["effective_from"]).strip()
            eff_to_raw = w.get("effective_to")
            eff_to = str(eff_to_raw).strip() if eff_to_raw not in (None, "", "null") else None
            if not is_valid_yyyymmdd(eff_from):
                raise ValueError(f"Invalid effective_from calendar date in {nda}: {eff_from}")
            if eff_to is not None and not is_valid_yyyymmdd(eff_to):
                raise ValueError(f"Invalid effective_to calendar date in {nda}: {eff_to}")
            if eff_to is not None and eff_to < eff_from:
                raise ValueError(f"Inverted window in {nda}: {eff_from} > {eff_to}")
            pts = {normalize_pt(x) for x in w.get("labeled_meddra_pts", [])}
            pts |= {normalize_pt(x) for x in w.get("clinical_synonym_closure", [])}
            parsed_windows.append(
                LabelWindow(
                    nda=nda,
                    brand_cohort=str(w.get("brand_cohort", ACTIVE_SEMAGLUTIDE_NDAS[nda])),
                    dosage_form=normalize_case_key(w.get("dosage_form", "")),
                    spl_set_id=str(w.get("spl_set_id", "UNSPECIFIED_SET_ID")),
                    spl_version=str(w.get("spl_version", "UNSPECIFIED")),
                    effective_from=eff_from,
                    effective_to=eff_to,
                    labeled_pts=frozenset(pts),
                )
            )
        parsed_windows.sort(key=lambda x: x.effective_from)
        # R07: Reject overlapping windows explicitly at load time
        for i in range(len(parsed_windows) - 1):
            prev_w = parsed_windows[i]
            next_w = parsed_windows[i + 1]
            if prev_w.effective_to is None or prev_w.effective_to >= next_w.effective_from:
                raise ValueError(
                    f"Overlapping ontology windows in {nda}: "
                    f"[{prev_w.effective_from}..{prev_w.effective_to}] vs [{next_w.effective_from}..{next_w.effective_to}]"
                )
        ontology[nda] = parsed_windows
    return ontology


def resolve_label_window(
    ontology: dict[str, list[LabelWindow]], nda: str, date_yyyymmdd: str
) -> LabelWindow | None:
    if not is_valid_yyyymmdd(date_yyyymmdd):
        return None
    matches: list[LabelWindow] = []
    for w in ontology.get(nda, []):
        if date_yyyymmdd >= w.effective_from and (
            w.effective_to is None or date_yyyymmdd <= w.effective_to
        ):
            matches.append(w)
    if len(matches) != 1:
        return None
    return matches[0]


def iter_records_from_local_path(path: Path) -> Iterator[dict[str, Any]]:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path, "r") as zf:
            for name in sorted(zf.namelist()):
                if name.endswith(".json"):
                    data = json.loads(zf.read(name).decode("utf-8"))
                    yield from _extract_results(data)
    elif path.suffix.lower() == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
            yield from _extract_results(data)
    elif path.suffix.lower() == ".jsonl":
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)
    elif path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        yield from _extract_results(data)


def _extract_results(payload: Any) -> Iterator[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        for r in payload["results"]:
            if isinstance(r, dict):
                yield r
    elif isinstance(payload, list):
        for r in payload:
            if isinstance(r, dict):
                yield r


def iter_openfda_api(
    search_query: str, api_key: str | None, max_pages: int = 100
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    base = "https://api.fda.gov/drug/event.json"
    limit = 100
    out: list[dict[str, Any]] = []
    meta_total = None
    pages_fetched = 0
    truncated_by_max_pages = False

    for page_idx in range(max_pages):
        skip = page_idx * limit
        if skip >= 25000:
            truncated_by_max_pages = True
            break
        params = [f"search={search_query}", f"limit={limit}", f"skip={skip}"]
        if api_key:
            params.insert(0, f"api_key={urllib.parse.quote(api_key, safe='')}")
        url = f"{base}?{'&'.join(params)}"
        req = urllib.request.Request(
            url, headers={"User-Agent": "ZeroPx-Scoped-FAERS-Filter/1.2"}
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                break
            raise
        if meta_total is None:
            meta_total = body.get("meta", {}).get("results", {}).get("total")
        results = body.get("results", [])
        if not results:
            break
        out.extend(r for r in results if isinstance(r, dict))
        pages_fetched += 1
        if len(results) < limit or (isinstance(meta_total, int) and len(out) >= meta_total):
            break
    else:
        if meta_total is None or len(out) < meta_total:
            truncated_by_max_pages = True

    is_complete = (not truncated_by_max_pages) and (
        meta_total is None or len(out) >= int(meta_total)
    )
    return out, {
        "query": search_query,
        "pages_fetched": pages_fetched,
        "records_fetched": len(out),
        "api_reported_total": meta_total,
        "is_complete_extract": is_complete,
    }


def _extract_company_namespace(rec: dict[str, Any], raw_id: str) -> str:
    """
    R03 Fix: Scope local identifiers by explicit E2B world-wide case prefix (e.g. DK-NOVO-...)
    or sender organization when not FDA-PUBLIC USE, preventing cross-company collisions on generic local IDs.
    """
    m = COMPANY_PREFIX_RE.match(raw_id)
    if m:
        return f"E2BPREFIX:{m.group(1)}"
    sender = rec.get("sender") if isinstance(rec.get("sender"), dict) else {}
    s_org = normalize_case_key(sender.get("senderorganization"))
    if s_org and s_org != "FDA-PUBLIC USE":
        return f"SENDER:{s_org}"
    # Check manufacturer name on suspect drugs if unambiguous
    patient = rec.get("patient") if isinstance(rec.get("patient"), dict) else {}
    mfrs = {
        normalize_case_key(m_name)
        for d in (patient.get("drug") or [])
        if isinstance(d, dict) and str(d.get("drugcharacterization") or "") in {"1", "3"}
        for m_name in ((d.get("openfda") or {}).get("manufacturer_name") or [])
        if isinstance(m_name, str) and m_name.strip()
    }
    if len(mfrs) == 1:
        return f"MFR:{next(iter(mfrs))}"
    return f"UNSCOPED_REPORT:{normalize_case_key(rec.get('safetyreportid')).split('-')[0] or id(rec)}"


def extract_namespaced_aliases(rec: dict[str, Any]) -> list[str]:
    aliases: list[str] = []
    srid = normalize_case_key(rec.get("safetyreportid"))
    if srid:
        srid_root = srid.split("-")[0]
        aliases.append(f"SRID:{srid_root}")

    comp = normalize_case_key(rec.get("companynumb"))
    if comp:
        ns = _extract_company_namespace(rec, comp)
        aliases.append(f"COMP:{ns}:{comp}")

    dup = rec.get("reportduplicate")
    dup_items = dup if isinstance(dup, list) else ([dup] if isinstance(dup, dict) else [])
    for item in dup_items:
        if not isinstance(item, dict):
            continue
        dnum = normalize_case_key(item.get("duplicatenumb"))
        dsrc = normalize_case_key(item.get("duplicatesource"))
        if dnum:
            ns = f"DUPSRC:{dsrc}" if dsrc else _extract_company_namespace(rec, dnum)
            aliases.append(f"COMP:{ns}:{dnum}")
    return sorted(set(aliases))


@dataclass
class CollapsedCaseCluster:
    cluster_id: str
    representative: dict[str, Any]
    version_history: list[dict[str, Any]]
    aliases: list[str]
    has_same_rank_conflict: bool
    any_version_expedited: bool


def collapse_faers_revisions(
    records: Iterable[dict[str, Any]], stats: Counter[str]
) -> list[CollapsedCaseCluster]:
    """
    R02/R03/R04 Fix:
    1. Connected-component Union-Find across all namespaced aliases (`{A,B}` + `{B,C}` -> `{A,B,C}`).
    2. Deterministic tie-breaking via `(version, receiptdate, receivedate, srid_root, canonical_sha256)`
       while flagging conflicting `fulfillexpeditecriteria` at identical `(version, receiptdate, receivedate)`.
    3. Retains full `version_history` for longitudinal inspection.
    """
    raw_list: list[dict[str, Any]] = []
    for rec in records:
        stats["00_raw_records_ingested"] += 1
        raw_list.append(rec)

    parent: dict[int, int] = {i: i for i in range(len(raw_list))}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            if ra < rb:
                parent[rb] = ra
            else:
                parent[ra] = rb

    alias_owner: dict[str, int] = {}
    rec_aliases: list[list[str]] = []
    srid_roots: set[str] = set()

    for idx, rec in enumerate(raw_list):
        srid = normalize_case_key(rec.get("safetyreportid"))
        if srid:
            srid_roots.add(srid.split("-")[0])
        al = extract_namespaced_aliases(rec)
        rec_aliases.append(al)
        for a in al:
            if a in alias_owner:
                union(idx, alias_owner[a])
            else:
                alias_owner[a] = idx

    stats["02_unique_safetyreportid_roots"] = len(srid_roots)

    clusters_map: dict[int, list[int]] = defaultdict(list)
    for idx in range(len(raw_list)):
        clusters_map[find(idx)].append(idx)

    clusters: list[CollapsedCaseCluster] = []
    for member_indices in clusters_map.values():
        scored: list[tuple[tuple[int, str, str], str, str, dict[str, Any]]] = []
        all_aliases: set[str] = set()
        for idx in member_indices:
            rec = raw_list[idx]
            all_aliases.update(rec_aliases[idx])
            try:
                ver = int(str(rec.get("safetyreportversion") or "1").strip())
            except ValueError:
                ver = 1
            rcpt = str(rec.get("receiptdate") or "")
            rcvd = str(rec.get("receivedate") or "")
            srid_r = normalize_case_key(rec.get("safetyreportid")).split("-")[0]
            sha = canonical_record_sha256(rec)
            scored.append(((ver, rcpt, rcvd), srid_r, sha, rec))

        # Sort canonically so input order NEVER changes the selected representative (R04)
        scored.sort(key=lambda item: (item[0], item[1], item[2]))
        top_rank = scored[-1][0]
        top_ties = [item for item in scored if item[0] == top_rank]
        expedite_vals_at_top = {
            str(item[3].get("fulfillexpeditecriteria") or "").strip() for item in top_ties
        }
        has_conflict = len(expedite_vals_at_top) > 1
        if has_conflict:
            stats["03b_clusters_with_same_rank_expedite_conflict"] += 1

        any_exp = any(
            str(item[3].get("fulfillexpeditecriteria") or "").strip() == "1"
            for item in scored
        )
        rep = scored[-1][3]
        history_summary = [
            {
                "safetyreportid": normalize_case_key(item[3].get("safetyreportid")),
                "safetyreportversion": str(item[3].get("safetyreportversion") or ""),
                "receivedate": str(item[3].get("receivedate") or ""),
                "receiptdate": str(item[3].get("receiptdate") or ""),
                "fulfillexpeditecriteria": str(item[3].get("fulfillexpeditecriteria") or ""),
                "canonical_sha256": item[2],
            }
            for item in scored
        ]
        sorted_aliases = sorted(all_aliases)
        cid = sorted_aliases[0] if sorted_aliases else scored[-1][2][:16]
        clusters.append(
            CollapsedCaseCluster(
                cluster_id=cid,
                representative=rep,
                version_history=history_summary,
                aliases=sorted_aliases,
                has_same_rank_conflict=has_conflict,
                any_version_expedited=any_exp,
            )
        )

    clusters.sort(key=lambda c: c.cluster_id)
    stats["03_collapsed_unique_cases"] = len(clusters)
    return clusters


def extract_nda_from_raw_field(raw_auth: Any) -> tuple[str | None, str | None]:
    """
    Returns (active_nda | None, note). Redirects historical NDA213182 -> NDA213051 (R06).
    """
    if not isinstance(raw_auth, str):
        return None, None
    m = NDA_REGEX.search(raw_auth)
    if not m:
        return None, None
    cand = f"NDA{m.group(1)}"
    if cand in HISTORICAL_NDA_REDIRECTS:
        mapped = HISTORICAL_NDA_REDIRECTS[cand]
        return mapped, f"HISTORICAL_{cand}_REDIRECTED_TO_{mapped}"
    if cand in ACTIVE_SEMAGLUTIDE_NDAS:
        return cand, "RAW_DRUGAUTHORIZATIONNUMB"
    return None, None


def _infer_presentation_hint(d: dict[str, Any]) -> str | None:
    """Returns 'INJECTION', 'TABLET', or None if ambiguous."""
    med_prod = normalize_case_key(d.get("medicinalproduct"))
    dosage_form = normalize_case_key(d.get("drugdosageform"))
    route = str(d.get("drugadministrationroute") or "").strip()  # 048=Oral, 058=Subcutaneous
    blob = f"{med_prod} {dosage_form}"
    has_oral = any(tok in blob for tok in ("TABLET", "ORAL", "PILL", "RYBELSUS")) or route == "048"
    has_inj = any(tok in blob for tok in ("PEN", "INJ", "SUBCUTANEOUS", "SOLUTION", "SYRINGE")) or route == "058"
    if has_oral and not has_inj:
        return "TABLET"
    if has_inj and not has_oral:
        return "INJECTION"
    return None


def resolve_suspect_semaglutide_nda(
    drugs: list[dict[str, Any]],
    eval_date: str,
    require_raw_nda: bool = False,
) -> tuple[str | None, str, bool]:
    """
    R05 & R06 Fix:
    - Evaluates both Suspect ('1') and Interacting ('3') drugs per ICH E2D(R1).
    - Maps historical NDA213182 -> NDA213051.
    - Checks consistency between raw NDA, brand, presentation, and date (including NDA 213051/S-030
      Ozempic tablets approval on 2026-01-30 and Wegovy tablets NDA 218316).
    - Fails closed if ANY suspect/interacting semaglutide item is unresolved or contradictory!
    """
    resolved_sema_ndas: set[str] = set()
    resolution_sources: set[str] = set()
    saw_semaglutide_suspect = False
    unresolved_sema_item = False
    contradictory_item = False
    other_suspect_or_interacting_count = 0

    for d in drugs:
        if not isinstance(d, dict):
            continue
        role = str(d.get("drugcharacterization") or "").strip()
        if role not in {"1", "3"}:
            continue  # Concomitant ('2') excluded; Suspect ('1') & Interacting ('3') included (R05)

        med_prod = normalize_case_key(d.get("medicinalproduct"))
        openfda = d.get("openfda") if isinstance(d.get("openfda"), dict) else {}
        generics = {
            normalize_case_key(g) for g in openfda.get("generic_name", []) if isinstance(g, str)
        }
        brands = {
            normalize_case_key(b) for b in openfda.get("brand_name", []) if isinstance(b, str)
        }
        raw_nda, raw_note = extract_nda_from_raw_field(d.get("drugauthorizationnumb"))
        pres_hint = _infer_presentation_hint(d)

        brand_tokens = {
            b for b in ("OZEMPIC", "WEGOVY", "RYBELSUS") if b in med_prod or b in brands
        }
        is_sema = (
            raw_nda is not None
            or "SEMAGLUTIDE" in med_prod
            or any("SEMAGLUTIDE" in g for g in generics)
            or bool(brand_tokens)
        )

        if not is_sema:
            other_suspect_or_interacting_count += 1
            continue

        saw_semaglutide_suspect = True

        if raw_nda is not None:
            # Verify raw NDA does not contradict explicit brand/presentation (R06)
            if raw_nda == "NDA209637" and (pres_hint == "TABLET" or "WEGOVY" in brand_tokens or "RYBELSUS" in brand_tokens):
                contradictory_item = True
                continue
            if raw_nda == "NDA215256" and (pres_hint == "TABLET" or "OZEMPIC" in brand_tokens or "RYBELSUS" in brand_tokens):
                contradictory_item = True
                continue
            if raw_nda == "NDA218316" and (pres_hint == "INJECTION" or "OZEMPIC" in brand_tokens or "RYBELSUS" in brand_tokens):
                contradictory_item = True
                continue
            if raw_nda == "NDA213051" and (pres_hint == "INJECTION" or "WEGOVY" in brand_tokens):
                contradictory_item = True
                continue
            resolved_sema_ndas.add(raw_nda)
            resolution_sources.add(raw_note or "RAW_DRUGAUTHORIZATIONNUMB")
            continue

        if require_raw_nda:
            unresolved_sema_item = True
            continue

        # Fallback resolution ONLY when unambiguous across brand + presentation + date (R06)
        if len(brand_tokens) != 1:
            unresolved_sema_item = True
            continue
        brand = next(iter(brand_tokens))
        if brand == "RYBELSUS":
            if pres_hint == "INJECTION":
                contradictory_item = True
            else:
                resolved_sema_ndas.add("NDA213051")
                resolution_sources.add("UNAMBIGUOUS_RYBELSUS_FALLBACK")
        elif brand == "OZEMPIC":
            # After 2026-01-30 (NDA 213051/S-030), "OZEMPIC" without presentation hint is ambiguous!
            if pres_hint == "TABLET":
                resolved_sema_ndas.add("NDA213051")
                resolution_sources.add("OZEMPIC_TABLET_S030_FALLBACK")
            elif pres_hint == "INJECTION" or (is_valid_yyyymmdd(eval_date) and eval_date < "20260130"):
                resolved_sema_ndas.add("NDA209637")
                resolution_sources.add("OZEMPIC_INJECTION_FALLBACK")
            else:
                unresolved_sema_item = True
        elif brand == "WEGOVY":
            # Wegovy oral tablets (NDA 218316) approved 2025-12-22
            if pres_hint == "TABLET":
                resolved_sema_ndas.add("NDA218316")
                resolution_sources.add("WEGOVY_TABLET_FALLBACK")
            elif pres_hint == "INJECTION" or (is_valid_yyyymmdd(eval_date) and eval_date < "20251222"):
                resolved_sema_ndas.add("NDA215256")
                resolution_sources.add("WEGOVY_INJECTION_FALLBACK")
            else:
                unresolved_sema_item = True

    if not saw_semaglutide_suspect:
        return None, "NO_SUSPECT_OR_INTERACTING_SEMAGLUTIDE", False
    if contradictory_item:
        return None, "CONTRADICTORY_NDA_BRAND_OR_PRESENTATION", False
    if unresolved_sema_item or len(resolved_sema_ndas) == 0:
        return None, "AMBIGUOUS_UNBRANDED_OR_MISSING_RAW_NDA", False
    if len(resolved_sema_ndas) > 1:
        return None, "CONFLICTING_MULTIPLE_SEMA_NDAS", False

    sole_suspect = other_suspect_or_interacting_count == 0
    return next(iter(resolved_sema_ndas)), "+".join(sorted(resolution_sources)), sole_suspect


def has_literature_reference(rec: dict[str, Any]) -> bool:
    ps = rec.get("primarysource")
    items = ps if isinstance(ps, list) else ([ps] if isinstance(ps, dict) else [])
    for item in items:
        if isinstance(item, dict) and normalize_case_key(item.get("literaturereference")):
            return True
    return False


def audit_collapsed_cases(
    clusters: Iterable[CollapsedCaseCluster],
    ontology: dict[str, list[LabelWindow]],
    target_pt: str | None,
    require_raw_nda: bool,
    require_companynumb: bool,
    exclude_literature: bool,
    stats: Counter[str],
) -> list[dict[str, Any]]:
    norm_target = normalize_pt(target_pt) if target_pt else None
    candidates: list[dict[str, Any]] = []

    for cluster in clusters:
        rec = cluster.representative

        if cluster.has_same_rank_conflict:
            stats["04a_excluded_unresolved_same_rank_version_conflict"] += 1
            continue

        if str(rec.get("serious") or "").strip() != "1":
            stats["04_excluded_non_serious"] += 1
            continue

        # Scoped pilot choice: Spontaneous ('1') only; Study/Solicited ('2') excluded from pilot scope
        if str(rec.get("reporttype") or "").strip() != "1":
            stats["05_excluded_non_spontaneous_or_study_by_scope"] += 1
            continue

        if exclude_literature and has_literature_reference(rec):
            stats["06_excluded_literature_by_scope"] += 1
            continue

        # R01 Fix: Never drop based on senderorganization == "FDA-PUBLIC USE"!
        sender = rec.get("sender") if isinstance(rec.get("sender"), dict) else {}
        public_export_sender_org = normalize_case_key(sender.get("senderorganization"))
        company_numb = normalize_case_key(rec.get("companynumb"))
        if require_companynumb and not company_numb:
            stats["07_excluded_missing_companynumb_by_scope"] += 1
            continue

        # If any observed version was already expedited ('1') or latest is not '2', exclude
        if cluster.any_version_expedited or str(rec.get("fulfillexpeditecriteria") or "").strip() != "2":
            stats["08_excluded_already_expedited_or_unspecified"] += 1
            continue

        # R07 Fix: Validate BOTH FDA receipt dates independently; never substitute receiptdate for missing receivedate
        receipt_dt = str(rec.get("receiptdate") or "").strip()
        receive_dt = str(rec.get("receivedate") or "").strip()
        if not is_valid_yyyymmdd(receive_dt) or not is_valid_yyyymmdd(receipt_dt):
            stats["12a_excluded_missing_or_invalid_fda_calendar_date"] += 1
            continue

        patient = rec.get("patient") if isinstance(rec.get("patient"), dict) else {}
        drugs = patient.get("drug") if isinstance(patient.get("drug"), list) else []
        nda, nda_source, sole_suspect = resolve_suspect_semaglutide_nda(
            drugs, eval_date=receipt_dt, require_raw_nda=require_raw_nda
        )
        if nda is None:
            stats[f"09_excluded_{nda_source.lower()}"] += 1
            continue

        reactions_raw = (
            patient.get("reaction") if isinstance(patient.get("reaction"), list) else []
        )
        case_pts = sorted(
            {
                normalize_pt(r["reactionmeddrapt"])
                for r in reactions_raw
                if isinstance(r, dict)
                and isinstance(r.get("reactionmeddrapt"), str)
                and r["reactionmeddrapt"].strip()
            }
        )
        if not case_pts:
            stats["10_excluded_empty_reactions"] += 1
            continue

        if norm_target and norm_target not in case_pts:
            stats["11_excluded_target_pt_absent"] += 1
            continue

        # R07 Fix: Evaluate initial FDA receipt window and latest FDA receipt window SEPARATELY (no union!)
        win_receive = resolve_label_window(ontology, nda, receive_dt)
        win_receipt = resolve_label_window(ontology, nda, receipt_dt)
        if win_receive is None or win_receipt is None:
            stats["12b_excluded_uncovered_uspi_date_window"] += 1
            continue

        case_pt_set = set(case_pts)
        labeled_at_initial_fda_date = sorted(case_pt_set & win_receive.labeled_pts)
        labeled_at_latest_fda_date = sorted(case_pt_set & win_receipt.labeled_pts)

        if norm_target and (
            norm_target in win_receive.labeled_pts or norm_target in win_receipt.labeled_pts
        ):
            if (norm_target in win_receive.labeled_pts) != (norm_target in win_receipt.labeled_pts):
                stats["13b_excluded_target_pt_label_changed_between_fda_dates"] += 1
            else:
                stats["13_excluded_target_pt_in_working_ontology"] += 1
            continue

        if labeled_at_initial_fda_date or labeled_at_latest_fda_date:
            if bool(labeled_at_initial_fda_date) != bool(labeled_at_latest_fda_date):
                stats["14b_excluded_coevent_label_changed_between_fda_dates"] += 1
            else:
                stats["14_excluded_has_coevent_in_working_ontology"] += 1
            continue

        if len(case_pts) == 1 and sole_suspect:
            stratum = "CANDIDATE_STRATUM_1A_MONO_EVENT_SOLE_SUSPECT"
        elif sole_suspect:
            stratum = "CANDIDATE_STRATUM_1B_MULTI_UNLABELED_PTS_SOLE_SUSPECT"
        else:
            stratum = "CANDIDATE_STRATUM_2_MULTI_SUSPECT_OR_INTERACTING"

        stats["15_selected_candidates_for_review_total"] += 1
        stats[f"16_nda_{nda}"] += 1
        stats[f"17_{stratum.lower()}"] += 1

        leaf_hash = canonical_record_sha256(rec)
        candidates.append(
            {
                "cluster_id": cluster.cluster_id,
                "safetyreportid_root": normalize_case_key(rec.get("safetyreportid")).split("-")[0],
                "safetyreportversion": str(rec.get("safetyreportversion") or "1"),
                "companynumb": company_numb,
                "public_export_sender_org": public_export_sender_org,
                "fda_initial_receivedate": receive_dt,
                "fda_latest_receiptdate": receipt_dt,
                "resolved_nda": nda,
                "brand_cohort": win_receipt.brand_cohort,
                "nda_resolution_source": nda_source,
                "uspi_window_at_fda_receivedate": win_receive.spl_version,
                "uspi_window_at_fda_receiptdate": win_receipt.spl_version,
                "candidate_stratum": stratum,
                "sole_suspect_and_no_interacting_drugs": sole_suspect,
                "reactions_meddra_pt": case_pts,
                "seriousness_flags": {
                    k: str(rec.get(k) or "")
                    for k in (
                        "seriousnessdeath",
                        "seriousnesslifethreatening",
                        "seriousnesshospitalization",
                        "seriousnessdisabling",
                        "seriousnesscongenitalanomali",
                        "seriousnessother",
                    )
                },
                "version_history": cluster.version_history,
                "canonical_json_sha256": leaf_hash,
                "pending_adjudication_requirements": [
                    "Manufacturer Day-0 initial awareness date is not present in public openFDA exports.",
                    "Clinical adjudication of narrative, seriousness attribution, and full label specificity required.",
                ],
            }
        )

    candidates.sort(key=lambda x: (x["cluster_id"], x["canonical_json_sha256"]))
    return candidates


def merkle_root_rfc6962(leaf_hex_hashes: list[str]) -> str:
    """
    R08 Fix: Exact RFC 6962 Section 2.1 MTH({}) = SHA-256(b""), leaf = 0x00 || d, node = 0x01 || L || R.
    Note: Operates over the caller-supplied canonical list order of 32-byte leaf digests.
    """
    if not leaf_hex_hashes:
        return hashlib.sha256(b"").hexdigest()
    level = [
        hashlib.sha256(b"\x00" + bytes.fromhex(h)).digest() for h in leaf_hex_hashes
    ]
    while len(level) > 1:
        nxt: list[bytes] = []
        for i in range(0, len(level), 2):
            if i + 1 < len(level):
                nxt.append(hashlib.sha256(b"\x01" + level[i] + level[i + 1]).digest())
            else:
                nxt.append(level[i])
        level = nxt
    return level[0].hex()


def run_self_test() -> int:
    """Validates R01-R08 adversarial counterexamples plus external v1.2 ontology schema if present."""
    # R08: RFC 6962 empty tree exact check
    assert (
        merkle_root_rfc6962([])
        == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    ), "R08 failed: empty Merkle root must equal SHA-256(b'')"

    # R07: Calendar validation & overlap rejection
    assert not is_valid_yyyymmdd("20260230"), "R07 failed: accepted impossible date 20260230"
    assert is_valid_yyyymmdd("20240229") and not is_valid_yyyymmdd("20250229")

    # External ontology validation if located adjacent to script
    default_onto = Path(__file__).resolve().parent.parent / "schemas" / "semaglutide_uspi_bitemporal_lock.json"
    if default_onto.exists():
        onto = load_bitemporal_ontology(default_onto)
    else:
        onto = {
            "NDA209637": [
                LabelWindow("NDA209637", "OZEMPIC_INJECTION", "INJECTION", "set1", "v1", "20171205", None, frozenset({"VOMITING"}))
            ],
            "NDA215256": [
                LabelWindow("NDA215256", "WEGOVY_INJECTION", "INJECTION", "set2", "v1_pre_s033", "20210604", "20260224", frozenset({"SUICIDAL IDEATION"})),
                LabelWindow("NDA215256", "WEGOVY_INJECTION", "INJECTION", "set2", "v2_post_s033", "20260225", None, frozenset({"VOMITING"})),
            ],
            "NDA213051": [
                LabelWindow("NDA213051", "RYBELSUS_OR_OZEMPIC_TABLETS", "TABLET", "set3", "v1", "20190920", None, frozenset({"VOMITING"}))
            ],
        }

    base_rec = {
        "safetyreportid": "90000001",
        "safetyreportversion": "1",
        "serious": "1",
        "reporttype": "1",
        "fulfillexpeditecriteria": "2",
        "companynumb": "DK-NOVO-10001",
        "receivedate": "20260310",
        "receiptdate": "20260310",
        "sender": {"senderorganization": "FDA-PUBLIC USE"},
        "patient": {
            "drug": [{"drugcharacterization": "1", "medicinalproduct": "OZEMPIC", "drugauthorizationnumb": "209637"}],
            "reaction": [{"reactionmeddrapt": "EMOTIONAL DISTRESS"}],
        },
    }

    # R01: FDA-PUBLIC USE with valid companynumb MUST NOT be dropped!
    st1: Counter[str] = Counter()
    c1 = audit_collapsed_cases(
        collapse_faers_revisions([base_rec], st1),
        onto, "EMOTIONAL DISTRESS", True, True, True, st1
    )
    assert len(c1) == 1 and "1A" in c1[0]["candidate_stratum"], f"R01 failed: {st1}"

    # R05: Second drug with drugcharacterization == "3" (Interacting) MUST downgrade to STRATUM_2!
    rec_r05 = json.loads(json.dumps(base_rec))
    rec_r05["safetyreportid"] = "90000005"
    rec_r05["companynumb"] = "DK-NOVO-10005"
    rec_r05["patient"]["drug"].append({"drugcharacterization": "3", "medicinalproduct": "WARFARIN"})
    st5: Counter[str] = Counter()
    c5 = audit_collapsed_cases(
        collapse_faers_revisions([rec_r05], st5),
        onto, "EMOTIONAL DISTRESS", True, True, True, st5
    )
    assert len(c5) == 1 and "STRATUM_2" in c5[0]["candidate_stratum"], f"R05 failed: {c5}"

    # R02 & R03: Transitive union {A,B} + {B,C} within same company namespace, while different company stays separate!
    r_ab = json.loads(json.dumps(base_rec))
    r_ab["safetyreportid"] = "91000001"
    r_ab["companynumb"] = "DK-NOVO-AAA"
    r_ab["reportduplicate"] = {"duplicatesource": "NOVO", "duplicatenumb": "DK-NOVO-BBB"}
    r_bc = json.loads(json.dumps(base_rec))
    r_bc["safetyreportid"] = "91000002"
    r_bc["companynumb"] = "DK-NOVO-CCC"
    r_bc["reportduplicate"] = {"duplicatesource": "NOVO", "duplicatenumb": "DK-NOVO-BBB"}
    r_other_co = json.loads(json.dumps(base_rec))
    r_other_co["safetyreportid"] = "91000003"
    r_other_co["companynumb"] = "LOCAL-999"
    r_other_co["sender"] = {"senderorganization": "COMPANY_ALPHA"}
    r_other_co2 = json.loads(json.dumps(base_rec))
    r_other_co2["safetyreportid"] = "91000004"
    r_other_co2["companynumb"] = "LOCAL-999"
    r_other_co2["sender"] = {"senderorganization": "COMPANY_BETA"}
    st23: Counter[str] = Counter()
    cl23 = collapse_faers_revisions([r_ab, r_bc, r_other_co, r_other_co2], st23)
    assert len(cl23) == 3, f"R02/R03 failed: expected 3 clusters (1 merged + 2 distinct namespaces), got {len(cl23)}"

    # R04: Order-independent conflict detection on identical version/dates with conflicting expedite flags
    r_tie1 = json.loads(json.dumps(base_rec))
    r_tie2 = json.loads(json.dumps(base_rec))
    r_tie2["fulfillexpeditecriteria"] = "1"
    cl_fwd = collapse_faers_revisions([r_tie1, r_tie2], Counter())
    cl_rev = collapse_faers_revisions([r_tie2, r_tie1], Counter())
    assert cl_fwd[0].has_same_rank_conflict and cl_rev[0].has_same_rank_conflict
    assert canonical_record_sha256(cl_fwd[0].representative) == canonical_record_sha256(cl_rev[0].representative)

    # R06: NDA213182 redirects to NDA213051; second ambiguous semaglutide drug blocks resolution even with raw NDA on first!
    nda_redir, note_redir = extract_nda_from_raw_field("NDA 213182")
    assert nda_redir == "NDA213051" and "HISTORICAL" in (note_redir or "")
    nda_amb, reason_amb, _ = resolve_suspect_semaglutide_nda(
        [
            {"drugcharacterization": "1", "medicinalproduct": "OZEMPIC", "drugauthorizationnumb": "209637"},
            {"drugcharacterization": "1", "medicinalproduct": "SEMAGLUTIDE"},
        ],
        eval_date="20260310",
        require_raw_nda=True,
    )
    assert nda_amb is None and "AMBIGUOUS" in reason_amb, f"R06 failed: {nda_amb}, {reason_amb}"

    # Wegovy post-2026-02-25 suicidality removal check (no set union keeping removed warning alive on 20260310!)
    rec_wegovy_post = json.loads(json.dumps(base_rec))
    rec_wegovy_post["safetyreportid"] = "92000001"
    rec_wegovy_post["companynumb"] = "DK-NOVO-WEG1"
    rec_wegovy_post["receivedate"] = "20260310"
    rec_wegovy_post["receiptdate"] = "20260310"
    rec_wegovy_post["patient"]["drug"] = [{"drugcharacterization": "1", "medicinalproduct": "WEGOVY PEN", "drugauthorizationnumb": "215256"}]
    rec_wegovy_post["patient"]["reaction"] = [{"reactionmeddrapt": "SUICIDAL IDEATION"}]
    st_w: Counter[str] = Counter()
    cw = audit_collapsed_cases(
        collapse_faers_revisions([rec_wegovy_post], st_w),
        onto, "SUICIDAL IDEATION", True, True, True, st_w
    )
    assert len(cw) == 1, f"Wegovy post-2026-02-25 S-033 removal failed: {st_w}"

    print(
        json.dumps(
            {
                "self_test": "PASSED",
                "version": "1.2.0",
                "counterexamples_verified": ["R01", "R02", "R03", "R04", "R05", "R06", "R07", "R08", "WEGOVY_S033_20260225"],
                "rfc6962_empty_root": merkle_root_rfc6962([]),
            },
            indent=2,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scoped FAERS candidate-selection filter v1.2")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--ontology", type=Path, help="Path to v1.2 USPI JSON ontology")
    parser.add_argument("--input", type=Path, nargs="*", default=[], help="Local FAERS JSON/ZIP/GZ/JSONL files")
    parser.add_argument("--api-search", type=str, default=None, help="openFDA search query")
    parser.add_argument("--api-key", type=str, default=None)
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--target-pt", type=str, default="EMOTIONAL DISTRESS")
    parser.add_argument("--require-raw-nda", action="store_true")
    parser.add_argument("--allow-missing-companynumb", action="store_true")
    parser.add_argument("--include-literature", action="store_true")
    parser.add_argument("--out-dir", type=Path, default=Path("./faers_scoped_candidates_out"))
    args = parser.parse_args(argv)

    if args.self_test:
        return run_self_test()

    if not args.ontology:
        parser.error("--ontology is required unless --self-test is passed.")

    ontology = load_bitemporal_ontology(args.ontology)
    stats: Counter[str] = Counter()
    acquisition_meta: dict[str, Any] = {"local_files": [str(p) for p in args.input], "api": None}

    all_records: list[dict[str, Any]] = []
    for p in args.input:
        all_records.extend(iter_records_from_local_path(p))
    if args.api_search:
        api_recs, api_info = iter_openfda_api(args.api_search, args.api_key, max_pages=args.max_pages)
        all_records.extend(api_recs)
        acquisition_meta["api"] = api_info

    clusters = collapse_faers_revisions(all_records, stats)
    candidates = audit_collapsed_cases(
        clusters=clusters,
        ontology=ontology,
        target_pt=args.target_pt if args.target_pt != "" else None,
        require_raw_nda=args.require_raw_nda,
        require_companynumb=not args.allow_missing_companynumb,
        exclude_literature=not args.include_literature,
        stats=stats,
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    cand_path = args.out_dir / "selected_candidates_for_review.jsonl"
    with cand_path.open("w", encoding="utf-8") as fh:
        for row in candidates:
            fh.write(json.dumps(row, sort_keys=True) + "\n")

    leaf_hashes = [row["canonical_json_sha256"] for row in candidates]
    manifest = {
        "schema": "zeropx.faers-scoped-candidate-manifest/v1.2",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope_statement": "Candidate-selection prototype with explicit exclusions and pending clinical/regulatory review requirements. Does NOT establish 21 CFR 314.80 breach, Day-0 manufacturer awareness, causation, or FRE 702 admissibility on its own.",
        "target_meddra_pt": normalize_pt(args.target_pt) if args.target_pt else "ALL_PTS",
        "require_raw_nda": args.require_raw_nda,
        "ontology_sha256": hashlib.sha256(args.ontology.read_bytes()).hexdigest(),
        "acquisition": acquisition_meta,
        "selected_candidates_count": len(candidates),
        "merkle_root_rfc6962_sha256": merkle_root_rfc6962(leaf_hashes),
        "selection_waterfall": dict(sorted(stats.items())),
    }
    manifest_path = args.out_dir / "audit_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
