#!/bin/bash

set -x

for x in $(find . -name '*.atop'); do
    ./plot.sh $(basename $x .atop) &
done

wait

cat > index.html << EOF
<html>
    <body>
EOF

for x in $(find . -name '*.atop'); do
    x=$(basename $x .atop)
cat >> index.html << EOF
        <img src="$x.svg"><br>
EOF
done

cat >> index.html << EOF
    </body>
</html>
EOF
