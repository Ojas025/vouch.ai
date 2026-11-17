#!/usr/bin/env bash

PROJECT_ROOT="$(git rev-parse --show-toplevel)"

# Run bandit
bandit -r "$PROJECT_ROOT/src/vouch"
