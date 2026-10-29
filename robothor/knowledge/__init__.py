"""Knowledge base: per-collection documents with hybrid vector + keyword search.

``store`` holds documents and chunks (migration 155), ``search`` fuses the
vector and keyword legs, ``chunking`` splits text. The agent tools are
``kb_*`` in ``robothor/engine/tools/handlers/knowledge.py``; see
docs/knowledge-base.md.
"""
