#!/usr/bin/env python

import argparse
import datetime
import json
import logging
import os
import os
import re
import signal
import sys
import time
import shlex
import random

from collections import namedtuple
from subprocess import Popen, PIPE, check_output
from threading import Thread, Lock, Condition, local

import keystoneclient.v2_0.client
import novaclient
import novaclient.shell

DEV_NULL = open('/dev/null', 'w+')

PRINT_LOCK = Lock()

def status_line(msg=None):
    print '\r', ' ' * 80, '\r',
    if msg is not None:
        print msg,
        print ' ',
    sys.stdout.flush()

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


NetworkPort = namedtuple('NetworkPort', 'network ip mac type')

class Instance(object):
    def __init__(self, nova, id):
        self.__nova = nova
        self.__server_data = None
        self.id = id

    @property
    def __path(self):
        return os.path.join(self.__nova.instances_path, self.id)

    @property
    def console_log_path(self):
        return os.path.join(self.__path, 'console.log')

    def delete(self):
        self.__nova.delete(self.id)

    @property
    def __server(self):
        if self.__server_data == None:
            self.__server_data = self.__nova.show(self.id)
        return self.__server_data

    def refetch(self):
        self.__server_data = None

    @property
    def net2ports(self):
        net2ports = {}
        for network, addresses in self.__server.addresses.iteritems():
            ports = []
            net2ports[network] = ports
            for address in addresses:
                ports.append(NetworkPort(network, address['addr'],
                                         address['OS-EXT-IPS-MAC:mac_addr'],
                                         type=address['OS-EXT-IPS:type']))
        return net2ports

    @property
    def ports(self):
        ports = []
        for netports in self.net2ports.itervalues():
            ports.extend(netports)
        return ports

    @property
    def status(self):
        return self.__server.status

    @property
    def task_state(self):
        return getattr(self.__server, 'OS-EXT-STS:task_state')

    def get_port(self, network):
        if network is None:
            ports = self.ports
        else:
            ports = self.net2ports[network]
        if len(ports) == 0:
            raise InstanceHasNoIpError(self.id)
        return ports[0]

    def get_mac(self, network):
        return self.get_port(network).mac

    def get_ip(self, network):
        return self.get_port(network).ip

    INTERFACE_ID_RE=re.compile('<parameters interfaceid=\'([a-z0-9-]*)\'/>')

    def fetch_port_uuid(self, network):
        out = check_output(['virsh', 'dumpxml', self.id])
        match = self.INTERFACE_ID_RE.search(out)
        return match.groups()[0]

    def fetch_status(self):
        self.refetch()
        return self.status

    def __repr__(self):
        return 'Instance(%r, %r)' % (self.__nova, self.id)

class SharedTokenClientFactory(object):

    def __init__(self, username, password, tenant_name, tenant_id, auth_url):
        self.username = username
        self.password = password
        self.tenant_name = tenant_name
        self.tenant_id = tenant_id
        self.auth_url = auth_url

        self._auth_ref = None
        self._nova_extensions = None

    @property
    def auth_ref(self):
        if self._auth_ref is None:
            self.refresh_auth_ref()
        return self._auth_ref

    @property
    def service_catalog(self):
        return self.auth_ref.service_catalog

    @property
    def nova_api_url(self):
        return self.service_catalog.url_for(attr='region',
                                            service_type='compute',
                                            endpoint_type='publicURL')

    @property
    def nova_extensions(self):
        if self._nova_extensions is None:
            self.refresh_nova_extensions()
        return self._nova_extensions

    def refresh_auth_ref(self):
        self._auth_ref = self.create_keystone().auth_ref

    def refresh_nova_extensions(self):
        shell = novaclient.shell.OpenStackComputeShell()
        self._nova_extensions = shell._discover_extensions('1.1')

    def create_nova(self):
        client = novaclient.v1_1.Client(username=self.username,
                                        api_key=self.password,
                                        project_id=self.tenant_name,
                                        tenant_id=self.auth_ref.tenant_id,
                                        auth_url='/v2/',
                                        extensions=self.nova_extensions)
        client.client.management_url = self.nova_api_url
        client.client.auth_token = self.auth_ref.auth_token
        return client

    def create_keystone(self):
        return keystoneclient.v2_0.client.Client(username=self.username,
                                                 password=self.password,
                                                 tenant_name=self.tenant_name,
                                                 tenant_id=self.tenant_id,
                                                 auth_url=self.auth_url)

    def prepare(self):
        self.auth_ref
        self.nova_extensions

