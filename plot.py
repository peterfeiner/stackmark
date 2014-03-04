#!/usr/bin/env python
import sys
from subprocess import Popen, PIPE
from collections import namedtuple
from StringIO import StringIO
import tempfile
import os

Region = namedtuple('Region', 'title phases')

regions = [
    Region('create-api', ['create:api']),
    Region('pre-sched', ['create:none']),
    Region('sched', ['create:scheduling']),
    Region('netwk', ['create:networking']),
    Region('bdmap', ['create:block_device_mapping']),
    Region('spawn', ['create:spawning']),
    Region('ovs-vsctl', ['syslog_ovsvsctl']),
    Region('boot', ['console_boot']),
    Region('iptables', ['iptables']),
    Region('dhcp-host', ['syslog_dhcp']),
    Region('dhcp-guest', ['console_dhcp']),
    Region('pinging', ['ping']),
    Region('sshing', ['ssh']),
    Region('deleting', ['delete_api', 'delete']),
    Region('deleted', ['fin']),
]

plot = StringIO()
plot.write('merge_cmd = "< ./merge-phases.py " . TITLE . ".phases ')
for region in regions:
    for phase in region.phases:
        plot.write('%s ' % phase)
plot.write('"\n')

plot.write('plot merge_cmd\\\n')
column_count = sum([len(r.phases) for r in regions])
for i in range(len(regions)):
    region = regions[len(regions) - i - 1]
    if i > 0:
        plot.write('    ""\\\n')
    plot.write('    using ($1):((')
    plot.write(' + '.join(map(lambda c: '$%s' % (c + 2), range(column_count))))
    plot.write(') / N)\\\n')
    plot.write('    title "%s" with boxes fs solid ls %d,\\\n' %
               (region.title, i + 1))
    column_count -= len(region.phases)

plot.write(r'''    cpu_cmd \
    using ($3 - start_time):(utilization($6 * $7 * $8, $12))\
    title 'U(CPU)' ls 100,\
    dsk_cmd\
    using ($3 - start_time):($8 / ($6 * 1000))\
    title 'U(DSK)' ls 101
''')


script = tempfile.TemporaryFile()
script.write(open('plot.gpi').read())
script.write(plot.getvalue())
script.flush()
script_path = '/proc/%d/fd/%d' % (os.getpid(), script.fileno())

p = Popen(['gnuplot', '-e', 'TITLE=\'%s\';' % sys.argv[1], script_path])
p.wait()
sys.exit(p.returncode)
