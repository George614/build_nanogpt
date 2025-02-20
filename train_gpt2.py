#%%
import inspect
import math
from dataclasses import dataclass
import time
import torch
import torch.backends
import torch.nn as nn
import torch.nn.functional as F
import tiktoken 

@dataclass
class GPTConfigSmall:
    block_size: int = 256
    vocab_size: int = 65
    n_layer: int = 6
    n_head: int = 6
    n_embd: int = 384
    
@dataclass
class GPTConfig:
    block_size: int = 1024  # max sequence length
    vocab_size: int = 50257  # number of tokens: 50,000 BPE merges + 256 bytes tokens + 1 <|endoftext|> token
    n_layer: int = 12
    n_head: int = 12
    n_embd: int = 768
    

class MLP(nn.Module):
    def __init__(self, config, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.c_fc = nn.Linear(config.n_embd, 4 * config.n_embd)
        # 'tanh' approximation is to keep the model the same as 
        # the original GPT2 (historically, approximation was needed 
        # to make it run faster, now the exact GELU can be used)
        self.gelu = nn.GELU(approximate="tanh") 
        self.c_proj = nn.Linear(4 * config.n_embd, config.n_embd)
        self.c_proj.NANOGPT_SCALE_INIT = 1
        
    def forward(self, x):
        x = self.c_fc(x)
        x = self.gelu(x)
        x = self.c_proj(x)
        return x

class CausalSelfAttention(nn.Module):
    def __init__(self, config, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        assert config.n_embd % config.n_head == 0
        # key, query and value projections for all heads, but in a batch
        self.c_attn = nn.Linear(config.n_embd, 3 * config.n_embd)
        # output projection
        self.c_proj = nn.Linear(config.n_embd, config.n_embd)
        self.c_proj.NANOGPT_SCALE_INIT = 1
        # regularization
        self.n_head = config.n_head
        self.n_embd = config.n_embd
        # following the OpenAI/HF naming convention, the `bias` here is a mask
        self.register_buffer('bias', torch.tril(torch.ones(config.block_size, config.block_size))
                                    .view(1, 1, config.block_size, config.block_size))
        
    def forward(self, x):
        B, T, C = x.size()
        # calculate q, k, v for all heads in batch and move head forward to be
        # the batch. nh is number of heads, hs is head size, C (#channels) = nh * hs
        # eg. in GPT2 (124M), nh=12, hs=64, C = nh*hs = 768 channels in the Transformer
        qkv = self.c_attn(x)
        q, k, v = qkv.split(self.n_embd, dim=2)
        q = q.view(B, T, self.n_head, C // self.n_head).transpose(1, 2)  # (B, nh, T, hs)
        k = k.view(B, T, self.n_head, C // self.n_head).transpose(1, 2)  # (B, nh, T, hs)
        v = v.view(B, T, self.n_head, C // self.n_head).transpose(1, 2)  # (B, nh, T, hs)
        # self-attention (materializes the large (T, T) matrix for all the queries and keys)
        # att = (q @ k.transpose(-2, -1)) * (1. / math.sqrt(k.size(-1)))
        # att = att.masked_fill(self.bias[:, :, :T, :T] == 0, float('-inf'))
        # att = F.softmax(att, dim=-1)  # (B, nh, T, T)
        # y = att @ v  # (B, nh, T, hs)
        
        # flash attention: 199ms -> 157ms
        y = F.scaled_dot_product_attention(q, k, v, is_causal=True)
        
        y = y.transpose(1, 2).contiguous().view(B, T, C) # re-assemble all head ouputs (concat)
        # output project
        y = self.c_proj(y)
        return y
        

class Block(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.ln_1 = nn.LayerNorm(config.n_embd)
        self.attn = CausalSelfAttention(config)
        self.ln_2 = nn.LayerNorm(config.n_embd)
        self.mlp = MLP(config)
    
    def forward(self, x):
        x = x + self.attn(self.ln_1(x))
        x = x + self.mlp(self.ln_2(x))
        return x


class GPT(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.config = config
        
        self.transformer = nn.ModuleDict(dict(
            wte = nn.Embedding(config.vocab_size, config.n_embd), # token embedding weights
            wpe = nn.Embedding(config.block_size, config.n_embd), # positional embedding weights
            h = nn.ModuleList([Block(config) for _ in range(config.n_layer)]), # attention
            ln_f = nn.LayerNorm(config.n_embd)
        ))
        self.lm_head = nn.Linear(config.n_embd, config.vocab_size, bias=False)
        
        # weight sharing scheme
        self.transformer.wte.weight = self.lm_head.weight
        
        # init params
        self.apply(self._init_weights)
    
    def _init_weights(self, module):
        std = 0.02
        if hasattr(module, 'NANOGPT_SCALE_INIT'):
            std *= (2 * self.config.n_layer) ** -0.5
        if isinstance(module, nn.Linear):
            torch.nn.init.normal_(module.weight, mean=0., std=std)
            if module.bias is not None:
                torch.nn.init.zeros_(module.bias)
        elif isinstance(module, nn.Embedding):
            torch.nn.init.normal_(module.weight, mean=0., std=.02)
        
    def forward(self, idx, targets=None):
        # idx is of shape (B, T)
        B, T = idx.size()
        assert T <= self.config.block_size, f"Cannot forward sequence of length {T}, block size is {self.block_size}"
        # forward the token and position embeddings
        pos = torch.arange(0, T, dtype=torch.long, device=idx.device)
        pos_emb = self.transformer.wpe(pos)  # shape (T, n_embd)
        tok_emb = self.transformer.wte(idx)  # shape (B, T, n_embd)
        x = pos_emb + tok_emb
        # forward the blocks of transformer
        for block in self.transformer.h:
            x = block(x)
        # forward the final layernorm and the classifier
        x = self.transformer.ln_f(x)
        logits = self.lm_head(x)  # (B, T, vocab_size)
        loss = None
        if targets is not None:
            loss = F.cross_entropy(logits.view(-1, logits.size(-1)), targets.view(-1))
        return logits, loss
    
    @classmethod
    def from_pretrained(cls, model_type):
        """Loads pretrained GPT-2 model weights from HuggingFace"""
        assert model_type in {'gpt2', 'gpt2-medium', 'gpt2-large', 'gpt2-xl'}
        from transformers import GPT2LMHeadModel
        print(f"Loading weights from pretrained GPT: {model_type}")
        
        # n_layer, n_head, and n_embd are determined from model_type
        config_args = {
            'gpt2':         dict(n_layer=12, n_head=12, n_embd=768),  # 124M params
            'gpt2-medium':  dict(n_layer=24, n_head=16, n_embd=1024),  # 350M params
            'gpt2-large':   dict(n_layer=36, n_head=20, n_embd=1280),  # 774M params
            'gpt2-xl':      dict(n_layer=48, n_head=25, n_embd=1600),  # 1558M params
        }[model_type]
        config_args['vocab_size'] = 50257  # always 50257 for GPT model checkpoints
        config_args['block_size'] = 1024 # always 1024 for GPT model checkpoints
        # create a from-scratch initialized minGPT model
        config = GPTConfig(**config_args)
        model = GPT(config)
        sd = model.state_dict()
        sd_keys = sd.keys()
        sd_keys = [k for k in sd_keys if not k.endswith('.attn.bias')] # discard this mask / buffer, not a param

        # init a huggingface/transformers model
        model_hf = GPT2LMHeadModel.from_pretrained(model_type)
        sd_hf = model_hf.state_dict()

        # copy while ensuring all of the parameters are aligned and match in names and shapes
        sd_keys_hf = sd_hf.keys()
        sd_keys_hf = [k for k in sd_keys_hf if not k.endswith('.attn.masked_bias')] # ignore these, just a buffer
        sd_keys_hf = [k for k in sd_keys_hf if not k.endswith('.attn.bias')] # same, just the mask (buffer)
        transposed = ['attn.c_attn.weight', 'attn.c_proj.weight', 'mlp.c_fc.weight', 'mlp.c_proj.weight']
        # basically the openai checkpoints use a "Conv1D" module, but we only want to use a vanilla Linear
        # this means that we have to transpose these weights when we import them
        assert len(sd_keys_hf) == len(sd_keys), f"mismatched keys: {len(sd_keys_hf)} != {len(sd_keys)}"
        for k in sd_keys_hf:
            if any(k.endswith(w) for w in transposed):
                # special treatment for the Conv1D weights we need to transpose
                assert sd_hf[k].shape[::-1] == sd[k].shape
                with torch.no_grad():
                    sd[k].copy_(sd_hf[k].t())
            else:
                # vanilla copy over the other parameters
                assert sd_hf[k].shape == sd[k].shape
                with torch.no_grad():
                    sd[k].copy_(sd_hf[k])

        return model
    
    def configure_optimizers(self, weight_decay, learning_rate, device):
        # start with all of the candidate parameters that require grad
        param_dict = {pn: p for pn, p in self.named_parameters()}
        param_dict = {pn: p for pn, p in param_dict.items() if p.requires_grad}
        # create optim groups. Any params that is 2D will be weight decayed, otherwise no
        # i.e. all weight tensors in matmuls + embeddings decay. All biases and layernorm don't
        decay_params = [p for n, p in param_dict.items() if p.dim() >= 2]
        nodecay_params = [p for n, p in param_dict.items() if p.dim() < 2]
        optim_groups = [
            {'params': decay_params, 'weight_decay': weight_decay},
            {'params': nodecay_params, 'weight_decay': 0.0}
        ]
        num_decay_params = sum(p.numel() for p in decay_params)
        num_nodecay_params = sum(p.numel() for p in nodecay_params)
        print(f"num decayed params tensors: {len(decay_params)} with {num_decay_params:,} params")
        print(f"num non-decayed params tensors: {len(nodecay_params)} with {num_nodecay_params:,} params")
        # create AdamW optimizer and use the fused version if available
        fused_available = 'fused' in inspect.signature(torch.optim.AdamW).parameters
        use_fused = fused_available and 'cuda' in device
        print(f"using fused AdamW: {use_fused}")
        optimizer = torch.optim.AdamW(optim_groups, lr=learning_rate, betas=(0.9, 0.95), eps=1e-8, fused=use_fused)
        return optimizer


class DataLoaderLite:
    def __init__(self, B, T) -> None:
        self.B = B
        self.T = T
        # at init load tokens from disk and store them in memory
        with open('input.txt', 'r') as f:
            text = f.read()
        enc = tiktoken.get_encoding('gpt2')
        tokens = enc.encode(text)
        self.tokens = torch.tensor(tokens)
        print(f"loaded {len(self.tokens)} tokens")
        print(f"1 epoch = {len(self.tokens)//(B*T)} batches")
        # state
        self.current_position = 0
        
    def next_batch(self):
        B, T = self.B, self.T
        buf = self.tokens[self.current_position: self.current_position+B*T+1]
        x = buf[:-1].view(B, T)
        y = buf[1:].view(B, T)
        self.current_position += B*T
        if self.current_position + B*T+1 >= len(self.tokens):
            self.current_position = 0
        return x, y


#%%
####### Test loading from HuggingFace ########
# automatically detect device
device = 'cpu'
if torch.cuda.is_available():
    device = 'cuda'
elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
    device = 'mps'
print(f"Using device {device}")

torch.manual_seed(1337)
if torch.cuda.is_available():
    torch.cuda.manual_seed(1337)

# model = GPT.from_pretrained('gpt2')
# print("load succeeded!")

# predix tokens
enc = tiktoken.get_encoding('gpt2')

#%%
# get a batch of data
with open('input.txt', 'r') as f:
    text = f.read()
text = text[:1000]
tokens = enc.encode(text)
B, T = 4, 32
buf = torch.tensor(tokens[:B*T + 1])
buf = buf.to(device)
x = buf[:-1].view(B, T)
y = buf[1:].view(B, T)

# before this it took around 500ms on RTX 3090
torch.set_float32_matmul_precision('high') 
# after this it took around 396ms

max_lr = 3e-4
min_lr = max_lr * 0.1
warmup_steps = 10
max_steps = 50
def get_lr(it):
    if it < warmup_steps:
        return max_lr * (it + 1) / warmup_steps
    if it > max_steps:
        return min_lr
    # in between, use cosine decay
    decay_ratio = (it - warmup_steps) / (max_steps - warmup_steps)
    assert 0 <= decay_ratio <= 1
    coeff = 0.5 * (1.0 + math.cos(math.pi * decay_ratio))  # start at 1 and goes to 0
    return min_lr  + coeff * (max_lr - min_lr)


# get logits
model = GPT(GPTConfig(vocab_size=50304))  # nice number
# nice number for vocab_size: 157ms -> 149ms
# device = 'cpu'  # override
model.to(device)
# before compile 293ms, after compile 200ms
model = torch.compile(model)

logits, loss = model(x, y)
print(f"logit shape {logits.shape}")
print(loss)

total_batch_size = 524288  # 2**19, roughly 0.5M in number of tokens
B = 8  # micro batch size
T = 1024  # sequence length
assert total_batch_size % (B * T) == 0, "make sure total_batch_size is divisible by B * T"
grad_accum_steps = total_batch_size // (B * T)
print(f"total desired batch size: {total_batch_size}")
print(f"=> calculated gradient accumulation steps: {grad_accum_steps}")


train_loader = DataLoaderLite(B=B, T=T)
# optimizer = torch.optim.AdamW(model.parameters(), lr=3e-4, betas=(0.9, 0.95), eps=1e-8)
# the fused AdamW doesn't show any improments on RTX3090 with torch==2.0.1, pytorch-cuda==11.7
optimizer = model.configure_optimizers(weight_decay=0.1, learning_rate=6e-4, device=device)

# training loop
for step in range(max_steps):
    t0 = time.perf_counter_ns()
    optimizer.zero_grad()
    loss_accum = 0.
    for micro_step in range(grad_accum_steps):
        x, y = train_loader.next_batch()
        x, y = x.to(device), y.to(device)
        with torch.autocast(device_type=device, dtype=torch.bfloat16):
            # casting to bfloat16, 396ms -> 293ms
            logits, loss = model(x, y)
            # import code; code.interact(local=locals())
        loss /=  grad_accum_steps  # add the normalizing constant back since the loss will use the mean reudction of a microbatch
        loss_accum += loss.detach()
        loss.backward()  # always a += operation on the grads
    norm = torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
    # determine and set the learning rate for this iteration
    lr = get_lr(step)
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr
    optimizer.step()
    torch.cuda.synchronize()
    t1 = time.perf_counter_ns()
    dt = (t1 - t0) / 1000000.  # in milisecond
    token_processed = train_loader.B * train_loader.T * grad_accum_steps
    token_per_sec = token_processed / (dt / 1000.)
    print(f"step {step:4d} | loss {loss_accum.item():.6f} | lr {lr:.4e} | norm {norm:.4f} | dt {dt:.2f}ms | token/sec {token_per_sec:.2f}")

import sys; sys.exit(0)

#%%


#%%
# test on generating text given one sentence
model = GPT(GPTConfig())
model.eval()
model.to(device)
max_length = 30
number_return_sequences = 5
tokens = enc.encode("Hello, I'm a language model,")
tokens = torch.tensor(tokens, dtype=torch.long)  # (8,)
tokens = tokens.unsqueeze(0).repeat(number_return_sequences, 1) # (5, 8)
x = tokens.to(device)  # (B, T) or (5, 8)

# generate
torch.manual_seed(42)
torch.cuda.manual_seed(42)
while x.size(1) < max_length:
    with torch.no_grad():
        logits = model(x)  # (B, T, vocab_size)
        # take the logits at the last position
        logits = logits[:, -1, :] # (B, vocab_size)
        # get the probabilities
        probs = F.softmax(logits, dim=-1)
        # do top-k sampling of 50 (HF pipeline default)
        # topk_probs is (5, 50), topk_indices is (5, 50)
        topk_probs, topk_indices = torch.topk(probs, 50, dim=-1)
        # select a token from the top-k probabilities
        idx = torch.multinomial(topk_probs, 1)  # (B, 1) or (5, 1)
        xcol = torch.gather(topk_indices, -1, idx) # (B, 1)
        # append to the sequence
        x = torch.cat((x, xcol), dim=1)

# print the generated text
for i in range(number_return_sequences):
    tokens = x[i, :max_length].tolist()
    decoded = enc.decode(tokens)
    print(">", decoded)