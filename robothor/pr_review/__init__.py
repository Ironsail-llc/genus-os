"""Pull-request review: the platform logic behind the GitHub review tools.

Pure modules only. The HTTP calls live in
``robothor/engine/tools/handlers/github_api.py``; this package holds the rules
those calls follow, so they can be tested without a network and reused by the
review intake and policy that build on them.
"""
