#!/usr/bin/env python3

import re
import sys
import json
import socket
import string
import urllib.parse
import urllib.request
import markov

from collections import deque

class IRCMessage:
    def __init__(self,message):
        self.raw = message
        print('>>',message.strip())
        if len(message) < 1:
           raise RuntimeError('empty message')
        if message[0] == ':':
            self.prefix, message = message[1:].split(' ', 1)
        else:
            self.prefix = None
        if message.find(' :') != -1:
            message, rest = message.split(' :', 1)
            self.args = message.split(' ')
            self.args.append(rest)
        else:
            self.args = message.split()
        self.cmd = self.args.pop(0).upper()

    def __str__(self):
        return self.raw

class IRCConnection:

    def __init__(self,host,port,nick='Treebard',ident='pybot',realname='Fangorn'):
        self.buff = ''
        self.pending = deque()
        self.last = {}
        
        def single(b):
            return '\s%s$|^%s\s|^%s$' % (b,b,b)
        self.badwords = [single('pray'),single('prayer'),single('alot'),'zoid','s/.*//']
        self.badwords = [re.compile(b,flags=re.IGNORECASE) for b in self.badwords]
    
        self.s = socket.socket()
        self.s.connect((host, port))
        self.send('NICK',nick)
        self.send('USER',ident,host,'*',rest=realname)
    
    def send(self,cmd,*args,rest=None):
        cmd = cmd.upper()
        if cmd == 'PRIVMSG':
            dest = args[0]
            if dest in self.last and self.last[dest] == rest:
                return
            self.last[dest] = rest
            for badword in self.badwords:
                if badword.search(rest):
                    return
        if len(args) > 0:
            packet = '%s %s' % (cmd,' '.join(args))
        else:
            packet = cmd
        if rest:
            packet = '%s :%s\r\n' % (packet,rest)
        else:
            packet = '%s\r\n' % (packet)
        print('<<',packet.strip())
        self.s.sendall(packet.encode('UTF-8'))
        
    def recv(self):
        if len(self.pending) > 0:
            return IRCMessage(self.pending.popleft())
        self.buff = self.buff + self.s.recv(1024).decode('UTF-8')
        parts = self.buff.split('\r\n')
        self.buff = parts.pop() 
        self.pending.extend(parts)
        return IRCMessage(self.pending.popleft())
        
def strip_prefix(prefix):
    if prefix.find('!') != -1:
        nick,*_ = prefix.split('!')
        return nick
    return prefix

def ctcp_version(c,msg,replyto,params):
    c.send('NOTICE',replyto,rest='\x01VERSION pybot\x01')

def ctcp_ping(c,msg,replyto,params):
    c.send('NOTICE',replyto,rest='\x01PING %s\x01'%params)

def ctcp_action(c,msg,replyto,params):
    hook_sed(c,msg,replyto,params,action=True)

ctcp_handlers = {}
ctcp_handlers['VERSION'] = ctcp_version
ctcp_handlers['PING'] = ctcp_ping
ctcp_handlers['ACTION'] = ctcp_action

acl = {'BENLAND100':1000}
def acl_level(nick,newlvl=None):
    nick = nick.upper()
    if newlvl is not None:
        acl[nick] = newlvl
    return acl[nick] if nick in acl else 0

def cmd_quit(c,msg,replyto,params):
    c.send('QUIT',rest=(params if params is not None else 'Leaving.'))

def cmd_join(c,msg,replyto,params):
    c.send('JOIN',params)
    
def cmd_part(c,msg,replyto,params):
    c.send('PART',params if params is not None else replyto)
    
def cmd_say(c,msg,replyto,params):
    if params is not None:
        c.send('PRIVMSG',replyto,rest=params)
        
def cmd_do(c,msg,replyto,params):
    if params is not None:
        c.send('PRIVMSG',replyto,rest='\x01ACTION %s\x01'%params)
        
def cmd_access(c,msg,replyto,params):
    if params is None:
        return
    args = params.split()
    if len(args) == 1:
        c.send('PRIVMSG',replyto,rest='%s has access level %i'%(args[0],acl_level(args[0])))
    elif len(args) == 2:
        usrlvl = acl_level(strip_prefix(msg.prefix))
        setlvl = int(args[1])
        if setlvl < usrlvl:
            c.send('PRIVMSG',replyto,rest='%s has access level %i'%(args[0],acl_level(args[0],setlvl)))

