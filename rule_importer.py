"""
One-time importer: parses dataFilter/*.txt (Qlik-style If()/LOAD-WHERE rule
scripts) into structured rules/*.json (see rules_engine.py for the schema).

After running this once, rules/*.json is the source of truth — edit rules
through the Streamlit GUI (app.py), not by hand-editing dataFilter/*.txt.
Re-running this script overwrites rules/*.json with a fresh parse of the
.txt files, discarding any GUI edits, so it's meant to be run once at setup
(or deliberately, to reset to the raw source).

Usage:
    python rule_importer.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from rules_engine import parse_condition_dnf, single_leaf_dnf

DATA_FILTER_DIR = Path(__file__).parent / "dataFilter"
RULES_DIR = Path(__file__).parent / "rules"

_IF_STEP_RE = re.compile(
    r"If\(\s*(?P<col>\w+)\s*(?P<op>=|like)\s*'(?P<val>[^']*)'\s*,\s*'(?P<res>[^']*)'\s*,\s*",
    re.IGNORECASE,
)


def _parse_nested_if_chain(text: str) -> tuple[list[tuple[str, str, str, str]], str]:
    """Unwrap If(c1,'r1',If(c2,'r2',...,default)) into an ordered rule list
    plus the trailing default (quoted literal or bare column-passthrough)."""
    text = text.strip()
    rules = []
    pos = 0
    while True:
        m = _IF_STEP_RE.match(text, pos)
        if not m:
            break
        rules.append((m.group("col"), m.group("op"), m.group("val"), m.group("res")))
        pos = m.end()
    rest = text[pos:].strip()
    rest = re.split(r"\)+\s*(as\s+\w+)?\s*,?\s*$", rest, flags=re.IGNORECASE)[0].strip()
    return rules, rest


_IF_MULTI_RE = re.compile(
    r"If\(\s*(?P<cond>[^,]+?)\s*,\s*'(?P<res>[^']*)'\s*,\s*",
    re.IGNORECASE,
)


def _parse_nested_if_chain_multi(text: str) -> tuple[list[tuple[dict, str]], str]:
    """Same shape as _parse_nested_if_chain, but each If()'s condition may be
    a compound boolean expression (e.g. "AIRLINE = 'AF' and subClass = 'A'"),
    not just a single column = literal leaf — parsed via the same
    tokenizer/parser used for the LOAD/WHERE dialect (parse_condition_dnf)."""
    text = text.strip()
    rules = []
    pos = 0
    while True:
        m = _IF_MULTI_RE.match(text, pos)
        if not m:
            break
        rules.append((parse_condition_dnf(m.group("cond")), m.group("res")))
        pos = m.end()
    rest = text[pos:].strip()
    rest = re.split(r"\)+\s*(as\s+\w+)?\s*,?\s*$", rest, flags=re.IGNORECASE)[0].strip()
    return rules, rest


def _default_spec(output_col: str, rest: str) -> dict:
    if rest.startswith("'") and rest.endswith("'"):
        return {output_col: {"type": "literal", "value": rest.strip("'")}}
    return {output_col: {"type": "column", "column": rest}}


def _build_single_output_step(step_name: str, label: str, key_col: str,
                               output_col: str, chain_rules, default_rest: str) -> dict:
    rules = []
    for i, (col, op, val, res) in enumerate(chain_rules, start=1):
        rules.append({
            "id": i,
            "condition": single_leaf_dnf(col, op, value=val),
            "result": {output_col: {"type": "literal", "value": res}},
        })
    return {
        "step": step_name,
        "label": label,
        "key_columns": [key_col],
        "output_columns": [output_col],
        "default": _default_spec(output_col, default_rest),
        "rules": rules,
    }


def import_business_unit() -> dict:
    text = (DATA_FILTER_DIR / "businessUnit.txt").read_text()
    idx = text.index("as businessUnit")
    chain_rules, rest = _parse_nested_if_chain(text[:idx])
    return _build_single_output_step("businessUnit", "Business Unit", "Divisi",
                                      "businessUnit", chain_rules, rest)


def import_sub_business_unit() -> dict:
    text = (DATA_FILTER_DIR / "subBusinessUnit.txt").read_text()
    idx = text.index("as subBusinessUnit")
    chain_rules, rest = _parse_nested_if_chain(text[:idx])
    return _build_single_output_step("subBusinessUnit", "Sub Business Unit", "Divisi",
                                      "subBusinessUnit", chain_rules, rest)


def import_product() -> dict:
    text = (DATA_FILTER_DIR / "product.txt").read_text()
    idx1 = text.index("as productNew,")
    chain1_text = text[:idx1]
    rest_text = text[idx1 + len("as productNew,"):]
    idx2 = rest_text.index("as subProduct,")
    chain2_text = rest_text[:idx2]

    r1, d1 = _parse_nested_if_chain(chain1_text)
    r2, d2 = _parse_nested_if_chain(chain2_text)

    rules1 = [{"id": i, "condition": single_leaf_dnf(c, op, value=v),
               "result": {"productNew": {"type": "literal", "value": res}}}
              for i, (c, op, v, res) in enumerate(r1, start=1)]
    rules2 = [{"id": i, "condition": single_leaf_dnf(c, op, value=v),
               "result": {"subProduct": {"type": "literal", "value": res}}}
              for i, (c, op, v, res) in enumerate(r2, start=1)]

    return {
        "step": "product",
        "label": "Product",
        "key_columns": ["PCID"],
        "output_columns": ["productNew", "subProduct"],
        "chains": {
            "productNew": {
                "default": _default_spec("productNew", d1),
                "rules": rules1,
            },
            "subProduct": {
                "default": _default_spec("subProduct", d2),
                "rules": rules2,
            },
        },
    }


def import_sub_class2() -> dict:
    """subClass2.txt: nested If chain, keyed on subClass (itself derived from
    SUMMARYROUTE — see pipeline.derive_sub_class()) and AIRLINE for the
    per-carrier First/Business exceptions. Default 'Economy'."""
    text = (DATA_FILTER_DIR / "subClass2.txt").read_text()
    idx = text.index("as subClass2")
    chain_rules, rest = _parse_nested_if_chain_multi(text[:idx])
    rules = [{"id": i, "condition": cond, "result": {"subClass2": {"type": "literal", "value": res}}}
              for i, (cond, res) in enumerate(chain_rules, start=1)]
    return {
        "step": "subClass2",
        "label": "Sub Class 2",
        "key_columns": ["subClass", "AIRLINE"],
        "output_columns": ["subClass2"],
        "default": _default_spec("subClass2", rest),
        "rules": rules,
    }


def import_sub_class3() -> dict:
    """subClass3.txt: nested If chain of per-carrier Premium Economy
    exceptions, keyed on subClass and AIRLINE; falls back to subClass2
    (column passthrough, not a literal) when none match."""
    text = (DATA_FILTER_DIR / "subClass3.txt").read_text()
    idx = text.index("as subClass3")
    chain_rules, rest = _parse_nested_if_chain_multi(text[:idx])
    rules = [{"id": i, "condition": cond, "result": {"subClass3": {"type": "literal", "value": res}}}
              for i, (cond, res) in enumerate(chain_rules, start=1)]
    return {
        "step": "subClass3",
        "label": "Sub Class 3",
        "key_columns": ["subClass", "AIRLINE", "subClass2"],
        "output_columns": ["subClass3"],
        "default": _default_spec("subClass3", rest),
        "rules": rules,
    }


def _import_where_blocks(path: Path, step_name: str, label: str,
                          key_columns: list[str], output_columns: list[str],
                          value_markers: list[str]) -> dict:
    """channel.txt / market.txt / mgmtRpt.txt: LOAD ... 'val' as Col1, 'val' as Col2 ...
    WHERE cond; repeated N times via Concatenate. First matching block wins, no default.
    Full-line `//` comments are dropped first — mgmtRpt.txt has a commented-out
    `//Where ...;` block that would otherwise be mistaken for a real one."""
    text = path.read_text()
    text = "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))
    where_matches = list(re.finditer(r"Where\s+(.*?);", text, re.DOTALL | re.IGNORECASE))
    rules = []
    prev_end = 0
    for i, m in enumerate(where_matches, start=1):
        segment = text[prev_end:m.start()]
        result = {}
        for out_col, marker in zip(output_columns, value_markers):
            vm = re.search(rf"'([^']*)'\s+as\s+{re.escape(marker)}\s*,", segment)
            if not vm:
                raise ValueError(f"block {i} in {path.name}: could not find '{marker}' assignment")
            result[out_col] = {"type": "literal", "value": vm.group(1)}
        cond_text = re.sub(r"\s+", " ", m.group(1)).strip()
        rules.append({"id": i, "condition": parse_condition_dnf(cond_text), "result": result})
        prev_end = m.end()

    return {
        "step": step_name,
        "label": label,
        "key_columns": key_columns,
        "output_columns": output_columns,
        "default": None,
        "rules": rules,
    }


def import_channel() -> dict:
    return _import_where_blocks(
        DATA_FILTER_DIR / "channel.txt", "channel", "Channel",
        key_columns=["Divisi", "ClientSegment", "ClientName", "InvoiceID"],
        output_columns=["Channels", "subChannels"],
        value_markers=["Channels", "subChannels2"],
    )


def import_market() -> dict:
    return _import_where_blocks(
        DATA_FILTER_DIR / "market.txt", "market", "Market",
        key_columns=["Divisi", "ClientSegment", "ClientName", "InvoiceID",
                     "subProduct", "subChannels"],
        output_columns=["Market", "subMarket"],
        value_markers=["Market", "subMarket"],
    )


def import_mgmt_rpt() -> dict:
    return _import_where_blocks(
        DATA_FILTER_DIR / "mgmtRpt.txt", "mgmtRpt", "Management Report",
        key_columns=["subBusinessUnit", "clientSegment", "subProduct", "Divisi",
                     "newPCID", "marker", "Channels"],
        output_columns=["mgmtRpt"],
        value_markers=["mgmtRpt"],
    )


def import_report_date() -> dict:
    """reportDate.txt: flat 'If <cond> then <result> else' chain, one per line,
    result is always a column reference (DateOfCreated / VoidDate /
    DATEOFSERVICESTART), never a literal."""
    lines = (DATA_FILTER_DIR / "reportDate.txt").read_text().splitlines()
    line_re = re.compile(r"If\s+(?P<cond>.*?)\s+then\s+(?P<res>\w+)\s*(?:else)?\s*$", re.IGNORECASE)
    rules = []
    for i, line in enumerate(lines, start=1):
        line = line.strip()
        if not line:
            continue
        m = line_re.match(line)
        if not m:
            raise ValueError(f"reportDate.txt line {i}: could not parse {line!r}")
        rules.append({
            "id": i,
            "condition": parse_condition_dnf(m.group("cond")),
            "result": {"reportDate": {"type": "column", "column": m.group("res")}},
        })
    return {
        "step": "reportDate",
        "label": "Report Date",
        "key_columns": ["productNew", "Voided", "DateOfCreated", "VoidDate", "DATEOFSERVICESTART"],
        "output_columns": ["reportDate"],
        "default": None,
        "rules": rules,
    }


DEFAULT_CONFIG = {
    "voided_column": "Voided",
    "voided_nonzero_encoding": True,
    "voided_literal_in_rules": 1,
    "negation_columns": [
        "ADDFEE", "AGENTCHARGE", "NETFARE", "NIGHTS", "ORGAMT",
        "RealNETFARE", "RealTAX", "SUBTOTAL", "SUBTOTALORG", "TAX", "VAT",
    ],
    "void_status_active_value": "all",
    "void_status_void_value": "void",
    "column_aliases": {"subChannels2": "subChannels"},
}


def run() -> None:
    RULES_DIR.mkdir(exist_ok=True)
    steps = {
        "businessUnit": import_business_unit(),
        "subBusinessUnit": import_sub_business_unit(),
        "product": import_product(),
        "channel": import_channel(),
        "market": import_market(),
        "reportDate": import_report_date(),
        "mgmtRpt": import_mgmt_rpt(),
        "subClass2": import_sub_class2(),
        "subClass3": import_sub_class3(),
    }
    for name, step_def in steps.items():
        out_path = RULES_DIR / f"{name}.json"
        out_path.write_text(json.dumps(step_def, indent=2))
        print(f"wrote {out_path} ({len(step_def.get('rules', [])) or 'n/a'} rules)")

    config_path = RULES_DIR / "config.json"
    if not config_path.exists():
        config_path.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
        print(f"wrote {config_path}")
    else:
        print(f"kept existing {config_path} (delete it to reset to defaults)")


if __name__ == "__main__":
    run()
