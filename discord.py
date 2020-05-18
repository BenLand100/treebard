#!/usr/bin/env python3

import os
import re
import json
import aiohttp
import asyncio
import requests
import traceback
import websockets
import srl_approve

from markov import MarkovChain
from aiohttp import ClientSession
from concurrent.futures import ThreadPoolExecutor

class DiscordConnection:
    def __init__(self):
        pass
        
    async def connect(self,bot_token,api_version=6):    
        gateway_url = 'https://discordapp.com/api/v%i/gateway/bot?encoding=%s'%(api_version,'json')
        headers = {'Authorization': 'Bot %s'%bot_token}
        async with aiohttp.ClientSession(headers=headers) as s:
            async with s.get(gateway_url) as r:
                gateway_msg = await r.json()
                
        self.ws = await websockets.connect(gateway_msg['url'])
    
    async def send(self,op='None',d='None',msg={}):
        if op != 'None':
            msg['op'] = op
        if d != 'None':
            msg['d'] = d
        msg = json.dumps(msg)
        print('<<',msg)
        await self.ws.send(msg)
        
    async def recv(self):
        msg = await self.ws.recv()
        #print('>>',msg)
        return json.loads(msg)



class Guild:
    #none of this is comprehensive
    
    def __init__(self,msg):
        self.name = msg['name']
        self.channels = {}
        for chan in msg['channels']:
            self.channel_add(chan)
        self.members = {}
        for memb in msg['members']:
            self.member_add(memb)
            
    def update(self,msg):
        self.name = msg['name']
            
    def channel_add(self,channel):
        self.channels[channel['id']] = channel['name']
            
    def channel_remove(self,channel):
        del self.channels[channel['id']]
    
    def member_add(self,member):
        user = member['user']
        display = member['nick'] if 'nick' in member and member['nick'] is not None else user['username']
        self.members[user['id']] = (user['username'],user['discriminator'],display)
        
    def member_update(self,member):
        self.member_add(member)
        
    def member_remove(self,member):
        del self.members[member['user']['id']]
        
    def get_member_name(self,user_id):
        return self.members[user_id][2] if user_id in self.members else user_id

    def get_channel_name(self,channel_id):
        return self.channels[channel_id] if channel_id in self.channels else channel_id
        
    user_re = re.compile(r'<@!?([^>]+)>')
    channel_re = re.compile(r'<#([^>]+)>')
    def to_text(self,content):
        def user_repl(match):
            return '@'+self.get_member_name(match.group(1))
        def channel_rep(match):
            return '#'+self.get_channel_name(match.group(1))
        return Guild.user_re.sub(user_repl,Guild.channel_re.sub(channel_rep,content))
        
        

