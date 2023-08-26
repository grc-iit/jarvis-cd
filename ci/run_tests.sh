#!/bin/bash
coverage run -m pytest test
rm -rf "*.pyc"
coverage report
coverage-lcov