giphy_key = 'MGH18YkELG9nJV0S6AHfwTDHT0UuyZrA'
giphy_last = ''
giphy_last_count = 0
def cmd_giphy(c,msg,replyto,params):
    global giphy_last,giphy_last_count
    if params == giphy_last:
        print('NEXT')
        giphy_last_count = giphy_last_count + 1
        args = urllib.parse.urlencode({'api_key':giphy_key,'q':params,'limit':1,'offset':giphy_last_count})
    else:
        giphy_last = params
        args = urllib.parse.urlencode({'api_key':giphy_key,'q':params,'limit':1})
    url = 'https://api.giphy.com/v1/gifs/search?%s' % args
    with urllib.request.urlopen(url) as req:
        meta = json.loads(req.read().decode('UTF-8'))
    if len(meta['data']) > 0:
        c.send('PRIVMSG',replyto,rest='%s %s'%(meta['data'][0]['bitly_gif_url'], meta['data'][0]['title']))

cmds = {}
cmds['ACCESS'] = (0,cmd_access)
cmds['QUIT'] = (100,cmd_quit)
cmds['JOIN'] = (100,cmd_join)
cmds['PART'] = (100,cmd_part)
cmds['SAY'] = (0,cmd_say)
cmds['DO'] = (0,cmd_do)
cmds['GIPHY'] = (0,cmd_giphy)

url_re = re.compile('(?:youtu.be\/|v\/|u\/\w\/|embed\/|watch\?v=)([^#\&\?]*)')
def hook_youtube(c,msg,replyto,text):
    if 'youtube.com' in text or 'youtu.be' in text:
        print('TRIGGERED')
        for url in url_re.finditer(text):  
            video_id = url.group(1)
            query_url = 'https://youtube.com/get_video_info?video_id=%s' % video_id
            with urllib.request.urlopen(query_url) as req:
                meta = urllib.parse.parse_qs(req.read())
            if b'view_count' in meta and b'title' in meta and b'avg_rating' in meta:
                title = meta[b'title'][0].decode('UTF-8')
                views = meta[b'view_count'][0]
                rating = meta[b'avg_rating'][0]
                c.send('PRIVMSG',replyto,rest='"%s" - %0.1f / 5.0 - %i views - https://youtu.be/%s'%(title,float(rating),int(views),video_id))

sed_re = re.compile('(?:(?:^|;)s(.)(.+?)\\1(.+?)\\1([gi0-9]*))+?;?')
sed_re_iter = re.compile('(?:^|;)s(.)(.+?)\\1(.+?)\\1([gi0-9]*)')
history = deque(maxlen=50)
def hook_sed(c,msg,replyto,text,action=False):
    match = sed_re.fullmatch(text)
    if match:
        messageidx = None
        for expr_match in sed_re_iter.finditer(text):
            _,expr,tmpl,flags = expr_match.groups()
            if messageidx is None:
                reexpr = re.compile(expr)
                for messageidx,text in enumerate(history):
                    if reexpr.search(text):
                        history[messageidx] = re.sub(expr,tmpl,text)
                        break
                else:
                    messageidx = None
            else:
                history[messageidx] = re.sub(expr,tmpl,history[messageidx])
        if messageidx is not None:
            c.send('PRIVMSG',replyto,rest=history[messageidx])
    else:
        if action:
            history.appendleft('* %s %s'%(strip_prefix(msg.prefix),text))
        else:
            history.appendleft('<%s> %s'%(strip_prefix(msg.prefix),text))
        
mc = markov.MarkovChain()
def hook_markov(c,msg,replyto,text):
    mc.process(text)

msg_hooks = [hook_youtube,hook_sed,hook_markov]

def privmsg(c,msg):
    src = strip_prefix(msg.prefix)
    dest,text = msg.args
    if dest[0] in ['#','&','$']:
        replyto = dest
    else:
        replyto = src
    if len(text) > 1:
        if text[0] == '\x01': #ctcp
            ctcp,*params = text.strip('\x01').split(' ',1)
            ctcp = ctcp.upper()
            if ctcp in ctcp_handlers:
                ctcp_handlers[ctcp](c,msg,replyto,params[0] if len(params) else None)
        elif text[0] == '.': #commands
            cmd,*params = text[1:].split(' ',1)
            cmd = cmd.upper()
            print(cmd,params)
            if cmd in cmds:
                req,handler = cmds[cmd]
                print(req)
                lvl = acl_level(src)
                print(lvl)
                if req <= lvl:
                    handler(c,msg,replyto,params[0] if len(params) > 0 else None)
        else: #regular messages
            for hook in msg_hooks:
                hook(c,msg,replyto,text)

def ping(c,msg):
    c.send('PONG',*msg.args)

def init(c,msg):
    c.send('JOIN','#letest')

handlers = {}
handlers['PING'] = ping
handlers['PRIVMSG'] = privmsg
handlers['001'] = init

c = IRCConnection('irc.rizon.net',7000)
while True:
    msg = c.recv()
    if msg.cmd in handlers:
        handlers[msg.cmd](c,msg)
