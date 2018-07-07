import apsw
import os
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
            

def create_ngram_table(c,depth):
    names = string.ascii_lowercase[:depth+1]
    c.execute('CREATE TABLE ngrams_%i(%s, count INTEGER);'%(depth,', '.join(['%s TEXT'%var for var in names])))
    c.execute('CREATE UNIQUE INDEX ngram_%i ON ngrams_%i(%s);'%(depth,depth,','.join(names)))

def add_statement(depth):
    ngram = ','.join(['?']*(depth+1))
    clause = ' AND '.join(['%s==?' % name for name in string.ascii_lowercase[:depth+1]])
    return 'INSERT OR REPLACE INTO ngrams_%i VALUES (%s, COALESCE( (SELECT count FROM ngrams_%i WHERE %s), 0) + 1);' % (depth,ngram,depth,clause)
    
def get_statement(depth):
    ngram = ','.join(['?']*(depth+1))
    clause = ' AND '.join(['%s==?' % name for name in string.ascii_lowercase[:depth]])
    last = string.ascii_lowercase[depth:depth+1]
    return 'SELECT %s,count FROM ngrams_%i WHERE %s;' % (last,depth,clause)
    
add_statements = [add_statement(depth) for depth in range(1,5)]
def add_ngrams(c,ngrams,commit=True):
    if commit:
        c.execute('BEGIN TRANSACTION;')
    for igrams,statement in zip(ngrams,add_statements):
        for igram in igrams:
            c.execute(statement,igram*2)
    if commit:
        c.execute('COMMIT;')    

get_statements = [get_statement(depth) for depth in range(1,5)]
def get_next(c,seed):
    depth = len(seed)
    return [(opt,count) for opt,count in c.execute(get_statements[depth-1],seed)]
    

class MarkovChain:
    def __init__(self,dbfile='markov.sqlite'):
        self.dbfile = dbfile
        self._init()
        
    def _init(self):
        self.tknzr = TweetTokenizer()
        if os.path.exists(self.dbfile):
            self.conn = apsw.Connection(self.dbfile)
        else:
            self.conn = apsw.Connection(self.dbfile)
            c = self.conn.cursor()
            for depth in range(1,5):
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
        
    def begin(self):
        self.txn = self.conn.cursor()
        self.txn.execute('BEGIN TRANSACTION;')

    def process(self,text):
        start = time.time()
        tokens = self.tknzr.tokenize(text)
        c = self.conn.cursor() if self.txn is None else self.txn
        maxlen = len(tokens)
        ngrams = [[(tokens[i:i+j+1] if i+j+1 <= maxlen else tokens[i:i+j]+[None])for i in range(maxlen+1-j)] for j in range(1,5)]
        add_ngrams(self.conn.cursor(),ngrams,commit=(self.txn is None))
        end = time.time()
        
    def commit(self):
        if self.txn:
            self.txn.execute('COMMIT;')
            self.txn = None
        
    def extend(self,seed,min_choices=2,start_depth=4):
        if start_depth > len(seed):
            start_depth = len(seed)
        c = self.conn.cursor()
        for depth in range(start_depth,0,-1):
            opts = get_next(c,seed[-depth:])
            if depth > 1 and len(opts) < min_choices:
                continue
            if depth == 1 and len(opts) == 0:
                return None
            weights = [weight for token,weight in opts]
            tokens = [token for token,weight in opts]
            return choices(tokens,weights)[0]
        return None
          
    def find_seed(self,tokens,min_choices=2,start_depth=4):    
        c = self.conn.cursor()    
        for depth in range(start_depth,0,-1):
            for attempt in range(50):
                seed = choices(tokens,k=depth)
                print(seed)
                opts = get_next(c,seed)
                if len(opts) > min_choices:
                    return seed
        return None
                      
    def gen_reply(self,text,min_choices=3,start_depth=4):
        tokens = self.tknzr.tokenize(text)
        if len(tokens) < 1:
            return None
        seed = self.find_seed(tokens,min_choices,start_depth)
        if seed is None:
            return None
        guesses = []
        for i in range(50):
            guess = seed
            while True:
                next = self.extend(guess)
                print(next)
                if next:
                    guess.append(next)
                else:
                    break
            guesses.append(''.join([' '+i if not i.startswith("'") and i not in string.punctuation else i for i in guess]).strip())
        return sorted(guesses,key=len)[-1]

