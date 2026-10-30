# StripeDouble

Python 3.15 standard-library local Stripe simulator; .python-version pins 3.15.2. Support macOS and Linux;
Unix descriptor passing, file ownership checks and flock are intentional.
Keep the provider engine independent of application accounts, consent and ledgers.
Preserve per-instance API profile isolation, synthetic identities and private bootstrap.
The default Stripe version is 2026-10-28.endive; explicit Acacia compatibility
keeps existing consumers working. Read docs/api-profiles.md before changing it.
Do not add provider traffic, automatic mutation retries or new Stripe capabilities
as part of packaging or cleanup.

On this developer machine, /Volumes/Scratch must be mounted before producing
builds or test output. Use /Volumes/Scratch/stripe-double/build,
/Volumes/Scratch/stripe-double/tmp and /Volumes/Scratch/stripe-double/reports.
Set TMPDIR and PYTHONDONTWRITEBYTECODE=1 for local tests. Never use /tmp for local
builds, captures or copied source. The app itself supports caller-selected state
paths so it remains portable to machines without this developer's Scratch volume.
Never commit state databases, locks, secrets, logs, captures or built .pyz files.
Clean only owned outputs after checking open processes and files.

Run python3.15 -B -m unittest discover -s tests -p 'test_*.py' -v from this repository.
The suite includes HTTP, SQLite, process restart/shutdown and standalone artifact
checks. Simulated results do not establish genuine Stripe behavior.
