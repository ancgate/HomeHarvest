"""
Minimal GraphQL document validator backed by the bundled Realtor.com introspection schema.

It checks that every selected field, argument, input-object key and enum literal used in a
query exists in ``homeharvest/core/scrapers/realtor/introspection.json``. This lets the test
suite verify queries offline (no network access to Realtor.com required).

It intentionally does not implement the full GraphQL spec - only what HomeHarvest's queries use.
"""

from __future__ import annotations

import json
import os
import re

_SCHEMA_PATH = os.path.join(
    os.path.dirname(__file__), "..", "homeharvest", "core", "scrapers", "realtor", "introspection.json"
)

_TOKEN_RE = re.compile(
    r'\s+|,|#[^\n]*'                      # ignored
    r'|(?P<spread>\.\.\.)'
    r'|(?P<string>"(?:[^"\\]|\\.)*")'
    r'|(?P<number>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)'
    r'|(?P<name>[_A-Za-z][_0-9A-Za-z]*)'
    r'|(?P<punct>[{}()\[\]:!$=@])'
)


class GraphQLValidationError(AssertionError):
    pass


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens, pos = [], 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise GraphQLValidationError(f"Unexpected character {text[pos]!r} at {pos}: {text[pos:pos + 40]!r}")
        pos = m.end()
        for kind in ("spread", "string", "number", "name", "punct"):
            if m.group(kind) is not None:
                tokens.append((kind, m.group(kind)))
                break
    return tokens


class _Schema:
    def __init__(self, path: str = _SCHEMA_PATH):
        with open(path) as f:
            data = json.load(f)
        schema = data["data"]["__schema"]
        self.types = {t["name"]: t for t in schema["types"]}
        self.query_type = schema["queryType"]["name"]

    @staticmethod
    def unwrap(type_ref: dict) -> tuple[str, bool]:
        """Return (named type, is_list)."""
        is_list = False
        while type_ref.get("ofType") is not None and type_ref["kind"] in ("NON_NULL", "LIST"):
            if type_ref["kind"] == "LIST":
                is_list = True
            type_ref = type_ref["ofType"]
        return type_ref["name"], is_list

    def field(self, type_name: str, field_name: str) -> dict | None:
        t = self.types.get(type_name) or {}
        for f in t.get("fields") or []:
            if f["name"] == field_name:
                return f
        return None

    def input_field(self, type_name: str, field_name: str) -> dict | None:
        t = self.types.get(type_name) or {}
        for f in t.get("inputFields") or []:
            if f["name"] == field_name:
                return f
        return None


_SCHEMA: _Schema | None = None


def _schema() -> _Schema:
    global _SCHEMA
    if _SCHEMA is None:
        _SCHEMA = _Schema()
    return _SCHEMA


class _Parser:
    def __init__(self, text: str):
        self.tokens = _tokenize(text)
        self.i = 0

    def peek(self, offset: int = 0):
        j = self.i + offset
        return self.tokens[j] if j < len(self.tokens) else (None, None)

    def next(self):
        tok = self.peek()
        self.i += 1
        return tok

    def expect(self, value: str):
        kind, val = self.next()
        if val != value:
            raise GraphQLValidationError(f"Expected {value!r} but got {val!r}")

    # --- values -------------------------------------------------------------
    def value(self):
        kind, val = self.peek()
        if val == "$":
            self.next()
            return ("var", self.next()[1])
        if val == "{":
            self.next()
            obj = {}
            while self.peek()[1] != "}":
                key = self.next()[1]
                self.expect(":")
                obj[key] = self.value()
            self.next()
            return ("object", obj)
        if val == "[":
            self.next()
            items = []
            while self.peek()[1] != "]":
                items.append(self.value())
            self.next()
            return ("list", items)
        self.next()
        if kind == "string":
            return ("string", val)
        if kind == "number":
            return ("number", val)
        if val in ("true", "false"):
            return ("bool", val)
        if val == "null":
            return ("null", val)
        return ("enum", val)

    def arguments(self) -> dict:
        args = {}
        if self.peek()[1] == "(":
            self.next()
            while self.peek()[1] != ")":
                name = self.next()[1]
                self.expect(":")
                args[name] = self.value()
            self.next()
        return args

    # --- selections ---------------------------------------------------------
    def selection_set(self) -> list:
        self.expect("{")
        selections = []
        while self.peek()[1] != "}":
            if self.peek()[0] == "spread":
                self.next()
                if self.peek()[1] == "on":
                    self.next()
                    type_cond = self.next()[1]
                    selections.append(("inline", type_cond, self.selection_set()))
                else:
                    selections.append(("spread", self.next()[1]))
                continue
            name = self.next()[1]
            alias = None
            if self.peek()[1] == ":":
                self.next()
                alias, name = name, self.next()[1]
            args = self.arguments()
            sub = self.selection_set() if self.peek()[1] == "{" else None
            selections.append(("field", alias, name, args, sub))
        self.next()
        return selections

    def document(self):
        operations, fragments = [], {}
        while self.peek()[0] is not None:
            kind, val = self.peek()
            if val == "{":
                operations.append(self.selection_set())
            elif val in ("query", "mutation"):
                self.next()
                if self.peek()[0] == "name":
                    self.next()
                if self.peek()[1] == "(":  # variable definitions - skip balanced
                    depth = 0
                    while True:
                        _, v = self.next()
                        depth += v == "("
                        depth -= v == ")"
                        if depth == 0:
                            break
                operations.append(self.selection_set())
            elif val == "fragment":
                self.next()
                name = self.next()[1]
                self.expect("on")
                type_cond = self.next()[1]
                fragments[name] = (type_cond, self.selection_set())
            else:
                raise GraphQLValidationError(f"Unexpected token {val!r} at document level")
        return operations, fragments


