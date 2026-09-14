#!/usr/bin/env python3
"""Validate anilibria.yml against the official Cardigann v11 JSON schema.

Prowlarr silently ignores keys it does not understand and drops category
mappings it cannot resolve, so a typo usually shows up as missing data in search
results rather than as an error. This checks the definition up front.

Only PyYAML is required; the small subset of JSON Schema that the Cardigann
schema actually uses is implemented here so nothing has to be installed.

Usage:
    python3 scripts/validate_definition.py [path/to/definition.yml]
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: python3 -m pip install pyyaml")

SCHEMA_URL = "https://raw.githubusercontent.com/Prowlarr/Indexers/master/definitions/v11/schema.json"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CACHE = os.path.join(ROOT, ".cache", "schema-v11.json")
DEFAULT_DEFINITION = os.path.join(ROOT, "definitions", "anilibria.yml")

TYPE_MAP = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


def fetch_schema() -> dict:
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    if os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8") as handle:
            return json.load(handle)
    with urllib.request.urlopen(SCHEMA_URL, timeout=30) as response:
        schema = json.loads(response.read().decode("utf-8"))
    with open(CACHE, "w", encoding="utf-8") as handle:
        json.dump(schema, handle)
    return schema


def resolve_ref(root: dict, ref: str):
    if not ref.startswith("#/"):
        raise ValueError(f"unsupported $ref {ref}")
    node = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def type_matches(value, expected: str) -> bool:
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "null":
        return value is None
    return True


def validate(instance, schema: dict, root: dict, path: str = "") -> list[str]:
    errors: list[str] = []

    if "$ref" in schema:
        errors.extend(validate(instance, resolve_ref(root, schema["$ref"]), root, path))
        schema = {k: v for k, v in schema.items() if k != "$ref"}
        if not schema:
            return errors

    if "type" in schema:
        expected = schema["type"]
        candidates = expected if isinstance(expected, list) else [expected]
        if not any(type_matches(instance, one) for one in candidates):
            return [f"{path or '<root>'}: expected {'/'.join(candidates)}, got {type(instance).__name__}"]

    if "enum" in schema and instance not in schema["enum"]:
        allowed = schema["enum"]
        preview = ", ".join(repr(a) for a in allowed[:8])
        more = f" ... ({len(allowed)} total)" if len(allowed) > 8 else ""
        errors.append(f"{path or '<root>'}: {instance!r} is not one of [{preview}{more}]")
        return errors

    if "not" in schema and not validate(instance, schema["not"], root, path):
        errors.append(f"{path or '<root>'}: value {instance!r} is not allowed here")

    if "oneOf" in schema:
        matches = [sub for sub in schema["oneOf"] if not validate(instance, sub, root, path)]
        if len(matches) != 1:
            errors.append(f"{path or '<root>'}: expected exactly one of the allowed shapes, matched {len(matches)}")

    if "anyOf" in schema:
        if not any(not validate(instance, sub, root, path) for sub in schema["anyOf"]):
            errors.append(f"{path or '<root>'}: does not match any of the allowed shapes")

    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path or '<root>'}: missing required key {key!r}")

        properties = schema.get("properties", {})
        patterns = schema.get("patternProperties", {})

        for key, value in instance.items():
            child = f"{path}.{key}" if path else key
            if key in properties:
                errors.extend(validate(value, properties[key], root, child))
                continue
            matched = False
            for pattern, subschema in patterns.items():
                if re.search(pattern, key):
                    matched = True
                    errors.extend(validate(value, subschema, root, child))
            if not matched and schema.get("additionalProperties") is False:
                known = ", ".join(sorted(properties)) or "(none)"
                errors.append(f"{child}: unknown key; allowed here: {known}")

    if isinstance(instance, list):
        if schema.get("uniqueItems") and len(instance) != len({json.dumps(i, sort_keys=True) for i in instance}):
            errors.append(f"{path or '<root>'}: duplicate items are not allowed")
        if "items" in schema:
            for index, item in enumerate(instance):
                errors.extend(validate(item, schema["items"], root, f"{path}[{index}]"))

    return errors


def main() -> int:
    definition_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DEFINITION
    schema = fetch_schema()

    with open(definition_path, encoding="utf-8") as handle:
        definition = yaml.safe_load(handle)

    errors = validate(definition, schema, schema)

    relative = os.path.relpath(definition_path, ROOT)
    print(f"validating {relative} against Cardigann v11")
    print(f"  id           : {definition.get('id')}")
    print(f"  name         : {definition.get('name')}")
    print(f"  search modes : {sorted((definition.get('caps', {}).get('modes') or {}))}")
    mappings = definition.get("caps", {}).get("categorymappings") or []
    print(f"  categories   : {len(mappings)} mapping(s)")

    fields = definition.get("search", {}).get("fields") or {}
    print(f"  fields       : {len(fields)} -> {', '.join(fields)}")

    if errors:
        print(f"\n{len(errors)} schema violation(s):")
        for error in errors:
            print(f"  - {error}")
        return 1

    print("\nOK: definition conforms to the Cardigann v11 schema")
    return 0


if __name__ == "__main__":
    sys.exit(main())
