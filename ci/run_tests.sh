#!/bin/bash
set -e

python -m pytest test/unit/ \
    --cov=jarvis_cd \
    --cov=builtin \
    --cov-report=xml:coverage.xml \
    --cov-report=html:htmlcov \
    --cov-report=term-missing \
    -v
