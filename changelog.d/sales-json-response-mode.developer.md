Agent authors can set `model.response_format: json_object` to request a single
JSON result through the native runner, including streaming and fallback calls.
Sales dossier schemas now expose the existing requirement that each asserted
criterion has evidence references. See `docs/AGENT_BUILDER.md` and
`docs/SALES_INTELLIGENCE.md`; workflow validation still checks business meaning.
