"""Controller-owned service construction; no environment supplied by the worker."""
import json
from pathlib import Path
import shutil
import tempfile

from .live import command, verify_pin
from .code_host import CodeHost
from .protocol import ProtocolClient
from .write_contracts import safe_path, safe_root
from .write_live import WriteClient, write_command
from .write_sandbox import qualify_write_environment
from .wsl_adapter import inspect_runtime

ENVIRONMENT_PATH = Path(__file__).with_name('write-environment.json')


def build_write_services(primary: Path, journal) -> dict:
    primary = safe_root(primary)
    try:
        path = safe_path(ENVIRONMENT_PATH.parent, ENVIRONMENT_PATH.name)
        if path.stat().st_size > 4*1024*1024:
            raise ValueError('Write environment exceeds size bound')
        environment = json.loads(path.read_text(encoding='utf-8'),
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Nonfinite environment')))
    except FileNotFoundError as exc:
        raise ValueError('No qualified write environment installed') from exc
    if (not isinstance(environment, dict) or
            set(environment) != {'schema_version', 'pin', 'runtime', 'checks', 'selector'} or
            type(environment['schema_version']) is not int or environment['schema_version'] != 1 or
            any(not isinstance(environment[key], dict) for key in ('pin', 'runtime', 'checks', 'selector'))):
        raise ValueError('Invalid controller write environment')
    storage = Path(tempfile.gettempdir()).resolve() / 'abrams-dispatch-copies'
    if storage.is_relative_to(primary) or primary.is_relative_to(storage):
        raise ValueError('Controller storage overlaps primary workspace')
    return dict(journal=journal, storage=storage, environment=environment,
                client_factory=qualified_write_client)


def qualified_write_client(worker: Path, environment: dict):
    worker = safe_root(worker)
    admission = qualify_write_environment(environment, {'worker': str(worker)})
    if admission.get('status') != 'qualified' or admission.get('live_writes') is not True:
        raise ValueError('Editing environment not qualified')
    executable = shutil.which('codex')
    if not executable:
        raise ValueError('Codex executable unavailable')
    verify_pin(executable, environment['pin'])
    inspect_runtime(environment['runtime'])
    # Metadata-only discovery disables every inherited server before tool startup.
    probe = ProtocolClient(command(executable))
    try:
        probe.initialize()
        cfg = probe.request('config/read', {'cwd': str(worker), 'includeLayers': False})['config']
        servers = cfg.get('mcp_servers')
        if not isinstance(servers, dict):
            raise ValueError('MCP configuration unavailable')
    finally:
        probe.close()
    host = CodeHost(Path(executable).with_name('codex-code-mode-host.exe'),
                    environment['pin'].get('code_host_sha256'))
    client = None
    try:
        args = write_command(executable, servers) + ['--code-mode-host', host.url]
        client = WriteClient(args, worker, host=host)
        client.initialize()
        return client
    except BaseException:
        try:
            if client is not None:client.close()
        finally:host.close()
        raise
