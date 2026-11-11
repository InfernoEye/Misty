"""Strict parsing and validation of model-generated tool calls."""

import json


def parse_tool_arguments(raw_arguments):
    if isinstance(raw_arguments, dict):
        return raw_arguments

    if not isinstance(raw_arguments, str):
        raise ValueError("Tool arguments must be a JSON object.")

    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError as error:
        raise ValueError("Tool arguments contain invalid JSON.") from error

    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must decode to an object.")

    return arguments
