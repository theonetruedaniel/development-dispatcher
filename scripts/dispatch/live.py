"""Pinned local App Server with per-process restrictions; no global config writes."""
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
from .protocol import ProtocolClient, clean_environment

DISABLED_FEATURES = (
    'apps', 'hooks', 'plugins', 'remote_plugin', 'browser_use', 'browser_use_external',
    'computer_use', 'image_generation', 'multi_agent', 'multi_agent_v2', 'shell_tool',
    'unified_exec', 'memories', 'shell_snapshot', 'shell_snapshot_v2',
    'skill_mcp_dependency_install', 'code_mode', 'code_mode_host', 'in_app_browser',
    'workspace_dependencies', 'tool_suggest', 'goals', 'auth_elicitation', 'view_image',
)
SETTINGS = {'web_search': 'disabled', 'notify': [], 'model_provider': 'openai',
            'forced_login_method': 'chatgpt', 'approvals_reviewer': 'user',
            'sandbox_mode': 'read-only', 'approval_policy': 'on-request'}
TELEMETRY = {'exporter': 'none', 'trace_exporter': 'none', 'metrics_exporter': 'none',
             'log_user_prompt': False}
PIN_PATH = Path(__file__).with_name('protocol-compatibility.json')


def validate_config(cfg):
    if not isinstance(cfg, dict) or not isinstance(cfg.get('features'), dict):
        raise ValueError('Effective isolation configuration unavailable')
    if any(cfg['features'].get(k) is not False for k in DISABLED_FEATURES):
        raise ValueError('Inherited capability is not disabled')
    if any(cfg.get(k) != v for k, v in SETTINGS.items()):
        raise ValueError('Isolation or account configuration mismatch')
    telemetry = cfg.get('otel')
    if not isinstance(telemetry, dict) or any(telemetry.get(k) != v for k, v in TELEMETRY.items()):
        raise ValueError('Telemetry export is not disabled')
    servers = cfg.get('mcp_servers')
    if not isinstance(servers, dict) or any(not isinstance(v, dict) or v.get('enabled') is not False for v in servers.values()):
        raise ValueError('An MCP server is not disabled')
    if cfg.get('openai_base_url') or cfg.get('chatgpt_base_url') not in (None, 'https://chatgpt.com/backend-api', 'https://chatgpt.com/backend-api/'):
        raise ValueError('Custom account endpoint is not qualified')
    if (cfg.get('model_providers') or {}).get('openai'):
        raise ValueError('Custom OpenAI provider is not qualified')


def validate_inventory(value, disabled_servers=()):
    if not isinstance(value, dict) or not isinstance(value.get('data'), list) or value.get('nextCursor') is not None:
        raise ValueError('Connected-tool inventory is incomplete')
    for entry in value['data']:
        if (not isinstance(entry, dict) or entry.get('name') not in disabled_servers
                or entry.get('tools') != {} or entry.get('resources') != []
                or entry.get('resourceTemplates') != []
                or entry.get('runtimeStatus') != 'disabled'
                or any(entry.get(k) is not None for k in ('toolsError', 'serverInfo', 'serverCapabilities'))):
            raise ValueError('Connected-tool inventory contains an unqualified capability')


def validate_thread(response, route):
    sandbox = response.get('sandbox', {})
    if (response.get('model') != route.model or response.get('reasoningEffort') != route.effort
            or response.get('modelProvider') != 'openai'
            or response.get('approvalPolicy') != 'on-request'
            or response.get('approvalsReviewer') != 'user'
            or sandbox.get('type') != 'readOnly' or sandbox.get('networkAccess', False) is not False):
        raise ValueError('Server did not accept the scoped route and permissions')
    return {'model': response['model'], 'effort': response['reasoningEffort']}


