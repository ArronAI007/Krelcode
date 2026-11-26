"""零依赖 JSON Schema 校验（工具参数执行前的安全边界）。

覆盖主流 function calling schema 所需子集：`$ref`/`$defs`、type、enum、
const、object properties/required/additionalProperties/patternProperties、
array items/minItems/maxItems/uniqueItems、string/numeric 约束，以及
allOf/anyOf/oneOf/not。未知关键字忽略，符合 JSON Schema 的前向兼容语义。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence


@dataclass(frozen=True)
class SchemaValidationError:
    path: str
    message: str

    def render(self) -> str:
        return f"{self.path}: {self.message}"


def validate_tool_arguments(schema: Any, arguments: Any) -> list[SchemaValidationError]:
    """校验工具参数；返回空列表表示通过。"""
    if not isinstance(schema, (Mapping, bool)):
        return [SchemaValidationError(
            "$", "工具 JSON Schema 本身必须是 object 或 boolean")]
    normalized_schema = dict(schema) if isinstance(schema, Mapping) else schema
    root = normalized_schema if isinstance(normalized_schema, dict) else {}
    if not isinstance(arguments, dict):
        return [SchemaValidationError("$", "工具参数必须是 JSON object")]
    return _validate(arguments, normalized_schema, root, "$", depth=0)


def _validate(
    value: Any,
    schema: Any,
    root: Mapping[str, Any],
    path: str,
    *,
    depth: int,
) -> list[SchemaValidationError]:
    if depth > 64:
        return [SchemaValidationError(path, "JSON Schema 引用层级过深")]
    if schema is True:
        return []
    if schema is False:
        return [SchemaValidationError(path, "不允许任何值")]
    if not isinstance(schema, dict):
        return []

    errors: list[SchemaValidationError] = []
    ref = schema.get("$ref")
    if isinstance(ref, str):
        target = _resolve_ref(root, ref)
        if target is None:
            return [SchemaValidationError(path, f"无法解析 $ref: {ref}")]
        errors.extend(_validate(
            value, target, root, path, depth=depth + 1))

    expected = schema.get("type")
    if expected is not None:
        expected_types = (
            [expected] if isinstance(expected, str) else list(expected or []))
        if not any(_matches_type(value, item) for item in expected_types):
            errors.append(SchemaValidationError(
                path, f"类型应为 {'/'.join(map(str, expected_types))}，"
                      f"实际为 {_json_type(value)}"))
            # 后续约束依赖类型，避免产生无意义的级联错误。
            return errors

    if "enum" in schema and not any(
            _json_equal(value, option) for option in schema.get("enum") or []):
        errors.append(SchemaValidationError(
            path, f"值必须是枚举之一: {schema.get('enum')!r}"))
    if "const" in schema and not _json_equal(value, schema["const"]):
        errors.append(SchemaValidationError(
            path, f"值必须等于常量 {schema['const']!r}"))

    if isinstance(value, dict):
        errors.extend(_validate_object(
            value, schema, root, path, depth=depth))
    elif isinstance(value, list):
        errors.extend(_validate_array(
            value, schema, root, path, depth=depth))
    elif isinstance(value, str):
        errors.extend(_validate_string(value, schema, path))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        errors.extend(_validate_number(value, schema, path))

    for index, child in enumerate(schema.get("allOf") or []):
        errors.extend(_validate(value, child, root, path, depth=depth + 1))
    any_of = schema.get("anyOf")
    if isinstance(any_of, list) and not any(
            not _validate(value, child, root, path, depth=depth + 1)
            for child in any_of):
        errors.append(SchemaValidationError(path, "不满足 anyOf 中任一分支"))
    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        matched = sum(
            not _validate(value, child, root, path, depth=depth + 1)
            for child in one_of)
        if matched != 1:
            errors.append(SchemaValidationError(
                path, f"oneOf 必须且只能匹配一个分支，实际匹配 {matched} 个"))
    if "not" in schema and not _validate(
            value, schema["not"], root, path, depth=depth + 1):
        errors.append(SchemaValidationError(path, "命中了 not 禁止的 schema"))
    return errors


def _validate_object(
    value: Dict[str, Any],
    schema: Mapping[str, Any],
    root: Mapping[str, Any],
    path: str,
    *,
    depth: int,
) -> list[SchemaValidationError]:
    errors: list[SchemaValidationError] = []
    properties = schema.get("properties") or {}
    properties = properties if isinstance(properties, dict) else {}
    patterns = schema.get("patternProperties") or {}
    patterns = patterns if isinstance(patterns, dict) else {}

    required = schema.get("required") or []
    for name in required:
        if name not in value:
            errors.append(SchemaValidationError(
                f"{path}.{name}", "缺少必填字段"))

    for name, child_schema in properties.items():
        if name in value:
            errors.extend(_validate(
                value[name], child_schema, root, f"{path}.{name}",
                depth=depth + 1,
            ))

    matched_patterns: set[str] = set()
    for name, item in value.items():
        for pattern, child_schema in patterns.items():
            try:
                matched = re.search(pattern, name) is not None
            except re.error:
                continue
            if matched:
                matched_patterns.add(name)
                errors.extend(_validate(
                    item, child_schema, root, f"{path}.{name}",
                    depth=depth + 1,
                ))
        if name in properties or name in matched_patterns:
            continue
        additional = schema.get("additionalProperties", True)
        if additional is False:
            errors.append(SchemaValidationError(
                f"{path}.{name}", "不允许的额外字段"))
        elif isinstance(additional, dict):
            errors.extend(_validate(
                item, additional, root, f"{path}.{name}",
                depth=depth + 1,
            ))

    if isinstance(schema.get("minProperties"), int) and len(value) < schema["minProperties"]:
        errors.append(SchemaValidationError(
            path, f"字段数不能少于 {schema['minProperties']}"))
    if isinstance(schema.get("maxProperties"), int) and len(value) > schema["maxProperties"]:
        errors.append(SchemaValidationError(
            path, f"字段数不能多于 {schema['maxProperties']}"))
    return errors


def _validate_array(
    value: Sequence[Any],
    schema: Mapping[str, Any],
    root: Mapping[str, Any],
    path: str,
    *,
    depth: int,
) -> list[SchemaValidationError]:
    errors: list[SchemaValidationError] = []
    items = schema.get("items")
    if isinstance(items, (dict, bool)):
        for index, item in enumerate(value):
            errors.extend(_validate(
                item, items, root, f"{path}[{index}]", depth=depth + 1))
    elif isinstance(items, list):
        for index, child_schema in enumerate(items[:len(value)]):
            errors.extend(_validate(
                value[index], child_schema, root, f"{path}[{index}]",
                depth=depth + 1,
            ))
    if isinstance(schema.get("minItems"), int) and len(value) < schema["minItems"]:
        errors.append(SchemaValidationError(
            path, f"数组元素不能少于 {schema['minItems']}"))
    if isinstance(schema.get("maxItems"), int) and len(value) > schema["maxItems"]:
        errors.append(SchemaValidationError(
            path, f"数组元素不能多于 {schema['maxItems']}"))
    if schema.get("uniqueItems"):
        for index, item in enumerate(value):
            if any(_json_equal(item, previous) for previous in value[:index]):
                errors.append(SchemaValidationError(
                    f"{path}[{index}]", "数组元素必须唯一"))
    return errors


def _validate_string(
    value: str,
    schema: Mapping[str, Any],
    path: str,
) -> list[SchemaValidationError]:
    errors: list[SchemaValidationError] = []
    if isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
        errors.append(SchemaValidationError(
            path, f"字符串长度不能少于 {schema['minLength']}"))
    if isinstance(schema.get("maxLength"), int) and len(value) > schema["maxLength"]:
        errors.append(SchemaValidationError(
            path, f"字符串长度不能多于 {schema['maxLength']}"))
    pattern = schema.get("pattern")
    if isinstance(pattern, str):
        try:
            if re.search(pattern, value) is None:
                errors.append(SchemaValidationError(
                    path, f"字符串不匹配模式 {pattern!r}"))
        except re.error as exc:
            errors.append(SchemaValidationError(path, f"无效 pattern: {exc}"))
    return errors


def _validate_number(
    value: float,
    schema: Mapping[str, Any],
    path: str,
) -> list[SchemaValidationError]:
    errors: list[SchemaValidationError] = []
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    exclusive_minimum = schema.get("exclusiveMinimum")
    exclusive_maximum = schema.get("exclusiveMaximum")
    if isinstance(minimum, (int, float)) and value < minimum:
        errors.append(SchemaValidationError(path, f"数值不能小于 {minimum}"))
    if isinstance(maximum, (int, float)) and value > maximum:
        errors.append(SchemaValidationError(path, f"数值不能大于 {maximum}"))
    if isinstance(exclusive_minimum, (int, float)) and value <= exclusive_minimum:
        errors.append(SchemaValidationError(
            path, f"数值必须大于 {exclusive_minimum}"))
    if isinstance(exclusive_maximum, (int, float)) and value >= exclusive_maximum:
        errors.append(SchemaValidationError(
            path, f"数值必须小于 {exclusive_maximum}"))
    multiple = schema.get("multipleOf")
    if isinstance(multiple, (int, float)) and multiple > 0:
        quotient = value / multiple
        if not math.isclose(quotient, round(quotient), rel_tol=0, abs_tol=1e-9):
            errors.append(SchemaValidationError(
                path, f"数值必须是 {multiple} 的整数倍"))
    return errors


def _matches_type(value: Any, expected: Any) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


def _json_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    return bool(left == right)


def _resolve_ref(root: Mapping[str, Any], ref: str) -> Optional[Any]:
    if not ref.startswith("#/"):
        return None
    current: Any = root
    for part in ref[2:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current