class Nova(object):
    def __init__(self, client_factory, simple_list, instances_path):
        self.__list = []
        self.__list_cond = Condition()
        self.__list_status = 'IDLE'
        self.__tls = local()
        self.__client_factory = client_factory
        self.__simple_show = simple_list
        self.instances_path = instances_path

    @property
    def __client(self):
        try:
            return self.__tls.client
        except AttributeError:
            self.__tls.client = self.__client_factory.create_nova()
            return self.__tls.client

    def show(self, instance_id):
        if not self.__simple_show:
            for instance in self.coalesced_list():
                if instance.id == instance_id:
                    return instance
        for instance in self.simple_list():
            if instance.id == instance_id:
                return instance
        raise InstanceDoesNotExistError(instance_id)

    def list(self):
        if self.__simple_show:
            return self.simple_list()
        else:
            return self.coalesced_list()

    def simple_list(self):
        return self.__client.servers.list()

    def coalesced_list(self):
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
                new_list = self.__client.servers.list()
                break
            except Exception, e:
                with PRINT_LOCK:
                    print 'error retrieving list (%s), retrying in 0.5s' % e
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

    def boot(self, name, image, flavor, key_name, num_instances=1):
        instance = self.__client.servers.create(name=name,
                                                image=image,
                                                flavor=flavor,
                                                key_name=key_name,
                                                min_count=num_instances)
        return Instance(self, instance.id)

    def live_image_start(self, name, image, key_name, num_instances=1):
        instances =\
            self.__client.cobalt.start_live_image(server=image,
                                                  name=name,
                                                  key_name=key_name,
                                                  num_instances=num_instances)
        assert len(instances) == 1
        return Instance(self, instances[0].id)

    def delete(self, id):
        self.__client.servers.delete(id)

class Atop(object):
    def __init__(self, title, interval, output_path):
        self.title = title
        self.process = None
        self.interval = interval
        self.output_path = output_path

    def start(self):
        assert self.process == None
        self.process = Popen(
            ['sudo', 'atop', '-w', '%s/%s.atop' % (self.output_path,
                                                   self.title),
                             str(self.interval)],
            close_fds=True)

    def stop(self):
        # Wow, this is weird. In Ubuntu 13.10, you can't send signals to sudo
        # processes. Strange how the shell can though.
        os.system('sudo kill -INT %d' % self.process.pid)
        #self.process.send_signal(signal.SIGINT)
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
    def __init__(self, timer, title, output_path):
        self.last_phase = {}
        self.in_phase = {}
        self.timer = timer
        self.lock = Lock()
        self.order = []
        self.last_in_phase = {}
        self.data = open('%s/%s.phases' % (output_path, title), 'w')

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, type, value, traceback):
        self.stop()

    def start(self):
        pass

    def stop(self):
        self.data.close()

    def event(self, experiment, *args):
        t = self.timer.elapsed()
        with self.lock:
            phase = experiment.phase
            if phase not in self.in_phase:
                self.order.append(phase)

            try:
                last_phase = self.last_phase[experiment]
            except KeyError:
                last_phase = None
                pass
            else:
                # No change.
                if last_phase == phase:
                    return
                self.in_phase[last_phase] -= 1
                if self.in_phase[last_phase] == 0:
                    self.last_in_phase[last_phase] = experiment
                self.data.write('%s\tin-%s\t%s\n' % (t, last_phase, self.in_phase[last_phase]))
                self.data.write('%s\tend-%s\n' % (t, last_phase))

            self.in_phase.setdefault(phase, 0)
            self.in_phase[phase] += 1
            self.data.write('%s\tstart-%s\n' % (t, phase))
            self.data.write('%s\tin-%s\t%s\n' % (t, phase, self.in_phase[phase]))
            self.last_in_phase.pop(phase, None)
            self.last_phase[experiment] = phase

        #self.print_progress()

    def print_progress(self):
        with self.lock, PRINT_LOCK:
            status_line()
            print '       RUNNING: ',
            print 't+%.1fs' % self.timer.elapsed(), '\t',
            for phase in self.order:
                print phase.replace('create:', ''), '%-3d' % self.in_phase[phase], 
            print ' ',
            sys.stdout.flush()


    def report(self):
        self.print_progress()
        with PRINT_LOCK:
            # One newline to clear the status line .
            print 
            return
            fmtstr = '%%-%ds %%-10s' % (max([len('PHASE')] + map(len, self.order)))
            print fmtstr % ('PHASE', 'LAST')
            for phase in self.order:
                try:
                    last = self.last_in_phase[phase]
                except KeyError:
                    last_id = '<still active or none exited>'
                else:
                    if last.instance == None:
                        last_id = '-'
                    else:
                        last_id = last.instance.id
                print fmtstr % (phase, last_id)