def verify_pin(executable, pin):
    with Path(executable).open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != pin.get('executable_sha256'):
            raise ValueError('Codex executable changed; requalification required')
    kwargs = dict(capture_output=True, timeout=30, check=True, env=clean_environment(os.environ),
                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    version = subprocess.run([str(executable), '--version'], **kwargs).stdout.decode().strip()
    if version != pin['cli_version']:
        raise ValueError('Codex version changed; requalification required')
    with tempfile.TemporaryDirectory(prefix='abrams-schema-') as directory:
        subprocess.run([str(executable), 'app-server', 'generate-json-schema', '--out', directory], **kwargs)
        for name, expected in pin['files'].items():
            if hashlib.sha256((Path(directory)/name).read_bytes()).hexdigest() != expected:
                raise ValueError('Codex protocol changed; requalification required')


def command(executable, servers=()):
    result = [str(executable), 'app-server']
    for feature in DISABLED_FEATURES:
        result += ['--disable', feature]
    for key, value in SETTINGS.items():
        result += ['-c', key+'='+json.dumps(value)]
    for key, value in TELEMETRY.items():
        result += ['-c', 'otel.'+key+'='+json.dumps(value)]
    for name in servers:
        if not re.fullmatch(r'[A-Za-z0-9_-]+', name):
            raise ValueError('MCP server name cannot be safely overridden')
        result += ['-c', 'mcp_servers.'+name+'.enabled=false']
    return result


class LiveClient(ProtocolClient):
    isolation_verified = False

    def __init__(self, cmd, workspace):
        self.workspace = str(Path(workspace).resolve(strict=True))
        self.ready = False
        super().__init__(cmd)

    def initialize(self):
        if self.ready:
            return
        super().initialize()
        self.check_configuration()
        self.isolation_verified = self.ready = True

    def check_configuration(self):
        requirements = super().request('configRequirements/read', {})
        # Managed requirements can add hooks. Do not bypass or reinterpret them.
        managed = requirements.get('requirements')
        if managed is not None and (not isinstance(managed, dict) or any(
                value is not None and not (key == 'allowedLoginMethods' and value == ['chatgpt'])
                for key, value in managed.items())):
            raise ValueError('Managed requirements need separate isolation qualification')
        cfg = super().request('config/read', {'cwd': self.workspace, 'includeLayers': False})['config']
        self.validate_configuration(cfg)
        self.disabled_servers = set(cfg['mcp_servers'])

    def validate_configuration(self, cfg):
        validate_config(cfg)

    def request(self, method, params, timeout=15):
        if method == 'thread/start':
            self.check_configuration()
        return super().request(method, params, timeout)

    def verify_thread(self, response, route):
        accepted = validate_thread(response, route)
        validate_inventory(self.request('mcpServerStatus/list', {
            'threadId': response['thread']['id'], 'limit': 100}), self.disabled_servers)
        return accepted



def resolve_executable(pin):
    explicit = pin.get('executable_path')
    if explicit is not None:
        if not isinstance(explicit, str) or not Path(explicit).is_absolute():
            raise ValueError('Pinned Codex executable must be an absolute path')
        if not Path(explicit).is_file():
            raise ValueError('Pinned Codex executable unavailable')
        return explicit
    executable = shutil.which('codex')
    if not executable:
        raise ValueError('Codex executable unavailable')
    return executable


def qualified_client(workspace):
    pin = json.loads(PIN_PATH.read_text(encoding='utf-8'))
    executable = resolve_executable(pin)
    verify_pin(executable, pin)
    # This first process performs metadata reads only: no thread, model, or MCP startup.
    probe = ProtocolClient(command(executable))
    try:
        probe.initialize()
        cfg = probe.request('config/read', {'cwd': str(Path(workspace).resolve(strict=True)), 'includeLayers': False})['config']
        servers = cfg.get('mcp_servers')
        if not isinstance(servers, dict):
            raise ValueError('MCP configuration unavailable')
    finally:
        probe.close()
    client = LiveClient(command(executable, servers), workspace)
    try:
        client.initialize()
        return client
    except BaseException:
        client.close()
        raise
