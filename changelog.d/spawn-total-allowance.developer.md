Agent manifests can set `v2.max_spawn_total` to bound admitted child attempts
across repeated batches and descendants. Atomic ancestor accounting retains slots
for failed, deduplicated and cancelled attempts; zero never removes an inherited limit.