class PeriodicCaller(Thread):
    def __init__(self, period, func, *args, **kwargs):
        super(PeriodicCaller, self).__init__(name='Periodic Caller')
        self.__period = period
        self.__callee = lambda: func(*args, **kwargs)
        self.__stopped = False
        self.__cond = Condition()

    def run(self):
        with self.__cond:
            while True:
                self.__cond.wait(timeout=self.__period)
                if self.__stopped:
                    break
                self.__callee()

    def stop(self):
        with self.__cond:
            self.__stopped = True
            self.__cond.notify()

class Phase(object):
    def __init__(self, timer):
        self.name = timer.name
        self.start = timer.start_time
        self.duration = timer.elapsed()
        self.end = self.start + self.duration

class PhaseError(Exception):
    pass

class Tail(object):
    def __init__(self, path, from_beginning=False):
        self.path = path
        args = ['tail']
        if from_beginning:
            args.extend(['-c', '+0'])
        args.extend(['-f', path])
        self.__p = Popen(args, stdout=PIPE, close_fds=True)

    def readline(self):
        return self.__p.stdout.readline()

    def stop(self):
        self.__p.kill()
        self.__p.wait()
        self.__p.stdout.close()
        self.__p = None

class InstanceCreator(object):
    def __init__(self, nova, nova_op, total, name_prefix,
                 image, key_name, flavor):
        self.nova = nova
        self.total = total
        self.name_prefix = name_prefix
        self.nova_op = nova_op
        self.image = image
        self.key_name = key_name
        self.flavor = flavor
        self.__created = 0
        self._cond = Condition()

    @staticmethod
    def create(nova, args):
        if args.multi:
            cls = MultiInstanceCreator
        else:
            cls = SingleInstanceCreator

        if args.op == 'boot':
            func = nova.boot
            image = args.image
        elif args.op == 'launch':
            func = nova.live_image_start
            image = args.live_image
        else:
            raise ValueError(args.op)

        return cls(nova, func, args.n, '%s-%s' % (args.name_prefix, args.op),
                   image, args.key_name, args.flavor)

    def _do_nova_op(self, name, num_instances):
        return self.nova_op(name, self.image, self.flavor, self.key_name,
                            num_instances)

    def next_instance(self):
        with self._cond:
            self.__created += 1
            id = self.__created
        assert id <= self.total
        return self._next_instance(id)

class SingleInstanceCreator(InstanceCreator):
    def _next_instance(self, id):
        name = '%s-%d-of-%d' % (self.name_prefix, id, self.total)
        return self._do_nova_op(name, num_instances=1)

class MultiInstanceCreator(InstanceCreator):
    __state = 'init'

    def _next_instance(self, id):
        with self._cond:
            if self.__state == 'init':
                try:
                    self.__state = 'waiting'
                    self.__seen = set()
                    self.__available = set()
                    self.__multi_prefix = 'multi-%d' % random.randint(0, 10000)
                    i = self._do_nova_op(self.__multi_prefix, num_instances=self.total)
                    self.__seen = set([i.id])
                    self.__available = set([i.id])
                    self.__state = 'done'
                except:
                    self.__state = 'error'
                    raise
                finally:
                    self._cond.notify_all()

            while self.__state is 'waiting':
                self._cond.wait()

            if self.__state is 'error':
                raise Exception()

            while not self.__available:
                for instance in self.nova.list():
                    if instance.id not in self.__seen:
                        self.__seen.add(instance.id)
                        self.__available.add(instance.id)

            return Instance(self.nova, self.__available.pop())

