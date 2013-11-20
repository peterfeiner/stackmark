#!/bin/bash

set -e

if test $# -ne 1; then
    echo 'usage: $0 TITLE' 2>&1;
    exit 1
fi

exec gnuplot -e "TITLE='$1';" plot.gpi
