#!/bin/sh
set -e
python -m unittest discover -s tests
cram --shell=/bin/bash tests/*.t
