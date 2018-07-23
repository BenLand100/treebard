import apsw
import os
import re
import string
import nltk
import time
import random
import numpy as np
from nltk.tokenize import TweetTokenizer
#from nltk.tokenize.moses import MosesTokenizer
#from nltk.tokenize.moses import MosesDetokenizer

if hasattr(random,'choices'):
    choices = random.choices
else:
    def choices(seq,weights=None,k=1):
        if weights is None:
            return [random.choice(seq) for i in range(k)]
        else:
            weights = np.asarray(weights)
            cdf = np.cumsum(weights)
            np.insert(cdf,0,0)
            rnd = np.random.randint(cdf[-1],size=k)
            idx = np.digitize(rnd,cdf)
            return list(np.asarray(seq)[idx])

def drop_index(c,depth):
    c.execute('DROP INDEX ngram_%i;'%(depth,))

def create_index(c,depth,level=None):
    names = string.ascii_lowercase[:level if level else min(depth+1,4)]
    c.execute('CREATE UNIQUE INDEX ngram_%i ON ngrams_%i(%s);'%(depth,depth,','.join(names)))    

def create_ngram_table(c,depth):
    names = string.ascii_lowercase[:depth+1]
    c.execute('CREATE TABLE ngrams_%i(%s, count INTEGER);'%(depth,', '.join(['%s TEXT'%var for var in names])))
    create_index(c,depth,min(4,depth+1))
    
def add_statement(depth):
    ngram = ','.join(['?']*(depth+1))
    clause = ' AND '.join(['%s is ?' % name for name in string.ascii_lowercase[:depth+1]])
    return 'INSERT OR REPLACE INTO ngrams_%i VALUES (%s, COALESCE( (SELECT count FROM ngrams_%i WHERE %s), 0) + 1);' % (depth,ngram,depth,clause)
    
def get_statement(depth):
    ngram = ','.join(['?']*(depth+1))
    clause = ' AND '.join(['%s is ?' % name for name in string.ascii_lowercase[:depth]])
    last = string.ascii_lowercase[depth:depth+1]
    return 'SELECT %s,count FROM ngrams_%i WHERE %s;' % (last,depth,clause)
    
add_statements = [add_statement(depth) for depth in range(1,10)]
def add_ngrams(c,ngrams,commit=True):
    if commit:
        c.execute('BEGIN TRANSACTION;')
    for igrams,statement in zip(ngrams,add_statements):
        for igram in igrams:
            c.execute(statement,igram*2)
    if commit:
        c.execute('COMMIT;')    

get_statements = [get_statement(depth) for depth in range(1,10)]
def get_next(c,seed):
    depth = len(seed)
    return [(opt,count) for opt,count in c.execute(get_statements[depth-1],seed)]
    
    
class BasicTokenizer:
    def __init__(self):
        self.re = re.compile('https?://[^\s]+|\[[^\]]\]|[\w\d`#%\'-]+|['+string.punctuation+'\d]+[\w\d'+string.punctuation+']*',re.I)
    
    def tokenize(self,text):
        return self.re.findall(text)
        
def get_ngram(tokens,start,nlen,maxlen):
    if start+nlen <= maxlen-1 and start >= 0 :
        return tokens[start:start+nlen] 
    else:
        if start >= 0:
            return tokens[start:start+nlen-1]+[None]
        elif start+nlen > maxlen-1:
            return [None]+tokens+[None]
        else:
            return [None]+tokens[:nlen-1]

class MarkovChain:
    def __init__(self,dbfile='markov.sqlite'):
        self.dbfile = dbfile
        self._init()
        
    def _init(self):
        self.tknzr = BasicTokenizer()
        if os.path.exists(self.dbfile):
            self.conn = apsw.Connection(self.dbfile)
        else:
            self.conn = apsw.Connection(self.dbfile)
            c = self.conn.cursor()
            for depth in range(1,10):
                create_ngram_table(c,depth)
        self.txn = None
    
    def __getstate__(self):
        state = self.__dict__.copy()
        del state['txn']
        del state['conn']
        return state
        
    def __setstate__(self,state):
        self.__dict__.update(state)
        self._init()
        
    def begin(self,recreate_index=None):
        self.txn = self.conn.cursor()
        if recreate_index is not None:
            self.txn.execute('BEGIN TRANSACTION;')
            print('creating simplified index...')
            for depth in range(1,10):
                drop_index(self.txn,depth)
                create_index(self.txn,depth,level=min(recreate_index,depth+1))
            self.txn.execute('COMMIT;')
        self.txn.execute('BEGIN TRANSACTION;')

    def process(self,text,ngrams=8):
        tokens = self.tknzr.tokenize(text)
        c = self.conn.cursor() if self.txn is None else self.txn
        maxlen = len(tokens)+1
        ngrams = [[get_ngram(tokens,start,nlen,maxlen) for start in range(-1,maxlen-nlen+1)] for nlen in range(2,min(ngrams+1,maxlen+2))]
        add_ngrams(c,ngrams,commit=(self.txn is None))
        
    def commit(self,recreate_index=None):
        if self.txn:
            self.txn.execute('COMMIT;')
            if recreate_index is not None:
                self.txn.execute('BEGIN TRANSACTION;')
                print('regenerating full index...')
                for depth in range(1,10):
                    drop_index(self.txn,depth)
                    create_index(self.txn,depth,recreate_index)
                self.txn.execute('COMMIT;')
            self.txn = None
        
    def extend(self,seed,min_choices=2,start_depth=8,min_depth=2):
        if start_depth > len(seed):
            start_depth = len(seed)
        c = self.conn.cursor()
        for depth in range(start_depth,min_depth-1,-1):
            opts = get_next(c,seed[-depth:])
            if depth > 1 and len(opts) < min_choices:
                continue
            if depth == 1 and len(opts) == 0:
                return None
            weights = [weight for token,weight in opts]
            tokens = [token for token,weight in opts]
            return choices(tokens,weights)[0]
        return None
          
    def find_seed(self,tokens,min_choices=2,start_depth=8,min_depth=2):    
        c = self.conn.cursor()    
        for depth in range(start_depth,min_depth-1,-1):
            for attempt in range(50):
                seed = [None]+choices(tokens,k=depth-1)
                print(seed)
                opts = get_next(c,seed)
                if len(opts) > min_choices:
                    return seed
        return None
                      
    def gen_reply(self,text,min_seed_choices=3,min_extend_choices=2,start_depth=8,min_depth=1):
        tokens = self.tknzr.tokenize(text)
        if len(tokens) < 1:
            return None
        guesses = []
        for i in range(15):
            print('attempt',i)
            guess = self.find_seed(tokens,min_seed_choices,start_depth,3)
            if guess is None:
                continue
            while True:
                next = self.extend(guess,min_choices=min_extend_choices,start_depth=start_depth,min_depth=min_depth)
                print(next)
                if next:
                    guess.append(next)
                else:
                    break
            guesses.append(''.join([' '+i if not i.startswith("'") and i not in string.punctuation else i for i in guess if i]).strip())
        return sorted(guesses,key=len)[-1] if len(guesses) else None

