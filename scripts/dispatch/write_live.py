"""Experimental dynamic-tool transport. Construction does not admit live writes."""
import copy
import json
import threading

from .live import LiveClient, command, validate_config


TOOL_SPEC = {
    'type':'function', 'name':'dispatch_tool',
    'description':'Scoped project operations. read/delete/list arguments: {path}. write arguments: {path,text}. command arguments: {argv}. Paths are relative to the assigned copy; list path may be empty for the root. Commands use absolute Linux executables and /work. Grants and limits are controller-owned.',
    'inputSchema':{'type':'object','properties':{
        'operation':{'type':'string','enum':['read','write','delete','list','command']},
        'arguments':{'type':'object','properties':{'path':{'type':'string'},
            'text':{'type':'string'},'argv':{'type':'array','items':{'type':'string'}}},
            'additionalProperties':False}},'required':['operation','arguments'],'additionalProperties':False}}


def validate_write_config(cfg):
    if not isinstance(cfg,dict) or not isinstance(cfg.get('features'),dict) or cfg['features'].get('code_mode_host') is not True:
        raise ValueError('Write tools require the qualified Code Mode host')
    # Reuse every read-only restriction, with this one explicit host exception.
    normalized=copy.deepcopy(cfg)
    normalized['features']['code_mode_host']=False
    validate_config(normalized)
    profiles=cfg.get('permissions')
    profile=profiles.get('dispatch') if isinstance(profiles,dict) else None
    if (not isinstance(profile,dict) or
            any(value is not None for key,value in profile.items()
                if key not in ('description','filesystem','network'))):
        raise ValueError('Exact dispatch permission profile required')
    filesystem=profile.get('filesystem')
    network=profile.get('network')
    if (not isinstance(filesystem,dict) or
            {key:value for key,value in filesystem.items() if value is not None} !=
            {':minimal':'read',':workspace_roots':'read'}):
        raise ValueError('Dispatch filesystem profile changed')
    if (not isinstance(network,dict) or network.get('enabled') is not False or
            any(value is not None for key,value in network.items() if key!='enabled')):
        raise ValueError('Dispatch network profile changed')


def write_command(executable, servers=()):
    args=command(executable,servers)
    index=args.index('code_mode_host')
    del args[index-1:index+1]
    args += ['--enable','code_mode_host',
             '-c','permissions.dispatch.filesystem={":minimal"="read",":workspace_roots"="read"}',
             '-c','permissions.dispatch.network.enabled=false']
    return args


class WriteClient(LiveClient):
    def __init__(self, cmd, workspace, *, host=None):
        self.host=host
        self.broker=None
        self.stop=threading.Event()
        super().__init__(cmd,workspace)

    def request(self, method, params, timeout=15):
        if self.host is not None:self.host.check()
        result=super().request(method,params,timeout)
        if self.host is not None:self.host.check()
        return result

    def close(self):
        host=getattr(self,'host',None)
        try:
            if host is not None and not host.closed:host.check()
        finally:
            try:super().close()
            finally:
                if host is not None:host.close()

    def initialize(self):
        if self.ready:return
        self.request('initialize',{'clientInfo':{'name':'abrams_write_dispatch','version':'0.1'},
                                    'capabilities':{'experimentalApi':True}})
        self._send({'method':'initialized'})
        self.check_configuration()
        # Configuration is checked, but full runtime admission is a separate gate.
        self.ready=True

    def validate_configuration(self,cfg):
        validate_write_config(cfg)

    def bind_broker(self, broker, stop):
        if self.broker is not None:
            raise ValueError('Create a dedicated client for each write turn')
        self.broker=broker
        self.stop=stop

    def _event(self,msg,timeout=15):
        if msg.get('method') != 'item/tool/call' or 'id' not in msg:
            return super()._event(msg,timeout)
        try:
            params=msg.get('params')
            if (not isinstance(params,dict)
                    or set(params)-{'threadId','turnId','callId','namespace','tool','arguments'}
                    or params.get('namespace') is not None or params.get('tool')!='dispatch_tool'
                    or self.broker is None):
                raise ValueError('Unbound or unsupported dynamic tool')
            args=params.get('arguments')
            if not isinstance(args,dict) or set(args)!={'operation','arguments'}:
                raise ValueError('Invalid dynamic tool arguments')
            result=self.broker.handle(dict(account_fingerprint=self.broker.session['account_fingerprint'],
                thread_id=params['threadId'],turn_id=params['turnId'],call_id=params['callId'],
                operation=args['operation'],arguments=args['arguments']),self.stop)
        except (ValueError,TypeError,KeyError) as exc:
            result={'success':False,'error':str(exc)}
        content=json.dumps(result,ensure_ascii=True,allow_nan=False)
        if len(content.encode())>2*1024*1024:
            content='{"success":false,"error":"Tool result exceeds transport bound"}'
            result={'success':False}
        self._send({'id':msg['id'],'result':{'success':result.get('success') is True,
            'contentItems':[{'type':'inputText','text':content}]}},timeout)
