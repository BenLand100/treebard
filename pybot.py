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
import time
import ssl
import asyncio

from aiohttp import ClientSession
from concurrent.futures import ThreadPoolExecutor
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

    def __init__(self,filter_re_map={}):
        self.buff = ''
        self.pending = deque()
        self.last = {}
        self.throttle = {}
        self.filter_re_map = filter_re_map
        self.reader, self.writer = None,None
        
    async def connect(self,host,port,use_ssl=True,loop=None):
        if use_ssl:
            self.sc = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        else:
            self.sc = None
        self.reader, self.writer = await asyncio.open_connection(host, port, ssl=self.sc, loop=loop)
    
    async def send(self,cmd,*args,rest=None):
        cmd = cmd.upper()
        if cmd == 'PRIVMSG' or cmd == 'NOTICE':
            dest = args[0].upper()
            if dest in self.filter_re_map and self.filter_re_map[dest].search(rest):
                print('FF',cmd,args,rest)
                return
            if dest in self.last and self.last[dest] == rest:
                print('LL',cmd,args,rest)
                return
            else:
                self.last[dest] = rest
            if dest in self.throttle:
                throttle = self.throttle[dest]
                if len(throttle) == 5 and time.time() - throttle[4] <= 5:
                    print('TT',cmd,args,rest)
                    return
                else:
                    throttle.appendleft(time.time())
            else:
                self.throttle[dest] = deque(maxlen=5)
        if len(args) > 0:
            packet = '%s %s' % (cmd,' '.join(args))
        else:
            packet = cmd
        if rest:
            packet = '%s :%s' % (packet,rest)
        else:
            packet = '%s' % (packet)
        if len(packet) > 510:
            packet = packet[:510]
        print('<<',packet)
        packet = packet + '\r\n'
        self.writer.write(packet.encode('UTF-8'))
        await self.writer.drain()
        
    async def recv(self):
        if len(self.pending) > 0:
            return IRCMessage(self.pending.popleft())
        data = await self.reader.read(1024)
        self.buff = self.buff + data.decode('UTF-8',errors='ignore')
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
        
        self.mute = set() #put tokens for things to mute in here
        
        self.giphy_last = ''
        self.giphy_last_count = 0
        
        self.mc = None
        self.mc_learning = False
        self.mc_lock = asyncio.Lock()
        self.reply_prob = 0.01
        
    def __getstate__(self):
        state = self.__dict__.copy()
        del state['mc_lock']
        return state
        
    def __setstate__(self,state):
        self.__dict__.update(state)
        self.mc_lock = asyncio.Lock()
        
    def badword_tuple(self,word,style=''):
        word = word.lower()
        if style == '':
            return (word,style,word)
        elif style == 'single':
            return (word,style,'\s%s\s|\s%s$|^%s\s|^%s$' % (word,word,word,word))
        elif style == 'start':
            return (word,style,'\s%s|^%s' % (word,word))
        else:
            raise RuntimeError('unknown badword style')
        
    def add_badword(self,word,style=''):
        self.badwords.add(self.badword_tuple(word,style))
        
    def del_badword(self,word,style=''):
        self.badwords.remove(self.badword_tuple(word,style))
        
    def update_badwords(self,c):
        expr = '|'.join([regex for word,style,regex in self.badwords])
        key = self.name.upper()
        if len(expr) > 0:
            c.filter_re_map[key] = re.compile(expr,re.I)
        elif key in c.filter_re_map:
            del c.filter_re_map[key]
    
    def set_mute(self,token,muted=True):
        if 'mute' not in self.__dict__:
            self.mute = set()
        if muted:
            self.mute.add(token)
        else:
            self.mute.discard(token)
    
    def get_mute(self,token):
        if 'mute' not in self.__dict__:
            self.mute = set()
        return token in self.mute

