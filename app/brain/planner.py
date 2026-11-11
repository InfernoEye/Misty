"""Safe, deterministic task-plan validation and execution tracking."""

from dataclasses import dataclass, field
import json
import re

from app.brain.validator import parse_tool_arguments


MAX_PLAN_STEPS = 12

# -- Observation-driven continuation: result references ---------------------
# A string argument may contain at most one reference of the form
#   $steps.<step_id>.result.<field>
# (optionally followed by dotted sub-fields).  ``<step_id>`` must be a step
# declared earlier in the same plan *and* listed in the referring step's
# ``depends_on``.  The reference is replaced with the scalar found at that
# path of the referenced step's result immediately before the tool runs.
REFERENCE_MARKER = "$steps."
MAX_RESULT_PATH_DEPTH = 4
_MAX_SUMMARY_EXCERPT = 400

_STEP_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
_FIELD_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
#: A list index inside a result reference ("paths.0").
_INDEX_PATTERN = re.compile(r"[0-9]{1,3}")
# Character whitelist for any argument string that carries a reference:
# printable ASCII without backslashes, quotes or braces.
_REFERENCE_HOST_PATTERN = re.compile(r"[\x20-\x7e]+")
_FORBIDDEN_HOST_CHARACTERS = frozenset("\\\"'{}")

_SCALAR_TYPES = {"string", "integer", "number", "boolean"}
_SPECIAL_JSON = "special-json"


@dataclass
class PlanStep:
    call_id: str
    name: str
    arguments: dict
    description: str
    status: str = "pending"
    result: dict | None = None
    depends_on: list[str] = field(default_factory=list)
    reason: str = ""
    # 1-based declaration order in the plan; a result reference may only
    # point to a step with a smaller position.
    position: int = 0


@dataclass
class TaskPlan:
    steps: list[PlanStep] = field(default_factory=list)
    status: str = "ready"
    intent_confidence: float = 1.0
    clarification: str = ""

    @property
    def completed(self):
        return sum(step.status == "success" for step in self.steps)

    @property
    def failed(self):
        return sum(step.status == "failed" for step in self.steps)

    @property
    def skipped(self):
        return sum(step.status == "skipped" for step in self.steps)

    @property
    def blocked(self):
        return sum(step.status == "blocked" for step in self.steps)


class PlanValidationError(ValueError):
    pass


class ResultReferenceError(RuntimeError):
    """A result reference could not be resolved; the step must not run."""


class _ResultReference:
    """One parsed ``$steps.<id>.result.<field>`` reference inside an argument."""

    __slots__ = ("step_id", "path", "start", "end", "text")

    def __init__(self, step_id, path, start, end, text):
        self.step_id = step_id
        self.path = tuple(path)
        self.start = start
        self.end = end
        self.text = text

    @property
    def display(self):
        return f"$steps.{self.step_id}.result." + ".".join(self.path)


def _contains_reference(value):
    if isinstance(value, str):
        return REFERENCE_MARKER in value
    if isinstance(value, dict):
        return any(
            _contains_reference(item) for item in value.values()
        )
    if isinstance(value, list):
        return any(_contains_reference(item) for item in value)
    return False


def _parse_reference_token(token):
    """Parse the text after ``$steps.`` into ``(step_id, path)``.

    Returns None when the token does not have the exact
    ``<id>.result.<field>[.<field>...]`` shape with whitelisted characters.
    """
    parts = token.split(".")
    if len(parts) < 3 or parts[1] != "result":
        return None
    step_id = parts[0]
    path = parts[2:]
    if _STEP_ID_PATTERN.fullmatch(step_id) is None:
        return None
    if len(path) > MAX_RESULT_PATH_DEPTH:
        return None
    # A field that is private or a dunder is never a result value: such a
    # token is not a reference at all (no attribute access, no coercion).
    previous_was_field = False
    for segment in path:
        if _INDEX_PATTERN.fullmatch(segment) is not None:
            # "paths.0" means item 0 of the list in ``paths``. An index may
            # not follow another index (no nested list indexing).
            if not previous_was_field:
                return None
            previous_was_field = False
            continue
        if _FIELD_PATTERN.fullmatch(segment) is None:
            return None
        previous_was_field = True
    return step_id, path


# A reference token never contains whitespace; a second "$steps." starts a
# new token, and delimiters such as "/" or "," separate tokens.
_REFERENCE_TOKEN = re.compile(
    re.escape(REFERENCE_MARKER)
    + r"([A-Za-z0-9_-]{1,64}\.result"
    r"(?:\.[A-Za-z_][A-Za-z0-9_]{0,63}|\.[0-9]{1,3}){1,5})"
)


