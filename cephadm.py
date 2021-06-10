#!/usr/bin/python3

import asyncio
import asyncio.subprocess
import argparse
import datetime
import fcntl
import ipaddress
import json
import logging
from logging.config import dictConfig
import os
import platform
import pwd
import random
import shlex
import shutil
import socket
import string
import subprocess
import sys
import tempfile
import time
import errno
import struct
from socketserver import ThreadingMixIn
from http.server import BaseHTTPRequestHandler, HTTPServer
import signal
import io
from contextlib import redirect_stdout
import ssl
from enum import Enum

from typing import Dict, List, Tuple, Optional, Union, Any, NoReturn, Callable, IO

import re
import uuid

from configparser import ConfigParser
from functools import wraps
from glob import glob
from io import StringIO
from threading import Thread, RLock
from urllib.error import HTTPError
from urllib.request import urlopen
from pathlib import Path

# Default container images -----------------------------------------------------
DEFAULT_IMAGE = 'quay.ceph.io/ceph-ci/ceph:master'
DEFAULT_IMAGE_IS_MASTER = True
DEFAULT_IMAGE_RELEASE = 'quincy'
DEFAULT_PROMETHEUS_IMAGE = 'docker.io/prom/prometheus:v2.18.1'
DEFAULT_NODE_EXPORTER_IMAGE = 'docker.io/prom/node-exporter:v0.18.1'
DEFAULT_GRAFANA_IMAGE = 'docker.io/ceph/ceph-grafana:6.7.4'
DEFAULT_ALERT_MANAGER_IMAGE = 'docker.io/prom/alertmanager:v0.20.0'
DEFAULT_REGISTRY = 'docker.io'   # normalize unqualified digests to this
# ------------------------------------------------------------------------------

LATEST_STABLE_RELEASE = 'pacific'
DATA_DIR = '/var/lib/ceph'
LOG_DIR = '/var/log/ceph'
LOCK_DIR = '/run/cephadm'
LOGROTATE_DIR = '/etc/logrotate.d'
SYSCTL_DIR = '/usr/lib/sysctl.d'
UNIT_DIR = '/etc/systemd/system'
LOG_DIR_MODE = 0o770
DATA_DIR_MODE = 0o700
CONTAINER_INIT = True
MIN_PODMAN_VERSION = (2, 0, 2)
CGROUPS_SPLIT_PODMAN_VERSION = (2, 1, 0)
CUSTOM_PS1 = r'[ceph: \u@\h \W]\$ '
DEFAULT_TIMEOUT = None  # in seconds
DEFAULT_RETRY = 15
SHELL_DEFAULT_CONF = '/etc/ceph/ceph.conf'
SHELL_DEFAULT_KEYRING = '/etc/ceph/ceph.client.admin.keyring'
DATEFMT = '%Y-%m-%dT%H:%M:%S.%fZ'

logger: logging.Logger = None  # type: ignore

"""
You can invoke cephadm in two ways:

1. The normal way, at the command line.

2. By piping the script to the python3 binary.  In this latter case, you should
   prepend one or more lines to the beginning of the script.

   For arguments,

       injected_argv = [...]

   e.g.,

       injected_argv = ['ls']

   For reading stdin from the '--config-json -' argument,

       injected_stdin = '...'
"""
cached_stdin = None

##################################


class BaseConfig:

    def __init__(self):
        self.image: str = ''
        self.docker: bool = False
        self.data_dir: str = DATA_DIR
        self.log_dir: str = LOG_DIR
        self.logrotate_dir: str = LOGROTATE_DIR
        self.sysctl_dir: str = SYSCTL_DIR
        self.unit_dir: str = UNIT_DIR
        self.verbose: bool = False
        self.timeout: Optional[int] = DEFAULT_TIMEOUT
        self.retry: int = DEFAULT_RETRY
        self.env: List[str] = []
        self.memory_request: Optional[int] = None
        self.memory_limit: Optional[int] = None
        self.log_to_journald: Optional[bool] = None

        self.container_init: bool = CONTAINER_INIT
        self.container_engine: Optional[ContainerEngine] = None

    def set_from_args(self, args: argparse.Namespace):
        argdict: Dict[str, Any] = vars(args)
        for k, v in argdict.items():
            if hasattr(self, k):
                setattr(self, k, v)