class Experiment(object):
    def __init__(self, args, nova, creator):
        self.args = args
        self.creator = creator
        self.timer = Timer('total')
        self.phase = 'setup'
        self.listeners = []
        self.nova = nova
        self.phases = []
        self.instance = None
        self.console_tail = None
        self.__instance_port = None

        for name in vars(self.args):
            if name.startswith('check_syslog') and getattr(self.args, name):
                self.syslog_tail = Tail('/var/log/syslog', False)
                break
        else:
            self.syslog_tail = None

        if args.netns is not None:
            self.netns_exec = ['sudo', 'ip', 'netns', 'exec', args.netns]
        else:
            self.netns_exec = []

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

    def phase_error(self, msg):
        raise PhaseError('Error during phase %s for instance %s: %s' % 
                         (self.phase, self.instance.id, msg))

    def run(self):
        try:
            self.__create()
            if self.args.check_dhcp_hosts:
                self.__check_dhcp_hosts()
            if self.args.check_syslog_ovsvsctl:
                self.__check_syslog_ovsvsctl()
            if self.args.check_console_boot:
                self.__check_console_boot()
            if self.args.check_iptables:
                self.__check_iptables()
            if self.args.check_syslog_dhcp:
                self.__check_syslog_dhcp()
            if self.args.check_console_dhcp:
                self.__check_console_dhcp()
            if self.args.check_ping:
                self.__check_ping()
            if self.args.check_nmap:
                self.__check_nmap()
            if self.args.check_ssh:
                self.__check_ssh()
            if self.args.delete:
                self.__delete()
            self.start_phase('fin')
        finally:
            if self.console_tail != None:
                self.console_tail.stop()
            if self.syslog_tail != None:
                self.syslog_tail.stop()

    def boot_op(self, name):
        return self.nova.boot(name, self.args.image,
                              self.args.flavor, self.args.key_name)

    def launch_op(self, name):
        return self.nova.live_image_start(name, self.args.live_image,
                                          self.args.key_name)

    def __create(self):
        self.start_phase('create:api')
        self.instance = self.creator.next_instance()
        self.start_phase('create:none')
        last_task_state = None
        while True:
            self.instance.refetch()

            task_state = self.instance.task_state
            if task_state != last_task_state and task_state is not None:
                self.start_phase('create:%s' % task_state)
                last_task_state = task_state

            status = self.instance.status
            assert status != 'ERROR'
            if status == 'ACTIVE':
                break

    def __delete(self):
        self.start_phase('delete_api')
        self.instance.delete()
        self.start_phase('delete')
        while True:
            try:
                assert self.instance.fetch_status() != 'ERROR'
            except InstanceDoesNotExistError:
                break

    def __check_dhcp_hosts(self):
        self.start_phase('dhcp_hosts')
        ip = self.__instance_ip()
        while True:
            with open(self.args.check_dhcp_hosts) as f:
                if ip in f.read():
                    break
            time.sleep(1)

    def __check_iptables(self):
        self.start_phase('iptables')
        prefix = self.__instance_port_uuid_prefix()
        regex = re.compile('%s.*--[ds]port 6[78]' % re.escape(prefix))
        while True:
            out = check_output(['sudo', 'iptables-save'])
            if regex.search(out):
                break
            time.sleep(1)

    def __check_tail(self, tail, regex):
        if isinstance(regex, basestring):
            regex = re.compile(regex)
        while True:
            line = tail.readline()
            if line == '':
                self.phase_error('%s not in %s' % (regex.pattern, tail.path))
            if regex.search(line):
                break

    def __check_console(self, regex):
        if self.console_tail == None:
            self.console_tail =\
                Tail(os.path.join(self.instance.console_log_path), True)
        self.__check_tail(self.console_tail, regex)

    def __check_syslog(self, regex):
        self.__check_tail(self.syslog_tail, regex)

    def __check_syslog_dhcp(self):
        self.start_phase('syslog_dhcp')
        self.__check_syslog('DHCPDISCOVER.*%s' %
                            re.escape(self.__instance_mac()))

    def __check_syslog_ovsvsctl(self):
        self.start_phase('syslog_ovsvsctl')
        self.__check_syslog('ovs-vsctl.*%s' % re.escape(self.__instance_mac()))

    def __check_console_boot(self):
        self.start_phase('console_boot')
        self.__check_console('Sending discover...')

    def __check_console_dhcp(self):
        self.start_phase('console_dhcp')
        self.__check_console(re.escape(self.__instance_ip()))

    def __check_nmap(self):
        self.start_phase('nmap')

    def __get_instance_port(self):
        if self.__instance_port is None:
            get = lambda: self.instance.get_port(self.args.network)
            try:
                self.__instance_port = get()
            except InstanceHasNoIpError:
                self.instance.refetch()
                self.__instance_port = get()
        return self.__instance_port

    def __instance_ip(self):
        return self.__get_instance_port().ip

    def __instance_mac(self):
        return self.__get_instance_port().mac

    def __instance_port_uuid_prefix(self):
        uuid = self.instance.fetch_port_uuid(self.args.network)
        return uuid.partition('-')[0]

    def __check_ping(self):
        self.start_phase('ping')
        while True:
            p = Popen(self.netns_exec +
                      ['ping', '-c', '1', '-w', '1', self.__instance_ip()],
                      stdout=DEV_NULL,
                      close_fds=True)
            if p.wait() == 0:
                break

    def __check_ssh(self):
        self.start_phase('ssh')

        args = list(self.netns_exec)
        args.extend([
            'ssh',
            '-l', self.args.check_ssh_user,
            '-o', 'UserKnownHostsFile=/dev/null',
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'PasswordAuthentication=no',
        ])
        if self.args.check_ssh_key is not None:
            args.extend(['-i', self.args.check_ssh_key])
        args.extend([
            self.__instance_ip(),
            self.args.check_ssh_command,
        ])

        while True:
            p = Popen(args, stdout=DEV_NULL, stderr=DEV_NULL, close_fds=True)
            if p.wait() == 0:
                break
            time.sleep(1)