class DiscordBot:
    def __init__(self,bot_token,master=None):
        self.bot_token = bot_token
        self.acl = {master.upper():1000} if master is not None else {}
        
        self._default_handlers()
        
        self.mc = MarkovChain()
        
        self.seq_num = None
        self.hb_every = -1
        self.hb_task = None
        
        self.ident = ('','','') #user,disc,nick
        self.ident_id = ''
        self.guilds = {}
        
        self.cmd_re = re.compile('\\.(\\S+)')
        
        self.approver = srl_approve.SRLApprove()
        
    def _default_handlers(self):
        self.handlers = {}
        self.register_handler(0,self.handle_event)  
        self.register_handler(1,self.handle_heartbeat)  
        self.register_handler(10,self.handle_hello)   
        self.register_handler(11,self.handle_heartbeat_ack)   
        self.register_handler(0,self.handle_event)  
        
        self.events = {}
        self.register_event('READY',self.ev_ready)  
        self.register_event('MESSAGE_CREATE',self.ev_message_create)
        self.register_event('GUILD_CREATE',self.ev_guild_create)
        self.register_event('GUILD_MEMBER_ADD',self.ev_guild_member_add)
        self.register_event('GUILD_MEMBER_REMOVE',self.ev_guild_member_remove)
        self.register_event('GUILD_MEMBER_UPDATE',self.ev_guild_member_update)
        self.register_event('PRESENCE_UPDATE',None)
        
        self.cmds = {}
        self.register_cmd('APPROVE',25,self.cmd_approve)
        self.register_cmd('ACCESS',25,self.cmd_access)
        
        self.msg_hooks = []
        self.register_hook(self.hook_markov)
    
    def __getstate__(self):
        state = self.__dict__.copy()
        del state['handlers']
        del state['events']
        del state['cmds']
        del state['msg_hooks']
        del state['workers']
        del state['hb_task']
        return state
        
    def __setstate__(self,state):
        self.__dict__.update(state)
        self._default_handlers()
        self.hb_task = None
        
    async def _work_on(self,func,*args):
        return await asyncio.get_event_loop().run_in_executor(self.workers,func,*args)
        
    async def _post(self,target,data={},api_version=6):
        async with ClientSession(headers={'Authorization':'Bot %s'%self.bot_token}) as session:
            async with session.post('https://discord.com/api/v'+str(api_version)+target, data=data) as resp:
                return await resp.json()
                
    async def _get(self,target,data={},api_version=6):
        async with ClientSession(headers={'Authorization':'Bot %s'%self.bot_token}) as session:
            async with session.get('https://discord.com/api/v'+str(api_version)+target, data=data) as resp:
                return await resp.json()
                
    def register_cmd(self,cmd,req,func):
        self.cmds[cmd.upper()] = (req,func)
        
    def register_hook(self,func):
        self.msg_hooks.append(func)
     
    def register_handler(self,op,func):
        self.handlers[op] = func
        
    def register_event(self,event,func):
        self.events[event] = func

    def acl_level(self,nick,newlvl=None):
        nick = nick.upper()
        if newlvl is not None:
            try:
                self.acl[nick] = int(newlvl)
            except:
                pass
        return self.acl[nick] if nick in self.acl else 0
        
    async def connect(self,api_version=6,loop=None):
        if loop is None:
            loop = asyncio.get_event_loop()
        conn = DiscordConnection()
        self.workers = ThreadPoolExecutor(max_workers=4)
        await conn.connect(self.bot_token,api_version=6)
        while True:
            msg = await conn.recv()
            self.seq_num = msg['s']
            if msg['op'] in self.handlers:
                loop.create_task(self.handlers[msg['op']](conn,msg))
           
        
    async def send_message(self,channel,content):
        return await self._post('/channels/%s/messages'%channel,{'content':content})
             
    async def send_heartbeat(self,ws):
        while True:
            self.heartbeat_ack = False
            await ws.send(op=1,d=self.seq_num)
            await asyncio.sleep(self.hb_every/1000.0)
            if not self.heartbeat_ack:
                print('missed heartbeat ack!')
           
    async def send_identify(self,ws):
        identify = {
            'token':self.bot_token,
            'properties': {'$os':'linux','$browser':'pybot','$device':'pybot'},
            'compress': False,
            'guild_subscriptions': True,
            'intents': 0x7FFF
            }
        await ws.send(2,identify)
           
    async def handle_hello(self,ws,msg):
        self.hb_every = msg['d']['heartbeat_interval']
        print('Heartbeat interval:',self.hb_every,'ms')
        if self.hb_task:
            self.hb_task.cancel()
        self.hb_task = asyncio.get_event_loop().create_task(self.send_heartbeat(ws))
        await self.send_identify(ws)
    
    async def handle_heartbeat_ack(self,ws,msg):
        self.heartbeat_ack = True
    
    async def handle_heartbeat(self,ws,msg):   
        await ws.send(op=11)
    
    async def handle_event(self,ws,msg):
        ev = msg['t']
        if ev in self.events:
            handler = self.events[ev]
            if handler:
                await handler(ws,msg['d'])
        else:
            print('>>',ev)
    
    async def ev_ready(self,ws,msg):
        me = await self._get('/users/@me')
        self.ident = (me['username'],me['discriminator'],me['username'])
        self.ident_id = me['id']
        expr = r'(?:<?@?!?%s>?|%s) ?\:?(\S+)'%(self.ident_id,self.ident[2])
        self.cmd_re = re.compile(expr)
        
    async def ev_guild_create(self,ws,msg):
        guild = Guild(msg)
        self.guilds[msg['id']] = guild
        print('Created:',guild.name)
        print('\t',len(guild.channels),' channels')
        print('\t',len(guild.members),' members')
    
    async def ev_guild_member_add(self,wbs,msg):
        if msg['guild_id'] in self.guilds:
            self.guilds[msg['guild_id']].member_add(msg)
            
    async def ev_guild_member_remove(self,wbs,msg):
        if msg['guild_id'] in self.guilds:
            self.guilds[msg['guild_id']].member_remove(msg)
            
    async def ev_guild_member_update(self,wbs,msg):
        if msg['guild_id'] in self.guilds:
            self.guilds[msg['guild_id']].member_update(msg)
    
    async def ev_message_create(self,ws,msg):
        if msg['type'] != 0:
            print('what is this message type: ',msg)
            return
        guild_id = msg['guild_id']
        channel_id = msg['channel_id']
        author_id = msg['author']['id']
        guild = self.guilds[guild_id] if guild_id in self.guilds else None
        if guild is None:
            print('what is this message: ',msg)
            return
        channel = guild.channels[channel_id]
        if author_id not in guild.members:
            print('who is this user: ', msg['author'])
            return
        author = guild.members[author_id]
        content = msg['content']
        timestamp = msg['timestamp']
        text = guild.to_text(content)
        
        print('#%s <%s (%s#%s)> : %s'%(channel,author[2],author[0],author[1],text))
            
        #check for commands after a preamble
        for match in self.cmd_re.finditer(content):
            cmd,*params = content[match.start(1):].split(' ',1)
            cmd = cmd.upper()
            if cmd in self.cmds:
                req,handler = self.cmds[cmd]
                lvl = self.acl_level(author_id)
                if req <= lvl:
                    args = (guild,channel_id,author_id,params[0] if len(params) else None)
                    try:
                        await handler(*args)
                    except:
                        traceback.print_exc()
                return
                
        #regular messages
        for hook in self.msg_hooks:
            try:
                await hook(guild,channel_id,author_id,text)
            except:
                traceback.print_exc()
    
    async def cmd_access(self,guild,channel_id,author_id,args):
        args = args.split() if args else ''
        if len(args) == 0:
            for user_id,lvl in self.acl.items():
                await self.send_message(channel_id,'%s %i'%(guild.get_member_name(user_id),lvl))
        elif len(args) == 1:
            user_id = re.sub(r'<@!?(.*)>',r'\1',args[0])
            await self.send_message(channel_id,'<@!%s> has access level %i'%(user_id,self.acl_level(user_id)))
        elif len(args) == 2:
            user_id = re.sub(r'<@!?(.*)>',r'\1',args[0])
            usrlvl = self.acl_level(author_id)
            setlvl = int(args[1])
            if setlvl < usrlvl:
                usrlvl = self.acl_level(user_id,setlvl)
                await self.send_message(channel_id,'<@!%s> has access level %i'%(user_id,usrlvl))
                
    async def cmd_approve(self,guild,channel_id,author_id,args):
        args = args.strip()
        if len(args) > 0:
            await self.send_message(channel_id,'Approving %s for <@!%s>'%(args,author_id))
            await self.approver.approve(args)
    
    async def hook_markov(self,guild,channel_id,author_id,text):
        text = re.sub(r'^\*\*<.+>\*\* *','',text)
        await self._work_on(self.mc.process,text)
        if self.ident[2].upper() in text.upper():
            seed_text = re.sub(self.ident[2]+'[;,: ]*|[<>\\/\|\?.,\(\)!@#\$\%^&\*]','',text,flags=re.IGNORECASE)
            ' '.join(set(seed_text.split()))
            reply = await self._work_on(self.mc.gen_reply,seed_text)
            if reply:
                await self.send_message(channel_id,reply)
