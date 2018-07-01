import apsw
import os
import string
import nltk
from nltk.tokenize import TweetTokenizer

def create_ngram_table(c,depth):
    names = string.ascii_lowercase[:depth+1]
    c.execute('CREATE TABLE ngrams_%i(%s, count INTEGER);'%(depth,', '.join(['%s TEXT'%var for var in names])))
    c.execute('CREATE UNIQUE INDEX ngram_%i ON ngrams_%i(%s);'%(depth,depth,','.join(names)))

def add_statement(depth): 
    ngram = ','.join(['?']*(depth+1))
    clause = ' AND '.join(string.ascii_lowercase[:depth+1])
    return 'INSERT OR REPLACE INTO ngrams_%i VALUES (%s, COALESCE( (SELECT count FROM ngrams_%i WHERE %s), 0) + 1);' % (depth,ngram,depth,clause)
    
add_statements = [add_statement(depth) for depth in range(1,5)]
def add_ngrams(c,ngrams,commit=True):
    for igrams,statement in zip(ngrams,add_statements):
        for igram in igrams:
            c.execute(statement,igram)
    if commit:
        c.execute('COMMIT;')
    

class MarkovChain:
    def __init__(self,dbfile='markov.sqlite'):
        if os.path.exists(dbfile):
            self.conn = apsw.Connection(dbfile)
        else:
            self.conn = apsw.Connection(dbfile)
            c = self.conn.cursor()
            for depth in range(1,5):
                create_ngram_table(c,depth)
            c.execute('COMMIT;')
        self.tknzr = TweetTokenizer()

    def process(self,text):
        tokens = self.tknzr.tokenize(text)
        c = self.conn.cursor()
        maxlen = len(tokens)
        ngrams = [[tokens[i:i+j+1] for i in range(maxlen) if i+j+1 <= maxlen] for j in range(1,5)]
        add_ngrams(self.conn.cursor(),ngrams)