class ParallelExperiment(object):
    def __init__(self, args, atop, nova, title, creator, output_path):
        self.creator = creator
        self.args = args
        self.atop = atop
        self.nova = nova
        self.title = title
        self.output_path = output_path

    def run(self):
        threads = []
        timer = Timer('total')
        log = PhaseLog(timer, self.title, self.output_path)
        progress_thread = PeriodicCaller(1, log.print_progress)
        timer.start()
        with self.atop, log:
            log.print_progress()
            progress_thread.start()
            for i in range(self.args.n):
                experiment = Experiment(self.args, self.nova, self.creator)
                experiment.add_listener(log.event)
                thread = Thread(target=experiment.run,
                                name='experiment %d' % (i + 1))
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
            progress_thread.stop()
            progress_thread.join()
        log.report()

class ArgumentParser(argparse.ArgumentParser):
    def add_bool_arg(self, yes_arg, default=False, help=None):
        assert yes_arg[:2] == '--'
        arg_name = yes_arg[2:]
        no_arg = '--no-%s' % arg_name
        dest = arg_name.replace('-', '_')
        self.add_argument(yes_arg, dest=dest, action='store_true',
                          help=help)
        self.add_argument(no_arg, dest=dest, action='store_false',
                          help='Disables %s' % yes_arg)
        self.set_defaults(**{dest: default})

