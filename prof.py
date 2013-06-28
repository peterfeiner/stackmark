#!/usr/bin/python

import argparse
import datetime
import json
import logging
import novaclient
import novaclient.shell
import os
import signal
import sys
import time

from contextlib import contextmanager
from numpy import mean, median
from subprocess import Popen, check_output, CalledProcessError, PIPE
from threading import Thread, Lock, Condition

DEV_NULL = open('/dev/null', 'w+')

PRINT_LOCK = Lock()

def timestamp():
    return str(datetime.datetime.now())

class Timer(object):
    def __init__(self, name):
        self.start()
        self.name = name

    def start(self):
        self.start_time = time.time()

    def elapsed(self):
        return time.time() - self.start_time

class InstanceDoesNotExistError(Exception):
    def __init__(self, instance_id):
        Exception.__init__(self, 'Instance with id %s does not exist.' %
                           instance_id)

class InstanceHasNoIpError(Exception):
    def __init__(self, instance_id):
        Exception.__init__(self, 'Instance with id %s has no ip address.' %
                           instance_id)

class Instance(object):
    def __init__(self, nova, id):
        self.__nova = nova
        self.__id = id

    @property
    def id(self):
        return self.__id

    def delete(self):
        self.__nova.delete(self.__id)

    def __get(self):
        instances = self.__nova.list()
        for instance in instances:
            if instance.id == self.__id:
                return instance
        raise InstanceDoesNotExistError(self.__id)

    def any_ip(self):
        instance = self.__get()
        for addrs in instance.networks.itervalues():
            if len(addrs) > 0:
                return addrs[0]
        raise InstanceHasNoIpError(self.__id)

    def get_status(self):
        return self.__get().status

    def __repr__(self):
        return 'Instance(%r, %r)' % (self.__nova, self.__id)

def create_novaclient():
    shell = novaclient.shell.OpenStackComputeShell()
    extensions = shell._discover_extensions('1.1')
    no_cache = os.environ.get('OS_NO_CACHE', '1') in ['1', 'y']
    getenv = os.environ.get
    return novaclient.client.Client('2',
                                    getenv('OS_USERNAME'),
                                    getenv('OS_PASSWORD'),
                                    getenv('OS_TENANT_NAME'),
                                    getenv('OS_AUTH_URL'),
                                    no_cache=no_cache,
                                    http_log_debug=getenv('NOVACLIENT_DEBUG'),
                                    extensions=extensions)

class NovaClientPool(object):
    def __init__(self, size):
        self.__all = set([create_novaclient() for i in range(size)])
        self.__available = self.__all.copy()
        self.__cond = Condition()

    @contextmanager
    def scoped(self):
        client = self.get()
        try:
            yield client
        finally:
            self.put(client)

    def get(self):
        with self.__cond:
            while len(self.__available) == 0:
                self.__cond.wait()
            return self.__available.pop()

    def put(self, client):
        assert client in self.__all
        assert client not in self.__available
        with self.__cond:
            self.__available.add(client)
            self.__cond.notify()

class Nova(object):
    def __init__(self, poolsize=1, simple_list=False):
        self.__list = []
        self.__list_cond = Condition()
        self.__list_status = 'IDLE'
        self.__novaclient_pool = NovaClientPool(poolsize)
        if simple_list:
            self.list = self.__simple_list
        else:
            self.list = self.__coalesced_list

    def __simple_list(self):
        with self.__novaclient_pool.scoped() as client:
            return client.servers.list()

    def __coalesced_list(self):
        with self.__list_cond:
            if self.__list_status == 'IDLE':
                self.__list_status = 'ACTIVE'
            else:
                while True:
                    self.__list_cond.wait()
                    if self.__list_status == 'ERROR':
                        self.__list_status = 'ACTIVE'
                        break
                    elif self.__list_status == 'IDLE':
                        return self.__list
                    else:
                        assert self.__list_status == 'ACTIVE'

        while True:
            try:
                with self.__novaclient_pool.scoped() as client:
                    new_list = client.servers.list()
                break
            except Exception:
                time.sleep(0.5)
                continue
            except:
                with self.__list_cond:
                    self.__list_status = 'ERROR'
                    self.__list_cond.notify()
                raise

        with self.__list_cond:
            self.__list = new_list
            self.__list_status = 'IDLE'
            self.__list_cond.notify_all()
            return self.__list

    def boot(self, name, image, flavor, key_name=None):
        with self.__novaclient_pool.scoped() as client:
            instance = client.servers.create(name=name,
                                             image=image,
                                             flavor=flavor,
                                             key_name=key_name)
        return Instance(self, instance.id)

    def live_image_start(self, name, image):
        with self.__novaclient_pool.scoped() as client:
            instances = client.cobalt.start_live_image(server=image, name=name)
        assert len(instances) == 1
        return Instance(self, instances[0].id)

    def delete(self, id):
        with self.__novaclient_pool.scoped() as client:
            client.servers.delete(id)

