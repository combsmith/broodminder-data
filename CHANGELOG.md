# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.7] - 2026-10-12

### Security
- Hash-pinned Python dependencies in `requirements.txt` and `scripts/build-requirements.txt`, and hardened workflow pip installations with `--require-hashes --only-binary :all:` to satisfy OpenSSF Scorecard Pinned-Dependencies check and achieve accepted exception auto-closure (#44).
- Expanded `SECURITY.md` with explicit vulnerability disclosure guidelines, GitHub Private Vulnerability Reporting instructions, email fallback, response timelines, and scope to satisfy OpenSSF Scorecard `securityPolicyContainsVulnerabilityDisclosure` probe and achieve a 10/10 security-policy score (#33).

## [0.1.6] - 2026-10-12

### Security
- Hardened GitHub Actions CI workflow: switched gitleaks download to `curl` with strict protocol and redirect restrictions (`--proto '=https' --proto-redir '=https'`) to disable insecure redirect downgrades (guarding S6506), and disabled credential persistence on repository checkouts (#23).

## [0.1.5] - 2026-10-11

### Added
- Added `raw=True` option to `BroodMinderClient.hive_notes()` and added `BroodMinderClient.hive_notes_raw()` convenience method to retrieve unnormalized wire payloads for lossless archiving.

### Changed
- Normalized `BroodMinderClient.hive_notes()` to consistently return `list[dict]` regardless of whether the API returns a bare array (`[]` when empty) or an object wrapper (`{"notes": [...]}` when populated).
- Updated OpenAPI 3.1 specification for `/user/hive/{id}/notes` with `oneOf` response schema modeling both observed response shapes.
- Updated `Note` schema to use structured `NoteTag` objects (`{id, name}`) matching live API payloads, marked `modified` timestamp as unconfirmed, and documented that apiary-level notes are not returned by the API.

## [0.1.4] - 2026-10-03

### Added
- Added `CHANGELOG.md` documenting historical and current releases.
- Registered `Changelog` URL under `[project.urls]` in `pyproject.toml` so PyPI displays a direct release notes link in the project sidebar.
- Added packaging unit tests in `tests/test_packaging.py` validating `CHANGELOG.md` presence, structure, and `Changelog` project URL registration.

## [0.1.3] - 2026-10-03

### Added
- **Sensor Battery Health & Offline Reporting Monitor:**
  - Evaluates battery percentages against low-battery threshold (<80%) and flags devices needing battery replacement.
  - Detects silent dropouts ("not reporting" / stale sensors) with default 7-day cutoff.
  - Implemented core evaluation engine in `bm/battery.py` (`evaluate_device_health`, `scan_device_health_from_stream`, `scan_device_health_from_file`) with output rendering via `format_table`/`format_json`/`format_csv`.
  - Added standalone CLI script `scripts/battery_health.py` and console script entrypoint `broodminder-battery`.
  - Added deterministic unit test suite in `tests/test_battery.py` with 98.6% test coverage.
- **Automated On-Merge Release Workflow:**
  - Added `push: branches: [main]` trigger in `.github/workflows/publish.yml` to automatically publish releases to PyPI and generate GitHub Releases when version is bumped.
  - Added version extraction and release gate helpers in `scripts/pypi_onboard.py` (`get_package_version`, `should_release_version`).
  - Implemented least-privilege isolated CI jobs (`prepare`, `build`, `publish`, `release`) with concurrency serialization to eliminate check-then-act race conditions.

## [0.1.2] - 2026-10-02

### Changed
- Cleaned up organizational metadata and documentation references.

## [0.1.1] - 2026-10-02

### Changed
- Cleaned up lingering repository references and aligned scope across project documents.

## [0.1.0] - 2026-10-02

### Added
- Initial PyPI release of `broodminder-data`.
- Reusable, typed Python client SDK (`bm.client.BroodMinderClient`) with rate-limit awareness, exponential backoff retry, and explicit time-window helpers (`iter_windows`) for walking histories longer than the API's per-request cap.
- Standalone CLI utilities:
  - Account topology discovery (`scripts/discover.py`).
  - Resumable, budget-aware bulk historical export (`scripts/extract_all.py`).
  - Incremental data flattener with deduplication into NDJSON and CSV (`scripts/flatten.py`).
- Authoritative OpenAPI 3.1 specification for the External User API (`openapi/broodminder-openapi.yaml`) with Redocly linting and interactive docs preview.
- PyPI onboarding probe, build verification, and Trusted Publishing OIDC workflow.

[Unreleased]: https://github.com/petry-projects/broodminder-data/compare/v0.1.7...HEAD
[0.1.7]: https://github.com/petry-projects/broodminder-data/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/petry-projects/broodminder-data/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/petry-projects/broodminder-data/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/petry-projects/broodminder-data/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/petry-projects/broodminder-data/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/petry-projects/broodminder-data/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/petry-projects/broodminder-data/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/petry-projects/broodminder-data/releases/tag/v0.1.0
