#!/usr/bin/env python

import sys

if __name__ == '__main__':
    
    last_t = None

    in_phase = dict([(phase, 0) for phase in sys.argv[2:]])

    def p(t):
        print '%.10f' % float(t),
        for phase in sys.argv[2:]:
            print in_phase[phase],
        print

    for line in open(sys.argv[1]):
        line = line.strip()
        t, event = line.split('\t', 1)

        if t != last_t:
            if last_t != None:
                p(last_t)
            p(t)
            last_t = t

        if event.startswith('start-'):
            phase = event[6:]
            inc = 1
        elif event.startswith('end-'):
            phase = event[4:]
            inc = -1
        elif event.startswith('in-'):
            inc = 0
            pass
        else:
            assert 0

        if phase in in_phase:
            in_phase[phase] += inc

    p(t)
