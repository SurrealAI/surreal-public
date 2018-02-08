"""
Simple python client. The official client is poorly documented and crashes
on import. Except for `watch`, other info can simply be parsed from stdout.

~/.surreal.yml
-
"""
import sys
import time
import subprocess as pc
import shlex
import functools
import os
import re
import os.path as path
from surreal.kube.yaml_util import YamlList, JinjaYaml, file_content
from surreal.kube.git_snapshot import push_snapshot
from surreal.utils.ezdict import EzDict
import surreal.utils as U


def run_process(cmd):
    # if isinstance(cmd, str):  # useful for shell=False
    #     cmd = shlex.split(cmd.strip())
    proc = pc.Popen(cmd, stdout=pc.PIPE, stderr=pc.PIPE, shell=True)
    out, err = proc.communicate()
    return out.decode('utf-8'), err.decode('utf-8'), proc.returncode


def print_err(*args, **kwargs):
    print(*args, **kwargs, file=sys.stderr)


_DNS_RE = re.compile('^[a-z0-9]([-a-z0-9]*[a-z0-9])?$')


def check_valid_dns(name):
    """
    experiment name is used as namespace, which must conform to DNS format
    """
    if not _DNS_RE.match(name):
        raise ValueError(name + ' must be a valid DNS name with only lower-case '
            'letters, 0-9 and hyphen. No underscore or dot allowed.')


