"""Safe, deterministic task-plan validation and execution tracking."""

from dataclasses import dataclass, field

from app.brain.validator import parse_tool_arguments


MAX_PLAN_STEPS = 12


@dataclass
class PlanStep:
    call_id: str
    name: str
    arguments: dict
    description: str
    status: str = "pending"
    result: dict | None = None
    depends_on: list[str] = field(default_factory=list)


@dataclass
class TaskPlan:
    steps: list[PlanStep] = field(default_factory=list)
    status: str = "ready"

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


class TaskPlanner:
    """Validates proposed tool batches and tracks their execution."""

    def __init__(self, max_steps=MAX_PLAN_STEPS):
        if not isinstance(max_steps, int) or not 1 <= max_steps <= 50:
            raise ValueError("max_steps must be between 1 and 50.")
        self.max_steps = max_steps

    @staticmethod
    def _validate_value(value, schema, path):
        expected = schema.get("type")

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

            for key, item in value.items():
                if key in properties:
                    TaskPlanner._validate_value(
                        item, properties[key], f"{path}.{key}"
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

            item_schema = schema.get("items")
            if item_schema:
                for index, item in enumerate(value):
                    TaskPlanner._validate_value(
                        item, item_schema, f"{path}[{index}]"
                    )

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
            self._validate_value(arguments, parameters, f"step {index}")

            steps.append(PlanStep(
                call_id=call_id,
                name=name,
                arguments=arguments,
                description=definition.get("description", ""),
            ))

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

        return TaskPlan(steps=steps)


    def prepare_structured_plan(self, data, tool_definitions):
        """Validate a JSON plan returned by the dedicated planning stage."""
        if not isinstance(data, dict):
            raise PlanValidationError("The proposed plan must be a JSON object.")

        allowed_keys = {"steps", "response"}
        extra_keys = set(data) - allowed_keys
        if extra_keys:
            raise PlanValidationError(
                "The plan contains unexpected field(s): "
                + ", ".join(sorted(str(key) for key in extra_keys))
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
            return TaskPlan(steps=[], status="completed")

        tool_calls = []
        for index, item in enumerate(steps_data, start=1):
            if not isinstance(item, dict):
                raise PlanValidationError(f"Step {index} must be an object.")

            required = {"id", "tool", "arguments", "depends_on"}
            if set(item) != required:
                missing = required - set(item)
                extra = set(item) - required
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

            tool_calls.append({
                "id": item["id"],
                "type": "function",
                "function": {
                    "name": item["tool"],
                    "arguments": item["arguments"],
                },
                "depends_on": item["depends_on"],
            })

        return self.prepare(tool_calls, tool_definitions)

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
