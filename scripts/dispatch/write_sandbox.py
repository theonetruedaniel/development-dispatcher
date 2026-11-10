"""Capability inspection. No live write backend has been qualified yet."""
import hashlib
import json


def inspect_write_tool_surface(schema: dict, configuration: dict, inventory: dict) -> dict:
    """Diagnose observed nested exposure without inventing direct-path authority.

    Schema and inventory are observations, not grants. No accepted live direct
    dispatch enforcement contract exists for this build, so this inspector never
    returns qualified, including for an apparently exact nested tool list.
    """
    result=dict(status='blocked',reason='invalid_schema',schema_digest=None,allowed_tools=[])
    if not isinstance(schema,dict) or not schema:return result
    try:
        raw=json.dumps(schema,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    except (TypeError,ValueError):return result
    result['schema_digest']=hashlib.sha256(raw).hexdigest()
    from .write_live import validate_write_config
    try:validate_write_config(configuration)
    except (TypeError,ValueError,KeyError,AttributeError):
        return result|{'reason':'configuration_not_isolated'}
    if (not isinstance(inventory,dict) or set(inventory)!={'scope','schema_digest','tools'} or
            inventory['scope']!='nested_code_mode' or not isinstance(inventory['tools'],list) or
            any(not isinstance(tool,str) for tool in inventory['tools'])):
        return result|{'reason':'invalid_inventory'}
    if inventory['schema_digest']!=result['schema_digest']:
        return result|{'reason':'schema_inventory_mismatch'}
    tools=inventory['tools']
    if len(tools)!=2 or set(tools)!={'clock__curr_time','dispatch_tool'}:
        return result|{'reason':'unexpected_tool_surface'}
    return result|{'reason':'direct_dispatch_enforcement_unverified'}


def inspect_schema(schema: dict) -> dict:
    definitions = schema.get("definitions", {})
    policies = definitions.get("SandboxPolicy", {}).get("anyOf", [])
    write = next((p.get("properties", {}) for p in policies
                  if "workspaceWrite" in p.get("properties", {}).get("type", {}).get("enum", [])), {})
    return {"workspace_write": bool(write),
            "scoped_reads": "readOnlyAccess" in write,
            "network_control": "networkAccess" in write,
            "qualified": False}


def qualify_write_environment(pin: dict, roots: dict) -> dict:
    # A pin, roots list, or caller-provided boolean is not enforcement evidence.
    return {"status": "blocked", "live_writes": False,
            "reason": "No runtime-qualified contained editing backend",
            "required": ["scoped tool reads and writes", "network denial",
                         "protected controller separation", "descendant Stop",
                         "current executable and schema qualification"]}
