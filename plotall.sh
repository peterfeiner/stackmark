#!/bin/bash

for x in *.atop; do
    ./plot.sh $x &
done

wait

cat > index.html << EOF
<html>
    <body>
EOF

for x in $(ls -t *.atop); do
cat >> index.html << EOF
        <img src="$x.svg"><br>
EOF
done

cat >> index.html << EOF
    </body>
</html>
EOF
