"""A tiny JSON Schema subset validator, enough for the tool input schemas in this server.

Supports: type (string, integer, boolean, object), enum, minimum, maximum, minLength, maxLength,
required and additionalProperties: false. Anything else in a schema is ignored.
"""


def _type_ok(value, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    return True


def validate(arguments, schema: dict) -> list:
    """Return a list of human-readable problems; empty means valid."""
    if not isinstance(arguments, dict):
        return ["arguments must be an object"]
    problems = []
    properties = schema.get("properties", {})
    for name in schema.get("required", []):
        if name not in arguments:
            problems.append(f"missing required argument: {name}")
    for name, value in arguments.items():
        spec = properties.get(name)
        if spec is None:
            if schema.get("additionalProperties") is False:
                problems.append(f"unexpected argument: {name}")
            continue
        if "type" in spec and not _type_ok(value, spec["type"]):
            problems.append(f"{name} must be of type {spec['type']}")
            continue
        if "enum" in spec and value not in spec["enum"]:
            problems.append(f"{name} must be one of {spec['enum']}")
        if isinstance(value, int) and not isinstance(value, bool):
            if "minimum" in spec and value < spec["minimum"]:
                problems.append(f"{name} must be >= {spec['minimum']}")
            if "maximum" in spec and value > spec["maximum"]:
                problems.append(f"{name} must be <= {spec['maximum']}")
        if isinstance(value, str):
            if "minLength" in spec and len(value) < spec["minLength"]:
                problems.append(f"{name} must be at least {spec['minLength']} characters")
            if "maxLength" in spec and len(value) > spec["maxLength"]:
                problems.append(f"{name} must be at most {spec['maxLength']} characters")
    return problems
