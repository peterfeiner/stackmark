#!/bin/bash

set -x

for x in $(find . -name '*.atop'); do
    ./plot.sh $(dirname $x)/$(basename $x .atop) &
done

wait

cat > index.html << EOF
<html>
    <body>
EOF

for x in $(find . -name '*.png' | sort); do
cat >> index.html << EOF
        <img src="$x"><br>
EOF
done

cat >> index.html << EOF
    </body>
</html>
EOF