class Atop(object):
    def __init__(self, title, interval=2):
        self.title = title
        self.process = None
        self.interval = interval

    def start(self):
        assert self.process == None
        path = '%s@%s.atop' % (self.title, timestamp().replace(' ', '-'))
        self.process = Popen(['sudo', 'atop', '-w', path, str(self.interval)])

    def stop(self):
        self.process.send_signal(signal.SIGINT)
        self.process.wait()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, type, value, traceback):
        self.stop()

class NullAtop(object):
    def start(self):
        pass

    def stop(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        pass

class PhaseLog(object):
    def __init__(self, timer):
        self.last_phase = {}
        self.in_phase = {}
        self.timer = timer
        self.lock = Lock()
        self.order = []
        self.last_in_phase = {}

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, type, value, traceback):
        self.stop()

    def start(self):
        pass

    def stop(self):
        pass

    def event(self, experiment, *args):
        with self.lock:
            phase = experiment.phase
            if phase not in self.in_phase:
                self.order.append(phase)

            try:
                last_phase = self.last_phase[experiment]
            except KeyError:
                pass
            else:
                # No change.
                if last_phase == phase:
                    return
                self.in_phase[last_phase] -= 1
                if self.in_phase[last_phase] == 0:
                    self.last_in_phase[last_phase] = experiment

            self.in_phase.setdefault(phase, 0)
            self.in_phase[phase] += 1
            self.last_in_phase.pop(phase, None)
            self.last_phase[experiment] = phase

            with PRINT_LOCK:
                print '%.2f' % self.timer.elapsed(), '\t',
                for phase in self.order:
                    print phase, '%-3d' % self.in_phase[phase], 
                print

    def report(self):
        with PRINT_LOCK:
            fmtstr = '%%-%ds %%-10s' % (max(map(len, self.order)))
            print fmtstr % ('PHASE', 'LAST')
            for phase in self.order:
                try:
                    last = self.last_in_phase[phase]
                except KeyError:
                    last_id = '<still active or none exited>'
                else:
                    last_id = last.instance.id
                print fmtstr % (phase, last_id)

class Phase(object):
    def __init__(self, timer):
        self.name = timer.name
        self.start = timer.start_time
        self.duration = timer.elapsed()
        self.end = self.start + self.duration

