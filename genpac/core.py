# -*- coding: utf-8 -*-
from __future__ import (unicode_literals, absolute_import,
                        division, print_function)
import os
import sys
import argparse
import re
import base64
import time
from datetime import datetime, timedelta
from urllib2 import build_opener
import copy
from pprint import pprint  # noqa: F401

from . import __version__
from .pysocks.socks import PROXY_TYPES as _proxy_types
from .pysocks.sockshandler import SocksiPyHandler
from .config import Config
from .deprecated import check_deprecated_args, check_deprecated_config
from .util import exit_error, exit_success
from .util import abspath, open_file, get_resource_data
from .util import conv_bool, conv_list, conv_lower, conv_path


_GFWLIST_URL = \
    'https://raw.githubusercontent.com/gfwlist/gfwlist/master/gfwlist.txt'


class Namespace(argparse.Namespace):
    def __init__(self, **kwargs):
        self.update(**kwargs)

    def update(self, **kwargs):
        keys = [k.strip().replace('-', '_') for k in kwargs.keys()]
        self.__dict__.update(**dict(zip(keys, kwargs.values())))

    def dict(self):
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d):
        return cls(**d)


class GenPAC(object):
    # 格式化器列表
    _formaters = {}

    def __init__(self):
        super(GenPAC, self).__init__()
        self.default_opts = {}
        self.jobs = []

    @classmethod
    def add_formater(cls, name, fmt_cls, **options):
        # TODO: 检查cls是否合法
        fmt_cls._name = name
        cls._formaters[name] = {'cls': fmt_cls,
                                'options': options}

    def walk_formaters(self, attr, *args, **kargs):
        for fmter in self._formaters.itervalues():
            getattr(fmter['cls'], attr)(*args, **kargs)

    def build_args_parser(self):
        # 如果某选项同时可以在配置文件和命令行中设定，则必须使default=None
        # 以避免命令行中即使没指定该参数，也会覆盖配置文件中的值
        # 原因见parse_config() -> update(name, key, default=None)
        parser = argparse.ArgumentParser(
            prog='genpac',
            formatter_class=argparse.RawTextHelpFormatter,
            description='获取gfwlist生成多种格式的翻墙工具配置文件, '
                        '支持自定义规则',
            epilog=get_resource_data('res/rule-syntax.txt'),
            argument_default=argparse.SUPPRESS,
            add_help=False)
        parser.add_argument(
            '-v', '--version', action='version',
            version='%(prog)s {}'.format(__version__),
            help='版本信息')
        parser.add_argument(
            '-h', '--help', action='help',
            help='帮助信息')
        parser.add_argument(
            '--init', nargs='?', const=True, default=False, metavar='PATH',
            help='初始化配置和用户规则文件')

        group = parser.add_argument_group(
            title='通用参数')
        group.add_argument(
            '--format', choices=self._formaters.keys(),
            help='生成格式, 只有指定了格式, 相应格式的参数才作用')
        group.add_argument(
            '--gfwlist-url', metavar='URL',
            help='gfwlist网址，无此参数或URL为空则使用默认地址, URL为-则不在线获取')
        group.add_argument(
            '--gfwlist-proxy', metavar='PROXY',
            help='获取gfwlist时的代理, 如果可正常访问gfwlist地址, 则无必要使用该选项\n'
                 '格式为 "代理类型 [用户名:密码]@地址:端口" 其中用户名和密码可选, 如:\n'
                 '  SOCKS5 127.0.0.1:8080\n'
                 '  SOCKS5 username:password@127.0.0.1:8080\n')
        group.add_argument(
            '--gfwlist-local', metavar='FILE',
            help='本地gfwlist文件地址, 当在线地址获取失败时使用')
        group.add_argument(
            '--gfwlist-update-local', action='store_true',
            help='当在线gfwlist成功获取且--gfwlist-local参数存在时, '
                 '更新gfwlist-local内容')
        group.add_argument(
            '--gfwlist-disabled', action='store_true',
            help='禁用在线获取gfwlist')
        group.add_argument(
            '--user-rule', action='append', metavar='RULE',
            help='自定义规则, 允许重复使用或在单个参数中使用`,`分割多个规则，如:\n'
                 '  --user-rule="@@sina.com" --user-rule="||youtube.com"\n'
                 '  --user-rule="@@sina.com,||youtube.com"')
        group.add_argument(
            '--user-rule-from', action='append', metavar='FILE',
            help='从文件中读取自定义规则, 使用方法如--user-rule')
        group.add_argument(
            '-o', '--output', metavar='FILE',
            help='输出到文件, 无此参数或FILE为-, 则输出到stdout')
        group.add_argument(
            '-c', '--config-from', default=None, metavar='FILE',
            help='从文件中读取配置信息')

        return parser

    def read_config(self, config_file):
        if not config_file:
            return [{}], {}
        try:
            cfg = Config()
            cfg.read(config_file)
            return (
                cfg.sections('job', sub_section_key='format') or [{}],
                cfg.section('config') or {})
        except:
            exit_error('配置文件读取失败')

    def update_opt(self, args, cfgs, key,
                   default=None, conv=None, dest=None, **kwargs):
        conv = conv or []
        if not isinstance(conv, list):
            conv = [conv]

        if dest is None:
            dest = key.replace('-', '_').lower()

        if hasattr(args, dest):
            v = getattr(args, dest)
        else:
            replaced = kwargs.get('replaced')
            if key in cfgs:
                v = cfgs[key]
            elif replaced and replaced in cfgs:
                v = cfgs[replaced]
            else:
                v = default

        if isinstance(v, basestring):
            v = v.strip(' \'\t"')

        for c in conv:
            v = c(v)

        return dest, v

    def parse_options(self):
        # 检查弃用参数 警告
        check_deprecated_args()

        parser = self.build_args_parser()
        self.walk_formaters('arguments', parser)
        args = parser.parse_args()

        pprint(args)

        if args.init:
            self.init(args.init)

        cfgs, self.default_opts = self.read_config(args.config_from)

        opts = {}
        opts['format'] = {'conv': conv_lower}

        opts['gfwlist-url'] = {'default': _GFWLIST_URL}
        opts['gfwlist-proxy'] = {}
        opts['gfwlist-local'] = {'conv': conv_path}
        opts['gfwlist-disabled'] = {'conv': conv_bool}
        opts['gfwlist-update-local'] = {'conv': conv_bool}
        opts['output'] = {}

        opts['user-rule'] = {'conv': conv_list}
        opts['user-rule-from'] = {'conv': [conv_list, conv_path]}

        self.walk_formaters('config', opts)

        self.jobs = []

        for c in cfgs:
            cfg = self.default_opts.copy()
            cfg.update(c)
            check_deprecated_config(cfg.keys())
            job = Namespace.from_dict(
                dict([(k, v) for k, v in cfg.iteritems() if k in opts]))
            for k, v in opts.iteritems():
                dest, value = self.update_opt(args, cfg, k, **v)
                job.update(**{dest: value})
            self.jobs.append(job)

    def init(self, dest):
        try:
            path = abspath(dest)
            if not os.path.isdir(path):
                os.makedirs(path)
            config_dst = os.path.join(path, 'config.ini')
            user_rule_dst = os.path.join(path, 'user-rules.txt')
            if os.path.exists(config_dst) or os.path.exists(user_rule_dst):
                ans = raw_input('文件已存在, 是否覆盖?[y|n]: '.encode('utf-8'))
                if ans.lower() != 'y':
                    raise Exception('文件已存在')
            with open_file(config_dst, 'w') as fp:
                fp.write(get_resource_data('res/tpl-config.ini'))
            with open_file(user_rule_dst, 'w') as fp:
                fp.write(get_resource_data('res/tpl-user-rules.txt'))
        except Exception as e:
            exit_error('初始化失败: {}'.format(e))
        exit_success('已成功初始化')

    def walk_jobs(self):
        for job in self.jobs:
            yield job

    def run(self):
        self.parse_options()

        for job in self.walk_jobs():
            self.generate(job)

    def generate(self, job):
        if not job.format:
            exit_error('生成的格式不能为空, 请检查参数--format或配置format.')
        if job.format not in self._formaters:
            exit_error('发现不支持的生成格式: {}, 可选格式为: {}'.format(
                job.format, ', '.join(self._formaters.keys())))
        # print('-')
        # pprint(job)
        generator = Generator(job, self._formaters[job.format]['cls'])
        generator.generate()


