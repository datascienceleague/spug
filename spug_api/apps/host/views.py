# Copyright: (c) OpenSpug Organization. https://github.com/openspug/spug
# Copyright: (c) <spug.dev@gmail.com>
# Released under the AGPL-3.0 License.
from django.views.generic import View
from django.db.models import F
from django.http.response import HttpResponseBadRequest
from libs import json_response, JsonParser, Argument, AttrDict
from apps.setting.utils import AppSetting
from apps.account.utils import get_host_perms
from apps.host.models import Host, Group
from apps.host.utils import batch_sync_host
from apps.app.models import Deploy
from apps.schedule.models import Task
from apps.monitor.models import Detection
from libs.ssh import SSH, AuthenticationException
from paramiko.ssh_exception import BadAuthenticationType
from openpyxl import load_workbook
from threading import Thread
import uuid


class HostView(View):
    def get(self, request):
        hosts = Host.objects.select_related('hostextend')
        if not request.user.is_supper:
            hosts = hosts.filter(id__in=get_host_perms(request.user))
        hosts = {x.id: x.to_view() for x in hosts}
        for rel in Group.hosts.through.objects.filter(host_id__in=hosts.keys()):
            hosts[rel.host_id]['group_ids'].append(rel.group_id)
        return json_response(list(hosts.values()))

    def post(self, request):
        form, error = JsonParser(
            Argument('id', type=int, required=False),
            Argument('group_ids', type=list, filter=lambda x: len(x), help='请选择主机分组'),
            Argument('name', help='请输主机名称'),
            Argument('username', handler=str.strip, help='请输入登录用户名'),
            Argument('hostname', handler=str.strip, help='请输入主机名或IP'),
            Argument('port', type=int, help='请输入SSH端口'),
            Argument('pkey', required=False),
            Argument('desc', required=False),
            Argument('password', required=False),
        ).parse(request.body)
        if error is None:
            if not valid_ssh(form.hostname, form.port, form.username, password=form.pop('password'), pkey=form.pkey):
                return json_response('auth fail')

            group_ids = form.pop('group_ids')
            other = Host.objects.filter(name=form.name).first()
            if other and (not form.id or other.id != form.id):
                return json_response(error=f'已存在的主机名称【{form.name}】')
            if form.id:
                Host.objects.filter(pk=form.id).update(is_verified=True, **form)
                host = Host.objects.get(pk=form.id)
            else:
                host = Host.objects.create(created_by=request.user, is_verified=True, **form)
            host.groups.set(group_ids)
            response = host.to_view()
            response['group_ids'] = group_ids
            return json_response(response)
        return json_response(error=error)

    def patch(self, request):
        form, error = JsonParser(
            Argument('host_ids', type=list, filter=lambda x: len(x), help='请选择主机'),
            Argument('s_group_id', type=int, help='参数错误'),
            Argument('t_group_id', type=int, help='参数错误'),
            Argument('is_copy', type=bool, help='参数错误'),
        ).parse(request.body)
        if error is None:
            s_group = Group.objects.get(pk=form.s_group_id)
            t_group = Group.objects.get(pk=form.t_group_id)
            t_group.hosts.add(*form.host_ids)
            if not form.is_copy:
                s_group.hosts.remove(*form.host_ids)
        return json_response(error=error)

    def delete(self, request):
        form, error = JsonParser(
            Argument('id', type=int, help='请指定操作对象')
        ).parse(request.GET)
        if error is None:
            deploy = Deploy.objects.filter(host_ids__regex=fr'[^0-9]{form.id}[^0-9]').annotate(
                app_name=F('app__name'),
                env_name=F('env__name')
            ).first()
            if deploy:
                return json_response(error=f'应用【{deploy.app_name}】在【{deploy.env_name}】的发布配置关联了该主机，请解除关联后再尝试删除该主机')
            task = Task.objects.filter(targets__regex=fr'[^0-9]{form.id}[^0-9]').first()
            if task:
                return json_response(error=f'任务计划中的任务【{task.name}】关联了该主机，请解除关联后再尝试删除该主机')
            detection = Detection.objects.filter(type__in=('3', '4'), targets__regex=fr'[^0-9]{form.id}[^0-9]').first()
            if detection:
                return json_response(error=f'监控中心的任务【{detection.name}】关联了该主机，请解除关联后再尝试删除该主机')
            Host.objects.filter(pk=form.id).delete()
        return json_response(error=error)


def post_import(request):
    group_id = request.POST.get('group_id')
    file = request.FILES['file']
    ws = load_workbook(file, read_only=True)['Sheet1']
    summary = {'invalid': [], 'skip': [], 'repeat': [], 'success': []}
    for i, row in enumerate(ws.rows):
        if i == 0:  # 第1行是表头 略过
            continue
        if not all([row[x].value for x in range(5)]):
            summary['invalid'].append(i)
            continue
        data = AttrDict(
            name=row[0].value,
            hostname=row[1].value,
            port=row[2].value,
            username=row[3].value,
            desc=row[4].value
        )
        if Host.objects.filter(hostname=data.hostname, port=data.port, username=data.username).exists():
            summary['skip'].append(i)
            continue
        if Host.objects.filter(name=data.name).exists():
            summary['repeat'].append(i)
            continue
        host = Host.objects.create(created_by=request.user, **data)
        host.groups.add(group_id)
        summary['success'].append(i)
    return json_response(summary)


def valid_ssh(hostname, port, username, password=None, pkey=None):
    private_key, public_key = AppSetting.get_ssh_key()
    try:
        if pkey:
            private_key = pkey
        elif password:
            ssh = SSH(hostname, port, username, password=str(password))
            ssh.add_public_key(public_key)

        ssh = SSH(hostname, port, username, private_key)
        ssh.ping()
        return True
    except BadAuthenticationType:
        raise TypeError('该主机不支持密钥认证，请参考官方文档，错误代码：E01')
    except AuthenticationException:
        if password:
            raise ValueError('密钥认证失败，请参考官方文档，错误代码：E02')


def post_parse(request):
    file = request.FILES['file']
    if file:
        data = file.read()
        return json_response(data.decode())
    else:
        return HttpResponseBadRequest()


def batch_valid(request):
    form, error = JsonParser(
        Argument('password', required=False),
        Argument('range', filter=lambda x: x in ('1', '2'), help='参数错误')
    ).parse(request.body)
    if error is None:
        if form.range == '1':  # all hosts
            hosts = Host.objects.all()
        else:
            hosts = Host.objects.filter(is_verified=False).all()
        token = uuid.uuid4().hex
        Thread(target=batch_sync_host, args=(token, hosts, form.password)).start()
        return json_response({'token': token, 'hosts': {x.id: {'name': x.name} for x in hosts}})
    return json_response(error=error)