def find_result_references(value):
    """Return every well-formed reference found in a string or nested value.

    Each token is validated independently (step ID, the literal ``result``
    segment, at most :data:`MAX_RESULT_PATH_DEPTH` whitelisted fields), so a
    malformed or hostile token such as ``$steps.x.__class__`` is simply not a
    reference and is never traversed.
    """
    found = []
    if isinstance(value, str):
        for match in _REFERENCE_TOKEN.finditer(value):
            token = match.group(0)
            parsed = _parse_reference_token(token[len(REFERENCE_MARKER):])
            if parsed is None:
                continue
            found.append(_ResultReference(
                parsed[0],
                parsed[1],
                match.start(),
                match.end(),
                token,
            ))
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(find_result_references(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(find_result_references(item))
    return found


def parse_reference(value):
    """Return the single reference carried by ``value``, or None.

    ``value`` must be exactly one ``$steps.<id>.result.<field>`` token with
    no surrounding text.  Anything else (no reference, several references,
    extra text, disallowed or private field characters) returns None.
    """
    if not isinstance(value, str) or not value.startswith(REFERENCE_MARKER):
        return None
    parsed = _parse_reference_token(value[len(REFERENCE_MARKER):])
    if parsed is None:
        return None
    step_id, path = parsed
    return _ResultReference(step_id, path, 0, len(value), value)


def _summarize_argument_value(value):
    """Return a short, content-free description of an argument value."""
    if isinstance(value, str):
        if len(value) <= _MAX_SUMMARY_EXCERPT:
            return json.dumps(value, ensure_ascii=False)
        return f"{len(value)} characters of text"
    if isinstance(value, (dict, list)):
        return f"{type(value).__name__} with {len(value)} item(s)"
    return type(value).__name__


class TaskPlanner:
    """Validates proposed tool batches and tracks their execution."""

    def __init__(self, max_steps=MAX_PLAN_STEPS):
        if not isinstance(max_steps, int) or not 1 <= max_steps <= 50:
            raise ValueError("max_steps must be between 1 and 50.")
        self.max_steps = max_steps

    @staticmethod
    def _value_type(value, schema):
        """Schema type of ``value`` (base type for an enum naming a type)."""
        expected = schema.get("type")
        if expected in _SCALAR_TYPES | {"object", "array"}:
            return expected
        # A schema without a type that enumerates a value of another JSON
        # type (e.g. enum ["Home", ...]) still describes that type.
        for candidate in (
            "integer", "boolean", "string", "number", "object", "array"
        ):
            if candidate in schema.get("enum", ()):
                return candidate
        # No declared or implied type: special JSON values (null) are left to
        # the registry's own argument checks.
        return _SPECIAL_JSON

    @staticmethod
    def _validate_type(value, expected, schema, path):
        """Type, bounds and enum checks for one value of a known type."""
        if expected == "object":
            if not isinstance(value, dict):
                raise PlanValidationError(f"{path} must be an object.")

            required = schema.get("required", [])
            for key in required:
                if key not in value:
                    raise PlanValidationError(
                        f"{path} is missing required argument '{key}'."
                    )

            properties = schema.get("properties", {})
            if schema.get("additionalProperties") is False:
                extra = set(value) - set(properties)
                if extra:
                    raise PlanValidationError(
                        f"{path} contains unknown argument(s): "
                        + ", ".join(sorted(extra))
                    )

        elif expected == "string":
            if not isinstance(value, str):
                raise PlanValidationError(f"{path} must be text.")

        elif expected == "integer":
            if not isinstance(value, int) or isinstance(value, bool):
                raise PlanValidationError(f"{path} must be an integer.")

        elif expected == "number":
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
            ):
                raise PlanValidationError(f"{path} must be a number.")

        elif expected == "boolean":
            if not isinstance(value, bool):
                raise PlanValidationError(f"{path} must be true or false.")

        elif expected == "array":
            if not isinstance(value, list):
                raise PlanValidationError(f"{path} must be a list.")

        if expected in {"integer", "number"}:
            if "minimum" in schema and value < schema["minimum"]:
                raise PlanValidationError(
                    f"{path} must be at least {schema['minimum']}."
                )
            if "maximum" in schema and value > schema["maximum"]:
                raise PlanValidationError(
                    f"{path} must be at most {schema['maximum']}."
                )

        if expected == "string":
            if "minLength" in schema and len(value) < schema["minLength"]:
                raise PlanValidationError(
                    f"{path} is too short."
                )
            if "maxLength" in schema and len(value) > schema["maxLength"]:
                raise PlanValidationError(
                    f"{path} is too long."
                )

        if expected == "array":
            if "minItems" in schema and len(value) < schema["minItems"]:
                raise PlanValidationError(
                    f"{path} contains too few items."
                )
            if "maxItems" in schema and len(value) > schema["maxItems"]:
                raise PlanValidationError(
                    f"{path} contains too many items."
                )

        if "enum" in schema and value not in schema["enum"]:
            choices = ", ".join(str(item) for item in schema["enum"])
            raise PlanValidationError(
                f"{path} must be one of: {choices}."
            )

    @staticmethod
    def _validate_value(value, schema, path, references=None, seen=None):
        """Validate a value, recording result references instead of running.

        For a string argument that contains references, the literal template
        is type-checked only when the schema names a string type; a reference
        may target a scalar of a different type, so literal bounds and enums
        do not apply to templates.  ``references`` receives one
        ``_ResultReference`` per substituted argument, and ``seen`` (a dict
        keyed by ``id(reference)``) records the argument path of each one.
        """
        expected = TaskPlanner._value_type(value, schema)

        if (
            references is not None
            and isinstance(value, str)
            and REFERENCE_MARKER in value
        ):
            # Any "$steps." template enters reference handling: malformed
            # templates, several references in one argument, and templates
            # filling non-scalar arguments are rejected below instead of
            # slipping through as ordinary strings.
            if expected not in {
                None, _SPECIAL_JSON, "string",
                "integer", "number", "boolean",
            }:
                raise PlanValidationError(
                    f"{path} must be a {expected} value, but it is a "
                    "result reference (a reference can only fill a "
                    "text, number or boolean argument)."
                )
            found = find_result_references(value)
            if not found:
                raise PlanValidationError(
                    f"{path} contains an invalid result reference; expected "
                    "$steps.<step_id>.result.<field> with lower-case, "
                    "digit, underscore or dash characters only."
                )
            if len(found) > 1:
                raise PlanValidationError(
                    f"{path} contains {len(found)} result references; at "
                    "most one is allowed per argument."
                )
            if len(found[0].text) != len(value):
                raise PlanValidationError(
                    f"{path} must be exactly one result reference with no "
                    "surrounding text."
                )
            references.append(found[0])
            seen[id(found[0])] = path
            return

        if expected == "object" and isinstance(value, dict):
            TaskPlanner._validate_type(value, expected, schema, path)
            properties = schema.get("properties", {})
            for key, item in value.items():
                if key in properties:
                    TaskPlanner._validate_value(
                        item,
                        properties[key],
                        f"{path}.{key}",
                        references,
                        seen,
                    )
            return

        if expected == "array" and isinstance(value, list):
            TaskPlanner._validate_type(value, expected, schema, path)
            item_schema = schema.get("items")
            if item_schema:
                for index, item in enumerate(value):
                    TaskPlanner._validate_value(
                        item,
                        item_schema,
                        f"{path}[{index}]",
                        references,
                        seen,
                    )
            return

        TaskPlanner._validate_type(value, expected, schema, path)

    def _resolve_references(
        self, references, seen, step, by_id, position
    ):
        """Check that every reference targets an earlier declared dependency."""
        for reference in references:
            where = seen.get(id(reference), "an argument")
            if reference.step_id not in by_id:
                raise PlanValidationError(
                    f"{where} references unknown step "
                    f"'{reference.step_id}'."
                )
            if reference.step_id not in step.depends_on:
                raise PlanValidationError(
                    f"{where} references step '{reference.step_id}', but "
                    "'depends_on' does not list it. A reference requires an "
                    "explicit dependency."
                )
            if by_id[reference.step_id].position >= position:
                raise PlanValidationError(
                    f"{where} references step '{reference.step_id}', which "
                    "is not declared earlier in the plan. A result reference "
                    "may only use an earlier step."
                )

    @staticmethod
    def _quote_blocks_second_plan(step, plan):
        """True when a step carries text quoted from an earlier result.

        Continuation planning is bounded to ONE automatic round.  If any
        result string returned by an earlier step of the same plan appears
        verbatim (at least eight characters) inside this step's arguments,
        the model is reasoning about fresh observations, i.e. proposing
        another continuation round.  Such rounds are rejected deterministically.
        """
        texts = []
        for earlier in plan.steps:
            if earlier.status != "success" or not isinstance(
                earlier.result, dict
            ):
                continue
            for value in earlier.result.values():
                if isinstance(value, str) and len(value) >= 8:
                    texts.append(value[:_MAX_SUMMARY_EXCERPT])
        if not texts:
            return False
        candidates = []

        def collect(value):
            if isinstance(value, str):
                candidates.append(value)
            elif isinstance(value, dict):
                for item in value.values():
                    collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)

        collect(step.arguments)
        for candidate in candidates:
            for text in texts:
                if text in candidate:
                    return True
        return False

    def unresolved_references(self, plan, index):
        """Return the reference step IDs a step waits for before substitution.

        ``substitute_references`` must only run once those steps succeeded
        (normally guaranteed by ``depends_on`` scheduling).
        """
        step = plan.steps[index]
        return tuple(sorted({
            reference.step_id
            for reference in find_result_references(step.arguments)
        }))

    def substitute_references(
        self, plan, index, by_id=None
    ):
        """Return a copy of ``plan.steps[index].arguments`` with references filled.

        ``by_id`` may map call IDs to arbitrary objects exposing ``status`` and
        ``result`` (used by a combined execution report).  Raises
        :class:`ResultReferenceError` when a reference cannot be resolved
        unambiguously to one scalar; the caller must then fail the step closed.
        """
        step = plan.steps[index]
        if by_id is None:
            by_id = {item.call_id: item for item in plan.steps}

        def resolve(reference):
            target = by_id.get(reference.step_id)
            if target is None:
                raise ResultReferenceError(
                    f"Result reference {reference.display} points to an "
                    "unknown step."
                )
            if target.status != "success" or target.result is None:
                raise ResultReferenceError(
                    f"Result reference {reference.display} needs step "
                    f"'{reference.step_id}', which did not succeed."
                )
            value = target.result
            if not isinstance(value, dict):
                raise ResultReferenceError(
                    f"Result reference {reference.display} targets a result "
                    "that is not an object."
                )
            walked = []
            for segment in reference.path:
                walked.append(segment)
                if _INDEX_PATTERN.fullmatch(segment) is not None:
                    # Bounded list read: never negative, never out of range.
                    index = int(segment)
                    if not isinstance(value, (list, tuple)) or not (
                        0 <= index < len(value)
                    ):
                        raise ResultReferenceError(
                            f"Result reference {reference.display} has no "
                            f"item {index} in '" + ".".join(walked[:-1]) + "'."
                        )
                    value = value[index]
                    continue
                if not isinstance(value, dict) or segment not in value:
                    raise ResultReferenceError(
                        f"Result reference {reference.display} is missing "
                        "field '" + ".".join(walked) + "'."
                    )
                value = value[segment]
            if isinstance(value, bool) or not isinstance(
                value, (str, int, float)
            ):
                raise ResultReferenceError(
                    f"Result reference {reference.display} must resolve to "
                    "one text or number value."
                )
            if isinstance(value, str):
                if len(value) > _MAX_SUMMARY_EXCERPT:
                    raise ResultReferenceError(
                        f"Result reference {reference.display} is too long "
                        f"to pass as an argument (limit "
                        f"{_MAX_SUMMARY_EXCERPT} characters)."
                    )
                if any(
                    ord(character) < 32 or ord(character) == 127
                    for character in value
                ):
                    raise ResultReferenceError(
                        f"Result reference {reference.display} contains "
                        "control characters."
                    )
                return value
            return str(value)

        def substitute(value):
            if isinstance(value, str) and REFERENCE_MARKER in value:
                found = find_result_references(value)
                if len(found) != 1 or len(found[0].text) != len(value):
                    raise ResultReferenceError(
                        "An argument contains a malformed result reference."
                    )
                return resolve(found[0])
            if isinstance(value, dict):
                return {key: substitute(item) for key, item in value.items()}
            if isinstance(value, list):
                return [substitute(item) for item in value]
            return value

        return substitute({key: item for key, item in step.arguments.items()})

    def prepare(self, tool_calls, tool_definitions):
        """Validate a complete tool-call batch before any action is executed."""
        if not isinstance(tool_calls, list) or not tool_calls:
            raise PlanValidationError("A plan must contain at least one step.")

        if len(tool_calls) > self.max_steps:
            raise PlanValidationError(
                f"Plan exceeds the {self.max_steps}-step limit."
            )

        definitions = {}
        for item in tool_definitions:
            function = item.get("function", {})
            name = function.get("name")
            if isinstance(name, str):
                definitions[name] = function

        steps = []
        seen_ids = set()
        step_references = []

        for index, call in enumerate(tool_calls, start=1):
            if not isinstance(call, dict):
                raise PlanValidationError(
                    f"Step {index} is not a valid tool call."
                )

            call_id = call.get("id")
            function = call.get("function")

            if not isinstance(call_id, str) or not call_id.strip():
                raise PlanValidationError(
                    f"Step {index} has no valid call ID."
                )

            if call_id in seen_ids:
                raise PlanValidationError(
                    f"Duplicate tool-call ID: {call_id}."
                )
            seen_ids.add(call_id)

            if not isinstance(function, dict):
                raise PlanValidationError(
                    f"Step {index} has no valid function."
                )

            name = function.get("name")
            if not isinstance(name, str) or name not in definitions:
                raise PlanValidationError(
                    f"Step {index} requests an unknown tool: {name!r}."
                )

            try:
                arguments = parse_tool_arguments(
                    function.get("arguments", "{}")
                )
            except (ValueError, TypeError) as error:
                raise PlanValidationError(
                    f"Step {index} has invalid arguments: {error}"
                ) from error

            definition = definitions[name]
            parameters = definition.get("parameters", {})
            references = []
            argument_paths = {}
            self._validate_value(
                arguments,
                parameters,
                f"step {index}",
                references,
                argument_paths,
            )

            reason = call.get("reason", "")
            if not isinstance(reason, str) or len(reason) > 300:
                raise PlanValidationError(
                    f"Step {index} reason must be text of at most 300 characters."
                )

            steps.append(PlanStep(
                call_id=call_id,
                name=name,
                arguments=arguments,
                description=definition.get("description", ""),
                reason=reason.strip(),
                position=index,
            ))
            step_references.append((references, argument_paths, index))

        # Dependencies are optional metadata on each proposed tool call.
        # They must reference call IDs in this same plan.
        by_id = {step.call_id: step for step in steps}

        for index, (call, step) in enumerate(zip(tool_calls, steps), start=1):
            dependencies = call.get("depends_on", [])

            if not isinstance(dependencies, list):
                raise PlanValidationError(
                    f"Step {index} dependencies must be a list."
                )

            if any(
                not isinstance(dep, str) or not dep.strip()
                for dep in dependencies
            ):
                raise PlanValidationError(
                    f"Step {index} contains an invalid dependency ID."
                )

            if len(dependencies) != len(set(dependencies)):
                raise PlanValidationError(
                    f"Step {index} contains duplicate dependencies."
                )

            if step.call_id in dependencies:
                raise PlanValidationError(
                    f"Step {index} cannot depend on itself."
                )

            for dependency in dependencies:
                if dependency not in by_id:
                    raise PlanValidationError(
                        f"Step {index} depends on unknown step '{dependency}'."
                    )

            step.depends_on = list(dependencies)

        # Reject cycles before any action can execute.
        visiting = set()
        visited = set()

        def visit(call_id):
            if call_id in visiting:
                raise PlanValidationError(
                    "The task plan contains circular dependencies."
                )
            if call_id in visited:
                return

            visiting.add(call_id)
            for dependency in by_id[call_id].depends_on:
                visit(dependency)
            visiting.remove(call_id)
            visited.add(call_id)

        for step in steps:
            visit(step.call_id)

        # Result references are checked last, after depends_on is known: each
        # one must name an earlier declared step that is also a dependency.
        for references, argument_paths, index in step_references:
            if references:
                self._resolve_references(
                    references,
                    argument_paths,
                    steps[index - 1],
                    by_id,
                    index,
                )

        return TaskPlan(steps=steps)


    def prepare_structured_plan(self, data, tool_definitions):
        """Validate a JSON plan returned by the dedicated planning stage."""
        if not isinstance(data, dict):
            raise PlanValidationError("The proposed plan must be a JSON object.")

        allowed_keys = {
            "steps", "response", "intent_confidence", "clarification"
        }
        extra_keys = set(data) - allowed_keys
        if extra_keys:
            raise PlanValidationError(
                "The plan contains unexpected field(s): "
                + ", ".join(sorted(str(key) for key in extra_keys))
            )

        confidence = data.get("intent_confidence", 1.0)
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0.0 <= confidence <= 1.0
        ):
            raise PlanValidationError(
                "intent_confidence must be a number between 0 and 1."
            )

        clarification = data.get("clarification", "")
        if not isinstance(clarification, str) or len(clarification) > 500:
            raise PlanValidationError(
                "clarification must be text of at most 500 characters."
            )

        steps_data = data.get("steps")
        if not isinstance(steps_data, list):
            raise PlanValidationError("The plan's 'steps' field must be a list.")

        if len(steps_data) > self.max_steps:
            raise PlanValidationError(
                f"Plan exceeds the {self.max_steps}-step limit."
            )

        response = data.get("response", "")
        if not isinstance(response, str):
            raise PlanValidationError("The plan's 'response' field must be text.")

        if not steps_data:
            return TaskPlan(
                steps=[],
                status="completed",
                intent_confidence=float(confidence),
                clarification=clarification.strip(),
            )

        tool_calls = []
        for index, item in enumerate(steps_data, start=1):
            if not isinstance(item, dict):
                raise PlanValidationError(f"Step {index} must be an object.")

            required = {"id", "tool", "arguments", "depends_on"}
            allowed_step_keys = required | {"reason"}
            if not required.issubset(item) or set(item) - allowed_step_keys:
                missing = required - set(item)
                extra = set(item) - allowed_step_keys
                details = []
                if missing:
                    details.append("missing " + ", ".join(sorted(missing)))
                if extra:
                    details.append("unexpected " + ", ".join(sorted(extra)))
                raise PlanValidationError(
                    f"Step {index} has an invalid structure: "
                    + "; ".join(details)
                )

            if not isinstance(item["id"], str) or not item["id"].strip():
                raise PlanValidationError(f"Step {index} has an invalid ID.")

            if not isinstance(item["tool"], str) or not item["tool"].strip():
                raise PlanValidationError(f"Step {index} has an invalid tool name.")

            if not isinstance(item["arguments"], dict):
                raise PlanValidationError(
                    f"Step {index} arguments must be a JSON object."
                )

            if not isinstance(item["depends_on"], list):
                raise PlanValidationError(
                    f"Step {index} dependencies must be a list."
                )

            tool_call = {
                "id": item["id"],
                "type": "function",
                "function": {
                    "name": item["tool"],
                    "arguments": item["arguments"],
                },
                "depends_on": item["depends_on"],
            }
            if "reason" in item:
                tool_call["reason"] = item["reason"]
            tool_calls.append(tool_call)

        plan = self.prepare(tool_calls, tool_definitions)
        plan.intent_confidence = float(confidence)
        plan.clarification = clarification.strip()
        return plan

    @staticmethod
    def start_step(plan, index):
        if plan.status != "ready" and plan.status != "running":
            raise RuntimeError("This plan is no longer executable.")

        if not isinstance(index, int) or not 0 <= index < len(plan.steps):
            raise IndexError("Invalid plan step.")

        step = plan.steps[index]
        if step.status != "pending":
            raise RuntimeError("This step is not pending.")

        by_id = {item.call_id: item for item in plan.steps}
        for dependency in step.depends_on:
            dependency_step = by_id[dependency]
            if dependency_step.status != "success":
                raise RuntimeError(
                    f"Dependency '{dependency}' has not succeeded."
                )

        step.status = "running"
        plan.status = "running"
        return step

    @staticmethod
    def finish_step(plan, index, result):
        if not isinstance(result, dict):
            result = {
                "success": False,
                "message": "Invalid tool result.",
            }

        step = plan.steps[index]
        if step.status != "running":
            raise RuntimeError("This step is not running.")

        step.result = result
        step.status = "success" if result.get("success") is True else "failed"

        if step.status == "failed":
            # Only block steps that transitively depend on this failed step.
            by_id = {item.call_id: item for item in plan.steps}
            failed_ids = {step.call_id}
            changed = True

            while changed:
                changed = False
                for candidate in plan.steps:
                    if candidate.status != "pending":
                        continue
                    if any(dep in failed_ids for dep in candidate.depends_on):
                        candidate.status = "blocked"
                        failed_ids.add(candidate.call_id)
                        changed = True

        # Keep the plan executable while independent steps remain pending.
        if all(item.status == "success" for item in plan.steps):
            plan.status = "completed"
        elif any(item.status in {"pending", "running"} for item in plan.steps):
            plan.status = "running"
        else:
            plan.status = "failed"

        return step.status