class Generator(object):
    # 在线获取gfwlist的结果
    _gfwlists = {}

    def __init__(self, options, formater_cls):
        super(Generator, self).__init__()
        self.options = copy.copy(options)
        self.formater = formater_cls(options=self.options)

    def generate(self):
        if not self.formater.pre_generate():
            return

        gfwlist_rules, gfwlist_from, gfwlist_modified = self.fetch_gfwlist()
        user_rules = self.fetch_user_rules()

        modified, generated = self.std_datetime(gfwlist_modified)

        replacements = {'__VERSION__': __version__,
                        '__GENERATED__': generated,
                        '__MODIFIED__': modified,
                        '__GFWLIST_FROM__': gfwlist_from}

        content = self.formater.generate(
            gfwlist_rules, user_rules, replacements)

        output = self.options.output
        try:
            if not output or output == '-':
                sys.stdout.write(content)
            else:
                with open_file(output, 'w') as fp:
                    fp.write(content)
        except Exception:
            exit_error('写入输出文件`{}`失败'.format(output))

        self.formater.post_generate()

    def init_opener(self):
        if not self.options.gfwlist_proxy:
            return build_opener()
        _proxy_types['SOCKS'] = _proxy_types['SOCKS4']
        _proxy_types['PROXY'] = _proxy_types['HTTP']
        try:
            # format: PROXY|SOCKS|SOCKS4|SOCKS5 [USR:PWD]@HOST:PORT
            matches = re.match(
                r'(PROXY|SOCKS|SOCKS4|SOCKS5) (?:(.+):(.+)@)?(.+):(\d+)',
                self.options.gfwlist_proxy,
                re.IGNORECASE)
            type_, usr, pwd, host, port = matches.groups()
            type_ = _proxy_types[type_.upper()]
            return build_opener(
                SocksiPyHandler(type_, host, int(port),
                                username=usr, password=pwd))
        except:
            exit_error('解析获取gfwlist的代理`{}`失败'.format(
                self.options.gfwlist_proxy))

    def fetch_gfwlist_online(self):
        # 使用类变量缓存gfwlist在线获取的内容
        url = self.options.gfwlist_url
        if url in self.__class__._gfwlists:
            return self.__class__._gfwlists[url]
        opener = self.init_opener()
        res = opener.open(url)
        content = res.read()
        if content:
            self.__class__._gfwlists[url] = content
        return content

    def fetch_gfwlist(self):
        if self.options.gfwlist_disabled:
            return [], '-', '-'

        content = ''
        gfwlist_from = '-'
        gfwlist_modified = '-'
        try:
            content = self.fetch_gfwlist_online()
        except:
            try:
                with open_file(self.options.gfwlist_local) as fp:
                    content = fp.read()
                gfwlist_from = 'local[{}]'.format(self.options.gfwlist_local)
            except:
                pass
        else:
            gfwlist_from = 'online[{}]'.format(self.options.gfwlist_url)
            if self.options.gfwlist_local \
                    and self.options.gfwlist_update_local:
                with open_file(self.options.gfwlist_local, 'w') as fp:
                    fp.write(content)

        if not content:
            if self.options.gfwlist_url != '-' or self.options.gfwlist_local:
                exit_error('获取gfwlist失败. online: {} local: {}'.format(
                    self.options.gfwlist_url, self.options.gfwlist_local))
            else:
                gfwlist_from = '-'

        try:
            content = '! {}'.format(base64.decodestring(content))
        except:
            exit_error('解码gfwlist失败.')

        content = content.splitlines()
        for line in content:
            if line.startswith('! Last Modified:'):
                gfwlist_modified = line.split(':', 1)[1].strip()
                break

        return content, gfwlist_from, gfwlist_modified

    def fetch_user_rules(self):
        rules = []
        rules.extend(self.options.user_rule)
        for f in self.options.user_rule_from:
            try:
                with open_file(f) as fp:
                    file_rules = fp.read().splitlines()
                    rules.extend(file_rules)
            except:
                exit_error('读取自定义规则文件`{}`失败'.format(f))
        return rules

    def std_datetime(self, modified_datestr):
        def to_local(date_str):
            naive_date_str, _, offset_str = date_str.rpartition(' ')
            naive_dt = datetime.strptime(
                naive_date_str, '%a, %d %b %Y %H:%M:%S')
            offset = int(offset_str[-4:-2]) * 60 + int(offset_str[-2:])
            if offset_str[0] == "-":
                offset = -offset
            utc_date = naive_dt - timedelta(minutes=offset)

            ts = time.time()
            offset = datetime.fromtimestamp(ts) - \
                datetime.utcfromtimestamp(ts)
            return utc_date + offset

        try:
            modified = to_local(modified_datestr)
            return (modified.strftime('%Y-%m-%d %H:%M:%S'),
                    datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        except:
            return (modified_datestr,
                    time.strftime('%a, %d %b %Y %H:%M:%S %z',
                                  time.localtime()))


# decorator: 添加格式化器
def formater(name, **options):
    def decorator(fmt_cls):
        GenPAC.add_formater(name, fmt_cls, **options)
        return fmt_cls
    return decorator
