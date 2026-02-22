# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial project structure with `pyproject.toml` and package skeleton
- Config system with environment variable support (`robothor.config`)
- Database connection factory with pooling (`robothor.db`)
- Service registry for port indirection (`robothor.services`)
- CI pipeline with ruff, mypy, and pytest on Python 3.11/3.12/3.13
