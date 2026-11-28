You can now measure how accurately conversation ingest extracts facts with
`robothor memory-eval --extraction --extraction-model <local model>`. It runs
the real extraction on a labelled set of generic conversations, needs no
database, and reports precision, recall, and how often the trust label, the
speaker, the source turns and the date are right. It also counts pasted or
third-party content that was stored as the operator's own words. See
[Memory System](memory-system.md#conversation-facts) for what each number means.