class IRCBot:
    
    chan_prefix_chars = '#&$+!'

    def __init__(self,master=None,giphy_key=None,nick=None,ident=None,realname=None,autojoin=None):
        self.nick = nick
        self.ident = ident
        self.realname = realname
        self.autojoin = autojoin
    
        self.acl = {master.upper():1000} if master is not None else {}
        self.giphy_key = giphy_key
        self.chans = {}
        
        self.nn_temp = 0.7
        
        self.deferred_cmds = deque()
        self._default_handlers()
        
    def __getstate__(self):
        state = self.__dict__.copy()
        del state['handlers']
        del state['ctcp_handlers']
        del state['msg_hooks']
        del state['cmds']
        del state['deferred_cmds']
        del state['workers']
        return state
        
    def __setstate__(self,state):
        self.__dict__.update(state)
        self.deferred_cmds = deque()
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
        self.register_handler('352',self.handle_who)

        self.ctcp_handlers = {}
        self.register_ctcp_handler('VERSION',self.ctcp_version)
        self.register_ctcp_handler('PING',self.ctcp_ping)
        self.register_ctcp_handler('ACTION',self.ctcp_action)
            
        self.cmds = {}
        self.register_cmd('HELP',0,self.cmd_help)
        self.register_cmd('ACCESS',25,self.cmd_access)
        self.register_cmd('APPROVE',25,self.cmd_approve)
        self.register_cmd('QUIT',100,self.cmd_quit)
        self.register_cmd('RECONNECT',100,self.cmd_reconnect)
        self.register_cmd('JOIN',100,self.cmd_join)
        self.register_cmd('PART',100,self.cmd_part)
        self.register_cmd('SAY',0,self.cmd_say)
        self.register_cmd('DO',0,self.cmd_do)
        self.register_cmd('GIPHY',0,self.cmd_giphy)
        self.register_cmd('CHATTINESS',50,self.cmd_chattiness)
        self.register_cmd('PROFILE',50,self.cmd_profile)
        self.register_cmd('BADWORDS',75,self.cmd_badwords)
        #self.register_cmd('NN',0,self.cmd_nn)
        #self.register_cmd('NN-TEMP',10,self.cmd_nn_temp)
        self.register_cmd('MUTE',75,self.cmd_mute)
        self.register_cmd('UNMUTE',75,self.cmd_unmute)
        
        self.msg_hooks = []
        self.register_hook(self.hook_youtube)
        self.register_hook(self.hook_sed)
        self.register_hook(self.hook_markov)
        
    async def connect(self,host,port,loop=None,timeout=240):
        if not (self.nick and self.ident and self.realname):
            raise RuntimeError('must specify nick, ident, and realname to connect')
        if loop is None:
            loop = asyncio.get_event_loop()
        self.clean_exit = False
        self.workers = ThreadPoolExecutor(max_workers=4)
        conn = IRCConnection()
        await conn.connect(host,port)
        self.update_badwords(conn)
        await conn.send('NICK',self.nick)
        await conn.send('USER',self.ident,host,'*',rest=self.realname)
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(conn.recv(), timeout=timeout)
                except asyncio.TimeoutError:
                    return False
                if msg.cmd in self.handlers:
                    loop.create_task(self.handlers[msg.cmd](conn,msg))
                if msg.cmd == 'ERROR':
                    return self.clean_exit
        except:
            traceback.print_exc()
            return False
        
    async def _work_on(self,func,*args):
        return await asyncio.get_event_loop().run_in_executor(self.workers,func,*args)
    
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

    async def ctcp_version(self,c,msg,replyto,params):
        await c.send('NOTICE',replyto,rest='\x01VERSION %s\x01'%self.ident)

    async def ctcp_ping(self,c,msg,replyto,params):
        await c.send('NOTICE',replyto,rest='\x01PING %s\x01'%params)

    async def ctcp_action(self,c,msg,replyto,params):
        await self.hook_sed(c,msg,replyto,params,action=True)
        
    ### User commands
    
    async def cmd_help(self,c,msg,replyto,params):
        replyto = strip_prefix(msg.prefix)
        lvl = self.acl_level(replyto)
        avail = [cmd for cmd,(req,*_) in self.cmds.items() if lvl >= req]
        response = 'Avaliable commands: %s' % ', '.join(avail)
        await c.send('NOTICE',replyto,rest=response)

    async def cmd_reconnect(self,c,msg,replyto,params):
        self.clean_exit = False
        await c.send('QUIT',rest=(params if params else 'BRB'))
        
    async def cmd_quit(self,c,msg,replyto,params):
        self.clean_exit = True
        await c.send('QUIT',rest=(params if params else 'Leaving.'))

    async def cmd_join(self,c,msg,replyto,params):
        await c.send('JOIN',params)
        
    async def cmd_part(self,c,msg,replyto,params):
        chan_name = params if params else replyto
        self.get_chan(chan_name).joined = False
        await c.send('PART',chan_name)
        
    async def cmd_say(self,c,msg,replyto,params):
        if params:
            await c.send('PRIVMSG',replyto,rest=params)
            
    async def cmd_do(self,c,msg,replyto,params):
        if params:
            await c.send('PRIVMSG',replyto,rest='\x01ACTION %s\x01'%params)
            
    async def cmd_mute(self,c,msg,replyto,params):
        chan = self.get_chan(replyto)
        if params and len(params.strip()) > 0:
            params = params.strip().lower()
            chan.set_mute(params)
            await c.send('PRIVMSG',replyto,rest='Muted %s'%params)
        else:
            mutes = ', '.join(list(chan.mute))
            await c.send('NOTICE',strip_prefix(msg.prefix),rest='Current mutes for %s: %s'%(replyto,mutes))
            
    async def cmd_unmute(self,c,msg,replyto,params):
        if params:
            chan = self.get_chan(replyto)
            params = params.strip().lower()
            chan.set_mute(params,False)
            await c.send('PRIVMSG',replyto,rest='Unmuted %s'%params)
            
    async def cmd_badwords(self,c,msg,replyto,params):
        parts = deque(params.split() if params else [])
        if replyto[0] in IRCBot.chan_prefix_chars:
            chan_name = replyto
        else:
            chan_name = parts.popleft()
        chan = self.get_chan(chan_name)
        replyto = strip_prefix(msg.prefix)
        cmd = parts.popleft()
        if cmd == 'list':
            await c.send('NOTICE',replyto,rest='Badwords for %s' % chan_name)
            for word,style,_ in chan.badwords:
                if len(style) > 0:
                    line = '%s (%s)'%(word,style.upper())
                else:
                    line = '%s'%(word,)
                await c.send('NOTICE',replyto,rest=line)
        elif cmd == 'add':
            if len(parts) > 1:
                word,style = parts[0].lower(),parts[1].lower()
            else:
                word,style = parts[0].lower(),''
            chan.add_badword(word,style)
            chan.update_badwords(c)
        elif cmd == 'del':
            if len(parts) > 1:
                word,style = parts[0].lower(),parts[1].lower()
            else:
                word,style = parts[0].lower(),''
            chan.del_badword(word,style)
            chan.update_badwords(c)
        else:
            await c.send('NOTICE',replyto,rest='.badwords [channel] [list|add|del] [word]')
    
    async def cmd_profile(self,c,msg,replyto,params):
        chan = self.get_chan(replyto)
        params = params.strip() if params is not None else ''
        if len(params) == 0:
            if chan.mc and chan.mc_learning:
                await c.send('PRIVMSG',replyto,rest='Learning to chat')
        if params == 'disable':
            chan.mc = None
            chan.mc_learning = False
            await c.send('PRIVMSG',replyto,rest='Chatting deactivated')
        elif params == 'learn':
            chan.mc = markov.MarkovChain()
            chan.mc_learning = True
            await c.send('PRIVMSG',replyto,rest='Now chatting and learning')
        else:
            path = '%s.sqlite' % params.lower()
            if os.path.exists(path):
                chan.mc = markov.MarkovChain(path)
                chan.mc_learning = False
                await c.send('PRIVMSG',replyto,rest='Now chatting like %s' % params)
    
    async def cmd_chattiness(self,c,msg,replyto,params):
        chan = self.get_chan(replyto)
        if params is not None:
            params = params.strip()
            if len(params) > 0:
                chan.reply_prob = float(params)
        await c.send('PRIVMSG',replyto,rest='Reply probability set to %0.02f'%chan.reply_prob)
    
    async def cmd_nn_temp(self,c,msg,replyto,params):
        if params is not None:
            params = params.strip()
            if len(params) > 0:
                self.nn_temp = float(params)
        await c.send('PRIVMSG',replyto,rest='Neural network temperature set to %0.02f'%self.nn_temp)
    
    async def cmd_approve(self,c,msg,replyto,params):
        args = params.split() if params else ''
        if len(args) == 1:
            import srl_approve
            a = srl_approve.SRLApprove()
            result = await a.approve(args[0])
            if result:
                await c.send('PRIVMSG',replyto,rest='Approved %s'%(args[0]))
            else:
                await c.send('PRIVMSG',replyto,rest='Failed to approve %s'%(args[0]))
            
    async def cmd_access(self,c,msg,replyto,params):
        args = params.split() if params else ''
        if len(args) == 0:
            replyto = strip_prefix(msg.prefix)
            for nick,lvl in self.acl.items():
                await c.send('NOTICE',replyto,rest='%s %i'%(nick,lvl))
        if len(args) == 1:
            await c.send('PRIVMSG',replyto,rest='%s has access level %i'%(args[0],self.acl_level(args[0])))
        elif len(args) == 2:
            usrlvl = self.acl_level(strip_prefix(msg.prefix))
            setlvl = int(args[1])
            if setlvl < usrlvl:
                usrlvl = self.acl_level(args[0],setlvl)
                await c.send('PRIVMSG',replyto,rest='%s has access level %i'%(args[0],usrlvl))

    async def cmd_giphy(self,c,msg,replyto,params):
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
        async with ClientSession() as session:
            async with session.get(url) as resp:
                resp = await resp.read()
        meta = json.loads(resp.decode('UTF-8'))
        if len(meta['data']) > 0:
            await c.send('PRIVMSG',replyto,rest='%s %s'%(meta['data'][0]['images']['original']['url'], meta['data'][0]['title'].replace(' GIF','')))
    
    async def cmd_nn(self,c,msg,replyto,params):
        if not 'nn' in self.__dict__ or self.nn is None:
            try:
                def _load_nn():                
                    import nntextgen
                    self.nn = nntextgen.LanguageCenter(model='nn.h5')
                await self._work_on(_load_nn)
            except:
                print('can\'t load nntextgen module')
                self.nn = None
                raise 
        chan = self.get_chan(replyto)
        if not chan.get_mute('neural') and self.nn:
            def _generate():
                return self.nn.generate(params if params else '',temp=self.nn_temp,maxlen=250)
            text = await self._work_on(_generate)
            await c.send('PRIVMSG',replyto,rest=text)
    
    ### Text hooks

    url_re = re.compile('(?:youtu.be\/|v\/|u\/\w\/|embed\/|watch\?v=)([^#\&\?]*)')
    async def hook_youtube(self,c,msg,replyto,text):
        if 'youtube.com' in text or 'youtu.be' in text:
            chan = self.get_chan(replyto)
            if not chan.get_mute('youtube'):
                for url in IRCBot.url_re.finditer(text):  
                    video_id = url.group(1)
                    query_url = 'https://youtube.com/get_video_info?video_id=%s' % video_id
                    async with ClientSession() as session:
                        async with session.get(query_url) as resp:
                            resp = await resp.read()
                    meta = urllib.parse.parse_qs(resp.decode('UTF-8',errors='ignore'))
                    details = json.loads(meta['player_response'][0])['videoDetails']
                    title = details['title']
                    views = details['viewCount']
                    rating = details['averageRating']
                    await c.send('PRIVMSG',replyto,rest='"%s" - %0.1f / 5.0 - %i views - https://youtu.be/%s'%(title,float(rating),int(views),video_id))

    sed_re = re.compile('(?:(?:^|;)\s*s(.)((?:\\\\\\1|(?!\\1).)+?)\\1((?:\\\\\\1|(?!\\1).)*?)\\1([gi0-9]*)\s*)+?;?')
    sed_re_iter = re.compile('(?:^|;)\s*s(.)((?:\\\\\\1|(?!\\1).)+?)\\1((?:\\\\\\1|(?!\\1).)*?)\\1([gi0-9]*)\s*')
    flag_re = re.compile('g|i|[0-9]+')
    async def hook_sed(self,c,msg,replyto,text,action=False):
        match = IRCBot.sed_re.fullmatch(text)
        chan = self.get_chan(replyto)
        history = chan.history
        if not chan.get_mute('sed') and match:
            msg = ''
            msg_idx = None
            tentative = True
            for expr_match in IRCBot.sed_re_iter.finditer(text):
                _,expr,tmpl,flags = expr_match.groups()
                flags = IRCBot.flag_re.findall(flags.strip())
                reexpr = re.compile(expr,flags=re.IGNORECASE if 'i' in flags else 0)
                if msg_idx is None: #try to find this regex if no regex found
                    for msg_idx,msg in enumerate(history):
                        search = reexpr.search(msg)
                        if search:
                            break                                    
                    else:
                        msg_idx = None
                if msg_idx is not None: #if any regex has matched
                    if 'g' in flags:
                        msg = reexpr.sub(tmpl,msg)
                        tentative = False
                    else:
                        int_flags = [int(flag) for flag in flags if flag.isdigit()]
                        nth = 1 if len(int_flags) == 0 else int_flags[0]
                        search = reexpr.search(msg)
                        for i in range(nth-1):
                            search = reexpr.search(msg,search.end())
                            if search is None:
                                break
                        if search:
                            msg = msg[:search.start()] + reexpr.sub(tmpl,msg[search.start():],count=1)
                            tentative = False
                        elif tentative:
                            msg_idx = None
            if msg_idx is not None:
                history[msg_idx] = msg if len(msg) < 512 else msg[:512]
                await c.send('PRIVMSG',replyto,rest=msg)
        else:
            if action:
                history.appendleft('* %s %s'%(strip_prefix(msg.prefix),text))
            else:
                history.appendleft('<%s> %s'%(strip_prefix(msg.prefix),text))
            
    
    async def hook_markov(self,c,msg,replyto,text):
        chan = self.get_chan(replyto)
        if chan.mc is None:
            return
        if chan.mc_learning:
            async with chan.mc_lock:
                await self._work_on(chan.mc.process,text)
        if not chan.get_mute('markov'):
            if random.random() < chan.reply_prob or self.nick.upper() in text.upper():
                seed_text = re.sub(self.nick+'[;,: ]*|[<>\\/\|\?.,\(\)!@#\$\%^&\*]','',text,flags=re.IGNORECASE)
                ' '.join(set(seed_text.split()))
                reply = await self._work_on(chan.mc.gen_reply,seed_text)
                if reply:
                    await c.send('PRIVMSG',replyto,rest=reply)

    ### Raw message handlers
    cmd_re = re.compile('\\.(\\S+)')
    async def handle_privmsg(self,c,msg):
        src = strip_prefix(msg.prefix).upper()
        dest,text = msg.args
        if dest[0] in IRCBot.chan_prefix_chars:
            replyto = dest
        else:
            replyto = src
        if len(text) > 1:
            if text[0] == '\x01': #ctcp
                ctcp,*params = text.strip('\x01').split(' ',1)
                ctcp = ctcp.upper()
                if ctcp in self.ctcp_handlers:
                    try:
                        await self.ctcp_handlers[ctcp](c,msg,replyto,params[0] if len(params) else None)
                    except:
                        traceback.print_exc()
                return
                
            #check for commands after a preamble
            for match in IRCBot.cmd_re.finditer(text):
                cmd,*params = text[match.start(1):].split(' ',1)
                cmd = cmd.upper()
                if cmd in self.cmds:
                    req,handler = self.cmds[cmd]
                    lvl = self.acl_level(src)
                    if req <= lvl:
                        args = (c,msg,replyto,params[0] if len(params) else None)
                        if req > 0: #require identified nick 
                            self.deferred_cmds.append((src,handler,args))
                            await c.send('WHO',src)
                        else:
                            try:
                                await handler(*args)
                            except:
                                traceback.print_exc()
                    return
                    
            #regular messages
            for hook in self.msg_hooks:
                try:
                    await hook(c,msg,replyto,text)
                except:
                    traceback.print_exc()
    
    async def handle_who(self,c,msg):
        _,chan,user,host,server,nick,mode,rest = msg.args
        if len(self.deferred_cmds) > 0:
            nick = nick.upper()
            src,handler,args = self.deferred_cmds[0]
            if src == nick:
                self.deferred_cmds.popleft()
                if 'r' in mode:
                    try:
                        await handler(*args)
                    except:
                        traceback.print_exc()
    
    async def handle_join(self,c,msg):
        chan = self.get_chan(msg.args[0])
        who = strip_prefix(msg.prefix).upper()
        if who == self.nick.upper():
            chan.joined = True
        
    async def handle_part(self,c,msg):
        chan = self.get_chan(msg.args[0])
        who = strip_prefix(msg.prefix).upper()
        if who == self.nick.upper():
            chan.joined = False

    async def handle_quit(self,c,msg):
        chan = self.get_chan(msg.args[0])
        who = strip_prefix(msg.prefix).upper()
        if who == self.nick.upper():
            chan.joined = False
            
    async def handle_kick(self,c,msg):
        chan = self.get_chan(msg.args[0])
        who = strip_prefix(msg.prefix).upper()
        if who == self.nick.upper():
            chan.joined = False
            
    async def handle_ping(self,c,msg):
        await c.send('PONG',*msg.args)

    async def handle_init(self,c,msg):
        self.nick = msg.args[0]
        join_chans = set([chan for chan in self.chans.keys() if len(chan)>0 and chan[0] in IRCBot.chan_prefix_chars and self.chans[chan].joined])
        if self.autojoin:
            join_chans.update([chan.upper() for chan in self.autojoin.split(',')])
            self.autojoin = None
        if len(join_chans) > 0:
            for chan in join_chans:
                await c.send('JOIN',chan)
        
