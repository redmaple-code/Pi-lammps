"""Extract original MCP tool schemas without starting a service or opening a store."""
import ast
import importlib
import json
from pathlib import Path
import sys
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ROOT/'packages/0.1.1'

def literal(path, symbol):
    node=ast.parse(path.read_text(encoding='utf-8'))
    for item in node.body:
        if isinstance(item, ast.Assign) and any(isinstance(x,ast.Name) and x.id==symbol for x in item.targets):
            return ast.literal_eval(item.value)
    raise RuntimeError('symbol missing: '+symbol)

evidence=literal(PACKAGES/'evidence/src/mcp/evidence_api.py','TOOL')
gate=literal(PACKAGES/'gate/src/mcp/md_proj_mcp.py','TOOLS')
sys.path.insert(0,str(PACKAGES/'knowledge/src/ukb'))
knowledge=importlib.import_module('ukb.mcp_server').TOOLS
for name,tools in [('knowledge',knowledge),('gate',gate),('evidence',[evidence])]:
    dest=PACKAGES/name/'MCP-TOOLS.json'
    dest.write_text(json.dumps({'source':'original-handoff-tool-registry','module':name,'tools':tools},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(name,len(tools))
