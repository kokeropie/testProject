"""
Rule model + evaluator shared by every derived-column step (businessUnit,
subBusinessUnit, productNew/subProduct, Channels/subChannels, Market/subMarket,
reportDate).

Rules are stored on disk as JSON (rules/*.json) in canonical DNF form:

    condition := {"any": [clause, clause, ...]}          # OR of clauses
    clause    := [leaf, leaf, ...]                        # AND of leaves
    leaf      := {"column": str, "operator": op, "negate": bool,
                  "value": literal}            OR
                 {"column": str, "operator": op, "negate": bool,
                  "value_column": str}         # compare to another column

    op in {"=", "<>", "like", ">", ">=", "<", "<="}

    result := {output_col: {"type": "literal", "value": X}, ...}
            | {output_col: {"type": "column", "column": "SomeCol"}, ...}

A step file looks like:

    {
      "step": "businessUnit",
      "label": "Business Unit",
      "key_columns": ["Divisi"],
      "output_columns": ["businessUnit"],
      "default": {"businessUnit": {"type": "literal", "value": "LEI"}},
      "rules": [
        {"id": 1, "condition": {"any": [[{"column": "Divisi", "operator": "=",
          "negate": false, "value": "CC01"}]]},
         "result": {"businessUnit": {"type": "literal", "value": "CTM"}}}
      ]
    }

First matching rule wins. `default` is applied (and may be null, meaning the
row is left unmatched / flagged) when no rule matches.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Any


# ---------------------------------------------------------------------------
# Tokenizer / recursive-descent parser for Qlik-style boolean expressions
# ("Divisi = 'X' and (A or not(B) like 'C*')"). Used only at import time to
# turn dataFilter/*.txt into the canonical DNF rule JSON above — the runtime
# evaluator never re-parses text, it walks the DNF structure directly.
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""
        (?P<lparen>\()
      | (?P<rparen>\))
      | (?P<string>'(?:[^'])*')
      | (?P<ne>\<\>)
      | (?P<ge>\>=)
      | (?P<le>\<=)
      | (?P<gt>\>)
      | (?P<lt>\<)
      | (?P<eq>\=)
      | (?P<word>[A-Za-z_][A-Za-z0-9_]*)
      | (?P<num>-?\d+(\.\d+)?)
    """,
    re.VERBOSE,
)

_OPMAP = {"eq": "=", "ne": "<>", "gt": ">", "lt": "<", "ge": ">=", "le": "<="}


def tokenize(text: str) -> list[tuple[str, str]]:
    toks = []
    pos = 0
    while pos < len(text):
        if text[pos].isspace():
            pos += 1
            continue
        m = _TOKEN_RE.match(text, pos)
        if not m or m.end() == pos:
            raise ValueError(f"cannot tokenize at {pos}: {text[pos:pos + 30]!r}")
        toks.append((m.lastgroup, m.group()))
        pos = m.end()
    return toks


