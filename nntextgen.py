import os
import numpy as np
import tensorflow as tf

from keras import backend as K; 
K.set_session(K.tf.Session(config=K.tf.ConfigProto(intra_op_parallelism_threads=4, inter_op_parallelism_threads=4)))

from keras.layers import Input, Embedding, LSTM, Dense, Flatten, concatenate
from keras.models import Model, load_model
from keras.callbacks import ModelCheckpoint
from keras.utils import np_utils

c_start = 96
c_stop = 97
c_size = 97

def c2i(c):
    if len(c) != 1:
        return None
    i = ord(c)
    return i-32+1 if i >= 32 and i <= 126 else None

def i2c(i):
    return chr(i+32-1) if i >= 1 and i <= 95 else ''
        
def get_text(msg):
    return ''.join([i2c(i-1) for i in msg])

def i2state(i):
    state = np.zeros(c_size)
    state[i-1] = 1
    return state
    
def state2i(state):
    return np.argmax(state)+1
    
def sample_state(state,temperature=0.2):
    state = np.asarray(state).astype('float64')
    state = np.log(state) / temperature
    exp_state = np.exp(state)
    state = exp_state / np.sum(exp_state)
    probs = np.random.multinomial(1, state, 1)
    return i2c(np.argmax(probs)+1)
    
def encode_message(msg,seed=False,ngram_size=None):
    msg = [c_start]+[c2i(c) for c in msg if c2i(c) is not None]
    if not seed:
        msg.append(c_stop)
    if ngram_size is not None:
        if len(msg)>ngram_size:
            msg = msg[:ngram_size]
        else:
            msg += [0]*(ngram_size-len(msg))
    return np.asarray(msg)
    
def extract_ngram(vec,i,length,val):
    if i+length <= len(vec):
        return vec[i:i+length]
    else:
        return vec[i:]+[val]*(length-len(vec)+i)
    
def ngram_iter(text,ngram_size=None,stride=1):
    if ngram_size is None:
        ints = encode_message(text)
        yield ints
    elif ngram_size < 0:
        ints = encode_message(text)
        yield from (ints[:i+2] for i in range(len(ints)-1))
    else:
        ints = encode_message(text)
        yield from (extract_ngram(ints,i,ngram_size,c_stop) for i in range(0,len(ints)+1-ngram_size,stride))
        
class LanguageCenter:
    def __init__(self,vocab_size=c_size,embedding_space=100,lstm_space=500,lstm_depth=4,dense_size=500,dense_depth=2,model='neural.h5'):
        self.model_name = model
        self.vocab_size = vocab_size
        self.embedding_space = embedding_space
        self.lstm_space = lstm_space
        
        if os.path.exists(model):
            self.model = load_model(self.model_name)
            self._thread_init()
            return
        
        ngram_input = Input(shape=(None,), name='ngram_input')
        embedding = Embedding(output_dim=embedding_space,input_dim=vocab_size+1,input_length=None)(ngram_input)
        prev_layer = embedding
        lstm_layers = []
        assert lstm_depth > 0, 'need at least one LSTM layer'
        for i in range(lstm_depth):
            prev_layer = LSTM(lstm_space, return_sequences=(i!=lstm_depth-1), return_state=(i==lstm_depth-1), name=('lstm_%i'%i))(prev_layer)
            if isinstance(prev_layer,list):
                prev_layer,state_h,state_c = prev_layer
            lstm_layers.append(prev_layer)
        state_history = concatenate([prev_layer,state_h,state_c],name='state_history')
        prev_layer = state_history
        dense_layers = []
        assert dense_depth > 0, 'need at least one Dense layer'
        for i in range(dense_depth):
            prev_layer = Dense(lstm_space, name=('dense_%i'%i), activation='tanh')(prev_layer)
            dense_layers.append(prev_layer)
        output = Dense(vocab_size, name='letter_out', activation='softmax')(prev_layer)
        self.model = Model(inputs=[ngram_input], outputs=[output])
        self.model.compile(loss='categorical_crossentropy', optimizer='adam')
        self._thread_init()
        
    def __getstate__(self):
        state = self.__dict__.copy()
        del state['model']
        del state['graph']
        self.model.save(self.model_name)
        return state
        
    def __setstate__(self,state):
        self.__dict__.update(state)
        self.model = load_model(self.model_name)
        self._thread_init()
        
    def _thread_init(self):
        self.model._make_predict_function()
        self.graph = tf.get_default_graph()        

    def train_from_gen(self,gen,stride=1,batch=5000,mini_batch=32,test_seed=None,ngram_size=50,skip=0):
        if test_seed is not None:
            print(self.generate(test_seed))
        if ngram_size > 0:
            x,y = [],[]
            skipping = skip!=0 if skip is not None else False
            for imsg,msg in enumerate(gen):
                for ngram in ngram_iter(msg,ngram_size+1,stride):
                    if len(ngram) != ngram_size+1:
                        continue
                    x.append(ngram[:-1])
                    y.append(i2state(ngram[-1]))
                    if len(x) == batch:
                        if imsg > skip:
                            skipping = False
                        if not skipping:
                            self.model.fit(x,y,batch_size=mini_batch)
                        x,y = [],[]
                        if not skipping and test_seed is not None:
                            print(self.generate(test_seed))
        else:
            max_ngram_size = -ngram_size
            depth = []
            for i in range(max_ngram_size):
                depth.append(([],[]))
            skipping = skip!=0 if skip is not None else False
            for imsg,msg in enumerate(gen):
                for ngram in ngram_iter(msg,ngram_size,stride):
                    if len(ngram) < 5 or len(ngram) > max_ngram_size:
                        continue
                    x,y = depth[len(ngram)-1]
                    x.append(ngram[:-1])
                    y.append(i2state(ngram[-1]))
                    if len(x) == batch:
                        if imsg > skip:
                            skipping = False
                        if not skipping:
                            print('running ngram length',len(ngram))
                            x,y = np.asarray(x),np.asarray(y)
                            self.model.fit(x,y,batch_size=mini_batch)
                        depth[len(ngram)-1] = ([],[])
                        if not skipping and len(ngram) > 10 and test_seed is not None:
                            self.generate(test_seed,verbose=True)
                if imsg%1000 == 0 and imsg>0:
                    print('processed',imsg,'messages')
        
    def generate(self,seed='',maxlen=100,temp=0.5,verbose=False):
        if verbose:
            print(seed,end='',flush=True)
        generated = seed
        seed = encode_message(seed,seed=True)
        with self.graph.as_default():
            while len(generated) < maxlen:
                guess = self.model.predict(seed[np.newaxis,:])
                c = sample_state(guess[0],temperature=temp)
                if len(c) == 0:
                    break
                i = c2i(c)
                seed = np.append(seed,i)
                generated += c
                if verbose:
                    print(i2c(i),end='',flush=True)
        if verbose:
            print('\n',end='',flush=True)
        return generated
        
        
        
        
