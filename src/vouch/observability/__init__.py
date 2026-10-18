"""Observability building blocks: correlation, sanitisation, spans and auditing.

Kept separate from :mod:`vouch.logger`, which stays a single concern (handler
topology), so each piece here owns one responsibility.
"""