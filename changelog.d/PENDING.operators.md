A memory fact whose text contains a NUL character is now stored with that
character removed, instead of failing the insert and being skipped. Nothing
else in the text changes. Each such write logs a WARNING with the number of
characters stripped. See [Memory system](memory-system.md).
