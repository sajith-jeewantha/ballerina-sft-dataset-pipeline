"""Licence gate, licence register and code fingerprints."""

from __future__ import annotations

from pathlib import Path

from balpipe_common.dedup import LSH, MinHasher, code_hash, similarity
from balpipe_common.licences import LicenceRegister, decide, detect_licence, normalize_spdx, pre_gate

APACHE = "                                 Apache License\n                           Version 2.0, January 2004\n"
MIT = "MIT License\n\nPermission is hereby granted, free of charge, to any person obtaining a copy"
GPL = "GNU GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007"


def test_detect_licence_texts() -> None:
    assert detect_licence(APACHE) == "Apache-2.0"
    assert detect_licence(MIT) == "MIT"
    assert detect_licence(GPL) == "GPL"
    assert detect_licence("Boost Software License - Version 1.0\n\nPermission is hereby granted, free of charge, "
                          "to any person or organization") == "BSL-1.0"
    assert detect_licence("Copyright me. All rights reserved.") is None
    assert normalize_spdx("Apache-2.0 (reported)") == "Apache-2.0" and normalize_spdx("NOASSERTION") is None


def test_gate_decisions() -> None:
    # agrees with GitHub
    assert decide("Apache-2.0", "Apache-2.0", None, licence_file=True)[0] == "safe"
    # GitHub couldn't classify, but the file is clearly MIT
    v, note, spdx = decide(None, "MIT", None, licence_file=True)
    assert (v, spdx) == ("safe", "MIT") and "detected" in note
    # mismatch, missing file, unknown text: a person decides
    assert decide("MIT", "Apache-2.0", None, licence_file=True)[0] == "check"
    assert decide("Apache-2.0", None, None, licence_file=False)[0] == "check"
    assert decide(None, None, None, licence_file=True)[0] == "check"
    # copyleft text, or no licence at all
    assert decide("MIT", "GPL", None, licence_file=True)[0] == "exclude"
    assert decide(None, None, None, licence_file=False, has_licence_key=False)[0] == "exclude"
    # people win: exclude sticks, `verified:` keeps a manual safe
    assert decide("MIT", "MIT", {"verdict": "exclude", "notes": "vendor code"}, licence_file=True)[0] == "exclude"
    row = {"verdict": "safe", "notes": "verified: MIT in COPYING", "spdx": "MIT"}
    assert decide("MIT", None, row, licence_file=False)[:1] == ("safe",)


def test_pre_gate_skips_clones_that_cannot_pass() -> None:
    assert pre_gate(None, None, has_licence_key=False)[0] == "exclude"
    assert pre_gate("GPL-3.0", None)[0] == "exclude"
    assert pre_gate(None, None, has_licence_key=True) is None      # NOASSERTION: read the file
    assert pre_gate("Apache-2.0", None) is None
    assert pre_gate("MIT", {"verdict": "exclude", "notes": "x"})[0] == "exclude"


def test_register_keeps_people_fields_and_extra_columns(tmp_path: Path) -> None:
    path = tmp_path / "reg.csv"
    path.write_text("owner,repo,spdx,verdict,intended_use,size_kb,pinned_sha,checked_on,notes,reviewer\n"
                    "o,a,MIT,check,codebase-pool,,,2026-11-16,,sam\n")
    reg = LicenceRegister(path)
    reg.upsert("o/a", spdx="MIT", verdict="safe", intended_use="corpus", pinned_sha="abc", notes="")
    reg.upsert("o/b", spdx="", verdict="exclude", intended_use="corpus", notes="no licence")
    reg.save()
    again = LicenceRegister(path)
    a = again.get("o/a")
    assert a["verdict"] == "safe" and a["intended_use"] == "codebase-pool" and a["reviewer"] == "sam"
    assert a["pinned_sha"] == "abc" and a["checked_on"] != "2026-11-16"
    assert again.counts() == {"safe": 1, "exclude": 1}
    assert path.read_text().splitlines()[0].endswith("licence_file_sha256,files,reviewer")


CODE = """import ballerina/http;

service /hello on new http:Listener(9090) {
    resource function get greeting(string name) returns string {
        return "Hello, " + name + "! Welcome to the service, glad to see you here today.";
    }

    resource function get farewell(string name) returns string {
        return "Goodbye, " + name;
    }
}
"""


def test_code_hash_ignores_comments_and_layout() -> None:
    noisy = "// Copyright 2024 WSO2 LLC. Licensed under the Apache License.\n" + CODE.replace("    ", "\t")
    assert code_hash(noisy) == code_hash(CODE)
    assert code_hash(CODE.replace("9090", "8080")) != code_hash(CODE)


def test_minhash_finds_near_copies_only() -> None:
    mh = MinHasher()
    a = mh.signature(CODE)
    near = mh.signature(CODE.replace("farewell", "bye"))
    other = mh.signature("public function main() {\n    int[] xs = [1, 2, 3];\n    foreach int x in xs { }\n}\n")
    assert similarity(a, near) > 0.6 > similarity(a, other)
    assert mh.from_bytes(mh.to_bytes(a)).tolist() == a.tolist()
    lsh = LSH(threshold=0.5)
    lsh.add(1, a)
    assert lsh.query(near) == 1 and lsh.query(other) is None