class CephadmContext:

    def __init__(self):
        self.__dict__['_args'] = None
        self.__dict__['_conf'] = BaseConfig()

    def set_args(self, args: argparse.Namespace) -> None:
        self._conf.set_from_args(args)
        self._args = args

    def has_function(self) -> bool:
        return 'func' in self._args

    def __contains__(self, name: str) -> bool:
        return hasattr(self, name)

    def __getattr__(self, name: str) -> Any:
        if '_conf' in self.__dict__ and hasattr(self._conf, name):
            return getattr(self._conf, name)
        elif '_args' in self.__dict__ and hasattr(self._args, name):
            return getattr(self._args, name)
        else:
            return super().__getattribute__(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if hasattr(self._conf, name):
            setattr(self._conf, name, value)
        elif hasattr(self._args, name):
            setattr(self._args, name, value)
        else:
            super().__setattr__(name, value)


class ContainerEngine:
    def __init__(self):
        self.path = find_program(self.EXE)

    @property
    def EXE(self) -> str:
        raise NotImplementedError()


class Podman(ContainerEngine):
    EXE = 'podman'

    def __init__(self):
        super().__init__()
        self._version = None

    @property
    def version(self):
        if self._version is None:
            raise RuntimeError('Please call `get_version` first')
        return self._version

    def get_version(self, ctx: CephadmContext):
        out, _, _ = call_throws(ctx, [self.path, 'version', '--format', '{{.Client.Version}}'])
        self._version = _parse_podman_version(out)


class Docker(ContainerEngine):
    EXE = 'docker'


CONTAINER_PREFERENCE = (Podman, Docker)  # prefer podman to docker


# Log and console output config
logging_config = {
    'version': 1,
    'disable_existing_loggers': True,
    'formatters': {
        'cephadm': {
            'format': '%(asctime)s %(levelname)s %(message)s'
        },
    },
    'handlers': {
        'console': {
            'level': 'INFO',
            'class': 'logging.StreamHandler',
        },
        'log_file': {
            'level': 'DEBUG',
            'class': 'logging.handlers.WatchedFileHandler',
            'formatter': 'cephadm',
            'filename': '%s/cephadm.log' % LOG_DIR,
        }
    },
    'loggers': {
        '': {
            'level': 'DEBUG',
            'handlers': ['console', 'log_file'],
        }
    }
}


class termcolor:
    yellow = '\033[93m'
    red = '\033[31m'
    end = '\033[0m'


class Error(Exception):
    pass


class TimeoutExpired(Error):
    pass

##################################


class Ceph(object):
    daemons = ('mon', 'mgr', 'mds', 'osd', 'rgw', 'rbd-mirror',
               'crash', 'cephfs-mirror')

##################################


class OSD(object):
    @staticmethod
    def get_sysctl_settings() -> List[str]:
        return [
            '# allow a large number of OSDs',
            'fs.aio-max-nr = 1048576',
            'kernel.pid_max = 4194304',
        ]

##################################


class Monitoring(object):
    """Define the configs for the monitoring containers"""

    port_map = {
        'prometheus': [9095],  # Avoid default 9090, due to conflict with cockpit UI
        'node-exporter': [9100],
        'grafana': [3000],
        'alertmanager': [9093, 9094],
    }

    components = {
        'prometheus': {
            'image': DEFAULT_PROMETHEUS_IMAGE,
            'cpus': '2',
            'memory': '4GB',
            'args': [
                '--config.file=/etc/prometheus/prometheus.yml',
                '--storage.tsdb.path=/prometheus',
            ],
            'config-json-files': [
                'prometheus.yml',
            ],
        },
        'node-exporter': {
            'image': DEFAULT_NODE_EXPORTER_IMAGE,
            'cpus': '1',
            'memory': '1GB',
            'args': [
                '--no-collector.timex',
            ],
        },
        'grafana': {
            'image': DEFAULT_GRAFANA_IMAGE,
            'cpus': '2',
            'memory': '4GB',
            'args': [],
            'config-json-files': [
                'grafana.ini',
                'provisioning/datasources/ceph-dashboard.yml',
                'certs/cert_file',
                'certs/cert_key',
            ],
        },
        'alertmanager': {
            'image': DEFAULT_ALERT_MANAGER_IMAGE,
            'cpus': '2',
            'memory': '2GB',
            'args': [
                '--cluster.listen-address=:{}'.format(port_map['alertmanager'][1]),
            ],
            'config-json-files': [
                'alertmanager.yml',
            ],
            'config-json-args': [
                'peers',
            ],
        },
    }  # type: ignore

    @staticmethod
    def get_version(ctx, container_id, daemon_type):
        # type: (CephadmContext, str, str) -> str
        """
        :param: daemon_type Either "prometheus", "alertmanager" or "node-exporter"
        """
        assert daemon_type in ('prometheus', 'alertmanager', 'node-exporter')
        cmd = daemon_type.replace('-', '_')
        code = -1
        err = ''
        version = ''
        if daemon_type == 'alertmanager':
            for cmd in ['alertmanager', 'prometheus-alertmanager']:
                _, err, code = call(ctx, [
                    ctx.container_engine.path, 'exec', container_id, cmd,
                    '--version'
                ], verbosity=CallVerbosity.DEBUG)
                if code == 0:
                    break
            cmd = 'alertmanager'  # reset cmd for version extraction
        else:
            _, err, code = call(ctx, [
                ctx.container_engine.path, 'exec', container_id, cmd, '--version'
            ], verbosity=CallVerbosity.DEBUG)
        if code == 0 and \
                err.startswith('%s, version ' % cmd):
            version = err.split(' ')[2]
        return version

##################################


def populate_files(config_dir, config_files, uid, gid):
    # type: (str, Dict, int, int) -> None
    """create config files for different services"""
    for fname in config_files:
        config_file = os.path.join(config_dir, fname)
        config_content = dict_get_join(config_files, fname)
        logger.info('Write file: %s' % (config_file))
        with open(config_file, 'w', encoding='utf-8') as f:
            os.fchown(f.fileno(), uid, gid)
            os.fchmod(f.fileno(), 0o600)
            f.write(config_content)


class NFSGanesha(object):
    """Defines a NFS-Ganesha container"""

    daemon_type = 'nfs'
    entrypoint = '/usr/bin/ganesha.nfsd'
    daemon_args = ['-F', '-L', 'STDERR']

    required_files = ['ganesha.conf']

    port_map = {
        'nfs': 2049,
    }

    def __init__(self,
                 ctx,
                 fsid,
                 daemon_id,
                 config_json,
                 image=DEFAULT_IMAGE):
        # type: (CephadmContext, str, Union[int, str], Dict, str) -> None
        self.ctx = ctx
        self.fsid = fsid
        self.daemon_id = daemon_id
        self.image = image

        # config-json options
        self.pool = dict_get(config_json, 'pool', require=True)
        self.namespace = dict_get(config_json, 'namespace')
        self.userid = dict_get(config_json, 'userid')
        self.extra_args = dict_get(config_json, 'extra_args', [])
        self.files = dict_get(config_json, 'files', {})
        self.rgw = dict_get(config_json, 'rgw', {})

        # validate the supplied args
        self.validate()

    @classmethod
    def init(cls, ctx, fsid, daemon_id):
        # type: (CephadmContext, str, Union[int, str]) -> NFSGanesha
        return cls(ctx, fsid, daemon_id, get_parm(ctx.config_json), ctx.image)

    def get_container_mounts(self, data_dir):
        # type: (str) -> Dict[str, str]
        mounts = dict()
        mounts[os.path.join(data_dir, 'config')] = '/etc/ceph/ceph.conf:z'
        mounts[os.path.join(data_dir, 'keyring')] = '/etc/ceph/keyring:z'
        mounts[os.path.join(data_dir, 'etc/ganesha')] = '/etc/ganesha:z'
        if self.rgw:
            cluster = self.rgw.get('cluster', 'ceph')
            rgw_user = self.rgw.get('user', 'admin')
            mounts[os.path.join(data_dir, 'keyring.rgw')] = \
                '/var/lib/ceph/radosgw/%s-%s/keyring:z' % (cluster, rgw_user)
        return mounts

    @staticmethod
    def get_container_envs():
        # type: () -> List[str]
        envs = [
            'CEPH_CONF=%s' % ('/etc/ceph/ceph.conf')
        ]
        return envs

    @staticmethod
    def get_version(ctx, container_id):
        # type: (CephadmContext, str) -> Optional[str]
        version = None
        out, err, code = call(ctx,
                              [ctx.container_engine.path, 'exec', container_id,
                               NFSGanesha.entrypoint, '-v'],
                              verbosity=CallVerbosity.DEBUG)
        if code == 0:
            match = re.search(r'NFS-Ganesha Release\s*=\s*[V]*([\d.]+)', out)
            if match:
                version = match.group(1)
        return version

    def validate(self):
        # type: () -> None
        if not is_fsid(self.fsid):
            raise Error('not an fsid: %s' % self.fsid)
        if not self.daemon_id:
            raise Error('invalid daemon_id: %s' % self.daemon_id)
        if not self.image:
            raise Error('invalid image: %s' % self.image)

        # check for the required files
        if self.required_files:
            for fname in self.required_files:
                if fname not in self.files:
                    raise Error('required file missing from config-json: %s' % fname)

        # check for an RGW config
        if self.rgw:
            if not self.rgw.get('keyring'):
                raise Error('RGW keyring is missing')
            if not self.rgw.get('user'):
                raise Error('RGW user is missing')

    def get_daemon_name(self):
        # type: () -> str
        return '%s.%s' % (self.daemon_type, self.daemon_id)

    def get_container_name(self, desc=None):
        # type: (Optional[str]) -> str
        cname = 'ceph-%s-%s' % (self.fsid, self.get_daemon_name())
        if desc:
            cname = '%s-%s' % (cname, desc)
        return cname

    def get_daemon_args(self):
        # type: () -> List[str]
        return self.daemon_args + self.extra_args

    def create_daemon_dirs(self, data_dir, uid, gid):
        # type: (str, int, int) -> None
        """Create files under the container data dir"""
        if not os.path.isdir(data_dir):
            raise OSError('data_dir is not a directory: %s' % (data_dir))

        logger.info('Creating ganesha config...')

        # create the ganesha conf dir
        config_dir = os.path.join(data_dir, 'etc/ganesha')
        makedirs(config_dir, uid, gid, 0o755)

        # populate files from the config-json
        populate_files(config_dir, self.files, uid, gid)

        # write the RGW keyring
        if self.rgw:
            keyring_path = os.path.join(data_dir, 'keyring.rgw')
            with open(keyring_path, 'w') as f:
                os.fchmod(f.fileno(), 0o600)
                os.fchown(f.fileno(), uid, gid)
                f.write(self.rgw.get('keyring', ''))

##################################


class CephIscsi(object):
    """Defines a Ceph-Iscsi container"""

    daemon_type = 'iscsi'
    entrypoint = '/usr/bin/rbd-target-api'

    required_files = ['iscsi-gateway.cfg']

    def __init__(self,
                 ctx,
                 fsid,
                 daemon_id,
                 config_json,
                 image=DEFAULT_IMAGE):
        # type: (CephadmContext, str, Union[int, str], Dict, str) -> None
        self.ctx = ctx
        self.fsid = fsid
        self.daemon_id = daemon_id
        self.image = image

        # config-json options
        self.files = dict_get(config_json, 'files', {})

        # validate the supplied args
        self.validate()

    @classmethod
    def init(cls, ctx, fsid, daemon_id):
        # type: (CephadmContext, str, Union[int, str]) -> CephIscsi
        return cls(ctx, fsid, daemon_id,
                   get_parm(ctx.config_json), ctx.image)

    @staticmethod
    def get_container_mounts(data_dir, log_dir):
        # type: (str, str) -> Dict[str, str]
        mounts = dict()
        mounts[os.path.join(data_dir, 'config')] = '/etc/ceph/ceph.conf:z'
        mounts[os.path.join(data_dir, 'keyring')] = '/etc/ceph/keyring:z'
        mounts[os.path.join(data_dir, 'iscsi-gateway.cfg')] = '/etc/ceph/iscsi-gateway.cfg:z'
        mounts[os.path.join(data_dir, 'configfs')] = '/sys/kernel/config'
        mounts[log_dir] = '/var/log/rbd-target-api:z'
        mounts['/dev'] = '/dev'
        return mounts

    @staticmethod
    def get_container_binds():
        # type: () -> List[List[str]]
        binds = []
        lib_modules = ['type=bind',
                       'source=/lib/modules',
                       'destination=/lib/modules',
                       'ro=true']
        binds.append(lib_modules)
        return binds

    @staticmethod
    def get_version(ctx, container_id):
        # type: (CephadmContext, str) -> Optional[str]
        version = None
        out, err, code = call(ctx,
                              [ctx.container_engine.path, 'exec', container_id,
                               '/usr/bin/python3', '-c', "import pkg_resources; print(pkg_resources.require('ceph_iscsi')[0].version)"],
                              verbosity=CallVerbosity.DEBUG)
        if code == 0:
            version = out.strip()
        return version

    def validate(self):
        # type: () -> None
        if not is_fsid(self.fsid):
            raise Error('not an fsid: %s' % self.fsid)
        if not self.daemon_id:
            raise Error('invalid daemon_id: %s' % self.daemon_id)
        if not self.image:
            raise Error('invalid image: %s' % self.image)

        # check for the required files
        if self.required_files:
            for fname in self.required_files:
                if fname not in self.files:
                    raise Error('required file missing from config-json: %s' % fname)

    def get_daemon_name(self):
        # type: () -> str
        return '%s.%s' % (self.daemon_type, self.daemon_id)

    def get_container_name(self, desc=None):
        # type: (Optional[str]) -> str
        cname = 'ceph-%s-%s' % (self.fsid, self.get_daemon_name())
        if desc:
            cname = '%s-%s' % (cname, desc)
        return cname

    def create_daemon_dirs(self, data_dir, uid, gid):
        # type: (str, int, int) -> None
        """Create files under the container data dir"""
        if not os.path.isdir(data_dir):
            raise OSError('data_dir is not a directory: %s' % (data_dir))

        logger.info('Creating ceph-iscsi config...')
        configfs_dir = os.path.join(data_dir, 'configfs')
        makedirs(configfs_dir, uid, gid, 0o755)

        # populate files from the config-json
        populate_files(data_dir, self.files, uid, gid)

    @staticmethod
    def configfs_mount_umount(data_dir, mount=True):
        # type: (str, bool) -> List[str]
        mount_path = os.path.join(data_dir, 'configfs')
        if mount:
            cmd = 'if ! grep -qs {0} /proc/mounts; then ' \
                  'mount -t configfs none {0}; fi'.format(mount_path)
        else:
            cmd = 'if grep -qs {0} /proc/mounts; then ' \
                  'umount {0}; fi'.format(mount_path)
        return cmd.split()

    def get_tcmu_runner_container(self):
        # type: () -> CephContainer
        tcmu_container = get_container(self.ctx, self.fsid, self.daemon_type, self.daemon_id)
        tcmu_container.entrypoint = '/usr/bin/tcmu-runner'
        tcmu_container.cname = self.get_container_name(desc='tcmu')
        # remove extra container args for tcmu container.
        # extra args could cause issue with forking service type
        tcmu_container.container_args = []
        return tcmu_container

##################################


class HAproxy(object):
    """Defines an HAproxy container"""
    daemon_type = 'haproxy'
    required_files = ['haproxy.cfg']
    default_image = 'haproxy'

    def __init__(self,
                 ctx: CephadmContext,
                 fsid: str, daemon_id: Union[int, str],
                 config_json: Dict, image: str) -> None:
        self.ctx = ctx
        self.fsid = fsid
        self.daemon_id = daemon_id
        self.image = image

        # config-json options
        self.files = dict_get(config_json, 'files', {})

        self.validate()

    @classmethod
    def init(cls, ctx: CephadmContext,
             fsid: str, daemon_id: Union[int, str]) -> 'HAproxy':
        return cls(ctx, fsid, daemon_id, get_parm(ctx.config_json),
                   ctx.image)

    def create_daemon_dirs(self, data_dir: str, uid: int, gid: int) -> None:
        """Create files under the container data dir"""
        if not os.path.isdir(data_dir):
            raise OSError('data_dir is not a directory: %s' % (data_dir))

        # create additional directories in data dir for HAproxy to use
        if not os.path.isdir(os.path.join(data_dir, 'haproxy')):
            makedirs(os.path.join(data_dir, 'haproxy'), uid, gid, DATA_DIR_MODE)

        data_dir = os.path.join(data_dir, 'haproxy')
        populate_files(data_dir, self.files, uid, gid)

    def get_daemon_args(self) -> List[str]:
        return ['haproxy', '-f', '/var/lib/haproxy/haproxy.cfg']

    def validate(self):
        # type: () -> None
        if not is_fsid(self.fsid):
            raise Error('not an fsid: %s' % self.fsid)
        if not self.daemon_id:
            raise Error('invalid daemon_id: %s' % self.daemon_id)
        if not self.image:
            raise Error('invalid image: %s' % self.image)

        # check for the required files
        if self.required_files:
            for fname in self.required_files:
                if fname not in self.files:
                    raise Error('required file missing from config-json: %s' % fname)

    def get_daemon_name(self):
        # type: () -> str
        return '%s.%s' % (self.daemon_type, self.daemon_id)

    def get_container_name(self, desc=None):
        # type: (Optional[str]) -> str
        cname = 'ceph-%s-%s' % (self.fsid, self.get_daemon_name())
        if desc:
            cname = '%s-%s' % (cname, desc)
        return cname

    def extract_uid_gid_haproxy(self):
        # better directory for this?
        return extract_uid_gid(self.ctx, file_path='/var/lib')

    @staticmethod
    def get_container_mounts(data_dir: str) -> Dict[str, str]:
        mounts = dict()
        mounts[os.path.join(data_dir, 'haproxy')] = '/var/lib/haproxy'
        return mounts

    @staticmethod
    def get_sysctl_settings() -> List[str]:
        return [
            '# IP forwarding',
            'net.ipv4.ip_forward = 1',
        ]

##################################


class Keepalived(object):
    """Defines an Keepalived container"""
    daemon_type = 'keepalived'
    required_files = ['keepalived.conf']
    default_image = 'arcts/keepalived'

    def __init__(self,
                 ctx: CephadmContext,
                 fsid: str, daemon_id: Union[int, str],
                 config_json: Dict, image: str) -> None:
        self.ctx = ctx
        self.fsid = fsid
        self.daemon_id = daemon_id
        self.image = image

        # config-json options
        self.files = dict_get(config_json, 'files', {})

        self.validate()

    @classmethod
    def init(cls, ctx: CephadmContext, fsid: str,
             daemon_id: Union[int, str]) -> 'Keepalived':
        return cls(ctx, fsid, daemon_id,
                   get_parm(ctx.config_json), ctx.image)

    def create_daemon_dirs(self, data_dir: str, uid: int, gid: int) -> None:
        """Create files under the container data dir"""
        if not os.path.isdir(data_dir):
            raise OSError('data_dir is not a directory: %s' % (data_dir))

        # create additional directories in data dir for keepalived to use
        if not os.path.isdir(os.path.join(data_dir, 'keepalived')):
            makedirs(os.path.join(data_dir, 'keepalived'), uid, gid, DATA_DIR_MODE)

        # populate files from the config-json
        populate_files(data_dir, self.files, uid, gid)

    def validate(self):
        # type: () -> None
        if not is_fsid(self.fsid):
            raise Error('not an fsid: %s' % self.fsid)
        if not self.daemon_id:
            raise Error('invalid daemon_id: %s' % self.daemon_id)
        if not self.image:
            raise Error('invalid image: %s' % self.image)

        # check for the required files
        if self.required_files:
            for fname in self.required_files:
                if fname not in self.files:
                    raise Error('required file missing from config-json: %s' % fname)

    def get_daemon_name(self):
        # type: () -> str
        return '%s.%s' % (self.daemon_type, self.daemon_id)

    def get_container_name(self, desc=None):
        # type: (Optional[str]) -> str
        cname = 'ceph-%s-%s' % (self.fsid, self.get_daemon_name())
        if desc:
            cname = '%s-%s' % (cname, desc)
        return cname

    @staticmethod
    def get_container_envs():
        # type: () -> List[str]
        envs = [
            'KEEPALIVED_AUTOCONF=false',
            'KEEPALIVED_CONF=/etc/keepalived/keepalived.conf',
            'KEEPALIVED_CMD=/usr/sbin/keepalived -n -l -f /etc/keepalived/keepalived.conf',
            'KEEPALIVED_DEBUG=false'
        ]
        return envs

    @staticmethod
    def get_sysctl_settings() -> List[str]:
        return [
            '# IP forwarding and non-local bind',
            'net.ipv4.ip_forward = 1',
            'net.ipv4.ip_nonlocal_bind = 1',
        ]

    def extract_uid_gid_keepalived(self):
        # better directory for this?
        return extract_uid_gid(self.ctx, file_path='/var/lib')

    @staticmethod
    def get_container_mounts(data_dir: str) -> Dict[str, str]:
        mounts = dict()
        mounts[os.path.join(data_dir, 'keepalived.conf')] = '/etc/keepalived/keepalived.conf'
        return mounts

##################################


class CustomContainer(object):
    """Defines a custom container"""
    daemon_type = 'container'

    def __init__(self,
                 fsid: str, daemon_id: Union[int, str],
                 config_json: Dict, image: str) -> None:
        self.fsid = fsid
        self.daemon_id = daemon_id
        self.image = image

        # config-json options
        self.entrypoint = dict_get(config_json, 'entrypoint')
        self.uid = dict_get(config_json, 'uid', 65534)  # nobody
        self.gid = dict_get(config_json, 'gid', 65534)  # nobody
        self.volume_mounts = dict_get(config_json, 'volume_mounts', {})
        self.args = dict_get(config_json, 'args', [])
        self.envs = dict_get(config_json, 'envs', [])
        self.privileged = dict_get(config_json, 'privileged', False)
        self.bind_mounts = dict_get(config_json, 'bind_mounts', [])
        self.ports = dict_get(config_json, 'ports', [])
        self.dirs = dict_get(config_json, 'dirs', [])
        self.files = dict_get(config_json, 'files', {})

    @classmethod
    def init(cls, ctx: CephadmContext,
             fsid: str, daemon_id: Union[int, str]) -> 'CustomContainer':
        return cls(fsid, daemon_id,
                   get_parm(ctx.config_json), ctx.image)

    def create_daemon_dirs(self, data_dir: str, uid: int, gid: int) -> None:
        """
        Create dirs/files below the container data directory.
        """
        logger.info('Creating custom container configuration '
                    'dirs/files in {} ...'.format(data_dir))

        if not os.path.isdir(data_dir):
            raise OSError('data_dir is not a directory: %s' % data_dir)

        for dir_path in self.dirs:
            logger.info('Creating directory: {}'.format(dir_path))
            dir_path = os.path.join(data_dir, dir_path.strip('/'))
            makedirs(dir_path, uid, gid, 0o755)

        for file_path in self.files:
            logger.info('Creating file: {}'.format(file_path))
            content = dict_get_join(self.files, file_path)
            file_path = os.path.join(data_dir, file_path.strip('/'))
            with open(file_path, 'w', encoding='utf-8') as f:
                os.fchown(f.fileno(), uid, gid)
                os.fchmod(f.fileno(), 0o600)
                f.write(content)

    def get_daemon_args(self) -> List[str]:
        return []

    def get_container_args(self) -> List[str]:
        return self.args

    def get_container_envs(self) -> List[str]:
        return self.envs

    def get_container_mounts(self, data_dir: str) -> Dict[str, str]:
        """
        Get the volume mounts. Relative source paths will be located below
        `/var/lib/ceph/<cluster-fsid>/<daemon-name>`.

        Example:
        {
            /foo/conf: /conf
            foo/conf: /conf
        }
        becomes
        {
            /foo/conf: /conf
            /var/lib/ceph/<cluster-fsid>/<daemon-name>/foo/conf: /conf
        }
        """
        mounts = {}
        for source, destination in self.volume_mounts.items():
            source = os.path.join(data_dir, source)
            mounts[source] = destination
        return mounts

    def get_container_binds(self, data_dir: str) -> List[List[str]]:
        """
        Get the bind mounts. Relative `source=...` paths will be located below
        `/var/lib/ceph/<cluster-fsid>/<daemon-name>`.

        Example:
        [
            'type=bind',
            'source=lib/modules',
            'destination=/lib/modules',
            'ro=true'
        ]
        becomes
        [
            ...
            'source=/var/lib/ceph/<cluster-fsid>/<daemon-name>/lib/modules',
            ...
        ]
        """
        binds = self.bind_mounts.copy()
        for bind in binds:
            for index, value in enumerate(bind):
                match = re.match(r'^source=(.+)$', value)
                if match:
                    bind[index] = 'source={}'.format(os.path.join(
                        data_dir, match.group(1)))
        return binds

##################################


def touch(file_path: str, uid: Optional[int] = None, gid: Optional[int] = None) -> None:
    Path(file_path).touch()
    if uid and gid:
        os.chown(file_path, uid, gid)


##################################


def dict_get(d: Dict, key: str, default: Any = None, require: bool = False) -> Any:
    """
    Helper function to get a key from a dictionary.
    :param d: The dictionary to process.
    :param key: The name of the key to get.
    :param default: The default value in case the key does not
        exist. Default is `None`.
    :param require: Set to `True` if the key is required. An
        exception will be raised if the key does not exist in
        the given dictionary.
    :return: Returns the value of the given key.
    :raises: :exc:`self.Error` if the given key does not exist
        and `require` is set to `True`.
    """
    if require and key not in d.keys():
        raise Error('{} missing from dict'.format(key))
    return d.get(key, default)  # type: ignore

##################################


def dict_get_join(d: Dict, key: str) -> Any:
    """
    Helper function to get the value of a given key from a dictionary.
    `List` values will be converted to a string by joining them with a
    line break.
    :param d: The dictionary to process.
    :param key: The name of the key to get.
    :return: Returns the value of the given key. If it was a `list`, it
        will be joining with a line break.
    """
    value = d.get(key)
    if isinstance(value, list):
        value = '\n'.join(map(str, value))
    return value

##################################


def get_supported_daemons():
    # type: () -> List[str]
    supported_daemons = list(Ceph.daemons)
    supported_daemons.extend(Monitoring.components)
    supported_daemons.append(NFSGanesha.daemon_type)
    supported_daemons.append(CephIscsi.daemon_type)
    supported_daemons.append(CustomContainer.daemon_type)
    supported_daemons.append(CephadmDaemon.daemon_type)
    supported_daemons.append(HAproxy.daemon_type)
    supported_daemons.append(Keepalived.daemon_type)
    assert len(supported_daemons) == len(set(supported_daemons))
    return supported_daemons

##################################


class PortOccupiedError(Error):
    pass


def attempt_bind(ctx, s, address, port):
    # type: (CephadmContext, socket.socket, str, int) -> None
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((address, port))
    except (socket.error, OSError) as e:  # py2 and py3
        if e.errno == errno.EADDRINUSE:
            msg = 'Cannot bind to IP %s port %d: %s' % (address, port, e)
            logger.warning(msg)
            raise PortOccupiedError(msg)
        else:
            raise e
    finally:
        s.close()


def port_in_use(ctx, port_num):
    # type: (CephadmContext, int) -> bool
    """Detect whether a port is in use on the local machine - IPv4 and IPv6"""
    logger.info('Verifying port %d ...' % port_num)

    def _port_in_use(af: socket.AddressFamily, address: str) -> bool:
        try:
            s = socket.socket(af, socket.SOCK_STREAM)
            attempt_bind(ctx, s, address, port_num)
        except PortOccupiedError:
            return True
        except OSError as e:
            if e.errno in (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL):
                # Ignore EAFNOSUPPORT and EADDRNOTAVAIL as two interfaces are
                # being tested here and one might be intentionally be disabled.
                # In that case no error should be raised.
                return False
            else:
                raise e
        return False
    return any(_port_in_use(af, address) for af, address in (
        (socket.AF_INET, '0.0.0.0'),
        (socket.AF_INET6, '::')
    ))


def check_ip_port(ctx, ip, port):
    # type: (CephadmContext, str, int) -> None
    if not ctx.skip_ping_check:
        logger.info('Verifying IP %s port %d ...' % (ip, port))
        if is_ipv6(ip):
            s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            ip = unwrap_ipv6(ip)
        else:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        attempt_bind(ctx, s, ip, port)

##################################


# this is an abbreviated version of
# https://github.com/benediktschmitt/py-filelock/blob/master/filelock.py
# that drops all of the compatibility (this is Unix/Linux only).

class Timeout(TimeoutError):
    """
    Raised when the lock could not be acquired in *timeout*
    seconds.
    """

    def __init__(self, lock_file):
        """
        """
        #: The path of the file lock.
        self.lock_file = lock_file
        return None

    def __str__(self):
        temp = "The file lock '{}' could not be acquired."\
               .format(self.lock_file)
        return temp


class _Acquire_ReturnProxy(object):
    def __init__(self, lock):
        self.lock = lock
        return None

    def __enter__(self):
        return self.lock

    def __exit__(self, exc_type, exc_value, traceback):
        self.lock.release()
        return None


class FileLock(object):
    def __init__(self, ctx: CephadmContext, name, timeout=-1):
        if not os.path.exists(LOCK_DIR):
            os.mkdir(LOCK_DIR, 0o700)
        self._lock_file = os.path.join(LOCK_DIR, name + '.lock')
        self.ctx = ctx

        # The file descriptor for the *_lock_file* as it is returned by the
        # os.open() function.
        # This file lock is only NOT None, if the object currently holds the
        # lock.
        self._lock_file_fd: Optional[int] = None
        self.timeout = timeout
        # The lock counter is used for implementing the nested locking
        # mechanism. Whenever the lock is acquired, the counter is increased and
        # the lock is only released, when this value is 0 again.
        self._lock_counter = 0
        return None

    @property
    def is_locked(self):
        return self._lock_file_fd is not None

    def acquire(self, timeout=None, poll_intervall=0.05):
        """
        Acquires the file lock or fails with a :exc:`Timeout` error.
        .. code-block:: python
            # You can use this method in the context manager (recommended)
            with lock.acquire():
                pass
            # Or use an equivalent try-finally construct:
            lock.acquire()
            try:
                pass
            finally:
                lock.release()
        :arg float timeout:
            The maximum time waited for the file lock.
            If ``timeout < 0``, there is no timeout and this method will
            block until the lock could be acquired.
            If ``timeout`` is None, the default :attr:`~timeout` is used.
        :arg float poll_intervall:
            We check once in *poll_intervall* seconds if we can acquire the
            file lock.
        :raises Timeout:
            if the lock could not be acquired in *timeout* seconds.
        .. versionchanged:: 2.0.0
            This method returns now a *proxy* object instead of *self*,
            so that it can be used in a with statement without side effects.
        """

        # Use the default timeout, if no timeout is provided.
        if timeout is None:
            timeout = self.timeout

        # Increment the number right at the beginning.
        # We can still undo it, if something fails.
        self._lock_counter += 1

        lock_id = id(self)
        lock_filename = self._lock_file
        start_time = time.time()
        try:
            while True:
                if not self.is_locked:
                    logger.debug('Acquiring lock %s on %s', lock_id,
                                 lock_filename)
                    self._acquire()

                if self.is_locked:
                    logger.debug('Lock %s acquired on %s', lock_id,
                                 lock_filename)
                    break
                elif timeout >= 0 and time.time() - start_time > timeout:
                    logger.warning('Timeout acquiring lock %s on %s', lock_id,
                                   lock_filename)
                    raise Timeout(self._lock_file)
                else:
                    logger.debug(
                        'Lock %s not acquired on %s, waiting %s seconds ...',
                        lock_id, lock_filename, poll_intervall
                    )
                    time.sleep(poll_intervall)
        except Exception:
            # Something did go wrong, so decrement the counter.
            self._lock_counter = max(0, self._lock_counter - 1)

            raise
        return _Acquire_ReturnProxy(lock=self)

    def release(self, force=False):
        """
        Releases the file lock.
        Please note, that the lock is only completly released, if the lock
        counter is 0.
        Also note, that the lock file itself is not automatically deleted.
        :arg bool force:
            If true, the lock counter is ignored and the lock is released in
            every case.
        """
        if self.is_locked:
            self._lock_counter -= 1

            if self._lock_counter == 0 or force:
                lock_id = id(self)
                lock_filename = self._lock_file

                logger.debug('Releasing lock %s on %s', lock_id, lock_filename)
                self._release()
                self._lock_counter = 0
                logger.debug('Lock %s released on %s', lock_id, lock_filename)

        return None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()
        return None

    def __del__(self):
        self.release(force=True)
        return None

    def _acquire(self):
        open_mode = os.O_RDWR | os.O_CREAT | os.O_TRUNC
        fd = os.open(self._lock_file, open_mode)

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (IOError, OSError):
            os.close(fd)
        else:
            self._lock_file_fd = fd
        return None

    def _release(self):
        # Do not remove the lockfile:
        #
        #   https://github.com/benediktschmitt/py-filelock/issues/31
        #   https://stackoverflow.com/questions/17708885/flock-removing-locked-file-without-race-condition
        fd = self._lock_file_fd
        self._lock_file_fd = None
        fcntl.flock(fd, fcntl.LOCK_UN)  # type: ignore
        os.close(fd)  # type: ignore
        return None


##################################
# Popen wrappers, lifted from ceph-volume

class CallVerbosity(Enum):
    SILENT = 0
    # log stdout/stderr to logger.debug
    DEBUG = 1
    # On a non-zero exit status, it will forcefully set
    # logging ON for the terminal
    VERBOSE_ON_FAILURE = 2
    # log at info (instead of debug) level.
    VERBOSE = 3


if sys.version_info < (3, 8):
    import itertools
    import threading
    import warnings
    from asyncio import events

    class ThreadedChildWatcher(asyncio.AbstractChildWatcher):
        """Threaded child watcher implementation.
        The watcher uses a thread per process
        for waiting for the process finish.
        It doesn't require subscription on POSIX signal
        but a thread creation is not free.
        The watcher has O(1) complexity, its performance doesn't depend
        on amount of spawn processes.
        """

        def __init__(self):
            self._pid_counter = itertools.count(0)
            self._threads = {}

        def is_active(self):
            return True

        def close(self):
            self._join_threads()

        def _join_threads(self):
            """Internal: Join all non-daemon threads"""
            threads = [thread for thread in list(self._threads.values())
                       if thread.is_alive() and not thread.daemon]
            for thread in threads:
                thread.join()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

        def __del__(self, _warn=warnings.warn):
            threads = [thread for thread in list(self._threads.values())
                       if thread.is_alive()]
            if threads:
                _warn(f'{self.__class__} has registered but not finished child processes',
                      ResourceWarning,
                      source=self)

        def add_child_handler(self, pid, callback, *args):
            loop = events.get_event_loop()
            thread = threading.Thread(target=self._do_waitpid,
                                      name=f'waitpid-{next(self._pid_counter)}',
                                      args=(loop, pid, callback, args),
                                      daemon=True)
            self._threads[pid] = thread
            thread.start()

        def remove_child_handler(self, pid):
            # asyncio never calls remove_child_handler() !!!
            # The method is no-op but is implemented because
            # abstract base classe requires it
            return True

        def attach_loop(self, loop):
            pass

        def _do_waitpid(self, loop, expected_pid, callback, args):
            assert expected_pid > 0

            try:
                pid, status = os.waitpid(expected_pid, 0)
            except ChildProcessError:
                # The child process is already reaped
                # (may happen if waitpid() is called elsewhere).
                pid = expected_pid
                returncode = 255
                logger.warning(
                    'Unknown child process pid %d, will report returncode 255',
                    pid)
            else:
                if os.WIFEXITED(status):
                    returncode = os.WEXITSTATUS(status)
                elif os.WIFSIGNALED(status):
                    returncode = -os.WTERMSIG(status)
                else:
                    raise ValueError(f'unknown wait status {status}')
                if loop.get_debug():
                    logger.debug('process %s exited with returncode %s',
                                 expected_pid, returncode)

            if loop.is_closed():
                logger.warning('Loop %r that handles pid %r is closed', loop, pid)
            else:
                loop.call_soon_threadsafe(callback, pid, returncode, *args)

            self._threads.pop(expected_pid)

    # unlike SafeChildWatcher which handles SIGCHLD in the main thread,
    # ThreadedChildWatcher runs in a separated thread, hence allows us to
    # run create_subprocess_exec() in non-main thread, see
    # https://bugs.python.org/issue35621
    asyncio.set_child_watcher(ThreadedChildWatcher())


try:
    from asyncio import run as async_run   # type: ignore[attr-defined]
except ImportError:
    def async_run(coro):  # type: ignore
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(coro)
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                asyncio.set_event_loop(None)
                loop.close()


def call(ctx: CephadmContext,
         command: List[str],
         desc: Optional[str] = None,
         verbosity: CallVerbosity = CallVerbosity.VERBOSE_ON_FAILURE,
         timeout: Optional[int] = DEFAULT_TIMEOUT,
         **kwargs) -> Tuple[str, str, int]:
    """
    Wrap subprocess.Popen to

    - log stdout/stderr to a logger,
    - decode utf-8
    - cleanly return out, err, returncode

    :param timeout: timeout in seconds
    """

    prefix = command[0] if desc is None else desc
    if prefix:
        prefix += ': '
    timeout = timeout or ctx.timeout

    logger.debug('Running command: %s' % ' '.join(command))

    async def tee(reader: asyncio.StreamReader) -> str:
        collected = StringIO()
        async for line in reader:
            message = line.decode('utf-8')
            collected.write(message)
            if verbosity == CallVerbosity.VERBOSE:
                logger.info(prefix + message.rstrip())
            elif verbosity != CallVerbosity.SILENT:
                logger.debug(prefix + message.rstrip())
        return collected.getvalue()

    async def run_with_timeout() -> Tuple[str, str, int]:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        assert process.stdout
        assert process.stderr
        try:
            stdout, stderr = await asyncio.gather(tee(process.stdout),
                                                  tee(process.stderr))
            returncode = await asyncio.wait_for(process.wait(), timeout)
        except asyncio.TimeoutError:
            logger.info(prefix + f'timeout after {timeout} seconds')
            return '', '', 124
        else:
            return stdout, stderr, returncode

    stdout, stderr, returncode = async_run(run_with_timeout())
    if returncode != 0 and verbosity == CallVerbosity.VERBOSE_ON_FAILURE:
        logger.info('Non-zero exit code %d from %s',
                    returncode, ' '.join(command))
        for line in stdout.splitlines():
            logger.info(prefix + 'stdout ' + line)
        for line in stderr.splitlines():
            logger.info(prefix + 'stderr ' + line)
    return stdout, stderr, returncode


def call_throws(
        ctx: CephadmContext,
        command: List[str],
        desc: Optional[str] = None,
        verbosity: CallVerbosity = CallVerbosity.VERBOSE_ON_FAILURE,
        timeout: Optional[int] = DEFAULT_TIMEOUT,
        **kwargs) -> Tuple[str, str, int]:
    out, err, ret = call(ctx, command, desc, verbosity, timeout, **kwargs)
    if ret:
        raise RuntimeError('Failed command: %s' % ' '.join(command))
    return out, err, ret


def call_timeout(ctx, command, timeout):
    # type: (CephadmContext, List[str], int) -> int
    logger.debug('Running command (timeout=%s): %s'
                 % (timeout, ' '.join(command)))

    def raise_timeout(command, timeout):
        # type: (List[str], int) -> NoReturn
        msg = 'Command `%s` timed out after %s seconds' % (command, timeout)
        logger.debug(msg)
        raise TimeoutExpired(msg)

    try:
        return subprocess.call(command, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise_timeout(command, timeout)

##################################


def json_loads_retry(cli_func):
    for sleep_secs in [1, 4, 4]:
        try:
            return json.loads(cli_func())
        except json.JSONDecodeError:
            logger.debug('Invalid JSON. Retrying in %s seconds...' % sleep_secs)
            time.sleep(sleep_secs)
    return json.loads(cli_func())


def is_available(ctx, what, func):
    # type: (CephadmContext, str, Callable[[], bool]) -> None
    """
    Wait for a service to become available

    :param what: the name of the service
    :param func: the callable object that determines availability
    """
    retry = ctx.retry
    logger.info('Waiting for %s...' % what)
    num = 1
    while True:
        if func():
            logger.info('%s is available'
                        % what)
            break
        elif num > retry:
            raise Error('%s not available after %s tries'
                        % (what, retry))

        logger.info('%s not available, waiting (%s/%s)...'
                    % (what, num, retry))

        num += 1
        time.sleep(2)


def read_config(fn):
    # type: (Optional[str]) -> ConfigParser
    cp = ConfigParser()
    if fn:
        cp.read(fn)
    return cp


def pathify(p):
    # type: (str) -> str
    p = os.path.expanduser(p)
    return os.path.abspath(p)


def get_file_timestamp(fn):
    # type: (str) -> Optional[str]
    try:
        mt = os.path.getmtime(fn)
        return datetime.datetime.fromtimestamp(
            mt, tz=datetime.timezone.utc
        ).strftime(DATEFMT)
    except Exception:
        return None


def try_convert_datetime(s):
    # type: (str) -> Optional[str]
    # This is super irritating because
    #  1) podman and docker use different formats
    #  2) python's strptime can't parse either one
    #
    # I've seen:
    #  docker 18.09.7:  2020-03-03T09:21:43.636153304Z
    #  podman 1.7.0:    2020-03-03T15:52:30.136257504-06:00
    #                   2020-03-03 15:52:30.136257504 -0600 CST
    # (In the podman case, there is a different string format for
    # 'inspect' and 'inspect --format {{.Created}}'!!)

    # In *all* cases, the 9 digit second precision is too much for
    # python's strptime.  Shorten it to 6 digits.
    p = re.compile(r'(\.[\d]{6})[\d]*')
    s = p.sub(r'\1', s)

    # replace trailing Z with -0000, since (on python 3.6.8) it won't parse
    if s and s[-1] == 'Z':
        s = s[:-1] + '-0000'

    # cut off the redundant 'CST' part that strptime can't parse, if
    # present.
    v = s.split(' ')
    s = ' '.join(v[0:3])

    # try parsing with several format strings
    fmts = [
        '%Y-%m-%dT%H:%M:%S.%f%z',
        '%Y-%m-%d %H:%M:%S.%f %z',
    ]
    for f in fmts:
        try:
            # return timestamp normalized to UTC, rendered as DATEFMT.
            return datetime.datetime.strptime(s, f).astimezone(tz=datetime.timezone.utc).strftime(DATEFMT)
        except ValueError:
            pass
    return None


def _parse_podman_version(version_str):
    # type: (str) -> Tuple[int, ...]
    def to_int(val, org_e=None):
        if not val and org_e:
            raise org_e
        try:
            return int(val)
        except ValueError as e:
            return to_int(val[0:-1], org_e or e)

    return tuple(map(to_int, version_str.split('.')))


def get_hostname():
    # type: () -> str
    return socket.gethostname()


def get_fqdn():
    # type: () -> str
    return socket.getfqdn() or socket.gethostname()


def get_arch():
    # type: () -> str
    return platform.uname().machine


def generate_service_id():
    # type: () -> str
    return get_hostname() + '.' + ''.join(random.choice(string.ascii_lowercase)
                                          for _ in range(6))


def generate_password():
    # type: () -> str
    return ''.join(random.choice(string.ascii_lowercase + string.digits)
                   for i in range(10))


def normalize_container_id(i):
    # type: (str) -> str
    # docker adds the sha256: prefix, but AFAICS both
    # docker (18.09.7 in bionic at least) and podman
    # both always use sha256, so leave off the prefix
    # for consistency.
    prefix = 'sha256:'
    if i.startswith(prefix):
        i = i[len(prefix):]
    return i


def make_fsid():
    # type: () -> str
    return str(uuid.uuid1())


def is_fsid(s):
    # type: (str) -> bool
    try:
        uuid.UUID(s)
    except ValueError:
        return False
    return True


def infer_fsid(func):
    """
    If we only find a single fsid in /var/lib/ceph/*, use that
    """
    @wraps(func)
    def _infer_fsid(ctx: CephadmContext):
        if ctx.fsid:
            logger.debug('Using specified fsid: %s' % ctx.fsid)
            return func(ctx)

        fsids_set = set()
        daemon_list = list_daemons(ctx, detail=False)
        for daemon in daemon_list:
            if not is_fsid(daemon['fsid']):
                # 'unknown' fsid
                continue
            elif 'name' not in ctx or not ctx.name:
                # ctx.name not specified
                fsids_set.add(daemon['fsid'])
            elif daemon['name'] == ctx.name:
                # ctx.name is a match
                fsids_set.add(daemon['fsid'])
        fsids = sorted(fsids_set)

        if not fsids:
            # some commands do not always require an fsid
            pass
        elif len(fsids) == 1:
            logger.info('Inferring fsid %s' % fsids[0])
            ctx.fsid = fsids[0]
        else:
            raise Error('Cannot infer an fsid, one must be specified: %s' % fsids)
        return func(ctx)

    return _infer_fsid


def infer_config(func):
    """
    If we find a MON daemon, use the config from that container
    """
    @wraps(func)
    def _infer_config(ctx: CephadmContext):
        if ctx.config:
            logger.debug('Using specified config: %s' % ctx.config)
            return func(ctx)
        config = None
        if ctx.fsid:
            name = ctx.name
            if not name:
                daemon_list = list_daemons(ctx, detail=False)
                for daemon in daemon_list:
                    if daemon['name'].startswith('mon.'):
                        name = daemon['name']
                        break
            if name:
                config = '/var/lib/ceph/{}/{}/config'.format(ctx.fsid,
                                                             name)
        if config:
            logger.info('Inferring config %s' % config)
            ctx.config = config
        elif os.path.exists(SHELL_DEFAULT_CONF):
            logger.debug('Using default config: %s' % SHELL_DEFAULT_CONF)
            ctx.config = SHELL_DEFAULT_CONF
        return func(ctx)

    return _infer_config


def _get_default_image(ctx: CephadmContext):
    if DEFAULT_IMAGE_IS_MASTER:
        warn = """This is a development version of cephadm.
For information regarding the latest stable release:
    https://docs.ceph.com/docs/{}/cephadm/install
""".format(LATEST_STABLE_RELEASE)
        for line in warn.splitlines():
            logger.warning('{}{}{}'.format(termcolor.yellow, line, termcolor.end))
    return DEFAULT_IMAGE


def infer_image(func):
    """
    Use the most recent ceph image
    """
    @wraps(func)
    def _infer_image(ctx: CephadmContext):
        if not ctx.image:
            ctx.image = os.environ.get('CEPHADM_IMAGE')
        if not ctx.image:
            ctx.image = get_last_local_ceph_image(ctx, ctx.container_engine.path)
        if not ctx.image:
            ctx.image = _get_default_image(ctx)
        return func(ctx)

    return _infer_image


def default_image(func):
    @wraps(func)
    def _default_image(ctx: CephadmContext):
        if not ctx.image:
            if 'name' in ctx and ctx.name:
                type_ = ctx.name.split('.', 1)[0]
                if type_ in Monitoring.components:
                    ctx.image = Monitoring.components[type_]['image']
                if type_ == 'haproxy':
                    ctx.image = HAproxy.default_image
                if type_ == 'keepalived':
                    ctx.image = Keepalived.default_image
            if not ctx.image:
                ctx.image = os.environ.get('CEPHADM_IMAGE')
            if not ctx.image:
                ctx.image = _get_default_image(ctx)

        return func(ctx)

    return _default_image


def get_last_local_ceph_image(ctx: CephadmContext, container_path: str):
    """
    :return: The most recent local ceph image (already pulled)
    """
    out, _, _ = call_throws(ctx,
                            [container_path, 'images',
                             '--filter', 'label=ceph=True',
                             '--filter', 'dangling=false',
                             '--format', '{{.Repository}}@{{.Digest}}'])
    return _filter_last_local_ceph_image(out)


def _filter_last_local_ceph_image(out):
    # type: (str) -> Optional[str]
    for image in out.splitlines():
        if image and not image.endswith('@'):
            logger.info('Using recent ceph image %s' % image)
            return image
    return None


def write_tmp(s, uid, gid):
    # type: (str, int, int) -> IO[str]
    tmp_f = tempfile.NamedTemporaryFile(mode='w',
                                        prefix='ceph-tmp')
    os.fchown(tmp_f.fileno(), uid, gid)
    tmp_f.write(s)
    tmp_f.flush()

    return tmp_f


def makedirs(dir, uid, gid, mode):
    # type: (str, int, int, int) -> None
    if not os.path.exists(dir):
        os.makedirs(dir, mode=mode)
    else:
        os.chmod(dir, mode)
    os.chown(dir, uid, gid)
    os.chmod(dir, mode)   # the above is masked by umask...


def get_data_dir(fsid, data_dir, t, n):
    # type: (str, str, str, Union[int, str]) -> str
    return os.path.join(data_dir, fsid, '%s.%s' % (t, n))


def get_log_dir(fsid, log_dir):
    # type: (str, str) -> str
    return os.path.join(log_dir, fsid)


def make_data_dir_base(fsid, data_dir, uid, gid):
    # type: (str, str, int, int) -> str
    data_dir_base = os.path.join(data_dir, fsid)
    makedirs(data_dir_base, uid, gid, DATA_DIR_MODE)
    makedirs(os.path.join(data_dir_base, 'crash'), uid, gid, DATA_DIR_MODE)
    makedirs(os.path.join(data_dir_base, 'crash', 'posted'), uid, gid,
             DATA_DIR_MODE)
    return data_dir_base


def make_data_dir(ctx, fsid, daemon_type, daemon_id, uid=None, gid=None):
    # type: (CephadmContext, str, str, Union[int, str], Optional[int], Optional[int]) -> str
    if uid is None or gid is None:
        uid, gid = extract_uid_gid(ctx)
    make_data_dir_base(fsid, ctx.data_dir, uid, gid)
    data_dir = get_data_dir(fsid, ctx.data_dir, daemon_type, daemon_id)
    makedirs(data_dir, uid, gid, DATA_DIR_MODE)
    return data_dir


def make_log_dir(ctx, fsid, uid=None, gid=None):
    # type: (CephadmContext, str, Optional[int], Optional[int]) -> str
    if uid is None or gid is None:
        uid, gid = extract_uid_gid(ctx)
    log_dir = get_log_dir(fsid, ctx.log_dir)
    makedirs(log_dir, uid, gid, LOG_DIR_MODE)
    return log_dir


def make_var_run(ctx, fsid, uid, gid):
    # type: (CephadmContext, str, int, int) -> None
    call_throws(ctx, ['install', '-d', '-m0770', '-o', str(uid), '-g', str(gid),
                      '/var/run/ceph/%s' % fsid])


def copy_tree(ctx, src, dst, uid=None, gid=None):
    # type: (CephadmContext, List[str], str, Optional[int], Optional[int]) -> None
    """
    Copy a directory tree from src to dst
    """
    if uid is None or gid is None:
        (uid, gid) = extract_uid_gid(ctx)

    for src_dir in src:
        dst_dir = dst
        if os.path.isdir(dst):
            dst_dir = os.path.join(dst, os.path.basename(src_dir))

        logger.debug('copy directory `%s` -> `%s`' % (src_dir, dst_dir))
        shutil.rmtree(dst_dir, ignore_errors=True)
        shutil.copytree(src_dir, dst_dir)  # dirs_exist_ok needs python 3.8

        for dirpath, dirnames, filenames in os.walk(dst_dir):
            logger.debug('chown %s:%s `%s`' % (uid, gid, dirpath))
            os.chown(dirpath, uid, gid)
            for filename in filenames:
                logger.debug('chown %s:%s `%s`' % (uid, gid, filename))
                os.chown(os.path.join(dirpath, filename), uid, gid)


def copy_files(ctx, src, dst, uid=None, gid=None):
    # type: (CephadmContext, List[str], str, Optional[int], Optional[int]) -> None
    """
    Copy a files from src to dst
    """
    if uid is None or gid is None:
        (uid, gid) = extract_uid_gid(ctx)

    for src_file in src:
        dst_file = dst
        if os.path.isdir(dst):
            dst_file = os.path.join(dst, os.path.basename(src_file))

        logger.debug('copy file `%s` -> `%s`' % (src_file, dst_file))
        shutil.copyfile(src_file, dst_file)

        logger.debug('chown %s:%s `%s`' % (uid, gid, dst_file))
        os.chown(dst_file, uid, gid)


def move_files(ctx, src, dst, uid=None, gid=None):
    # type: (CephadmContext, List[str], str, Optional[int], Optional[int]) -> None
    """
    Move files from src to dst
    """
    if uid is None or gid is None:
        (uid, gid) = extract_uid_gid(ctx)

    for src_file in src:
        dst_file = dst
        if os.path.isdir(dst):
            dst_file = os.path.join(dst, os.path.basename(src_file))

        if os.path.islink(src_file):
            # shutil.move() in py2 does not handle symlinks correctly
            src_rl = os.readlink(src_file)
            logger.debug("symlink '%s' -> '%s'" % (dst_file, src_rl))
            os.symlink(src_rl, dst_file)
            os.unlink(src_file)
        else:
            logger.debug("move file '%s' -> '%s'" % (src_file, dst_file))
            shutil.move(src_file, dst_file)
            logger.debug('chown %s:%s `%s`' % (uid, gid, dst_file))
            os.chown(dst_file, uid, gid)


# copied from distutils
def find_executable(executable, path=None):
    """Tries to find 'executable' in the directories listed in 'path'.
    A string listing directories separated by 'os.pathsep'; defaults to
    os.environ['PATH'].  Returns the complete filename or None if not found.
    """
    _, ext = os.path.splitext(executable)
    if (sys.platform == 'win32') and (ext != '.exe'):
        executable = executable + '.exe'

    if os.path.isfile(executable):
        return executable

    if path is None:
        path = os.environ.get('PATH', None)
        if path is None:
            try:
                path = os.confstr('CS_PATH')
            except (AttributeError, ValueError):
                # os.confstr() or CS_PATH is not available
                path = os.defpath
        # bpo-35755: Don't use os.defpath if the PATH environment variable is
        # set to an empty string

    # PATH='' doesn't match, whereas PATH=':' looks in the current directory
    if not path:
        return None

    paths = path.split(os.pathsep)
    for p in paths:
        f = os.path.join(p, executable)
        if os.path.isfile(f):
            # the file exists, we have a shot at spawn working
            return f
    return None


def find_program(filename):
    # type: (str) -> str
    name = find_executable(filename)
    if name is None:
        raise ValueError('%s not found' % filename)
    return name


def find_container_engine(ctx: CephadmContext):
    if ctx.docker:
        return Docker()
    else:
        for i in CONTAINER_PREFERENCE:
            try:
                return i()
            except Exception as e:
                logger.debug('Could not locate %s: %s' % (i.EXE, e))
    return None


def check_container_engine(ctx):
    # type: (CephadmContext) -> None
    engine = ctx.container_engine
    if not isinstance(engine, CONTAINER_PREFERENCE):
        raise Error('Unable to locate any of %s' % [i.EXE for i in CONTAINER_PREFERENCE])
    elif isinstance(engine, Podman):
        engine.get_version(ctx)
        if engine.version < MIN_PODMAN_VERSION:
            raise Error('podman version %d.%d.%d or later is required' % MIN_PODMAN_VERSION)


def get_unit_name(fsid, daemon_type, daemon_id=None):
    # type: (str, str, Optional[Union[int, str]]) -> str
    # accept either name or type + id
    if daemon_type == CephadmDaemon.daemon_type and daemon_id is not None:
        return 'ceph-%s-%s.%s' % (fsid, daemon_type, daemon_id)
    elif daemon_id is not None:
        return 'ceph-%s@%s.%s' % (fsid, daemon_type, daemon_id)
    else:
        return 'ceph-%s@%s' % (fsid, daemon_type)


def get_unit_name_by_daemon_name(ctx: CephadmContext, fsid, name):
    daemon = get_daemon_description(ctx, fsid, name)
    try:
        return daemon['systemd_unit']
    except KeyError:
        raise Error('Failed to get unit name for {}'.format(daemon))


def check_unit(ctx, unit_name):
    # type: (CephadmContext, str) -> Tuple[bool, str, bool]
    # NOTE: we ignore the exit code here because systemctl outputs
    # various exit codes based on the state of the service, but the
    # string result is more explicit (and sufficient).
    enabled = False
    installed = False
    try:
        out, err, code = call(ctx, ['systemctl', 'is-enabled', unit_name],
                              verbosity=CallVerbosity.DEBUG)
        if code == 0:
            enabled = True
            installed = True
        elif 'disabled' in out:
            installed = True
    except Exception as e:
        logger.warning('unable to run systemctl: %s' % e)
        enabled = False
        installed = False

    state = 'unknown'
    try:
        out, err, code = call(ctx, ['systemctl', 'is-active', unit_name],
                              verbosity=CallVerbosity.DEBUG)
        out = out.strip()
        if out in ['active']:
            state = 'running'
        elif out in ['inactive']:
            state = 'stopped'
        elif out in ['failed', 'auto-restart']:
            state = 'error'
        else:
            state = 'unknown'
    except Exception as e:
        logger.warning('unable to run systemctl: %s' % e)
        state = 'unknown'
    return (enabled, state, installed)


def check_units(ctx, units, enabler=None):
    # type: (CephadmContext, List[str], Optional[Packager]) -> bool
    for u in units:
        (enabled, state, installed) = check_unit(ctx, u)
        if enabled and state == 'running':
            logger.info('Unit %s is enabled and running' % u)
            return True
        if enabler is not None:
            if installed:
                logger.info('Enabling unit %s' % u)
                enabler.enable_service(u)
    return False


def is_container_running(ctx: CephadmContext, name: str) -> bool:
    out, err, ret = call(ctx, [
        ctx.container_engine.path, 'container', 'inspect',
        '--format', '{{.State.Status}}', name
    ])
    return out == 'running'


def get_legacy_config_fsid(cluster, legacy_dir=None):
    # type: (str, Optional[str]) -> Optional[str]
    config_file = '/etc/ceph/%s.conf' % cluster
    if legacy_dir is not None:
        config_file = os.path.abspath(legacy_dir + config_file)

    if os.path.exists(config_file):
        config = read_config(config_file)
        if config.has_section('global') and config.has_option('global', 'fsid'):
            return config.get('global', 'fsid')
    return None


def get_legacy_daemon_fsid(ctx, cluster,
                           daemon_type, daemon_id, legacy_dir=None):
    # type: (CephadmContext, str, str, Union[int, str], Optional[str]) -> Optional[str]
    fsid = None
    if daemon_type == 'osd':
        try:
            fsid_file = os.path.join(ctx.data_dir,
                                     daemon_type,
                                     'ceph-%s' % daemon_id,
                                     'ceph_fsid')
            if legacy_dir is not None:
                fsid_file = os.path.abspath(legacy_dir + fsid_file)
            with open(fsid_file, 'r') as f:
                fsid = f.read().strip()
        except IOError:
            pass
    if not fsid:
        fsid = get_legacy_config_fsid(cluster, legacy_dir=legacy_dir)
    return fsid


def should_log_to_journald(ctx):
    if ctx.log_to_journald is not None:
        return ctx.log_to_journald
    return isinstance(ctx.container_engine, Podman) and \
        ctx.container_engine.version >= CGROUPS_SPLIT_PODMAN_VERSION


def get_daemon_args(ctx, fsid, daemon_type, daemon_id):
    # type: (CephadmContext, str, str, Union[int, str]) -> List[str]
    r = list()  # type: List[str]

    if daemon_type in Ceph.daemons and daemon_type != 'crash':
        r += [
            '--setuser', 'ceph',
            '--setgroup', 'ceph',
            '--default-log-to-file=false',
        ]
        log_to_journald = should_log_to_journald(ctx)
        if log_to_journald:
            r += [
                '--default-log-to-journald=true',
                '--default-log-to-stderr=false',
            ]
        else:
            r += [
                '--default-log-to-stderr=true',
                '--default-log-stderr-prefix=debug ',
            ]
        if daemon_type == 'mon':
            r += [
                '--default-mon-cluster-log-to-file=false',
            ]
            if log_to_journald:
                r += [
                    '--default-mon-cluster-log-to-journald=true',
                    '--default-mon-cluster-log-to-stderr=false',
                ]
            else:
                r += ['--default-mon-cluster-log-to-stderr=true']
    elif daemon_type in Monitoring.components:
        metadata = Monitoring.components[daemon_type]
        r += metadata.get('args', list())
        # set ip and port to bind to for nodeexporter,alertmanager,prometheus
        if daemon_type != 'grafana':
            ip = ''
            port = Monitoring.port_map[daemon_type][0]
            if 'meta_json' in ctx and ctx.meta_json:
                meta = json.loads(ctx.meta_json) or {}
                if 'ip' in meta and meta['ip']:
                    ip = meta['ip']
                if 'ports' in meta and meta['ports']:
                    port = meta['ports'][0]
            r += [f'--web.listen-address={ip}:{port}']
        if daemon_type == 'alertmanager':
            config = get_parm(ctx.config_json)
            peers = config.get('peers', list())  # type: ignore
            for peer in peers:
                r += ['--cluster.peer={}'.format(peer)]
            # some alertmanager, by default, look elsewhere for a config
            r += ['--config.file=/etc/alertmanager/alertmanager.yml']
    elif daemon_type == NFSGanesha.daemon_type:
        nfs_ganesha = NFSGanesha.init(ctx, fsid, daemon_id)
        r += nfs_ganesha.get_daemon_args()
    elif daemon_type == HAproxy.daemon_type:
        haproxy = HAproxy.init(ctx, fsid, daemon_id)
        r += haproxy.get_daemon_args()
    elif daemon_type == CustomContainer.daemon_type:
        cc = CustomContainer.init(ctx, fsid, daemon_id)
        r.extend(cc.get_daemon_args())

    return r


def create_daemon_dirs(ctx, fsid, daemon_type, daemon_id, uid, gid,
                       config=None, keyring=None):
    # type: (CephadmContext, str, str, Union[int, str], int, int, Optional[str], Optional[str]) ->  None
    data_dir = make_data_dir(ctx, fsid, daemon_type, daemon_id, uid=uid, gid=gid)
    make_log_dir(ctx, fsid, uid=uid, gid=gid)

    if config:
        config_path = os.path.join(data_dir, 'config')
        with open(config_path, 'w') as f:
            os.fchown(f.fileno(), uid, gid)
            os.fchmod(f.fileno(), 0o600)
            f.write(config)

    if keyring:
        keyring_path = os.path.join(data_dir, 'keyring')
        with open(keyring_path, 'w') as f:
            os.fchmod(f.fileno(), 0o600)
            os.fchown(f.fileno(), uid, gid)
            f.write(keyring)

    if daemon_type in Monitoring.components.keys():
        config_json: Dict[str, Any] = get_parm(ctx.config_json)

        # Set up directories specific to the monitoring component
        config_dir = ''
        data_dir_root = ''
        if daemon_type == 'prometheus':
            data_dir_root = get_data_dir(fsid, ctx.data_dir,
                                         daemon_type, daemon_id)
            config_dir = 'etc/prometheus'
            makedirs(os.path.join(data_dir_root, config_dir), uid, gid, 0o755)
            makedirs(os.path.join(data_dir_root, config_dir, 'alerting'), uid, gid, 0o755)
            makedirs(os.path.join(data_dir_root, 'data'), uid, gid, 0o755)
        elif daemon_type == 'grafana':
            data_dir_root = get_data_dir(fsid, ctx.data_dir,
                                         daemon_type, daemon_id)
            config_dir = 'etc/grafana'
            makedirs(os.path.join(data_dir_root, config_dir), uid, gid, 0o755)
            makedirs(os.path.join(data_dir_root, config_dir, 'certs'), uid, gid, 0o755)
            makedirs(os.path.join(data_dir_root, config_dir, 'provisioning/datasources'), uid, gid, 0o755)
            makedirs(os.path.join(data_dir_root, 'data'), uid, gid, 0o755)
            touch(os.path.join(data_dir_root, 'data', 'grafana.db'), uid, gid)
        elif daemon_type == 'alertmanager':
            data_dir_root = get_data_dir(fsid, ctx.data_dir,
                                         daemon_type, daemon_id)
            config_dir = 'etc/alertmanager'
            makedirs(os.path.join(data_dir_root, config_dir), uid, gid, 0o755)
            makedirs(os.path.join(data_dir_root, config_dir, 'data'), uid, gid, 0o755)

        # populate the config directory for the component from the config-json
        if 'files' in config_json:
            for fname in config_json['files']:
                content = dict_get_join(config_json['files'], fname)
                if os.path.isabs(fname):
                    fpath = os.path.join(data_dir_root, fname.lstrip(os.path.sep))
                else:
                    fpath = os.path.join(data_dir_root, config_dir, fname)
                with open(fpath, 'w', encoding='utf-8') as f:
                    os.fchown(f.fileno(), uid, gid)
                    os.fchmod(f.fileno(), 0o600)
                    f.write(content)

    elif daemon_type == NFSGanesha.daemon_type:
        nfs_ganesha = NFSGanesha.init(ctx, fsid, daemon_id)
        nfs_ganesha.create_daemon_dirs(data_dir, uid, gid)

    elif daemon_type == CephIscsi.daemon_type:
        ceph_iscsi = CephIscsi.init(ctx, fsid, daemon_id)
        ceph_iscsi.create_daemon_dirs(data_dir, uid, gid)

    elif daemon_type == HAproxy.daemon_type:
        haproxy = HAproxy.init(ctx, fsid, daemon_id)
        haproxy.create_daemon_dirs(data_dir, uid, gid)

    elif daemon_type == Keepalived.daemon_type:
        keepalived = Keepalived.init(ctx, fsid, daemon_id)
        keepalived.create_daemon_dirs(data_dir, uid, gid)

    elif daemon_type == CustomContainer.daemon_type:
        cc = CustomContainer.init(ctx, fsid, daemon_id)
        cc.create_daemon_dirs(data_dir, uid, gid)


def get_parm(option):
    # type: (str) -> Dict[str, str]

    if not option:
        return dict()

    global cached_stdin
    if option == '-':
        if cached_stdin is not None:
            j = cached_stdin
        else:
            j = sys.stdin.read()
            cached_stdin = j
    else:
        # inline json string
        if option[0] == '{' and option[-1] == '}':
            j = option
        # json file
        elif os.path.exists(option):
            with open(option, 'r') as f:
                j = f.read()
        else:
            raise Error('Config file {} not found'.format(option))

    try:
        js = json.loads(j)
    except ValueError as e:
        raise Error('Invalid JSON in {}: {}'.format(option, e))
    else:
        return js


def get_config_and_keyring(ctx):
    # type: (CephadmContext) -> Tuple[Optional[str], Optional[str]]
    config = None
    keyring = None

    if 'config_json' in ctx and ctx.config_json:
        d = get_parm(ctx.config_json)
        config = d.get('config')
        keyring = d.get('keyring')

    if 'config' in ctx and ctx.config:
        try:
            with open(ctx.config, 'r') as f:
                config = f.read()
        except FileNotFoundError as e:
            raise Error(e)

    if 'key' in ctx and ctx.key:
        keyring = '[%s]\n\tkey = %s\n' % (ctx.name, ctx.key)
    elif 'keyring' in ctx and ctx.keyring:
        try:
            with open(ctx.keyring, 'r') as f:
                keyring = f.read()
        except FileNotFoundError as e:
            raise Error(e)

    return config, keyring


def get_container_binds(ctx, fsid, daemon_type, daemon_id):
    # type: (CephadmContext, str, str, Union[int, str, None]) -> List[List[str]]
    binds = list()

    if daemon_type == CephIscsi.daemon_type:
        binds.extend(CephIscsi.get_container_binds())
    elif daemon_type == CustomContainer.daemon_type:
        assert daemon_id
        cc = CustomContainer.init(ctx, fsid, daemon_id)
        data_dir = get_data_dir(fsid, ctx.data_dir, daemon_type, daemon_id)
        binds.extend(cc.get_container_binds(data_dir))

    return binds


def get_container_mounts(ctx, fsid, daemon_type, daemon_id,
                         no_config=False):
    # type: (CephadmContext, str, str, Union[int, str, None], Optional[bool]) -> Dict[str, str]
    mounts = dict()

    if daemon_type in Ceph.daemons:
        if fsid:
            run_path = os.path.join('/var/run/ceph', fsid)
            if os.path.exists(run_path):
                mounts[run_path] = '/var/run/ceph:z'
            log_dir = get_log_dir(fsid, ctx.log_dir)
            mounts[log_dir] = '/var/log/ceph:z'
            crash_dir = '/var/lib/ceph/%s/crash' % fsid
            if os.path.exists(crash_dir):
                mounts[crash_dir] = '/var/lib/ceph/crash:z'
            if daemon_type != 'crash' and should_log_to_journald(ctx):
                journald_sock_dir = '/run/systemd/journal'
                mounts[journald_sock_dir] = journald_sock_dir

    if daemon_type in Ceph.daemons and daemon_id:
        data_dir = get_data_dir(fsid, ctx.data_dir, daemon_type, daemon_id)
        if daemon_type == 'rgw':
            cdata_dir = '/var/lib/ceph/radosgw/ceph-rgw.%s' % (daemon_id)
        else:
            cdata_dir = '/var/lib/ceph/%s/ceph-%s' % (daemon_type, daemon_id)
        if daemon_type != 'crash':
            mounts[data_dir] = cdata_dir + ':z'
        if not no_config:
            mounts[data_dir + '/config'] = '/etc/ceph/ceph.conf:z'
        if daemon_type in ['rbd-mirror', 'cephfs-mirror', 'crash']:
            # these do not search for their keyrings in a data directory
            mounts[data_dir + '/keyring'] = '/etc/ceph/ceph.client.%s.%s.keyring' % (daemon_type, daemon_id)

    if daemon_type in ['mon', 'osd', 'clusterless-ceph-volume']:
        mounts['/dev'] = '/dev'  # FIXME: narrow this down?
        mounts['/run/udev'] = '/run/udev'
    if daemon_type in ['osd', 'clusterless-ceph-volume']:
        mounts['/sys'] = '/sys'  # for numa.cc, pick_address, cgroups, ...
        mounts['/run/lvm'] = '/run/lvm'
        mounts['/run/lock/lvm'] = '/run/lock/lvm'
    if daemon_type == 'osd':
        # selinux-policy in the container may not match the host.
        if HostFacts(ctx).selinux_enabled:
            selinux_folder = '/var/lib/ceph/%s/selinux' % fsid
            if not os.path.exists(selinux_folder):
                os.makedirs(selinux_folder, mode=0o755)
            mounts[selinux_folder] = '/sys/fs/selinux:ro'

    try:
        if ctx.shared_ceph_folder:  # make easy manager modules/ceph-volume development
            ceph_folder = pathify(ctx.shared_ceph_folder)
            if os.path.exists(ceph_folder):
                mounts[ceph_folder + '/src/ceph-volume/ceph_volume'] = '/usr/lib/python3.6/site-packages/ceph_volume'
                mounts[ceph_folder + '/src/pybind/mgr'] = '/usr/share/ceph/mgr'
                mounts[ceph_folder + '/src/python-common/ceph'] = '/usr/lib/python3.6/site-packages/ceph'
                mounts[ceph_folder + '/monitoring/grafana/dashboards'] = '/etc/grafana/dashboards/ceph-dashboard'
                mounts[ceph_folder + '/monitoring/prometheus/alerts'] = '/etc/prometheus/ceph'
            else:
                logger.error('{}{}{}'.format(termcolor.red,
                                             'Ceph shared source folder does not exist.',
                                             termcolor.end))
    except AttributeError:
        pass

    if daemon_type in Monitoring.components and daemon_id:
        data_dir = get_data_dir(fsid, ctx.data_dir, daemon_type, daemon_id)
        if daemon_type == 'prometheus':
            mounts[os.path.join(data_dir, 'etc/prometheus')] = '/etc/prometheus:Z'
            mounts[os.path.join(data_dir, 'data')] = '/prometheus:Z'
        elif daemon_type == 'node-exporter':
            mounts['/proc'] = '/host/proc:ro'
            mounts['/sys'] = '/host/sys:ro'
            mounts['/'] = '/rootfs:ro'
        elif daemon_type == 'grafana':
            mounts[os.path.join(data_dir, 'etc/grafana/grafana.ini')] = '/etc/grafana/grafana.ini:Z'
            mounts[os.path.join(data_dir, 'etc/grafana/provisioning/datasources')] = '/etc/grafana/provisioning/datasources:Z'
            mounts[os.path.join(data_dir, 'etc/grafana/certs')] = '/etc/grafana/certs:Z'
            mounts[os.path.join(data_dir, 'data/grafana.db')] = '/var/lib/grafana/grafana.db:Z'
        elif daemon_type == 'alertmanager':
            mounts[os.path.join(data_dir, 'etc/alertmanager')] = '/etc/alertmanager:Z'

    if daemon_type == NFSGanesha.daemon_type:
        assert daemon_id
        data_dir = get_data_dir(fsid, ctx.data_dir, daemon_type, daemon_id)
        nfs_ganesha = NFSGanesha.init(ctx, fsid, daemon_id)
        mounts.update(nfs_ganesha.get_container_mounts(data_dir))

    if daemon_type == HAproxy.daemon_type:
        assert daemon_id
        data_dir = get_data_dir(fsid, ctx.data_dir, daemon_type, daemon_id)
        mounts.update(HAproxy.get_container_mounts(data_dir))

    if daemon_type == CephIscsi.daemon_type:
        assert daemon_id
        data_dir = get_data_dir(fsid, ctx.data_dir, daemon_type, daemon_id)
        log_dir = get_log_dir(fsid, ctx.log_dir)
        mounts.update(CephIscsi.get_container_mounts(data_dir, log_dir))

    if daemon_type == Keepalived.daemon_type:
        assert daemon_id
        data_dir = get_data_dir(fsid, ctx.data_dir, daemon_type, daemon_id)
        mounts.update(Keepalived.get_container_mounts(data_dir))

    if daemon_type == CustomContainer.daemon_type:
        assert daemon_id
        cc = CustomContainer.init(ctx, fsid, daemon_id)
        data_dir = get_data_dir(fsid, ctx.data_dir, daemon_type, daemon_id)
        mounts.update(cc.get_container_mounts(data_dir))

    return mounts


def get_container(ctx: CephadmContext,
                  fsid: str, daemon_type: str, daemon_id: Union[int, str],
                  privileged: bool = False,
                  ptrace: bool = False,
                  container_args: Optional[List[str]] = None) -> 'CephContainer':
    entrypoint: str = ''
    name: str = ''
    ceph_args: List[str] = []
    envs: List[str] = []
    host_network: bool = True

    if container_args is None:
        container_args = []
    if daemon_type in ['mon', 'osd']:
        # mon and osd need privileged in order for libudev to query devices
        privileged = True
    if daemon_type == 'rgw':
        entrypoint = '/usr/bin/radosgw'
        name = 'client.rgw.%s' % daemon_id
    elif daemon_type == 'rbd-mirror':
        entrypoint = '/usr/bin/rbd-mirror'
        name = 'client.rbd-mirror.%s' % daemon_id
    elif daemon_type == 'cephfs-mirror':
        entrypoint = '/usr/bin/cephfs-mirror'
        name = 'client.cephfs-mirror.%s' % daemon_id
    elif daemon_type == 'crash':
        entrypoint = '/usr/bin/ceph-crash'
        name = 'client.crash.%s' % daemon_id
    elif daemon_type in ['mon', 'mgr', 'mds', 'osd']:
        entrypoint = '/usr/bin/ceph-' + daemon_type
        name = '%s.%s' % (daemon_type, daemon_id)
    elif daemon_type in Monitoring.components:
        entrypoint = ''
    elif daemon_type == NFSGanesha.daemon_type:
        entrypoint = NFSGanesha.entrypoint
        name = '%s.%s' % (daemon_type, daemon_id)
        envs.extend(NFSGanesha.get_container_envs())
    elif daemon_type == HAproxy.daemon_type:
        name = '%s.%s' % (daemon_type, daemon_id)
    elif daemon_type == Keepalived.daemon_type:
        name = '%s.%s' % (daemon_type, daemon_id)
        envs.extend(Keepalived.get_container_envs())
        container_args.extend(['--cap-add=NET_ADMIN', '--cap-add=NET_RAW'])
    elif daemon_type == CephIscsi.daemon_type:
        entrypoint = CephIscsi.entrypoint
        name = '%s.%s' % (daemon_type, daemon_id)
        # So the container can modprobe iscsi_target_mod and have write perms
        # to configfs we need to make this a privileged container.
        privileged = True
    elif daemon_type == CustomContainer.daemon_type:
        cc = CustomContainer.init(ctx, fsid, daemon_id)
        entrypoint = cc.entrypoint
        host_network = False
        envs.extend(cc.get_container_envs())
        container_args.extend(cc.get_container_args())

    if daemon_type in Monitoring.components:
        uid, gid = extract_uid_gid_monitoring(ctx, daemon_type)
        monitoring_args = [
            '--user',
            str(uid),
            # FIXME: disable cpu/memory limits for the time being (not supported
            # by ubuntu 18.04 kernel!)
        ]
        container_args.extend(monitoring_args)
    elif daemon_type == 'crash':
        ceph_args = ['-n', name]
    elif daemon_type in Ceph.daemons:
        ceph_args = ['-n', name, '-f']

    # if using podman, set -d, --conmon-pidfile & --cidfile flags
    # so service can have Type=Forking
    if isinstance(ctx.container_engine, Podman):
        runtime_dir = '/run'
        container_args.extend([
            '-d', '--log-driver', 'journald',
            '--conmon-pidfile',
            runtime_dir + '/ceph-%s@%s.%s.service-pid' % (fsid, daemon_type, daemon_id),
            '--cidfile',
            runtime_dir + '/ceph-%s@%s.%s.service-cid' % (fsid, daemon_type, daemon_id),
        ])
        if ctx.container_engine.version >= CGROUPS_SPLIT_PODMAN_VERSION:
            container_args.append('--cgroups=split')

    return CephContainer(
        ctx,
        image=ctx.image,
        entrypoint=entrypoint,
        args=ceph_args + get_daemon_args(ctx, fsid, daemon_type, daemon_id),
        container_args=container_args,
        volume_mounts=get_container_mounts(ctx, fsid, daemon_type, daemon_id),
        bind_mounts=get_container_binds(ctx, fsid, daemon_type, daemon_id),
        cname='ceph-%s-%s.%s' % (fsid, daemon_type, daemon_id),
        envs=envs,
        privileged=privileged,
        ptrace=ptrace,
        host_network=host_network,
    )


def extract_uid_gid(ctx, img='', file_path='/var/lib/ceph'):
    # type: (CephadmContext, str, Union[str, List[str]]) -> Tuple[int, int]

    if not img:
        img = ctx.image

    if isinstance(file_path, str):
        paths = [file_path]
    else:
        paths = file_path

    for fp in paths:
        try:
            out = CephContainer(
                ctx,
                image=img,
                entrypoint='stat',
                args=['-c', '%u %g', fp]
            ).run()
            uid, gid = out.split(' ')
            return int(uid), int(gid)
        except RuntimeError:
            pass
    raise RuntimeError('uid/gid not found')


def deploy_daemon(ctx, fsid, daemon_type, daemon_id, c, uid, gid,
                  config=None, keyring=None,
                  osd_fsid=None,
                  reconfig=False,
                  ports=None):
    # type: (CephadmContext, str, str, Union[int, str], Optional[CephContainer], int, int, Optional[str], Optional[str], Optional[str], Optional[bool], Optional[List[int]]) -> None

    ports = ports or []
    if any([port_in_use(ctx, port) for port in ports]):
        if daemon_type == 'mgr':
            # non-fatal for mgr when we are in mgr_standby_modules=false, but we can't
            # tell whether that is the case here.
            logger.warning(
                f"ceph-mgr TCP port(s) {','.join(map(str, ports))} already in use"
            )
        else:
            raise Error("TCP Port(s) '{}' required for {} already in use".format(','.join(map(str, ports)), daemon_type))

    data_dir = get_data_dir(fsid, ctx.data_dir, daemon_type, daemon_id)
    if reconfig and not os.path.exists(data_dir):
        raise Error('cannot reconfig, data path %s does not exist' % data_dir)
    if daemon_type == 'mon' and not os.path.exists(data_dir):
        assert config
        assert keyring
        # tmp keyring file
        tmp_keyring = write_tmp(keyring, uid, gid)

        # tmp config file
        tmp_config = write_tmp(config, uid, gid)

        # --mkfs
        create_daemon_dirs(ctx, fsid, daemon_type, daemon_id, uid, gid)
        mon_dir = get_data_dir(fsid, ctx.data_dir, 'mon', daemon_id)
        log_dir = get_log_dir(fsid, ctx.log_dir)
        CephContainer(
            ctx,
            image=ctx.image,
            entrypoint='/usr/bin/ceph-mon',
            args=[
                '--mkfs',
                '-i', str(daemon_id),
                '--fsid', fsid,
                '-c', '/tmp/config',
                '--keyring', '/tmp/keyring',
            ] + get_daemon_args(ctx, fsid, 'mon', daemon_id),
            volume_mounts={
                log_dir: '/var/log/ceph:z',
                mon_dir: '/var/lib/ceph/mon/ceph-%s:z' % (daemon_id),
                tmp_keyring.name: '/tmp/keyring:z',
                tmp_config.name: '/tmp/config:z',
            },
        ).run()

        # write conf
        with open(mon_dir + '/config', 'w') as f:
            os.fchown(f.fileno(), uid, gid)
            os.fchmod(f.fileno(), 0o600)
            f.write(config)
    else:
        # dirs, conf, keyring
        create_daemon_dirs(
            ctx,
            fsid, daemon_type, daemon_id,
            uid, gid,
            config, keyring)

    if not reconfig:
        if daemon_type == CephadmDaemon.daemon_type:
            port = next(iter(ports), None)  # get first tcp port provided or None

            if ctx.config_json == '-':
                config_js = get_parm('-')
            else:
                config_js = get_parm(ctx.config_json)
            assert isinstance(config_js, dict)

            cephadm_exporter = CephadmDaemon(ctx, fsid, daemon_id, port)
            cephadm_exporter.deploy_daemon_unit(config_js)
        else:
            if c:
                deploy_daemon_units(ctx, fsid, uid, gid, daemon_type, daemon_id,
                                    c, osd_fsid=osd_fsid, ports=ports)
            else:
                raise RuntimeError('attempting to deploy a daemon without a container image')

    if not os.path.exists(data_dir + '/unit.created'):
        with open(data_dir + '/unit.created', 'w') as f:
            os.fchmod(f.fileno(), 0o600)
            os.fchown(f.fileno(), uid, gid)
            f.write('mtime is time the daemon deployment was created\n')

    with open(data_dir + '/unit.configured', 'w') as f:
        f.write('mtime is time we were last configured\n')
        os.fchmod(f.fileno(), 0o600)
        os.fchown(f.fileno(), uid, gid)

    update_firewalld(ctx, daemon_type)

    # Open ports explicitly required for the daemon
    if ports:
        fw = Firewalld(ctx)
        fw.open_ports(ports)
        fw.apply_rules()

    if reconfig and daemon_type not in Ceph.daemons:
        # ceph daemons do not need a restart; others (presumably) do to pick
        # up the new config
        call_throws(ctx, ['systemctl', 'reset-failed',
                          get_unit_name(fsid, daemon_type, daemon_id)])
        call_throws(ctx, ['systemctl', 'restart',
                          get_unit_name(fsid, daemon_type, daemon_id)])


def _write_container_cmd_to_bash(ctx, file_obj, container, comment=None, background=False):
    # type: (CephadmContext, IO[str], CephContainer, Optional[str], Optional[bool]) -> None
    if comment:
        # Sometimes adding a comment, especially if there are multiple containers in one
        # unit file, makes it easier to read and grok.
        file_obj.write('# ' + comment + '\n')
    # Sometimes, adding `--rm` to a run_cmd doesn't work. Let's remove the container manually
    file_obj.write('! ' + ' '.join(container.rm_cmd()) + ' 2> /dev/null\n')
    # Sometimes, `podman rm` doesn't find the container. Then you'll have to add `--storage`
    if isinstance(ctx.container_engine, Podman):
        file_obj.write(
            '! '
            + ' '.join([shlex.quote(a) for a in container.rm_cmd(storage=True)])
            + ' 2> /dev/null\n')

    # container run command
    file_obj.write(
        ' '.join([shlex.quote(a) for a in container.run_cmd()])
        + (' &' if background else '') + '\n')


def deploy_daemon_units(
    ctx: CephadmContext,
    fsid: str,
    uid: int,
    gid: int,
    daemon_type: str,
    daemon_id: Union[int, str],
    c: 'CephContainer',
    enable: bool = True,
    start: bool = True,
    osd_fsid: Optional[str] = None,
    ports: Optional[List[int]] = None,
) -> None:
    # cmd
    data_dir = get_data_dir(fsid, ctx.data_dir, daemon_type, daemon_id)
    with open(data_dir + '/unit.run.new', 'w') as f, \
            open(data_dir + '/unit.meta.new', 'w') as metaf:
        f.write('set -e\n')

        if daemon_type in Ceph.daemons:
            install_path = find_program('install')
            f.write('{install_path} -d -m0770 -o {uid} -g {gid} /var/run/ceph/{fsid}\n'.format(install_path=install_path, fsid=fsid, uid=uid, gid=gid))

        # pre-start cmd(s)
        if daemon_type == 'osd':
            # osds have a pre-start step
            assert osd_fsid
            simple_fn = os.path.join('/etc/ceph/osd',
                                     '%s-%s.json.adopted-by-cephadm' % (daemon_id, osd_fsid))
            if os.path.exists(simple_fn):
                f.write('# Simple OSDs need chown on startup:\n')
                for n in ['block', 'block.db', 'block.wal']:
                    p = os.path.join(data_dir, n)
                    f.write('[ ! -L {p} ] || chown {uid}:{gid} {p}\n'.format(p=p, uid=uid, gid=gid))
            else:
                prestart = CephContainer(
                    ctx,
                    image=ctx.image,
                    entrypoint='/usr/sbin/ceph-volume',
                    args=[
                        'lvm', 'activate',
                        str(daemon_id), osd_fsid,
                        '--no-systemd'
                    ],
                    privileged=True,
                    volume_mounts=get_container_mounts(ctx, fsid, daemon_type, daemon_id),
                    bind_mounts=get_container_binds(ctx, fsid, daemon_type, daemon_id),
                    cname='ceph-%s-%s.%s-activate' % (fsid, daemon_type, daemon_id),
                    memory_request=ctx.memory_request,
                    memory_limit=ctx.memory_limit,
                )
                _write_container_cmd_to_bash(ctx, f, prestart, 'LVM OSDs use ceph-volume lvm activate')
        elif daemon_type == CephIscsi.daemon_type:
            f.write(' '.join(CephIscsi.configfs_mount_umount(data_dir, mount=True)) + '\n')
            ceph_iscsi = CephIscsi.init(ctx, fsid, daemon_id)
            tcmu_container = ceph_iscsi.get_tcmu_runner_container()
            _write_container_cmd_to_bash(ctx, f, tcmu_container, 'iscsi tcmu-runnter container', background=True)

        _write_container_cmd_to_bash(ctx, f, c, '%s.%s' % (daemon_type, str(daemon_id)))

        # some metadata about the deploy
        meta: Dict[str, Any] = {}
        if 'meta_json' in ctx and ctx.meta_json:
            meta = json.loads(ctx.meta_json) or {}
        meta.update({
            'memory_request': int(ctx.memory_request) if ctx.memory_request else None,
            'memory_limit': int(ctx.memory_limit) if ctx.memory_limit else None,
        })
        if not meta.get('ports'):
            meta['ports'] = ports
        metaf.write(json.dumps(meta, indent=4) + '\n')

        os.fchmod(f.fileno(), 0o600)
        os.fchmod(metaf.fileno(), 0o600)
        os.rename(data_dir + '/unit.run.new',
                  data_dir + '/unit.run')
        os.rename(data_dir + '/unit.meta.new',
                  data_dir + '/unit.meta')

    # post-stop command(s)
    with open(data_dir + '/unit.poststop.new', 'w') as f:
        if daemon_type == 'osd':
            assert osd_fsid
            poststop = CephContainer(
                ctx,
                image=ctx.image,
                entrypoint='/usr/sbin/ceph-volume',
                args=[
                    'lvm', 'deactivate',
                    str(daemon_id), osd_fsid,
                ],
                privileged=True,
                volume_mounts=get_container_mounts(ctx, fsid, daemon_type, daemon_id),
                bind_mounts=get_container_binds(ctx, fsid, daemon_type, daemon_id),
                cname='ceph-%s-%s.%s-deactivate' % (fsid, daemon_type,
                                                    daemon_id),
            )
            _write_container_cmd_to_bash(ctx, f, poststop, 'deactivate osd')
        elif daemon_type == CephIscsi.daemon_type:
            # make sure we also stop the tcmu container
            ceph_iscsi = CephIscsi.init(ctx, fsid, daemon_id)
            tcmu_container = ceph_iscsi.get_tcmu_runner_container()
            f.write('! ' + ' '.join(tcmu_container.stop_cmd()) + '\n')
            f.write(' '.join(CephIscsi.configfs_mount_umount(data_dir, mount=False)) + '\n')
        os.fchmod(f.fileno(), 0o600)
        os.rename(data_dir + '/unit.poststop.new',
                  data_dir + '/unit.poststop')

    if c:
        with open(data_dir + '/unit.image.new', 'w') as f:
            f.write(c.image + '\n')
            os.fchmod(f.fileno(), 0o600)
            os.rename(data_dir + '/unit.image.new',
                      data_dir + '/unit.image')

    # sysctl
    install_sysctl(ctx, fsid, daemon_type)

    # systemd
    install_base_units(ctx, fsid)
    unit = get_unit_file(ctx, fsid)
    unit_file = 'ceph-%s@.service' % (fsid)
    with open(ctx.unit_dir + '/' + unit_file + '.new', 'w') as f:
        f.write(unit)
        os.rename(ctx.unit_dir + '/' + unit_file + '.new',
                  ctx.unit_dir + '/' + unit_file)
    call_throws(ctx, ['systemctl', 'daemon-reload'])

    unit_name = get_unit_name(fsid, daemon_type, daemon_id)
    call(ctx, ['systemctl', 'stop', unit_name],
         verbosity=CallVerbosity.DEBUG)
    call(ctx, ['systemctl', 'reset-failed', unit_name],
         verbosity=CallVerbosity.DEBUG)
    if enable:
        call_throws(ctx, ['systemctl', 'enable', unit_name])
    if start:
        call_throws(ctx, ['systemctl', 'start', unit_name])


class Firewalld(object):
    def __init__(self, ctx):
        # type: (CephadmContext) -> None
        self.ctx = ctx
        self.available = self.check()

    def check(self):
        # type: () -> bool
        self.cmd = find_executable('firewall-cmd')
        if not self.cmd:
            logger.debug('firewalld does not appear to be present')
            return False
        (enabled, state, _) = check_unit(self.ctx, 'firewalld.service')
        if not enabled:
            logger.debug('firewalld.service is not enabled')
            return False
        if state != 'running':
            logger.debug('firewalld.service is not running')
            return False

        logger.info('firewalld ready')
        return True

    def enable_service_for(self, daemon_type):
        # type: (str) -> None
        if not self.available:
            logger.debug('Not possible to enable service <%s>. firewalld.service is not available' % daemon_type)
            return

        if daemon_type == 'mon':
            svc = 'ceph-mon'
        elif daemon_type in ['mgr', 'mds', 'osd']:
            svc = 'ceph'
        elif daemon_type == NFSGanesha.daemon_type:
            svc = 'nfs'
        else:
            return

        if not self.cmd:
            raise RuntimeError('command not defined')

        out, err, ret = call(self.ctx, [self.cmd, '--permanent', '--query-service', svc], verbosity=CallVerbosity.DEBUG)
        if ret:
            logger.info('Enabling firewalld service %s in current zone...' % svc)
            out, err, ret = call(self.ctx, [self.cmd, '--permanent', '--add-service', svc])
            if ret:
                raise RuntimeError(
                    'unable to add service %s to current zone: %s' % (svc, err))
        else:
            logger.debug('firewalld service %s is enabled in current zone' % svc)

    def open_ports(self, fw_ports):
        # type: (List[int]) -> None
        if not self.available:
            logger.debug('Not possible to open ports <%s>. firewalld.service is not available' % fw_ports)
            return

        if not self.cmd:
            raise RuntimeError('command not defined')

        for port in fw_ports:
            tcp_port = str(port) + '/tcp'
            out, err, ret = call(self.ctx, [self.cmd, '--permanent', '--query-port', tcp_port], verbosity=CallVerbosity.DEBUG)
            if ret:
                logger.info('Enabling firewalld port %s in current zone...' % tcp_port)
                out, err, ret = call(self.ctx, [self.cmd, '--permanent', '--add-port', tcp_port])
                if ret:
                    raise RuntimeError('unable to add port %s to current zone: %s' %
                                       (tcp_port, err))
            else:
                logger.debug('firewalld port %s is enabled in current zone' % tcp_port)

    def close_ports(self, fw_ports):
        # type: (List[int]) -> None
        if not self.available:
            logger.debug('Not possible to close ports <%s>. firewalld.service is not available' % fw_ports)
            return

        if not self.cmd:
            raise RuntimeError('command not defined')

        for port in fw_ports:
            tcp_port = str(port) + '/tcp'
            out, err, ret = call(self.ctx, [self.cmd, '--permanent', '--query-port', tcp_port], verbosity=CallVerbosity.DEBUG)
            if not ret:
                logger.info('Disabling port %s in current zone...' % tcp_port)
                out, err, ret = call(self.ctx, [self.cmd, '--permanent', '--remove-port', tcp_port])
                if ret:
                    raise RuntimeError('unable to remove port %s from current zone: %s' %
                                       (tcp_port, err))
                else:
                    logger.info(f'Port {tcp_port} disabled')
            else:
                logger.info(f'firewalld port {tcp_port} already closed')

    def apply_rules(self):
        # type: () -> None
        if not self.available:
            return

        if not self.cmd:
            raise RuntimeError('command not defined')

        call_throws(self.ctx, [self.cmd, '--reload'])


def update_firewalld(ctx, daemon_type):
    # type: (CephadmContext, str) -> None
    firewall = Firewalld(ctx)
    firewall.enable_service_for(daemon_type)
    firewall.apply_rules()


def install_sysctl(ctx: CephadmContext, fsid: str, daemon_type: str) -> None:
    """
    Set up sysctl settings
    """
    def _write(conf: Path, lines: List[str]) -> None:
        lines = [
            '# created by cephadm',
            '',
            *lines,
            '',
        ]
        with open(conf, 'w') as f:
            f.write('\n'.join(lines))

    conf = Path(ctx.sysctl_dir).joinpath(f'90-ceph-{fsid}-{daemon_type}.conf')
    lines: Optional[List] = None

    if daemon_type == 'osd':
        lines = OSD.get_sysctl_settings()
    elif daemon_type == 'haproxy':
        lines = HAproxy.get_sysctl_settings()
    elif daemon_type == 'keepalived':
        lines = Keepalived.get_sysctl_settings()

    # apply the sysctl settings
    if lines:
        _write(conf, lines)
        call_throws(ctx, ['sysctl', '--system'])


def install_base_units(ctx, fsid):
    # type: (CephadmContext, str) -> None
    """
    Set up ceph.target and ceph-$fsid.target units.
    """
    # global unit
    existed = os.path.exists(ctx.unit_dir + '/ceph.target')
    with open(ctx.unit_dir + '/ceph.target.new', 'w') as f:
        f.write('[Unit]\n'
                'Description=All Ceph clusters and services\n'
                '\n'
                '[Install]\n'
                'WantedBy=multi-user.target\n')
        os.rename(ctx.unit_dir + '/ceph.target.new',
                  ctx.unit_dir + '/ceph.target')
    if not existed:
        # we disable before enable in case a different ceph.target
        # (from the traditional package) is present; while newer
        # systemd is smart enough to disable the old
        # (/lib/systemd/...) and enable the new (/etc/systemd/...),
        # some older versions of systemd error out with EEXIST.
        call_throws(ctx, ['systemctl', 'disable', 'ceph.target'])
        call_throws(ctx, ['systemctl', 'enable', 'ceph.target'])
        call_throws(ctx, ['systemctl', 'start', 'ceph.target'])

    # cluster unit
    existed = os.path.exists(ctx.unit_dir + '/ceph-%s.target' % fsid)
    with open(ctx.unit_dir + '/ceph-%s.target.new' % fsid, 'w') as f:
        f.write(
            '[Unit]\n'
            'Description=Ceph cluster {fsid}\n'
            'PartOf=ceph.target\n'
            'Before=ceph.target\n'
            '\n'
            '[Install]\n'
            'WantedBy=multi-user.target ceph.target\n'.format(
                fsid=fsid)
        )
        os.rename(ctx.unit_dir + '/ceph-%s.target.new' % fsid,
                  ctx.unit_dir + '/ceph-%s.target' % fsid)
    if not existed:
        call_throws(ctx, ['systemctl', 'enable', 'ceph-%s.target' % fsid])
        call_throws(ctx, ['systemctl', 'start', 'ceph-%s.target' % fsid])

    # logrotate for the cluster
    with open(ctx.logrotate_dir + '/ceph-%s' % fsid, 'w') as f:
        """
        This is a bit sloppy in that the killall/pkill will touch all ceph daemons
        in all containers, but I don't see an elegant way to send SIGHUP *just* to
        the daemons for this cluster.  (1) systemd kill -s will get the signal to
        podman, but podman will exit.  (2) podman kill will get the signal to the
        first child (bash), but that isn't the ceph daemon.  This is simpler and
        should be harmless.
        """
        f.write("""# created by cephadm
/var/log/ceph/%s/*.log {
    rotate 7
    daily
    compress
    sharedscripts
    postrotate
        killall -q -1 ceph-mon ceph-mgr ceph-mds ceph-osd ceph-fuse radosgw rbd-mirror cephfs-mirror || pkill -1 -x 'ceph-mon|ceph-mgr|ceph-mds|ceph-osd|ceph-fuse|radosgw|rbd-mirror|cephfs-mirror' || true
    endscript
    missingok
    notifempty
    su root root
}
""" % fsid)


def get_unit_file(ctx, fsid):
    # type: (CephadmContext, str) -> str
    extra_args = ''
    if isinstance(ctx.container_engine, Podman):
        extra_args = ('ExecStartPre=-/bin/rm -f %t/%n-pid %t/%n-cid\n'
                      'ExecStopPost=-/bin/rm -f %t/%n-pid %t/%n-cid\n'
                      'Type=forking\n'
                      'PIDFile=%t/%n-pid\n')
        if ctx.container_engine.version >= CGROUPS_SPLIT_PODMAN_VERSION:
            extra_args += 'Delegate=yes\n'

    docker = isinstance(ctx.container_engine, Docker)
    u = """# generated by cephadm
[Unit]
Description=Ceph %i for {fsid}

# According to:
#   http://www.freedesktop.org/wiki/Software/systemd/NetworkTarget
# these can be removed once ceph-mon will dynamically change network
# configuration.
After=network-online.target local-fs.target time-sync.target{docker_after}
Wants=network-online.target local-fs.target time-sync.target
{docker_requires}

PartOf=ceph-{fsid}.target
Before=ceph-{fsid}.target

[Service]
LimitNOFILE=1048576
LimitNPROC=1048576
EnvironmentFile=-/etc/environment
ExecStart=/bin/bash {data_dir}/{fsid}/%i/unit.run
ExecStop=-{container_path} stop ceph-{fsid}-%i
ExecStopPost=-/bin/bash {data_dir}/{fsid}/%i/unit.poststop
KillMode=none
Restart=on-failure
RestartSec=10s
TimeoutStartSec=120
TimeoutStopSec=120
StartLimitInterval=30min
StartLimitBurst=5
{extra_args}
[Install]
WantedBy=ceph-{fsid}.target
""".format(container_path=ctx.container_engine.path,
           fsid=fsid,
           data_dir=ctx.data_dir,
           extra_args=extra_args,
           # if docker, we depend on docker.service
           docker_after=' docker.service' if docker else '',
           docker_requires='Requires=docker.service\n' if docker else '')

    return u

##################################


class CephContainer:
    def __init__(self,
                 ctx: CephadmContext,
                 image: str,
                 entrypoint: str,
                 args: List[str] = [],
                 volume_mounts: Dict[str, str] = {},
                 cname: str = '',
                 container_args: List[str] = [],
                 envs: Optional[List[str]] = None,
                 privileged: bool = False,
                 ptrace: bool = False,
                 bind_mounts: Optional[List[List[str]]] = None,
                 init: Optional[bool] = None,
                 host_network: bool = True,
                 memory_request: Optional[str] = None,
                 memory_limit: Optional[str] = None,
                 ) -> None:
        self.ctx = ctx
        self.image = image
        self.entrypoint = entrypoint
        self.args = args
        self.volume_mounts = volume_mounts
        self.cname = cname
        self.container_args = container_args
        self.envs = envs
        self.privileged = privileged
        self.ptrace = ptrace
        self.bind_mounts = bind_mounts if bind_mounts else []
        self.init = init if init else ctx.container_init
        self.host_network = host_network
        self.memory_request = memory_request
        self.memory_limit = memory_limit

    def run_cmd(self) -> List[str]:
        cmd_args: List[str] = [
            str(self.ctx.container_engine.path),
            'run',
            '--rm',
            '--ipc=host',
            # some containers (ahem, haproxy) override this, but we want a fast
            # shutdown always (and, more importantly, a successful exit even if we
            # fall back to SIGKILL).
            '--stop-signal=SIGTERM',
        ]

        if isinstance(self.ctx.container_engine, Podman):
            if os.path.exists('/etc/ceph/podman-auth.json'):
                cmd_args.append('--authfile=/etc/ceph/podman-auth.json')

        envs: List[str] = [
            '-e', 'CONTAINER_IMAGE=%s' % self.image,
            '-e', 'NODE_NAME=%s' % get_hostname(),
        ]
        vols: List[str] = []
        binds: List[str] = []

        if self.memory_request:
            cmd_args.extend(['-e', 'POD_MEMORY_REQUEST', str(self.memory_request)])
        if self.memory_limit:
            cmd_args.extend(['-e', 'POD_MEMORY_LIMIT', str(self.memory_limit)])
            cmd_args.extend(['--memory', str(self.memory_limit)])

        if self.host_network:
            cmd_args.append('--net=host')
        if self.entrypoint:
            cmd_args.extend(['--entrypoint', self.entrypoint])
        if self.privileged:
            cmd_args.extend([
                '--privileged',
                # let OSD etc read block devs that haven't been chowned
                '--group-add=disk'])
        if self.ptrace and not self.privileged:
            # if privileged, the SYS_PTRACE cap is already added
            # in addition, --cap-add and --privileged are mutually
            # exclusive since podman >= 2.0
            cmd_args.append('--cap-add=SYS_PTRACE')
        if self.init:
            cmd_args.append('--init')
            envs += ['-e', 'CEPH_USE_RANDOM_NONCE=1']
        if self.cname:
            cmd_args.extend(['--name', self.cname])
        if self.envs:
            for env in self.envs:
                envs.extend(['-e', env])

        vols = sum(
            [['-v', '%s:%s' % (host_dir, container_dir)]
             for host_dir, container_dir in self.volume_mounts.items()], [])
        binds = sum([['--mount', '{}'.format(','.join(bind))]
                     for bind in self.bind_mounts], [])

        return \
            cmd_args + self.container_args + \
            envs + vols + binds + \
            [self.image] + self.args  # type: ignore

    def shell_cmd(self, cmd: List[str]) -> List[str]:
        cmd_args: List[str] = [
            str(self.ctx.container_engine.path),
            'run',
            '--rm',
            '--ipc=host',
        ]
        envs: List[str] = [
            '-e', 'CONTAINER_IMAGE=%s' % self.image,
            '-e', 'NODE_NAME=%s' % get_hostname(),
        ]
        vols: List[str] = []
        binds: List[str] = []

        if self.host_network:
            cmd_args.append('--net=host')
        if self.ctx.no_hosts:
            cmd_args.append('--no-hosts')
        if self.privileged:
            cmd_args.extend([
                '--privileged',
                # let OSD etc read block devs that haven't been chowned
                '--group-add=disk',
            ])
        if self.init:
            cmd_args.append('--init')
            envs += ['-e', 'CEPH_USE_RANDOM_NONCE=1']
        if self.envs:
            for env in self.envs:
                envs.extend(['-e', env])

        vols = sum(
            [['-v', '%s:%s' % (host_dir, container_dir)]
             for host_dir, container_dir in self.volume_mounts.items()], [])
        binds = sum([['--mount', '{}'.format(','.join(bind))]
                     for bind in self.bind_mounts], [])

        return cmd_args + self.container_args + envs + vols + binds + [
            '--entrypoint', cmd[0],
            self.image,
        ] + cmd[1:]

    def exec_cmd(self, cmd):
        # type: (List[str]) -> List[str]
        return [
            str(self.ctx.container_engine.path),
            'exec',
        ] + self.container_args + [
            self.cname,
        ] + cmd

    def rm_cmd(self, storage=False):
        # type: (bool) -> List[str]
        ret = [
            str(self.ctx.container_engine.path),
            'rm', '-f',
        ]
        if storage:
            ret.append('--storage')
        ret.append(self.cname)
        return ret

    def stop_cmd(self):
        # type () -> List[str]
        ret = [
            str(self.ctx.container_engine.path),
            'stop', self.cname,
        ]
        return ret

    def run(self, timeout=DEFAULT_TIMEOUT):
        # type: (Optional[int]) -> str
        out, _, _ = call_throws(self.ctx, self.run_cmd(),
                                desc=self.entrypoint, timeout=timeout)
        return out

##################################


@infer_image
def command_version(ctx):
    # type: (CephadmContext) -> int
    c = CephContainer(ctx, ctx.image, 'ceph', ['--version'])
    out, err, ret = call(ctx, c.run_cmd(), desc=c.entrypoint)
    if not ret:
        print(out.strip())
    return ret

##################################


@infer_image
def command_pull(ctx):
    # type: (CephadmContext) -> int

    _pull_image(ctx, ctx.image)
    return command_inspect_image(ctx)


def _pull_image(ctx, image):
    # type: (CephadmContext, str) -> None
    logger.info('Pulling container image %s...' % image)

    ignorelist = [
        'error creating read-write layer with ID',
        'net/http: TLS handshake timeout',
        'Digest did not match, expected',
    ]

    cmd = [ctx.container_engine.path, 'pull', image]
    if isinstance(ctx.container_engine, Podman) and os.path.exists('/etc/ceph/podman-auth.json'):
        cmd.append('--authfile=/etc/ceph/podman-auth.json')
    cmd_str = ' '.join(cmd)

    for sleep_secs in [1, 4, 25]:
        out, err, ret = call(ctx, cmd)
        if not ret:
            return

        if not any(pattern in err for pattern in ignorelist):
            raise RuntimeError('Failed command: %s' % cmd_str)

        logger.info('`%s` failed transiently. Retrying. waiting %s seconds...' % (cmd_str, sleep_secs))
        time.sleep(sleep_secs)

    raise RuntimeError('Failed command: %s: maximum retries reached' % cmd_str)

##################################


@infer_image
def command_inspect_image(ctx):
    # type: (CephadmContext) -> int
    out, err, ret = call_throws(ctx, [
        ctx.container_engine.path, 'inspect',
        '--format', '{{.ID}},{{.RepoDigests}}',
        ctx.image])
    if ret:
        return errno.ENOENT
    info_from = get_image_info_from_inspect(out.strip(), ctx.image)

    ver = CephContainer(ctx, ctx.image, 'ceph', ['--version']).run().strip()
    info_from['ceph_version'] = ver

    print(json.dumps(info_from, indent=4, sort_keys=True))
    return 0


def normalize_image_digest(digest):
    # normal case:
    #   ceph/ceph -> docker.io/ceph/ceph
    # edge cases that shouldn't ever come up:
    #   ubuntu -> docker.io/ubuntu    (ubuntu alias for library/ubuntu)
    # no change:
    #   quay.ceph.io/ceph/ceph -> ceph
    #   docker.io/ubuntu -> no change
    bits = digest.split('/')
    if '.' not in bits[0] or len(bits) < 3:
        digest = DEFAULT_REGISTRY + '/' + digest
    return digest


def get_image_info_from_inspect(out, image):
    # type: (str, str) -> Dict[str, Union[str,List[str]]]
    image_id, digests = out.split(',', 1)
    if not out:
        raise Error('inspect {}: empty result'.format(image))
    r = {
        'image_id': normalize_container_id(image_id)
    }  # type: Dict[str, Union[str,List[str]]]
    if digests:
        r['repo_digests'] = list(map(normalize_image_digest, digests[1:-1].split(' ')))
    return r

##################################


def check_subnet(subnets: str) -> Tuple[int, List[int], str]:
    """Determine whether the given string is a valid subnet

    :param subnets: subnet string, a single definition or comma separated list of CIDR subnets
    :returns: return code, IP version list of the subnets and msg describing any errors validation errors
    """

    rc = 0
    versions = set()
    errors = []
    subnet_list = subnets.split(',')
    for subnet in subnet_list:
        # ensure the format of the string is as expected address/netmask
        if not re.search(r'\/\d+$', subnet):
            rc = 1
            errors.append(f'{subnet} is not in CIDR format (address/netmask)')
            continue
        try:
            v = ipaddress.ip_network(subnet).version
            versions.add(v)
        except ValueError as e:
            rc = 1
            errors.append(f'{subnet} invalid: {str(e)}')

    return rc, list(versions), ', '.join(errors)


def unwrap_ipv6(address):
    # type: (str) -> str
    if address.startswith('[') and address.endswith(']'):
        return address[1:-1]
    return address


def wrap_ipv6(address):
    # type: (str) -> str

    # We cannot assume it's already wrapped or even an IPv6 address if
    # it's already wrapped it'll not pass (like if it's a hostname) and trigger
    # the ValueError
    try:
        if ipaddress.ip_address(address).version == 6:
            return f'[{address}]'
    except ValueError:
        pass

    return address


def is_ipv6(address):
    # type: (str) -> bool
    address = unwrap_ipv6(address)
    try:
        return ipaddress.ip_address(address).version == 6
    except ValueError:
        logger.warning('Address: {} is not a valid IP address'.format(address))
        return False


def prepare_mon_addresses(
    ctx: CephadmContext
) -> Tuple[str, bool, Optional[str]]:
    r = re.compile(r':(\d+)$')
    base_ip = ''
    ipv6 = False

    if ctx.mon_ip:
        ipv6 = is_ipv6(ctx.mon_ip)
        if ipv6:
            ctx.mon_ip = wrap_ipv6(ctx.mon_ip)
        hasport = r.findall(ctx.mon_ip)
        if hasport:
            port = int(hasport[0])
            if port == 6789:
                addr_arg = '[v1:%s]' % ctx.mon_ip
            elif port == 3300:
                addr_arg = '[v2:%s]' % ctx.mon_ip
            else:
                logger.warning('Using msgr2 protocol for unrecognized port %d' %
                               port)
                addr_arg = '[v2:%s]' % ctx.mon_ip
            base_ip = ctx.mon_ip[0:-(len(str(port))) - 1]
            check_ip_port(ctx, base_ip, port)
        else:
            base_ip = ctx.mon_ip
            addr_arg = '[v2:%s:3300,v1:%s:6789]' % (ctx.mon_ip, ctx.mon_ip)
            check_ip_port(ctx, ctx.mon_ip, 3300)
            check_ip_port(ctx, ctx.mon_ip, 6789)
    elif ctx.mon_addrv:
        addr_arg = ctx.mon_addrv
        if addr_arg[0] != '[' or addr_arg[-1] != ']':
            raise Error('--mon-addrv value %s must use square backets' %
                        addr_arg)
        ipv6 = addr_arg.count('[') > 1
        for addr in addr_arg[1:-1].split(','):
            hasport = r.findall(addr)
            if not hasport:
                raise Error('--mon-addrv value %s must include port number' %
                            addr_arg)
            port = int(hasport[0])
            # strip off v1: or v2: prefix
            addr = re.sub(r'^\w+:', '', addr)
            base_ip = addr[0:-(len(str(port))) - 1]
            check_ip_port(ctx, base_ip, port)
    else:
        raise Error('must specify --mon-ip or --mon-addrv')
    logger.debug('Base mon IP is %s, final addrv is %s' % (base_ip, addr_arg))

    mon_network = None
    if not ctx.skip_mon_network:
        # make sure IP is configured locally, and then figure out the
        # CIDR network
        for net, ifaces in list_networks(ctx).items():
            ips: List[str] = []
            for iface, ls in ifaces.items():
                ips.extend(ls)
            if ipaddress.ip_address(unwrap_ipv6(base_ip)) in \
                    [ipaddress.ip_address(ip) for ip in ips]:
                mon_network = net
                logger.info('Mon IP %s is in CIDR network %s' % (base_ip,
                                                                 mon_network))
                break
        if not mon_network:
            raise Error('Failed to infer CIDR network for mon ip %s; pass '
                        '--skip-mon-network to configure it later' % base_ip)

    return (addr_arg, ipv6, mon_network)


def prepare_cluster_network(ctx: CephadmContext) -> Tuple[str, bool]:
    cluster_network = ''
    ipv6_cluster_network = False
    # the cluster network may not exist on this node, so all we can do is
    # validate that the address given is valid ipv4 or ipv6 subnet
    if ctx.cluster_network:
        rc, versions, err_msg = check_subnet(ctx.cluster_network)
        if rc:
            raise Error(f'Invalid --cluster-network parameter: {err_msg}')
        cluster_network = ctx.cluster_network
        ipv6_cluster_network = True if 6 in versions else False
    else:
        logger.info('- internal network (--cluster-network) has not '
                    'been provided, OSD replication will default to '
                    'the public_network')

    return cluster_network, ipv6_cluster_network


def create_initial_keys(
    ctx: CephadmContext,
    uid: int, gid: int,
    mgr_id: str
) -> Tuple[str, str, str, Any, Any]:  # type: ignore

    _image = ctx.image

    # create some initial keys
    logger.info('Creating initial keys...')
    mon_key = CephContainer(
        ctx,
        image=_image,
        entrypoint='/usr/bin/ceph-authtool',
        args=['--gen-print-key'],
    ).run().strip()
    admin_key = CephContainer(
        ctx,
        image=_image,
        entrypoint='/usr/bin/ceph-authtool',
        args=['--gen-print-key'],
    ).run().strip()
    mgr_key = CephContainer(
        ctx,
        image=_image,
        entrypoint='/usr/bin/ceph-authtool',
        args=['--gen-print-key'],
    ).run().strip()

    keyring = ('[mon.]\n'
               '\tkey = %s\n'
               '\tcaps mon = allow *\n'
               '[client.admin]\n'
               '\tkey = %s\n'
               '\tcaps mon = allow *\n'
               '\tcaps mds = allow *\n'
               '\tcaps mgr = allow *\n'
               '\tcaps osd = allow *\n'
               '[mgr.%s]\n'
               '\tkey = %s\n'
               '\tcaps mon = profile mgr\n'
               '\tcaps mds = allow *\n'
               '\tcaps osd = allow *\n'
               % (mon_key, admin_key, mgr_id, mgr_key))

    admin_keyring = write_tmp('[client.admin]\n'
                              '\tkey = ' + admin_key + '\n',
                              uid, gid)

    # tmp keyring file
    bootstrap_keyring = write_tmp(keyring, uid, gid)
    return (mon_key, mgr_key, admin_key,
            bootstrap_keyring, admin_keyring)


def create_initial_monmap(
    ctx: CephadmContext,
    uid: int, gid: int,
    fsid: str,
    mon_id: str, mon_addr: str
) -> Any:
    logger.info('Creating initial monmap...')
    monmap = write_tmp('', 0, 0)
    out = CephContainer(
        ctx,
        image=ctx.image,
        entrypoint='/usr/bin/monmaptool',
        args=[
            '--create',
            '--clobber',
            '--fsid', fsid,
            '--addv', mon_id, mon_addr,
            '/tmp/monmap'
        ],
        volume_mounts={
            monmap.name: '/tmp/monmap:z',
        },
    ).run()
    logger.debug(f'monmaptool for {mon_id} {mon_addr} on {out}')

    # pass monmap file to ceph user for use by ceph-mon --mkfs below
    os.fchown(monmap.fileno(), uid, gid)
    return monmap


def prepare_create_mon(
    ctx: CephadmContext,
    uid: int, gid: int,
    fsid: str, mon_id: str,
    bootstrap_keyring_path: str,
    monmap_path: str
):
    logger.info('Creating mon...')
    create_daemon_dirs(ctx, fsid, 'mon', mon_id, uid, gid)
    mon_dir = get_data_dir(fsid, ctx.data_dir, 'mon', mon_id)
    log_dir = get_log_dir(fsid, ctx.log_dir)
    out = CephContainer(
        ctx,
        image=ctx.image,
        entrypoint='/usr/bin/ceph-mon',
        args=[
            '--mkfs',
            '-i', mon_id,
            '--fsid', fsid,
            '-c', '/dev/null',
            '--monmap', '/tmp/monmap',
            '--keyring', '/tmp/keyring',
        ] + get_daemon_args(ctx, fsid, 'mon', mon_id),
        volume_mounts={
            log_dir: '/var/log/ceph:z',
            mon_dir: '/var/lib/ceph/mon/ceph-%s:z' % (mon_id),
            bootstrap_keyring_path: '/tmp/keyring:z',
            monmap_path: '/tmp/monmap:z',
        },
    ).run()
    logger.debug(f'create mon.{mon_id} on {out}')
    return (mon_dir, log_dir)


def create_mon(
    ctx: CephadmContext,
    uid: int, gid: int,
    fsid: str, mon_id: str
) -> None:
    mon_c = get_container(ctx, fsid, 'mon', mon_id)
    ctx.meta_json = json.dumps({'service_name': 'mon'})
    deploy_daemon(ctx, fsid, 'mon', mon_id, mon_c, uid, gid,
                  config=None, keyring=None)


def wait_for_mon(
    ctx: CephadmContext,
    mon_id: str, mon_dir: str,
    admin_keyring_path: str, config_path: str
):
    logger.info('Waiting for mon to start...')
    c = CephContainer(
        ctx,
        image=ctx.image,
        entrypoint='/usr/bin/ceph',
        args=[
            'status'],
        volume_mounts={
            mon_dir: '/var/lib/ceph/mon/ceph-%s:z' % (mon_id),
            admin_keyring_path: '/etc/ceph/ceph.client.admin.keyring:z',
            config_path: '/etc/ceph/ceph.conf:z',
        },
    )

    # wait for the service to become available
    def is_mon_available():
        # type: () -> bool
        timeout = ctx.timeout if ctx.timeout else 60  # seconds
        out, err, ret = call(ctx, c.run_cmd(),
                             desc=c.entrypoint,
                             timeout=timeout)
        return ret == 0

    is_available(ctx, 'mon', is_mon_available)


def create_mgr(
    ctx: CephadmContext,
    uid: int, gid: int,
    fsid: str, mgr_id: str, mgr_key: str,
    config: str, clifunc: Callable
) -> None:
    logger.info('Creating mgr...')
    mgr_keyring = '[mgr.%s]\n\tkey = %s\n' % (mgr_id, mgr_key)
    mgr_c = get_container(ctx, fsid, 'mgr', mgr_id)
    # Note:the default port used by the Prometheus node exporter is opened in fw
    ctx.meta_json = json.dumps({'service_name': 'mgr'})
    deploy_daemon(ctx, fsid, 'mgr', mgr_id, mgr_c, uid, gid,
                  config=config, keyring=mgr_keyring, ports=[9283])

    # wait for the service to become available
    logger.info('Waiting for mgr to start...')

    def is_mgr_available():
        # type: () -> bool
        timeout = ctx.timeout if ctx.timeout else 60  # seconds
        try:
            out = clifunc(['status', '-f', 'json-pretty'], timeout=timeout)
            j = json.loads(out)
            return j.get('mgrmap', {}).get('available', False)
        except Exception as e:
            logger.debug('status failed: %s' % e)
            return False
    is_available(ctx, 'mgr', is_mgr_available)


def prepare_ssh(
    ctx: CephadmContext,
    cli: Callable, wait_for_mgr_restart: Callable
) -> None:

    cli(['cephadm', 'set-user', ctx.ssh_user])

    if ctx.ssh_config:
        logger.info('Using provided ssh config...')
        mounts = {
            pathify(ctx.ssh_config.name): '/tmp/cephadm-ssh-config:z',
        }
        cli(['cephadm', 'set-ssh-config', '-i', '/tmp/cephadm-ssh-config'], extra_mounts=mounts)

    if ctx.ssh_private_key and ctx.ssh_public_key:
        logger.info('Using provided ssh keys...')
        mounts = {
            pathify(ctx.ssh_private_key.name): '/tmp/cephadm-ssh-key:z',
            pathify(ctx.ssh_public_key.name): '/tmp/cephadm-ssh-key.pub:z'
        }
        cli(['cephadm', 'set-priv-key', '-i', '/tmp/cephadm-ssh-key'], extra_mounts=mounts)
        cli(['cephadm', 'set-pub-key', '-i', '/tmp/cephadm-ssh-key.pub'], extra_mounts=mounts)
    else:
        logger.info('Generating ssh key...')
        cli(['cephadm', 'generate-key'])
        ssh_pub = cli(['cephadm', 'get-pub-key'])

        with open(ctx.output_pub_ssh_key, 'w') as f:
            f.write(ssh_pub)
        logger.info('Wrote public SSH key to %s' % ctx.output_pub_ssh_key)

        logger.info('Adding key to %s@localhost authorized_keys...' % ctx.ssh_user)
        try:
            s_pwd = pwd.getpwnam(ctx.ssh_user)
        except KeyError:
            raise Error('Cannot find uid/gid for ssh-user: %s' % (ctx.ssh_user))
        ssh_uid = s_pwd.pw_uid
        ssh_gid = s_pwd.pw_gid
        ssh_dir = os.path.join(s_pwd.pw_dir, '.ssh')

        if not os.path.exists(ssh_dir):
            makedirs(ssh_dir, ssh_uid, ssh_gid, 0o700)

        auth_keys_file = '%s/authorized_keys' % ssh_dir
        add_newline = False

        if os.path.exists(auth_keys_file):
            with open(auth_keys_file, 'r') as f:
                f.seek(0, os.SEEK_END)
                if f.tell() > 0:
                    f.seek(f.tell() - 1, os.SEEK_SET)  # go to last char
                    if f.read() != '\n':
                        add_newline = True

        with open(auth_keys_file, 'a') as f:
            os.fchown(f.fileno(), ssh_uid, ssh_gid)  # just in case we created it
            os.fchmod(f.fileno(), 0o600)  # just in case we created it
            if add_newline:
                f.write('\n')
            f.write(ssh_pub.strip() + '\n')

    host = get_hostname()
    logger.info('Adding host %s...' % host)
    try:
        args = ['orch', 'host', 'add', host]
        if ctx.mon_ip:
            args.append(ctx.mon_ip)
        cli(args)
    except RuntimeError as e:
        raise Error('Failed to add host <%s>: %s' % (host, e))

    for t in ['mon', 'mgr']:
        if not ctx.orphan_initial_daemons:
            logger.info('Deploying %s service with default placement...' % t)
            cli(['orch', 'apply', t])
        else:
            logger.info('Deploying unmanaged %s service...' % t)
            cli(['orch', 'apply', t, '--unmanaged'])

    if not ctx.orphan_initial_daemons:
        logger.info('Deploying crash service with default placement...')
        cli(['orch', 'apply', 'crash'])

    if not ctx.skip_monitoring_stack:
        logger.info('Enabling mgr prometheus module...')
        cli(['mgr', 'module', 'enable', 'prometheus'])
        for t in ['prometheus', 'grafana', 'node-exporter', 'alertmanager']:
            logger.info('Deploying %s service with default placement...' % t)
            cli(['orch', 'apply', t])


def enable_cephadm_mgr_module(
    cli: Callable, wait_for_mgr_restart: Callable
) -> None:

    logger.info('Enabling cephadm module...')
    cli(['mgr', 'module', 'enable', 'cephadm'])
    wait_for_mgr_restart()
    logger.info('Setting orchestrator backend to cephadm...')
    cli(['orch', 'set', 'backend', 'cephadm'])


def prepare_dashboard(
    ctx: CephadmContext,
    uid: int, gid: int,
    cli: Callable, wait_for_mgr_restart: Callable
) -> None:

    # Configure SSL port (cephadm only allows to configure dashboard SSL port)
    # if the user does not want to use SSL he can change this setting once the cluster is up
    cli(['config', 'set', 'mgr', 'mgr/dashboard/ssl_server_port', str(ctx.ssl_dashboard_port)])

    # configuring dashboard parameters
    logger.info('Enabling the dashboard module...')
    cli(['mgr', 'module', 'enable', 'dashboard'])
    wait_for_mgr_restart()

    # dashboard crt and key
    if ctx.dashboard_key and ctx.dashboard_crt:
        logger.info('Using provided dashboard certificate...')
        mounts = {
            pathify(ctx.dashboard_crt.name): '/tmp/dashboard.crt:z',
            pathify(ctx.dashboard_key.name): '/tmp/dashboard.key:z'
        }
        cli(['dashboard', 'set-ssl-certificate', '-i', '/tmp/dashboard.crt'], extra_mounts=mounts)
        cli(['dashboard', 'set-ssl-certificate-key', '-i', '/tmp/dashboard.key'], extra_mounts=mounts)
    else:
        logger.info('Generating a dashboard self-signed certificate...')
        cli(['dashboard', 'create-self-signed-cert'])

    logger.info('Creating initial admin user...')
    password = ctx.initial_dashboard_password or generate_password()
    tmp_password_file = write_tmp(password, uid, gid)
    cmd = ['dashboard', 'ac-user-create', ctx.initial_dashboard_user, '-i', '/tmp/dashboard.pw', 'administrator', '--force-password']
    if not ctx.dashboard_password_noupdate:
        cmd.append('--pwd-update-required')
    cli(cmd, extra_mounts={pathify(tmp_password_file.name): '/tmp/dashboard.pw:z'})
    logger.info('Fetching dashboard port number...')
    out = cli(['config', 'get', 'mgr', 'mgr/dashboard/ssl_server_port'])
    port = int(out)

    # Open dashboard port
    fw = Firewalld(ctx)
    fw.open_ports([port])
    fw.apply_rules()

    logger.info('Ceph Dashboard is now available at:\n\n'
                '\t     URL: https://%s:%s/\n'
                '\t    User: %s\n'
                '\tPassword: %s\n' % (
                    get_fqdn(), port,
                    ctx.initial_dashboard_user,
                    password))


def prepare_bootstrap_config(
    ctx: CephadmContext,
    fsid: str, mon_addr: str, image: str

) -> str:

    cp = read_config(ctx.config)
    if not cp.has_section('global'):
        cp.add_section('global')
    cp.set('global', 'fsid', fsid)
    cp.set('global', 'mon_host', mon_addr)
    cp.set('global', 'container_image', image)

    if not cp.has_section('mon'):
        cp.add_section('mon')
    if (
            not cp.has_option('mon', 'auth_allow_insecure_global_id_reclaim')
            and not cp.has_option('mon', 'auth allow insecure global id reclaim')
    ):
        cp.set('mon', 'auth_allow_insecure_global_id_reclaim', 'false')

    if ctx.single_host_defaults:
        logger.info('Adjusting default settings to suit single-host cluster...')
        # replicate across osds, not hosts
        if (
                not cp.has_option('global', 'osd_crush_choose_leaf_type')
                and not cp.has_option('global', 'osd crush choose leaf type')
        ):
            cp.set('global', 'osd_crush_choose_leaf_type', '0')
        # replica 2x
        if (
                not cp.has_option('global', 'osd_pool_default_size')
                and not cp.has_option('global', 'osd pool default size')
        ):
            cp.set('global', 'osd_pool_default_size', '2')
        # disable mgr standby modules (so we can colocate multiple mgrs on one host)
        if not cp.has_section('mgr'):
            cp.add_section('mgr')
        if (
                not cp.has_option('mgr', 'mgr_standby_modules')
                and not cp.has_option('mgr', 'mgr standby modules')
        ):
            cp.set('mgr', 'mgr_standby_modules', 'false')

    cpf = StringIO()
    cp.write(cpf)
    config = cpf.getvalue()

    if ctx.registry_json or ctx.registry_url:
        command_registry_login(ctx)

    return config


def finish_bootstrap_config(
    ctx: CephadmContext,
    fsid: str,
    config: str,
    mon_id: str, mon_dir: str,
    mon_network: Optional[str], ipv6: bool,
    cli: Callable,
    cluster_network: Optional[str], ipv6_cluster_network: bool

) -> None:
    if not ctx.no_minimize_config:
        logger.info('Assimilating anything we can from ceph.conf...')
        cli([
            'config', 'assimilate-conf',
            '-i', '/var/lib/ceph/mon/ceph-%s/config' % mon_id
        ], {
            mon_dir: '/var/lib/ceph/mon/ceph-%s:z' % mon_id
        })
        logger.info('Generating new minimal ceph.conf...')
        cli([
            'config', 'generate-minimal-conf',
            '-o', '/var/lib/ceph/mon/ceph-%s/config' % mon_id
        ], {
            mon_dir: '/var/lib/ceph/mon/ceph-%s:z' % mon_id
        })
        # re-read our minimized config
        with open(mon_dir + '/config', 'r') as f:
            config = f.read()
        logger.info('Restarting the monitor...')
        call_throws(ctx, [
            'systemctl',
            'restart',
            get_unit_name(fsid, 'mon', mon_id)
        ])

    if mon_network:
        logger.info(f'Setting mon public_network to {mon_network}')
        cli(['config', 'set', 'mon', 'public_network', mon_network])

    if cluster_network:
        logger.info(f'Setting cluster_network to {cluster_network}')
        cli(['config', 'set', 'global', 'cluster_network', cluster_network])

    if ipv6 or ipv6_cluster_network:
        logger.info('Enabling IPv6 (ms_bind_ipv6) binding')
        cli(['config', 'set', 'global', 'ms_bind_ipv6', 'true'])

    with open(ctx.output_config, 'w') as f:
        f.write(config)
    logger.info('Wrote config to %s' % ctx.output_config)
    pass


@default_image
def command_bootstrap(ctx):
    # type: (CephadmContext) -> int

    if not ctx.output_config:
        ctx.output_config = os.path.join(ctx.output_dir, 'ceph.conf')
    if not ctx.output_keyring:
        ctx.output_keyring = os.path.join(ctx.output_dir,
                                          'ceph.client.admin.keyring')
    if not ctx.output_pub_ssh_key:
        ctx.output_pub_ssh_key = os.path.join(ctx.output_dir, 'ceph.pub')

    # verify output files
    for f in [ctx.output_config, ctx.output_keyring,
              ctx.output_pub_ssh_key]:
        if not ctx.allow_overwrite:
            if os.path.exists(f):
                raise Error('%s already exists; delete or pass '
                            '--allow-overwrite to overwrite' % f)
        dirname = os.path.dirname(f)
        if dirname and not os.path.exists(dirname):
            fname = os.path.basename(f)
            logger.info(f'Creating directory {dirname} for {fname}')
            try:
                # use makedirs to create intermediate missing dirs
                os.makedirs(dirname, 0o755)
            except PermissionError:
                raise Error(f'Unable to create {dirname} due to permissions failure. Retry with root, or sudo or preallocate the directory.')

    (user_conf, _) = get_config_and_keyring(ctx)

    if not ctx.skip_prepare_host:
        command_prepare_host(ctx)
    else:
        logger.info('Skip prepare_host')

    # initial vars
    fsid = ctx.fsid or make_fsid()
    hostname = get_hostname()
    if '.' in hostname and not ctx.allow_fqdn_hostname:
        raise Error('hostname is a fully qualified domain name (%s); either fix (e.g., "sudo hostname %s" or similar) or pass --allow-fqdn-hostname' % (hostname, hostname.split('.')[0]))
    mon_id = ctx.mon_id or hostname
    mgr_id = ctx.mgr_id or generate_service_id()
    logger.info('Cluster fsid: %s' % fsid)

    lock = FileLock(ctx, fsid)
    lock.acquire()

    (addr_arg, ipv6, mon_network) = prepare_mon_addresses(ctx)
    cluster_network, ipv6_cluster_network = prepare_cluster_network(ctx)

    config = prepare_bootstrap_config(ctx, fsid, addr_arg, ctx.image)

    if not ctx.skip_pull:
        _pull_image(ctx, ctx.image)

    image_ver = CephContainer(ctx, ctx.image, 'ceph', ['--version']).run().strip()
    logger.info(f'Ceph version: {image_ver}')

    if not ctx.allow_mismatched_release:
        image_release = image_ver.split()[4]
        if image_release not in \
                [DEFAULT_IMAGE_RELEASE, LATEST_STABLE_RELEASE]:
            raise Error(
                f'Container release {image_release} != cephadm release {DEFAULT_IMAGE_RELEASE};'
                ' please use matching version of cephadm (pass --allow-mismatched-release to continue anyway)'
            )

    logger.info('Extracting ceph user uid/gid from container image...')
    (uid, gid) = extract_uid_gid(ctx)

    # create some initial keys
    (mon_key, mgr_key, admin_key, bootstrap_keyring, admin_keyring) = \
        create_initial_keys(ctx, uid, gid, mgr_id)

    monmap = create_initial_monmap(ctx, uid, gid, fsid, mon_id, addr_arg)
    (mon_dir, log_dir) = \
        prepare_create_mon(ctx, uid, gid, fsid, mon_id,
                           bootstrap_keyring.name, monmap.name)

    with open(mon_dir + '/config', 'w') as f:
        os.fchown(f.fileno(), uid, gid)
        os.fchmod(f.fileno(), 0o600)
        f.write(config)

    make_var_run(ctx, fsid, uid, gid)
    create_mon(ctx, uid, gid, fsid, mon_id)

    # config to issue various CLI commands
    tmp_config = write_tmp(config, uid, gid)

    # a CLI helper to reduce our typing
    def cli(cmd, extra_mounts={}, timeout=DEFAULT_TIMEOUT):
        # type: (List[str], Dict[str, str], Optional[int]) -> str
        mounts = {
            log_dir: '/var/log/ceph:z',
            admin_keyring.name: '/etc/ceph/ceph.client.admin.keyring:z',
            tmp_config.name: '/etc/ceph/ceph.conf:z',
        }
        for k, v in extra_mounts.items():
            mounts[k] = v
        timeout = timeout or ctx.timeout
        return CephContainer(
            ctx,
            image=ctx.image,
            entrypoint='/usr/bin/ceph',
            args=cmd,
            volume_mounts=mounts,
        ).run(timeout=timeout)

    wait_for_mon(ctx, mon_id, mon_dir, admin_keyring.name, tmp_config.name)

    finish_bootstrap_config(ctx, fsid, config, mon_id, mon_dir,
                            mon_network, ipv6, cli,
                            cluster_network, ipv6_cluster_network)

    # output files
    with open(ctx.output_keyring, 'w') as f:
        os.fchmod(f.fileno(), 0o600)
        f.write('[client.admin]\n'
                '\tkey = ' + admin_key + '\n')
    logger.info('Wrote keyring to %s' % ctx.output_keyring)

    # create mgr
    create_mgr(ctx, uid, gid, fsid, mgr_id, mgr_key, config, cli)

    if user_conf:
        # user given config settings were already assimilated earlier
        # but if the given settings contained any attributes in
        # the mgr (e.g. mgr/cephadm/container_image_prometheus)
        # they don't seem to be stored if there isn't a mgr yet.
        # Since re-assimilating the same conf settings should be
        # idempotent we can just do it again here.
        with tempfile.NamedTemporaryFile(buffering=0) as tmp:
            tmp.write(user_conf.encode('utf-8'))
            cli(['config', 'assimilate-conf',
                 '-i', '/var/lib/ceph/user.conf'],
                {tmp.name: '/var/lib/ceph/user.conf:z'})

    # wait for mgr to restart (after enabling a module)
    def wait_for_mgr_restart():
        # first get latest mgrmap epoch from the mon.  try newer 'mgr
        # stat' command first, then fall back to 'mgr dump' if
        # necessary
        try:
            j = json_loads_retry(lambda: cli(['mgr', 'stat']))
        except Exception:
            j = json_loads_retry(lambda: cli(['mgr', 'dump']))
        epoch = j['epoch']

        # wait for mgr to have it
        logger.info('Waiting for the mgr to restart...')

        def mgr_has_latest_epoch():
            # type: () -> bool
            try:
                out = cli(['tell', 'mgr', 'mgr_status'])
                j = json.loads(out)
                return j['mgrmap_epoch'] >= epoch
            except Exception as e:
                logger.debug('tell mgr mgr_status failed: %s' % e)
                return False
        is_available(ctx, 'mgr epoch %d' % epoch, mgr_has_latest_epoch)

    enable_cephadm_mgr_module(cli, wait_for_mgr_restart)

    # ssh
    if not ctx.skip_ssh:
        prepare_ssh(ctx, cli, wait_for_mgr_restart)

    if ctx.registry_url and ctx.registry_username and ctx.registry_password:
        cli(['config', 'set', 'mgr', 'mgr/cephadm/registry_url', ctx.registry_url, '--force'])
        cli(['config', 'set', 'mgr', 'mgr/cephadm/registry_username', ctx.registry_username, '--force'])
        cli(['config', 'set', 'mgr', 'mgr/cephadm/registry_password', ctx.registry_password, '--force'])

    cli(['config', 'set', 'mgr', 'mgr/cephadm/container_init', str(ctx.container_init), '--force'])

    if ctx.with_exporter:
        cli(['config-key', 'set', 'mgr/cephadm/exporter_enabled', 'true'])
        if ctx.exporter_config:
            logger.info('Applying custom cephadm exporter settings')
            # validated within the parser, so we can just apply to the store
            with tempfile.NamedTemporaryFile(buffering=0) as tmp:
                tmp.write(json.dumps(ctx.exporter_config).encode('utf-8'))
                mounts = {
                    tmp.name: '/tmp/exporter-config.json:z'
                }
                cli(['cephadm', 'set-exporter-config', '-i', '/tmp/exporter-config.json'], extra_mounts=mounts)
            logger.info('-> Use ceph orch apply cephadm-exporter to deploy')
        else:
            # generate a default SSL configuration for the exporter(s)
            logger.info('Generating a default cephadm exporter configuration (self-signed)')
            cli(['cephadm', 'generate-exporter-config'])
        #
        # deploy the service (commented out until the cephadm changes are in the ceph container build)
        logger.info('Deploying cephadm exporter service with default placement...')
        cli(['orch', 'apply', 'cephadm-exporter'])

    if not ctx.skip_dashboard:
        prepare_dashboard(ctx, uid, gid, cli, wait_for_mgr_restart)

    if ctx.output_config == '/etc/ceph/ceph.conf' and not ctx.skip_admin_label:
        logger.info('Enabling client.admin keyring and conf on hosts with "admin" label')
        try:
            cli(['orch', 'client-keyring', 'set', 'client.admin', 'label:_admin'])
            cli(['orch', 'host', 'label', 'add', get_hostname(), '_admin'])
        except Exception:
            logger.info('Unable to set up "admin" label; assuming older version of Ceph')

    if ctx.apply_spec:
        logger.info('Applying %s to cluster' % ctx.apply_spec)

        with open(ctx.apply_spec) as f:
            for line in f:
                if 'hostname:' in line:
                    line = line.replace('\n', '')
                    split = line.split(': ')
                    if split[1] != hostname:
                        logger.info('Adding ssh key to %s' % split[1])

                        ssh_key = '/etc/ceph/ceph.pub'
                        if ctx.ssh_public_key:
                            ssh_key = ctx.ssh_public_key.name
                        out, err, code = call_throws(ctx, ['sudo', '-u', ctx.ssh_user, 'ssh-copy-id', '-f', '-i', ssh_key, '-o StrictHostKeyChecking=no', '%s@%s' % (ctx.ssh_user, split[1])])

        mounts = {}
        mounts[pathify(ctx.apply_spec)] = '/tmp/spec.yml:z'

        out = cli(['orch', 'apply', '-i', '/tmp/spec.yml'], extra_mounts=mounts)
        logger.info(out)

    logger.info('You can access the Ceph CLI with:\n\n'
                '\tsudo %s shell --fsid %s -c %s -k %s\n' % (
                    sys.argv[0],
                    fsid,
                    ctx.output_config,
                    ctx.output_keyring))
    logger.info('Please consider enabling telemetry to help improve Ceph:\n\n'
                '\tceph telemetry on\n\n'
                'For more information see:\n\n'
                '\thttps://docs.ceph.com/docs/master/mgr/telemetry/\n')
    logger.info('Bootstrap complete.')
    return 0

##################################


def command_registry_login(ctx: CephadmContext):
    if ctx.registry_json:
        logger.info('Pulling custom registry login info from %s.' % ctx.registry_json)
        d = get_parm(ctx.registry_json)
        if d.get('url') and d.get('username') and d.get('password'):
            ctx.registry_url = d.get('url')
            ctx.registry_username = d.get('username')
            ctx.registry_password = d.get('password')
            registry_login(ctx, ctx.registry_url, ctx.registry_username, ctx.registry_password)
        else:
            raise Error('json provided for custom registry login did not include all necessary fields. '
                        'Please setup json file as\n'
                        '{\n'
                        ' "url": "REGISTRY_URL",\n'
                        ' "username": "REGISTRY_USERNAME",\n'
                        ' "password": "REGISTRY_PASSWORD"\n'
                        '}\n')
    elif ctx.registry_url and ctx.registry_username and ctx.registry_password:
        registry_login(ctx, ctx.registry_url, ctx.registry_username, ctx.registry_password)
    else:
        raise Error('Invalid custom registry arguments received. To login to a custom registry include '
                    '--registry-url, --registry-username and --registry-password '
                    'options or --registry-json option')
    return 0


def registry_login(ctx: CephadmContext, url, username, password):
    logger.info('Logging into custom registry.')
    try:
        engine = ctx.container_engine
        cmd = [engine.path, 'login',
               '-u', username, '-p', password,
               url]
        if isinstance(engine, Podman):
            cmd.append('--authfile=/etc/ceph/podman-auth.json')
        out, _, _ = call_throws(ctx, cmd)
        if isinstance(engine, Podman):
            os.chmod('/etc/ceph/podman-auth.json', 0o600)
    except Exception:
        raise Error('Failed to login to custom registry @ %s as %s with given password' % (ctx.registry_url, ctx.registry_username))

##################################


def extract_uid_gid_monitoring(ctx, daemon_type):
    # type: (CephadmContext, str) -> Tuple[int, int]

    if daemon_type == 'prometheus':
        uid, gid = extract_uid_gid(ctx, file_path='/etc/prometheus')
    elif daemon_type == 'node-exporter':
        uid, gid = 65534, 65534
    elif daemon_type == 'grafana':
        uid, gid = extract_uid_gid(ctx, file_path='/var/lib/grafana')
    elif daemon_type == 'alertmanager':
        uid, gid = extract_uid_gid(ctx, file_path=['/etc/alertmanager', '/etc/prometheus'])
    else:
        raise Error('{} not implemented yet'.format(daemon_type))
    return uid, gid


@default_image
def command_deploy(ctx):
    # type: (CephadmContext) -> None
    daemon_type, daemon_id = ctx.name.split('.', 1)

    lock = FileLock(ctx, ctx.fsid)
    lock.acquire()

    if daemon_type not in get_supported_daemons():
        raise Error('daemon type %s not recognized' % daemon_type)

    redeploy = False
    unit_name = get_unit_name(ctx.fsid, daemon_type, daemon_id)
    container_name = 'ceph-%s-%s.%s' % (ctx.fsid, daemon_type, daemon_id)
    (_, state, _) = check_unit(ctx, unit_name)
    if state == 'running' or is_container_running(ctx, container_name):
        redeploy = True

    if ctx.reconfig:
        logger.info('%s daemon %s ...' % ('Reconfig', ctx.name))
    elif redeploy:
        logger.info('%s daemon %s ...' % ('Redeploy', ctx.name))
    else:
        logger.info('%s daemon %s ...' % ('Deploy', ctx.name))

    # Get and check ports explicitly required to be opened
    daemon_ports = []  # type: List[int]

    # only check port in use if not reconfig or redeploy since service
    # we are redeploying/reconfiguring will already be using the port
    if not ctx.reconfig and not redeploy:
        if ctx.tcp_ports:
            daemon_ports = list(map(int, ctx.tcp_ports.split()))

    if daemon_type in Ceph.daemons:
        config, keyring = get_config_and_keyring(ctx)
        uid, gid = extract_uid_gid(ctx)
        make_var_run(ctx, ctx.fsid, uid, gid)

        c = get_container(ctx, ctx.fsid, daemon_type, daemon_id,
                          ptrace=ctx.allow_ptrace)
        deploy_daemon(ctx, ctx.fsid, daemon_type, daemon_id, c, uid, gid,
                      config=config, keyring=keyring,
                      osd_fsid=ctx.osd_fsid,
                      reconfig=ctx.reconfig,
                      ports=daemon_ports)

    elif daemon_type in Monitoring.components:
        # monitoring daemon - prometheus, grafana, alertmanager, node-exporter
        # Default Checks
        # make sure provided config-json is sufficient
        config = get_parm(ctx.config_json)  # type: ignore
        required_files = Monitoring.components[daemon_type].get('config-json-files', list())
        required_args = Monitoring.components[daemon_type].get('config-json-args', list())
        if required_files:
            if not config or not all(c in config.get('files', {}).keys() for c in required_files):  # type: ignore
                raise Error('{} deployment requires config-json which must '
                            'contain file content for {}'.format(daemon_type.capitalize(), ', '.join(required_files)))
        if required_args:
            if not config or not all(c in config.keys() for c in required_args):  # type: ignore
                raise Error('{} deployment requires config-json which must '
                            'contain arg for {}'.format(daemon_type.capitalize(), ', '.join(required_args)))

        uid, gid = extract_uid_gid_monitoring(ctx, daemon_type)
        c = get_container(ctx, ctx.fsid, daemon_type, daemon_id)
        deploy_daemon(ctx, ctx.fsid, daemon_type, daemon_id, c, uid, gid,
                      reconfig=ctx.reconfig,
                      ports=daemon_ports)

    elif daemon_type == NFSGanesha.daemon_type:
        if not ctx.reconfig and not redeploy and not daemon_ports:
            daemon_ports = list(NFSGanesha.port_map.values())

        config, keyring = get_config_and_keyring(ctx)
        # TODO: extract ganesha uid/gid (997, 994) ?
        uid, gid = extract_uid_gid(ctx)
        c = get_container(ctx, ctx.fsid, daemon_type, daemon_id)
        deploy_daemon(ctx, ctx.fsid, daemon_type, daemon_id, c, uid, gid,
                      config=config, keyring=keyring,
                      reconfig=ctx.reconfig,
                      ports=daemon_ports)

    elif daemon_type == CephIscsi.daemon_type:
        config, keyring = get_config_and_keyring(ctx)
        uid, gid = extract_uid_gid(ctx)
        c = get_container(ctx, ctx.fsid, daemon_type, daemon_id)
        deploy_daemon(ctx, ctx.fsid, daemon_type, daemon_id, c, uid, gid,
                      config=config, keyring=keyring,
                      reconfig=ctx.reconfig,
                      ports=daemon_ports)

    elif daemon_type == HAproxy.daemon_type:
        haproxy = HAproxy.init(ctx, ctx.fsid, daemon_id)
        uid, gid = haproxy.extract_uid_gid_haproxy()
        c = get_container(ctx, ctx.fsid, daemon_type, daemon_id)
        deploy_daemon(ctx, ctx.fsid, daemon_type, daemon_id, c, uid, gid,
                      reconfig=ctx.reconfig,
                      ports=daemon_ports)

    elif daemon_type == Keepalived.daemon_type:
        keepalived = Keepalived.init(ctx, ctx.fsid, daemon_id)
        uid, gid = keepalived.extract_uid_gid_keepalived()
        c = get_container(ctx, ctx.fsid, daemon_type, daemon_id)
        deploy_daemon(ctx, ctx.fsid, daemon_type, daemon_id, c, uid, gid,
                      reconfig=ctx.reconfig,
                      ports=daemon_ports)

    elif daemon_type == CustomContainer.daemon_type:
        cc = CustomContainer.init(ctx, ctx.fsid, daemon_id)
        if not ctx.reconfig and not redeploy:
            daemon_ports.extend(cc.ports)
        c = get_container(ctx, ctx.fsid, daemon_type, daemon_id,
                          privileged=cc.privileged,
                          ptrace=ctx.allow_ptrace)
        deploy_daemon(ctx, ctx.fsid, daemon_type, daemon_id, c,
                      uid=cc.uid, gid=cc.gid, config=None,
                      keyring=None, reconfig=ctx.reconfig,
                      ports=daemon_ports)

    elif daemon_type == CephadmDaemon.daemon_type:
        # get current user gid and uid
        uid = os.getuid()
        gid = os.getgid()
        config_js = get_parm(ctx.config_json)  # type: Dict[str, str]
        if not daemon_ports:
            logger.info('cephadm-exporter will use default port ({})'.format(CephadmDaemon.default_port))
            daemon_ports = [CephadmDaemon.default_port]

        CephadmDaemon.validate_config(config_js)

        deploy_daemon(ctx, ctx.fsid, daemon_type, daemon_id, None,
                      uid, gid, ports=daemon_ports)

    else:
        raise Error('daemon type {} not implemented in command_deploy function'
                    .format(daemon_type))

##################################


@infer_image
def command_run(ctx):
    # type: (CephadmContext) -> int
    (daemon_type, daemon_id) = ctx.name.split('.', 1)
    c = get_container(ctx, ctx.fsid, daemon_type, daemon_id)
    command = c.run_cmd()
    return call_timeout(ctx, command, ctx.timeout)

##################################


def fsid_conf_mismatch(ctx):
    # type: (CephadmContext) -> bool
    (config, _) = get_config_and_keyring(ctx)
    if config:
        for c in config.split('\n'):
            if 'fsid = ' in c.strip():
                if 'fsid = ' + ctx.fsid != c.strip():
                    return True
    return False


@infer_fsid
@infer_config
@infer_image
def command_shell(ctx):
    # type: (CephadmContext) -> int
    if fsid_conf_mismatch(ctx):
        raise Error('fsid does not match ceph conf')

    if ctx.fsid:
        make_log_dir(ctx, ctx.fsid)
    if ctx.name:
        if '.' in ctx.name:
            (daemon_type, daemon_id) = ctx.name.split('.', 1)
        else:
            daemon_type = ctx.name
            daemon_id = None
    else:
        daemon_type = 'osd'  # get the most mounts
        daemon_id = None

    if daemon_id and not ctx.fsid:
        raise Error('must pass --fsid to specify cluster')

    # use /etc/ceph files by default, if present.  we do this instead of
    # making these defaults in the arg parser because we don't want an error
    # if they don't exist.
    if not ctx.keyring and os.path.exists(SHELL_DEFAULT_KEYRING):
        ctx.keyring = SHELL_DEFAULT_KEYRING

    container_args: List[str] = ['-i']
    mounts = get_container_mounts(ctx, ctx.fsid, daemon_type, daemon_id,
                                  no_config=True if ctx.config else False)
    binds = get_container_binds(ctx, ctx.fsid, daemon_type, daemon_id)
    if ctx.config:
        mounts[pathify(ctx.config)] = '/etc/ceph/ceph.conf:z'
    if ctx.keyring:
        mounts[pathify(ctx.keyring)] = '/etc/ceph/ceph.keyring:z'
    if ctx.mount:
        for _mount in ctx.mount:
            split_src_dst = _mount.split(':')
            mount = pathify(split_src_dst[0])
            filename = os.path.basename(split_src_dst[0])
            if len(split_src_dst) > 1:
                dst = split_src_dst[1] + ':z' if len(split_src_dst) == 3 else split_src_dst[1]
                mounts[mount] = dst
            else:
                mounts[mount] = '/mnt/{}:z'.format(filename)
    if ctx.command:
        command = ctx.command
    else:
        command = ['bash']
        container_args += [
            '-t',
            '-e', 'LANG=C',
            '-e', 'PS1=%s' % CUSTOM_PS1,
        ]
        if ctx.fsid:
            home = os.path.join(ctx.data_dir, ctx.fsid, 'home')
            if not os.path.exists(home):
                logger.debug('Creating root home at %s' % home)
                makedirs(home, 0, 0, 0o660)
                if os.path.exists('/etc/skel'):
                    for f in os.listdir('/etc/skel'):
                        if f.startswith('.bash'):
                            shutil.copyfile(os.path.join('/etc/skel', f),
                                            os.path.join(home, f))
            mounts[home] = '/root'

    for i in ctx.volume:
        a, b = i.split(':', 1)
        mounts[a] = b

    c = CephContainer(
        ctx,
        image=ctx.image,
        entrypoint='doesnotmatter',
        args=[],
        container_args=container_args,
        volume_mounts=mounts,
        bind_mounts=binds,
        envs=ctx.env,
        privileged=True)
    command = c.shell_cmd(command)

    return call_timeout(ctx, command, ctx.timeout)

##################################


@infer_fsid
def command_enter(ctx):
    # type: (CephadmContext) -> int
    if not ctx.fsid:
        raise Error('must pass --fsid to specify cluster')
    (daemon_type, daemon_id) = ctx.name.split('.', 1)
    container_args = ['-i']  # type: List[str]
    if ctx.command:
        command = ctx.command
    else:
        command = ['sh']
        container_args += [
            '-t',
            '-e', 'LANG=C',
            '-e', 'PS1=%s' % CUSTOM_PS1,
        ]
    c = CephContainer(
        ctx,
        image=ctx.image,
        entrypoint='doesnotmatter',
        container_args=container_args,
        cname='ceph-%s-%s.%s' % (ctx.fsid, daemon_type, daemon_id),
    )
    command = c.exec_cmd(command)
    return call_timeout(ctx, command, ctx.timeout)

##################################


@infer_fsid
@infer_image
def command_ceph_volume(ctx):
    # type: (CephadmContext) -> None
    if ctx.fsid:
        make_log_dir(ctx, ctx.fsid)

        lock = FileLock(ctx, ctx.fsid)
        lock.acquire()

    (uid, gid) = (0, 0)  # ceph-volume runs as root
    mounts = get_container_mounts(ctx, ctx.fsid, 'osd', None)

    tmp_config = None
    tmp_keyring = None

    (config, keyring) = get_config_and_keyring(ctx)

    if config:
        # tmp config file
        tmp_config = write_tmp(config, uid, gid)
        mounts[tmp_config.name] = '/etc/ceph/ceph.conf:z'

    if keyring:
        # tmp keyring file
        tmp_keyring = write_tmp(keyring, uid, gid)
        mounts[tmp_keyring.name] = '/var/lib/ceph/bootstrap-osd/ceph.keyring:z'

    c = CephContainer(
        ctx,
        image=ctx.image,
        entrypoint='/usr/sbin/ceph-volume',
        envs=ctx.env,
        args=ctx.command,
        privileged=True,
        volume_mounts=mounts,
    )

    out, err, code = call_throws(ctx, c.run_cmd())
    if not code:
        print(out)

##################################


@infer_fsid
def command_unit(ctx):
    # type: (CephadmContext) -> None
    if not ctx.fsid:
        raise Error('must pass --fsid to specify cluster')

    unit_name = get_unit_name_by_daemon_name(ctx, ctx.fsid, ctx.name)

    call_throws(ctx, [
        'systemctl',
        ctx.command,
        unit_name],
        verbosity=CallVerbosity.VERBOSE,
        desc=''
    )

##################################


@infer_fsid
def command_logs(ctx):
    # type: (CephadmContext) -> None
    if not ctx.fsid:
        raise Error('must pass --fsid to specify cluster')

    unit_name = get_unit_name_by_daemon_name(ctx, ctx.fsid, ctx.name)

    cmd = [find_program('journalctl')]
    cmd.extend(['-u', unit_name])
    if ctx.command:
        cmd.extend(ctx.command)

    # call this directly, without our wrapper, so that we get an unmolested
    # stdout with logger prefixing.
    logger.debug('Running command: %s' % ' '.join(cmd))
    subprocess.call(cmd)  # type: ignore

##################################


def list_networks(ctx):
    # type: (CephadmContext) -> Dict[str,Dict[str,List[str]]]

    # sadly, 18.04's iproute2 4.15.0-2ubun doesn't support the -j flag,
    # so we'll need to use a regex to parse 'ip' command output.
    #
    # out, _, _ = call_throws(['ip', '-j', 'route', 'ls'])
    # j = json.loads(out)
    # for x in j:

    res = _list_ipv4_networks(ctx)
    res.update(_list_ipv6_networks(ctx))
    return res


def _list_ipv4_networks(ctx: CephadmContext):
    execstr: Optional[str] = find_executable('ip')
    if not execstr:
        raise FileNotFoundError("unable to find 'ip' command")
    out, _, _ = call_throws(ctx, [execstr, 'route', 'ls'])
    return _parse_ipv4_route(out)


def _parse_ipv4_route(out):
    r = {}  # type: Dict[str,Dict[str,List[str]]]
    p = re.compile(r'^(\S+) dev (\S+) (.*)scope link (.*)src (\S+)')
    for line in out.splitlines():
        m = p.findall(line)
        if not m:
            continue
        net = m[0][0]
        iface = m[0][1]
        ip = m[0][4]
        if net not in r:
            r[net] = {}
        if iface not in r[net]:
            r[net][iface] = []
        r[net][iface].append(ip)
    return r


def _list_ipv6_networks(ctx: CephadmContext):
    execstr: Optional[str] = find_executable('ip')
    if not execstr:
        raise FileNotFoundError("unable to find 'ip' command")
    routes, _, _ = call_throws(ctx, [execstr, '-6', 'route', 'ls'])
    ips, _, _ = call_throws(ctx, [execstr, '-6', 'addr', 'ls'])
    return _parse_ipv6_route(routes, ips)


def _parse_ipv6_route(routes, ips):
    r = {}  # type: Dict[str,Dict[str,List[str]]]
    route_p = re.compile(r'^(\S+) dev (\S+) proto (\S+) metric (\S+) .*pref (\S+)$')
    ip_p = re.compile(r'^\s+inet6 (\S+)/(.*)scope (.*)$')
    iface_p = re.compile(r'^(\d+): (\S+): (.*)$')
    for line in routes.splitlines():
        m = route_p.findall(line)
        if not m or m[0][0].lower() == 'default':
            continue
        net = m[0][0]
        if '/' not in net:  # only consider networks with a mask
            continue
        iface = m[0][1]
        if net not in r:
            r[net] = {}
        if iface not in r[net]:
            r[net][iface] = []

    iface = None
    for line in ips.splitlines():
        m = ip_p.findall(line)
        if not m:
            m = iface_p.findall(line)
            if m:
                # drop @... suffix, if present
                iface = m[0][1].split('@')[0]
            continue
        ip = m[0][0]
        # find the network it belongs to
        net = [n for n in r.keys()
               if ipaddress.ip_address(ip) in ipaddress.ip_network(n)]
        if net:
            assert(iface)
            r[net[0]][iface].append(ip)

    return r


def command_list_networks(ctx):
    # type: (CephadmContext) -> None
    r = list_networks(ctx)
    print(json.dumps(r, indent=4))

##################################


def command_ls(ctx):
    # type: (CephadmContext) -> None
    ls = list_daemons(ctx, detail=not ctx.no_detail,
                      legacy_dir=ctx.legacy_dir)
    print(json.dumps(ls, indent=4))


def with_units_to_int(v: str) -> int:
    if v.endswith('iB'):
        v = v[:-2]
    elif v.endswith('B'):
        v = v[:-1]
    mult = 1
    if v[-1].upper() == 'K':
        mult = 1024
        v = v[:-1]
    elif v[-1].upper() == 'M':
        mult = 1024 * 1024
        v = v[:-1]
    elif v[-1].upper() == 'G':
        mult = 1024 * 1024 * 1024
        v = v[:-1]
    elif v[-1].upper() == 'T':
        mult = 1024 * 1024 * 1024 * 1024
        v = v[:-1]
    return int(float(v) * mult)


def list_daemons(ctx, detail=True, legacy_dir=None):
    # type: (CephadmContext, bool, Optional[str]) -> List[Dict[str, str]]
    host_version: Optional[str] = None
    ls = []
    container_path = ctx.container_engine.path

    data_dir = ctx.data_dir
    if legacy_dir is not None:
        data_dir = os.path.abspath(legacy_dir + data_dir)

    # keep track of ceph versions we see
    seen_versions = {}  # type: Dict[str, Optional[str]]

    # keep track of image digests
    seen_digests = {}   # type: Dict[str, List[str]]

    # keep track of memory usage we've seen
    seen_memusage = {}  # type: Dict[str, int]
    out, err, code = call(
        ctx,
        [container_path, 'stats', '--format', '{{.ID}},{{.MemUsage}}', '--no-stream'],
        verbosity=CallVerbosity.DEBUG
    )
    seen_memusage_cid_len = 0
    if not code:
        for line in out.splitlines():
            (cid, usage) = line.split(',')
            (used, limit) = usage.split(' / ')
            seen_memusage[cid] = with_units_to_int(used)
            if not seen_memusage_cid_len:
                seen_memusage_cid_len = len(cid)

    # /var/lib/ceph
    if os.path.exists(data_dir):
        for i in os.listdir(data_dir):
            if i in ['mon', 'osd', 'mds', 'mgr']:
                daemon_type = i
                for j in os.listdir(os.path.join(data_dir, i)):
                    if '-' not in j:
                        continue
                    (cluster, daemon_id) = j.split('-', 1)
                    fsid = get_legacy_daemon_fsid(ctx,
                                                  cluster, daemon_type, daemon_id,
                                                  legacy_dir=legacy_dir)
                    legacy_unit_name = 'ceph-%s@%s' % (daemon_type, daemon_id)
                    val: Dict[str, Any] = {
                        'style': 'legacy',
                        'name': '%s.%s' % (daemon_type, daemon_id),
                        'fsid': fsid if fsid is not None else 'unknown',
                        'systemd_unit': legacy_unit_name,
                    }
                    if detail:
                        (val['enabled'], val['state'], _) = \
                            check_unit(ctx, legacy_unit_name)
                        if not host_version:
                            try:
                                out, err, code = call(ctx,
                                                      ['ceph', '-v'],
                                                      verbosity=CallVerbosity.DEBUG)
                                if not code and out.startswith('ceph version '):
                                    host_version = out.split(' ')[2]
                            except Exception:
                                pass
                        val['host_version'] = host_version
                    ls.append(val)
            elif is_fsid(i):
                fsid = str(i)  # convince mypy that fsid is a str here
                for j in os.listdir(os.path.join(data_dir, i)):
                    if '.' in j and os.path.isdir(os.path.join(data_dir, fsid, j)):
                        name = j
                        (daemon_type, daemon_id) = j.split('.', 1)
                        unit_name = get_unit_name(fsid,
                                                  daemon_type,
                                                  daemon_id)
                    else:
                        continue
                    val = {
                        'style': 'cephadm:v1',
                        'name': name,
                        'fsid': fsid,
                        'systemd_unit': unit_name,
                    }
                    if detail:
                        # get container id
                        (val['enabled'], val['state'], _) = \
                            check_unit(ctx, unit_name)
                        container_id = None
                        image_name = None
                        image_id = None
                        image_digests = None
                        version = None
                        start_stamp = None

                        cmd = [
                            container_path, 'inspect',
                            '--format', '{{.Id}},{{.Config.Image}},{{.Image}},{{.Created}},{{index .Config.Labels "io.ceph.version"}}',
                            'ceph-%s-%s' % (fsid, j)
                        ]
                        out, err, code = call(ctx, cmd, verbosity=CallVerbosity.DEBUG)
                        if not code:
                            (container_id, image_name, image_id, start,
                             version) = out.strip().split(',')
                            image_id = normalize_container_id(image_id)
                            daemon_type = name.split('.', 1)[0]
                            start_stamp = try_convert_datetime(start)

                            # collect digests for this image id
                            image_digests = seen_digests.get(image_id)
                            if not image_digests:
                                out, err, code = call(
                                    ctx,
                                    [
                                        container_path, 'image', 'inspect', image_id,
                                        '--format', '{{.RepoDigests}}',
                                    ],
                                    verbosity=CallVerbosity.DEBUG)
                                if not code:
                                    image_digests = list(set(map(
                                        normalize_image_digest,
                                        out.strip()[1:-1].split(' '))))
                                    seen_digests[image_id] = image_digests

                            # identify software version inside the container (if we can)
                            if not version or '.' not in version:
                                version = seen_versions.get(image_id, None)
                            if daemon_type == NFSGanesha.daemon_type:
                                version = NFSGanesha.get_version(ctx, container_id)
                            if daemon_type == CephIscsi.daemon_type:
                                version = CephIscsi.get_version(ctx, container_id)
                            elif not version:
                                if daemon_type in Ceph.daemons:
                                    out, err, code = call(ctx,
                                                          [container_path, 'exec', container_id,
                                                           'ceph', '-v'],
                                                          verbosity=CallVerbosity.DEBUG)
                                    if not code and \
                                       out.startswith('ceph version '):
                                        version = out.split(' ')[2]
                                        seen_versions[image_id] = version
                                elif daemon_type == 'grafana':
                                    out, err, code = call(ctx,
                                                          [container_path, 'exec', container_id,
                                                           'grafana-server', '-v'],
                                                          verbosity=CallVerbosity.DEBUG)
                                    if not code and \
                                       out.startswith('Version '):
                                        version = out.split(' ')[1]
                                        seen_versions[image_id] = version
                                elif daemon_type in ['prometheus',
                                                     'alertmanager',
                                                     'node-exporter']:
                                    version = Monitoring.get_version(ctx, container_id, daemon_type)
                                    seen_versions[image_id] = version
                                elif daemon_type == 'haproxy':
                                    out, err, code = call(ctx,
                                                          [container_path, 'exec', container_id,
                                                           'haproxy', '-v'],
                                                          verbosity=CallVerbosity.DEBUG)
                                    if not code and \
                                       out.startswith('HA-Proxy version '):
                                        version = out.split(' ')[2]
                                        seen_versions[image_id] = version
                                elif daemon_type == 'keepalived':
                                    out, err, code = call(ctx,
                                                          [container_path, 'exec', container_id,
                                                           'keepalived', '--version'],
                                                          verbosity=CallVerbosity.DEBUG)
                                    if not code and \
                                       err.startswith('Keepalived '):
                                        version = err.split(' ')[1]
                                        if version[0] == 'v':
                                            version = version[1:]
                                        seen_versions[image_id] = version
                                elif daemon_type == CustomContainer.daemon_type:
                                    # Because a custom container can contain
                                    # everything, we do not know which command
                                    # to execute to get the version.
                                    pass
                                else:
                                    logger.warning('version for unknown daemon type %s' % daemon_type)
                        else:
                            vfile = os.path.join(data_dir, fsid, j, 'unit.image')  # type: ignore
                            try:
                                with open(vfile, 'r') as f:
                                    image_name = f.read().strip() or None
                            except IOError:
                                pass

                        # unit.meta?
                        mfile = os.path.join(data_dir, fsid, j, 'unit.meta')  # type: ignore
                        try:
                            with open(mfile, 'r') as f:
                                meta = json.loads(f.read())
                                val.update(meta)
                        except IOError:
                            pass

                        val['container_id'] = container_id
                        val['container_image_name'] = image_name
                        val['container_image_id'] = image_id
                        val['container_image_digests'] = image_digests
                        if container_id:
                            val['memory_usage'] = seen_memusage.get(container_id[0:seen_memusage_cid_len])
                        val['version'] = version
                        val['started'] = start_stamp
                        val['created'] = get_file_timestamp(
                            os.path.join(data_dir, fsid, j, 'unit.created')
                        )
                        val['deployed'] = get_file_timestamp(
                            os.path.join(data_dir, fsid, j, 'unit.image'))
                        val['configured'] = get_file_timestamp(
                            os.path.join(data_dir, fsid, j, 'unit.configured'))

                    ls.append(val)

    return ls


def get_daemon_description(ctx, fsid, name, detail=False, legacy_dir=None):
    # type: (CephadmContext, str, str, bool, Optional[str]) -> Dict[str, str]

    for d in list_daemons(ctx, detail=detail, legacy_dir=legacy_dir):
        if d['fsid'] != fsid:
            continue
        if d['name'] != name:
            continue
        return d
    raise Error('Daemon not found: {}. See `cephadm ls`'.format(name))

##################################


@default_image
def command_adopt(ctx):
    # type: (CephadmContext) -> None

    if not ctx.skip_pull:
        _pull_image(ctx, ctx.image)

    (daemon_type, daemon_id) = ctx.name.split('.', 1)

    # legacy check
    if ctx.style != 'legacy':
        raise Error('adoption of style %s not implemented' % ctx.style)

    # lock
    fsid = get_legacy_daemon_fsid(ctx,
                                  ctx.cluster,
                                  daemon_type,
                                  daemon_id,
                                  legacy_dir=ctx.legacy_dir)
    if not fsid:
        raise Error('could not detect legacy fsid; set fsid in ceph.conf')
    lock = FileLock(ctx, fsid)
    lock.acquire()

    # call correct adoption
    if daemon_type in Ceph.daemons:
        command_adopt_ceph(ctx, daemon_type, daemon_id, fsid)
    elif daemon_type == 'prometheus':
        command_adopt_prometheus(ctx, daemon_id, fsid)
    elif daemon_type == 'grafana':
        command_adopt_grafana(ctx, daemon_id, fsid)
    elif daemon_type == 'node-exporter':
        raise Error('adoption of node-exporter not implemented')
    elif daemon_type == 'alertmanager':
        command_adopt_alertmanager(ctx, daemon_id, fsid)
    else:
        raise Error('daemon type %s not recognized' % daemon_type)


class AdoptOsd(object):
    def __init__(self, ctx, osd_data_dir, osd_id):
        # type: (CephadmContext, str, str) -> None
        self.ctx = ctx
        self.osd_data_dir = osd_data_dir
        self.osd_id = osd_id

    def check_online_osd(self):
        # type: () -> Tuple[Optional[str], Optional[str]]

        osd_fsid, osd_type = None, None

        path = os.path.join(self.osd_data_dir, 'fsid')
        try:
            with open(path, 'r') as f:
                osd_fsid = f.read().strip()
            logger.info('Found online OSD at %s' % path)
        except IOError:
            logger.info('Unable to read OSD fsid from %s' % path)
        if os.path.exists(os.path.join(self.osd_data_dir, 'type')):
            with open(os.path.join(self.osd_data_dir, 'type')) as f:
                osd_type = f.read().strip()
        else:
            logger.info('"type" file missing for OSD data dir')

        return osd_fsid, osd_type

    def check_offline_lvm_osd(self):
        # type: () -> Tuple[Optional[str], Optional[str]]
        osd_fsid, osd_type = None, None

        c = CephContainer(
            self.ctx,
            image=self.ctx.image,
            entrypoint='/usr/sbin/ceph-volume',
            args=['lvm', 'list', '--format=json'],
            privileged=True
        )
        out, err, code = call_throws(self.ctx, c.run_cmd())
        if not code:
            try:
                js = json.loads(out)
                if self.osd_id in js:
                    logger.info('Found offline LVM OSD {}'.format(self.osd_id))
                    osd_fsid = js[self.osd_id][0]['tags']['ceph.osd_fsid']
                    for device in js[self.osd_id]:
                        if device['tags']['ceph.type'] == 'block':
                            osd_type = 'bluestore'
                            break
                        if device['tags']['ceph.type'] == 'data':
                            osd_type = 'filestore'
                            break
            except ValueError as e:
                logger.info('Invalid JSON in ceph-volume lvm list: {}'.format(e))

        return osd_fsid, osd_type

    def check_offline_simple_osd(self):
        # type: () -> Tuple[Optional[str], Optional[str]]
        osd_fsid, osd_type = None, None

        osd_file = glob('/etc/ceph/osd/{}-[a-f0-9-]*.json'.format(self.osd_id))
        if len(osd_file) == 1:
            with open(osd_file[0], 'r') as f:
                try:
                    js = json.loads(f.read())
                    logger.info('Found offline simple OSD {}'.format(self.osd_id))
                    osd_fsid = js['fsid']
                    osd_type = js['type']
                    if osd_type != 'filestore':
                        # need this to be mounted for the adopt to work, as it
                        # needs to move files from this directory
                        call_throws(self.ctx, ['mount', js['data']['path'], self.osd_data_dir])
                except ValueError as e:
                    logger.info('Invalid JSON in {}: {}'.format(osd_file, e))

        return osd_fsid, osd_type


def command_adopt_ceph(ctx, daemon_type, daemon_id, fsid):
    # type: (CephadmContext, str, str, str) -> None

    (uid, gid) = extract_uid_gid(ctx)

    data_dir_src = ('/var/lib/ceph/%s/%s-%s' %
                    (daemon_type, ctx.cluster, daemon_id))
    data_dir_src = os.path.abspath(ctx.legacy_dir + data_dir_src)

    if not os.path.exists(data_dir_src):
        raise Error("{}.{} data directory '{}' does not exist.  "
                    'Incorrect ID specified, or daemon already adopted?'.format(
                        daemon_type, daemon_id, data_dir_src))

    osd_fsid = None
    if daemon_type == 'osd':
        adopt_osd = AdoptOsd(ctx, data_dir_src, daemon_id)
        osd_fsid, osd_type = adopt_osd.check_online_osd()
        if not osd_fsid:
            osd_fsid, osd_type = adopt_osd.check_offline_lvm_osd()
        if not osd_fsid:
            osd_fsid, osd_type = adopt_osd.check_offline_simple_osd()
        if not osd_fsid:
            raise Error('Unable to find OSD {}'.format(daemon_id))
        logger.info('objectstore_type is %s' % osd_type)
        assert osd_type
        if osd_type == 'filestore':
            raise Error('FileStore is not supported by cephadm')

    # NOTE: implicit assumption here that the units correspond to the
    # cluster we are adopting based on the /etc/{defaults,sysconfig}/ceph
    # CLUSTER field.
    unit_name = 'ceph-%s@%s' % (daemon_type, daemon_id)
    (enabled, state, _) = check_unit(ctx, unit_name)
    if state == 'running':
        logger.info('Stopping old systemd unit %s...' % unit_name)
        call_throws(ctx, ['systemctl', 'stop', unit_name])
    if enabled:
        logger.info('Disabling old systemd unit %s...' % unit_name)
        call_throws(ctx, ['systemctl', 'disable', unit_name])

    # data
    logger.info('Moving data...')
    data_dir_dst = make_data_dir(ctx, fsid, daemon_type, daemon_id,
                                 uid=uid, gid=gid)
    move_files(ctx, glob(os.path.join(data_dir_src, '*')),
               data_dir_dst,
               uid=uid, gid=gid)
    logger.debug('Remove dir `%s`' % (data_dir_src))
    if os.path.ismount(data_dir_src):
        call_throws(ctx, ['umount', data_dir_src])
    os.rmdir(data_dir_src)

    logger.info('Chowning content...')
    call_throws(ctx, ['chown', '-c', '-R', '%d.%d' % (uid, gid), data_dir_dst])

    if daemon_type == 'mon':
        # rename *.ldb -> *.sst, in case they are coming from ubuntu
        store = os.path.join(data_dir_dst, 'store.db')
        num_renamed = 0
        if os.path.exists(store):
            for oldf in os.listdir(store):
                if oldf.endswith('.ldb'):
                    newf = oldf.replace('.ldb', '.sst')
                    oldp = os.path.join(store, oldf)
                    newp = os.path.join(store, newf)
                    logger.debug('Renaming %s -> %s' % (oldp, newp))
                    os.rename(oldp, newp)
        if num_renamed:
            logger.info('Renamed %d leveldb *.ldb files to *.sst',
                        num_renamed)
    if daemon_type == 'osd':
        for n in ['block', 'block.db', 'block.wal']:
            p = os.path.join(data_dir_dst, n)
            if os.path.exists(p):
                logger.info('Chowning %s...' % p)
                os.chown(p, uid, gid)
        # disable the ceph-volume 'simple' mode files on the host
        simple_fn = os.path.join('/etc/ceph/osd',
                                 '%s-%s.json' % (daemon_id, osd_fsid))
        if os.path.exists(simple_fn):
            new_fn = simple_fn + '.adopted-by-cephadm'
            logger.info('Renaming %s -> %s', simple_fn, new_fn)
            os.rename(simple_fn, new_fn)
            logger.info('Disabling host unit ceph-volume@ simple unit...')
            call(ctx, ['systemctl', 'disable',
                       'ceph-volume@simple-%s-%s.service' % (daemon_id, osd_fsid)])
        else:
            # assume this is an 'lvm' c-v for now, but don't error
            # out if it's not.
            logger.info('Disabling host unit ceph-volume@ lvm unit...')
            call(ctx, ['systemctl', 'disable',
                       'ceph-volume@lvm-%s-%s.service' % (daemon_id, osd_fsid)])

    # config
    config_src = '/etc/ceph/%s.conf' % (ctx.cluster)
    config_src = os.path.abspath(ctx.legacy_dir + config_src)
    config_dst = os.path.join(data_dir_dst, 'config')
    copy_files(ctx, [config_src], config_dst, uid=uid, gid=gid)

    # logs
    logger.info('Moving logs...')
    log_dir_src = ('/var/log/ceph/%s-%s.%s.log*' %
                   (ctx.cluster, daemon_type, daemon_id))
    log_dir_src = os.path.abspath(ctx.legacy_dir + log_dir_src)
    log_dir_dst = make_log_dir(ctx, fsid, uid=uid, gid=gid)
    move_files(ctx, glob(log_dir_src),
               log_dir_dst,
               uid=uid, gid=gid)

    logger.info('Creating new units...')
    make_var_run(ctx, fsid, uid, gid)
    c = get_container(ctx, fsid, daemon_type, daemon_id)
    deploy_daemon_units(ctx, fsid, uid, gid, daemon_type, daemon_id, c,
                        enable=True,  # unconditionally enable the new unit
                        start=(state == 'running' or ctx.force_start),
                        osd_fsid=osd_fsid)
    update_firewalld(ctx, daemon_type)


def command_adopt_prometheus(ctx, daemon_id, fsid):
    # type: (CephadmContext, str, str) -> None
    daemon_type = 'prometheus'
    (uid, gid) = extract_uid_gid_monitoring(ctx, daemon_type)

    _stop_and_disable(ctx, 'prometheus')

    data_dir_dst = make_data_dir(ctx, fsid, daemon_type, daemon_id,
                                 uid=uid, gid=gid)

    # config
    config_src = '/etc/prometheus/prometheus.yml'
    config_src = os.path.abspath(ctx.legacy_dir + config_src)
    config_dst = os.path.join(data_dir_dst, 'etc/prometheus')
    makedirs(config_dst, uid, gid, 0o755)
    copy_files(ctx, [config_src], config_dst, uid=uid, gid=gid)

    # data
    data_src = '/var/lib/prometheus/metrics/'
    data_src = os.path.abspath(ctx.legacy_dir + data_src)
    data_dst = os.path.join(data_dir_dst, 'data')
    copy_tree(ctx, [data_src], data_dst, uid=uid, gid=gid)

    make_var_run(ctx, fsid, uid, gid)
    c = get_container(ctx, fsid, daemon_type, daemon_id)
    deploy_daemon(ctx, fsid, daemon_type, daemon_id, c, uid, gid)
    update_firewalld(ctx, daemon_type)


def command_adopt_grafana(ctx, daemon_id, fsid):
    # type: (CephadmContext, str, str) -> None

    daemon_type = 'grafana'
    (uid, gid) = extract_uid_gid_monitoring(ctx, daemon_type)

    _stop_and_disable(ctx, 'grafana-server')

    data_dir_dst = make_data_dir(ctx, fsid, daemon_type, daemon_id,
                                 uid=uid, gid=gid)

    # config
    config_src = '/etc/grafana/grafana.ini'
    config_src = os.path.abspath(ctx.legacy_dir + config_src)
    config_dst = os.path.join(data_dir_dst, 'etc/grafana')
    makedirs(config_dst, uid, gid, 0o755)
    copy_files(ctx, [config_src], config_dst, uid=uid, gid=gid)

    prov_src = '/etc/grafana/provisioning/'
    prov_src = os.path.abspath(ctx.legacy_dir + prov_src)
    prov_dst = os.path.join(data_dir_dst, 'etc/grafana')
    copy_tree(ctx, [prov_src], prov_dst, uid=uid, gid=gid)

    # cert
    cert = '/etc/grafana/grafana.crt'
    key = '/etc/grafana/grafana.key'
    if os.path.exists(cert) and os.path.exists(key):
        cert_src = '/etc/grafana/grafana.crt'
        cert_src = os.path.abspath(ctx.legacy_dir + cert_src)
        makedirs(os.path.join(data_dir_dst, 'etc/grafana/certs'), uid, gid, 0o755)
        cert_dst = os.path.join(data_dir_dst, 'etc/grafana/certs/cert_file')
        copy_files(ctx, [cert_src], cert_dst, uid=uid, gid=gid)

        key_src = '/etc/grafana/grafana.key'
        key_src = os.path.abspath(ctx.legacy_dir + key_src)
        key_dst = os.path.join(data_dir_dst, 'etc/grafana/certs/cert_key')
        copy_files(ctx, [key_src], key_dst, uid=uid, gid=gid)

        _adjust_grafana_ini(os.path.join(config_dst, 'grafana.ini'))
    else:
        logger.debug('Skipping ssl, missing cert {} or key {}'.format(cert, key))

    # data - possible custom dashboards/plugins
    data_src = '/var/lib/grafana/'
    data_src = os.path.abspath(ctx.legacy_dir + data_src)
    data_dst = os.path.join(data_dir_dst, 'data')
    copy_tree(ctx, [data_src], data_dst, uid=uid, gid=gid)

    make_var_run(ctx, fsid, uid, gid)
    c = get_container(ctx, fsid, daemon_type, daemon_id)
    deploy_daemon(ctx, fsid, daemon_type, daemon_id, c, uid, gid)
    update_firewalld(ctx, daemon_type)


def command_adopt_alertmanager(ctx, daemon_id, fsid):
    # type: (CephadmContext, str, str) -> None

    daemon_type = 'alertmanager'
    (uid, gid) = extract_uid_gid_monitoring(ctx, daemon_type)

    _stop_and_disable(ctx, 'prometheus-alertmanager')

    data_dir_dst = make_data_dir(ctx, fsid, daemon_type, daemon_id,
                                 uid=uid, gid=gid)

    # config
    config_src = '/etc/prometheus/alertmanager.yml'
    config_src = os.path.abspath(ctx.legacy_dir + config_src)
    config_dst = os.path.join(data_dir_dst, 'etc/alertmanager')
    makedirs(config_dst, uid, gid, 0o755)
    copy_files(ctx, [config_src], config_dst, uid=uid, gid=gid)

    # data
    data_src = '/var/lib/prometheus/alertmanager/'
    data_src = os.path.abspath(ctx.legacy_dir + data_src)
    data_dst = os.path.join(data_dir_dst, 'etc/alertmanager/data')
    copy_tree(ctx, [data_src], data_dst, uid=uid, gid=gid)

    make_var_run(ctx, fsid, uid, gid)
    c = get_container(ctx, fsid, daemon_type, daemon_id)
    deploy_daemon(ctx, fsid, daemon_type, daemon_id, c, uid, gid)
    update_firewalld(ctx, daemon_type)


def _adjust_grafana_ini(filename):
    # type: (str) -> None

    # Update cert_file, cert_key pathnames in server section
    # ConfigParser does not preserve comments
    try:
        with open(filename, 'r') as grafana_ini:
            lines = grafana_ini.readlines()
        with open('{}.new'.format(filename), 'w') as grafana_ini:
            server_section = False
            for line in lines:
                if line.startswith('['):
                    server_section = False
                if line.startswith('[server]'):
                    server_section = True
                if server_section:
                    line = re.sub(r'^cert_file.*',
                                  'cert_file = /etc/grafana/certs/cert_file', line)
                    line = re.sub(r'^cert_key.*',
                                  'cert_key = /etc/grafana/certs/cert_key', line)
                grafana_ini.write(line)
        os.rename('{}.new'.format(filename), filename)
    except OSError as err:
        raise Error('Cannot update {}: {}'.format(filename, err))


def _stop_and_disable(ctx, unit_name):
    # type: (CephadmContext, str) -> None

    (enabled, state, _) = check_unit(ctx, unit_name)
    if state == 'running':
        logger.info('Stopping old systemd unit %s...' % unit_name)
        call_throws(ctx, ['systemctl', 'stop', unit_name])
    if enabled:
        logger.info('Disabling old systemd unit %s...' % unit_name)
        call_throws(ctx, ['systemctl', 'disable', unit_name])

##################################


def command_rm_daemon(ctx):
    # type: (CephadmContext) -> None
    lock = FileLock(ctx, ctx.fsid)
    lock.acquire()

    (daemon_type, daemon_id) = ctx.name.split('.', 1)
    unit_name = get_unit_name_by_daemon_name(ctx, ctx.fsid, ctx.name)

    if daemon_type in ['mon', 'osd'] and not ctx.force:
        raise Error('must pass --force to proceed: '
                    'this command may destroy precious data!')

    call(ctx, ['systemctl', 'stop', unit_name],
         verbosity=CallVerbosity.DEBUG)
    call(ctx, ['systemctl', 'reset-failed', unit_name],
         verbosity=CallVerbosity.DEBUG)
    call(ctx, ['systemctl', 'disable', unit_name],
         verbosity=CallVerbosity.DEBUG)
    data_dir = get_data_dir(ctx.fsid, ctx.data_dir, daemon_type, daemon_id)
    if daemon_type in ['mon', 'osd', 'prometheus'] and \
       not ctx.force_delete_data:
        # rename it out of the way -- do not delete
        backup_dir = os.path.join(ctx.data_dir, ctx.fsid, 'removed')
        if not os.path.exists(backup_dir):
            makedirs(backup_dir, 0, 0, DATA_DIR_MODE)
        dirname = '%s.%s_%s' % (daemon_type, daemon_id,
                                datetime.datetime.utcnow().strftime(DATEFMT))
        os.rename(data_dir,
                  os.path.join(backup_dir, dirname))
    else:
        if daemon_type == CephadmDaemon.daemon_type:
            CephadmDaemon.uninstall(ctx, ctx.fsid, daemon_type, daemon_id)
        call_throws(ctx, ['rm', '-rf', data_dir])

##################################


def _zap(ctx, what):
    mounts = get_container_mounts(ctx, ctx.fsid, 'clusterless-ceph-volume', None)
    c = CephContainer(
        ctx,
        image=ctx.image,
        entrypoint='/usr/sbin/ceph-volume',
        envs=ctx.env,
        args=['lvm', 'zap', '--destroy', what],
        privileged=True,
        volume_mounts=mounts,
    )
    logger.info(f'Zapping {what}...')
    out, err, code = call_throws(ctx, c.run_cmd())


@infer_image
def _zap_osds(ctx):
    # assume fsid lock already held

    # list
    mounts = get_container_mounts(ctx, ctx.fsid, 'clusterless-ceph-volume', None)
    c = CephContainer(
        ctx,
        image=ctx.image,
        entrypoint='/usr/sbin/ceph-volume',
        envs=ctx.env,
        args=['inventory', '--format', 'json'],
        privileged=True,
        volume_mounts=mounts,
    )
    out, err, code = call_throws(ctx, c.run_cmd())
    if code:
        raise Error('failed to list osd inventory')
    try:
        ls = json.loads(out)
    except ValueError as e:
        raise Error(f'Invalid JSON in ceph-volume inventory: {e}')

    for i in ls:
        matches = [lv.get('cluster_fsid') == ctx.fsid for lv in i.get('lvs', [])]
        if any(matches) and all(matches):
            _zap(ctx, i.get('path'))
        elif any(matches):
            lv_names = [lv['name'] for lv in i.get('lvs', [])]
            # TODO: we need to map the lv_names back to device paths (the vg
            # id isn't part of the output here!)
            logger.warning(f'Not zapping LVs (not implemented): {lv_names}')


def command_zap_osds(ctx):
    if not ctx.force:
        raise Error('must pass --force to proceed: '
                    'this command may destroy precious data!')

    lock = FileLock(ctx, ctx.fsid)
    lock.acquire()

    _zap_osds(ctx)

##################################


def command_rm_cluster(ctx):
    # type: (CephadmContext) -> None
    if not ctx.force:
        raise Error('must pass --force to proceed: '
                    'this command may destroy precious data!')

    lock = FileLock(ctx, ctx.fsid)
    lock.acquire()

    # stop + disable individual daemon units
    for d in list_daemons(ctx, detail=False):
        if d['fsid'] != ctx.fsid:
            continue
        if d['style'] != 'cephadm:v1':
            continue
        unit_name = get_unit_name(ctx.fsid, d['name'])
        call(ctx, ['systemctl', 'stop', unit_name],
             verbosity=CallVerbosity.DEBUG)
        call(ctx, ['systemctl', 'reset-failed', unit_name],
             verbosity=CallVerbosity.DEBUG)
        call(ctx, ['systemctl', 'disable', unit_name],
             verbosity=CallVerbosity.DEBUG)

    # cluster units
    for unit_name in ['ceph-%s.target' % ctx.fsid]:
        call(ctx, ['systemctl', 'stop', unit_name],
             verbosity=CallVerbosity.DEBUG)
        call(ctx, ['systemctl', 'reset-failed', unit_name],
             verbosity=CallVerbosity.DEBUG)
        call(ctx, ['systemctl', 'disable', unit_name],
             verbosity=CallVerbosity.DEBUG)

    slice_name = 'system-%s.slice' % (('ceph-%s' % ctx.fsid).replace('-', '\\x2d'))
    call(ctx, ['systemctl', 'stop', slice_name],
         verbosity=CallVerbosity.DEBUG)

    # osds?
    if ctx.zap_osds:
        _zap_osds(ctx)

    # rm units
    call_throws(ctx, ['rm', '-f', ctx.unit_dir
                      + '/ceph-%s@.service' % ctx.fsid])
    call_throws(ctx, ['rm', '-f', ctx.unit_dir
                      + '/ceph-%s.target' % ctx.fsid])
    call_throws(ctx, ['rm', '-rf',
                      ctx.unit_dir + '/ceph-%s.target.wants' % ctx.fsid])
    # rm data
    call_throws(ctx, ['rm', '-rf', ctx.data_dir + '/' + ctx.fsid])

    if not ctx.keep_logs:
        # rm logs
        call_throws(ctx, ['rm', '-rf', ctx.log_dir + '/' + ctx.fsid])
        call_throws(ctx, ['rm', '-rf', ctx.log_dir
                          + '/*.wants/ceph-%s@*' % ctx.fsid])

    # rm logrotate config
    call_throws(ctx, ['rm', '-f', ctx.logrotate_dir + '/ceph-%s' % ctx.fsid])

    # rm cephadm logrotate config if last cluster on host
    if not os.listdir(ctx.data_dir):
        call_throws(ctx, ['rm', '-f', ctx.logrotate_dir + '/cephadm'])

    # rm sysctl settings
    sysctl_dir = Path(ctx.sysctl_dir)
    for p in sysctl_dir.glob(f'90-ceph-{ctx.fsid}-*.conf'):
        p.unlink()

    # clean up config, keyring, and pub key files
    files = ['/etc/ceph/ceph.conf', '/etc/ceph/ceph.pub', '/etc/ceph/ceph.client.admin.keyring']

    if os.path.exists(files[0]):
        valid_fsid = False
        with open(files[0]) as f:
            if ctx.fsid in f.read():
                valid_fsid = True
        if valid_fsid:
            for n in range(0, len(files)):
                if os.path.exists(files[n]):
                    os.remove(files[n])


##################################


def check_time_sync(ctx, enabler=None):
    # type: (CephadmContext, Optional[Packager]) -> bool
    units = [
        'chrony.service',  # 18.04 (at least)
        'chronyd.service',  # el / opensuse
        'systemd-timesyncd.service',
        'ntpd.service',  # el7 (at least)
        'ntp.service',  # 18.04 (at least)
        'ntpsec.service',  # 20.04 (at least) / buster
    ]
    if not check_units(ctx, units, enabler):
        logger.warning('No time sync service is running; checked for %s' % units)
        return False
    return True


def command_check_host(ctx: CephadmContext) -> None:
    container_path = ctx.container_engine.path

    errors = []
    commands = ['systemctl', 'lvcreate']

    try:
        check_container_engine(ctx)
        logger.info('podman|docker (%s) is present' % container_path)
    except Error as e:
        errors.append(str(e))

    for command in commands:
        try:
            find_program(command)
            logger.info('%s is present' % command)
        except ValueError:
            errors.append('%s binary does not appear to be installed' % command)

    # check for configured+running chronyd or ntp
    if not check_time_sync(ctx):
        errors.append('No time synchronization is active')

    if 'expect_hostname' in ctx and ctx.expect_hostname:
        if get_hostname().lower() != ctx.expect_hostname.lower():
            errors.append('hostname "%s" does not match expected hostname "%s"' % (
                get_hostname(), ctx.expect_hostname))
        logger.info('Hostname "%s" matches what is expected.',
                    ctx.expect_hostname)

    if errors:
        raise Error('\nERROR: '.join(errors))

    logger.info('Host looks OK')

##################################


def command_prepare_host(ctx: CephadmContext) -> None:
    logger.info('Verifying podman|docker is present...')
    pkg = None
    try:
        check_container_engine(ctx)
    except Error as e:
        logger.warning(str(e))
        if not pkg:
            pkg = create_packager(ctx)
        pkg.install_podman()

    logger.info('Verifying lvm2 is present...')
    if not find_executable('lvcreate'):
        if not pkg:
            pkg = create_packager(ctx)
        pkg.install(['lvm2'])

    logger.info('Verifying time synchronization is in place...')
    if not check_time_sync(ctx):
        if not pkg:
            pkg = create_packager(ctx)
        pkg.install(['chrony'])
        # check again, and this time try to enable
        # the service
        check_time_sync(ctx, enabler=pkg)

    if 'expect_hostname' in ctx and ctx.expect_hostname and ctx.expect_hostname != get_hostname():
        logger.warning('Adjusting hostname from %s -> %s...' % (get_hostname(), ctx.expect_hostname))
        call_throws(ctx, ['hostname', ctx.expect_hostname])
        with open('/etc/hostname', 'w') as f:
            f.write(ctx.expect_hostname + '\n')

    logger.info('Repeating the final host check...')
    command_check_host(ctx)

##################################


class CustomValidation(argparse.Action):

    def _check_name(self, values):
        try:
            (daemon_type, daemon_id) = values.split('.', 1)
        except ValueError:
            raise argparse.ArgumentError(self,
                                         'must be of the format <type>.<id>. For example, osd.1 or prometheus.myhost.com')

        daemons = get_supported_daemons()
        if daemon_type not in daemons:
            raise argparse.ArgumentError(self,
                                         'name must declare the type of daemon e.g. '
                                         '{}'.format(', '.join(daemons)))

    def __call__(self, parser, namespace, values, option_string=None):
        if self.dest == 'name':
            self._check_name(values)
            setattr(namespace, self.dest, values)
        elif self.dest == 'exporter_config':
            cfg = get_parm(values)
            # run the class' validate method, and convert to an argparse error
            # if problems are found
            try:
                CephadmDaemon.validate_config(cfg)
            except Error as e:
                raise argparse.ArgumentError(self,
                                             str(e))
            setattr(namespace, self.dest, cfg)

##################################


def get_distro():
    # type: () -> Tuple[Optional[str], Optional[str], Optional[str]]
    distro = None
    distro_version = None
    distro_codename = None
    with open('/etc/os-release', 'r') as f:
        for line in f.readlines():
            line = line.strip()
            if '=' not in line or line.startswith('#'):
                continue
            (var, val) = line.split('=', 1)
            if val[0] == '"' and val[-1] == '"':
                val = val[1:-1]
            if var == 'ID':
                distro = val.lower()
            elif var == 'VERSION_ID':
                distro_version = val.lower()
            elif var == 'VERSION_CODENAME':
                distro_codename = val.lower()
    return distro, distro_version, distro_codename


class Packager(object):
    def __init__(self, ctx: CephadmContext,
                 stable=None, version=None, branch=None, commit=None):
        assert \
            (stable and not version and not branch and not commit) or \
            (not stable and version and not branch and not commit) or \
            (not stable and not version and branch) or \
            (not stable and not version and not branch and not commit)
        self.ctx = ctx
        self.stable = stable
        self.version = version
        self.branch = branch
        self.commit = commit

    def add_repo(self):
        raise NotImplementedError

    def rm_repo(self):
        raise NotImplementedError

    def query_shaman(self, distro, distro_version, branch, commit):
        # query shaman
        logger.info('Fetching repo metadata from shaman and chacra...')
        shaman_url = 'https://shaman.ceph.com/api/repos/ceph/{branch}/{sha1}/{distro}/{distro_version}/repo/?arch={arch}'.format(
            distro=distro,
            distro_version=distro_version,
            branch=branch,
            sha1=commit or 'latest',
            arch=get_arch()
        )
        try:
            shaman_response = urlopen(shaman_url)
        except HTTPError as err:
            logger.error('repository not found in shaman (might not be available yet)')
            raise Error('%s, failed to fetch %s' % (err, shaman_url))
        chacra_url = ''
        try:
            chacra_url = shaman_response.geturl()
            chacra_response = urlopen(chacra_url)
        except HTTPError as err:
            logger.error('repository not found in chacra (might not be available yet)')
            raise Error('%s, failed to fetch %s' % (err, chacra_url))
        return chacra_response.read().decode('utf-8')

    def repo_gpgkey(self):
        if self.ctx.gpg_url:
            return self.ctx.gpg_url
        if self.stable or self.version:
            return 'https://download.ceph.com/keys/release.asc', 'release'
        else:
            return 'https://download.ceph.com/keys/autobuild.asc', 'autobuild'

    def enable_service(self, service):
        """
        Start and enable the service (typically using systemd).
        """
        call_throws(self.ctx, ['systemctl', 'enable', '--now', service])


class Apt(Packager):
    DISTRO_NAMES = {
        'ubuntu': 'ubuntu',
        'debian': 'debian',
    }

    def __init__(self, ctx: CephadmContext,
                 stable, version, branch, commit,
                 distro, distro_version, distro_codename):
        super(Apt, self).__init__(ctx, stable=stable, version=version,
                                  branch=branch, commit=commit)
        self.ctx = ctx
        self.distro = self.DISTRO_NAMES[distro]
        self.distro_codename = distro_codename
        self.distro_version = distro_version

    def repo_path(self):
        return '/etc/apt/sources.list.d/ceph.list'

    def add_repo(self):

        url, name = self.repo_gpgkey()
        logger.info('Installing repo GPG key from %s...' % url)
        try:
            response = urlopen(url)
        except HTTPError as err:
            logger.error('failed to fetch GPG repo key from %s: %s' % (
                url, err))
            raise Error('failed to fetch GPG key')
        key = response.read().decode('utf-8')
        with open('/etc/apt/trusted.gpg.d/ceph.%s.gpg' % name, 'w') as f:
            f.write(key)

        if self.version:
            content = 'deb %s/debian-%s/ %s main\n' % (
                self.ctx.repo_url, self.version, self.distro_codename)
        elif self.stable:
            content = 'deb %s/debian-%s/ %s main\n' % (
                self.ctx.repo_url, self.stable, self.distro_codename)
        else:
            content = self.query_shaman(self.distro, self.distro_codename, self.branch,
                                        self.commit)

        logger.info('Installing repo file at %s...' % self.repo_path())
        with open(self.repo_path(), 'w') as f:
            f.write(content)

    def rm_repo(self):
        for name in ['autobuild', 'release']:
            p = '/etc/apt/trusted.gpg.d/ceph.%s.gpg' % name
            if os.path.exists(p):
                logger.info('Removing repo GPG key %s...' % p)
                os.unlink(p)
        if os.path.exists(self.repo_path()):
            logger.info('Removing repo at %s...' % self.repo_path())
            os.unlink(self.repo_path())

        if self.distro == 'ubuntu':
            self.rm_kubic_repo()

    def install(self, ls):
        logger.info('Installing packages %s...' % ls)
        call_throws(self.ctx, ['apt-get', 'install', '-y'] + ls)

    def install_podman(self):
        if self.distro == 'ubuntu':
            logger.info('Setting up repo for podman...')
            self.add_kubic_repo()
            call_throws(self.ctx, ['apt-get', 'update'])

        logger.info('Attempting podman install...')
        try:
            self.install(['podman'])
        except Error:
            logger.info('Podman did not work.  Falling back to docker...')
            self.install(['docker.io'])

    def kubic_repo_url(self):
        return 'https://download.opensuse.org/repositories/devel:/kubic:/' \
               'libcontainers:/stable/xUbuntu_%s/' % self.distro_version

    def kubic_repo_path(self):
        return '/etc/apt/sources.list.d/devel:kubic:libcontainers:stable.list'

    def kubric_repo_gpgkey_url(self):
        return '%s/Release.key' % self.kubic_repo_url()

    def kubric_repo_gpgkey_path(self):
        return '/etc/apt/trusted.gpg.d/kubic.release.gpg'

    def add_kubic_repo(self):
        url = self.kubric_repo_gpgkey_url()
        logger.info('Installing repo GPG key from %s...' % url)
        try:
            response = urlopen(url)
        except HTTPError as err:
            logger.error('failed to fetch GPG repo key from %s: %s' % (
                url, err))
            raise Error('failed to fetch GPG key')
        key = response.read().decode('utf-8')
        tmp_key = write_tmp(key, 0, 0)
        keyring = self.kubric_repo_gpgkey_path()
        call_throws(self.ctx, ['apt-key', '--keyring', keyring, 'add', tmp_key.name])

        logger.info('Installing repo file at %s...' % self.kubic_repo_path())
        content = 'deb %s /\n' % self.kubic_repo_url()
        with open(self.kubic_repo_path(), 'w') as f:
            f.write(content)

    def rm_kubic_repo(self):
        keyring = self.kubric_repo_gpgkey_path()
        if os.path.exists(keyring):
            logger.info('Removing repo GPG key %s...' % keyring)
            os.unlink(keyring)

        p = self.kubic_repo_path()
        if os.path.exists(p):
            logger.info('Removing repo at %s...' % p)
            os.unlink(p)


class YumDnf(Packager):
    DISTRO_NAMES = {
        'centos': ('centos', 'el'),
        'rhel': ('centos', 'el'),
        'scientific': ('centos', 'el'),
        'rocky': ('centos', 'el'),
        'fedora': ('fedora', 'fc'),
    }

    def __init__(self, ctx: CephadmContext,
                 stable, version, branch, commit,
                 distro, distro_version):
        super(YumDnf, self).__init__(ctx, stable=stable, version=version,
                                     branch=branch, commit=commit)
        self.ctx = ctx
        self.major = int(distro_version.split('.')[0])
        self.distro_normalized = self.DISTRO_NAMES[distro][0]
        self.distro_code = self.DISTRO_NAMES[distro][1] + str(self.major)
        if (self.distro_code == 'fc' and self.major >= 30) or \
           (self.distro_code == 'el' and self.major >= 8):
            self.tool = 'dnf'
        else:
            self.tool = 'yum'

    def custom_repo(self, **kw):
        """
        Repo files need special care in that a whole line should not be present
        if there is no value for it. Because we were using `format()` we could
        not conditionally add a line for a repo file. So the end result would
        contain a key with a missing value (say if we were passing `None`).

        For example, it could look like::

        [ceph repo]
        name= ceph repo
        proxy=
        gpgcheck=

        Which breaks. This function allows us to conditionally add lines,
        preserving an order and be more careful.

        Previously, and for historical purposes, this is how the template used
        to look::

        custom_repo =
        [{repo_name}]
        name={name}
        baseurl={baseurl}
        enabled={enabled}
        gpgcheck={gpgcheck}
        type={_type}
        gpgkey={gpgkey}
        proxy={proxy}

        """
        lines = []

        # by using tuples (vs a dict) we preserve the order of what we want to
        # return, like starting with a [repo name]
        tmpl = (
            ('reponame', '[%s]'),
            ('name', 'name=%s'),
            ('baseurl', 'baseurl=%s'),
            ('enabled', 'enabled=%s'),
            ('gpgcheck', 'gpgcheck=%s'),
            ('_type', 'type=%s'),
            ('gpgkey', 'gpgkey=%s'),
            ('proxy', 'proxy=%s'),
            ('priority', 'priority=%s'),
        )

        for line in tmpl:
            tmpl_key, tmpl_value = line  # key values from tmpl

            # ensure that there is an actual value (not None nor empty string)
            if tmpl_key in kw and kw.get(tmpl_key) not in (None, ''):
                lines.append(tmpl_value % kw.get(tmpl_key))

        return '\n'.join(lines)

    def repo_path(self):
        return '/etc/yum.repos.d/ceph.repo'

    def repo_baseurl(self):
        assert self.stable or self.version
        if self.version:
            return '%s/rpm-%s/%s' % (self.ctx.repo_url, self.version,
                                     self.distro_code)
        else:
            return '%s/rpm-%s/%s' % (self.ctx.repo_url, self.stable,
                                     self.distro_code)

    def add_repo(self):
        if self.stable or self.version:
            content = ''
            for n, t in {
                    'Ceph': '$basearch',
                    'Ceph-noarch': 'noarch',
                    'Ceph-source': 'SRPMS'}.items():
                content += '[%s]\n' % (n)
                content += self.custom_repo(
                    name='Ceph %s' % t,
                    baseurl=self.repo_baseurl() + '/' + t,
                    enabled=1,
                    gpgcheck=1,
                    gpgkey=self.repo_gpgkey()[0],
                )
                content += '\n\n'
        else:
            content = self.query_shaman(self.distro_normalized, self.major,
                                        self.branch,
                                        self.commit)

        logger.info('Writing repo to %s...' % self.repo_path())
        with open(self.repo_path(), 'w') as f:
            f.write(content)

        if self.distro_code.startswith('el'):
            logger.info('Enabling EPEL...')
            call_throws(self.ctx, [self.tool, 'install', '-y', 'epel-release'])

    def rm_repo(self):
        if os.path.exists(self.repo_path()):
            os.unlink(self.repo_path())

    def install(self, ls):
        logger.info('Installing packages %s...' % ls)
        call_throws(self.ctx, [self.tool, 'install', '-y'] + ls)

    def install_podman(self):
        self.install(['podman'])


class Zypper(Packager):
    DISTRO_NAMES = [
        'sles',
        'opensuse-tumbleweed',
        'opensuse-leap'
    ]

    def __init__(self, ctx: CephadmContext,
                 stable, version, branch, commit,
                 distro, distro_version):
        super(Zypper, self).__init__(ctx, stable=stable, version=version,
                                     branch=branch, commit=commit)
        self.ctx = ctx
        self.tool = 'zypper'
        self.distro = 'opensuse'
        self.distro_version = '15.1'
        if 'tumbleweed' not in distro and distro_version is not None:
            self.distro_version = distro_version

    def custom_repo(self, **kw):
        """
        See YumDnf for format explanation.
        """
        lines = []

        # by using tuples (vs a dict) we preserve the order of what we want to
        # return, like starting with a [repo name]
        tmpl = (
            ('reponame', '[%s]'),
            ('name', 'name=%s'),
            ('baseurl', 'baseurl=%s'),
            ('enabled', 'enabled=%s'),
            ('gpgcheck', 'gpgcheck=%s'),
            ('_type', 'type=%s'),
            ('gpgkey', 'gpgkey=%s'),
            ('proxy', 'proxy=%s'),
            ('priority', 'priority=%s'),
        )

        for line in tmpl:
            tmpl_key, tmpl_value = line  # key values from tmpl

            # ensure that there is an actual value (not None nor empty string)
            if tmpl_key in kw and kw.get(tmpl_key) not in (None, ''):
                lines.append(tmpl_value % kw.get(tmpl_key))

        return '\n'.join(lines)

    def repo_path(self):
        return '/etc/zypp/repos.d/ceph.repo'

    def repo_baseurl(self):
        assert self.stable or self.version
        if self.version:
            return '%s/rpm-%s/%s' % (self.ctx.repo_url,
                                     self.stable, self.distro)
        else:
            return '%s/rpm-%s/%s' % (self.ctx.repo_url,
                                     self.stable, self.distro)

    def add_repo(self):
        if self.stable or self.version:
            content = ''
            for n, t in {
                    'Ceph': '$basearch',
                    'Ceph-noarch': 'noarch',
                    'Ceph-source': 'SRPMS'}.items():
                content += '[%s]\n' % (n)
                content += self.custom_repo(
                    name='Ceph %s' % t,
                    baseurl=self.repo_baseurl() + '/' + t,
                    enabled=1,
                    gpgcheck=1,
                    gpgkey=self.repo_gpgkey()[0],
                )
                content += '\n\n'
        else:
            content = self.query_shaman(self.distro, self.distro_version,
                                        self.branch,
                                        self.commit)

        logger.info('Writing repo to %s...' % self.repo_path())
        with open(self.repo_path(), 'w') as f:
            f.write(content)

    def rm_repo(self):
        if os.path.exists(self.repo_path()):
            os.unlink(self.repo_path())

    def install(self, ls):
        logger.info('Installing packages %s...' % ls)
        call_throws(self.ctx, [self.tool, 'in', '-y'] + ls)

    def install_podman(self):
        self.install(['podman'])


def create_packager(ctx: CephadmContext,
                    stable=None, version=None, branch=None, commit=None):
    distro, distro_version, distro_codename = get_distro()
    if distro in YumDnf.DISTRO_NAMES:
        return YumDnf(ctx, stable=stable, version=version,
                      branch=branch, commit=commit,
                      distro=distro, distro_version=distro_version)
    elif distro in Apt.DISTRO_NAMES:
        return Apt(ctx, stable=stable, version=version,
                   branch=branch, commit=commit,
                   distro=distro, distro_version=distro_version,
                   distro_codename=distro_codename)
    elif distro in Zypper.DISTRO_NAMES:
        return Zypper(ctx, stable=stable, version=version,
                      branch=branch, commit=commit,
                      distro=distro, distro_version=distro_version)
    raise Error('Distro %s version %s not supported' % (distro, distro_version))


def command_add_repo(ctx: CephadmContext):
    if ctx.version and ctx.release:
        raise Error('you can specify either --release or --version but not both')
    if not ctx.version and not ctx.release and not ctx.dev and not ctx.dev_commit:
        raise Error('please supply a --release, --version, --dev or --dev-commit argument')
    if ctx.version:
        try:
            (x, y, z) = ctx.version.split('.')
        except Exception:
            raise Error('version must be in the form x.y.z (e.g., 15.2.0)')

    pkg = create_packager(ctx, stable=ctx.release,
                          version=ctx.version,
                          branch=ctx.dev,
                          commit=ctx.dev_commit)
    pkg.add_repo()


def command_rm_repo(ctx: CephadmContext):
    pkg = create_packager(ctx)
    pkg.rm_repo()


def command_install(ctx: CephadmContext):
    pkg = create_packager(ctx)
    pkg.install(ctx.packages)

##################################


def get_ipv4_address(ifname):
    # type: (str) -> str
    def _extract(sock, offset):
        return socket.inet_ntop(
            socket.AF_INET,
            fcntl.ioctl(
                sock.fileno(),
                offset,
                struct.pack('256s', bytes(ifname[:15], 'utf-8'))
            )[20:24])

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        addr = _extract(s, 35093)  # '0x8915' = SIOCGIFADDR
        dq_mask = _extract(s, 35099)  # 0x891b = SIOCGIFNETMASK
    except OSError:
        # interface does not have an ipv4 address
        return ''

    dec_mask = sum([bin(int(i)).count('1')
                    for i in dq_mask.split('.')])
    return '{}/{}'.format(addr, dec_mask)


def get_ipv6_address(ifname):
    # type: (str) -> str
    if not os.path.exists('/proc/net/if_inet6'):
        return ''

    raw = read_file(['/proc/net/if_inet6'])
    data = raw.splitlines()
    # based on docs @ https://www.tldp.org/HOWTO/Linux+IPv6-HOWTO/ch11s04.html
    # field 0 is ipv6, field 2 is scope
    for iface_setting in data:
        field = iface_setting.split()
        if field[-1] == ifname:
            ipv6_raw = field[0]
            ipv6_fmtd = ':'.join([ipv6_raw[_p:_p + 4] for _p in range(0, len(field[0]), 4)])
            # apply naming rules using ipaddress module
            ipv6 = ipaddress.ip_address(ipv6_fmtd)
            return '{}/{}'.format(str(ipv6), int('0x{}'.format(field[2]), 16))
    return ''


def bytes_to_human(num, mode='decimal'):
    # type: (float, str) -> str
    """Convert a bytes value into it's human-readable form.

    :param num: number, in bytes, to convert
    :param mode: Either decimal (default) or binary to determine divisor
    :returns: string representing the bytes value in a more readable format
    """
    unit_list = ['', 'KB', 'MB', 'GB', 'TB', 'PB', 'EB', 'ZB']
    divisor = 1000.0
    yotta = 'YB'

    if mode == 'binary':
        unit_list = ['', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB', 'EiB', 'ZiB']
        divisor = 1024.0
        yotta = 'YiB'

    for unit in unit_list:
        if abs(num) < divisor:
            return '%3.1f%s' % (num, unit)
        num /= divisor
    return '%.1f%s' % (num, yotta)


def read_file(path_list, file_name=''):
    # type: (List[str], str) -> str
    """Returns the content of the first file found within the `path_list`

    :param path_list: list of file paths to search
    :param file_name: optional file_name to be applied to a file path
    :returns: content of the file or 'Unknown'
    """
    for path in path_list:
        if file_name:
            file_path = os.path.join(path, file_name)
        else:
            file_path = path
        if os.path.exists(file_path):
            with open(file_path, 'r') as f:
                try:
                    content = f.read().strip()
                except OSError:
                    # sysfs may populate the file, but for devices like
                    # virtio reads can fail
                    return 'Unknown'
                else:
                    return content
    return 'Unknown'

##################################


class HostFacts():
    _dmi_path_list = ['/sys/class/dmi/id']
    _nic_path_list = ['/sys/class/net']
    _selinux_path_list = ['/etc/selinux/config']
    _apparmor_path_list = ['/etc/apparmor']
    _disk_vendor_workarounds = {
        '0x1af4': 'Virtio Block Device'
    }

    def __init__(self, ctx: CephadmContext):
        self.ctx: CephadmContext = ctx
        self.cpu_model: str = 'Unknown'
        self.cpu_count: int = 0
        self.cpu_cores: int = 0
        self.cpu_threads: int = 0
        self.interfaces: Dict[str, Any] = {}

        self._meminfo: List[str] = read_file(['/proc/meminfo']).splitlines()
        self._get_cpuinfo()
        self._process_nics()
        self.arch: str = platform.processor()
        self.kernel: str = platform.release()

    def _get_cpuinfo(self):
        # type: () -> None
        """Determine cpu information via /proc/cpuinfo"""
        raw = read_file(['/proc/cpuinfo'])
        output = raw.splitlines()
        cpu_set = set()

        for line in output:
            field = [f.strip() for f in line.split(':')]
            if 'model name' in line:
                self.cpu_model = field[1]
            if 'physical id' in line:
                cpu_set.add(field[1])
            if 'siblings' in line:
                self.cpu_threads = int(field[1].strip())
            if 'cpu cores' in line:
                self.cpu_cores = int(field[1].strip())
            pass
        self.cpu_count = len(cpu_set)

    def _get_block_devs(self):
        # type: () -> List[str]
        """Determine the list of block devices by looking at /sys/block"""
        return [dev for dev in os.listdir('/sys/block')
                if not dev.startswith('dm')]

    def _get_devs_by_type(self, rota='0'):
        # type: (str) -> List[str]
        """Filter block devices by a given rotational attribute (0=flash, 1=spinner)"""
        devs = list()
        for blk_dev in self._get_block_devs():
            rot_path = '/sys/block/{}/queue/rotational'.format(blk_dev)
            rot_value = read_file([rot_path])
            if rot_value == rota:
                devs.append(blk_dev)
        return devs

    @property
    def operating_system(self):
        # type: () -> str
        """Determine OS version"""
        raw_info = read_file(['/etc/os-release'])
        os_release = raw_info.splitlines()
        rel_str = 'Unknown'
        rel_dict = dict()

        for line in os_release:
            if '=' in line:
                var_name, var_value = line.split('=')
                rel_dict[var_name] = var_value.strip('"')

        # Would normally use PRETTY_NAME, but NAME and VERSION are more
        # consistent
        if all(_v in rel_dict for _v in ['NAME', 'VERSION']):
            rel_str = '{} {}'.format(rel_dict['NAME'], rel_dict['VERSION'])
        return rel_str

    @property
    def hostname(self):
        # type: () -> str
        """Return the hostname"""
        return platform.node()

    @property
    def subscribed(self):
        # type: () -> str
        """Highlevel check to see if the host is subscribed to receive updates/support"""
        def _red_hat():
            # type: () -> str
            # RHEL 7 and RHEL 8
            entitlements_dir = '/etc/pki/entitlement'
            if os.path.exists(entitlements_dir):
                pems = glob('{}/*.pem'.format(entitlements_dir))
                if len(pems) >= 2:
                    return 'Yes'

            return 'No'

        os_name = self.operating_system
        if os_name.upper().startswith('RED HAT'):
            return _red_hat()

        return 'Unknown'

    @property
    def hdd_count(self):
        # type: () -> int
        """Return a count of HDDs (spinners)"""
        return len(self._get_devs_by_type(rota='1'))

    def _get_capacity(self, dev):
        # type: (str) -> int
        """Determine the size of a given device"""
        size_path = os.path.join('/sys/block', dev, 'size')
        size_blocks = int(read_file([size_path]))
        blk_path = os.path.join('/sys/block', dev, 'queue', 'logical_block_size')
        blk_count = int(read_file([blk_path]))
        return size_blocks * blk_count

    def _get_capacity_by_type(self, rota='0'):
        # type: (str) -> int
        """Return the total capacity of a category of device (flash or hdd)"""
        devs = self._get_devs_by_type(rota=rota)
        capacity = 0
        for dev in devs:
            capacity += self._get_capacity(dev)
        return capacity

    def _dev_list(self, dev_list):
        # type: (List[str]) -> List[Dict[str, object]]
        """Return a 'pretty' name list for each device in the `dev_list`"""
        disk_list = list()

        for dev in dev_list:
            disk_model = read_file(['/sys/block/{}/device/model'.format(dev)]).strip()
            disk_rev = read_file(['/sys/block/{}/device/rev'.format(dev)]).strip()
            disk_wwid = read_file(['/sys/block/{}/device/wwid'.format(dev)]).strip()
            vendor = read_file(['/sys/block/{}/device/vendor'.format(dev)]).strip()
            disk_vendor = HostFacts._disk_vendor_workarounds.get(vendor, vendor)
            disk_size_bytes = self._get_capacity(dev)
            disk_list.append({
                'description': '{} {} ({})'.format(disk_vendor, disk_model, bytes_to_human(disk_size_bytes)),
                'vendor': disk_vendor,
                'model': disk_model,
                'rev': disk_rev,
                'wwid': disk_wwid,
                'dev_name': dev,
                'disk_size_bytes': disk_size_bytes,
            })
        return disk_list

    @property
    def hdd_list(self):
        # type: () -> List[Dict[str, object]]
        """Return a list of devices that are HDDs (spinners)"""
        devs = self._get_devs_by_type(rota='1')
        return self._dev_list(devs)

    @property
    def flash_list(self):
        # type: () -> List[Dict[str, object]]
        """Return a list of devices that are flash based (SSD, NVMe)"""
        devs = self._get_devs_by_type(rota='0')
        return self._dev_list(devs)

    @property
    def hdd_capacity_bytes(self):
        # type: () -> int
        """Return the total capacity for all HDD devices (bytes)"""
        return self._get_capacity_by_type(rota='1')

    @property
    def hdd_capacity(self):
        # type: () -> str
        """Return the total capacity for all HDD devices (human readable format)"""
        return bytes_to_human(self.hdd_capacity_bytes)

    @property
    def cpu_load(self):
        # type: () -> Dict[str, float]
        """Return the cpu load average data for the host"""
        raw = read_file(['/proc/loadavg']).strip()
        data = raw.split()
        return {
            '1min': float(data[0]),
            '5min': float(data[1]),
            '15min': float(data[2]),
        }

    @property
    def flash_count(self):
        # type: () -> int
        """Return the number of flash devices in the system (SSD, NVMe)"""
        return len(self._get_devs_by_type(rota='0'))

    @property
    def flash_capacity_bytes(self):
        # type: () -> int
        """Return the total capacity for all flash devices (bytes)"""
        return self._get_capacity_by_type(rota='0')

    @property
    def flash_capacity(self):
        # type: () -> str
        """Return the total capacity for all Flash devices (human readable format)"""
        return bytes_to_human(self.flash_capacity_bytes)

    def _process_nics(self):
        # type: () -> None
        """Look at the NIC devices and extract network related metadata"""
        # from https://github.com/torvalds/linux/blob/master/include/uapi/linux/if_arp.h
        hw_lookup = {
            '1': 'ethernet',
            '32': 'infiniband',
            '772': 'loopback',
        }

        for nic_path in HostFacts._nic_path_list:
            if not os.path.exists(nic_path):
                continue
            for iface in os.listdir(nic_path):

                lower_devs_list = [os.path.basename(link.replace('lower_', '')) for link in glob(os.path.join(nic_path, iface, 'lower_*'))]
                upper_devs_list = [os.path.basename(link.replace('upper_', '')) for link in glob(os.path.join(nic_path, iface, 'upper_*'))]

                try:
                    mtu = int(read_file([os.path.join(nic_path, iface, 'mtu')]))
                except ValueError:
                    mtu = 0

                operstate = read_file([os.path.join(nic_path, iface, 'operstate')])
                try:
                    speed = int(read_file([os.path.join(nic_path, iface, 'speed')]))
                except (OSError, ValueError):
                    # OSError : device doesn't support the ethtool get_link_ksettings
                    # ValueError : raised when the read fails, and returns Unknown
                    #
                    # Either way, we show a -1 when speed isn't available
                    speed = -1

                if os.path.exists(os.path.join(nic_path, iface, 'bridge')):
                    nic_type = 'bridge'
                elif os.path.exists(os.path.join(nic_path, iface, 'bonding')):
                    nic_type = 'bonding'
                else:
                    nic_type = hw_lookup.get(read_file([os.path.join(nic_path, iface, 'type')]), 'Unknown')

                dev_link = os.path.join(nic_path, iface, 'device')
                if os.path.exists(dev_link):
                    iftype = 'physical'
                    driver_path = os.path.join(dev_link, 'driver')
                    if os.path.exists(driver_path):
                        driver = os.path.basename(os.path.realpath(driver_path))
                    else:
                        driver = 'Unknown'

                else:
                    iftype = 'logical'
                    driver = ''

                self.interfaces[iface] = {
                    'mtu': mtu,
                    'upper_devs_list': upper_devs_list,
                    'lower_devs_list': lower_devs_list,
                    'operstate': operstate,
                    'iftype': iftype,
                    'nic_type': nic_type,
                    'driver': driver,
                    'speed': speed,
                    'ipv4_address': get_ipv4_address(iface),
                    'ipv6_address': get_ipv6_address(iface),
                }

    @property
    def nic_count(self):
        # type: () -> int
        """Return a total count of all physical NICs detected in the host"""
        phys_devs = []
        for iface in self.interfaces:
            if self.interfaces[iface]['iftype'] == 'physical':
                phys_devs.append(iface)
        return len(phys_devs)

    def _get_mem_data(self, field_name):
        # type: (str) -> int
        for line in self._meminfo:
            if line.startswith(field_name):
                _d = line.split()
                return int(_d[1])
        return 0

    @property
    def memory_total_kb(self):
        # type: () -> int
        """Determine the memory installed (kb)"""
        return self._get_mem_data('MemTotal')

    @property
    def memory_free_kb(self):
        # type: () -> int
        """Determine the memory free (not cache, immediately usable)"""
        return self._get_mem_data('MemFree')

    @property
    def memory_available_kb(self):
        # type: () -> int
        """Determine the memory available to new applications without swapping"""
        return self._get_mem_data('MemAvailable')

    @property
    def vendor(self):
        # type: () -> str
        """Determine server vendor from DMI data in sysfs"""
        return read_file(HostFacts._dmi_path_list, 'sys_vendor')

    @property
    def model(self):
        # type: () -> str
        """Determine server model information from DMI data in sysfs"""
        family = read_file(HostFacts._dmi_path_list, 'product_family')
        product = read_file(HostFacts._dmi_path_list, 'product_name')
        if family == 'Unknown' and product:
            return '{}'.format(product)

        return '{} ({})'.format(family, product)

    @property
    def bios_version(self):
        # type: () -> str
        """Determine server BIOS version from  DMI data in sysfs"""
        return read_file(HostFacts._dmi_path_list, 'bios_version')

    @property
    def bios_date(self):
        # type: () -> str
        """Determine server BIOS date from  DMI data in sysfs"""
        return read_file(HostFacts._dmi_path_list, 'bios_date')

    @property
    def timestamp(self):
        # type: () -> float
        """Return the current time as Epoch seconds"""
        return time.time()

    @property
    def system_uptime(self):
        # type: () -> float
        """Return the system uptime (in secs)"""
        raw_time = read_file(['/proc/uptime'])
        up_secs, _ = raw_time.split()
        return float(up_secs)

    @property
    def kernel_security(self):
        # type: () -> Dict[str, str]
        """Determine the security features enabled in the kernel - SELinux, AppArmor"""
        def _fetch_selinux() -> Dict[str, str]:
            """Read the selinux config file to determine state"""
            security = {}
            for selinux_path in HostFacts._selinux_path_list:
                if os.path.exists(selinux_path):
                    selinux_config = read_file([selinux_path]).splitlines()
                    security['type'] = 'SELinux'
                    for line in selinux_config:
                        if line.strip().startswith('#'):
                            continue
                        k, v = line.split('=')
                        security[k] = v
                    if security['SELINUX'].lower() == 'disabled':
                        security['description'] = 'SELinux: Disabled'
                    else:
                        security['description'] = 'SELinux: Enabled({}, {})'.format(security['SELINUX'], security['SELINUXTYPE'])
                    return security
            return {}

        def _fetch_apparmor() -> Dict[str, str]:
            """Read the apparmor profiles directly, returning an overview of AppArmor status"""
            security = {}
            for apparmor_path in HostFacts._apparmor_path_list:
                if os.path.exists(apparmor_path):
                    security['type'] = 'AppArmor'
                    security['description'] = 'AppArmor: Enabled'
                    try:
                        profiles = read_file(['/sys/kernel/security/apparmor/profiles'])
                        if len(profiles) == 0:
                            return {}
                    except OSError:
                        pass
                    else:
                        summary = {}  # type: Dict[str, int]
                        for line in profiles.split('\n'):
                            item, mode = line.split(' ')
                            mode = mode.strip('()')
                            if mode in summary:
                                summary[mode] += 1
                            else:
                                summary[mode] = 0
                        summary_str = ','.join(['{} {}'.format(v, k) for k, v in summary.items()])
                        security = {**security, **summary}  # type: ignore
                        security['description'] += '({})'.format(summary_str)

                    return security
            return {}

        ret = {}
        if os.path.exists('/sys/kernel/security/lsm'):
            lsm = read_file(['/sys/kernel/security/lsm']).strip()
            if 'selinux' in lsm:
                ret = _fetch_selinux()
            elif 'apparmor' in lsm:
                ret = _fetch_apparmor()
            else:
                return {
                    'type': 'Unknown',
                    'description': 'Linux Security Module framework is active, but is not using SELinux or AppArmor'
                }

        if ret:
            return ret

        return {
            'type': 'None',
            'description': 'Linux Security Module framework is not available'
        }

    @property
    def selinux_enabled(self):
        return (self.kernel_security['type'] == 'SELinux') and \
               (self.kernel_security['description'] != 'SELinux: Disabled')

    @property
    def kernel_parameters(self):
        # type: () -> Dict[str, str]
        """Get kernel parameters required/used in Ceph clusters"""

        k_param = {}
        out, _, _ = call_throws(self.ctx, ['sysctl', '-a'], verbosity=CallVerbosity.SILENT)
        if out:
            param_list = out.split('\n')
            param_dict = {param.split(' = ')[0]: param.split(' = ')[-1] for param in param_list}

            # return only desired parameters
            if 'net.ipv4.ip_nonlocal_bind' in param_dict:
                k_param['net.ipv4.ip_nonlocal_bind'] = param_dict['net.ipv4.ip_nonlocal_bind']

        return k_param

    def dump(self):
        # type: () -> str
        """Return the attributes of this HostFacts object as json"""
        data = {
            k: getattr(self, k) for k in dir(self)
            if not k.startswith('_')
            and isinstance(getattr(self, k), (float, int, str, list, dict, tuple))
        }
        return json.dumps(data, indent=2, sort_keys=True)

##################################


def command_gather_facts(ctx: CephadmContext):
    """gather_facts is intended to provide host releated metadata to the caller"""
    host = HostFacts(ctx)
    print(host.dump())


##################################


class CephadmCache:
    task_types = ['disks', 'daemons', 'host', 'http_server']

    def __init__(self):
        self.started_epoch_secs = time.time()
        self.tasks = {
            'daemons': 'inactive',
            'disks': 'inactive',
            'host': 'inactive',
            'http_server': 'inactive',
        }
        self.errors = []
        self.disks = {}
        self.daemons = {}
        self.host = {}
        self.lock = RLock()

    @property
    def health(self):
        return {
            'started_epoch_secs': self.started_epoch_secs,
            'tasks': self.tasks,
            'errors': self.errors,
        }

    def to_json(self):
        return {
            'health': self.health,
            'host': self.host,
            'daemons': self.daemons,
            'disks': self.disks,
        }

    def update_health(self, task_type, task_status, error_msg=None):
        assert task_type in CephadmCache.task_types
        with self.lock:
            self.tasks[task_type] = task_status
            if error_msg:
                self.errors.append(error_msg)

    def update_task(self, task_type, content):
        assert task_type in CephadmCache.task_types
        assert isinstance(content, dict)
        with self.lock:
            current = getattr(self, task_type)
            for k in content:
                current[k] = content[k]

            setattr(self, task_type, current)


class CephadmHTTPServer(ThreadingMixIn, HTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    cephadm_cache: CephadmCache
    token: str


class CephadmDaemonHandler(BaseHTTPRequestHandler):
    server: CephadmHTTPServer
    api_version = 'v1'
    valid_routes = [
        f'/{api_version}/metadata',
        f'/{api_version}/metadata/health',
        f'/{api_version}/metadata/disks',
        f'/{api_version}/metadata/daemons',
        f'/{api_version}/metadata/host',
    ]

    class Decorators:
        @classmethod
        def authorize(cls, f):
            """Implement a basic token check.

            The token is installed at deployment time and must be provided to
            ensure we only respond to callers who know our token i.e. mgr
            """

            def wrapper(self, *args, **kwargs):
                auth = self.headers.get('Authorization', None)
                if auth != 'Bearer ' + self.server.token:
                    self.send_error(401)
                    return
                f(self, *args, **kwargs)

            return wrapper

    def _help_page(self):
        return """<!DOCTYPE html>
<html>
<head><title>cephadm metadata exporter</title></head>
<style>
body {{
  font-family: sans-serif;
  font-size: 0.8em;
}}
table {{
  border-width: 0px;
  border-spacing: 0px;
  margin-left:20px;
}}
tr:hover {{
  background: PowderBlue;
}}
td,th {{
  padding: 5px;
}}
</style>
<body>
    <h1>cephadm metadata exporter {api_version}</h1>
    <table>
      <thead>
        <tr><th>Endpoint</th><th>Methods</th><th>Response</th><th>Description</th></tr>
      </thead>
      <tr><td><a href='{api_version}/metadata'>{api_version}/metadata</a></td><td>GET</td><td>JSON</td><td>Return <b>all</b> metadata for the host</td></tr>
      <tr><td><a href='{api_version}/metadata/daemons'>{api_version}/metadata/daemons</a></td><td>GET</td><td>JSON</td><td>Return daemon and systemd states for ceph daemons (ls)</td></tr>
      <tr><td><a href='{api_version}/metadata/disks'>{api_version}/metadata/disks</a></td><td>GET</td><td>JSON</td><td>show disk inventory (ceph-volume)</td></tr>
      <tr><td><a href='{api_version}/metadata/health'>{api_version}/metadata/health</a></td><td>GET</td><td>JSON</td><td>Show current health of the exporter sub-tasks</td></tr>
      <tr><td><a href='{api_version}/metadata/host'>{api_version}/metadata/host</a></td><td>GET</td><td>JSON</td><td>Show host metadata (gather-facts)</td></tr>
    </table>
</body>
</html>""".format(api_version=CephadmDaemonHandler.api_version)

    def _fetch_root(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(self._help_page().encode('utf-8'))

    @Decorators.authorize
    def do_GET(self):
        """Handle *all* GET requests"""

        if self.path == '/':
            # provide a html response if someone hits the root url, to document the
            # available api endpoints
            return self._fetch_root()
        elif self.path in CephadmDaemonHandler.valid_routes:
            u = self.path.split('/')[-1]
            data = json.dumps({})
            status_code = 200

            tasks = self.server.cephadm_cache.health.get('tasks', {})
            assert tasks

            # We're using the http status code to help indicate thread health
            # - 200 (OK): request successful
            # - 204 (No Content): access to a cache relating to a dead thread
            # - 206 (Partial content): one or more theads are inactive
            # - 500 (Server Error): all threads inactive
            if u == 'metadata':
                data = json.dumps(self.server.cephadm_cache.to_json())
                if all([tasks[task_name] == 'inactive' for task_name in tasks if task_name != 'http_server']):
                    # All the subtasks are dead!
                    status_code = 500
                elif any([tasks[task_name] == 'inactive' for task_name in tasks if task_name != 'http_server']):
                    status_code = 206

            # Individual GETs against the a tasks endpoint will also return a 503 if the corresponding thread is inactive
            elif u == 'daemons':
                data = json.dumps(self.server.cephadm_cache.daemons)
                if tasks['daemons'] == 'inactive':
                    status_code = 204
            elif u == 'disks':
                data = json.dumps(self.server.cephadm_cache.disks)
                if tasks['disks'] == 'inactive':
                    status_code = 204
            elif u == 'host':
                data = json.dumps(self.server.cephadm_cache.host)
                if tasks['host'] == 'inactive':
                    status_code = 204

            # a GET against health will always return a 200, since the op is always successful
            elif u == 'health':
                data = json.dumps(self.server.cephadm_cache.health)

            self.send_response(status_code)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(data.encode('utf-8'))
        else:
            # Invalid GET URL
            bad_request_msg = 'Valid URLs are: {}'.format(', '.join(CephadmDaemonHandler.valid_routes))
            self.send_response(404, message=bad_request_msg)  # reason
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'message': bad_request_msg}).encode('utf-8'))

    def log_message(self, format, *args):
        rqst = ' '.join(str(a) for a in args)
        logger.info(f'client:{self.address_string()} [{self.log_date_time_string()}] {rqst}')


class CephadmDaemon():

    daemon_type = 'cephadm-exporter'
    default_port = 9443
    key_name = 'key'
    crt_name = 'crt'
    token_name = 'token'
    config_requirements = [
        key_name,
        crt_name,
        token_name,
    ]
    loop_delay = 1
    thread_check_interval = 5

    def __init__(self, ctx: CephadmContext, fsid, daemon_id=None, port=None):
        self.ctx = ctx
        self.fsid = fsid
        self.daemon_id = daemon_id
        if not port:
            self.port = CephadmDaemon.default_port
        else:
            self.port = port
        self.workers: List[Thread] = []
        self.http_server: CephadmHTTPServer
        self.stop = False
        self.cephadm_cache = CephadmCache()
        self.errors: List[str] = []
        self.token = read_file([os.path.join(self.daemon_path, CephadmDaemon.token_name)])

    @classmethod
    def validate_config(cls, config):
        reqs = ', '.join(CephadmDaemon.config_requirements)
        errors = []

        if not config or not all([k_name in config for k_name in CephadmDaemon.config_requirements]):
            raise Error(f'config must contain the following fields : {reqs}')

        if not all([isinstance(config[k_name], str) for k_name in CephadmDaemon.config_requirements]):
            errors.append(f'the following fields must be strings: {reqs}')

        crt = config[CephadmDaemon.crt_name]
        key = config[CephadmDaemon.key_name]
        token = config[CephadmDaemon.token_name]

        if not crt.startswith('-----BEGIN CERTIFICATE-----') or not crt.endswith('-----END CERTIFICATE-----\n'):
            errors.append('crt field is not a valid SSL certificate')
        if not key.startswith('-----BEGIN PRIVATE KEY-----') or not key.endswith('-----END PRIVATE KEY-----\n'):
            errors.append('key is not a valid SSL private key')
        if len(token) < 8:
            errors.append("'token' must be more than 8 characters long")

        if 'port' in config:
            try:
                p = int(config['port'])
                if p <= 1024:
                    raise ValueError
            except (TypeError, ValueError):
                errors.append('port must be an integer > 1024')

        if errors:
            raise Error('Parameter errors : {}'.format(', '.join(errors)))

    @property
    def port_active(self):
        return port_in_use(self.ctx, self.port)

    @property
    def can_run(self):
        # if port is in use
        if self.port_active:
            self.errors.append(f'TCP port {self.port} already in use, unable to bind')
        if not os.path.exists(os.path.join(self.daemon_path, CephadmDaemon.key_name)):
            self.errors.append(f"Key file '{CephadmDaemon.key_name}' is missing from {self.daemon_path}")
        if not os.path.exists(os.path.join(self.daemon_path, CephadmDaemon.crt_name)):
            self.errors.append(f"Certificate file '{CephadmDaemon.crt_name}' is missing from {self.daemon_path}")
        if self.token == 'Unknown':
            self.errors.append(f"Authentication token '{CephadmDaemon.token_name}' is missing from {self.daemon_path}")
        return len(self.errors) == 0

    @staticmethod
    def _unit_name(fsid, daemon_id):
        return '{}.service'.format(get_unit_name(fsid, CephadmDaemon.daemon_type, daemon_id))

    @property
    def unit_name(self):
        return CephadmDaemon._unit_name(self.fsid, self.daemon_id)

    @property
    def daemon_path(self):
        return os.path.join(
            self.ctx.data_dir,
            self.fsid,
            f'{self.daemon_type}.{self.daemon_id}'
        )

    @property
    def binary_path(self):
        path = os.path.realpath(__file__)
        assert os.path.isfile(path)
        return path

    def _handle_thread_exception(self, exc, thread_type):
        e_msg = f'{exc.__class__.__name__} exception: {str(exc)}'
        thread_info = getattr(self.cephadm_cache, thread_type)
        errors = thread_info.get('scrape_errors', [])
        errors.append(e_msg)
        logger.error(e_msg)
        logger.exception(exc)
        self.cephadm_cache.update_task(
            thread_type,
            {
                'scrape_errors': errors,
                'data': None,
            }
        )

    def _scrape_host_facts(self, refresh_interval=10):
        ctr = 0
        exception_encountered = False

        while True:

            if self.stop or exception_encountered:
                break

            if ctr >= refresh_interval:
                ctr = 0
                logger.debug('executing host-facts scrape')
                errors = []
                s_time = time.time()

                try:
                    facts = HostFacts(self.ctx)
                except Exception as e:
                    self._handle_thread_exception(e, 'host')
                    exception_encountered = True
                else:
                    elapsed = time.time() - s_time
                    try:
                        data = json.loads(facts.dump())
                    except json.decoder.JSONDecodeError:
                        errors.append('host-facts provided invalid JSON')
                        logger.warning(errors[-1])
                        data = {}
                    self.cephadm_cache.update_task(
                        'host',
                        {
                            'scrape_timestamp': s_time,
                            'scrape_duration_secs': elapsed,
                            'scrape_errors': errors,
                            'data': data,
                        }
                    )
                    logger.debug(f'completed host-facts scrape - {elapsed}s')

            time.sleep(CephadmDaemon.loop_delay)
            ctr += CephadmDaemon.loop_delay
        logger.info('host-facts thread stopped')

    def _scrape_ceph_volume(self, refresh_interval=15):
        # we're invoking the ceph_volume command, so we need to set the args that it
        # expects to use
        self.ctx.command = 'inventory --format=json'.split()
        self.ctx.fsid = self.fsid

        ctr = 0
        exception_encountered = False

        while True:
            if self.stop or exception_encountered:
                break

            if ctr >= refresh_interval:
                ctr = 0
                logger.debug('executing ceph-volume scrape')
                errors = []
                s_time = time.time()
                stream = io.StringIO()
                try:
                    with redirect_stdout(stream):
                        command_ceph_volume(self.ctx)
                except Exception as e:
                    self._handle_thread_exception(e, 'disks')
                    exception_encountered = True
                else:
                    elapsed = time.time() - s_time

                    # if the call to ceph-volume returns junk with the
                    # json, it won't parse
                    stdout = stream.getvalue()

                    data = []
                    if stdout:
                        try:
                            data = json.loads(stdout)
                        except json.decoder.JSONDecodeError:
                            errors.append('ceph-volume thread provided bad json data')
                            logger.warning(errors[-1])
                    else:
                        errors.append('ceph-volume did not return any data')
                        logger.warning(errors[-1])

                    self.cephadm_cache.update_task(
                        'disks',
                        {
                            'scrape_timestamp': s_time,
                            'scrape_duration_secs': elapsed,
                            'scrape_errors': errors,
                            'data': data,
                        }
                    )

                    logger.debug(f'completed ceph-volume scrape - {elapsed}s')
            time.sleep(CephadmDaemon.loop_delay)
            ctr += CephadmDaemon.loop_delay

        logger.info('ceph-volume thread stopped')

    def _scrape_list_daemons(self, refresh_interval=20):
        ctr = 0
        exception_encountered = False
        while True:
            if self.stop or exception_encountered:
                break

            if ctr >= refresh_interval:
                ctr = 0
                logger.debug('executing list-daemons scrape')
                errors = []
                s_time = time.time()

                try:
                    # list daemons should ideally be invoked with a fsid
                    data = list_daemons(self.ctx)
                except Exception as e:
                    self._handle_thread_exception(e, 'daemons')
                    exception_encountered = True
                else:
                    if not isinstance(data, list):
                        errors.append('list-daemons did not supply a list?')
                        logger.warning(errors[-1])
                        data = []
                    elapsed = time.time() - s_time
                    self.cephadm_cache.update_task(
                        'daemons',
                        {
                            'scrape_timestamp': s_time,
                            'scrape_duration_secs': elapsed,
                            'scrape_errors': errors,
                            'data': data,
                        }
                    )
                    logger.debug(f'completed list-daemons scrape - {elapsed}s')

            time.sleep(CephadmDaemon.loop_delay)
            ctr += CephadmDaemon.loop_delay
        logger.info('list-daemons thread stopped')

    def _create_thread(self, target, name, refresh_interval=None):
        if refresh_interval:
            t = Thread(target=target, args=(refresh_interval,))
        else:
            t = Thread(target=target)
        t.daemon = True
        t.name = name
        self.cephadm_cache.update_health(name, 'active')
        t.start()

        start_msg = f'Started {name} thread'
        if refresh_interval:
            logger.info(f'{start_msg}, with a refresh interval of {refresh_interval}s')
        else:
            logger.info(f'{start_msg}')
        return t

    def reload(self, *args):
        """reload -HUP received

        This is a placeholder function only, and serves to provide the hook that could
        be exploited later if the exporter evolves to incorporate a config file
        """
        logger.info('Reload request received - ignoring, no action needed')

    def shutdown(self, *args):
        logger.info('Shutdown request received')
        self.stop = True
        self.http_server.shutdown()

    def run(self):
        logger.info(f"cephadm exporter starting for FSID '{self.fsid}'")
        if not self.can_run:
            logger.error('Unable to start the exporter daemon')
            for e in self.errors:
                logger.error(e)
            return

        # register signal handlers for running under systemd control
        signal.signal(signal.SIGTERM, self.shutdown)
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGHUP, self.reload)
        logger.debug('Signal handlers attached')

        host_facts = self._create_thread(self._scrape_host_facts, 'host', 5)
        self.workers.append(host_facts)

        daemons = self._create_thread(self._scrape_list_daemons, 'daemons', 20)
        self.workers.append(daemons)

        disks = self._create_thread(self._scrape_ceph_volume, 'disks', 20)
        self.workers.append(disks)

        self.http_server = CephadmHTTPServer(('0.0.0.0', self.port), CephadmDaemonHandler)  # IPv4 only
        self.http_server.socket = ssl.wrap_socket(self.http_server.socket,
                                                  keyfile=os.path.join(self.daemon_path, CephadmDaemon.key_name),
                                                  certfile=os.path.join(self.daemon_path, CephadmDaemon.crt_name),
                                                  server_side=True)

        self.http_server.cephadm_cache = self.cephadm_cache
        self.http_server.token = self.token
        server_thread = self._create_thread(self.http_server.serve_forever, 'http_server')
        logger.info(f'https server listening on {self.http_server.server_address[0]}:{self.http_server.server_port}')

        ctr = 0
        while server_thread.is_alive():
            if self.stop:
                break

            if ctr >= CephadmDaemon.thread_check_interval:
                ctr = 0
                for worker in self.workers:
                    if self.cephadm_cache.tasks[worker.name] == 'inactive':
                        continue
                    if not worker.is_alive():
                        logger.warning(f'{worker.name} thread not running')
                        stop_time = datetime.datetime.now().strftime('%Y/%m/%d %H:%M:%S')
                        self.cephadm_cache.update_health(worker.name, 'inactive', f'{worker.name} stopped at {stop_time}')

            time.sleep(CephadmDaemon.loop_delay)
            ctr += CephadmDaemon.loop_delay

        logger.info('Main http server thread stopped')

    @property
    def unit_run(self):

        return """set -e
{py3} {bin_path} exporter --fsid {fsid} --id {daemon_id} --port {port} &""".format(
            py3=shutil.which('python3'),
            bin_path=self.binary_path,
            fsid=self.fsid,
            daemon_id=self.daemon_id,
            port=self.port
        )

    @property
    def unit_file(self):
        docker = isinstance(self.ctx.container_engine, Docker)
        return """#generated by cephadm
[Unit]
Description=cephadm exporter service for cluster {fsid}
After=network-online.target{docker_after}
Wants=network-online.target
{docker_requires}

PartOf=ceph-{fsid}.target
Before=ceph-{fsid}.target

[Service]
Type=forking
ExecStart=/bin/bash {daemon_path}/unit.run
ExecReload=/bin/kill -HUP $MAINPID
Restart=on-failure
RestartSec=10s

[Install]
WantedBy=ceph-{fsid}.target
""".format(fsid=self.fsid,
           daemon_path=self.daemon_path,
           # if docker, we depend on docker.service
           docker_after=' docker.service' if docker else '',
           docker_requires='Requires=docker.service\n' if docker else '')

    def deploy_daemon_unit(self, config=None):
        """deploy a specific unit file for cephadm

        The normal deploy_daemon_units doesn't apply for this
        daemon since it's not a container, so we just create a
        simple service definition and add it to the fsid's target
        """
        if not config:
            raise Error('Attempting to deploy cephadm daemon without a config')
        assert isinstance(config, dict)

        # Create the required config files in the daemons dir, with restricted permissions
        for filename in config:
            with open(os.open(os.path.join(self.daemon_path, filename), os.O_CREAT | os.O_WRONLY, mode=0o600), 'w') as f:
                f.write(config[filename])

        # When __file__ is <stdin> we're being invoked over remoto via the orchestrator, so
        # we pick up the file from where the orchestrator placed it - otherwise we'll
        # copy it to the binary location for this cluster
        if not __file__ == '<stdin>':
            shutil.copy(__file__,
                        self.binary_path)

        with open(os.path.join(self.daemon_path, 'unit.run'), 'w') as f:
            f.write(self.unit_run)

        with open(
                os.path.join(self.ctx.unit_dir,
                             f'{self.unit_name}.new'),
                'w'
        ) as f:
            f.write(self.unit_file)
            os.rename(
                os.path.join(self.ctx.unit_dir, f'{self.unit_name}.new'),
                os.path.join(self.ctx.unit_dir, self.unit_name))

        call_throws(self.ctx, ['systemctl', 'daemon-reload'])
        call(self.ctx, ['systemctl', 'stop', self.unit_name],
             verbosity=CallVerbosity.DEBUG)
        call(self.ctx, ['systemctl', 'reset-failed', self.unit_name],
             verbosity=CallVerbosity.DEBUG)
        call_throws(self.ctx, ['systemctl', 'enable', '--now', self.unit_name])

    @classmethod
    def uninstall(cls, ctx: CephadmContext, fsid, daemon_type, daemon_id):
        unit_name = CephadmDaemon._unit_name(fsid, daemon_id)
        unit_path = os.path.join(ctx.unit_dir, unit_name)
        unit_run = os.path.join(ctx.data_dir, fsid, f'{daemon_type}.{daemon_id}', 'unit.run')
        port = None
        try:
            with open(unit_run, 'r') as u:
                contents = u.read().strip(' &')
        except OSError:
            logger.warning(f'Unable to access the unit.run file @ {unit_run}')
            return

        port = None
        for line in contents.split('\n'):
            if '--port ' in line:
                try:
                    port = int(line.split('--port ')[-1])
                except ValueError:
                    logger.warning('Unexpected format in unit.run file: port is not numeric')
                    logger.warning('Unable to remove the systemd file and close the port')
                    return
                break

        if port:
            fw = Firewalld(ctx)
            try:
                fw.close_ports([port])
            except RuntimeError:
                logger.error(f'Unable to close port {port}')

        stdout, stderr, rc = call(ctx, ['rm', '-f', unit_path])
        if rc:
            logger.error(f'Unable to remove the systemd file @ {unit_path}')
        else:
            logger.info(f'removed systemd unit file @ {unit_path}')
            stdout, stderr, rc = call(ctx, ['systemctl', 'daemon-reload'])


def command_exporter(ctx: CephadmContext):
    exporter = CephadmDaemon(ctx, ctx.fsid, daemon_id=ctx.id, port=ctx.port)

    if ctx.fsid not in os.listdir(ctx.data_dir):
        raise Error(f"cluster fsid '{ctx.fsid}' not found in '{ctx.data_dir}'")

    exporter.run()

##################################


def systemd_target_state(target_name: str, subsystem: str = 'ceph') -> bool:
    # TODO: UNITTEST
    return os.path.exists(
        os.path.join(
            UNIT_DIR,
            f'{subsystem}.target.wants',
            target_name
        )
    )


@infer_fsid
def command_maintenance(ctx: CephadmContext):
    if not ctx.fsid:
        raise Error('must pass --fsid to specify cluster')

    target = f'ceph-{ctx.fsid}.target'

    if ctx.maintenance_action.lower() == 'enter':
        logger.info('Requested to place host into maintenance')
        if systemd_target_state(target):
            _out, _err, code = call(ctx,
                                    ['systemctl', 'disable', target],
                                    verbosity=CallVerbosity.DEBUG)
            if code:
                logger.error(f'Failed to disable the {target} target')
                return 'failed - to disable the target'
            else:
                # stopping a target waits by default
                _out, _err, code = call(ctx,
                                        ['systemctl', 'stop', target],
                                        verbosity=CallVerbosity.DEBUG)
                if code:
                    logger.error(f'Failed to stop the {target} target')
                    return 'failed - to disable the target'
                else:
                    return f'success - systemd target {target} disabled'

        else:
            return 'skipped - target already disabled'

    else:
        logger.info('Requested to exit maintenance state')
        # exit maintenance request
        if not systemd_target_state(target):
            _out, _err, code = call(ctx,
                                    ['systemctl', 'enable', target],
                                    verbosity=CallVerbosity.DEBUG)
            if code:
                logger.error(f'Failed to enable the {target} target')
                return 'failed - unable to enable the target'
            else:
                # starting a target waits by default
                _out, _err, code = call(ctx,
                                        ['systemctl', 'start', target],
                                        verbosity=CallVerbosity.DEBUG)
                if code:
                    logger.error(f'Failed to start the {target} target')
                    return 'failed - unable to start the target'
                else:
                    return f'success - systemd target {target} enabled and started'

##################################


def _get_parser():
    # type: () -> argparse.ArgumentParser
    parser = argparse.ArgumentParser(
        description='Bootstrap Ceph daemons with systemd and containers.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        '--image',
        help='container image. Can also be set via the "CEPHADM_IMAGE" '
        'env var')
    parser.add_argument(
        '--docker',
        action='store_true',
        help='use docker instead of podman')
    parser.add_argument(
        '--data-dir',
        default=DATA_DIR,
        help='base directory for daemon data')
    parser.add_argument(
        '--log-dir',
        default=LOG_DIR,
        help='base directory for daemon logs')
    parser.add_argument(
        '--logrotate-dir',
        default=LOGROTATE_DIR,
        help='location of logrotate configuration files')
    parser.add_argument(
        '--sysctl-dir',
        default=SYSCTL_DIR,
        help='location of sysctl configuration files')
    parser.add_argument(
        '--unit-dir',
        default=UNIT_DIR,
        help='base directory for systemd units')
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Show debug-level log messages')
    parser.add_argument(
        '--timeout',
        type=int,
        default=DEFAULT_TIMEOUT,
        help='timeout in seconds')
    parser.add_argument(
        '--retry',
        type=int,
        default=DEFAULT_RETRY,
        help='max number of retries')
    parser.add_argument(
        '--env', '-e',
        action='append',
        default=[],
        help='set environment variable')
    parser.add_argument(
        '--no-container-init',
        action='store_true',
        default=not CONTAINER_INIT,
        help='Do not run podman/docker with `--init`')

    subparsers = parser.add_subparsers(help='sub-command')

    parser_version = subparsers.add_parser(
        'version', help='get ceph version from container')
    parser_version.set_defaults(func=command_version)

    parser_pull = subparsers.add_parser(
        'pull', help='pull latest image version')
    parser_pull.set_defaults(func=command_pull)

    parser_inspect_image = subparsers.add_parser(
        'inspect-image', help='inspect local container image')
    parser_inspect_image.set_defaults(func=command_inspect_image)

    parser_ls = subparsers.add_parser(
        'ls', help='list daemon instances on this host')
    parser_ls.set_defaults(func=command_ls)
    parser_ls.add_argument(
        '--no-detail',
        action='store_true',
        help='Do not include daemon status')
    parser_ls.add_argument(
        '--legacy-dir',
        default='/',
        help='base directory for legacy daemon data')

    parser_list_networks = subparsers.add_parser(
        'list-networks', help='list IP networks')
    parser_list_networks.set_defaults(func=command_list_networks)

    parser_adopt = subparsers.add_parser(
        'adopt', help='adopt daemon deployed with a different tool')
    parser_adopt.set_defaults(func=command_adopt)
    parser_adopt.add_argument(
        '--name', '-n',
        required=True,
        help='daemon name (type.id)')
    parser_adopt.add_argument(
        '--style',
        required=True,
        help='deployment style (legacy, ...)')
    parser_adopt.add_argument(
        '--cluster',
        default='ceph',
        help='cluster name')
    parser_adopt.add_argument(
        '--legacy-dir',
        default='/',
        help='base directory for legacy daemon data')
    parser_adopt.add_argument(
        '--config-json',
        help='Additional configuration information in JSON format')
    parser_adopt.add_argument(
        '--skip-firewalld',
        action='store_true',
        help='Do not configure firewalld')
    parser_adopt.add_argument(
        '--skip-pull',
        action='store_true',
        help='do not pull the latest image before adopting')
    parser_adopt.add_argument(
        '--force-start',
        action='store_true',
        help='start newly adoped daemon, even if it was not running previously')
    parser_adopt.add_argument(
        '--container-init',
        action='store_true',
        default=CONTAINER_INIT,
        help=argparse.SUPPRESS)

    parser_rm_daemon = subparsers.add_parser(
        'rm-daemon', help='remove daemon instance')
    parser_rm_daemon.set_defaults(func=command_rm_daemon)
    parser_rm_daemon.add_argument(
        '--name', '-n',
        required=True,
        action=CustomValidation,
        help='daemon name (type.id)')
    parser_rm_daemon.add_argument(
        '--fsid',
        required=True,
        help='cluster FSID')
    parser_rm_daemon.add_argument(
        '--force',
        action='store_true',
        help='proceed, even though this may destroy valuable data')
    parser_rm_daemon.add_argument(
        '--force-delete-data',
        action='store_true',
        help='delete valuable daemon data instead of making a backup')

    parser_rm_cluster = subparsers.add_parser(
        'rm-cluster', help='remove all daemons for a cluster')
    parser_rm_cluster.set_defaults(func=command_rm_cluster)
    parser_rm_cluster.add_argument(
        '--fsid',
        required=True,
        help='cluster FSID')
    parser_rm_cluster.add_argument(
        '--force',
        action='store_true',
        help='proceed, even though this may destroy valuable data')
    parser_rm_cluster.add_argument(
        '--keep-logs',
        action='store_true',
        help='do not remove log files')
    parser_rm_cluster.add_argument(
        '--zap-osds',
        action='store_true',
        help='zap OSD devices for this cluster')

    parser_run = subparsers.add_parser(
        'run', help='run a ceph daemon, in a container, in the foreground')
    parser_run.set_defaults(func=command_run)
    parser_run.add_argument(
        '--name', '-n',
        required=True,
        help='daemon name (type.id)')
    parser_run.add_argument(
        '--fsid',
        required=True,
        help='cluster FSID')

    parser_shell = subparsers.add_parser(
        'shell', help='run an interactive shell inside a daemon container')
    parser_shell.set_defaults(func=command_shell)
    parser_shell.add_argument(
        '--fsid',
        help='cluster FSID')
    parser_shell.add_argument(
        '--name', '-n',
        help='daemon name (type.id)')
    parser_shell.add_argument(
        '--config', '-c',
        help='ceph.conf to pass through to the container')
    parser_shell.add_argument(
        '--keyring', '-k',
        help='ceph.keyring to pass through to the container')
    parser_shell.add_argument(
        '--mount', '-m',
        help=('mount a file or directory in the container. '
              'Support multiple mounts. '
              'ie: `--mount /foo /bar:/bar`. '
              'When no destination is passed, default is /mnt'),
        nargs='+')
    parser_shell.add_argument(
        '--env', '-e',
        action='append',
        default=[],
        help='set environment variable')
    parser_shell.add_argument(
        '--volume', '-v',
        action='append',
        default=[],
        help='set environment variable')
    parser_shell.add_argument(
        'command', nargs=argparse.REMAINDER,
        help='command (optional)')
    parser_shell.add_argument(
        '--no-hosts',
        action='store_true',
        help='dont pass /etc/hosts through to the container')

    parser_enter = subparsers.add_parser(
        'enter', help='run an interactive shell inside a running daemon container')
    parser_enter.set_defaults(func=command_enter)
    parser_enter.add_argument(
        '--fsid',
        help='cluster FSID')
    parser_enter.add_argument(
        '--name', '-n',
        required=True,
        help='daemon name (type.id)')
    parser_enter.add_argument(
        'command', nargs=argparse.REMAINDER,
        help='command')

    parser_ceph_volume = subparsers.add_parser(
        'ceph-volume', help='run ceph-volume inside a container')
    parser_ceph_volume.set_defaults(func=command_ceph_volume)
    parser_ceph_volume.add_argument(
        '--fsid',
        help='cluster FSID')
    parser_ceph_volume.add_argument(
        '--config-json',
        help='JSON file with config and (client.bootrap-osd) key')
    parser_ceph_volume.add_argument(
        '--config', '-c',
        help='ceph conf file')
    parser_ceph_volume.add_argument(
        '--keyring', '-k',
        help='ceph.keyring to pass through to the container')
    parser_ceph_volume.add_argument(
        'command', nargs=argparse.REMAINDER,
        help='command')

    parser_zap_osds = subparsers.add_parser(
        'zap-osds', help='zap all OSDs associated with a particular fsid')
    parser_zap_osds.set_defaults(func=command_zap_osds)
    parser_zap_osds.add_argument(
        '--fsid',
        required=True,
        help='cluster FSID')
    parser_zap_osds.add_argument(
        '--force',
        action='store_true',
        help='proceed, even though this may destroy valuable data')

    parser_unit = subparsers.add_parser(
        'unit', help="operate on the daemon's systemd unit")
    parser_unit.set_defaults(func=command_unit)
    parser_unit.add_argument(
        'command',
        help='systemd command (start, stop, restart, enable, disable, ...)')
    parser_unit.add_argument(
        '--fsid',
        help='cluster FSID')
    parser_unit.add_argument(
        '--name', '-n',
        required=True,
        help='daemon name (type.id)')

    parser_logs = subparsers.add_parser(
        'logs', help='print journald logs for a daemon container')
    parser_logs.set_defaults(func=command_logs)
    parser_logs.add_argument(
        '--fsid',
        help='cluster FSID')
    parser_logs.add_argument(
        '--name', '-n',
        required=True,
        help='daemon name (type.id)')
    parser_logs.add_argument(
        'command', nargs='*',
        help='additional journalctl args')

    parser_bootstrap = subparsers.add_parser(
        'bootstrap', help='bootstrap a cluster (mon + mgr daemons)')
    parser_bootstrap.set_defaults(func=command_bootstrap)
    parser_bootstrap.add_argument(
        '--config', '-c',
        help='ceph conf file to incorporate')
    parser_bootstrap.add_argument(
        '--mon-id',
        required=False,
        help='mon id (default: local hostname)')
    parser_bootstrap.add_argument(
        '--mon-addrv',
        help='mon IPs (e.g., [v2:localipaddr:3300,v1:localipaddr:6789])')
    parser_bootstrap.add_argument(
        '--mon-ip',
        help='mon IP')
    parser_bootstrap.add_argument(
        '--mgr-id',
        required=False,
        help='mgr id (default: randomly generated)')
    parser_bootstrap.add_argument(
        '--fsid',
        help='cluster FSID')
    parser_bootstrap.add_argument(
        '--output-dir',
        default='/etc/ceph',
        help='directory to write config, keyring, and pub key files')
    parser_bootstrap.add_argument(
        '--output-keyring',
        help='location to write keyring file with new cluster admin and mon keys')
    parser_bootstrap.add_argument(
        '--output-config',
        help='location to write conf file to connect to new cluster')
    parser_bootstrap.add_argument(
        '--output-pub-ssh-key',
        help="location to write the cluster's public SSH key")
    parser_bootstrap.add_argument(
        '--skip-admin-label',
        action='store_true',
        help='do not create admin label for ceph.conf and client.admin keyring distribution')
    parser_bootstrap.add_argument(
        '--skip-ssh',
        action='store_true',
        help='skip setup of ssh key on local host')
    parser_bootstrap.add_argument(
        '--initial-dashboard-user',
        default='admin',
        help='Initial user for the dashboard')
    parser_bootstrap.add_argument(
        '--initial-dashboard-password',
        help='Initial password for the initial dashboard user')
    parser_bootstrap.add_argument(
        '--ssl-dashboard-port',
        type=int,
        default=8443,
        help='Port number used to connect with dashboard using SSL')
    parser_bootstrap.add_argument(
        '--dashboard-key',
        type=argparse.FileType('r'),
        help='Dashboard key')
    parser_bootstrap.add_argument(
        '--dashboard-crt',
        type=argparse.FileType('r'),
        help='Dashboard certificate')

    parser_bootstrap.add_argument(
        '--ssh-config',
        type=argparse.FileType('r'),
        help='SSH config')
    parser_bootstrap.add_argument(
        '--ssh-private-key',
        type=argparse.FileType('r'),
        help='SSH private key')
    parser_bootstrap.add_argument(
        '--ssh-public-key',
        type=argparse.FileType('r'),
        help='SSH public key')
    parser_bootstrap.add_argument(
        '--ssh-user',
        default='root',
        help='set user for SSHing to cluster hosts, passwordless sudo will be needed for non-root users')

    parser_bootstrap.add_argument(
        '--skip-mon-network',
        action='store_true',
        help='set mon public_network based on bootstrap mon ip')
    parser_bootstrap.add_argument(
        '--skip-dashboard',
        action='store_true',
        help='do not enable the Ceph Dashboard')
    parser_bootstrap.add_argument(
        '--dashboard-password-noupdate',
        action='store_true',
        help='stop forced dashboard password change')
    parser_bootstrap.add_argument(
        '--no-minimize-config',
        action='store_true',
        help='do not assimilate and minimize the config file')
    parser_bootstrap.add_argument(
        '--skip-ping-check',
        action='store_true',
        help='do not verify that mon IP is pingable')
    parser_bootstrap.add_argument(
        '--skip-pull',
        action='store_true',
        help='do not pull the latest image before bootstrapping')
    parser_bootstrap.add_argument(
        '--skip-firewalld',
        action='store_true',
        help='Do not configure firewalld')
    parser_bootstrap.add_argument(
        '--allow-overwrite',
        action='store_true',
        help='allow overwrite of existing --output-* config/keyring/ssh files')
    parser_bootstrap.add_argument(
        '--allow-fqdn-hostname',
        action='store_true',
        help='allow hostname that is fully-qualified (contains ".")')
    parser_bootstrap.add_argument(
        '--allow-mismatched-release',
        action='store_true',
        help="allow bootstrap of ceph that doesn't match this version of cephadm")
    parser_bootstrap.add_argument(
        '--skip-prepare-host',
        action='store_true',
        help='Do not prepare host')
    parser_bootstrap.add_argument(
        '--orphan-initial-daemons',
        action='store_true',
        help='Set mon and mgr service to `unmanaged`, Do not create the crash service')
    parser_bootstrap.add_argument(
        '--skip-monitoring-stack',
        action='store_true',
        help='Do not automatically provision monitoring stack (prometheus, grafana, alertmanager, node-exporter)')
    parser_bootstrap.add_argument(
        '--apply-spec',
        help='Apply cluster spec after bootstrap (copy ssh key, add hosts and apply services)')

    parser_bootstrap.add_argument(
        '--shared_ceph_folder',
        metavar='CEPH_SOURCE_FOLDER',
        help='Development mode. Several folders in containers are volumes mapped to different sub-folders in the ceph source folder')

    parser_bootstrap.add_argument(
        '--registry-url',
        help='url for custom registry')
    parser_bootstrap.add_argument(
        '--registry-username',
        help='username for custom registry')
    parser_bootstrap.add_argument(
        '--registry-password',
        help='password for custom registry')
    parser_bootstrap.add_argument(
        '--registry-json',
        help='json file with custom registry login info (URL, Username, Password)')
    parser_bootstrap.add_argument(
        '--container-init',
        action='store_true',
        default=CONTAINER_INIT,
        help=argparse.SUPPRESS)
    parser_bootstrap.add_argument(
        '--with-exporter',
        action='store_true',
        help='Automatically deploy cephadm metadata exporter to each node')
    parser_bootstrap.add_argument(
        '--exporter-config',
        action=CustomValidation,
        help=f'Exporter configuration information in JSON format (providing: {", ".join(CephadmDaemon.config_requirements)}, port information)')
    parser_bootstrap.add_argument(
        '--cluster-network',
        help='subnet to use for cluster replication, recovery and heartbeats (in CIDR notation network/mask)')
    parser_bootstrap.add_argument(
        '--single-host-defaults',
        action='store_true',
        help='adjust configuration defaults to suit a single-host cluster')

    parser_deploy = subparsers.add_parser(
        'deploy', help='deploy a daemon')
    parser_deploy.set_defaults(func=command_deploy)
    parser_deploy.add_argument(
        '--name',
        required=True,
        action=CustomValidation,
        help='daemon name (type.id)')
    parser_deploy.add_argument(
        '--fsid',
        required=True,
        help='cluster FSID')
    parser_deploy.add_argument(
        '--config', '-c',
        help='config file for new daemon')
    parser_deploy.add_argument(
        '--config-json',
        help='Additional configuration information in JSON format')
    parser_deploy.add_argument(
        '--keyring',
        help='keyring for new daemon')
    parser_deploy.add_argument(
        '--key',
        help='key for new daemon')
    parser_deploy.add_argument(
        '--osd-fsid',
        help='OSD uuid, if creating an OSD container')
    parser_deploy.add_argument(
        '--skip-firewalld',
        action='store_true',
        help='Do not configure firewalld')
    parser_deploy.add_argument(
        '--tcp-ports',
        help='List of tcp ports to open in the host firewall')
    parser_deploy.add_argument(
        '--reconfig',
        action='store_true',
        help='Reconfigure a previously deployed daemon')
    parser_deploy.add_argument(
        '--allow-ptrace',
        action='store_true',
        help='Allow SYS_PTRACE on daemon container')
    parser_deploy.add_argument(
        '--container-init',
        action='store_true',
        default=CONTAINER_INIT,
        help=argparse.SUPPRESS)
    parser_deploy.add_argument(
        '--memory-request',
        help='Container memory request/target'
    )
    parser_deploy.add_argument(
        '--memory-limit',
        help='Container memory hard limit'
    )
    parser_deploy.add_argument(
        '--meta-json',
        help='JSON dict of additional metadata'
    )

    parser_check_host = subparsers.add_parser(
        'check-host', help='check host configuration')
    parser_check_host.set_defaults(func=command_check_host)
    parser_check_host.add_argument(
        '--expect-hostname',
        help='Check that hostname matches an expected value')

    parser_prepare_host = subparsers.add_parser(
        'prepare-host', help='prepare a host for cephadm use')
    parser_prepare_host.set_defaults(func=command_prepare_host)
    parser_prepare_host.add_argument(
        '--expect-hostname',
        help='Set hostname')

    parser_add_repo = subparsers.add_parser(
        'add-repo', help='configure package repository')
    parser_add_repo.set_defaults(func=command_add_repo)
    parser_add_repo.add_argument(
        '--release',
        help='use latest version of a named release (e.g., {})'.format(LATEST_STABLE_RELEASE))
    parser_add_repo.add_argument(
        '--version',
        help='use specific upstream version (x.y.z)')
    parser_add_repo.add_argument(
        '--dev',
        help='use specified bleeding edge build from git branch or tag')
    parser_add_repo.add_argument(
        '--dev-commit',
        help='use specified bleeding edge build from git commit')
    parser_add_repo.add_argument(
        '--gpg-url',
        help='specify alternative GPG key location')
    parser_add_repo.add_argument(
        '--repo-url',
        default='https://download.ceph.com',
        help='specify alternative repo location')
    # TODO: proxy?

    parser_rm_repo = subparsers.add_parser(
        'rm-repo', help='remove package repository configuration')
    parser_rm_repo.set_defaults(func=command_rm_repo)

    parser_install = subparsers.add_parser(
        'install', help='install ceph package(s)')
    parser_install.set_defaults(func=command_install)
    parser_install.add_argument(
        'packages', nargs='*',
        default=['cephadm'],
        help='packages')

    parser_registry_login = subparsers.add_parser(
        'registry-login', help='log host into authenticated registry')
    parser_registry_login.set_defaults(func=command_registry_login)
    parser_registry_login.add_argument(
        '--registry-url',
        help='url for custom registry')
    parser_registry_login.add_argument(
        '--registry-username',
        help='username for custom registry')
    parser_registry_login.add_argument(
        '--registry-password',
        help='password for custom registry')
    parser_registry_login.add_argument(
        '--registry-json',
        help='json file with custom registry login info (URL, Username, Password)')
    parser_registry_login.add_argument(
        '--fsid',
        help='cluster FSID')

    parser_gather_facts = subparsers.add_parser(
        'gather-facts', help='gather and return host related information (JSON format)')
    parser_gather_facts.set_defaults(func=command_gather_facts)

    parser_exporter = subparsers.add_parser(
        'exporter', help='Start cephadm in exporter mode (web service), providing host/daemon/disk metadata')
    parser_exporter.add_argument(
        '--fsid',
        required=True,
        type=str,
        help='fsid of the cephadm exporter to run against')
    parser_exporter.add_argument(
        '--port',
        type=int,
        default=int(CephadmDaemon.default_port),
        help='port number for the cephadm exporter service')
    parser_exporter.add_argument(
        '--id',
        type=str,
        default=get_hostname().split('.')[0],
        help='daemon identifer for the exporter')
    parser_exporter.set_defaults(func=command_exporter)

    parser_maintenance = subparsers.add_parser(
        'host-maintenance', help='Manage the maintenance state of a host')
    parser_maintenance.add_argument(
        '--fsid',
        help='cluster FSID')
    parser_maintenance.add_argument(
        'maintenance_action',
        type=str,
        choices=['enter', 'exit'],
        help='Maintenance action - enter maintenance, or exit maintenance')
    parser_maintenance.set_defaults(func=command_maintenance)

    return parser


def _parse_args(av):
    parser = _get_parser()

    args = parser.parse_args(av)
    if 'command' in args and args.command and args.command[0] == '--':
        args.command.pop(0)

    # workaround argparse to deprecate the subparser `--container-init` flag
    # container_init and no_container_init must always be mutually exclusive
    container_init_args = ('--container-init', '--no-container-init')
    if set(container_init_args).issubset(av):
        parser.error('argument %s: not allowed with argument %s' % (container_init_args))
    elif '--container-init' in av:
        args.no_container_init = not args.container_init
    else:
        args.container_init = not args.no_container_init
    assert args.container_init is not args.no_container_init

    return args


def cephadm_init_ctx(args: List[str]) -> CephadmContext:
    ctx = CephadmContext()
    ctx.set_args(_parse_args(args))
    return ctx


def cephadm_init(args: List[str]) -> CephadmContext:
    global logger
    ctx = cephadm_init_ctx(args)

    # Logger configuration
    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR)
    dictConfig(logging_config)
    logger = logging.getLogger()

    if not os.path.exists(ctx.logrotate_dir + '/cephadm'):
        with open(ctx.logrotate_dir + '/cephadm', 'w') as f:
            f.write("""# created by cephadm
/var/log/ceph/cephadm.log {
    rotate 7
    daily
    compress
    missingok
    notifempty
}
""")

    if ctx.verbose:
        for handler in logger.handlers:
            if handler.name == 'console':
                handler.setLevel(logging.DEBUG)

    return ctx


def main():

    # root?
    if os.geteuid() != 0:
        sys.stderr.write('ERROR: cephadm should be run as root\n')
        sys.exit(1)

    av: List[str] = []
    av = sys.argv[1:]

    ctx = cephadm_init(av)
    if not ctx.has_function():
        sys.stderr.write('No command specified; pass -h or --help for usage\n')
        sys.exit(1)

    try:
        # podman or docker?
        ctx.container_engine = find_container_engine(ctx)
        if ctx.func not in \
                [command_check_host, command_prepare_host, command_add_repo]:
            check_container_engine(ctx)
        # command handler
        r = ctx.func(ctx)
    except Error as e:
        if ctx.verbose:
            raise
        logger.error('ERROR: %s' % e)
        sys.exit(1)
    if not r:
        r = 0
    sys.exit(r)


if __name__ == '__main__':
    main()
