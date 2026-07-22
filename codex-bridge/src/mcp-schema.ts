const JSON_SCHEMA_KEYS = new Set([
  "$ref", "$defs", "anyOf", "oneOf", "allOf", "not", "type", "enum",
  "const", "title", "description", "default", "examples", "format",
  "items", "properties", "required", "additionalProperties", "minimum",
  "maximum", "minLength", "maxLength", "pattern", "minItems", "maxItems",
]);
const JSON_SCHEMA_TYPES = new Set(["object", "array", "string", "integer", "number", "boolean", "null"]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function normalizeParameterSchema(value: unknown): Record<string, unknown> {
  if (!isRecord(value)) return {};
  // Accept a complete JSON Schema when a future backend version supplies one.
  if (value.type === "object" || value.properties || value.$ref || value.anyOf || value.oneOf) {
    return value;
  }
  const schema: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value)) {
    if (JSON_SCHEMA_KEYS.has(key)) schema[key] = item;
  }
  return schema;
}

export function validateMcpInputSchema(value: unknown, path = "$", depth = 0): string[] {
  if (!isRecord(value)) return [`${path} must be an object`];
  if (depth > 12) return [`${path} exceeds schema nesting limit`];
  const errors: string[] = [];
  for (const key of Object.keys(value)) {
    if (!JSON_SCHEMA_KEYS.has(key)) errors.push(`${path}.${key} is not a JSON Schema keyword`);
  }
  if (value.type !== undefined && (typeof value.type !== "string" || !JSON_SCHEMA_TYPES.has(value.type))) {
    errors.push(`${path}.type is invalid`);
  }
  if (value.required !== undefined) {
    if (!Array.isArray(value.required) || value.required.some((item) => typeof item !== "string")) {
      errors.push(`${path}.required must be a string array`);
    }
  }
  if (value.properties !== undefined) {
    if (!isRecord(value.properties)) errors.push(`${path}.properties must be an object`);
    else for (const [name, child] of Object.entries(value.properties)) errors.push(...validateMcpInputSchema(child, `${path}.properties.${name}`, depth + 1));
  }
  if (value.items !== undefined) errors.push(...validateMcpInputSchema(value.items, `${path}.items`, depth + 1));
  if (value.additionalProperties !== undefined && typeof value.additionalProperties !== "boolean" && !isRecord(value.additionalProperties)) {
    errors.push(`${path}.additionalProperties must be boolean or schema`);
  }
  if (value.additionalProperties && typeof value.additionalProperties === "object") errors.push(...validateMcpInputSchema(value.additionalProperties, `${path}.additionalProperties`, depth + 1));
  if (value.enum !== undefined && !Array.isArray(value.enum)) errors.push(`${path}.enum must be an array`);
  for (const key of ["minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"]) {
    if (value[key] !== undefined && (typeof value[key] !== "number" || !Number.isFinite(value[key]))) errors.push(`${path}.${key} must be a finite number`);
  }
  if (value.type === "array" && value.items === undefined) errors.push(`${path}.items is required for arrays`);
  if (value.type === "object" && value.properties !== undefined && !isRecord(value.properties)) errors.push(`${path}.properties must be an object`);
  return errors;
}

/** Convert the backend's `{name: {type, required, ...}}` map to MCP JSON Schema. */
export function parametersToInputSchema(parameters: unknown, fallbackName?: string): Record<string, unknown> {
  if (!isRecord(parameters) || Object.keys(parameters).length === 0) {
    return fallbackName?.startsWith("workspace_")
      ? { type: "object", additionalProperties: true }
      : { type: "object", properties: {}, additionalProperties: true };
  }
  if (parameters.type === "object" && (parameters.properties || parameters.additionalProperties !== undefined)) {
    return parameters;
  }

  const properties: Record<string, unknown> = {};
  const required: string[] = [];
  for (const [name, rawSpec] of Object.entries(parameters)) {
    const spec = isRecord(rawSpec) ? rawSpec : { type: rawSpec };
    const schema = normalizeParameterSchema(spec);
    delete schema.required;
    properties[name] = schema;
    if (rawSpec && isRecord(rawSpec) && rawSpec.required === true) required.push(name);
  }
  return {
    type: "object",
    properties,
    ...(required.length ? { required } : {}),
    additionalProperties: false,
  };
}