class Experiment(object):
    def __init__(self, name, args, nova):
        self.args = args
        self.timer = Timer('total')
        self.phase = 'setup'
        self.listeners = []
        self.name = name
        self.nova = nova
        self.phases = []
        self.instance = None

    def add_listener(self, listener):
        self.listeners.append(listener)

    def remove_listener(self, listener):
        i = list(reversed(self.listeners)).index(listener)
        self.listeners.pop(len(self.listeners) - i - 1)

    def event(self, *args):
        for listener in self.listeners:
            listener(self, *args)

    def start_phase(self, name):
        self.phase = name
        self.event()

    def run(self):
        self.__create()
        if self.args.check_dhcp_hosts:
            self.__check_dhcp_hosts()
        if self.args.check_iptables:
            self.__check_iptables()
        if self.args.check_ping:
            self.__check_ping()
        if self.args.check_nmap:
            self.__check_nmap()
        if self.args.check_ssh:
            self.__check_ssh()
        if self.args.delete:
            self.__delete()
        self.start_phase('fin')

    def __boot_op(self, name):
        return self.nova.boot(name,
                              image=self.args.image,
                              flavor=self.args.flavor,
                              key_name=self.args.key_name)

    def __launch_op(self, name):
        return self.nova.live_image_start(name, self.args.image)

    def __create(self):
        self.start_phase(self.args.op + '_api')
        if self.args.op == 'boot':
            op_func = self.__boot_op
        else:
            op_func = self.__launch_op

        self.start_phase(self.args.op)
        self.instance = op_func(self.name)
        while True:
            status = self.instance.get_status()
            assert status != 'ERROR'
            if status == 'ACTIVE':
                break

    def __delete(self):
        self.start_phase('delete_api')
        self.instance.delete()
        self.start_phase('delete')
        while True:
            try:
                assert self.instance.get_status() != 'ERROR'
            except InstanceDoesNotExistError:
                break

    def __check_dhcp_hosts(self):
        self.start_phase('dhcp_hosts')
        while True:
            with open(self.args.check_dhcp_hosts) as f:
                if self.instance.any_ip() in f.read():
                    break
            time.sleep(1)

    def __check_iptables(self):
        self.start_phase('iptables')
        while True:
            p = Popen(['sudo', 'iptables-save'], stdout=PIPE)
            out, err = p.communicate()
            if self.instance.any_ip() in out:
                break
            time.sleep(1)

    def __check_nmap(self):
        self.start_phase('nmap')

    def __check_ping(self):
        self.start_phase('ping')
        while True:
            p = Popen(['ping', '-c', '1', self.instance.any_ip()],
                      stdout=DEV_NULL)
            if p.wait() == 0:
                break

    def __check_ssh(self):
        self.start_phase('ssh')
        while True:
            p = Popen(['ssh',
                       '-l', self.args.check_ssh_user,
                       '-o', 'UserKnownHostsFile=/dev/null',
                       '-o', 'StrictHostKeyChecking=no',
                       '-o', 'PasswordAuthentication=no',
                       self.instance.any_ip(),
                       self.args.check_ssh_command],
                       stdout=DEV_NULL,
                       stderr=DEV_NULL)
            if p.wait() == 0:
                break
            time.sleep(1)

class ParallelExperiment(object):
    def __init__(self, args, atop, nova):
        self.args = args
        self.atop = atop
        self.nova = nova

    def run(self):
        threads = []
        timer = Timer('total')
        log = PhaseLog(timer)
        timer.start()
        with self.atop, log:
            for i in range(self.args.n):
                experiment = Experiment('%s-%s-of-%s' % (self.args.name_prefix,
                                                         i + 1, self.args.n),
                                        self.args, self.nova)
                experiment.add_listener(log.event)
                thread = Thread(target=experiment.run)
                thread.daemon = True
                thread.start()
                threads.append(thread)

            # Join all of the threads. Wakeup every 1s so we can check for
            # keyboard interrupts.
            while True:
                for thread in threads:
                    if thread.is_alive():
                        thread.join(1)
                        break
                else:
                    break
        log.report()

def parse_argv(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument('op', choices=['boot', 'launch'])
    parser.add_argument('n', type=int, default=1, nargs='?')
    parser.add_argument('--image',
                        default='precise-server-cloudimg-amd64-disk1.img')
    parser.add_argument('--flavor', default='m1.tiny')
    parser.add_argument('--key-name', default=None)
    parser.add_argument('--name-prefix', default='prof')
    parser.add_argument('--check-dhcp-hosts', type=str, default=None)
    parser.add_argument('--check-iptables', action='store_true')
    parser.add_argument('--check-ssh', action='store_true')
    parser.add_argument('--check-ssh-user', default='ubuntu')
    parser.add_argument('--check-ssh-command', default='true')
    parser.add_argument('--check-ping', action='store_true')
    parser.add_argument('--check-nmap', type=int, default=None)
    parser.add_argument('--atop', action='store_true')
    parser.add_argument('--atop-interval', type=int, default=2)
    parser.add_argument('--debug', action='store_true')
    parser.add_argument('--simple-list', action='store_true')
    parser.add_argument('--client-pool-size', type=int, default=None)
    parser.add_argument('--no-delete', dest='delete', action='store_false')
    return parser.parse_args(argv[1:])

def clone_args(args):
    clone = args.__class__()
    for key, value in vars(args).iteritems():
        setattr(clone, key, value)
    return clone

def main(argv):
    args = parse_argv(argv)

    if args.client_pool_size == None:
        args.client_pool_size = args.n

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
        os.environ['NOVACLIENT_DEBUG'] = '1'

    if args.atop:
        atop = Atop('%s-%s' % (args.op, args.n), args.atop_interval)
    else:
        atop = NullAtop()

    nova = Nova(poolsize=args.client_pool_size, simple_list=args.simple_list)
    experiment = ParallelExperiment(args, atop=atop, nova=nova)
    experiment.run()

if __name__ == '__main__':
    main(sys.argv)