class Kubectl(object):
    def __init__(self, surreal_yml='~/.surreal.yml', dry_run=False):
        surreal_yml = U.f_expand(surreal_yml)
        assert path.exists(surreal_yml)
        # persistent config in the home dir that contains git access token
        self.config = YamlList.from_file(surreal_yml)[0]
        self.folder = U.f_expand(self.config.experiment_folder)
        self.dry_run = dry_run
        self._loop_start_time = None

    def _yaml_path(self, experiment_name, yaml_name='kurreal.yml'):
        "retrieve from the experiment folder"
        return U.f_join(self.folder, experiment_name, yaml_name)

    def run(self, cmd):
        if self.dry_run:
            print('kubectl ' + cmd)
            return '', '', 0
        else:
            out, err, retcode = run_process('kubectl ' + cmd)
            if 'could not find default credentials' in err:
                print("Please try `gcloud container clusters get-credentials mycluster` "
                      "to fix credential error")
            return out.strip(), err.strip(), retcode

    def _print_err_return(self, out, err, retcode):
        print_err('error code:', retcode)
        print_err('*' * 20, 'stderr', '*' * 20)
        print_err(err)
        print_err('*' * 20, 'stdout', '*' * 20)
        print_err(out)
        print_err('*' * 46)

    def run_verbose(self, cmd, print_out=True, raise_on_error=False):
        out, err, retcode = self.run(cmd)
        if retcode != 0:
            self._print_err_return(out, err, retcode)
            msg = 'Command `kubectl {}` fails'.format(cmd)
            if raise_on_error:
                raise RuntimeError(msg)
            else:
                print_err(msg)
        elif out and print_out:
            print(out)
        return out, err, retcode

    def run_event_loop(self, func, *args, poll_interval=1, **kwargs):
        """
        Run a function repeatedly until it returns True
        """
        self._loop_start_time = time.time()
        while True:
            if func(*args, **kwargs):
                break
            time.sleep(poll_interval)

    def _create_loop(self, yaml_file, namespace):
        """
        Useful for restarting a kube service.
        Resource might be in the process of deletion. Wait until deletion completes
        """
        yaml_file = U.f_expand(yaml_file)
        out, err, retcode = self.run('create -f "{}" --namespace {}'
                                     .format(yaml_file, namespace))
        if retcode:
            # TODO: very hacky check, should run checks on names instead
            if 'is being deleted' in err:
                if time.time() - self._loop_start_time > 30:
                    print_err('old resource being deleted, waiting ...')
                    print_err(err)
                    self._loop_start_time = time.time()
                return False
            else:
                if 'AlreadyExists' in err:
                    print('Warning: some components already exist')
                else:
                    print_err('create encounters an error that is not `being deleted`')
                self._print_err_return(out, err, retcode)
                return True
        else:
            print(out)
            return True

    def create(self,
               experiment_name,
               jinja_template,
               context=None,
               check_file_exists=True,
               **context_kwargs):
        """
        kubectl create namespace <experiment_name>
        kubectl create -f kurreal.yml --namespace <experiment_name>

        Args:
            jinja_template: Jinja template kurreal.yml
            context: see `YamlList`
            **context_kwargs: see `YamlList`

        Returns:
            path for the rendered kurreal.yml in the experiment folder
        """
        check_valid_dns(experiment_name)
        rendered_path = self._yaml_path(experiment_name)
        if check_file_exists and U.f_exists(rendered_path):
            raise FileExistsError(rendered_path
                                  + ' already exists, cannot run `create`.')
        U.f_mkdir_in_path(rendered_path)
        JinjaYaml.from_file(jinja_template).render_file(
            rendered_path, context=context, **context_kwargs
        )
        if self.dry_run:
            print(file_content(rendered_path))
        else:
            self.run('create namespace ' + experiment_name)
            self.run_event_loop(
                self._create_loop,
                rendered_path,
                namespace=experiment_name,
                poll_interval=5
            )

    def _yamlify_label_string(self, label_string):
        if not label_string:
            return ''
        assert (':' in label_string or '=' in label_string), \
            'label spec should look like <labelname>=<labelvalue>'
        if ':' in label_string:
            label_spec = label_string.split(':', 1)
        else:
            label_spec = label_string.split('=', 1)
        # the space after colon is necessary for valid yaml
        return '{}: {}'.format(*label_spec)

    def _get_docker_image(self, image_name):
        """
        image_name: key in ~/.surreal.yml `images` section that points to docker URL
            else assume it's a docker image URL itself.
        """
        if image_name in self.config.images:
            return self.config.images[image_name]
        else:
            assert '/' in image_name, 'must be a valid docker image URL'
            return image_name

    def create_surreal(self,
                       experiment_name,
                       jinja_template,
                       snapshot=True,
                       mujoco=True,
                       agent_pool_label='agent-pool',
                       nonagent_pool_label='nonagent-pool',
                       agent_image='agent',
                       nonagent_image='nonagent',
                       context=None,
                       check_file_exists=True,
                       **context_kwargs):
        """
        First create a snapshot of the git repos, upload to github
        Then create Kube objects with the git info
        Args:
            agent_pool_label: surreal-node=<agent_pool_label>
            nonagent_pool_label: surreal-node=<nonagent_pool_label>
            agent_image: key in ~/.surreal.yml `images` section.
                If not a key, assume it's a docker image URL itself
            nonagent_image: see `agent_image`
            context: for extra context variables
        """
        check_valid_dns(experiment_name)
        repo_paths = self.config.git.get('snapshot_repos', [])
        repo_paths = [U.f_expand(p) for p in repo_paths]
        if snapshot and not self.dry_run:
            for repo_path in repo_paths:
                push_snapshot(
                    snapshot_branch=self.config.git.snapshot_branch,
                    repo_path=repo_path
                )
        repo_names = [path.basename(path.normpath(p)).lower()
                      for p in repo_paths]
        surreal_context = {
            'GIT_USER': self.config.git.user,
            'GIT_TOKEN': self.config.git.token,
            'GIT_SNAPSHOT_BRANCH': self.config.git.snapshot_branch,
            'GIT_REPOS': repo_names,
        }
        if context is None:
            context = {}
        if mujoco:
            surreal_context['MUJOCO_KEY_TEXT'] = \
                file_content(self.config.mujoco_key_path)
        surreal_context.update(context)
        # select nodes from nodepool label to schedule agent/nonagent pods
        surreal_context['AGENT_POOL_LABEL'] = \
            self._yamlify_label_string('surreal-node=' + agent_pool_label)
        surreal_context['NONAGENT_POOL_LABEL'] = \
            self._yamlify_label_string('surreal-node=' + nonagent_pool_label)
        surreal_context['AGENT_IMAGE'] = self._get_docker_image(agent_image)
        surreal_context['NONAGENT_IMAGE'] = self._get_docker_image(nonagent_image)
        self.create(
            experiment_name,
            jinja_template,
            context=surreal_context,
            check_file_exists=check_file_exists,
            **context_kwargs
        )

    def delete(self, experiment_name):
        """
        kubectl delete -f kurreal.yml --namespace <experiment_name>
        kubectl delete namespace <experiment_name>
        """
        check_valid_dns(experiment_name)
        yaml_path = self._yaml_path(experiment_name)
        if not U.f_exists(yaml_path):
            raise FileNotFoundError(yaml_path + ' does not exist, cannot stop.')
        self.run_verbose(
            'delete -f "{}" --namespace {}'
                .format(yaml_path, experiment_name),
            print_out=True, raise_on_error=False
        )
        self.run_verbose(
            'delete namespace {}'.format(experiment_name),
            print_out=True, raise_on_error=False
        )

    def current_context(self):
        out, err, retcode = self.run_verbose(
            'config current-context', print_out=False, raise_on_error=True
        )
        return out

    def current_namespace(self):
        """
        Parse from `kubectl config view`
        """
        config = self.config_view()
        current_context = self.current_context()
        for context in config['contexts']:
            if context['name'] == current_context:
                return context['context']['namespace']
        raise RuntimeError('INTERNAL: current context not found')

    def set_namespace(self, namespace):
        """
        https://kubernetes.io/docs/concepts/overview/working-with-objects/namespaces/
        After this call, all subsequent `kubectl` will default to the namespace
        """
        check_valid_dns(namespace)
        _, _, retcode = self.run_verbose(
            'config set-context $(kubectl config current-context) --namespace='
            + namespace,
            print_out=True, raise_on_error=False
        )
        if retcode == 0:
            print('successfully switched to namespace `{}`'.format(namespace))

    def label_nodes(self, old_labels, new_label_name, new_label_value):
        """
        Select nodes that comply with `old_labels` spec, and assign them
        a set of new nodes: `label:value`
        https://kubernetes.io/docs/concepts/configuration/assign-pod-node/
        """
        node_names = self.query_resources('node', 'name', labels=old_labels)
        for node_name in node_names:
            new_label_string = shlex.quote('{}={}'.format(
                new_label_name, new_label_value
            ))
            # no need for `kubectl label nodes` because the `names` returned
            # will have fully qualified name "nodes/my-node-name`
            self.run_verbose('label --overwrite {} {}'.format(
                node_name, new_label_string
            ))

    def _get_selectors(self, labels, fields):
        """
        Helper for list_resources and list_jsonpath
        """
        labels, fields = labels.strip(), fields.strip()
        cmd= ' '
        if labels:
            cmd += '--selector ' + shlex.quote(labels)
        if fields:
            cmd += ' --field-selector ' + shlex.quote(fields)
        return cmd

    def query_resources(self, resource, output_format,
                        names=None, labels='', fields=''):
        """
        Query all items in the resource with `output_format`
        JSONpath: https://kubernetes.io/docs/reference/kubectl/jsonpath/
        label selectors: https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/

        Args:
            resource: pod, service, deployment, etc.
            output_format: https://kubernetes.io/docs/reference/kubectl/overview/#output-options
              - custom-columns=<spec>
              - custom-columns-file=<filename>
              - json: returns a dict
              - jsonpath=<template>
              - jsonpath-file=<filename>
              - name: list
              - wide
              - yaml: returns a dict
            names: list of names to get resource, mutually exclusive with
                label and field selectors. Should only specify one.
            labels: label selector syntax, comma separated as logical AND. E.g:
              - equality: mylabel=production
              - inequality: mylabel!=production
              - set: mylabel in (group1, group2)
              - set exclude: mylabel notin (group1, group2)
              - don't check value, only check key existence: mylabel
              - don't check value, only check key nonexistence: !mylabel
            fields: field selector, similar to label selector but operates on the
              pod fields, such as `status.phase=Running`
              fields can be found from `kubectl get pod <mypod> -o yaml`

        Returns:
            dict if output format is yaml or json
            list if output format is name
            string from stdout otherwise
        """
        if names and (labels or fields):
            raise ValueError('names and (labels or fields) are mutually exclusive')
        cmd = 'get ' + resource
        if names is None:
            cmd += self._get_selectors(labels, fields)
        else:
            assert isinstance(names, (list, tuple))
            cmd += ' ' + ' '.join(names)
        if '=' in output_format:
            # quoting the part after jsonpath=<...>
            prefix, arg = output_format.split('=', 1)
            output_format = prefix + '=' + shlex.quote(arg)
        cmd += ' -o ' + output_format
        out, _, _ = self.run_verbose(cmd, print_out=False, raise_on_error=True)
        if output_format == 'yaml':
            return EzDict.loads_yaml(out)
        elif output_format == 'json':
            return EzDict.loads_json(out)
        elif output_format == 'name':
            return out.split('\n')
        else:
            return out

    def query_jsonpath(self, resource, jsonpath,
                       names=None, labels='', fields=''):
        """
        Query items in the resource with jsonpath
        https://kubernetes.io/docs/reference/kubectl/jsonpath/
        This method is an extension of list_resources()
        Args:
            resource:
            jsonpath: make sure you escape dot if resource key string contains dot.
              key must be enclosed in *single* quote!!
              e.g. {.metadata.labels['kubernetes\.io/hostname']}
              you don't have to do the range over items, we take care of it
            labels: see `list_resources`
            fields:

        Returns:
            a list of returned jsonpath values
        """
        if '{' not in jsonpath:
            jsonpath = '{' + jsonpath + '}'
        jsonpath = '{range .items[*]}' + jsonpath + '{"\\n\\n"}{end}'
        output_format = "jsonpath=" + jsonpath
        out = self.query_resources(
            resource=resource,
            names=names,
            output_format=output_format,
            labels=labels,
            fields=fields
        )
        return out.split('\n\n')

    def config_view(self):
        """
        kubectl config view
        Generates a yaml of context and cluster info
        """
        out, err, retcode = self.run_verbose(
            'config view', print_out=False, raise_on_error=True
        )
        return EzDict.loads_yaml(out)

    def external_ip(self, pod_name):
        """
        Returns:
            "<ip>:<port>"
        """
        tb = self.query_resources('svc', 'yaml', names=[pod_name])
        conf = tb.status.loadBalancer
        if not ('ingress' in conf and 'ip' in conf.ingress[0]):
            print_err('Tensorboard does not have an external IP.')
            return ''
        ip = conf.ingress[0].ip
        port = tb.spec.ports[0].port
        return '{}:{}'.format(ip, port)

    def _get_logs_cmd(self, pod_name, container_name,
                      follow, since=0, tail=-1):
        return 'logs {} {} {} --since={} --tail={}'.format(
            pod_name,
            container_name,
            '--follow' if follow else '',
            since,
            tail
        )

    def logs(self, pod_name, container_name='', since=0, tail=100):
        """
        kubectl logs <pod_name> <container_name> --follow --since= --tail=
        https://kubernetes-v1-4.github.io/docs/user-guide/kubectl/kubectl_logs/

        Returns:
            stdout string
        """
        out, err, retcode = self.run_verbose(
            self._get_logs_cmd(pod_name, container_name,
                               follow=False, since=since, tail=tail),
            print_out=False,
            raise_on_error=False
        )
        if retcode != 0:
            return ''
        else:
            return out

    def print_logs(self, pod_name, container_name='',
                   follow=False, since=0, tail=100):
        """
        kubectl logs <pod_name> <container_name>
        No error checking, no string caching, delegates to os.system
        """
        cmd = self._get_logs_cmd(pod_name, container_name,
                                 follow=follow, since=since, tail=tail)
        os.system('kubectl ' + cmd)

    def logs_surreal(self, component_name, is_print=False,
                     follow=False, since=0, tail=100):
        """
        Args:
            component_name: can be agent-N, learner, ps, replay, tensorplex, tensorboard

        Returns:
            stdout string if is_print else None
        """
        if is_print:
            log_func = functools.partial(self.print_logs, follow=follow)
        else:
            log_func = self.logs
        log_func = functools.partial(log_func, since=since, tail=tail)
        if component_name.startswith('agent-'):
            return log_func(component_name)
        else:
            assert component_name in \
                   ['learner', 'ps', 'replay', 'tensorplex', 'tensorboard']
            return log_func('nonagent', component_name)


if __name__ == '__main__':
    # TODO: save temp file to experiment dir
    import pprint
    pp = pprint.pprint
    kube = Kubectl(dry_run=0)
    # 3 different ways to get a list of node names
    # pp(kube.query_jsonpath('nodes', '{.metadata.name}'))
    # pp(kube.query_jsonpath('nodes', "{.metadata.labels['kubernetes\.io/hostname']}"))
    # pp(kube.query_resources('nodes', 'name'))
    # yaml for pods
    # pp(kube.query_resources('pods', 'json', fields='metadata.name=agent-0').dumps_yaml())
