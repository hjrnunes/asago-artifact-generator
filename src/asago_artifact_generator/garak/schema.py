"""Small, deterministic JSON Schema validator for bound tool arguments.

The artifact generator deliberately does not depend on a runtime JSON Schema
package.  Tool schemas are supplied by the producer and must therefore be
checked at the compiler boundary and again when an artifact is reloaded.
This module implements the closed subset needed by the wire contract.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

_TYPES = frozenset({"array", "boolean", "integer", "null", "number", "object", "string"})


_SUPPORTED_KEYWORDS = frozenset(
    {
        "$comment",
        "$id",
        "$schema",
        "additionalProperties",
        "allOf",
        "anyOf",
        "const",
        "default",
        "deprecated",
        "description",
        "enum",
        "examples",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "items",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "oneOf",
        "pattern",
        "properties",
        "readOnly",
        "required",
        "title",
        "type",
        "uniqueItems",
        "writeOnly",
    }
)


def validate_schema(schema: Any) -> list[str]:
    """Return deterministic errors for an unsupported or malformed schema."""

    if not isinstance(schema, Mapping):
        return ["bound tool schema must be an object"]
    return _schema_errors(schema, "$", set())


def validate_instance(instance: Any, schema: Any) -> list[str]:
    """Return errors when *instance* is not accepted by *schema*."""

    schema_errors = validate_schema(schema)
    if schema_errors:
        return schema_errors
    return _instance_errors(instance, schema, "$")


def _schema_errors(schema: Mapping[str, Any], path: str, seen: set[int]) -> list[str]:
    if id(schema) in seen:
        return [f"{path}: recursive schemas are unsupported"]
    seen.add(id(schema))
    errors = _schema_keyword_errors(schema, path)
    errors.extend(_unsupported_keyword_errors(schema, path))
    errors.extend(_schema_child_errors(schema, path, seen))
    seen.remove(id(schema))
    return errors


def _schema_keyword_errors(schema: Mapping[str, Any], path: str) -> list[str]:
    return [
        *_unsupported_reference_errors(schema, path),
        *_schema_type_errors(schema, path),
        *_schema_required_errors(schema, path),
        *_schema_properties_errors(schema, path),
        *_schema_additional_errors(schema, path),
        *_schema_enum_errors(schema, path),
        *_numeric_schema_errors(schema, path),
        *_array_schema_errors(schema, path),
    ]


def _unsupported_reference_errors(schema: Mapping[str, Any], path: str) -> list[str]:
    if "$ref" in schema:
        return [f"{path}: $ref schemas are unsupported"]
    return []


def _schema_type_errors(schema: Mapping[str, Any], path: str) -> list[str]:
    schema_type = schema.get("type")
    if schema_type is None or _valid_type_value(schema_type):
        return []
    return [f"{path}: type must be a supported JSON Schema type"]


def _schema_required_errors(schema: Mapping[str, Any], path: str) -> list[str]:
    required = schema.get("required")
    if required is None or _valid_required(required):
        return []
    return [f"{path}: required must be an array of unique strings"]


def _schema_properties_errors(schema: Mapping[str, Any], path: str) -> list[str]:
    properties = schema.get("properties")
    if properties is None or isinstance(properties, Mapping):
        return []
    return [f"{path}: properties must be an object"]


def _schema_additional_errors(schema: Mapping[str, Any], path: str) -> list[str]:
    additional = schema.get("additionalProperties")
    if additional is None or _valid_additional(additional):
        return []
    return [f"{path}: additionalProperties must be boolean or schema"]


def _schema_enum_errors(schema: Mapping[str, Any], path: str) -> list[str]:
    enum = schema.get("enum")
    if enum is None or isinstance(enum, list):
        return []
    return [f"{path}: enum must be an array"]


def _unsupported_keyword_errors(schema: Mapping[str, Any], path: str) -> list[str]:
    return [
        f"{path}: unsupported JSON Schema keyword {key!r}"
        for key in sorted(set(schema) - _SUPPORTED_KEYWORDS)
    ]


def _valid_type_value(value: Any) -> bool:
    if isinstance(value, str):
        return value in _TYPES
    if not isinstance(value, list) or not value:
        return False
    return all(_valid_type_name(item) for item in value)


def _valid_type_name(value: Any) -> bool:
    return isinstance(value, str) and value in _TYPES


def _valid_required(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    if not all(isinstance(item, str) for item in value):
        return False
    return len(value) == len(set(value))


def _valid_additional(value: Any) -> bool:
    return isinstance(value, bool) or isinstance(value, Mapping)


def _numeric_schema_errors(schema: Mapping[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        if key in schema and not _valid_number(schema[key]):
            errors.append(f"{path}: {key} must be a number")
    return errors


def _valid_number(value: Any) -> bool:
    if type(value) is int:
        return True
    return type(value) is float and math.isfinite(value)


def _array_schema_errors(schema: Mapping[str, Any], path: str) -> list[str]:
    return [
        *_length_schema_errors(schema, path),
        *_unique_items_schema_errors(schema, path),
        *_pattern_schema_errors(schema, path),
    ]


def _length_schema_errors(schema: Mapping[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in schema and not _valid_length(schema[key]):
            errors.append(f"{path}: {key} must be a non-negative integer")
    return errors


def _valid_length(value: Any) -> bool:
    return type(value) is int and value >= 0


def _unique_items_schema_errors(schema: Mapping[str, Any], path: str) -> list[str]:
    if "uniqueItems" in schema and not isinstance(schema["uniqueItems"], bool):
        return [f"{path}: uniqueItems must be boolean"]
    return []


def _pattern_schema_errors(schema: Mapping[str, Any], path: str) -> list[str]:
    pattern = schema.get("pattern")
    if pattern is None:
        return []
    if not isinstance(pattern, str):
        return [f"{path}: pattern must be a string"]
    try:
        re.compile(pattern)
    except re.error:
        return [f"{path}: pattern must be a valid regular expression"]
    return []


def _schema_child_errors(schema: Mapping[str, Any], path: str, seen: set[int]) -> list[str]:
    return [
        *_property_schema_errors(schema.get("properties"), path, seen),
        *_additional_schema_errors(schema.get("additionalProperties"), path, seen),
        *_items_schema_errors(schema, path, seen),
        *_schema_alternative_errors(schema, path, seen),
    ]


def _property_schema_errors(properties: Any, path: str, seen: set[int]) -> list[str]:
    if not isinstance(properties, Mapping):
        return []
    errors: list[str] = []
    for name, child in properties.items():
        errors.extend(_property_schema_error(name, child, path, seen))
    return errors


def _property_schema_error(name: Any, child: Any, path: str, seen: set[int]) -> list[str]:
    if not isinstance(name, str):
        return [f"{path}: property names must be strings"]
    if not isinstance(child, Mapping):
        return [f"{path}.properties.{name}: property schema must be an object"]
    return _schema_errors(child, f"{path}.properties.{name}", seen)


def _additional_schema_errors(additional: Any, path: str, seen: set[int]) -> list[str]:
    if not isinstance(additional, Mapping):
        return []
    return _schema_errors(additional, f"{path}.additionalProperties", seen)


def _items_schema_errors(schema: Mapping[str, Any], path: str, seen: set[int]) -> list[str]:
    if isinstance(schema.get("items"), Mapping):
        return _schema_errors(schema["items"], f"{path}.items", seen)
    if "items" in schema:
        return [f"{path}.items: item schema must be an object"]
    return []


def _schema_alternative_errors(schema: Mapping[str, Any], path: str, seen: set[int]) -> list[str]:
    errors: list[str] = []
    for keyword in ("allOf", "anyOf", "oneOf"):
        errors.extend(_alternative_schema_errors(schema, keyword, path, seen))
    return errors


def _alternative_schema_errors(
    schema: Mapping[str, Any], keyword: str, path: str, seen: set[int]
) -> list[str]:
    alternatives = schema.get(keyword)
    if alternatives is None:
        return []
    if not isinstance(alternatives, list):
        return [f"{path}: {keyword} must be an array of schemas"]
    errors: list[str] = []
    for index, child in enumerate(alternatives):
        if not isinstance(child, Mapping):
            errors.append(f"{path}.{keyword}[{index}]: alternative must be an object")
        else:
            errors.extend(_schema_errors(child, f"{path}.{keyword}[{index}]", seen))
    return errors


def _instance_errors(instance: Any, schema: Mapping[str, Any], path: str) -> list[str]:
    errors = _composition_errors(instance, schema, path)
    errors.extend(_type_errors(instance, schema, path))
    if errors:
        return errors
    errors.extend(_value_errors(instance, schema, path))
    return errors


def _composition_errors(instance: Any, schema: Mapping[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for child in all_of:
            errors.extend(_instance_errors(instance, child, path))
    errors.extend(_alternative_errors(instance, schema, path, "anyOf", _at_least_one))
    errors.extend(_alternative_errors(instance, schema, path, "oneOf", _exactly_one))
    return errors


def _alternative_errors(
    instance: Any,
    schema: Mapping[str, Any],
    path: str,
    keyword: str,
    predicate: Any,
) -> list[str]:
    alternatives = schema.get(keyword)
    if not isinstance(alternatives, list):
        return []
    matches = sum(not _instance_errors(instance, child, path) for child in alternatives)
    if predicate(matches):
        return []
    return [f"{path}: {keyword} alternatives do not match exactly"]


def _exactly_one(matches: int) -> bool:
    return matches == 1


def _at_least_one(matches: int) -> bool:
    return matches > 0


def _type_errors(instance: Any, schema: Mapping[str, Any], path: str) -> list[str]:
    schema_type = schema.get("type")
    if schema_type is None:
        return []
    accepted = schema_type if isinstance(schema_type, list) else [schema_type]
    if any(_matches_type(instance, item) for item in accepted):
        return []
    return [f"{path}: value does not match type {schema_type!r}"]


def _matches_type(value: Any, schema_type: str) -> bool:
    matcher = _TYPE_MATCHERS.get(schema_type)
    if matcher is None:
        return False
    return matcher(value)


def _is_null(value: Any) -> bool:
    return value is None


def _is_boolean(value: Any) -> bool:
    return type(value) is bool


def _is_integer(value: Any) -> bool:
    return type(value) is int


def _is_number(value: Any) -> bool:
    return _valid_number(value)


def _is_object(value: Any) -> bool:
    return isinstance(value, Mapping)


def _is_array(value: Any) -> bool:
    return isinstance(value, list)


def _is_string(value: Any) -> bool:
    return isinstance(value, str)


_TYPE_MATCHERS = {
    "null": _is_null,
    "boolean": _is_boolean,
    "integer": _is_integer,
    "number": _is_number,
    "object": _is_object,
    "array": _is_array,
    "string": _is_string,
}


def _value_errors(instance: Any, schema: Mapping[str, Any], path: str) -> list[str]:
    errors = _enum_errors(instance, schema, path)
    errors.extend(_number_errors(instance, schema, path))
    errors.extend(_string_errors(instance, schema, path))
    errors.extend(_object_errors(instance, schema, path))
    errors.extend(_items_errors(instance, schema, path))
    return errors


def _enum_errors(instance: Any, schema: Mapping[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: value does not match const")
    enum = schema.get("enum")
    if enum is not None and not any(instance == item for item in enum):
        errors.append(f"{path}: value is not in enum")
    return errors


def _number_errors(instance: Any, schema: Mapping[str, Any], path: str) -> list[str]:
    if type(instance) not in (int, float):
        return []
    lower_bounds = (
        ("minimum", lambda a, b: a < b),
        ("exclusiveMinimum", lambda a, b: a <= b),
    )
    upper_bounds = (
        ("maximum", lambda a, b: a > b),
        ("exclusiveMaximum", lambda a, b: a >= b),
    )
    return [
        *_number_bound_errors(instance, schema, path, lower_bounds, "below"),
        *_number_bound_errors(instance, schema, path, upper_bounds, "above"),
    ]


def _number_bound_errors(
    instance: Any,
    schema: Mapping[str, Any],
    path: str,
    bounds: tuple[tuple[str, Any], ...],
    relation: str,
) -> list[str]:
    errors: list[str] = []
    for key, operator in bounds:
        if key in schema and operator(instance, schema[key]):
            errors.append(f"{path}: value is {relation} {key}")
    return errors


def _string_errors(instance: Any, schema: Mapping[str, Any], path: str) -> list[str]:
    if not isinstance(instance, str):
        return []
    return [
        *_string_length_errors(instance, schema, path),
        *_string_pattern_errors(instance, schema, path),
    ]


def _string_length_errors(instance: str, schema: Mapping[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    if len(instance) < schema.get("minLength", 0):
        errors.append(f"{path}: string is shorter than minLength")
    if "maxLength" in schema and len(instance) > schema["maxLength"]:
        errors.append(f"{path}: string is longer than maxLength")
    return errors


def _string_pattern_errors(instance: str, schema: Mapping[str, Any], path: str) -> list[str]:
    pattern = schema.get("pattern")
    if pattern is None or re.search(pattern, instance) is not None:
        return []
    return [f"{path}: string does not match pattern"]


def _object_errors(instance: Any, schema: Mapping[str, Any], path: str) -> list[str]:
    if not isinstance(instance, Mapping):
        return []
    return [
        *_required_value_errors(instance, schema, path),
        *_property_value_errors(instance, schema, path),
    ]


def _required_value_errors(
    instance: Mapping[str, Any], schema: Mapping[str, Any], path: str
) -> list[str]:
    errors: list[str] = []
    for name in schema.get("required", []):
        if name not in instance:
            errors.append(f"{path}: required property {name!r} is missing")
    return errors


def _property_value_errors(
    instance: Mapping[str, Any], schema: Mapping[str, Any], path: str
) -> list[str]:
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return []
    errors: list[str] = []
    for name, value in instance.items():
        errors.extend(_property_value_error(name, value, properties, schema, path))
    return errors


def _property_value_error(
    name: Any,
    value: Any,
    properties: Mapping[str, Any],
    schema: Mapping[str, Any],
    path: str,
) -> list[str]:
    if name in properties:
        return _instance_errors(value, properties[name], f"{path}.{name}")
    return _additional_property_errors(name, value, schema, path)


def _items_errors(instance: Any, schema: Mapping[str, Any], path: str) -> list[str]:
    if not isinstance(instance, list):
        return []
    return [
        *_array_limit_errors(instance, schema, path),
        *_unique_items_value_errors(instance, schema, path),
        *_item_schema_value_errors(instance, schema, path),
    ]


def _array_limit_errors(instance: list[Any], schema: Mapping[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    if len(instance) < schema.get("minItems", 0):
        errors.append(f"{path}: array is shorter than minItems")
    if "maxItems" in schema and len(instance) > schema["maxItems"]:
        errors.append(f"{path}: array is longer than maxItems")
    return errors


def _unique_items_value_errors(
    instance: list[Any], schema: Mapping[str, Any], path: str
) -> list[str]:
    if not schema.get("uniqueItems", False):
        return []
    if len({repr(item) for item in instance}) == len(instance):
        return []
    return [f"{path}: array items must be unique"]


def _item_schema_value_errors(
    instance: list[Any], schema: Mapping[str, Any], path: str
) -> list[str]:
    items = schema.get("items")
    if not isinstance(items, Mapping):
        return []
    errors: list[str] = []
    for index, value in enumerate(instance):
        errors.extend(_instance_errors(value, items, f"{path}[{index}]"))
    return errors


def _additional_property_errors(
    name: Any, value: Any, schema: Mapping[str, Any], path: str
) -> list[str]:
    additional = schema.get("additionalProperties", True)
    if additional is False:
        return [f"{path}: additional property {name!r} is not allowed"]
    if isinstance(additional, Mapping):
        return _instance_errors(value, additional, f"{path}.{name}")
    return []


__all__ = ["validate_instance", "validate_schema"]
