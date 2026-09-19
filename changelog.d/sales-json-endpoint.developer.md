Funded JSON-mode requests now select only endpoints that advertise response-format
support. An unsupported endpoint is excluded before spending admission, avoiding
predictable provider rejection and an unnecessary fallback attempt. See
`docs/SALES_INTELLIGENCE.md` for the bounded request contract.