class ArgumentLoader(object):
    def __init__(self):
        self.__option_parser = self.__create_option_parser()
        self.__arg_parser = self.__create_arg_parser()
        self.__args = argparse.Namespace()

    def add_argv(self, argv):
        '''Adds options from command line.'''
        self.__arg_parser.parse_args(argv[1:], self.__args)

    def add_rc(self, path):
        '''Adds path if it exists. Only parses optional arguments.'''
        try:
            fp = open(path)
        except IOError:
            return
        else:
            with fp:
                i = 0
                for line in fp:
                    i += 1
                    line = line.partition('#')[0]
                    try:
                        self.__option_parser.parse_args(shlex.split(line),
                                                        self.__args)
                    except SystemExit:
                        sys.stderr.write('in %s:%d:\n' %
                                         (os.path.abspath(path), i))
                        sys.stderr.write('    %s\n' % line.partition('\n')[0])
                        raise

    def load(self):
        def arg_error(msg):
            sys.stderr.write('%s: error: %s\n' % (sys.argv[0], msg))

        if not self.__args.nova_instances_path:
            for var in ['check_console_dhcp', 'check_console_boot']:
                if getattr(self.__args, var):
                    arg = '--%s' % var.replace('_', '-')
                    arg_error('%s: requires --nova-instances-path' % arg)
                    sys.exit(1)

        if self.__args.op == 'boot' and self.__args.image is None:
            arg_error('boot: requires --image')

        if self.__args.op == 'launch' and self.__args.live_image is None:
            arg_error('launch: requires --live-image')

        return self.__args

    @classmethod
    def __create_arg_parser(cls):
        parser = cls.__create_option_parser()
        parser.add_argument('op', choices=['boot', 'launch'])
        parser.add_argument('n', type=int, default=1)
        return parser

    @classmethod
    def __create_option_parser(cls):
        parser = ArgumentParser()
        parser.add_argument('--image', default=None)
        parser.add_argument('--live-image', default=None)
        parser.add_argument('--nova-instances-path', type=str, default=None)
        parser.add_argument('--flavor', default='m1.tiny')
        parser.add_argument('--key-name', default=None)
        parser.add_argument('--name-prefix', default='prof')
        parser.add_argument('--check-dhcp-hosts', type=str, default=None)
        parser.add_bool_arg('--check-console-dhcp',
                            help='note: requires --nova-instances-path')
        parser.add_bool_arg('--check-console-boot',
                            help='note: requires --nova-instances-path')
        parser.add_bool_arg('--check-syslog-ovsvsctl', default=False)
        parser.add_bool_arg('--check-syslog-dhcp', default=False)
        parser.add_bool_arg('--check-iptables', default=False)
        parser.add_bool_arg('--check-ssh', default=False)
        parser.add_argument('--check-ssh-user', default='ubuntu')
        parser.add_argument('--check-ssh-command', default='true')
        parser.add_argument('--check-ssh-key', default=None)
        parser.add_bool_arg('--check-ping', default=False)
        parser.add_argument('--check-nmap', type=int, default=None)
        parser.add_bool_arg('--atop', default=False)
        parser.add_argument('--atop-interval', type=int, default=2)
        parser.add_bool_arg('--debug', default=False)
        parser.add_argument('--simple-list', default=False)
        parser.add_argument('--client-pool-size', type=int, default=None)
        parser.add_bool_arg('--delete', default=True)
        parser.add_argument('--out', dest='output_path', default='.')
        parser.add_argument('--netns', default=None)
        parser.add_argument('--network', default=None)
        parser.add_argument('--multi', action='store_true')
        return parser

def load_args():
    loader = ArgumentLoader()
    if 'HOME' in os.environ:
        loader.add_rc(os.path.join(os.environ['HOME'], '.smrc'))
    loader.add_rc('.smrc')
    loader.add_argv(sys.argv)
    return loader.load()

def setup_faulthandler(args):
    try:
        import faulthandler
    except ImportError:
        sys.stderr.write('running without faulthandler\n')
        return
    else:
        faulthandler.enable()
        faulthandler.register(signal.SIGINT)

def main():
    args = load_args()

    setup_faulthandler(args)

    if args.client_pool_size == None:
        args.client_pool_size = args.n

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
        os.environ['NOVACLIENT_DEBUG'] = '1'

    title = ('%s-%s@%s' % (args.op, args.n, timestamp())).replace(' ', '-')

    if args.atop:
        atop = Atop(title, args.atop_interval, args.output_path)
    else:
        atop = NullAtop()

    client_factory =\
        SharedTokenClientFactory(username=os.environ['OS_USERNAME'],
                                 password=os.environ['OS_PASSWORD'],
                                 tenant_name=os.environ.get('OS_TENANT_NAME'),
                                 tenant_id=os.environ.get('OS_TENANT_ID'),
                                 auth_url=os.environ['OS_AUTH_URL'])
    status_line('AUTHENTICATING: ...')
    sys.stdout.flush()
    client_factory.prepare()

    nova = Nova(client_factory,
                simple_list=args.simple_list,
                instances_path=args.nova_instances_path)
    creator = InstanceCreator.create(nova, args)
    experiment = ParallelExperiment(args, atop=atop, nova=nova, title=title,
                                    creator=creator, output_path=args.output_path)
    experiment.run()

if __name__ == '__main__':
    main()
