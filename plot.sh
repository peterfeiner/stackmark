#!/bin/bash

if test $# -ne 1; then
    echo 'usage: $0 input_file' 2>&1;
    exit 1
fi

exec gnuplot -e "input_file='$1';" plot.gpi
