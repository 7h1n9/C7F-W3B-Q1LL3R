const JSON_SCHEMA_KEYS = new Set([
  "$ref", "$defs", "anyOf", "oneOf", "allOf", "not", "type", "enum",
  "const", "title", "description", "default", "examples", "format",
  "items", "properties", "required", "additionalProperties", "minimum",
  "maximum", "minLength", "maxLength", "pattern", "minItems", "maxItems",
]);

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
