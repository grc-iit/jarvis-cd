#!/bin/bash
set -e

uv lock --check
uv sync --frozen
