#!/bin/bash

set -e

for x in $(find . -name '*.phases'); do
    base="$(dirname $x)/$(basename $x .phases)"
    (
        ./plot.py $base > $base.log 2>&1
        if [[ "$?" != 0 ]]; then
            echo $base error
            cat $base.log
        else
            echo $base ok
        fi
    ) &
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
