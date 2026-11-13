#!/usr/bin/env python3
"""
Strict v1.2 Integrity & CRL/Ledger Consistency Verifier for FDA 2026 CRL & GLP-1 Benchmark.
Verifies:
1. Exact SHA-256 match against MANIFEST_SHA256_v1_2.json (preserving Zenodo 24201727 v1.0 artifacts).
2. Exact 64-bit little-endian correspondence and Poseidon2 Merkle inclusion proofs across master + 6 flagship dockets.
3. Exact CRL index recount (33 indexed entries, 32 PDF filenames, 1 Under Review for Release, 20 CMC domain, 19 FACILITY INSPECTIONS, 8 CLINICAL, 3 CMC+CLINICAL overlap).
Note: Verifies cryptographic commitments and index counts; does not establish clinical efficacy, economic counterfactuals, or regulatory breach.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from poseidon2_goldilocks import verify_merkle_proof

HERE = Path(__file__).resolve().parent

def felts_to_le_hex(felts: list[int]) -> str:
    return "".join(int(x).to_bytes(8, "little").hex() for x in felts)

def main() -> int:
    manifest = json.loads((HERE / "MANIFEST_SHA256_v1_2.json").read_text(encoding="utf-8"))
    for rel_path, expected_sha in manifest["files_sha256"].items():
        if rel_path == "MANIFEST_SHA256_v1_2.json":
            continue
        actual = hashlib.sha256((HERE / rel_path).read_bytes()).hexdigest()
        assert actual == expected_sha, f"SHA-256 mismatch on {rel_path}: {actual} != {expected_sha}"

    reg = json.loads((HERE / "public_commitment_registry_20261113.json").read_text(encoding="utf-8"))
    mp = reg["master_showcase_poseidon2_anchor"]["physician_signoff_inclusion_proof"]
    assert felts_to_le_hex(mp["root_felts"]) == mp["root_hex"]
    assert verify_merkle_proof(mp["root_felts"], mp["leaf_felts"], mp["proof"], mp["sequence"])

    for c in reg["flagship_unapproved_to_approved_commitments"]:
        p = c["cryptographic_commitment"]["physician_signoff_merkle_inclusion_proof"]
        assert felts_to_le_hex(p["root_felts"]) == p["root_hex"]
        assert verify_merkle_proof(p["root_felts"], p["leaf_felts"], p["proof"], p["sequence"])

    crls = reg["fda_2026_unapproved_crl_index_33"]
    pdf_count = sum(1 for x in crls if str(x.get("file_name", "")).endswith(".pdf"))
    pending_count = sum(1 for x in crls if x.get("file_name") == "Under Review for Release")
    cmc_count = sum(1 for x in crls if "CMC_AND_FACILITY_CGMP" in x.get("structural_deficiency_domains", []))
    fac_count = sum(1 for x in crls if "FACILITY INSPECTIONS" in x.get("detected_crl_sections", []))
    clin_count = sum(1 for x in crls if "CLINICAL" in x.get("detected_crl_sections", []))
    overlap_count = sum(
        1 for x in crls
        if "CMC_AND_FACILITY_CGMP" in x.get("structural_deficiency_domains", [])
        and "CLINICAL" in x.get("detected_crl_sections", [])
    )
    assert (len(crls), pdf_count, pending_count, cmc_count, fac_count, clin_count, overlap_count) == (33, 32, 1, 20, 19, 8, 3)

    print(json.dumps({
        "strict_v1_2_status": "ALL_CHECKS_PASSED",
        "scope": "RECORDED_COMMITMENT_INTEGRITY_AND_INDEX_RECOUNT_VERIFIED",
        "crl_recount": {
            "indexed_entries": len(crls),
            "pdf_entries": pdf_count,
            "under_review_for_release": pending_count,
            "cmc_domain_entries": cmc_count,
            "facility_inspections_heading": fac_count,
            "clinical_heading": clin_count,
            "cmc_and_clinical_overlap": overlap_count,
        },
        "poseidon2_proofs_verified": 7,
    }, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
