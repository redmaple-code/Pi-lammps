#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""localfs MCP server(云端 agent 进程内运行):把文件操作经长轮询桥转发到调用方本机。
用法: python3.11 localfs_mcp.py <username>   (runner 按 job 注入;run token 走 env LOCALFS_RUN_TOKEN,
不进 argv——/proc/<pid>/cmdline 全用户可读,environ 仅属主可读)
协议:换行分隔 JSON-RPC 2.0 over stdio(仿 mcp/md_mcp.py)。
注意:本进程不含任何文件逻辑——所有操作由用户本机的 mdagent_client.py 执行,
写操作由对方面前批准。这里只是转发 + 超时。"""
import json
import os
import sys
import traceback
import urllib.request

CALL_URL = "http://127.0.0.1:8090/v1/bridge/call"
CALL_TIMEOUT = 115  # 须 < toolCallTimeoutMs(120s),> bridge.CALL_TIMEOUT(110s)

USERNAME = sys.argv[1] if len(sys.argv) > 1 else ""
RUN_TOKEN = os.environ.get("LOCALFS_RUN_TOKEN", "")


def log(msg):
    sys.stderr.write("[localfs] %s\n" % msg)
    sys.stderr.flush()


def bridge_call(op, args):
    body = json.dumps({"token": RUN_TOKEN, "op": op, "args": args}).encode()
    req = urllib.request.Request(CALL_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=CALL_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as ex:
        return {"ok": False, "error": "桥调用失败: %s" % ex}


def _check_path_args(args, need_content=False):
    p = (args.get("path") or "").strip()
    if not p:
        raise ValueError("path 不能为空")
    if need_content and "content" not in args:
        raise ValueError("content 缺失")
    return p


def h_list(args):
    return bridge_call("list", {"path": _check_path_args(args)})


def h_read(args):
    return bridge_call("read", {"path": _check_path_args(args)})


def h_write(args):
    p = _check_path_args(args, need_content=True)
    return bridge_call("write", {"path": p, "content": str(args["content"])})


def h_mkdir(args):
    return bridge_call("mkdir", {"path": _check_path_args(args)})


def h_delete(args):
    return bridge_call("delete", {"path": _check_path_args(args)})


def h_exec(args):
    cmd = str(args.get("cmd") or "").strip()
    if not cmd:
        raise ValueError("cmd 不能为空")
    payload = {"cmd": cmd, "timeout_s": args.get("timeout_s")}
    if args.get("supervise"):
        payload["supervise"] = True
        if args.get("case"):
            payload["case"] = str(args.get("case"))
    return bridge_call("exec", payload)


def h_edit(args):
    p = _check_path_args(args)
    old, new = args.get("old"), args.get("new")
    if not isinstance(old, str) or not old:
        raise ValueError("old 不能为空")
    if not isinstance(new, str):
        raise ValueError("new 缺失")
    return bridge_call("edit", {"path": p, "old": old, "new": new})


def h_glob(args):
    pat = str(args.get("pattern") or "").strip()
    if not pat:
        raise ValueError("pattern 不能为空")
    return bridge_call("glob", {"pattern": pat, "path": args.get("path") or "."})


def h_grep(args):
    pat = str(args.get("pattern") or "").strip()
    if not pat:
        raise ValueError("pattern 不能为空")
    try:
        mx = max(1, min(int(args.get("max") or 100), 200))
    except (TypeError, ValueError):
        mx = 100
    return bridge_call("grep", {"pattern": pat, "path": args.get("path") or ".",
                                "max": mx})


_DESC = "操作的是**调用方本机**的文件(经加密桥在用户机器执行),不是云端服务器的文件。"
TOOLS = [
    {"name": "localfs_list",
     "description": "列出调用方本机目录内容。" + _DESC,
     "inputSchema": {"type": "object",
                     "properties": {"path": {"type": "string", "description": "相对用户开放根目录的路径"}},
                     "required": ["path"]}},
    {"name": "localfs_read",
     "description": "读取调用方本机文件内容(≤1MB)。**.docx(≤8MB)与 .pdf(若本机装了 pdftotext)自动解出纯文本直接可读**——读 Word/PDF 论文报告直接用它,不要绕道 exec 提取。" + _DESC,
     "inputSchema": {"type": "object",
                     "properties": {"path": {"type": "string"}},
                     "required": ["path"]}},
    {"name": "localfs_edit",
     "description": "精确修改调用方本机文件的一处文本(old→new 字符串替换)。old 必须在文件中**恰好出现一次**(多了少了都会拒绝,不会盲改);**对方会在屏幕上看到修改前后对比并现场批准/拒绝**,被拒即停,不要换种改法重试。适合微调脚本参数;整文件重写用 localfs_write。" + _DESC,
     "inputSchema": {"type": "object",
                     "properties": {"path": {"type": "string"},
                                    "old": {"type": "string", "description": "要替换的原文本(须唯一)"},
                                    "new": {"type": "string", "description": "替换后的文本"}},
                     "required": ["path", "old", "new"]}},
    {"name": "localfs_glob",
     "description": "在调用方本机开放目录下按通配符找文件(如 *.data、**/*.in)。只读,免批准。返回相对路径列表(≤500 条)。" + _DESC,
     "inputSchema": {"type": "object",
                     "properties": {"pattern": {"type": "string", "description": "glob 通配符,如 *.data 或 **/*.lammpstrj"},
                                    "path": {"type": "string", "description": "起始子目录(默认开放根)"}},
                     "required": ["pattern"]}},
    {"name": "localfs_grep",
     "description": "在调用方本机开放目录下按正则搜文件内容(文本文件,跳过二进制)。只读,免批准。返回 文件:行号:行(≤max 条,默认100)。找参数/电荷/口令这类内容用它,别一个个 localfs_read。" + _DESC,
     "inputSchema": {"type": "object",
                     "properties": {"pattern": {"type": "string", "description": "正则表达式(python re 语法)"},
                                    "path": {"type": "string", "description": "起始子目录(默认开放根)"},
                                    "max": {"type": "integer", "description": "最多返回条数(默认100,上限200)"}},
                     "required": ["pattern"]}},
    {"name": "localfs_write",
     "description": "写入调用方本机文件(≤1MB)。**对方会在自己屏幕上看到内容摘要并现场批准/拒绝**,被拒即停,不要换路径重试。" + _DESC,
     "inputSchema": {"type": "object",
                     "properties": {"path": {"type": "string"},
                                    "content": {"type": "string"}},
                     "required": ["path", "content"]}},
    {"name": "localfs_mkdir",
     "description": "在调用方本机建目录(递归)。" + _DESC,
     "inputSchema": {"type": "object",
                     "properties": {"path": {"type": "string"}},
                     "required": ["path"]}},
    {"name": "localfs_delete",
     "description": "删除调用方本机的文件或空目录。**对方会在自己屏幕上看到路径并现场批准/拒绝**,被拒即停,不要换路径重试;删除不可恢复,只在用户明确要求时调用。" + _DESC,
     "inputSchema": {"type": "object",
                     "properties": {"path": {"type": "string"}},
                     "required": ["path"]}},
    {"name": "localfs_exec",
     "description": "在调用方本机**后台执行 shell 命令**(bash,cwd=开放根目录)——这是在用户机器上跑程序(如 lmp/gmx/python)的唯一途径,服务器上的 bash 够不到用户机器。立即返回 run_id/pid/日志相对路径(固定日志目录 .mdagent_run/);之后用 localfs_read 读日志跟进进度,日志末尾有退出码。**若对方开了免批准(/auto)则直接执行,否则对方屏幕上看到完整命令现场批准**,被拒即停。长任务(模拟几小时)直接发,日志会持续增长;timeout_s 默认 7200 秒上限强杀。" + _DESC,
     "inputSchema": {"type": "object",
                     "properties": {"cmd": {"type": "string", "description": "完整 shell 命令"},
                                    "timeout_s": {"type": "integer", "description": "超时秒数(默认7200,最大86400)"},
                                    "supervise": {"type": "boolean", "description": "true 且对方机器有 MD 编排管线时,命令经安全门发车并进看护看板(崩溃自愈/进度可见),返回 case_id/prod_dir 供 localfs_read 跟进;对方无管线则直跑并在返回里注明"},
                                    "case": {"type": "string", "description": "supervise 时的算例名(看板显示用,可中文)"}},
                     "required": ["cmd"]}},
]
HANDLERS = {"localfs_list": h_list, "localfs_read": h_read,
            "localfs_write": h_write, "localfs_mkdir": h_mkdir,
            "localfs_delete": h_delete, "localfs_exec": h_exec,
            "localfs_edit": h_edit, "localfs_glob": h_glob,
            "localfs_grep": h_grep}


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(msg):
    mid = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        pv = (msg.get("params") or {}).get("protocolVersion") or "2024-11-05"
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": pv,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "localfs", "version": "0.1.0"}}})
    elif method in ("notifications/initialized", "initialized"):
        pass
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if fn is None:
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "未知工具: %s" % name}], "isError": True}})
            return
        try:
            data = fn(args)
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
                "structuredContent": data}})
        except Exception as ex:
            log("tool error: " + traceback.format_exc())
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "工具执行出错: %s" % ex}], "isError": True}})
    else:
        if mid is not None:
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32601, "message": "method not found: %s" % method}})


def main():
    if not USERNAME or not RUN_TOKEN:
        log("缺 username/run_token 参数,退出")
        return
    log("localfs MCP server 启动(user=%s)" % USERNAME)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception as ex:
            log("bad json: %s" % ex)
            continue
        try:
            handle(msg)
        except Exception:
            log("handle error: " + traceback.format_exc())


if __name__ == "__main__":
    main()
