import re
import os
import json
import socket
import string
import urllib.parse
import urllib.request
import markov
import random
import traceback

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

    def __init__(self,host,port,filter_re_map={},timeout=None):
        self.buff = ''
        self.pending = deque()
        self.last = {}
        self.filter_re_map = filter_re_map
    
        self.s = socket.socket()
        self.s.connect((host, port))
        if timeout is not None:        
            self.s.settimeout(timeout)
    
    def send(self,cmd,*args,rest=None):
        cmd = cmd.upper()
        if cmd == 'PRIVMSG' or cmd == 'NOTICE':
            dest = args[0].upper()
            if dest in self.last and self.last[dest] == rest:
                print('XX',cmd,args,rest)
                return
            self.last[dest] = rest
            if dest in self.filter_re_map and self.filter_re_map[dest].search(rest):
                print('XX',cmd,args,rest)
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
        self.buff = self.buff + self.s.recv(1024).decode('UTF-8',errors='ignore')
        parts = self.buff.split('\r\n')
        self.buff = parts.pop() 
        self.pending.extend(parts)
        return IRCMessage(self.pending.popleft())
        
def strip_prefix(prefix):
    if prefix.find('!') != -1:
        nick,*_ = prefix.split('!')
        return nick
    return prefix
    
class IRCChannel:

    def __init__(self,name):
        self.name = name
        self.joined = False
        
        self.badwords = set()
        
        self.history = deque(maxlen=100)
        
        self.giphy_last = ''
        self.giphy_last_count = 0
        
        self.mc = None
        self.mc_learning = False
        self.reply_prob = 0.01
        
    def add_badword(self,word,style=''):
        word = word.lower()
        if style == '':
            self.badwords.add(word)
        elif style == 'single':
            self.badwords.add('\s%s\s|\s%s$|^%s\s|^%s$' % (word,word,word,word))
        elif style == 'start':
            self.badwords.add('\s%s|^%s' % (word,word))
        
    def update_badwords(self,c):
        expr = '|'.join(self.badwords)
        key = self.name.upper()
        print(key,expr) 
        if len(expr) > 0:
            c.filter_re_map[key] = re.compile(expr)
        elif key in c.filter_re_map:
            del c.filter_re_map[key]

