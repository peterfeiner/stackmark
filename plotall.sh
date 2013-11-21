#!/bin/bash

set -e

for x in $(find . -name '*.atop'); do
    ./plot.py $(dirname $x)/$(basename $x .atop) &
done

wait

cat > index.html << EOF
<html>
    <body>
EOF

for x in $(find data -name '*.png' | sort -t @ -k 2 -r); do
cat >> index.html << EOF
        <img src="$x"><br>
EOF
done

cat >> index.html << EOF
    </body>
</html>
EOF