class _ExprParser:
    """Parses tokens into a generic (possibly nested) and/or/not tree."""

    def __init__(self, toks: list[tuple[str, str]]):
        self.toks = toks
        self.pos = 0

    def peek(self) -> tuple[str | None, str | None]:
        return self.toks[self.pos] if self.pos < len(self.toks) else (None, None)

    def advance(self) -> tuple[str | None, str | None]:
        t = self.peek()
        self.pos += 1
        return t

    def _is_word(self, kw: str) -> bool:
        kind, val = self.peek()
        return kind == "word" and val.lower() == kw

    def parse_expr(self) -> dict:
        node = self.parse_and()
        children = [node]
        while self._is_word("or"):
            self.advance()
            children.append(self.parse_and())
        return {"type": "group", "op": "or", "children": children} if len(children) > 1 else node

    def parse_and(self) -> dict:
        node = self.parse_not()
        children = [node]
        while self._is_word("and"):
            self.advance()
            children.append(self.parse_not())
        return {"type": "group", "op": "and", "children": children} if len(children) > 1 else node

    def parse_not(self) -> dict:
        if self._is_word("not"):
            self.advance()
            if self.peek()[0] == "lparen":
                self.advance()
                kind, colname = self.advance()
                assert kind == "word", f"expected identifier inside not(...), got {colname}"
                k2, _ = self.advance()
                assert k2 == "rparen", "expected ) closing not(...)"
                if self._is_word("like"):
                    self.advance()
                    k3, sval = self.advance()
                    assert k3 == "string", "expected string literal after LIKE"
                    return {"type": "cmp", "column": colname, "operator": "like",
                            "value": sval.strip("'"), "negate": True}
                return {"type": "cmp", "column": colname, "operator": "truthy",
                        "value": None, "negate": True}
            node = dict(self.parse_primary())
            node["negate"] = not node.get("negate", False)
            return node
        return self.parse_primary()

    def parse_primary(self) -> dict:
        kind, _ = self.peek()
        if kind == "lparen":
            self.advance()
            node = self.parse_expr()
            k2, v2 = self.advance()
            assert k2 == "rparen", f"expected ), got {v2!r}"
            return node
        kind, colname = self.advance()
        assert kind == "word", f"expected identifier, got {kind} {colname!r}"
        opkind, opval = self.advance()
        if opkind in _OPMAP:
            op = _OPMAP[opkind]
        elif opkind == "word" and opval.lower() == "like":
            op = "like"
        else:
            raise ValueError(f"expected comparison operator, got {opkind} {opval!r}")
        rkind, rval = self.advance()
        if rkind == "string":
            return {"type": "cmp", "column": colname, "operator": op, "value": rval.strip("'")}
        if rkind == "num":
            value: Any = float(rval) if "." in rval else int(rval)
            return {"type": "cmp", "column": colname, "operator": op, "value": value}
        if rkind == "word":
            return {"type": "cmp", "column": colname, "operator": op, "value_column": rval}
        raise ValueError(f"unexpected right-hand side {rkind} {rval!r}")


def parse_condition_tree(text: str) -> dict:
    """Parse a raw Qlik boolean expression into a generic and/or/not tree."""
    toks = tokenize(text)
    parser = _ExprParser(toks)
    node = parser.parse_expr()
    if parser.pos != len(parser.toks):
        raise ValueError(f"leftover tokens after parsing: {parser.toks[parser.pos:]}")
    return node


# ---------------------------------------------------------------------------
# Generic tree -> canonical DNF ({"any": [[leaf, ...], ...]})
# ---------------------------------------------------------------------------

def _leaf_clauses(node: dict) -> list[list[dict]]:
    """Distribute a generic and/or/not tree into a list of AND-clauses (DNF)."""
    if node["type"] == "cmp":
        leaf = {k: v for k, v in node.items() if k != "type"}
        leaf.setdefault("negate", False)
        return [[leaf]]

    op = node["op"]
    negate = node.get("negate", False)
    child_clauses = [_leaf_clauses(c) for c in node["children"]]

    if op == "and":
        result = [[]]
        for clauses in child_clauses:
            result = [prefix + clause for prefix in result for clause in clauses]
    else:  # or
        result = [clause for clauses in child_clauses for clause in clauses]

    if negate:
        # De Morgan: NOT(OR of AND-clauses) = AND over clauses of (OR of negated leaves),
        # then re-distribute that AND-of-ORs back into DNF.
        or_groups = []
        for clause in result:
            negated_leaves = []
            for leaf in clause:
                nl = dict(leaf)
                nl["negate"] = not nl.get("negate", False)
                negated_leaves.append([nl])
            or_groups.append(negated_leaves)
        result2 = [[]]
        for group in or_groups:
            result2 = [prefix + clause for prefix in result2 for clause in group]
        result = result2

    return result


def to_dnf(node: dict) -> dict:
    """Convert a generic and/or/not tree (or a single leaf) into {"any": [...]}."""
    return {"any": _leaf_clauses(node)}


def parse_condition_dnf(text: str) -> dict:
    """Parse raw Qlik boolean text straight into canonical DNF."""
    return to_dnf(parse_condition_tree(text))


def single_leaf_dnf(column: str, operator: str, value: Any = None,
                     value_column: str | None = None, negate: bool = False) -> dict:
    leaf = {"column": column, "operator": operator, "negate": negate}
    if value_column is not None:
        leaf["value_column"] = value_column
    else:
        leaf["value"] = value
    return {"any": [[leaf]]}


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------

def _cmp_ok(operator: str, actual: Any, expected: Any) -> bool:
    if operator == "=":
        return actual == expected
    if operator == "<>":
        return actual != expected
    if operator == "like":
        if actual is None:
            return False
        return fnmatch.fnmatchcase(str(actual), str(expected))
    if operator in (">", ">=", "<", "<="):
        try:
            if actual is None or expected is None:
                return False
            if operator == ">":
                return actual > expected
            if operator == ">=":
                return actual >= expected
            if operator == "<":
                return actual < expected
            return actual <= expected
        except TypeError:
            return False
    if operator == "truthy":
        return bool(actual)
    raise ValueError(f"unknown operator: {operator}")


