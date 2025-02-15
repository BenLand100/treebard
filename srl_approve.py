#!/bin/env python3

import re
import sys
import json
import aiohttp
import asyncio

from aiohttp import ClientSession, BasicAuth

user_re = re.compile(r'.*;u=([0-9]+)".*<b>(.*)</b>')
security_re = re.compile(r'.*input type="hidden" name="([^"]+)" value="([^"]+)".*')

class SRLApprove:

    def __init__(self,creds_file='creds.json'):
        with open(creds_file) as cf:
            creds = json.load(cf)
            
        cp_user = creds['cp_user']
        cp_pass = creds['cp_pass']
        vb_user = creds['vb_user']
        vb_pass_md5 = creds['vb_pass_md5']
        self.fourm_loc = creds['forum_loc']
        
        self._basic_auth = BasicAuth(cp_user,cp_pass)
        self._login_data = {'logintype':'cplogin','do':'login',
            'vb_login_md5password':vb_pass_md5,'vb_login_md5password_utf':vb_pass_md5,
            'vb_login_username':vb_user,'vb_login_password':''}

    async def approve(self,approve_name):
        jar = aiohttp.CookieJar()
        async with aiohttp.ClientSession(cookie_jar=jar,auth=self._basic_auth) as s:
            async with s.post(self.fourm_loc+'/login.php?do=login',data=self._login_data) as r:
                await r.read()

            async with s.post(self.fourm_loc+'/adm/user.php') as r:
                user_page = await r.text()

            post_data = {m.group(1):m.group(2) for line in user_page.split('\n') if (m := security_re.match(line))}
            post_data['do'] = "find"
            post_data['user[exact]'] = 'Exact+Match'
            post_data['user[username]'] = approve_name

            async with s.post(self.fourm_loc+'/adm/user.php?do=find',data=post_data) as r:
                user_page = await r.text()

            post_data = {m.group(1):m.group(2) for line in user_page.split('\n') if (m := security_re.match(line))}

            existing_gid = post_data['ousergroupid']
            if existing_gid not in {
                '3', # Awaiting Email
                '4' # Awaiting Moderation
            }:
                return False # No other groups are valid targets

            post_data['do'] = 'update'
            post_data['user[usergroupid]'] = '2' # Registered User

            async with s.post(self.fourm_loc+'/adm/user.php?do=update',data=post_data) as r:
                response = await r.read()
                return b'Saved User <i></i> Successfully' in response

    async def moderate(self,approve_name): # This is fully broken on the VB side
        jar = aiohttp.CookieJar()
        async with aiohttp.ClientSession(cookie_jar=jar,auth=self._basic_auth) as s:
            async with s.post(self.fourm_loc+'/login.php?do=login',data=self._login_data) as r:
                await r.read()
                
            async with s.get(self.fourm_loc+'/adm/user.php?do=moderate') as r:
                moderate_page = await r.text()
            
            moderate_page = moderate_page.split('\n')
            users = {m.group(2).lower():m.group(1) for line in moderate_page if (m := user_re.match(line))}
            
            if (key:= approve_name.lower()) not in users:
                return False
            approve_id = users[key]
            print('%s is %s'%(approve_name,approve_id))
            
            post_data = {m.group(1):m.group(2) for line in moderate_page if (m := security_re.match(line))}
            post_data['send_deleted'] = 1
            post_data['send_validated'] = 1
            post_data['do'] = 'domoderate'
            for user,uid in users.items():
                post_data['validate[%s]'%uid] = '1' if uid == approve_id else '0'
                
            async with s.post(self.fourm_loc+'/adm/user.php?do=moderate',data=post_data) as r:
                response = await r.read()
                return b'User accounts validated and users notified.' in response
                
if __name__ == "__main__":
    a = SRLApprove()
    asyncio.get_event_loop().run_until_complete(a.approve(sys.args[1]))
