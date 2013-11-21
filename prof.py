#!/usr/bin/env python

import argparse
import datetime
import json
import logging
import os
import os
import signal
import sys
import time

from subprocess import Popen, PIPE
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

class Instance(object):
    def __init__(self, nova, id):
        self.__nova = nova
        self.__id = id

    @property
    def id(self):
        return self.__id

    @property
    def __path(self):
        return os.path.join(self.__nova.instances_path, self.__id)

    @property
    def console_log_path(self):
        return os.path.join(self.__path, 'console.log')

    def delete(self):
        self.__nova.delete(self.__id)

    def __get(self):
        return self.__nova.show(self.__id)

    def any_ip(self):
        for addrs in self.__get().networks.itervalues():
            if len(addrs) > 0:
                return addrs[0]
        raise InstanceHasNoIpError(self.__id)

    def get_ip(self, network):
        if network is None:
            return self.any_ip()
        else:
            ips = self.__get().networks.get(network, [])
            try:
                return ips[0]
            except IndexError:
                raise InstanceHasNoIpError(self.__id)

    def get_status(self):
        return self.__get().status

    def __repr__(self):
        return 'Instance(%r, %r)' % (self.__nova, self.__id)

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
                                        auth_url='/v2/')
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

    def boot(self, name, image, flavor, key_name=None):
        instance = self.__client.servers.create(name=name,
                                                image=image,
                                                flavor=flavor,
                                                key_name=key_name)
        return Instance(self, instance.id)

    def live_image_start(self, name, image):
        instances = self.__client.cobalt.start_live_image(server=image,
                                                          name=name)
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
                             str(self.interval)])

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

        self.print_progress()

    def print_progress(self):
        with self.lock, PRINT_LOCK:
            status_line()
            print '       RUNNING: ',
            print 't+%.1fs' % self.timer.elapsed(), '\t',
            for phase in self.order:
                print phase, '%-3d' % self.in_phase[phase], 
            print ' ',
            sys.stdout.flush()


    def report(self):
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
        self.console_tail = None
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
            if self.args.check_iptables:
                self.__check_iptables()
            if self.args.check_console_boot:
                self.__check_console_boot()
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
                self.console_tail.kill()
                self.console_tail.wait()

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

    def __check_console(self, string):
        if self.console_tail == None:
            self.console_tail =\
                Popen(['tail', '-c', '+0',
                               '-f', self.instance.console_log_path],
                      stdout=PIPE)
        while True:
            line = self.console_tail.stdout.readline()
            if line == '':
                self.phase_error('%s not in console log %s' %
                                 (string, self.instance.console_log_path))
            if string in line:
                break

    def __check_console_boot(self):
        self.start_phase('console_boot')
        self.__check_console('initramfs: up at')

    def __check_console_dhcp(self):
        self.start_phase('console_dhcp')
        self.__check_console(self.__instance_ip())

    def __check_dhcp_console(self):
        self.start_phase('dhcp')
        ip = self.__instance_ip()
        p = Popen(['tail', '-c', '+0', '-f', self.instance.console_log_path],
                  stdout=PIPE)
        try:
            while True:
                line = p.stdout.readline()
                if line == '':
                    self.phase_error('ip address %s not in console log %s' %
                                    (ip, self.instance.console_log_path))
                if ip in line:
                    break
        finally:
            p.kill()
            p.wait()

    def __check_nmap(self):
        self.start_phase('nmap')

    def __instance_ip(self):
        return self.instance.get_ip(self.args.network)

    def __check_ping(self):
        self.start_phase('ping')
        while True:
            cmd = self.netns_exec + ['ping', '-c', '1', self.__instance_ip()]
            p = Popen(self.netns_exec +
                      ['ping', '-c', '1', self.__instance_ip()],
                      stdout=DEV_NULL)
            if p.wait() == 0:
                break

    def __check_ssh(self):
        self.start_phase('ssh')
        while True:
            p = Popen(self.netns_exec +
                      ['ssh',
                       '-l', self.args.check_ssh_user,
                       '-o', 'UserKnownHostsFile=/dev/null',
                       '-o', 'StrictHostKeyChecking=no',
                       '-o', 'PasswordAuthentication=no',
                       self.__instance_ip(),
                       self.args.check_ssh_command],
                       stdout=DEV_NULL,
                       stderr=DEV_NULL)
            if p.wait() == 0:
                break
            time.sleep(1)

class ParallelExperiment(object):
    def __init__(self, args, atop, nova, title, output_path):
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
                experiment = Experiment('%s-%s-of-%s' % (self.args.name_prefix,
                                                         i + 1, self.args.n),
                                        self.args, self.nova)
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

def parse_argv(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument('op', choices=['boot', 'launch'])
    parser.add_argument('n', type=int, default=1, nargs='?')
    parser.add_argument('--image',
                        default='precise-server-cloudimg-amd64-disk1.img')
    parser.add_argument('--nova-instances-path', type=str, default=None)
    parser.add_argument('--flavor', default='m1.tiny')
    parser.add_argument('--key-name', default=None)
    parser.add_argument('--name-prefix', default='prof')
    parser.add_argument('--check-dhcp-hosts', type=str, default=None)
    parser.add_argument('--check-console-dhcp', action='store_true',
                        help='note: requires --nova-instances-path')
    parser.add_argument('--check-console-boot', action='store_true',
                        help='note: requires --nova-instances-path')
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
    parser.add_argument('--out', dest='output_path', default='.')
    parser.add_argument('--netns', default=None)
    parser.add_argument('--network', default=None)

    args = parser.parse_args(argv[1:])

    def arg_error(msg):
        sys.stderr.write('%s: error: %s\n' % (sys.argv[0], msg))

    if not args.nova_instances_path:
        require = []
        for arg in ['check_console_dhcp', 'check_console_boot']:
            if args.getattr(arg):
                require.append(arg)
        if len(require) > 0:
            arg_error('need --nova-instances-path for: ' %
                      ', '.join(['--%s' % arg.replace('_', '-')
                                 for arg in require]))
            sys.exit(1)

    return args

def setup_faulthandler(args):
    try:
        import faulthandler
    except ImportError:
        sys.stderr.write('running without faulthandler\n')
        return
    else:
        faulthandler.enable()
        faulthandler.register(signal.SIGINT)

def main(argv):
    args = parse_argv(argv)

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
    experiment = ParallelExperiment(args, atop=atop, nova=nova, title=title,
                                    output_path=args.output_path)
    experiment.run()

if __name__ == '__main__':
    main(sys.argv)