def resolve_column(name: str, column_aliases: dict[str, str]) -> str:
    """Explicit alias first (e.g. Qlik's subChannels2 -> our subChannels),
    then case-insensitive fallback (rule text says clientSegment, the actual
    data column is ClientSegment — Qlik itself is case-insensitive on field
    names, our data isn't, so this fallback restores that behavior)."""
    if name in column_aliases:
        return column_aliases[name]
    lower = name.lower()
    if lower in column_aliases:
        return column_aliases[lower]
    return name


def eval_leaf(leaf: dict, row: Any, column_aliases: dict[str, str] | None = None) -> bool:
    column_aliases = column_aliases or {}
    col = resolve_column(leaf["column"], column_aliases)
    actual = row.get(col) if hasattr(row, "get") else row[col]
    if "value_column" in leaf:
        vc = resolve_column(leaf["value_column"], column_aliases)
        expected = row.get(vc) if hasattr(row, "get") else row[vc]
    else:
        expected = leaf.get("value")
    result = _cmp_ok(leaf["operator"], actual, expected)
    return (not result) if leaf.get("negate") else result


def eval_dnf(condition: dict, row: Any, column_aliases: dict[str, str] | None = None) -> bool:
    """condition = {"any": [[leaf, leaf, ...], [leaf, ...], ...]}"""
    for clause in condition["any"]:
        if all(eval_leaf(leaf, row, column_aliases) for leaf in clause):
            return True
    return False


def resolve_result(result: dict, row: Any, column_aliases: dict[str, str] | None = None) -> dict:
    column_aliases = column_aliases or {}
    out = {}
    for col, spec in result.items():
        if spec is None:
            out[col] = None
        elif spec["type"] == "literal":
            out[col] = spec["value"]
        elif spec["type"] == "column":
            src = resolve_column(spec["column"], column_aliases)
            out[col] = row.get(src) if hasattr(row, "get") else row[src]
        else:
            raise ValueError(f"unknown result spec type: {spec['type']}")
    return out


@dataclass
class MatchOutcome:
    rule_id: int | None  # None => default (or unmatched if default is also None)
    values: dict


def apply_step(step_def: dict, row: Any, column_aliases: dict[str, str] | None = None) -> MatchOutcome:
    for rule in step_def["rules"]:
        if eval_dnf(rule["condition"], row, column_aliases):
            return MatchOutcome(rule["id"], resolve_result(rule["result"], row, column_aliases))
    default = step_def.get("default")
    if default is None:
        return MatchOutcome(None, {col: None for col in step_def["output_columns"]})
    return MatchOutcome(None, resolve_result(default, row, column_aliases))


# ---------------------------------------------------------------------------
# Human-readable rendering (for the GUI's read-only rule list)
# ---------------------------------------------------------------------------

def leaf_to_text(leaf: dict) -> str:
    op = leaf["operator"]
    col = leaf["column"]
    prefix = "NOT " if leaf.get("negate") else ""
    if "value_column" in leaf:
        return f"{prefix}{col} {op} {leaf['value_column']}"
    val = leaf.get("value")
    if op == "truthy":
        return f"{prefix}{col}"
    return f"{prefix}{col} {op} '{val}'" if isinstance(val, str) else f"{prefix}{col} {op} {val}"


def condition_to_text(condition: dict) -> str:
    clauses = condition["any"]
    clause_strs = []
    for clause in clauses:
        leaf_strs = [leaf_to_text(leaf) for leaf in clause]
        clause_strs.append(" AND ".join(leaf_strs) if len(leaf_strs) > 1 else leaf_strs[0])
    if len(clause_strs) == 1:
        return clause_strs[0]
    return " OR ".join(f"({c})" if " AND " in c else c for c in clause_strs)


def result_to_text(result: dict) -> str:
    parts = []
    for col, spec in result.items():
        if spec is None:
            parts.append(f"{col}=NULL")
        elif spec["type"] == "literal":
            parts.append(f"{col}='{spec['value']}'")
        else:
            parts.append(f"{col}={spec['column']} (passthrough)")
    return ", ".join(parts)
