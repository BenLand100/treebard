import numpy as np

from keras.layers import Input, Embedding, LSTM, Dense, Flatten, concatenate
from keras.models import Model, load_model
from keras.callbacks import ModelCheckpoint
from keras.utils import np_utils

def c2i(c):
    i = ord(c)
    return i-32 if i >= 32 and i <= 126 else None

def i2c(i):
    return chr(i+32) if i >= 0 and i <= 94 else ''
        
def get_text(ngram):
    return ''.join([i2c(i) for i in ngram])

def i2state(i):
    state = np.zeros(97)
    state[i] = 1
    return state
    
def state2i(i):
    return np.argmax(i)
    
def extract_ngram(vec,i,length,val):
    if i+length <= len(vec):
        return vec[i:i+length]
    else:
        return vec[i:]+[val]*(length-len(vec)+i)
    
def ngram_iter(text,ngram_size=None,stride=1):
    if ngram_size is None:
        ints = [95]+[c2i(c) for c in text if c2i(c) is not None]
        yield ints
    else:
        ints = [95]*(ngram_size-1)+[c2i(c) for c in text if c2i(c) is not None]
        yield from (extract_ngram(ints,i,ngram_size,96) for i in range(0,len(text)-ngram_size,stride))
        
class LanguageCenter:
    def __init__(self,vocab_size=97,embedding_space=100,ngram_size=40,lstm_space=700,lstm_depth=2,model='neural.h5'):
        self.model_name = model
        self.vocab_size = vocab_size
        self.embedding_space = embedding_space
        self.ngram_size = ngram_size
        self.lstm_space = lstm_space
        
        ngram_input = Input(shape=(40,), name='ngram_input')
        embedding = Embedding(output_dim=embedding_space,input_dim=vocab_size,input_length=ngram_size)(ngram_input)
        prev_layer = embedding
        lstm_layers = []
        assert lstm_depth > 0, 'need at least one LSTM layer'
        for i in range(lstm_depth):
            prev_layer = LSTM(lstm_space, return_sequences=True, name=('lstm_%i'%i))(prev_layer)
            lstm_layers.append(prev_layer)
        everything = Flatten()(concatenate([embedding]+lstm_layers))
        dense = Dense(lstm_space, name='letter_dense', activation='tanh')(everything)
        output = Dense(vocab_size, name='letter_out', activation='softmax')(dense)
        self.model = Model(inputs=[ngram_input], outputs=[output])
        self.model.compile(loss='categorical_crossentropy', optimizer='adam')
        
    def __getstate__(self):
        state = self.__dict__.copy()
        del state['model']
        self.model.save(self.model_name)
        return state
        
    def __setstate__(self,state):
        self.__dict__.update(state)
        self.model = load_model(self.model_name)
        
    def generate(self,seed='',maxlen=500):
        generated = seed
        seed = [c2i(c) for c in seed]
        if len(seed) < self.ngram_size:
            seed = [95]*(self.ngram_size-len(seed))+seed
        seed = np.asarray(seed)
        seed = seed[-self.ngram_size:]
        while len(generated) < 500:
            guess = self.model.predict(seed[np.newaxis,:])
            i = np.argmax(guess[0])
            seed[:-1] = seed[1:]
            seed[-1] = i
            if i == 96:
                break
            generated += i2c(i)
        return generated
        
        
        
        