def _validate_value(schema: _Schema, type_ref: dict, value, path: str, errors: list):
    named, is_list = schema.unwrap(type_ref)
    kind, payload = value
    if kind in ("var", "null"):
        return
    if kind == "list":
        for item in payload:
            _validate_value(schema, _strip_list(type_ref), item, path + "[]", errors)
        return
    t = schema.types.get(named)
    if t is None:
        return
    if t["kind"] == "INPUT_OBJECT":
        if kind != "object":
            errors.append(f"{path}: expected input object {named}, got {kind}")
            return
        for key, sub_value in payload.items():
            input_field = schema.input_field(named, key)
            if input_field is None:
                errors.append(f"{path}.{key}: unknown input field on {named}")
                continue
            _validate_value(schema, input_field["type"], sub_value, f"{path}.{key}", errors)
    elif t["kind"] == "ENUM":
        allowed = {e["name"] for e in t.get("enumValues") or []}
        if kind != "enum" or payload not in allowed:
            errors.append(f"{path}: {payload!r} is not a valid {named} value")


def _strip_list(type_ref: dict) -> dict:
    while type_ref["kind"] == "NON_NULL":
        type_ref = type_ref["ofType"]
    if type_ref["kind"] == "LIST":
        return type_ref["ofType"]
    return type_ref


def _validate_selections(schema, type_name, selections, fragments, path, errors):
    for sel in selections:
        if sel[0] == "spread":
            frag = fragments.get(sel[1])
            if frag is None:
                errors.append(f"{path}: unknown fragment {sel[1]}")
                continue
            _validate_selections(schema, frag[0], frag[1], fragments, f"{path}...{sel[1]}", errors)
            continue
        if sel[0] == "inline":
            _validate_selections(schema, sel[1], sel[2], fragments, f"{path}...on {sel[1]}", errors)
            continue
        _, alias, name, args, sub = sel
        if name == "__typename":
            continue
        field = schema.field(type_name, name)
        if field is None:
            errors.append(f"{path}.{name}: field does not exist on {type_name}")
            continue
        arg_defs = {a["name"]: a for a in field.get("args") or []}
        for arg_name, arg_value in args.items():
            if arg_name not in arg_defs:
                errors.append(f"{path}.{name}({arg_name}): unknown argument")
                continue
            _validate_value(schema, arg_defs[arg_name]["type"], arg_value, f"{path}.{name}({arg_name})", errors)
        child_type, _ = schema.unwrap(field["type"])
        child_kind = schema.types.get(child_type, {}).get("kind")
        if sub is not None:
            _validate_selections(schema, child_type, sub, fragments, f"{path}.{alias or name}", errors)
        elif child_kind in ("OBJECT", "INTERFACE", "UNION"):
            errors.append(f"{path}.{name}: object field {child_type} requires a selection set")


def validate_query(query: str) -> list[str]:
    """Return a list of validation errors (empty list means the query is valid)."""
    schema = _schema()
    operations, fragments = _Parser(query).document()
    errors: list[str] = []
    for selections in operations:
        _validate_selections(schema, schema.query_type, selections, fragments, schema.query_type, errors)
    return errors


def assert_valid_query(query: str) -> None:
    errors = validate_query(query)
    if errors:
        raise GraphQLValidationError("Invalid GraphQL query:\n  " + "\n  ".join(errors))
