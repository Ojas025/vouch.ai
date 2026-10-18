"""Observability building blocks for Vouch.

Kept separate from :mod:`vouch.logger` so the logger module stays a single
concern (handler topology) while correlation, sanitisation, spans and the
security audit trail each own one module.
"""