class IRCBot:

    def __init__(self,master=None,giphy_key=None,nick=None,ident=None,realname=None,autojoin=None):
        self.nick = nick
        self.ident = ident
        self.realname = realname
        self.autojoin = autojoin
    
        self.acl = {master.upper():1000} if master is not None else {}
        self.giphy_key = giphy_key
        self.chans = {}

        self._default_handlers()
        
    def __getstate__(self):
        state = self.__dict__.copy()
        del state['handlers']
        del state['ctcp_handlers']
        del state['msg_hooks']
        del state['cmds']
        return state
        
    def __setstate__(self,state):
        self.__dict__.update(state)
        self._default_handlers()
        
    def update_badwords(self,conn):
        for chan in self.chans.values():
            chan.update_badwords(conn)
        
    def get_chan(self,chan,create=True):
        key = chan.upper()
        if key in self.chans:
            return self.chans[key]
        elif create:
            self.chans[key] = IRCChannel(chan)
            return self.chans[key]
        return None
        
    def _default_handlers(self):
        self.handlers = {}
        self.register_handler('PING',self.handle_ping)
        self.register_handler('PRIVMSG',self.handle_privmsg)
        self.register_handler('JOIN',self.handle_join)
        self.register_handler('PART',self.handle_part)
        self.register_handler('KICK',self.handle_kick)
        self.register_handler('QUIT',self.handle_quit)
        self.register_handler('001',self.handle_init)

        self.ctcp_handlers = {}
        self.register_ctcp_handler('VERSION',self.ctcp_version)
        self.register_ctcp_handler('PING',self.ctcp_ping)
        self.register_ctcp_handler('ACTION',self.ctcp_action)
            
        self.cmds = {}
        self.register_cmd('HELP',0,self.cmd_help)
        self.register_cmd('ACCESS',10,self.cmd_access)
        self.register_cmd('QUIT',100,self.cmd_quit)
        self.register_cmd('JOIN',100,self.cmd_join)
        self.register_cmd('PART',100,self.cmd_part)
        self.register_cmd('SAY',0,self.cmd_say)
        self.register_cmd('DO',0,self.cmd_do)
        self.register_cmd('GIPHY',0,self.cmd_giphy)
        self.register_cmd('CHATTINESS',50,self.cmd_chattiness)
        self.register_cmd('PROFILE',50,self.cmd_profile)
        
        self.msg_hooks = []
        self.register_hook(self.hook_youtube)
        self.register_hook(self.hook_sed)
        self.register_hook(self.hook_markov)
        
    def connect(self,host,port,timeout=None):
        if not (self.nick and self.ident and self.realname):
            raise RuntimeError('must specify nick, ident, and realname to connect')
        conn = IRCConnection(host,port,timeout=timeout)
        self.update_badwords(conn)
        conn.send('NICK',self.nick)
        conn.send('USER',self.ident,host,'*',rest=self.realname)
        try:
            while True:
                msg = conn.recv()
                if msg.cmd in self.handlers:
                    self.handlers[msg.cmd](conn,msg)
                if msg.cmd == 'ERROR':
                    return True
        except:
            traceback.print_exc()
            return False
        
    def register_handler(self,cmd,func):
        self.handlers[cmd.upper()] = func
        
    def register_ctcp_handler(self,cmd,func):
        self.ctcp_handlers[cmd.upper()] = func
        
    def register_cmd(self,cmd,req,func):
        self.cmds[cmd.upper()] = (req,func)
        
    def register_hook(self,func):
        self.msg_hooks.append(func)
    
    def acl_level(self,nick,newlvl=None):
        nick = nick.upper()
        if newlvl is not None:
            try:
                self.acl[nick] = int(newlvl)
            except:
                pass
        return self.acl[nick] if nick in self.acl else 0
        
    ### CTCP handlers

    def ctcp_version(self,c,msg,replyto,params):
        c.send('NOTICE',replyto,rest='\x01VERSION %s\x01'%self.ident)

    def ctcp_ping(self,c,msg,replyto,params):
        c.send('NOTICE',replyto,rest='\x01PING %s\x01'%params)

    def ctcp_action(self,c,msg,replyto,params):
        self.hook_sed(c,msg,replyto,params,action=True)
        
    ### User commands
    
    def cmd_help(self,c,msg,replyto,params):
        replyto = strip_prefix(msg.prefix)
        lvl = self.acl_level(replyto)
        avail = [cmd for cmd,(req,*_) in self.cmds.items() if lvl >= req]
        response = 'Avaliable commands: %s' % ', '.join(avail)
        c.send('NOTICE',replyto,rest=response)

    def cmd_quit(self,c,msg,replyto,params):
        c.send('QUIT',rest=(params if params is not None else 'Leaving.'))

    def cmd_join(self,c,msg,replyto,params):
        c.send('JOIN',params)
        
    def cmd_part(self,c,msg,replyto,params):
        c.send('PART',params if params is not None else replyto)
        
    def cmd_say(self,c,msg,replyto,params):
        if params is not None:
            c.send('PRIVMSG',replyto,rest=params)
            
    def cmd_do(self,c,msg,replyto,params):
        if params is not None:
            c.send('PRIVMSG',replyto,rest='\x01ACTION %s\x01'%params)
    
    def cmd_profile(self,c,msg,replyto,params):
        chan = self.get_chan(replyto)
        params = params.strip() if params is not None else ''
        if len(params) == 0:
            chan.mc = None
            chan.mc_learning = False
            c.send('PRIVMSG',replyto,rest='Chatting deactivated')
        elif params == 'learn':
            chan.mc = markov.MarkovChain()
            chan.mc_learning = True
            c.send('PRIVMSG',replyto,rest='Now chatting and learning')
        else:
            path = '%s.sqlite' % params.lower()
            if os.path.exists(path):
                chan.mc = markov.MarkovChain(path)
                chan.mc_learning = False
                c.send('PRIVMSG',replyto,rest='Now chatting like %s' % params)
    
    def cmd_chattiness(self,c,msg,replyto,params):
        chan = self.get_chan(replyto)
        chan.reply_prob = float(params)
        c.send('PRIVMSG',replyto,rest='Reply probability set to %0.02f'%chan.reply_prob)
            
    def cmd_access(self,c,msg,replyto,params):
        if params is None:
            return
        args = params.split()
        if len(args) == 1:
            c.send('PRIVMSG',replyto,rest='%s has access level %i'%(args[0],self.acl_level(args[0])))
        elif len(args) == 2:
            usrlvl = self.acl_level(strip_prefix(msg.prefix))
            setlvl = int(args[1])
            if setlvl < usrlvl:
                usrlvl = self.acl_level(args[0],setlvl)
                c.send('PRIVMSG',replyto,rest='%s has access level %i'%(args[0],usrlvl))

    def cmd_giphy(self,c,msg,replyto,params):
        if self.giphy_key is None:
            return
        chan = self.get_chan(replyto)
        if params == chan.giphy_last:
            chan.giphy_last_count = chan.giphy_last_count + 1
            args = urllib.parse.urlencode({'api_key':self.giphy_key,'q':params,'limit':1,'offset':chan.giphy_last_count})
        else:
            chan.giphy_last = params
            args = urllib.parse.urlencode({'api_key':self.giphy_key,'q':params,'limit':1})
        url = 'https://api.giphy.com/v1/gifs/search?%s' % args
        with urllib.request.urlopen(url) as req:
            meta = json.loads(req.read().decode('UTF-8'))
        if len(meta['data']) > 0:
            c.send('PRIVMSG',replyto,rest='%s %s'%(meta['data'][0]['images']['original']['url'], meta['data'][0]['title'].replace(' GIF','')))
    
    ### Text hooks

    url_re = re.compile('(?:youtu.be\/|v\/|u\/\w\/|embed\/|watch\?v=)([^#\&\?]*)')
    def hook_youtube(self,c,msg,replyto,text):
        if 'youtube.com' in text or 'youtu.be' in text:
            for url in IRCBot.url_re.finditer(text):  
                video_id = url.group(1)
                query_url = 'https://youtube.com/get_video_info?video_id=%s' % video_id
                with urllib.request.urlopen(query_url) as req:
                    meta = urllib.parse.parse_qs(req.read())
                if b'view_count' in meta and b'title' in meta and b'avg_rating' in meta:
                    title = meta[b'title'][0].decode('UTF-8')
                    views = meta[b'view_count'][0]
                    rating = meta[b'avg_rating'][0]
                    c.send('PRIVMSG',replyto,rest='"%s" - %0.1f / 5.0 - %i views - https://youtu.be/%s'%(title,float(rating),int(views),video_id))

    sed_re = re.compile('(?:(?:^|;)s(.)(.+?)\\1(.*?)\\1([gi0-9]*))+?;?')
    sed_re_iter = re.compile('(?:^|;)s(.)(.+?)\\1(.*?)\\1([gi0-9]*)')
    def hook_sed(self,c,msg,replyto,text,action=False):
        match = IRCBot.sed_re.fullmatch(text)
        chan = self.get_chan(replyto)
        history = chan.history
        if match:
            messageidx = None
            for expr_match in IRCBot.sed_re_iter.finditer(text):
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
            
    
    def hook_markov(self,c,msg,replyto,text):
        chan = self.get_chan(replyto)
        if chan.mc is None:
            return
        if chan.mc_learning:
            chan.mc.process(text)
        if random.random() < chan.reply_prob or self.nick.upper() in text.upper():
            seed_text = strip_prefix(msg.prefix) + ' ' + re.sub(self.nick+'[;,: ]*|<\w+>','',text,flags=re.IGNORECASE)
            reply = chan.mc.gen_reply(seed_text)
            if reply:
                c.send('PRIVMSG',replyto,rest=reply)

    ### Raw message handlers

    def handle_privmsg(self,c,msg):
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
                if ctcp in self.ctcp_handlers:
                    try:
                        self.ctcp_handlers[ctcp](c,msg,replyto,params[0] if len(params) else None)
                    except:
                        traceback.print_exc()
            elif text[0] == '.': #commands
                cmd,*params = text[1:].split(' ',1)
                cmd = cmd.upper()
                if cmd in self.cmds:
                    req,handler = self.cmds[cmd]
                    lvl = self.acl_level(src)
                    if req <= lvl:
                        try:
                            handler(c,msg,replyto,params[0] if len(params) > 0 else None)
                        except:
                            traceback.print_exc()
            else: #regular messages
                for hook in self.msg_hooks:
                    try:
                        hook(c,msg,replyto,text)
                    except:
                        traceback.print_exc()

    def handle_join(self,c,msg):
        chan = self.get_chan(msg.args[0])
        who = strip_prefix(msg.prefix).upper()
        if who == self.nick.upper():
            chan.joined = True
        
    def handle_part(self,c,msg):
        chan = self.get_chan(msg.args[0])
        who = strip_prefix(msg.prefix).upper()
        if who == self.nick.upper():
            chan.joined = False

    def handle_quit(self,c,msg):
        chan = self.get_chan(msg.args[0])
        who = strip_prefix(msg.prefix).upper()
        if who == self.nick.upper():
            chan.joined = False
            
    def handle_kick(self,c,msg):
        chan = self.get_chan(msg.args[0])
        who = strip_prefix(msg.prefix).upper()
        if who == self.nick.upper():
            chan.joined = False
            
    def handle_ping(self,c,msg):
        c.send('PONG',*msg.args)

    def handle_init(self,c,msg):
        self.nick = msg.args[0]
        print(list(self.chans.keys()))
        join_chans = set([chan for chan in self.chans.keys() if chan[0] in ['#','&','$'] and self.chans[chan].joined])
        if self.autojoin:
            join_chans.update([chan.upper() for chan in self.autojoin.split(',')])
        if len(join_chans) > 0:
            arg = ','.join(join_chans)
            c.send('JOIN',arg)
        
